from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gen_automation.db.models import (
    Asset,
    Project,
    Release,
    ReleaseSelection,
    ReleaseVersion,
    ReviewTask,
    SubjectApproval,
)
from gen_automation.domain.canonical import canonical_sha256
from gen_automation.domain.enums import (
    ApprovalStatus,
    AssetKind,
    AssetState,
    ReleasePhase,
    ReviewTaskState,
)
from gen_automation.domain.release_spec import ReleaseSpecification
from gen_automation.domain.storybooks import (
    MAX_STORYBOOK_CHARACTERS,
    StorybookCharacter,
    StorybookContentAssessment,
    StorybookInputMode,
    StorybookPlan,
    StorybookProjectRequest,
    StorybookSelectedSource,
    require_storybook_content_approval,
)
from gen_automation.semantic.storybooks import (
    StorybookPlannerRequest,
    build_storybook_planner_request,
)
from gen_automation.services.compliance import (
    ReleaseApprovalError,
    canonical_source_sha256,
    validate_release_approvals,
)

_RENDERABLE_RELEASE_PHASES = frozenset(
    {
        ReleasePhase.APPROVED,
        ReleasePhase.RENDERING,
        ReleasePhase.READY_TO_PUBLISH,
        ReleasePhase.PUBLISHING,
        ReleasePhase.PUBLISHED,
    }
)
_STORYBOOK_SOURCE_CONTENT_TYPES = frozenset(
    {
        "image/jpeg",
        "image/png",
        "image/webp",
    }
)


class StorybookSourceContextError(ValueError):
    """Base error for resolving server-owned Storybook inputs."""


class StorybookSourceNotFoundError(StorybookSourceContextError):
    pass


class StorybookSourceConflictError(StorybookSourceContextError):
    pass


class StorybookSourceAuthorizationError(StorybookSourceContextError):
    pass


@dataclass(frozen=True, slots=True)
class StorybookSourceContext:
    """Immutable provider context resolved exclusively from authoritative rows."""

    request: StorybookProjectRequest
    planner_request: StorybookPlannerRequest
    selected_sources: tuple[StorybookSelectedSource, ...]
    characters: tuple[StorybookCharacter, ...]
    adult_subject_gate_sha256: str | None
    release_specification_sha256: str | None
    approval_snapshot_sha256: str | None

    @property
    def source_context_sha256(self) -> str:
        return canonical_sha256(
            {
                "schema": "storybook-source-context/v1",
                "request_sha256": self.request.request_sha256,
                "planner_request_sha256": self.planner_request.planner_request_sha256,
                "selected_sources": [
                    source.model_dump(mode="json") for source in self.selected_sources
                ],
                "characters": [character.model_dump(mode="json") for character in self.characters],
                "adult_subject_gate_sha256": self.adult_subject_gate_sha256,
                "release_specification_sha256": self.release_specification_sha256,
                "approval_snapshot_sha256": self.approval_snapshot_sha256,
            }
        )


async def build_storybook_source_context(
    session: AsyncSession,
    *,
    request: StorybookProjectRequest,
) -> StorybookSourceContext:
    """Resolve a request without ever accepting a caller-controlled Asset identity.

    Selected-image requests name immutable ``ReleaseSelection`` rows and one exact
    current ``ReleaseVersion``. Idea-only requests name current subject-approval
    rows. The returned planner request contains only server-owned snapshots.
    """

    project_exists = await session.scalar(
        select(Project.id).where(Project.id == request.project_id).with_for_update(read=True)
    )
    if project_exists is None:
        raise StorybookSourceNotFoundError("storybook project was not found")
    if request.input_mode is StorybookInputMode.SELECTED_IMAGES:
        return await _selected_image_context(session, request=request)
    return await _idea_only_context(session, request=request)


