import asyncio
import hashlib
import io
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from PIL import Image, PngImagePlugin
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from gen_automation.db.models import (
    AdminUser,
    Asset,
    AssetRanking,
    AssetScore,
    DerivativeJob,
    DerivativeOutput,
    GenerationJob,
    Project,
    Release,
    ReleaseSelection,
    ReleaseVersion,
    ReviewDecision,
    ScoringRun,
)
from gen_automation.db.session import Database
from gen_automation.domain.deliverability import patreon_full_output_byte_budget
from gen_automation.domain.enums import (
    AdminRole,
    AssetKind,
    AssetScoreState,
    AssetState,
    DerivativeJobState,
    GenerationState,
    RankingDisposition,
    ReleasePhase,
    ReviewDecisionValue,
    ReviewTaskState,
    ScoringRunState,
)
from gen_automation.services.derivative_pipeline import (
    DerivativePipelineConflictError,
    DerivativePipelineLeaseError,
    DerivativePlanResult,
    claim_derivative_jobs,
    create_derivative_recipe_and_plan,
    record_derivative_output,
    retry_derivative_job,
    start_derivative_job,
    succeed_derivative_job,
)
from gen_automation.services.ranking_manifest import ranking_manifest_sha256
from gen_automation.services.review import (
    append_review_decision,
    create_review_task,
    transition_review_task,
)
from tests.image_privacy_assertions import PRIVATE_MASTER_PROMPT

NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
PLAN_AT = NOW + timedelta(minutes=10)


@dataclass(frozen=True, slots=True)
class ApprovedContext:
    database: Database
    owner_id: UUID
    release_id: UUID
    release_version_id: UUID
    review_task_id: UUID
    scoring_run_id: UUID
    raw_asset_ids: tuple[UUID, UUID]
    raw_payloads: tuple[bytes, bytes]


def _owner() -> AdminUser:
    return AdminUser(
        username_normalized="derivative-owner",
        display_name="Derivative Owner",
        password_hash="disabled-derivative-test-password-hash",  # noqa: S106
        role=AdminRole.OWNER,
        is_active=True,
        failed_login_count=0,
        password_changed_at=NOW,
        credential_version=1,
        lock_version=1,
    )


def _raw_png(index: int) -> bytes:
    output = io.BytesIO()
    image = Image.new(
        "RGB",
        (64, 64),
        color=(32 + index * 32, 64 + index * 32, 96 + index * 32),
    )
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("prompt", PRIVATE_MASTER_PROMPT)
    metadata.add_text("Software", "/private/generator/workstation")
    image.save(output, format="PNG", pnginfo=metadata)
    image.close()
    return output.getvalue()


