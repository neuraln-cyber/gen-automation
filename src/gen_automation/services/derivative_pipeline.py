import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from gen_automation.db.models import (
    AdminUser,
    Asset,
    AssetLineage,
    AuditEvent,
    DerivativeJob,
    DerivativeOutput,
    DerivativeRecipe,
    IdempotencyRecord,
    OutboxEvent,
    Release,
    ReleaseSelection,
    ReleaseVersion,
    ReviewTask,
    ReviewXSelection,
    XTeaserRevisionHead,
)
from gen_automation.domain.canonical import canonical_json_bytes, canonical_sha256
from gen_automation.domain.deliverability import (
    DeliverabilityError,
    patreon_full_output_byte_budget,
)
from gen_automation.domain.enums import (
    AdminRole,
    AssetKind,
    AssetState,
    DerivativeJobState,
    OutboxStatus,
    ReleasePhase,
    ReviewTaskState,
)
from gen_automation.domain.ids import uuid7
from gen_automation.services.watermarks import is_registered_watermark

_SHA256 = re.compile(r"[0-9a-f]{64}")
_TARGET = re.compile(r"[a-z][a-z0-9_-]{0,49}")
_CODE = re.compile(r"[a-z][a-z0-9_.-]{0,99}")
_MAX_CONFIGURATION_BYTES = 64 * 1024
_MAX_ERROR_DETAIL = 4_000
_FULL_TARGET = "full"
_X_TARGET = "x_teaser"
_SUPPORTED_TARGETS = frozenset({_FULL_TARGET, _X_TARGET})
_TERMINAL_STATES = frozenset(
    {
        DerivativeJobState.SUCCEEDED,
        DerivativeJobState.FAILED,
        DerivativeJobState.CANCELLED,
    }
)


def current_release_gating_job_predicate() -> ColumnElement[bool]:
    """Keep full jobs and only the X revision owned by the current head gating."""

    exact_x_revision_job = (
        select(XTeaserRevisionHead.id)
        .join(
            ReleaseSelection,
            ReleaseSelection.review_task_id == XTeaserRevisionHead.review_task_id,
        )
        .where(
            ReleaseSelection.id == DerivativeJob.release_selection_id,
            XTeaserRevisionHead.release_version_id == DerivativeJob.release_version_id,
            or_(
                XTeaserRevisionHead.active_revision_id == DerivativeJob.x_teaser_revision_id,
                XTeaserRevisionHead.pending_revision_id == DerivativeJob.x_teaser_revision_id,
            ),
        )
        .exists()
    )
    any_x_revision_head = (
        select(XTeaserRevisionHead.id)
        .join(
            ReleaseSelection,
            ReleaseSelection.review_task_id == XTeaserRevisionHead.review_task_id,
        )
        .where(
            ReleaseSelection.id == DerivativeJob.release_selection_id,
            XTeaserRevisionHead.release_version_id == DerivativeJob.release_version_id,
        )
        .exists()
    )
    legacy_job_has_full_target = (
        select(DerivativeRecipe.id)
        .where(
            DerivativeRecipe.id == DerivativeJob.derivative_recipe_id,
            or_(
                DerivativeRecipe.output_targets == [_FULL_TARGET],
                DerivativeRecipe.output_targets == [_FULL_TARGET, _X_TARGET],
                DerivativeRecipe.output_targets == [_X_TARGET, _FULL_TARGET],
            ),
        )
        .exists()
    )
    return or_(
        and_(
            DerivativeJob.x_teaser_revision_id.is_(None),
            or_(legacy_job_has_full_target, ~any_x_revision_head),
        ),
        and_(
            DerivativeJob.x_teaser_revision_id.is_not(None),
            exact_x_revision_job,
        ),
    )


class DerivativePipelineError(Exception):
    """Base error for durable derivative planning and job state."""


class DerivativePipelineNotFoundError(DerivativePipelineError):
    pass


class DerivativePipelineConflictError(DerivativePipelineError):
    pass


class DerivativePipelineInputError(DerivativePipelineError, ValueError):
    pass


class DerivativePipelineLeaseError(DerivativePipelineConflictError):
    pass


@dataclass(frozen=True, slots=True)
class DerivativePlanResult:
    review_task_id: UUID
    recipe_id: UUID
    release_version_id: UUID
    job_ids: tuple[UUID, ...]
    jobs_created: int
    total_jobs: int
    replayed: bool


@dataclass(frozen=True, slots=True)
class ClaimedDerivativeJob:
    job_id: UUID
    release_selection_id: UUID
    derivative_recipe_id: UUID
    request_payload: dict[str, Any]
    request_sha256: str
    attempt_count: int
    lock_version: int
    lease_expires_at: datetime


@dataclass(frozen=True, slots=True)
class DerivativeJobResult:
    job_id: UUID
    state: DerivativeJobState
    attempt_count: int
    lock_version: int
    retry_at: datetime | None
    completed_at: datetime | None


@dataclass(frozen=True, slots=True)
class DerivativeOutputResult:
    output_id: UUID
    job_id: UUID
    target: str
    asset_id: UUID
    lineage_id: UUID
    replayed: bool


@dataclass(frozen=True, slots=True)
class DerivativeRetryResult:
    review_task_id: UUID
    release_version_id: UUID
    job_ids: tuple[UUID, ...]
    retried_job_ids: tuple[UUID, ...]
    failed_jobs_found: int
    jobs_retried: int
    replayed: bool


async def create_derivative_recipe_and_plan(
    session: AsyncSession,
    *,
    review_task_id: UUID,
    configuration: Mapping[str, Any],
    recipe_version: int,
    renderer_version: str,
    pillow_version: str,
    created_by_user_id: UUID,
    approved_by_user_id: UUID,
    idempotency_key: str,
    output_targets: Sequence[str] = ("full",),
    watermark_asset_id: UUID | None = None,
    x_teaser_asset_ids: Sequence[UUID] | None = None,
    x_teaser_revision_id: UUID | None = None,
    max_attempts: int = 3,
    priority: int = 100,
    gates_release: bool = True,
    commit: bool = True,
    now: datetime | None = None,
) -> DerivativePlanResult:
    """Approve immutable recipes and plan target-isolated jobs for each selection."""

    normalized_configuration = _normalize_configuration(configuration)
    normalized_targets = _normalize_targets(output_targets)
    normalized_x_teaser_asset_ids = _normalize_x_teaser_asset_ids(x_teaser_asset_ids)
    if normalized_x_teaser_asset_ids is not None and _X_TARGET not in normalized_targets:
        raise DerivativePipelineInputError(
            "X teaser asset filtering requires the X teaser output target"
        )
    if x_teaser_revision_id is not None and normalized_targets != (_X_TARGET,):
        raise DerivativePipelineInputError("X teaser revisions may only plan the X teaser target")
    if _X_TARGET in normalized_targets and x_teaser_revision_id is None:
        raise DerivativePipelineInputError(
            "X teaser derivative work requires an owned X teaser revision"
        )
    if not gates_release and x_teaser_revision_id is None:
        raise DerivativePipelineInputError(
            "non-gating derivative work requires an X teaser revision"
        )
    normalized_renderer = _bounded_text(renderer_version, "renderer version", 100)
    normalized_pillow = _bounded_text(pillow_version, "Pillow version", 50)
    normalized_key = _bounded_text(idempotency_key, "idempotency key", 200)
    normalized_recipe_version = _bounded_int(
        recipe_version,
        "recipe version",
        minimum=1,
        maximum=1_000_000,
    )
    normalized_max_attempts = _bounded_int(
        max_attempts,
        "maximum attempts",
        minimum=1,
        maximum=100,
    )
    normalized_priority = _bounded_int(
        priority,
        "priority",
        minimum=-1_000_000,
        maximum=1_000_000,
    )
    planned_at = _as_utc(now or datetime.now(UTC))
    await _require_active_planner(session, created_by_user_id)
    await _require_active_planner(session, approved_by_user_id)

    if not commit:
        try:
            # A caller-owned revision transaction must survive a planner
            # uniqueness race.  Limit any rollback to this savepoint; never
            # silently erase the pending revision/head reservation.
            async with session.begin_nested():
                return await _create_plan_once(
                    session,
                    review_task_id=review_task_id,
                    configuration=normalized_configuration,
                    recipe_version=normalized_recipe_version,
                    renderer_version=normalized_renderer,
                    pillow_version=normalized_pillow,
                    created_by_user_id=created_by_user_id,
                    approved_by_user_id=approved_by_user_id,
                    idempotency_key=normalized_key,
                    output_targets=normalized_targets,
                    watermark_asset_id=watermark_asset_id,
                    x_teaser_asset_ids=normalized_x_teaser_asset_ids,
                    x_teaser_revision_id=x_teaser_revision_id,
                    max_attempts=normalized_max_attempts,
                    priority=normalized_priority,
                    gates_release=gates_release,
                    commit=False,
                    planned_at=planned_at,
                )
        except IntegrityError as error:
            raise DerivativePipelineConflictError(
                "derivative plan was created concurrently"
            ) from error

    for attempt in range(2):
        try:
            return await _create_plan_once(
                session,
                review_task_id=review_task_id,
                configuration=normalized_configuration,
                recipe_version=normalized_recipe_version,
                renderer_version=normalized_renderer,
                pillow_version=normalized_pillow,
                created_by_user_id=created_by_user_id,
                approved_by_user_id=approved_by_user_id,
                idempotency_key=normalized_key,
                output_targets=normalized_targets,
                watermark_asset_id=watermark_asset_id,
                x_teaser_asset_ids=normalized_x_teaser_asset_ids,
                x_teaser_revision_id=x_teaser_revision_id,
                max_attempts=normalized_max_attempts,
                priority=normalized_priority,
                gates_release=gates_release,
                commit=True,
                planned_at=planned_at,
            )
        except IntegrityError as error:
            await session.rollback()
            if attempt:
                raise DerivativePipelineConflictError(
                    "derivative plan was created concurrently"
                ) from error
    raise AssertionError("unreachable derivative plan retry")


