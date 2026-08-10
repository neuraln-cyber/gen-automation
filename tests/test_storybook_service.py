from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gen_automation.db.models import (
    Asset,
    AssetRanking,
    AssetScore,
    GenerationJob,
    Project,
    Release,
    ReleaseSelection,
    ReleaseVersion,
    ReviewDecision,
    ReviewTask,
    ScoringRun,
    SubjectApproval,
)
from gen_automation.db.session import Database
from gen_automation.domain.canonical import canonical_sha256
from gen_automation.domain.enums import (
    ApprovalStatus,
    AssetKind,
    AssetScoreState,
    AssetState,
    GenerationState,
    RankingDisposition,
    ReleasePhase,
    ReviewDecisionValue,
    ReviewTaskState,
    ScoringRunState,
)
from gen_automation.domain.release_spec import ReleaseSpecification
from gen_automation.domain.storybooks import (
    StorybookContentAssessment,
    StorybookContentAssessmentVerdict,
    StorybookContentRating,
    StorybookInputMode,
    StorybookPagePlan,
    StorybookPlan,
    StorybookPlannerIdentity,
    StorybookProjectRequest,
)
from gen_automation.services.storybooks import (
    StorybookSourceAuthorizationError,
    StorybookSourceConflictError,
    StorybookSourceContext,
    StorybookSourceNotFoundError,
    build_storybook_source_context,
    validate_storybook_finalization_context,
)
from tests.factories import seed_release_approvals, valid_release_payload

NOW = datetime(2026, 8, 10, 10, 0, tzinfo=UTC)
ASSESSMENT_PROFILE_SHA256 = "6" * 64
ASSESSMENT_MODEL = "storybook-assessor-test"
ASSESSMENT_MODEL_REVISION = "revision-2026-08-10"
ASSESSMENT_PROMPT_SHA256 = "7" * 64
ASSESSMENT_SCHEMA_SHA256 = "8" * 64


@dataclass(frozen=True, slots=True)
class StorybookFixture:
    database: Database
    owner_id: UUID
    project_id: UUID
    release_id: UUID
    release_version_id: UUID
    selection_ids: tuple[UUID, UUID]
    asset_ids: tuple[UUID, UUID]
    subject_approval_id: UUID