@pytest.fixture
async def approved_context(tmp_path: Path) -> AsyncIterator[ApprovedContext]:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'derivative-pipeline.db').as_posix()}")
    await database.create_schema()
    raw_asset_ids = (
        UUID("71000000-0000-4000-8000-000000000001"),
        UUID("71000000-0000-4000-8000-000000000002"),
    )
    raw_payloads = (_raw_png(0), _raw_png(1))
    async with database.sessions() as session:
        owner = _owner()
        project = Project(slug="derivative", name="Derivative")
        session.add_all([owner, project])
        await session.flush()
        release = Release(
            project_id=project.id,
            slug="release",
            title="Derivative release",
            phase=ReleasePhase.REVIEWING,
            current_version_no=1,
            desired_accepted_count=2,
            lock_version=1,
        )
        session.add(release)
        await session.flush()
        version = ReleaseVersion(
            release_id=release.id,
            version_no=1,
            specification={"schema_version": 1},
            specification_sha256="a" * 64,
            created_by="test",
            created_at=NOW,
        )
        session.add(version)
        await session.flush()
        generation_job = GenerationJob(
            release_version_id=version.id,
            logical_key="b" * 64,
            parameters={"batch": 2},
            parameters_sha256="c" * 64,
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
        raw_assets: list[Asset] = []
        for output_index, asset_id in enumerate(raw_asset_ids):
            payload = raw_payloads[output_index]
            asset = Asset(
                id=asset_id,
                release_id=release.id,
                generation_job_id=generation_job.id,
                output_index=output_index,
                kind=AssetKind.RAW_MASTER,
                state=AssetState.AVAILABLE,
                storage_backend="s3",
                storage_bucket="derivative-test",
                object_key=f"raw/{asset_id}.png",
                object_version_id=f"raw-version-{output_index}",
                sha256=hashlib.sha256(payload).hexdigest(),
                content_type="image/png",
                image_format="PNG",
                width=64,
                height=64,
                byte_size=len(payload),
                asset_metadata={"immutable": True},
                available_at=NOW,
            )
            session.add(asset)
            raw_assets.append(asset)
        await session.flush()
        scoring_run = ScoringRun(
            release_version_id=version.id,
            configuration={"quality": "v1"},
            config_sha256="d" * 64,
            input_manifest_sha256="e" * 64,
            scorer_version="test-scorer-v1",
            pillow_version="12.0.0",
            state=ScoringRunState.RUNNING,
            asset_count=2,
            max_attempts=3,
            created_at=NOW,
            started_at=NOW,
        )
        session.add(scoring_run)
        await session.flush()
        manifest_rows: list[tuple[AssetRanking, AssetScore]] = []
        for rank, asset in enumerate(raw_assets, start=1):
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
                state=AssetScoreState.FLAGGED_CORRUPT,
                attempts=1,
                max_attempts=3,
                available_at=NOW,
                aggregate_score_micros=1_000_000 - rank,
                signal_detail={"test": True},
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
                rank=rank,
                aggregate_score_micros=score.aggregate_score_micros,
                disposition=RankingDisposition.FLAGGED_REVIEW,
                explanation={"rank": rank},
                is_duplicate_representative=False,
                scorer_version=scoring_run.scorer_version,
                pillow_version=scoring_run.pillow_version,
                config_sha256=scoring_run.config_sha256,
                frozen_at=NOW + timedelta(minutes=1),
            )
            session.add(ranking)
            manifest_rows.append((ranking, score))
        await session.flush()
        scoring_run.ranking_manifest_sha256 = ranking_manifest_sha256(
            scoring_run,
            manifest_rows,
        )
        scoring_run.state = ScoringRunState.COMPLETED
        scoring_run.completed_at = NOW + timedelta(minutes=1)
        await session.commit()

        task = await create_review_task(
            session,
            scoring_run_id=scoring_run.id,
            created_by_user_id=owner.id,
            idempotency_key="create-derivative-review",
            now=NOW + timedelta(minutes=2),
        )
        first = await append_review_decision(
            session,
            review_task_id=task.task_id,
            asset_id=raw_asset_ids[0],
            decision=ReviewDecisionValue.ACCEPT,
            decided_by_user_id=owner.id,
            expected_lock_version=1,
            idempotency_key="accept-derivative-one",
            now=NOW + timedelta(minutes=3),
        )
        second = await append_review_decision(
            session,
            review_task_id=task.task_id,
            asset_id=raw_asset_ids[1],
            decision=ReviewDecisionValue.ACCEPT,
            decided_by_user_id=owner.id,
            expected_lock_version=first.task_lock_version,
            idempotency_key="accept-derivative-two",
            now=NOW + timedelta(minutes=4),
        )
        completed = await transition_review_task(
            session,
            review_task_id=task.task_id,
            target_state=ReviewTaskState.COMPLETED,
            changed_by_user_id=owner.id,
            expected_lock_version=second.task_lock_version,
            idempotency_key="complete-derivative-review",
            now=NOW + timedelta(minutes=5),
        )
        assert completed.state == ReviewTaskState.COMPLETED
        context = ApprovedContext(
            database=database,
            owner_id=owner.id,
            release_id=release.id,
            release_version_id=version.id,
            review_task_id=task.task_id,
            scoring_run_id=scoring_run.id,
            raw_asset_ids=raw_asset_ids,
            raw_payloads=raw_payloads,
        )
    try:
        yield context
    finally:
        await database.dispose()


async def _plan(
    context: ApprovedContext,
    *,
    key: str = "plan-derivatives",
) -> DerivativePlanResult:
    async with context.database.sessions() as session:
        return await create_derivative_recipe_and_plan(
            session,
            review_task_id=context.review_task_id,
            configuration={"full": {"format": "PNG"}},
            recipe_version=1,
            renderer_version="renderer-v1",
            pillow_version="12.0.0",
            created_by_user_id=context.owner_id,
            approved_by_user_id=context.owner_id,
            idempotency_key=key,
            output_targets=("full",),
            max_attempts=3,
            now=PLAN_AT,
        )