async def _create_plan_once(
    session: AsyncSession,
    *,
    review_task_id: UUID,
    configuration: dict[str, Any],
    recipe_version: int,
    renderer_version: str,
    pillow_version: str,
    created_by_user_id: UUID,
    approved_by_user_id: UUID,
    idempotency_key: str,
    output_targets: tuple[str, ...],
    watermark_asset_id: UUID | None,
    x_teaser_asset_ids: frozenset[UUID] | None,
    x_teaser_revision_id: UUID | None,
    max_attempts: int,
    priority: int,
    gates_release: bool,
    commit: bool,
    planned_at: datetime,
) -> DerivativePlanResult:
    task = await session.scalar(
        select(ReviewTask).where(ReviewTask.id == review_task_id).with_for_update()
    )
    if task is None:
        raise DerivativePipelineNotFoundError("review task was not found")
    if task.state != ReviewTaskState.COMPLETED:
        raise DerivativePipelineConflictError(
            "derivative planning requires a completed review task"
        )
    selections = await _load_and_validate_selections(session, task)
    try:
        full_output_byte_budget = patreon_full_output_byte_budget(len(selections))
    except DeliverabilityError as error:
        raise DerivativePipelineConflictError(str(error)) from None
    plans_x_teasers = _X_TARGET in output_targets
    frozen_x_selected_asset_ids = (
        await _load_x_selected_asset_ids(
            session,
            task=task,
            selections=selections,
        )
        if plans_x_teasers
        else frozenset()
    )
    if x_teaser_asset_ids is not None:
        if not x_teaser_asset_ids.issubset(frozen_x_selected_asset_ids):
            raise DerivativePipelineConflictError(
                "requested X teaser images do not match the frozen X selections"
            )
        x_selected_asset_ids = x_teaser_asset_ids
    else:
        x_selected_asset_ids = frozen_x_selected_asset_ids
    if plans_x_teasers and not x_selected_asset_ids and _FULL_TARGET not in output_targets:
        raise DerivativePipelineConflictError("X teaser preparation requires selected X images")
    if x_selected_asset_ids and watermark_asset_id is None:
        raise DerivativePipelineConflictError(
            "selected X images require an approved watermark asset"
        )
    if x_selected_asset_ids and configuration.get("watermark") is None:
        raise DerivativePipelineConflictError(
            "selected X images require a canonical watermark recipe"
        )
    if not plans_x_teasers and (
        watermark_asset_id is not None or configuration.get("watermark") is not None
    ):
        raise DerivativePipelineConflictError(
            "clean full-output preparation does not accept a watermark"
        )
    watermark = (
        await _load_watermark_snapshot(
            session,
            release_version_id=task.release_version_id,
            watermark_asset_id=watermark_asset_id,
        )
        if plans_x_teasers
        else None
    )

    config_sha256 = canonical_sha256(configuration)
    selected_selections = [
        selection for selection in selections if selection.asset_id in x_selected_asset_ids
    ]
    # Keep the clean member copy independent from every destination-specific
    # transform.  In particular, a watermark or teaser failure must not make
    # the already-approved full set unavailable for its owner.
    target_groups: list[tuple[tuple[str, ...], list[ReleaseSelection]]] = []
    if _FULL_TARGET in output_targets:
        target_groups.append(((_FULL_TARGET,), selections))
    if _X_TARGET in output_targets and selected_selections:
        target_groups.append(((_X_TARGET,), selected_selections))
    recipe_plans: list[
        tuple[
            tuple[str, ...],
            list[ReleaseSelection],
            dict[str, Any],
            str,
        ]
    ] = []
    for group_targets, group_selections in target_groups:
        identity = {
            "schema": "derivative-recipe-identity/v1",
            "release_version_id": str(task.release_version_id),
            "recipe_version": recipe_version,
            "config_sha256": config_sha256,
            "output_targets": list(group_targets),
            "renderer_version": renderer_version,
            "pillow_version": pillow_version,
            "watermark": watermark,
        }
        recipe_plans.append(
            (
                group_targets,
                group_selections,
                identity,
                canonical_sha256(identity),
            )
        )
    scope = f"review-task:{task.id}:derivative-plan"
    request_sha256 = canonical_sha256(
        {
            "schema": "derivative-plan-request/v1",
            "review_task_id": str(task.id),
            "recipes": [
                {
                    "logical_key": logical_key,
                    "selection_ids": [str(selection.id) for selection in group_selections],
                }
                for _, group_selections, _, logical_key in recipe_plans
            ],
            "created_by_user_id": str(created_by_user_id),
            "approved_by_user_id": str(approved_by_user_id),
            "max_attempts": max_attempts,
            "priority": priority,
            "x_selected_asset_ids": sorted(str(asset_id) for asset_id in x_selected_asset_ids),
            "x_teaser_revision_id": (
                str(x_teaser_revision_id) if x_teaser_revision_id is not None else None
            ),
            "gates_release": gates_release,
        }
    )
    replay = await _plan_replay(
        session,
        scope=scope,
        idempotency_key=idempotency_key,
        request_sha256=request_sha256,
    )
    if replay is not None:
        return replay

    release_version = await session.scalar(
        select(ReleaseVersion).where(ReleaseVersion.id == task.release_version_id).with_for_update()
    )
    if release_version is None:
        raise DerivativePipelineConflictError("derivative plan release version is unavailable")
    release = await session.scalar(
        select(Release).where(Release.id == release_version.release_id).with_for_update()
    )
    if release is None:
        raise DerivativePipelineConflictError("derivative plan release is unavailable")
    allowed_phases = {
        ReleasePhase.APPROVED,
        ReleasePhase.RENDERING,
        ReleasePhase.READY_TO_PUBLISH,
    }
    if not gates_release:
        allowed_phases.update({ReleasePhase.PUBLISHING, ReleasePhase.PUBLISHED})
    if (
        release.current_version_no != release_version.version_no
        or release.phase not in allowed_phases
    ):
        raise DerivativePipelineConflictError(
            "derivative plan is stale or the release phase does not allow rendering"
        )
    resume_before_new_job = gates_release and release.phase == ReleasePhase.READY_TO_PUBLISH
    if release.phase == ReleasePhase.APPROVED and not gates_release:
        raise DerivativePipelineConflictError(
            "replacement X teaser work requires an already rendered release"
        )
    if release.phase == ReleasePhase.APPROVED:
        await _transition_release_to_rendering(
            session,
            release=release,
            release_version=release_version,
            task=task,
            approved_by_user_id=approved_by_user_id,
            idempotency_key=idempotency_key,
            planned_at=planned_at,
            resumed=False,
        )

    target_owners: dict[tuple[UUID, str, UUID | None], tuple[DerivativeJob, DerivativeRecipe]] = {}
    existing_target_rows = (
        await session.execute(
            select(DerivativeJob, DerivativeRecipe)
            .join(DerivativeRecipe, DerivativeRecipe.id == DerivativeJob.derivative_recipe_id)
            .where(DerivativeJob.release_version_id == task.release_version_id)
        )
    ).all()
    for existing_job, existing_recipe in existing_target_rows:
        for target in _stored_targets(existing_recipe):
            owner_key = (
                existing_job.release_selection_id,
                target,
                existing_job.x_teaser_revision_id if target == _X_TARGET else None,
            )
            previous = target_owners.get(owner_key)
            if previous is not None and previous[0].id != existing_job.id:
                raise DerivativePipelineConflictError(
                    "multiple derivative jobs already own the same selection target"
                )
            target_owners[owner_key] = (existing_job, existing_recipe)

    recipes: list[DerivativeRecipe] = []
    job_ids: list[UUID] = []
    job_order: list[tuple[int, int, UUID]] = []
    jobs_created = 0
    recipes_created = 0
    for group_order, (
        group_targets,
        group_selections,
        recipe_identity,
        recipe_logical_key,
    ) in enumerate(recipe_plans):
        group_target = group_targets[0]
        for selection in group_selections:
            target_owner = target_owners.get(
                (
                    selection.id,
                    group_target,
                    x_teaser_revision_id if group_target == _X_TARGET else None,
                )
            )
            if target_owner is not None and target_owner[1].logical_key != recipe_logical_key:
                raise DerivativePipelineConflictError(
                    "the derivative selection target already has a different frozen recipe"
                )
        # Full outputs are the owner's durable set and should drain before
        # destination-only work even when every job shares the same caller
        # priority and request timestamp.
        group_available_at = planned_at + timedelta(microseconds=group_order)
        recipe = await session.scalar(
            select(DerivativeRecipe).where(DerivativeRecipe.logical_key == recipe_logical_key)
        )
        if recipe is None:
            recipe = DerivativeRecipe(
                id=uuid7(),
                release_version_id=task.release_version_id,
                logical_key=recipe_logical_key,
                recipe_version=recipe_version,
                configuration=configuration,
                config_sha256=config_sha256,
                output_targets=list(group_targets),
                expected_output_count=len(group_targets),
                renderer_version=renderer_version,
                pillow_version=pillow_version,
                watermark_asset_id=watermark_asset_id,
                watermark_storage_backend=_optional_string(
                    watermark,
                    "storage_backend",
                ),
                watermark_storage_bucket=_optional_string(
                    watermark,
                    "storage_bucket",
                ),
                watermark_object_key=_optional_string(watermark, "object_key"),
                watermark_object_version_id=_optional_string(
                    watermark,
                    "object_version_id",
                ),
                watermark_sha256=_optional_string(watermark, "sha256"),
                watermark_content_type=_optional_string(watermark, "content_type"),
                watermark_image_format=_optional_string(watermark, "image_format"),
                watermark_width=_optional_int(watermark, "width"),
                watermark_height=_optional_int(watermark, "height"),
                watermark_byte_size=_optional_int(watermark, "byte_size"),
                created_by_user_id=created_by_user_id,
                created_at=planned_at,
                approved_by_user_id=approved_by_user_id,
                approved_at=planned_at,
            )
            session.add(recipe)
            await session.flush()
            recipes_created += 1
            session.add(
                _audit(
                    actor=f"admin:{approved_by_user_id}",
                    action="derivative.recipe_approved",
                    resource_type="derivative_recipe",
                    resource_id=recipe.id,
                    correlation_id=idempotency_key,
                    detail={
                        "release_version_id": str(task.release_version_id),
                        "logical_key": recipe.logical_key,
                        "config_sha256": recipe.config_sha256,
                        "recipe_version": recipe.recipe_version,
                        "output_targets": list(group_targets),
                        "created_by_user_id": str(created_by_user_id),
                        "approved_by_user_id": str(approved_by_user_id),
                        "watermark_asset_id": (
                            str(watermark_asset_id) if watermark_asset_id is not None else None
                        ),
                    },
                    occurred_at=planned_at,
                )
            )
        else:
            _validate_existing_recipe(
                recipe,
                release_version_id=task.release_version_id,
                recipe_identity=recipe_identity,
                configuration=configuration,
                output_targets=group_targets,
            )
        recipes.append(recipe)

        existing_jobs = list(
            (
                await session.scalars(
                    select(DerivativeJob)
                    .where(DerivativeJob.derivative_recipe_id == recipe.id)
                    .order_by(DerivativeJob.logical_key)
                )
            ).all()
        )
        jobs_by_key = {job.logical_key: job for job in existing_jobs}
        for selection in group_selections:
            logical_key = canonical_sha256(
                {
                    "schema": "derivative-job-identity/v1",
                    "release_selection_id": str(selection.id),
                    "recipe_logical_key": recipe.logical_key,
                    "output_targets": list(group_targets),
                    "x_teaser_revision_id": (
                        str(x_teaser_revision_id) if x_teaser_revision_id is not None else None
                    ),
                    "gates_release": gates_release,
                }
            )
            request_payload = _job_request_payload(
                task=task,
                selection=selection,
                recipe=recipe,
                output_targets=group_targets,
                full_output_byte_budget=full_output_byte_budget,
            )
            job_request_sha256 = canonical_sha256(request_payload)
            existing = jobs_by_key.get(logical_key)
            if existing is not None:
                if (
                    existing.release_selection_id != selection.id
                    or existing.release_version_id != task.release_version_id
                    or existing.x_teaser_revision_id != x_teaser_revision_id
                    or existing.gates_release != gates_release
                    or existing.request_sha256 != job_request_sha256
                    or existing.request_payload != request_payload
                    or existing.expected_output_count != len(group_targets)
                    or existing.max_attempts != max_attempts
                    or existing.priority != priority
                ):
                    raise DerivativePipelineConflictError(
                        "existing derivative job conflicts with the frozen plan"
                    )
                job_ids.append(existing.id)
                job_order.append((selection.display_order, group_order, existing.id))
                continue

            if resume_before_new_job:
                await _transition_release_to_rendering(
                    session,
                    release=release,
                    release_version=release_version,
                    task=task,
                    approved_by_user_id=approved_by_user_id,
                    idempotency_key=idempotency_key,
                    planned_at=planned_at,
                    resumed=True,
                )
                resume_before_new_job = False
            job = DerivativeJob(
                id=uuid7(),
                release_selection_id=selection.id,
                derivative_recipe_id=recipe.id,
                x_teaser_revision_id=x_teaser_revision_id,
                gates_release=gates_release,
                release_version_id=task.release_version_id,
                logical_key=logical_key,
                request_payload=request_payload,
                request_sha256=job_request_sha256,
                expected_output_count=len(group_targets),
                state=DerivativeJobState.REQUESTED,
                priority=priority,
                attempt_count=0,
                max_attempts=max_attempts,
                lock_version=1,
                available_at=group_available_at,
                requested_at=planned_at,
            )
            session.add(job)
            job_ids.append(job.id)
            job_order.append((selection.display_order, group_order, job.id))
            jobs_created += 1
            session.add(
                _outbox(
                    topic="derivative.job.requested",
                    dedupe_key=f"derivative.job.requested:{job.id}",
                    correlation_id=idempotency_key,
                    aggregate_type="derivative_job",
                    aggregate_id=job.id,
                    payload={
                        "job_id": str(job.id),
                        "release_selection_id": str(selection.id),
                        "derivative_recipe_id": str(recipe.id),
                        "request_sha256": job_request_sha256,
                    },
                    occurred_at=planned_at,
                )
            )

    await session.flush()
    total_jobs = int(
        await session.scalar(
            select(func.count()).select_from(DerivativeJob).where(DerivativeJob.id.in_(job_ids))
        )
        or 0
    )
    expected_jobs = sum(len(group_selections) for _, group_selections in target_groups)
    if total_jobs != expected_jobs or len(job_ids) != expected_jobs:
        raise DerivativePipelineConflictError(
            "derivative job count conflicts with the frozen target plan"
        )
    ordered_job_ids = tuple(job_id for _display_order, _group_order, job_id in sorted(job_order))
    result = DerivativePlanResult(
        review_task_id=task.id,
        recipe_id=recipes[0].id,
        release_version_id=task.release_version_id,
        job_ids=ordered_job_ids,
        jobs_created=jobs_created,
        total_jobs=total_jobs,
        replayed=False,
    )
    session.add(
        _audit(
            actor=f"admin:{approved_by_user_id}",
            action="derivative.plan_created",
            resource_type="review_task",
            resource_id=task.id,
            correlation_id=idempotency_key,
            detail={
                "recipe_id": str(recipes[0].id),
                "recipe_ids": [str(recipe.id) for recipe in recipes],
                "recipes_created": recipes_created,
                "jobs_created": jobs_created,
                "total_jobs": total_jobs,
                "job_ids": [str(job_id) for job_id in ordered_job_ids],
            },
            occurred_at=planned_at,
        )
    )
    session.add(
        IdempotencyRecord(
            scope=scope,
            idempotency_key=idempotency_key,
            request_sha256=request_sha256,
            response_status=201,
            response_body=_plan_response_body(result),
            created_at=planned_at,
            expires_at=planned_at + timedelta(days=30),
        )
    )
    if commit:
        await session.commit()
    else:
        await session.flush()
    return result