@pytest.fixture
async def storybook_fixture(tmp_path: Path) -> AsyncIterator[StorybookFixture]:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'storybook-service.db').as_posix()}")
    await database.create_schema()
    payload = valid_release_payload()
    async with database.sessions() as session:
        owner = await seed_release_approvals(session, payload)
        specification = ReleaseSpecification.model_validate(payload["specification"])
        frozen_specification = specification.model_dump(mode="json")
        project = Project(slug="storybook-service", name="Storybook service")
        session.add(project)
        await session.flush()
        release = Release(
            project_id=project.id,
            slug="storybook-source",
            title="Storybook source",
            phase=ReleasePhase.REVIEWING,
            current_version_no=1,
            desired_accepted_count=2,
            lock_version=1,
        )
        session.add(release)
        await session.flush()
        release_version = ReleaseVersion(
            release_id=release.id,
            version_no=1,
            specification=frozen_specification,
            specification_sha256=canonical_sha256(frozen_specification),
            created_by="test",
            created_at=NOW,
        )
        session.add(release_version)
        await session.flush()
        generation_job = GenerationJob(
            release_version_id=release_version.id,
            logical_key="1" * 64,
            parameters={"ordinal": 0},
            parameters_sha256="2" * 64,
            provider="salad",
            state=GenerationState.SUCCEEDED,
            priority=100,
            expected_output_count=2,
            attempt_count=1,
            max_attempts=3,
            lock_version=1,
        )
        session.add(generation_job)
        await session.flush()

        assets: list[Asset] = []
        for index in range(2):
            payload_bytes = f"storybook-source-{index}".encode()
            asset = Asset(
                release_id=release.id,
                generation_job_id=generation_job.id,
                output_index=index,
                kind=AssetKind.RAW_MASTER,
                state=AssetState.AVAILABLE,
                storage_backend="s3",
                storage_bucket="storybook-test",
                object_key=f"raw/storybook-{index}.png",
                object_version_id=f"source-version-{index}",
                sha256=hashlib.sha256(payload_bytes).hexdigest(),
                content_type="image/png",
                image_format="PNG",
                width=1024,
                height=1536,
                byte_size=len(payload_bytes),
                asset_metadata={"test": True},
                available_at=NOW,
            )
            session.add(asset)
            assets.append(asset)
        await session.flush()

        scoring_run = ScoringRun(
            release_version_id=release_version.id,
            configuration={"storybook": True},
            config_sha256="3" * 64,
            input_manifest_sha256="4" * 64,
            ranking_manifest_sha256=None,
            scorer_version="storybook-test-v1",
            pillow_version="12.0.0",
            state=ScoringRunState.RUNNING,
            asset_count=2,
            max_attempts=3,
            created_at=NOW,
            started_at=NOW,
            completed_at=None,
        )
        session.add(scoring_run)
        await session.flush()
        rankings: list[AssetRanking] = []
        for index, asset in enumerate(assets, start=1):
            score = AssetScore(
                scoring_run_id=scoring_run.id,
                asset_id=asset.id,
                asset_storage_backend=asset.storage_backend,
                asset_storage_bucket=asset.storage_bucket,
                asset_sha256=asset.sha256,
                asset_object_key=asset.object_key,
                asset_object_version_id=asset.object_version_id,
                asset_byte_size=asset.byte_size,
                asset_image_format=asset.image_format,
                asset_width=asset.width,
                asset_height=asset.height,
                state=AssetScoreState.SCORED,
                attempts=1,
                max_attempts=3,
                available_at=NOW,
                luminance_mean_micros=500_000,
                luminance_std_micros=125_000,
                dynamic_range_micros=750_000,
                entropy_bits_micros=6_000_000,
                entropy_normalized_micros=750_000,
                sharpness_micros=800_000,
                dhash_hex=f"{index:016x}",
                aggregate_score_micros=900_000 - index,
                score_breakdown={"quality": 900_000 - index},
                signal_detail={"storybook": True},
                scorer_version=scoring_run.scorer_version,
                pillow_version=scoring_run.pillow_version,
                config_sha256=scoring_run.config_sha256,
                created_at=NOW,
                completed_at=NOW + timedelta(minutes=1),
            )
            session.add(score)
            await session.flush()
            ranking = AssetRanking(
                scoring_run_id=scoring_run.id,
                asset_score_id=score.id,
                asset_id=asset.id,
                rank=index,
                aggregate_score_micros=score.aggregate_score_micros,
                disposition=RankingDisposition.REVIEW_CANDIDATE,
                explanation={"storybook": True},
                is_duplicate_representative=False,
                scorer_version=scoring_run.scorer_version,
                pillow_version=scoring_run.pillow_version,
                config_sha256=scoring_run.config_sha256,
                frozen_at=NOW + timedelta(minutes=1),
            )
            session.add(ranking)
            rankings.append(ranking)
        await session.flush()

        scoring_run.ranking_manifest_sha256 = "5" * 64
        scoring_run.state = ScoringRunState.COMPLETED
        scoring_run.completed_at = NOW + timedelta(minutes=1)
        await session.flush()

        review = ReviewTask(
            release_version_id=release_version.id,
            release_version_no=1,
            release_specification_sha256=release_version.specification_sha256,
            scoring_run_id=scoring_run.id,
            scoring_config_sha256=scoring_run.config_sha256,
            scoring_input_manifest_sha256=scoring_run.input_manifest_sha256,
            ranking_manifest_sha256=scoring_run.ranking_manifest_sha256,
            desired_accepted_count=2,
            ranked_asset_count=2,
            state=ReviewTaskState.OPEN,
            lock_version=1,
            created_by_user_id=owner.id,
            created_at=NOW + timedelta(minutes=2),
        )
        session.add(review)
        await session.flush()

        selections: list[ReleaseSelection] = []
        for index, (asset, ranking) in enumerate(zip(assets, rankings, strict=True), start=1):
            decision = ReviewDecision(
                review_task_id=review.id,
                scoring_run_id=scoring_run.id,
                asset_id=asset.id,
                revision=1,
                decision=ReviewDecisionValue.ACCEPT,
                decided_by_user_id=owner.id,
                decided_at=NOW + timedelta(minutes=2),
            )
            session.add(decision)
            await session.flush()
            selection = ReleaseSelection(
                review_task_id=review.id,
                scoring_run_id=scoring_run.id,
                review_decision_id=decision.id,
                decision_revision=1,
                release_version_id=release_version.id,
                asset_id=asset.id,
                ranking_rank=ranking.rank,
                display_order=index,
                ranking_manifest_sha256=scoring_run.ranking_manifest_sha256,
                source_storage_backend=asset.storage_backend,
                source_storage_bucket=asset.storage_bucket,
                source_object_key=asset.object_key,
                source_object_version_id=asset.object_version_id,
                source_sha256=asset.sha256,
                source_content_type=asset.content_type,
                source_image_format=asset.image_format,
                source_width=asset.width,
                source_height=asset.height,
                source_byte_size=asset.byte_size,
                source_generation_job_id=generation_job.id,
                source_output_index=asset.output_index,
                source_generation_ordinal=0,
                source_generation_queue_position=index,
                source_available_at=asset.available_at,
                frozen_at=NOW + timedelta(minutes=3),
            )
            session.add(selection)
            selections.append(selection)
        await session.flush()
        review.state = ReviewTaskState.COMPLETED
        review.lock_version = 2
        review.completed_by_user_id = owner.id
        review.completed_at = NOW + timedelta(minutes=3)
        await session.commit()

        subject_approval_id = await session.scalar(
            select(SubjectApproval.id).where(SubjectApproval.is_current.is_(True))
        )
        assert subject_approval_id is not None
        fixture = StorybookFixture(
            database=database,
            owner_id=owner.id,
            project_id=project.id,
            release_id=release.id,
            release_version_id=release_version.id,
            selection_ids=(selections[0].id, selections[1].id),
            asset_ids=(assets[0].id, assets[1].id),
            subject_approval_id=subject_approval_id,
        )
    try:
        yield fixture
    finally:
        await database.dispose()