async def validate_storybook_finalization_context(
    session: AsyncSession,
    *,
    plan: StorybookPlan,
    assessment: StorybookContentAssessment,
    source_sha256s: tuple[str, ...],
    expected_profile_sha256: str,
    expected_model: str,
    expected_model_revision: str,
    expected_prompt_sha256: str,
    expected_schema_sha256: str,
) -> None:
    """Re-authorize bindings for an assessment loaded from server persistence.

    This function deliberately does not turn caller-supplied assessment JSON into
    a credential. A future runtime must load the assessment by its persisted job
    identity before calling this boundary; no public finalization route exists in
    the foundation slice.
    """

    try:
        validated_plan = StorybookPlan.model_validate(plan.model_dump(mode="python"))
        validated_assessment = StorybookContentAssessment.model_validate(
            assessment.model_dump(mode="python")
        )
    except (TypeError, ValueError, ValidationError):
        raise StorybookSourceAuthorizationError(
            "storybook finalization inputs are invalid"
        ) from None

    context = await build_storybook_source_context(
        session,
        request=validated_plan.request,
    )
    planned_sources = tuple(
        source for source in (page.source for page in validated_plan.pages) if source is not None
    )
    if (
        validated_plan.characters != context.characters
        or validated_plan.adult_subject_gate_sha256 != context.adult_subject_gate_sha256
        or planned_sources != context.selected_sources
    ):
        raise StorybookSourceAuthorizationError(
            "storybook plan no longer matches its authorized source context"
        )
    try:
        require_storybook_content_approval(
            plan=validated_plan,
            assessment=validated_assessment,
            source_sha256s=source_sha256s,
            expected_profile_sha256=expected_profile_sha256,
            expected_model=expected_model,
            expected_model_revision=expected_model_revision,
            expected_prompt_sha256=expected_prompt_sha256,
            expected_schema_sha256=expected_schema_sha256,
        )
    except ValueError as error:
        raise StorybookSourceAuthorizationError(
            "storybook content assessment does not authorize finalization"
        ) from error
    if validated_plan.request.input_mode is StorybookInputMode.IDEA_ONLY:
        raise StorybookSourceAuthorizationError(
            "idea-only finalization requires persisted generated page sources and assessment"
        )
    raise StorybookSourceAuthorizationError(
        "storybook finalization requires a persisted semantic-gateway assessment"
    )


async def _selected_image_context(
    session: AsyncSession,
    *,
    request: StorybookProjectRequest,
) -> StorybookSourceContext:
    release_version_id = request.selected_release_version_id
    if release_version_id is None:
        raise StorybookSourceConflictError("selected-image request has no release version")

    version_row = (
        await session.execute(
            select(ReleaseVersion, Release)
            .join(Release, Release.id == ReleaseVersion.release_id)
            .where(ReleaseVersion.id == release_version_id)
            .with_for_update(read=True, of=(ReleaseVersion, Release))
        )
    ).one_or_none()
    if version_row is None:
        raise StorybookSourceNotFoundError("selected release version was not found")
    version, release = version_row
    if release.project_id != request.project_id:
        raise StorybookSourceAuthorizationError(
            "selected release does not belong to the requested project"
        )
    if release.current_version_no != version.version_no:
        raise StorybookSourceConflictError("selected release version is no longer current")
    if release.phase not in _RENDERABLE_RELEASE_PHASES:
        raise StorybookSourceConflictError("selected release is not approved for reuse")
    if canonical_sha256(version.specification) != version.specification_sha256:
        raise StorybookSourceConflictError("selected release specification is invalid")
    try:
        specification = ReleaseSpecification.model_validate(version.specification)
    except ValidationError:
        raise StorybookSourceConflictError(
            "selected release specification no longer passes validation"
        ) from None
    try:
        approval_snapshot = await validate_release_approvals(session, specification)
    except ReleaseApprovalError as error:
        raise StorybookSourceAuthorizationError(
            "selected release no longer has current approval evidence"
        ) from error
    characters, adult_subject_gate_sha256 = _characters_from_release_approval(
        specification=specification,
        approval_checks=approval_snapshot.checks,
    )
    selected_sources = await _load_selected_sources(
        session,
        release=release,
        release_version=version,
        selection_ids=request.selected_release_selection_ids,
    )
    planner_request = build_storybook_planner_request(
        request=request,
        characters=characters,
        adult_subject_gate_sha256=adult_subject_gate_sha256,
        selected_sources=selected_sources,
    )
    return StorybookSourceContext(
        request=request,
        planner_request=planner_request,
        selected_sources=selected_sources,
        characters=characters,
        adult_subject_gate_sha256=adult_subject_gate_sha256,
        release_specification_sha256=version.specification_sha256,
        approval_snapshot_sha256=approval_snapshot.sha256,
    )