async def _transition_release_to_rendering(
    session: AsyncSession,
    *,
    release: Release,
    release_version: ReleaseVersion,
    task: ReviewTask,
    approved_by_user_id: UUID,
    idempotency_key: str,
    planned_at: datetime,
    resumed: bool,
) -> None:
    expected_phase = ReleasePhase.READY_TO_PUBLISH if resumed else ReleasePhase.APPROVED
    promoted_release_id = await session.scalar(
        update(Release)
        .where(
            Release.id == release.id,
            Release.current_version_no == release_version.version_no,
            Release.phase == expected_phase,
            Release.lock_version == release.lock_version,
        )
        .values(
            phase=ReleasePhase.RENDERING,
            lock_version=Release.lock_version + 1,
        )
        .returning(Release.id)
    )
    if promoted_release_id is None:
        raise DerivativePipelineConflictError(
            "derivative plan release phase compare-and-swap failed"
        )
    await session.refresh(release)
    event_name = (
        "release.derivative_rendering_resumed"
        if resumed
        else "release.derivative_rendering_started"
    )
    resume_identity = canonical_sha256(
        {
            "review_task_id": str(task.id),
            "idempotency_key": idempotency_key,
        }
    )
    dedupe_key = (
        f"release.derivative_rendering_resumed:{resume_identity}"
        if resumed
        else f"release.derivative_rendering_started:{task.id}"
    )
    session.add(
        _audit(
            actor=f"admin:{approved_by_user_id}",
            action=event_name,
            resource_type="release",
            resource_id=release.id,
            correlation_id=idempotency_key,
            detail={
                "review_task_id": str(task.id),
                "release_version_id": str(task.release_version_id),
                "phase": ReleasePhase.RENDERING.value,
            },
            occurred_at=planned_at,
        )
    )
    session.add(
        _outbox(
            topic=event_name,
            dedupe_key=dedupe_key,
            correlation_id=idempotency_key,
            aggregate_type="release",
            aggregate_id=release.id,
            payload={
                "release_id": str(release.id),
                "release_version_id": str(task.release_version_id),
                "review_task_id": str(task.id),
            },
            occurred_at=planned_at,
        )
    )


async def claim_derivative_jobs(
    session: AsyncSession,
    *,
    worker_id: str,
    limit: int = 10,
    lease_seconds: int = 900,
    now: datetime | None = None,
) -> list[ClaimedDerivativeJob]:
    normalized_worker = _bounded_text(worker_id, "worker id", 200)
    normalized_limit = _bounded_int(limit, "claim limit", minimum=1, maximum=100)
    normalized_lease = _bounded_int(
        lease_seconds,
        "lease seconds",
        minimum=30,
        maximum=3_600,
    )
    claimed_at = _as_utc(now or datetime.now(UTC))
    claimable = or_(
        (
            (DerivativeJob.state == DerivativeJobState.REQUESTED)
            & (DerivativeJob.available_at <= claimed_at)
        ),
        (
            (DerivativeJob.state == DerivativeJobState.RETRY_WAIT)
            & (DerivativeJob.retry_at.is_not(None))
            & (DerivativeJob.retry_at <= claimed_at)
        ),
        (
            DerivativeJob.state.in_((DerivativeJobState.CLAIMED, DerivativeJobState.PROCESSING))
            & (DerivativeJob.lease_expires_at.is_not(None))
            & (DerivativeJob.lease_expires_at <= claimed_at)
        ),
    )
    candidate_ids = list(
        (
            await session.scalars(
                select(DerivativeJob.id)
                .where(
                    claimable,
                    DerivativeJob.attempt_count < DerivativeJob.max_attempts,
                )
                .order_by(
                    DerivativeJob.priority,
                    DerivativeJob.available_at,
                    DerivativeJob.id,
                )
                .limit(normalized_limit)
                .with_for_update(skip_locked=True)
            )
        ).all()
    )
    lease_expires_at = claimed_at + timedelta(seconds=normalized_lease)
    claimed: list[ClaimedDerivativeJob] = []
    for job_id in candidate_ids:
        row = (
            await session.execute(
                update(DerivativeJob)
                .where(
                    DerivativeJob.id == job_id,
                    claimable,
                    DerivativeJob.attempt_count < DerivativeJob.max_attempts,
                )
                .values(
                    state=DerivativeJobState.CLAIMED,
                    attempt_count=DerivativeJob.attempt_count + 1,
                    lock_version=DerivativeJob.lock_version + 1,
                    retry_at=None,
                    lease_owner=normalized_worker,
                    lease_expires_at=lease_expires_at,
                    claimed_at=claimed_at,
                    processing_started_at=None,
                    last_error_code=None,
                    last_error_detail=None,
                )
                .returning(DerivativeJob)
            )
        ).scalar_one_or_none()
        if row is None:
            continue
        claimed.append(
            ClaimedDerivativeJob(
                job_id=row.id,
                release_selection_id=row.release_selection_id,
                derivative_recipe_id=row.derivative_recipe_id,
                request_payload=dict(row.request_payload),
                request_sha256=row.request_sha256,
                attempt_count=row.attempt_count,
                lock_version=row.lock_version,
                lease_expires_at=lease_expires_at,
            )
        )
        session.add(
            _audit(
                actor=normalized_worker,
                action="derivative.job_claimed",
                resource_type="derivative_job",
                resource_id=row.id,
                correlation_id=str(row.id),
                detail={
                    "attempt_count": row.attempt_count,
                    "lease_expires_at": lease_expires_at.isoformat(),
                },
                occurred_at=claimed_at,
            )
        )
        session.add(
            _outbox(
                topic="derivative.job.claimed",
                dedupe_key=f"derivative.job.claimed:{row.id}:{row.attempt_count}",
                correlation_id=str(row.id),
                aggregate_type="derivative_job",
                aggregate_id=row.id,
                payload={
                    "job_id": str(row.id),
                    "attempt_count": row.attempt_count,
                    "worker_id": normalized_worker,
                },
                occurred_at=claimed_at,
            )
        )
    await session.commit()
    return claimed