def _selected_request(
    fixture: StorybookFixture,
    *,
    selection_ids: tuple[UUID, ...] | None = None,
    release_version_id: UUID | None = None,
) -> StorybookProjectRequest:
    selected = selection_ids or fixture.selection_ids
    return StorybookProjectRequest(
        project_id=fixture.project_id,
        input_mode=StorybookInputMode.SELECTED_IMAGES,
        title="A safe mystery",
        general_idea="The approved adult cast solves a harmless mystery.",
        page_count=len(selected),
        selected_release_version_id=release_version_id or fixture.release_version_id,
        selected_release_selection_ids=selected,
    )


def _plan_for_context(context: StorybookSourceContext) -> StorybookPlan:
    if context.selected_sources:
        pages = tuple(
            StorybookPagePlan(
                page_number=index,
                scene_summary=f"Safe selected scene {index}.",
                source=source,
            )
            for index, source in enumerate(context.selected_sources, start=1)
        )
    else:
        pages = tuple(
            StorybookPagePlan(
                page_number=index,
                scene_summary=f"Safe generated scene {index}.",
                generation_prompt=f"Draw safe generated scene {index}.",
            )
            for index in range(1, context.request.page_count + 1)
        )
    return StorybookPlan(
        request=context.request,
        request_sha256=context.request.request_sha256,
        planner=StorybookPlannerIdentity(
            model="storybook-planner-test",
            model_revision="revision-2026-08-10",
            prompt_sha256="9" * 64,
            schema_sha256="a" * 64,
        ),
        characters=context.characters,
        adult_subject_gate_sha256=context.adult_subject_gate_sha256,
        pages=pages,
    )