@pytest.mark.asyncio
async def test_review_completion_freezes_exact_sources_and_approves_release(
    approved_context: ApprovedContext,
) -> None:
    async with approved_context.database.sessions() as session:
        selections = list(
            (
                await session.scalars(
                    select(ReleaseSelection)
                    .where(ReleaseSelection.review_task_id == approved_context.review_task_id)
                    .order_by(ReleaseSelection.display_order)
                )
            ).all()
        )
        release = await session.get(Release, approved_context.release_id)
        assert release is not None
        assert release.phase == ReleasePhase.APPROVED
        assert release.lock_version == 2
        assert [row.asset_id for row in selections] == list(approved_context.raw_asset_ids)
        assert [row.display_order for row in selections] == [1, 2]
        assert [row.source_object_version_id for row in selections] == [
            "raw-version-0",
            "raw-version-1",
        ]
        assert all(row.source_sha256 for row in selections)

        selections[0].source_sha256 = "0" * 64
        with pytest.raises(IntegrityError, match="release selections are immutable"):
            await session.commit()
        await session.rollback()

        session.add(
            ReviewDecision(
                review_task_id=approved_context.review_task_id,
                scoring_run_id=approved_context.scoring_run_id,
                asset_id=approved_context.raw_asset_ids[0],
                revision=2,
                decision=ReviewDecisionValue.ACCEPT,
                reason_code="late",
                decided_by_user_id=approved_context.owner_id,
                decided_at=NOW + timedelta(minutes=6),
            )
        )
        with pytest.raises(
            IntegrityError,
            match="terminal review tasks reject new decisions",
        ):
            await session.commit()


@pytest.mark.asyncio
async def test_plan_is_idempotent_and_cas_promotes_only_current_release(
    approved_context: ApprovedContext,
) -> None:
    first = await _plan(approved_context)
    replay = await _plan(approved_context)
    assert replay.replayed is True
    assert replay.recipe_id == first.recipe_id
    assert replay.job_ids == first.job_ids
    assert first.jobs_created == 2

    async with approved_context.database.sessions() as session:
        release = await session.get(Release, approved_context.release_id)
        jobs = list(
            (await session.scalars(select(DerivativeJob).order_by(DerivativeJob.logical_key))).all()
        )
        assert release is not None
        assert release.phase == ReleasePhase.RENDERING
        assert release.lock_version == 3
        assert len(jobs) == 2
        assert all(job.state == DerivativeJobState.REQUESTED for job in jobs)
        assert all(
            job.request_payload["full_output_byte_budget"] == patreon_full_output_byte_budget(2)
            for job in jobs
        )

    async with approved_context.database.sessions() as session:
        with pytest.raises(
            DerivativePipelineConflictError,
            match="phase does not allow rendering",
        ):
            await create_derivative_recipe_and_plan(
                session,
                review_task_id=approved_context.review_task_id,
                configuration={"full": {"format": "PNG"}},
                recipe_version=1,
                renderer_version="renderer-v1",
                pillow_version="12.0.0",
                created_by_user_id=approved_context.owner_id,
                approved_by_user_id=approved_context.owner_id,
                idempotency_key="different-plan-key",
                output_targets=("full",),
                now=PLAN_AT,
            )


@pytest.mark.asyncio
async def test_stale_release_version_rejects_planning_with_typed_conflict(
    approved_context: ApprovedContext,
) -> None:
    async with approved_context.database.sessions() as session:
        release = await session.get(Release, approved_context.release_id)
        assert release is not None
        session.add(
            ReleaseVersion(
                release_id=release.id,
                version_no=2,
                specification={"schema_version": 1, "revision": 2},
                specification_sha256="f" * 64,
                created_by="test",
                created_at=NOW + timedelta(minutes=6),
            )
        )
        release.current_version_no = 2
        await session.commit()

    with pytest.raises(
        DerivativePipelineConflictError,
        match="stale",
    ):
        await _plan(approved_context)