async def start_derivative_job(
    session: AsyncSession,
    *,
    job_id: UUID,
    worker_id: str,
    expected_lock_version: int,
    now: datetime | None = None,
) -> DerivativeJobResult:
    started_at = _as_utc(now or datetime.now(UTC))
    worker = _bounded_text(worker_id, "worker id", 200)
    expected = _positive_lock_version(expected_lock_version)
    job = await _load_job_locked(session, job_id)
    _require_active_lease(
        job,
        worker_id=worker,
        expected_lock_version=expected,
        now=started_at,
        allowed_states=(DerivativeJobState.CLAIMED,),
    )
    job.state = DerivativeJobState.PROCESSING
    job.processing_started_at = started_at
    job.lock_version += 1
    _record_job_transition(
        session,
        job=job,
        actor=worker,
        action="derivative.job_processing",
        occurred_at=started_at,
        detail={"attempt_count": job.attempt_count},
    )
    await session.commit()
    return _job_result(job)


async def retry_derivative_job(
    session: AsyncSession,
    *,
    job_id: UUID,
    worker_id: str,
    expected_lock_version: int,
    retry_at: datetime,
    error_code: str,
    error_detail: str | None = None,
    now: datetime | None = None,
) -> DerivativeJobResult:
    deferred_at = _as_utc(now or datetime.now(UTC))
    normalized_retry = _as_utc(retry_at)
    if normalized_retry <= deferred_at:
        raise DerivativePipelineInputError("retry time must be in the future")
    worker = _bounded_text(worker_id, "worker id", 200)
    expected = _positive_lock_version(expected_lock_version)
    code = _error_code(error_code)
    detail = _error_detail(error_detail)
    job = await _load_job_locked(session, job_id)
    _require_active_lease(
        job,
        worker_id=worker,
        expected_lock_version=expected,
        now=deferred_at,
        allowed_states=(
            DerivativeJobState.CLAIMED,
            DerivativeJobState.PROCESSING,
        ),
    )
    if job.attempt_count >= job.max_attempts:
        raise DerivativePipelineConflictError("derivative job has exhausted its retry attempts")
    job.state = DerivativeJobState.RETRY_WAIT
    job.retry_at = normalized_retry
    job.lease_owner = None
    job.lease_expires_at = None
    job.last_error_code = code
    job.last_error_detail = detail
    job.lock_version += 1
    _record_job_transition(
        session,
        job=job,
        actor=worker,
        action="derivative.job_retry_scheduled",
        occurred_at=deferred_at,
        detail={
            "attempt_count": job.attempt_count,
            "retry_at": normalized_retry.isoformat(),
            "error_code": code,
        },
    )
    await session.commit()
    return _job_result(job)


async def retry_failed_completed_review_target(
    session: AsyncSession,
    *,
    review_task_id: UUID,
    actor_user_id: UUID,
    idempotency_key: str,
    target: str,
    retry_allowance: int = 3,
    expected_failed_job_ids: Sequence[UUID] | None = None,
    now: datetime | None = None,
) -> DerivativeRetryResult:
    """Re-arm only failed jobs for one frozen target of a completed review.

    This is deliberately narrower than a new derivative plan: job, recipe, and
    source identities remain frozen.  A terminal object-identity conflict must
    be investigated instead of being retried against the immutable object key.
    """

    normalized_target = _normalize_target(target)
    if normalized_target != _FULL_TARGET:
        raise DerivativePipelineInputError("owner retry currently supports only full outputs")
    normalized_key = _bounded_text(idempotency_key, "idempotency key", 200)
    normalized_allowance = _bounded_int(
        retry_allowance,
        "retry allowance",
        minimum=1,
        maximum=3,
    )
    retried_at = _as_utc(now or datetime.now(UTC))
    await _require_active_owner(session, actor_user_id)

    task = await session.scalar(
        select(ReviewTask).where(ReviewTask.id == review_task_id).with_for_update()
    )
    if task is None:
        raise DerivativePipelineNotFoundError("review task was not found")
    if task.state != ReviewTaskState.COMPLETED:
        raise DerivativePipelineConflictError("derivative retry requires a completed review task")
    selections = await _load_and_validate_selections(session, task)
    release_version = await session.scalar(
        select(ReleaseVersion).where(ReleaseVersion.id == task.release_version_id).with_for_update()
    )
    if release_version is None:
        raise DerivativePipelineConflictError("derivative retry release version is unavailable")
    release = await session.scalar(
        select(Release).where(Release.id == release_version.release_id).with_for_update()
    )
    if release is None:
        raise DerivativePipelineConflictError("derivative retry release is unavailable")
    if (
        release.current_version_no != release_version.version_no
        or release.phase != ReleasePhase.RENDERING
    ):
        raise DerivativePipelineConflictError(
            "derivative retry requires the current release version to be rendering"
        )

    expected_ids = (
        tuple(sorted(set(expected_failed_job_ids), key=str))
        if expected_failed_job_ids is not None
        else None
    )
    request_sha256 = canonical_sha256(
        {
            "schema": "derivative-owner-retry-request/v1",
            "review_task_id": str(task.id),
            "release_version_id": str(task.release_version_id),
            "actor_user_id": str(actor_user_id),
            "target": normalized_target,
            "retry_allowance": normalized_allowance,
            "expected_failed_job_ids": (
                [str(job_id) for job_id in expected_ids] if expected_ids is not None else None
            ),
        }
    )
    scope = f"review-task:{task.id}:derivative-retry:{normalized_target}"
    replay = await _retry_replay(
        session,
        scope=scope,
        idempotency_key=normalized_key,
        request_sha256=request_sha256,
    )
    if replay is not None:
        return replay

    rows = (
        await session.execute(
            select(DerivativeJob, DerivativeRecipe)
            .join(DerivativeRecipe, DerivativeRecipe.id == DerivativeJob.derivative_recipe_id)
            .where(DerivativeJob.release_version_id == task.release_version_id)
            .order_by(DerivativeJob.logical_key)
            .with_for_update()
        )
    ).all()
    selection_ids = {selection.id for selection in selections}
    full_jobs: list[DerivativeJob] = []
    for job, recipe in rows:
        recipe_targets = _stored_targets(recipe)
        job_targets = _job_targets(job.request_payload, recipe_targets=recipe_targets)
        if job_targets != (_FULL_TARGET,):
            continue
        if (
            recipe_targets != (_FULL_TARGET,)
            or job.release_selection_id not in selection_ids
            or job.release_version_id != task.release_version_id
            or recipe.release_version_id != task.release_version_id
            or job.expected_output_count != 1
        ):
            raise DerivativePipelineConflictError(
                "full-output derivative job does not match the completed review"
            )
        full_jobs.append(job)
    if (
        len(full_jobs) != len(selections)
        or {job.release_selection_id for job in full_jobs} != selection_ids
    ):
        raise DerivativePipelineConflictError(
            "completed review does not have one frozen full-output job per selection"
        )

    failed_jobs = [job for job in full_jobs if job.state == DerivativeJobState.FAILED]
    failed_ids = tuple(sorted((job.id for job in failed_jobs), key=str))
    if expected_ids is not None and failed_ids != expected_ids:
        raise DerivativePipelineConflictError(
            "failed derivative job snapshot changed; reload before retrying"
        )
    if any(job.last_error_code == "output_object_conflict" for job in failed_jobs):
        raise DerivativePipelineConflictError(
            "an immutable derivative output object conflicts with its expected bytes"
        )

    retried_job_ids: list[UUID] = []
    for job in failed_jobs:
        next_max_attempts = min(10, job.max_attempts + normalized_allowance)
        if next_max_attempts <= job.max_attempts:
            raise DerivativePipelineConflictError(
                "derivative job reached the owner retry attempt cap"
            )
        previous_lock_version = job.lock_version
        previous_max_attempts = job.max_attempts
        previous_error_code = job.last_error_code
        updated_id = await session.scalar(
            update(DerivativeJob)
            .where(
                DerivativeJob.id == job.id,
                DerivativeJob.state == DerivativeJobState.FAILED,
                DerivativeJob.lock_version == previous_lock_version,
                DerivativeJob.max_attempts == previous_max_attempts,
                or_(
                    DerivativeJob.last_error_code.is_(None),
                    DerivativeJob.last_error_code != "output_object_conflict",
                ),
            )
            .values(
                state=DerivativeJobState.RETRY_WAIT,
                max_attempts=next_max_attempts,
                retry_at=retried_at,
                lease_owner=None,
                lease_expires_at=None,
                completed_at=None,
                last_error_code=None,
                last_error_detail=None,
                lock_version=previous_lock_version + 1,
            )
            .returning(DerivativeJob.id)
        )
        if updated_id is None:
            raise DerivativePipelineConflictError(
                "failed derivative job changed while the retry was being scheduled"
            )
        await session.refresh(job)
        retried_job_ids.append(job.id)
        _record_job_transition(
            session,
            job=job,
            actor=f"admin:{actor_user_id}",
            action="derivative.job_owner_retry_scheduled",
            occurred_at=retried_at,
            detail={
                "review_task_id": str(task.id),
                "target": normalized_target,
                "attempt_count": job.attempt_count,
                "previous_error_code": previous_error_code,
                "previous_max_attempts": previous_max_attempts,
                "max_attempts": next_max_attempts,
                "retry_at": retried_at.isoformat(),
            },
        )

    ordered_job_ids = tuple(
        job.id
        for job in sorted(
            full_jobs,
            key=lambda item: next(
                selection.display_order
                for selection in selections
                if selection.id == item.release_selection_id
            ),
        )
    )
    result = DerivativeRetryResult(
        review_task_id=task.id,
        release_version_id=task.release_version_id,
        job_ids=ordered_job_ids,
        retried_job_ids=tuple(retried_job_ids),
        failed_jobs_found=len(failed_jobs),
        jobs_retried=len(retried_job_ids),
        replayed=False,
    )
    summary_action = (
        "derivative.full_outputs_retry_requested"
        if retried_job_ids
        else "derivative.full_outputs_retry_noop"
    )
    session.add(
        _audit(
            actor=f"admin:{actor_user_id}",
            action=summary_action,
            resource_type="review_task",
            resource_id=task.id,
            correlation_id=normalized_key,
            detail={
                "release_version_id": str(task.release_version_id),
                "failed_jobs_found": len(failed_jobs),
                "jobs_retried": len(retried_job_ids),
                "retried_job_ids": [str(job_id) for job_id in retried_job_ids],
            },
            occurred_at=retried_at,
        )
    )
    if retried_job_ids:
        session.add(
            _outbox(
                topic="derivative.full_outputs.retry_requested",
                dedupe_key=(
                    "derivative.full_outputs.retry_requested:"
                    f"{canonical_sha256({'review_task_id': str(task.id), 'key': normalized_key})}"
                ),
                correlation_id=normalized_key,
                aggregate_type="review_task",
                aggregate_id=task.id,
                payload={
                    "review_task_id": str(task.id),
                    "release_version_id": str(task.release_version_id),
                    "job_ids": [str(job_id) for job_id in retried_job_ids],
                },
                occurred_at=retried_at,
            )
        )
    session.add(
        IdempotencyRecord(
            scope=scope,
            idempotency_key=normalized_key,
            request_sha256=request_sha256,
            response_status=200,
            response_body=_retry_response_body(result),
            created_at=retried_at,
            expires_at=retried_at + timedelta(days=30),
        )
    )
    await session.commit()
    return result