async def _idea_only_context(
    session: AsyncSession,
    *,
    request: StorybookProjectRequest,
) -> StorybookSourceContext:
    approvals = await _load_current_subject_approvals(
        session,
        approval_ids=request.requested_subject_approval_ids,
    )
    adult_gate = _adult_subject_gate(approvals) if approvals else None
    adult_subject_gate_sha256 = canonical_sha256(adult_gate) if adult_gate is not None else None
    characters = tuple(
        _character_from_subject_approval(approval, index=index)
        for index, approval in enumerate(approvals)
    )
    planner_request = build_storybook_planner_request(
        request=request,
        characters=characters,
        adult_subject_gate_sha256=adult_subject_gate_sha256,
    )
    return StorybookSourceContext(
        request=request,
        planner_request=planner_request,
        selected_sources=(),
        characters=characters,
        adult_subject_gate_sha256=adult_subject_gate_sha256,
        release_specification_sha256=None,
        approval_snapshot_sha256=None,
    )


async def _load_selected_sources(
    session: AsyncSession,
    *,
    release: Release,
    release_version: ReleaseVersion,
    selection_ids: tuple[UUID, ...],
) -> tuple[StorybookSelectedSource, ...]:
    rows = list(
        (
            await session.execute(
                select(ReleaseSelection, Asset, ReviewTask)
                .join(Asset, Asset.id == ReleaseSelection.asset_id)
                .join(ReviewTask, ReviewTask.id == ReleaseSelection.review_task_id)
                .where(ReleaseSelection.id.in_(selection_ids))
                .with_for_update(read=True, of=ReleaseSelection)
            )
        ).all()
    )
    rows_by_id = {selection.id: (selection, asset, review) for selection, asset, review in rows}
    if len(rows_by_id) != len(selection_ids):
        raise StorybookSourceNotFoundError("one or more release selections were not found")

    selected_sources: list[StorybookSelectedSource] = []
    for selection_id in selection_ids:
        selection, asset, review = rows_by_id[selection_id]
        if (
            selection.release_version_id != release_version.id
            or review.release_version_id != release_version.id
            or review.state is not ReviewTaskState.COMPLETED
            or asset.release_id != release.id
        ):
            raise StorybookSourceConflictError(
                "selected images do not belong to the requested release version"
            )
        _require_current_selected_asset(selection, asset)
        try:
            selected_sources.append(
                StorybookSelectedSource(
                    release_selection_id=selection.id,
                    release_version_id=selection.release_version_id,
                    asset_id=selection.asset_id,
                    source_storage_backend=selection.source_storage_backend,
                    source_storage_bucket=selection.source_storage_bucket,
                    source_object_key=selection.source_object_key,
                    source_object_version_id=selection.source_object_version_id,
                    source_sha256=selection.source_sha256,
                    source_content_type=selection.source_content_type,
                    source_image_format=selection.source_image_format,
                    source_width=selection.source_width,
                    source_height=selection.source_height,
                    source_byte_size=selection.source_byte_size,
                )
            )
        except ValidationError:
            raise StorybookSourceConflictError(
                "selected image source snapshot is invalid"
            ) from None
    return tuple(selected_sources)


def _require_current_selected_asset(selection: ReleaseSelection, asset: Asset) -> None:
    if asset.kind is not AssetKind.RAW_MASTER or asset.state is not AssetState.AVAILABLE:
        raise StorybookSourceConflictError("selected image is not an available raw master")
    if selection.source_content_type not in _STORYBOOK_SOURCE_CONTENT_TYPES:
        raise StorybookSourceConflictError("selected image type is not supported")
    expected = (
        selection.source_storage_backend,
        selection.source_storage_bucket,
        selection.source_object_key,
        selection.source_object_version_id,
        selection.source_sha256,
        selection.source_content_type,
        selection.source_image_format,
        selection.source_width,
        selection.source_height,
        selection.source_byte_size,
    )
    current = (
        asset.storage_backend,
        asset.storage_bucket,
        asset.object_key,
        asset.object_version_id,
        asset.sha256,
        asset.content_type,
        asset.image_format,
        asset.width,
        asset.height,
        asset.byte_size,
    )
    if expected != current:
        raise StorybookSourceConflictError(
            "selected image no longer matches its immutable release selection"
        )