@pytest.mark.asyncio
async def test_two_controllers_claim_disjoint_jobs_and_expired_lease_is_reclaimed(
    approved_context: ApprovedContext,
) -> None:
    plan = await _plan(approved_context)
    claim_at = PLAN_AT + timedelta(minutes=1)
    async with (
        approved_context.database.sessions() as first_session,
        approved_context.database.sessions() as second_session,
    ):
        first_claim, second_claim = await asyncio.gather(
            claim_derivative_jobs(
                first_session,
                worker_id="controller-one",
                limit=1,
                lease_seconds=60,
                now=claim_at,
            ),
            claim_derivative_jobs(
                second_session,
                worker_id="controller-two",
                limit=2,
                lease_seconds=60,
                now=claim_at,
            ),
        )
    claimed_ids = [row.job_id for row in (*first_claim, *second_claim)]
    assert len(claimed_ids) == len(set(claimed_ids)) == 2
    assert set(claimed_ids) == set(plan.job_ids)

    owned = first_claim[0]
    async with approved_context.database.sessions() as session:
        no_steal = await claim_derivative_jobs(
            session,
            worker_id="controller-three",
            limit=2,
            lease_seconds=60,
            now=claim_at + timedelta(seconds=30),
        )
    assert no_steal == []

    async with approved_context.database.sessions() as session:
        reclaimed = await claim_derivative_jobs(
            session,
            worker_id="controller-three",
            limit=2,
            lease_seconds=60,
            now=claim_at + timedelta(seconds=61),
        )
    assert {row.job_id for row in reclaimed} == set(plan.job_ids)
    reclaimed_owned = next(row for row in reclaimed if row.job_id == owned.job_id)
    assert reclaimed_owned.attempt_count == 2


@pytest.mark.asyncio
async def test_retry_wait_and_lease_lock_version_are_enforced(
    approved_context: ApprovedContext,
) -> None:
    await _plan(approved_context)
    claim_at = PLAN_AT + timedelta(minutes=1)
    async with approved_context.database.sessions() as session:
        claim = (
            await claim_derivative_jobs(
                session,
                worker_id="renderer-one",
                limit=10,
                lease_seconds=300,
                now=claim_at,
            )
        )[0]
    async with approved_context.database.sessions() as session:
        started = await start_derivative_job(
            session,
            job_id=claim.job_id,
            worker_id="renderer-one",
            expected_lock_version=claim.lock_version,
            now=claim_at + timedelta(seconds=1),
        )
    async with approved_context.database.sessions() as session:
        with pytest.raises(DerivativePipelineConflictError, match="stale"):
            await retry_derivative_job(
                session,
                job_id=claim.job_id,
                worker_id="renderer-one",
                expected_lock_version=claim.lock_version,
                retry_at=claim_at + timedelta(minutes=2),
                error_code="temporary_io",
                now=claim_at + timedelta(seconds=2),
            )

    retry_at = claim_at + timedelta(minutes=2)
    async with approved_context.database.sessions() as session:
        retried = await retry_derivative_job(
            session,
            job_id=claim.job_id,
            worker_id="renderer-one",
            expected_lock_version=started.lock_version,
            retry_at=retry_at,
            error_code="temporary_io",
            now=claim_at + timedelta(seconds=2),
        )
    assert retried.state == DerivativeJobState.RETRY_WAIT

    async with approved_context.database.sessions() as session:
        assert (
            await claim_derivative_jobs(
                session,
                worker_id="renderer-two",
                limit=1,
                lease_seconds=60,
                now=retry_at - timedelta(seconds=1),
            )
        ) == []
        reclaimed = await claim_derivative_jobs(
            session,
            worker_id="renderer-two",
            limit=1,
            lease_seconds=60,
            now=retry_at,
        )
    assert len(reclaimed) == 1
    assert reclaimed[0].attempt_count == 2
    assert reclaimed[0].lock_version == retried.lock_version + 1

    async with approved_context.database.sessions() as session:
        with pytest.raises(DerivativePipelineLeaseError, match="not active"):
            await start_derivative_job(
                session,
                job_id=claim.job_id,
                worker_id="renderer-one",
                expected_lock_version=reclaimed[0].lock_version,
                now=retry_at + timedelta(seconds=1),
            )


async def _add_derivative_asset(
    context: ApprovedContext,
    *,
    index: int,
    available_at: datetime,
) -> UUID:
    asset_id = uuid4()
    async with context.database.sessions() as session:
        session.add(
            Asset(
                id=asset_id,
                release_id=context.release_id,
                generation_job_id=None,
                output_index=None,
                kind=AssetKind.DERIVATIVE,
                state=AssetState.AVAILABLE,
                storage_backend="s3",
                storage_bucket="derivative-test",
                object_key=f"derivatives/{asset_id}.png",
                object_version_id=f"derivative-version-{index}",
                sha256=f"{index + 10:064x}",
                content_type="image/png",
                image_format="PNG",
                width=1024,
                height=1024,
                byte_size=3_000 + index,
                asset_metadata={"target": "full"},
                available_at=available_at,
            )
        )
        await session.commit()
    return asset_id