async def record_derivative_output(
    session: AsyncSession,
    *,
    job_id: UUID,
    target: str,
    asset_id: UUID,
    worker_id: str,
    expected_lock_version: int,
    now: datetime | None = None,
) -> DerivativeOutputResult:
    recorded_at = _as_utc(now or datetime.now(UTC))
    normalized_target = _normalize_target(target)
    worker = _bounded_text(worker_id, "worker id", 200)
    expected = _positive_lock_version(expected_lock_version)
    existing = await session.scalar(
        select(DerivativeOutput).where(
            DerivativeOutput.derivative_job_id == job_id,
            DerivativeOutput.target == normalized_target,
        )
    )
    if existing is not None:
        if existing.asset_id != asset_id:
            raise DerivativePipelineConflictError(
                "derivative target already references another asset"
            )
        return _output_result(existing, replayed=True)

    row = (
        await session.execute(
            select(DerivativeJob, ReleaseSelection, DerivativeRecipe)
            .join(
                ReleaseSelection,
                ReleaseSelection.id == DerivativeJob.release_selection_id,
            )
            .join(
                DerivativeRecipe,
                DerivativeRecipe.id == DerivativeJob.derivative_recipe_id,
            )
            .where(DerivativeJob.id == job_id)
            .with_for_update()
        )
    ).one_or_none()
    if row is None:
        raise DerivativePipelineNotFoundError("derivative job was not found")
    job, selection, recipe = row
    _require_active_lease(
        job,
        worker_id=worker,
        expected_lock_version=expected,
        now=recorded_at,
        allowed_states=(DerivativeJobState.PROCESSING,),
    )
    if normalized_target not in _stored_targets(recipe):
        raise DerivativePipelineConflictError(
            "derivative target is not part of the approved recipe"
        )
    asset = await session.scalar(select(Asset).where(Asset.id == asset_id).with_for_update())
    if asset is None:
        raise DerivativePipelineNotFoundError("derivative asset was not found")
    release_version = await session.get(ReleaseVersion, job.release_version_id)
    if release_version is None:
        raise DerivativePipelineConflictError("derivative job release version is unavailable")
    _validate_available_asset(
        asset,
        expected_kind=AssetKind.DERIVATIVE,
        expected_release_id=release_version.release_id,
        label="derivative asset",
    )
    if asset.id == selection.asset_id:
        raise DerivativePipelineConflictError("derivative output cannot reuse its raw source asset")

    lineage = await session.scalar(
        select(AssetLineage).where(
            AssetLineage.parent_asset_id == selection.asset_id,
            AssetLineage.child_asset_id == asset.id,
            AssetLineage.relation == "derivative",
            AssetLineage.recipe_version == recipe.config_sha256,
        )
    )
    if lineage is None:
        lineage = AssetLineage(
            id=uuid7(),
            parent_asset_id=selection.asset_id,
            child_asset_id=asset.id,
            relation="derivative",
            recipe_version=recipe.config_sha256,
            created_at=recorded_at,
        )
        session.add(lineage)
        await session.flush()

    output = DerivativeOutput(
        id=uuid7(),
        derivative_job_id=job.id,
        release_selection_id=selection.id,
        derivative_recipe_id=recipe.id,
        target=normalized_target,
        asset_id=asset.id,
        source_asset_id=selection.asset_id,
        asset_lineage_id=lineage.id,
        asset_storage_backend=asset.storage_backend,
        asset_storage_bucket=asset.storage_bucket,
        asset_object_key=_required_asset_text(asset.object_key, "object key"),
        asset_object_version_id=_required_asset_text(
            asset.object_version_id,
            "object version",
        ),
        asset_sha256=_required_asset_text(asset.sha256, "SHA-256"),
        asset_content_type=_required_asset_text(asset.content_type, "content type"),
        asset_image_format=_required_asset_text(asset.image_format, "image format"),
        asset_width=_required_asset_int(asset.width, "width"),
        asset_height=_required_asset_int(asset.height, "height"),
        asset_byte_size=_required_asset_int(asset.byte_size, "byte size"),
        lineage_relation=lineage.relation,
        lineage_recipe_version=lineage.recipe_version,
        recorded_by=worker,
        recorded_at=recorded_at,
    )
    session.add(output)
    session.add(
        _audit(
            actor=worker,
            action="derivative.output_recorded",
            resource_type="derivative_output",
            resource_id=output.id,
            correlation_id=str(job.id),
            detail={
                "job_id": str(job.id),
                "target": normalized_target,
                "asset_id": str(asset.id),
                "asset_sha256": asset.sha256,
                "lineage_id": str(lineage.id),
            },
            occurred_at=recorded_at,
        )
    )
    session.add(
        _outbox(
            topic="derivative.output.recorded",
            dedupe_key=f"derivative.output.recorded:{job.id}:{normalized_target}",
            correlation_id=str(job.id),
            aggregate_type="derivative_output",
            aggregate_id=output.id,
            payload={
                "job_id": str(job.id),
                "target": normalized_target,
                "asset_id": str(asset.id),
                "asset_sha256": asset.sha256,
            },
            occurred_at=recorded_at,
        )
    )
    try:
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        existing = await session.scalar(
            select(DerivativeOutput).where(
                DerivativeOutput.derivative_job_id == job_id,
                DerivativeOutput.target == normalized_target,
            )
        )
        if existing is not None and existing.asset_id == asset_id:
            return _output_result(existing, replayed=True)
        raise DerivativePipelineConflictError(
            "derivative output was recorded concurrently"
        ) from error
    return _output_result(output, replayed=False)