def _characters_from_release_approval(
    *,
    specification: ReleaseSpecification,
    approval_checks: dict[str, dict[str, Any]],
) -> tuple[tuple[StorybookCharacter, ...], str]:
    adult_gate = approval_checks.get("adult_subject_gate")
    if not isinstance(adult_gate, dict):
        raise StorybookSourceAuthorizationError("release adult-subject evidence is unavailable")
    raw_subjects = adult_gate.get("subjects")
    if not isinstance(raw_subjects, list) or len(raw_subjects) != len(specification.subjects):
        raise StorybookSourceAuthorizationError("release adult-subject evidence is invalid")
    if len(raw_subjects) > MAX_STORYBOOK_CHARACTERS:
        raise StorybookSourceConflictError("release cast exceeds the Storybook character limit")

    characters: list[StorybookCharacter] = []
    for index, (subject, raw_evidence) in enumerate(
        zip(specification.subjects, raw_subjects, strict=True)
    ):
        if not isinstance(raw_evidence, dict):
            raise StorybookSourceAuthorizationError("release adult-subject evidence is invalid")
        try:
            characters.append(
                StorybookCharacter(
                    key=_character_key(index),
                    display_name=subject.name,
                    subject_approval_id=UUID(str(raw_evidence["approval_id"])),
                    approval_version=int(raw_evidence["approval_version"]),
                    canonical_source_sha256=str(raw_evidence["canonical_source_sha256"]),
                    canonical_age=subject.canonical_age,
                    adult_approval_evidence_sha256=str(raw_evidence["evidence_sha256"]),
                    is_aged_up_minor=False,
                    continuity_description="",
                )
            )
        except (KeyError, TypeError, ValueError, ValidationError):
            raise StorybookSourceAuthorizationError(
                "release adult-subject evidence is invalid"
            ) from None
    return tuple(characters), canonical_sha256(adult_gate)


async def _load_current_subject_approvals(
    session: AsyncSession,
    *,
    approval_ids: tuple[UUID, ...],
) -> tuple[SubjectApproval, ...]:
    if not approval_ids:
        return ()
    rows = list(
        (
            await session.scalars(
                select(SubjectApproval)
                .where(SubjectApproval.id.in_(approval_ids))
                .with_for_update(read=True)
            )
        ).all()
    )
    rows_by_id = {row.id: row for row in rows}
    if len(rows_by_id) != len(approval_ids):
        raise StorybookSourceAuthorizationError(
            "one or more requested subjects are not currently approved"
        )
    ordered = tuple(rows_by_id[approval_id] for approval_id in approval_ids)
    for approval in ordered:
        _require_current_subject_approval(approval)
    return ordered


def _require_current_subject_approval(approval: SubjectApproval) -> None:
    if (
        approval.status is not ApprovalStatus.APPROVED
        or not approval.is_current
        or approval.revoked_at is not None
        or approval.revoked_by_user_id is not None
        or not approval.clearly_adult
        or approval.canonical_age < 18
        or not approval.is_fictional
        or approval.is_aged_up_minor
        or not approval.distribution_rights_approved
        or not approval.adult_derivative_rights_approved
        or approval.canonical_source_sha256
        != canonical_source_sha256(approval.canonical_source_url)
        or approval.evidence_sha256 != canonical_sha256(approval.evidence)
    ):
        raise StorybookSourceAuthorizationError(
            "one or more requested subjects are not currently approved"
        )


def _character_from_subject_approval(
    approval: SubjectApproval,
    *,
    index: int,
) -> StorybookCharacter:
    try:
        return StorybookCharacter(
            key=_character_key(index),
            display_name=approval.display_name,
            subject_approval_id=approval.id,
            approval_version=approval.approval_version,
            canonical_source_sha256=approval.canonical_source_sha256,
            canonical_age=approval.canonical_age,
            adult_approval_evidence_sha256=approval.evidence_sha256,
            is_aged_up_minor=False,
            continuity_description="",
        )
    except ValidationError:
        raise StorybookSourceAuthorizationError(
            "requested subject cannot be represented by the Storybook contract"
        ) from None


def _adult_subject_gate(approvals: tuple[SubjectApproval, ...]) -> dict[str, object]:
    return {
        "gate_version": 1,
        "subjects": [
            {
                "approval_id": str(approval.id),
                "approval_version": approval.approval_version,
                "approved_by_user_id": str(approval.approved_by_user_id),
                "approved_at": approval.approved_at.isoformat(),
                "canonical_source_sha256": approval.canonical_source_sha256,
                "evidence_sha256": approval.evidence_sha256,
            }
            for approval in approvals
        ],
    }


def _character_key(index: int) -> str:
    return f"character_{index + 1}"