@pytest.mark.asyncio
async def test_success_rejects_job_from_no_longer_current_release_version(
    approved_context: ApprovedContext,
) -> None:
    await _plan(approved_context)
    claim_at = PLAN_AT + timedelta(minutes=1)
    async with approved_context.database.sessions() as session:
        claim = (
            await claim_derivative_jobs(
                session,
                worker_id="renderer",
                limit=10,
                lease_seconds=300,
                now=claim_at,
            )
        )[0]
    async with approved_context.database.sessions() as session:
        started = await start_derivative_job(
            session,
            job_id=claim.job_id,
            worker_id="renderer",
            expected_lock_version=claim.lock_version,
            now=claim_at + timedelta(seconds=1),
        )
    asset_id = await _add_derivative_asset(
        approved_context,
        index=90,
        available_at=claim_at + timedelta(seconds=1),
    )
    async with approved_context.database.sessions() as session:
        await record_derivative_output(
            session,
            job_id=claim.job_id,
            target="full",
            asset_id=asset_id,
            worker_id="renderer",
            expected_lock_version=started.lock_version,
            now=claim_at + timedelta(seconds=2),
        )
    async with approved_context.database.sessions() as session:
        release = await session.get(Release, approved_context.release_id)
        assert release is not None
        session.add(
            ReleaseVersion(
                release_id=release.id,
                version_no=2,
                specification={"schema_version": 1, "revision": 2},
                specification_sha256="9" * 64,
                created_by="test",
                created_at=claim_at + timedelta(seconds=3),
            )
        )
        release.current_version_no = 2
        await session.commit()

    async with approved_context.database.sessions() as session:
        with pytest.raises(
            DerivativePipelineConflictError,
            match="completion snapshot is invalid",
        ):
            await succeed_derivative_job(
                session,
                job_id=claim.job_id,
                worker_id="renderer",
                expected_lock_version=started.lock_version,
                now=claim_at + timedelta(seconds=4),
            )
    async with approved_context.database.sessions() as session:
        job = await session.get(DerivativeJob, claim.job_id)
        assert job is not None
        assert job.state == DerivativeJobState.PROCESSING


@pytest.mark.asyncio
async def test_last_required_success_cas_promotes_release_ready_to_publish(
    approved_context: ApprovedContext,
) -> None:
    plan = await _plan(approved_context)
    claim_at = PLAN_AT + timedelta(minutes=1)
    async with approved_context.database.sessions() as session:
        claimed = await claim_derivative_jobs(
            session,
            worker_id="renderer",
            limit=10,
            lease_seconds=300,
            now=claim_at,
        )
    assert {row.job_id for row in claimed} == set(plan.job_ids)

    for index, claim in enumerate(claimed):
        started_at = claim_at + timedelta(seconds=index + 1)
        async with approved_context.database.sessions() as session:
            started = await start_derivative_job(
                session,
                job_id=claim.job_id,
                worker_id="renderer",
                expected_lock_version=claim.lock_version,
                now=started_at,
            )
        asset_id = await _add_derivative_asset(
            approved_context,
            index=index,
            available_at=started_at,
        )
        async with approved_context.database.sessions() as session:
            output = await record_derivative_output(
                session,
                job_id=claim.job_id,
                target="full",
                asset_id=asset_id,
                worker_id="renderer",
                expected_lock_version=started.lock_version,
                now=started_at + timedelta(seconds=1),
            )
        async with approved_context.database.sessions() as session:
            replay = await record_derivative_output(
                session,
                job_id=claim.job_id,
                target="full",
                asset_id=asset_id,
                worker_id="renderer",
                expected_lock_version=started.lock_version,
                now=started_at + timedelta(seconds=2),
            )
        assert replay.output_id == output.output_id
        assert replay.replayed is True

        async with approved_context.database.sessions() as session:
            succeeded = await succeed_derivative_job(
                session,
                job_id=claim.job_id,
                worker_id="renderer",
                expected_lock_version=started.lock_version,
                now=started_at + timedelta(seconds=3),
            )
        assert succeeded.state == DerivativeJobState.SUCCEEDED
        async with approved_context.database.sessions() as session:
            release = await session.get(Release, approved_context.release_id)
            assert release is not None
            expected_phase = (
                ReleasePhase.READY_TO_PUBLISH
                if index == len(claimed) - 1
                else ReleasePhase.RENDERING
            )
            assert release.phase == expected_phase

    async with approved_context.database.sessions() as session:
        outputs = list((await session.scalars(select(DerivativeOutput))).all())
        release = await session.get(Release, approved_context.release_id)
        assert len(outputs) == 2
        assert release is not None
        assert release.phase == ReleasePhase.READY_TO_PUBLISH
        assert release.lock_version == 4