def _approved_assessment(
    plan: StorybookPlan,
    *,
    source_sha256s: tuple[str, ...],
) -> StorybookContentAssessment:
    return StorybookContentAssessment(
        plan_sha256=plan.plan_sha256,
        request_sha256=plan.request_sha256,
        content_rating=plan.request.content_rating,
        source_sha256s=source_sha256s,
        adult_subject_gate_sha256=plan.adult_subject_gate_sha256,
        profile_sha256=ASSESSMENT_PROFILE_SHA256,
        model=ASSESSMENT_MODEL,
        model_revision=ASSESSMENT_MODEL_REVISION,
        prompt_sha256=ASSESSMENT_PROMPT_SHA256,
        schema_sha256=ASSESSMENT_SCHEMA_SHA256,
        verdict=StorybookContentAssessmentVerdict.APPROVED,
    )


async def _validate_finalization(
    session: AsyncSession,
    *,
    plan: StorybookPlan,
    assessment: StorybookContentAssessment,
    source_sha256s: tuple[str, ...],
) -> None:
    await validate_storybook_finalization_context(
        session,
        plan=plan,
        assessment=assessment,
        source_sha256s=source_sha256s,
        expected_profile_sha256=ASSESSMENT_PROFILE_SHA256,
        expected_model=ASSESSMENT_MODEL,
        expected_model_revision=ASSESSMENT_MODEL_REVISION,
        expected_prompt_sha256=ASSESSMENT_PROMPT_SHA256,
        expected_schema_sha256=ASSESSMENT_SCHEMA_SHA256,
    )


@pytest.mark.asyncio
async def test_selected_context_freezes_exact_release_selection_sources_and_cast(
    storybook_fixture: StorybookFixture,
) -> None:
    request = _selected_request(
        storybook_fixture,
        selection_ids=tuple(reversed(storybook_fixture.selection_ids)),
    )
    async with storybook_fixture.database.sessions() as session:
        context = await build_storybook_source_context(session, request=request)

    assert [source.release_selection_id for source in context.selected_sources] == list(
        reversed(storybook_fixture.selection_ids)
    )
    assert [source.asset_id for source in context.selected_sources] == list(
        reversed(storybook_fixture.asset_ids)
    )
    assert context.planner_request.selected_image_sha256s == tuple(
        source.source_sha256 for source in context.selected_sources
    )
    assert context.characters[0].subject_approval_id == storybook_fixture.subject_approval_id
    assert context.characters[0].canonical_age == 25
    assert context.adult_subject_gate_sha256 is not None
    assert context.approval_snapshot_sha256 is not None
    assert len(context.source_context_sha256) == 64


@pytest.mark.asyncio
async def test_selected_context_rejects_release_from_another_project(
    storybook_fixture: StorybookFixture,
) -> None:
    async with storybook_fixture.database.sessions() as session:
        other_project = Project(slug="other-storybook-project", name="Other project")
        session.add(other_project)
        await session.flush()
        request = _selected_request(storybook_fixture).model_copy(
            update={"project_id": other_project.id}
        )

        with pytest.raises(StorybookSourceAuthorizationError, match="requested project"):
            await build_storybook_source_context(session, request=request)


@pytest.mark.asyncio
async def test_selected_context_rejects_asset_ids_disguised_as_selection_ids(
    storybook_fixture: StorybookFixture,
) -> None:
    request = _selected_request(
        storybook_fixture,
        selection_ids=storybook_fixture.asset_ids,
    )
    async with storybook_fixture.database.sessions() as session:
        with pytest.raises(StorybookSourceNotFoundError, match="release selections"):
            await build_storybook_source_context(session, request=request)


@pytest.mark.asyncio
async def test_selected_context_rejects_selections_from_the_wrong_release_version(
    storybook_fixture: StorybookFixture,
) -> None:
    async with storybook_fixture.database.sessions() as session:
        release = await session.get(Release, storybook_fixture.release_id)
        old_version = await session.get(ReleaseVersion, storybook_fixture.release_version_id)
        assert release is not None and old_version is not None
        new_version = ReleaseVersion(
            release_id=release.id,
            version_no=2,
            specification=old_version.specification,
            specification_sha256=old_version.specification_sha256,
            created_by="test",
            created_at=NOW + timedelta(hours=1),
        )
        session.add(new_version)
        release.current_version_no = 2
        await session.commit()
        request = _selected_request(
            storybook_fixture,
            release_version_id=new_version.id,
        )
        with pytest.raises(StorybookSourceConflictError, match="requested release version"):
            await build_storybook_source_context(session, request=request)