async def succeed_derivative_job(
    session: AsyncSession,
    *,
    job_id: UUID,
    worker_id: str,
    expected_lock_version: int,
    now: datetime | None = None,
) -> DerivativeJobResult:
    completed_at = _as_utc(now or datetime.now(UTC))
    worker = _bounded_text(worker_id, "worker id", 200)
    expected = _positive_lock_version(expected_lock_version)
    # Every successful gating job can make its DB trigger lock Release. Take
    # Version first to match publication's Version -> Release order.
    await _prelock_job_release_version(session, job_id=job_id, x_revision_only=False)
    row = (
        await session.execute(
            select(DerivativeJob, DerivativeRecipe)
            .join(
                DerivativeRecipe,
                DerivativeRecipe.id == DerivativeJob.derivative_recipe_id,
            )
            .where(DerivativeJob.id == job_id)
            .with_for_update()
        )
    ).one_or_none()
    if row is None:
        raise DerivativePipelineNotFoundError("derivative job was not found")
    job, recipe = row
    _require_active_lease(
        job,
        worker_id=worker,
        expected_lock_version=expected,
        now=completed_at,
        allowed_states=(DerivativeJobState.PROCESSING,),
    )
    targets = tuple(
        (
            await session.scalars(
                select(DerivativeOutput.target)
                .where(DerivativeOutput.derivative_job_id == job.id)
                .order_by(DerivativeOutput.target)
            )
        ).all()
    )
    expected_targets = _job_targets(
        job.request_payload,
        recipe_targets=_stored_targets(recipe),
    )
    if targets != expected_targets:
        raise DerivativePipelineConflictError(
            "derivative outputs do not satisfy the frozen job targets"
        )
    job.state = DerivativeJobState.SUCCEEDED
    job.lease_owner = None
    job.lease_expires_at = None
    job.completed_at = completed_at
    job.last_error_code = None
    job.last_error_detail = None
    job.lock_version += 1
    _record_job_transition(
        session,
        job=job,
        actor=worker,
        action="derivative.job_succeeded",
        occurred_at=completed_at,
        detail={"output_targets": list(targets)},
    )
    try:
        await session.flush()
    except IntegrityError as error:
        await session.rollback()
        raise DerivativePipelineConflictError(
            "derivative job completion snapshot is invalid"
        ) from error
    if job.x_teaser_revision_id is not None:
        # Imported lazily to keep the revision orchestrator free to reuse the
        # immutable derivative planner without a module import cycle.
        from gen_automation.services.x_teaser_revisions import (
            activate_ready_x_teaser_revision,
        )

        await activate_ready_x_teaser_revision(
            session,
            revision_id=job.x_teaser_revision_id,
            activated_at=completed_at,
        )
    remaining_jobs = int(
        await session.scalar(
            select(func.count())
            .select_from(DerivativeJob)
            .where(
                DerivativeJob.release_version_id == job.release_version_id,
                DerivativeJob.gates_release.is_(True),
                DerivativeJob.state != DerivativeJobState.SUCCEEDED,
                current_release_gating_job_predicate(),
            )
        )
        or 0
    )
    if job.gates_release and remaining_jobs == 0:
        release_version = await session.scalar(
            select(ReleaseVersion)
            .where(ReleaseVersion.id == job.release_version_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if release_version is None:
            await session.rollback()
            raise DerivativePipelineConflictError("release version is unavailable")
        release = await session.scalar(
            select(Release)
            .where(Release.id == release_version.release_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if release is None or release.phase != ReleasePhase.READY_TO_PUBLISH:
            await session.rollback()
            raise DerivativePipelineConflictError("release readiness transition did not complete")
        readiness_dedupe_key = f"release.ready_to_publish:{job.release_version_id}"
        existing_readiness_event = await session.scalar(
            select(OutboxEvent.id).where(
                OutboxEvent.topic == "release.ready_to_publish",
                OutboxEvent.dedupe_key == readiness_dedupe_key,
            )
        )
        if existing_readiness_event is None:
            session.add(
                _audit(
                    actor=worker,
                    action="release.ready_to_publish",
                    resource_type="release",
                    resource_id=release.id,
                    correlation_id=str(job.id),
                    detail={
                        "release_version_id": str(job.release_version_id),
                        "phase": ReleasePhase.READY_TO_PUBLISH.value,
                    },
                    occurred_at=completed_at,
                )
            )
            session.add(
                _outbox(
                    topic="release.ready_to_publish",
                    dedupe_key=readiness_dedupe_key,
                    correlation_id=str(job.id),
                    aggregate_type="release",
                    aggregate_id=release.id,
                    payload={
                        "release_id": str(release.id),
                        "release_version_id": str(job.release_version_id),
                    },
                    occurred_at=completed_at,
                )
            )
    await session.commit()
    return _job_result(job)


async def fail_derivative_job(
    session: AsyncSession,
    *,
    job_id: UUID,
    worker_id: str,
    expected_lock_version: int,
    error_code: str,
    error_detail: str | None = None,
    now: datetime | None = None,
) -> DerivativeJobResult:
    failed_at = _as_utc(now or datetime.now(UTC))
    worker = _bounded_text(worker_id, "worker id", 200)
    expected = _positive_lock_version(expected_lock_version)
    code = _error_code(error_code)
    detail = _error_detail(error_detail)
    await _prelock_job_release_version(session, job_id=job_id, x_revision_only=True)
    job = await _load_job_locked(session, job_id)
    _require_active_lease(
        job,
        worker_id=worker,
        expected_lock_version=expected,
        now=failed_at,
        allowed_states=(
            DerivativeJobState.CLAIMED,
            DerivativeJobState.PROCESSING,
        ),
    )
    job.state = DerivativeJobState.FAILED
    job.retry_at = None
    job.lease_owner = None
    job.lease_expires_at = None
    job.completed_at = failed_at
    job.last_error_code = code
    job.last_error_detail = detail
    job.lock_version += 1
    _record_job_transition(
        session,
        job=job,
        actor=worker,
        action="derivative.job_failed",
        occurred_at=failed_at,
        detail={"error_code": code, "attempt_count": job.attempt_count},
    )
    if job.x_teaser_revision_id is not None:
        from gen_automation.services.x_teaser_revisions import (
            discard_failed_pending_x_teaser_revision,
        )

        await session.flush()
        await discard_failed_pending_x_teaser_revision(
            session,
            revision_id=job.x_teaser_revision_id,
            discarded_at=failed_at,
            actor=worker,
        )
    await session.commit()
    return _job_result(job)


async def expire_exhausted_derivative_job(
    session: AsyncSession,
    *,
    job_id: UUID,
    expected_lock_version: int,
    now: datetime | None = None,
) -> DerivativeJobResult:
    """Fail an exhausted active job only after its processing lease expires."""

    expired_at = _as_utc(now or datetime.now(UTC))
    expected = _positive_lock_version(expected_lock_version)
    await _prelock_job_release_version(session, job_id=job_id, x_revision_only=True)
    job = await _load_job_locked(session, job_id)
    lease_expires_at = _as_utc(job.lease_expires_at) if job.lease_expires_at is not None else None
    if (
        job.state not in (DerivativeJobState.CLAIMED, DerivativeJobState.PROCESSING)
        or lease_expires_at is None
        or lease_expires_at > expired_at
        or job.attempt_count < job.max_attempts
    ):
        raise DerivativePipelineConflictError("derivative job is not an exhausted expired lease")
    if job.lock_version != expected:
        raise DerivativePipelineConflictError("derivative job lock version is stale")
    job.state = DerivativeJobState.FAILED
    job.retry_at = None
    job.lease_owner = None
    job.lease_expires_at = None
    job.completed_at = expired_at
    job.last_error_code = "execution_lease_expired"
    job.last_error_detail = "Derivative execution exhausted its bounded retry attempts."
    job.lock_version += 1
    _record_job_transition(
        session,
        job=job,
        actor="derivative-lease-recovery",
        action="derivative.job_failed",
        occurred_at=expired_at,
        detail={
            "error_code": "execution_lease_expired",
            "attempt_count": job.attempt_count,
        },
    )
    if job.x_teaser_revision_id is not None:
        from gen_automation.services.x_teaser_revisions import (
            discard_failed_pending_x_teaser_revision,
        )

        await session.flush()
        await discard_failed_pending_x_teaser_revision(
            session,
            revision_id=job.x_teaser_revision_id,
            discarded_at=expired_at,
            actor="derivative-lease-recovery",
        )
    await session.commit()
    return _job_result(job)


async def cancel_derivative_job(
    session: AsyncSession,
    *,
    job_id: UUID,
    cancelled_by_user_id: UUID,
    expected_lock_version: int,
    reason_code: str,
    now: datetime | None = None,
) -> DerivativeJobResult:
    cancelled_at = _as_utc(now or datetime.now(UTC))
    expected = _positive_lock_version(expected_lock_version)
    code = _error_code(reason_code)
    await _require_active_planner(session, cancelled_by_user_id)
    await _prelock_job_release_version(session, job_id=job_id, x_revision_only=True)
    job = await _load_job_locked(session, job_id)
    if job.state in _TERMINAL_STATES:
        raise DerivativePipelineConflictError("derivative job is already terminal")
    if job.lock_version != expected:
        raise DerivativePipelineConflictError("derivative job lock version is stale")
    job.state = DerivativeJobState.CANCELLED
    job.retry_at = None
    job.lease_owner = None
    job.lease_expires_at = None
    job.completed_at = cancelled_at
    job.last_error_code = code
    job.last_error_detail = "Derivative job cancelled by an authorized reviewer."
    job.lock_version += 1
    _record_job_transition(
        session,
        job=job,
        actor=f"admin:{cancelled_by_user_id}",
        action="derivative.job_cancelled",
        occurred_at=cancelled_at,
        detail={"reason_code": code},
    )
    if job.x_teaser_revision_id is not None:
        from gen_automation.services.x_teaser_revisions import (
            discard_failed_pending_x_teaser_revision,
        )

        await session.flush()
        await discard_failed_pending_x_teaser_revision(
            session,
            revision_id=job.x_teaser_revision_id,
            discarded_at=cancelled_at,
            actor=f"admin:{cancelled_by_user_id}",
        )
    await session.commit()
    return _job_result(job)


async def _load_and_validate_selections(
    session: AsyncSession,
    task: ReviewTask,
) -> list[ReleaseSelection]:
    rows = list(
        (
            await session.scalars(
                select(ReleaseSelection)
                .where(ReleaseSelection.review_task_id == task.id)
                .order_by(ReleaseSelection.display_order)
            )
        ).all()
    )
    if len(rows) != task.desired_accepted_count or [row.display_order for row in rows] != list(
        range(1, task.desired_accepted_count + 1)
    ):
        raise DerivativePipelineConflictError(
            "completed review task has an incomplete selection snapshot"
        )
    assets = {
        asset.id: asset
        for asset in (
            await session.scalars(
                select(Asset).where(Asset.id.in_([row.asset_id for row in rows])).with_for_update()
            )
        ).all()
    }
    release_version = await session.get(ReleaseVersion, task.release_version_id)
    if release_version is None:
        raise DerivativePipelineConflictError(
            "completed review task release version is unavailable"
        )
    previous_rank = 0
    for selection in rows:
        asset = assets.get(selection.asset_id)
        if (
            selection.release_version_id != task.release_version_id
            or selection.scoring_run_id != task.scoring_run_id
            or selection.ranking_manifest_sha256 != task.ranking_manifest_sha256
            or selection.ranking_rank <= previous_rank
            or asset is None
        ):
            raise DerivativePipelineConflictError(
                "completed review task selection identity changed"
            )
        _validate_available_asset(
            asset,
            expected_kind=AssetKind.RAW_MASTER,
            expected_release_id=release_version.release_id,
            label="selected raw-master source",
        )
        if not _selection_matches_asset(selection, asset):
            raise DerivativePipelineConflictError("selected raw-master storage identity changed")
        previous_rank = selection.ranking_rank
    return rows


async def _load_x_selected_asset_ids(
    session: AsyncSession,
    *,
    task: ReviewTask,
    selections: Sequence[ReleaseSelection],
) -> frozenset[UUID]:
    selected = frozenset(
        (
            await session.scalars(
                select(ReviewXSelection.asset_id).where(ReviewXSelection.review_task_id == task.id)
            )
        ).all()
    )
    accepted = {selection.asset_id for selection in selections}
    if len(selected) > 4 or not selected.issubset(accepted):
        raise DerivativePipelineConflictError(
            "frozen X selections do not match the accepted image set"
        )
    return selected


async def _load_watermark_snapshot(
    session: AsyncSession,
    *,
    release_version_id: UUID,
    watermark_asset_id: UUID | None,
) -> dict[str, Any] | None:
    if watermark_asset_id is None:
        return None
    if await session.get(ReleaseVersion, release_version_id) is None:
        raise DerivativePipelineConflictError("recipe release version is unavailable")
    asset = await session.scalar(
        select(Asset).where(Asset.id == watermark_asset_id).with_for_update()
    )
    if asset is None:
        raise DerivativePipelineNotFoundError("watermark asset was not found")
    _validate_registered_watermark_asset(asset)
    return {
        "asset_id": str(asset.id),
        "storage_backend": asset.storage_backend,
        "storage_bucket": asset.storage_bucket,
        "object_key": _required_asset_text(asset.object_key, "object key"),
        "object_version_id": _required_asset_text(
            asset.object_version_id,
            "object version",
        ),
        "sha256": _required_asset_text(asset.sha256, "SHA-256"),
        "content_type": _required_asset_text(asset.content_type, "content type"),
        "image_format": _required_asset_text(asset.image_format, "image format"),
        "width": _required_asset_int(asset.width, "width"),
        "height": _required_asset_int(asset.height, "height"),
        "byte_size": _required_asset_int(asset.byte_size, "byte size"),
    }


def _validate_registered_watermark_asset(asset: Asset) -> None:
    """Validate a global watermark without weakening release-owned asset checks."""

    if (
        not is_registered_watermark(asset)
        or asset.state != AssetState.AVAILABLE
        or not asset.storage_backend.strip()
        or not asset.storage_bucket.strip()
        or asset.object_key is None
        or not asset.object_key.startswith("watermarks/")
        or asset.object_version_id is None
        or not asset.object_version_id.strip()
        or asset.sha256 is None
        or _SHA256.fullmatch(asset.sha256) is None
        or asset.content_type != "image/png"
        or asset.image_format != "PNG"
        or asset.width is None
        or asset.width <= 0
        or asset.height is None
        or asset.height <= 0
        or asset.byte_size is None
        or asset.byte_size <= 0
        or asset.available_at is None
    ):
        raise DerivativePipelineConflictError(
            "registered watermark asset is unavailable or incomplete"
        )


def _validate_existing_recipe(
    recipe: DerivativeRecipe,
    *,
    release_version_id: UUID,
    recipe_identity: dict[str, Any],
    configuration: dict[str, Any],
    output_targets: tuple[str, ...],
) -> None:
    if (
        recipe.release_version_id != release_version_id
        or recipe.logical_key != canonical_sha256(recipe_identity)
        or recipe.config_sha256 != canonical_sha256(configuration)
        or recipe.configuration != configuration
        or _stored_targets(recipe) != output_targets
        or recipe.expected_output_count != len(output_targets)
    ):
        raise DerivativePipelineConflictError(
            "existing derivative recipe conflicts with its canonical identity"
        )


def _job_request_payload(
    *,
    task: ReviewTask,
    selection: ReleaseSelection,
    recipe: DerivativeRecipe,
    output_targets: tuple[str, ...],
    full_output_byte_budget: int,
) -> dict[str, Any]:
    return {
        "schema": "derivative-job-request/v1",
        "review_task_id": str(task.id),
        "release_version_id": str(selection.release_version_id),
        "release_selection_id": str(selection.id),
        "display_order": selection.display_order,
        "scoring_run_id": str(selection.scoring_run_id),
        "ranking_manifest_sha256": selection.ranking_manifest_sha256,
        "output_targets": list(output_targets),
        "full_output_byte_budget": full_output_byte_budget,
        "source": {
            "asset_id": str(selection.asset_id),
            "storage_backend": selection.source_storage_backend,
            "storage_bucket": selection.source_storage_bucket,
            "object_key": selection.source_object_key,
            "object_version_id": selection.source_object_version_id,
            "sha256": selection.source_sha256,
            "content_type": selection.source_content_type,
            "image_format": selection.source_image_format,
            "width": selection.source_width,
            "height": selection.source_height,
            "byte_size": selection.source_byte_size,
        },
        "recipe": {
            "id": str(recipe.id),
            "logical_key": recipe.logical_key,
            "recipe_version": recipe.recipe_version,
            "config_sha256": recipe.config_sha256,
            "renderer_version": recipe.renderer_version,
            "pillow_version": recipe.pillow_version,
            "output_targets": list(_stored_targets(recipe)),
            "watermark_asset_id": (
                str(recipe.watermark_asset_id) if recipe.watermark_asset_id is not None else None
            ),
            "watermark_sha256": recipe.watermark_sha256,
            "watermark_object_version_id": recipe.watermark_object_version_id,
        },
    }


async def _plan_replay(
    session: AsyncSession,
    *,
    scope: str,
    idempotency_key: str,
    request_sha256: str,
) -> DerivativePlanResult | None:
    record = await session.scalar(
        select(IdempotencyRecord).where(
            IdempotencyRecord.scope == scope,
            IdempotencyRecord.idempotency_key == idempotency_key,
        )
    )
    if record is None:
        return None
    if record.request_sha256 != request_sha256:
        raise DerivativePipelineConflictError(
            "idempotency key was already used for another derivative plan"
        )
    body = record.response_body
    try:
        return DerivativePlanResult(
            review_task_id=UUID(str(body["review_task_id"])),
            recipe_id=UUID(str(body["recipe_id"])),
            release_version_id=UUID(str(body["release_version_id"])),
            job_ids=tuple(UUID(str(value)) for value in body["job_ids"]),
            jobs_created=int(body["jobs_created"]),
            total_jobs=int(body["total_jobs"]),
            replayed=True,
        )
    except (KeyError, TypeError, ValueError):
        raise DerivativePipelineConflictError(
            "derivative plan idempotency response is invalid"
        ) from None


async def _retry_replay(
    session: AsyncSession,
    *,
    scope: str,
    idempotency_key: str,
    request_sha256: str,
) -> DerivativeRetryResult | None:
    record = await session.scalar(
        select(IdempotencyRecord).where(
            IdempotencyRecord.scope == scope,
            IdempotencyRecord.idempotency_key == idempotency_key,
        )
    )
    if record is None:
        return None
    if record.request_sha256 != request_sha256:
        raise DerivativePipelineConflictError(
            "idempotency key was already used for another derivative retry"
        )
    body = record.response_body
    try:
        return DerivativeRetryResult(
            review_task_id=UUID(str(body["review_task_id"])),
            release_version_id=UUID(str(body["release_version_id"])),
            job_ids=tuple(UUID(str(value)) for value in body["job_ids"]),
            retried_job_ids=tuple(UUID(str(value)) for value in body["retried_job_ids"]),
            failed_jobs_found=int(body["failed_jobs_found"]),
            jobs_retried=int(body["jobs_retried"]),
            replayed=True,
        )
    except (KeyError, TypeError, ValueError):
        raise DerivativePipelineConflictError(
            "derivative retry idempotency response is invalid"
        ) from None


def _plan_response_body(result: DerivativePlanResult) -> dict[str, Any]:
    return {
        "review_task_id": str(result.review_task_id),
        "recipe_id": str(result.recipe_id),
        "release_version_id": str(result.release_version_id),
        "job_ids": [str(job_id) for job_id in result.job_ids],
        "jobs_created": result.jobs_created,
        "total_jobs": result.total_jobs,
    }


def _retry_response_body(result: DerivativeRetryResult) -> dict[str, Any]:
    return {
        "review_task_id": str(result.review_task_id),
        "release_version_id": str(result.release_version_id),
        "job_ids": [str(job_id) for job_id in result.job_ids],
        "retried_job_ids": [str(job_id) for job_id in result.retried_job_ids],
        "failed_jobs_found": result.failed_jobs_found,
        "jobs_retried": result.jobs_retried,
    }


async def _prelock_job_release_version(
    session: AsyncSession,
    *,
    job_id: UUID,
    x_revision_only: bool,
) -> None:
    snapshot = (
        await session.execute(
            select(
                DerivativeJob.release_version_id,
                DerivativeJob.x_teaser_revision_id,
            ).where(DerivativeJob.id == job_id)
        )
    ).one_or_none()
    if snapshot is None:
        raise DerivativePipelineNotFoundError("derivative job was not found")
    release_version_id, revision_id = snapshot
    if x_revision_only and revision_id is None:
        return
    version = await session.scalar(
        select(ReleaseVersion).where(ReleaseVersion.id == release_version_id).with_for_update()
    )
    if version is None:
        raise DerivativePipelineConflictError("release version is unavailable")


async def _load_job_locked(
    session: AsyncSession,
    job_id: UUID,
) -> DerivativeJob:
    job = await session.scalar(
        select(DerivativeJob).where(DerivativeJob.id == job_id).with_for_update()
    )
    if job is None:
        raise DerivativePipelineNotFoundError("derivative job was not found")
    return job


def _require_active_lease(
    job: DerivativeJob,
    *,
    worker_id: str,
    expected_lock_version: int,
    now: datetime,
    allowed_states: tuple[DerivativeJobState, ...],
) -> None:
    lease_expires_at = _as_utc(job.lease_expires_at) if job.lease_expires_at is not None else None
    if (
        job.state not in allowed_states
        or job.lease_owner != worker_id
        or lease_expires_at is None
        or lease_expires_at <= now
    ):
        raise DerivativePipelineLeaseError("derivative job lease is not active for this worker")
    if job.lock_version != expected_lock_version:
        raise DerivativePipelineConflictError("derivative job lock version is stale")


def _record_job_transition(
    session: AsyncSession,
    *,
    job: DerivativeJob,
    actor: str,
    action: str,
    occurred_at: datetime,
    detail: dict[str, Any],
) -> None:
    session.add(
        _audit(
            actor=actor,
            action=action,
            resource_type="derivative_job",
            resource_id=job.id,
            correlation_id=str(job.id),
            detail={
                "state": job.state.value,
                "lock_version": job.lock_version,
                **detail,
            },
            occurred_at=occurred_at,
        )
    )
    session.add(
        _outbox(
            topic=action,
            dedupe_key=f"{action}:{job.id}:{job.lock_version}",
            correlation_id=str(job.id),
            aggregate_type="derivative_job",
            aggregate_id=job.id,
            payload={
                "job_id": str(job.id),
                "state": job.state.value,
                "lock_version": job.lock_version,
                **detail,
            },
            occurred_at=occurred_at,
        )
    )


async def _require_active_planner(
    session: AsyncSession,
    user_id: UUID,
) -> None:
    actor_id = await session.scalar(
        select(AdminUser.id).where(
            AdminUser.id == user_id,
            AdminUser.is_active.is_(True),
            AdminUser.role.in_((AdminRole.OWNER, AdminRole.REVIEWER)),
        )
    )
    if actor_id is None:
        raise DerivativePipelineNotFoundError("authorized derivative planner was not found")


async def _require_active_owner(
    session: AsyncSession,
    user_id: UUID,
) -> None:
    actor_id = await session.scalar(
        select(AdminUser.id).where(
            AdminUser.id == user_id,
            AdminUser.is_active.is_(True),
            AdminUser.role == AdminRole.OWNER,
        )
    )
    if actor_id is None:
        raise DerivativePipelineNotFoundError("active owner was not found")


def _validate_available_asset(
    asset: Asset,
    *,
    expected_kind: AssetKind | None,
    expected_release_id: UUID,
    label: str,
) -> None:
    if (
        asset.release_id != expected_release_id
        or (expected_kind is not None and asset.kind != expected_kind)
        or asset.state != AssetState.AVAILABLE
        or not asset.storage_backend.strip()
        or not asset.storage_bucket.strip()
        or asset.object_key is None
        or not asset.object_key.strip()
        or asset.object_version_id is None
        or not asset.object_version_id.strip()
        or asset.sha256 is None
        or _SHA256.fullmatch(asset.sha256) is None
        or asset.content_type is None
        or not asset.content_type.strip()
        or asset.image_format is None
        or not asset.image_format.strip()
        or asset.width is None
        or asset.width <= 0
        or asset.height is None
        or asset.height <= 0
        or asset.byte_size is None
        or asset.byte_size <= 0
        or asset.available_at is None
    ):
        raise DerivativePipelineConflictError(f"{label} is unavailable or incomplete")


def _selection_matches_asset(
    selection: ReleaseSelection,
    asset: Asset,
) -> bool:
    return (
        selection.source_storage_backend == asset.storage_backend
        and selection.source_storage_bucket == asset.storage_bucket
        and selection.source_object_key == asset.object_key
        and selection.source_object_version_id == asset.object_version_id
        and selection.source_sha256 == asset.sha256
        and selection.source_content_type == asset.content_type
        and selection.source_image_format == asset.image_format
        and selection.source_width == asset.width
        and selection.source_height == asset.height
        and selection.source_byte_size == asset.byte_size
        and asset.available_at is not None
        and _as_utc(selection.source_available_at) == _as_utc(asset.available_at)
    )


def _normalize_configuration(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise DerivativePipelineInputError("derivative configuration must be a mapping")
    normalized = dict(value)
    if not normalized:
        raise DerivativePipelineInputError("derivative configuration cannot be empty")
    try:
        serialized = canonical_json_bytes(normalized)
    except (TypeError, ValueError):
        raise DerivativePipelineInputError(
            "derivative configuration is not canonical JSON"
        ) from None
    if len(serialized) > _MAX_CONFIGURATION_BYTES:
        raise DerivativePipelineInputError("derivative configuration is too large")
    return normalized


def _normalize_x_teaser_asset_ids(
    values: Sequence[UUID] | None,
) -> frozenset[UUID] | None:
    if values is None:
        return None
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise DerivativePipelineInputError("X teaser asset ids must be a sequence")
    normalized = tuple(values)
    if not 1 <= len(normalized) <= 4:
        raise DerivativePipelineInputError("X teaser asset ids must contain between 1 and 4 values")
    if any(not isinstance(value, UUID) for value in normalized):
        raise DerivativePipelineInputError("X teaser asset id is invalid")
    selected = frozenset(normalized)
    if len(selected) != len(normalized):
        raise DerivativePipelineInputError("X teaser asset ids must be unique")
    return selected


def _normalize_targets(values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise DerivativePipelineInputError("output targets must be a sequence")
    targets = tuple(_normalize_target(value) for value in values)
    if not targets or len(targets) > 20 or len(set(targets)) != len(targets):
        raise DerivativePipelineInputError(
            "output targets must contain between 1 and 20 unique values"
        )
    return tuple(sorted(targets))


def _normalize_target(value: str) -> str:
    if not isinstance(value, str):
        raise DerivativePipelineInputError("derivative target is invalid")
    normalized = value.strip().lower()
    if _TARGET.fullmatch(normalized) is None or normalized not in _SUPPORTED_TARGETS:
        raise DerivativePipelineInputError("derivative target is invalid")
    return normalized


def _stored_targets(recipe: DerivativeRecipe) -> tuple[str, ...]:
    try:
        targets = _normalize_targets(recipe.output_targets)
    except DerivativePipelineInputError:
        raise DerivativePipelineConflictError(
            "stored derivative recipe targets are invalid"
        ) from None
    if len(targets) != recipe.expected_output_count:
        raise DerivativePipelineConflictError("stored derivative recipe output count is invalid")
    return targets


def _job_targets(
    request_payload: object,
    *,
    recipe_targets: tuple[str, ...],
) -> tuple[str, ...]:
    if not isinstance(request_payload, dict):
        raise DerivativePipelineConflictError("stored derivative job request is invalid")
    try:
        targets = _normalize_targets(request_payload["output_targets"])
    except (KeyError, DerivativePipelineInputError):
        raise DerivativePipelineConflictError("stored derivative job targets are invalid") from None
    if not set(targets).issubset(recipe_targets):
        raise DerivativePipelineConflictError(
            "stored derivative job targets exceed the approved recipe"
        )
    return targets


def _bounded_text(value: str, label: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise DerivativePipelineInputError(f"{label} is invalid")
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise DerivativePipelineInputError(f"{label} is invalid")
    return normalized


def _bounded_int(
    value: int,
    label: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DerivativePipelineInputError(f"{label} is invalid")
    if value < minimum or value > maximum:
        raise DerivativePipelineInputError(f"{label} is invalid")
    return value


def _positive_lock_version(value: int) -> int:
    return _bounded_int(
        value,
        "expected lock version",
        minimum=1,
        maximum=2_147_483_647,
    )


def _error_code(value: str) -> str:
    normalized = _bounded_text(value, "error code", 100).lower()
    if _CODE.fullmatch(normalized) is None:
        raise DerivativePipelineInputError("error code is invalid")
    return normalized


def _error_detail(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized or len(normalized) > _MAX_ERROR_DETAIL:
        raise DerivativePipelineInputError("error detail is invalid")
    return normalized


def _required_asset_text(value: str | None, label: str) -> str:
    if value is None or not value.strip():
        raise DerivativePipelineConflictError(f"asset {label} is unavailable")
    return value


def _required_asset_int(value: int | None, label: str) -> int:
    if value is None or value <= 0:
        raise DerivativePipelineConflictError(f"asset {label} is unavailable")
    return value


def _optional_string(
    value: dict[str, Any] | None,
    key: str,
) -> str | None:
    if value is None:
        return None
    item = value[key]
    if not isinstance(item, str):
        raise DerivativePipelineConflictError("watermark snapshot is invalid")
    return item


def _optional_int(
    value: dict[str, Any] | None,
    key: str,
) -> int | None:
    if value is None:
        return None
    item = value[key]
    if isinstance(item, bool) or not isinstance(item, int):
        raise DerivativePipelineConflictError("watermark snapshot is invalid")
    return int(item)


def _job_result(job: DerivativeJob) -> DerivativeJobResult:
    return DerivativeJobResult(
        job_id=job.id,
        state=job.state,
        attempt_count=job.attempt_count,
        lock_version=job.lock_version,
        retry_at=job.retry_at,
        completed_at=job.completed_at,
    )


def _output_result(
    output: DerivativeOutput,
    *,
    replayed: bool,
) -> DerivativeOutputResult:
    return DerivativeOutputResult(
        output_id=output.id,
        job_id=output.derivative_job_id,
        target=output.target,
        asset_id=output.asset_id,
        lineage_id=output.asset_lineage_id,
        replayed=replayed,
    )


def _audit(
    *,
    actor: str,
    action: str,
    resource_type: str,
    resource_id: UUID,
    correlation_id: str,
    detail: dict[str, Any],
    occurred_at: datetime,
) -> AuditEvent:
    return AuditEvent(
        actor=actor,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        correlation_id=correlation_id,
        detail=detail,
        occurred_at=occurred_at,
    )


def _outbox(
    *,
    topic: str,
    dedupe_key: str,
    correlation_id: str,
    aggregate_type: str,
    aggregate_id: UUID,
    payload: dict[str, Any],
    occurred_at: datetime,
) -> OutboxEvent:
    return OutboxEvent(
        topic=topic,
        dedupe_key=dedupe_key,
        correlation_id=correlation_id,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        payload=payload,
        status=OutboxStatus.PENDING,
        attempts=0,
        max_attempts=10,
        available_at=occurred_at,
        created_at=occurred_at,
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