@pytest.mark.asyncio
async def test_selected_context_rejects_revoked_release_cast_approval(
    storybook_fixture: StorybookFixture,
) -> None:
    async with storybook_fixture.database.sessions() as session:
        approval = await session.get(SubjectApproval, storybook_fixture.subject_approval_id)
        assert approval is not None
        approval.status = ApprovalStatus.REVOKED
        approval.is_current = False
        approval.revoked_by_user_id = storybook_fixture.owner_id
        approval.revoked_at = NOW + timedelta(hours=1)
        await session.commit()
        with pytest.raises(StorybookSourceAuthorizationError, match="approval evidence"):
            await build_storybook_source_context(
                session,
                request=_selected_request(storybook_fixture),
            )


@pytest.mark.asyncio
async def test_selected_context_rejects_asset_drift_from_frozen_selection(
    storybook_fixture: StorybookFixture,
) -> None:
    async with storybook_fixture.database.sessions() as session:
        asset = await session.get(Asset, storybook_fixture.asset_ids[0])
        assert asset is not None
        asset.sha256 = "f" * 64
        await session.commit()
        with pytest.raises(StorybookSourceConflictError, match="immutable release selection"):
            await build_storybook_source_context(
                session,
                request=_selected_request(storybook_fixture),
            )


@pytest.mark.asyncio
async def test_idea_only_context_loads_exact_current_subject_approval(
    storybook_fixture: StorybookFixture,
) -> None:
    request = StorybookProjectRequest(
        project_id=storybook_fixture.project_id,
        input_mode=StorybookInputMode.IDEA_ONLY,
        title="A quiet afternoon",
        general_idea="The approved adult character helps a neighbor find a lost parcel.",
        page_count=3,
        requested_subject_approval_ids=(storybook_fixture.subject_approval_id,),
        content_rating=StorybookContentRating.SFW,
    )
    async with storybook_fixture.database.sessions() as session:
        context = await build_storybook_source_context(session, request=request)

    assert context.selected_sources == ()
    assert context.release_specification_sha256 is None
    assert context.characters[0].subject_approval_id == storybook_fixture.subject_approval_id
    assert context.planner_request.characters == context.characters
    assert context.adult_subject_gate_sha256 is not None


@pytest.mark.asyncio
async def test_idea_only_context_rejects_unknown_and_tampered_subject_approvals(
    storybook_fixture: StorybookFixture,
) -> None:
    unknown_request = StorybookProjectRequest(
        project_id=storybook_fixture.project_id,
        input_mode=StorybookInputMode.IDEA_ONLY,
        general_idea="A safe story.",
        page_count=1,
        requested_subject_approval_ids=(uuid4(),),
    )
    async with storybook_fixture.database.sessions() as session:
        with pytest.raises(StorybookSourceAuthorizationError, match="not currently approved"):
            await build_storybook_source_context(session, request=unknown_request)

        approval = await session.get(SubjectApproval, storybook_fixture.subject_approval_id)
        assert approval is not None
        approval.evidence_sha256 = "f" * 64
        await session.commit()
        tampered_request = StorybookProjectRequest(
            project_id=storybook_fixture.project_id,
            input_mode=StorybookInputMode.IDEA_ONLY,
            general_idea="A safe story.",
            page_count=1,
            requested_subject_approval_ids=(storybook_fixture.subject_approval_id,),
        )
        with pytest.raises(StorybookSourceAuthorizationError, match="not currently approved"):
            await build_storybook_source_context(session, request=tampered_request)


@pytest.mark.asyncio
async def test_finalization_rebuilds_selected_context_but_requires_persisted_assessment(
    storybook_fixture: StorybookFixture,
) -> None:
    async with storybook_fixture.database.sessions() as session:
        context = await build_storybook_source_context(
            session,
            request=_selected_request(storybook_fixture),
        )
        plan = _plan_for_context(context)
        source_sha256s = tuple(source.source_sha256 for source in context.selected_sources)
        assessment = _approved_assessment(plan, source_sha256s=source_sha256s)

        with pytest.raises(StorybookSourceAuthorizationError, match="persisted semantic"):
            await _validate_finalization(
                session,
                plan=plan,
                assessment=assessment,
                source_sha256s=source_sha256s,
            )


@pytest.mark.asyncio
async def test_finalization_rejects_forged_selected_source_snapshot(
    storybook_fixture: StorybookFixture,
) -> None:
    async with storybook_fixture.database.sessions() as session:
        context = await build_storybook_source_context(
            session,
            request=_selected_request(storybook_fixture),
        )
        plan = _plan_for_context(context)
        first_page = plan.pages[0]
        assert first_page.source is not None
        forged_source = first_page.source.model_copy(update={"source_sha256": "f" * 64})
        forged_plan = plan.model_copy(
            update={
                "pages": (
                    first_page.model_copy(update={"source": forged_source}),
                    *plan.pages[1:],
                )
            }
        )
        source_sha256s = tuple(
            page.source.source_sha256 for page in forged_plan.pages if page.source is not None
        )
        assessment = _approved_assessment(forged_plan, source_sha256s=source_sha256s)

        with pytest.raises(StorybookSourceAuthorizationError, match="authorized source context"):
            await _validate_finalization(
                session,
                plan=forged_plan,
                assessment=assessment,
                source_sha256s=source_sha256s,
            )


@pytest.mark.asyncio
async def test_finalization_rejects_non_authoritative_selected_source_digests(
    storybook_fixture: StorybookFixture,
) -> None:
    async with storybook_fixture.database.sessions() as session:
        context = await build_storybook_source_context(
            session,
            request=_selected_request(storybook_fixture),
        )
        plan = _plan_for_context(context)
        forged_source_sha256s = ("b" * 64, "c" * 64)
        assessment = _approved_assessment(plan, source_sha256s=forged_source_sha256s)

        with pytest.raises(StorybookSourceAuthorizationError, match="authorize finalization"):
            await _validate_finalization(
                session,
                plan=plan,
                assessment=assessment,
                source_sha256s=forged_source_sha256s,
            )


@pytest.mark.asyncio
async def test_finalization_rechecks_idea_only_subject_approval_and_assessor_identity(
    storybook_fixture: StorybookFixture,
) -> None:
    request = StorybookProjectRequest(
        project_id=storybook_fixture.project_id,
        input_mode=StorybookInputMode.IDEA_ONLY,
        general_idea="An approved adult character has a safe afternoon.",
        page_count=1,
        requested_subject_approval_ids=(storybook_fixture.subject_approval_id,),
    )
    async with storybook_fixture.database.sessions() as session:
        context = await build_storybook_source_context(session, request=request)
        plan = _plan_for_context(context)
        generated_source_sha256s = ("d" * 64,)
        assessment = _approved_assessment(plan, source_sha256s=generated_source_sha256s)

        with pytest.raises(StorybookSourceAuthorizationError, match="authorize finalization"):
            await validate_storybook_finalization_context(
                session,
                plan=plan,
                assessment=assessment,
                source_sha256s=generated_source_sha256s,
                expected_profile_sha256=ASSESSMENT_PROFILE_SHA256,
                expected_model="untrusted-assessor",
                expected_model_revision=ASSESSMENT_MODEL_REVISION,
                expected_prompt_sha256=ASSESSMENT_PROMPT_SHA256,
                expected_schema_sha256=ASSESSMENT_SCHEMA_SHA256,
            )

        with pytest.raises(StorybookSourceAuthorizationError, match="persisted generated"):
            await _validate_finalization(
                session,
                plan=plan,
                assessment=assessment,
                source_sha256s=generated_source_sha256s,
            )

        approval = await session.get(SubjectApproval, storybook_fixture.subject_approval_id)
        assert approval is not None
        approval.status = ApprovalStatus.REVOKED
        approval.is_current = False
        approval.revoked_by_user_id = storybook_fixture.owner_id
        approval.revoked_at = NOW + timedelta(hours=1)
        await session.commit()

        with pytest.raises(StorybookSourceAuthorizationError, match="currently approved"):
            await _validate_finalization(
                session,
                plan=plan,
                assessment=assessment,
                source_sha256s=generated_source_sha256s,
            )
