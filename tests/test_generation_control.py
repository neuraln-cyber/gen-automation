from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import func, select

from gen_automation.db.models import (
    Asset,
    AuditEvent,
    GenerationAttempt,
    GenerationJob,
    Project,
    ProviderBudgetGuard,
    Release,
    ReleaseVersion,
    SaladDeployment,
)
from gen_automation.db.session import Database
from gen_automation.domain.canonical import canonical_sha256
from gen_automation.domain.enums import (
    AssetKind,
    AssetState,
    GenerationAttemptState,
    GenerationState,
    ReleasePhase,
    ResourceHealth,
)
from gen_automation.services.generation_control import (
    GENERATION_STOP_ERROR_CODE,
    GENERATION_STOP_REQUESTED_ACTION,
    GENERATION_STOPPED_ACTION,
    GenerationControlConflictError,
    GenerationControlNotFoundError,
    request_generation_stop,
    settle_stopped_generation_once,
)
from gen_automation.services.new_sets import load_new_set_status

NOW = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class GenerationControlContext:
    database: Database
    release_id: UUID
    release_version_id: UUID
    deployment_id: UUID
    job_ids: dict[str, UUID]
    attempt_ids: dict[str, UUID]


def _job(
    *,
    release_version_id: UUID,
    name: str,
    ordinal: int,
    state: GenerationState,
) -> GenerationJob:
    parameters = {"schema_version": 1, "ordinal": ordinal}
    return GenerationJob(
        release_version_id=release_version_id,
        logical_key=f"{ordinal:064x}",
        parameters=parameters,
        parameters_sha256=canonical_sha256(parameters),
        provider="salad",
        state=state,
        priority=100 + ordinal,
        expected_output_count=1,
        attempt_count=int(state not in {GenerationState.QUEUED, GenerationState.RETRY_WAIT}),
        max_attempts=3,
        retry_at=(NOW - timedelta(seconds=1) if state == GenerationState.RETRY_WAIT else None),
        lease_owner=("stale-scheduler" if state == GenerationState.RETRY_WAIT else None),
        lease_expires_at=(
            NOW - timedelta(seconds=1) if state == GenerationState.RETRY_WAIT else None
        ),
        last_error_code=(
            "retryable_provider_error" if state == GenerationState.RETRY_WAIT else None
        ),
        last_error_detail=("retry later" if state == GenerationState.RETRY_WAIT else None),
    )


def _attempt(
    *,
    job_id: UUID,
    deployment: SaladDeployment,
    ordinal: int,
    state: GenerationAttemptState,
) -> GenerationAttempt:
    remote = state != GenerationAttemptState.CREATED
    terminal = state in {
        GenerationAttemptState.SUCCEEDED,
        GenerationAttemptState.FAILED,
        GenerationAttemptState.CANCELLED,
    }
    return GenerationAttempt(
        job_id=job_id,
        salad_deployment_id=deployment.id,
        attempt_no=1,
        provider="salad",
        provider_external_id=(f"provider-job-{ordinal}" if remote else None),
        submission_key=f"{ordinal + 100:064x}",
        request_sha256=f"{ordinal + 200:064x}",
        state=state,
        worker_image_digest=deployment.worker_image_digest,
        request_metadata={},
        submit_started_at=(NOW if remote else None),
        submitted_at=(NOW if remote else None),
        started_at=(NOW if state == GenerationAttemptState.RUNNING else None),
        completed_at=(NOW if terminal else None),
        created_at=NOW,
    )


def _available_asset(
    *,
    release_id: UUID,
    job_id: UUID,
    ordinal: int,
) -> Asset:
    return Asset(
        release_id=release_id,
        generation_job_id=job_id,
        output_index=0,
        kind=AssetKind.RAW_MASTER,
        state=AssetState.AVAILABLE,
        storage_backend="s3",
        storage_bucket="masters",
        object_key=f"releases/{release_id}/master-{ordinal}.png",
        object_version_id=f"version-{ordinal}",
        sha256=f"{ordinal + 300:064x}",
        content_type="image/png",
        image_format="png",
        width=1024,
        height=1024,
        byte_size=1024,
        asset_metadata={},
        available_at=NOW,
    )


@pytest.fixture
async def generation_control_context(
    tmp_path: Path,
) -> AsyncIterator[GenerationControlContext]:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'generation-control.db').as_posix()}")
    await database.create_schema()
    async with database.sessions() as session:
        project = Project(slug="generation-control", name="Generation Control")
        session.add(project)
        await session.flush()
        release = Release(
            project_id=project.id,
            slug="release",
            title="Release",
            phase=ReleasePhase.GENERATING,
            desired_accepted_count=5,
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
        deployment = SaladDeployment(
            version_no=1,
            config_sha256="b" * 64,
            provider_configuration={},
            worker_image_digest=f"registry.example/worker@sha256:{'c' * 64}",
            organization_name="organization",
            project_name="project",
            queue_name="queue",
            container_group_name="group",
            max_hourly_cost_microusd=1_000_000,
        )
        session.add(deployment)
        session.add(
            ProviderBudgetGuard(
                provider="salad",
                daily_limit_microusd=10_000_000,
                monthly_limit_microusd=100_000_000,
                updated_at=NOW,
            )
        )
        await session.flush()

        states = {
            "queued": GenerationState.QUEUED,
            "retry": GenerationState.RETRY_WAIT,
            "claimed": GenerationState.CLAIMED,
            "running": GenerationState.RUNNING,
            "collecting": GenerationState.COLLECTING,
            "verifying": GenerationState.VERIFYING,
            "succeeded": GenerationState.SUCCEEDED,
        }
        jobs: dict[str, GenerationJob] = {}
        for ordinal, (name, state) in enumerate(states.items(), start=1):
            job = _job(
                release_version_id=version.id,
                name=name,
                ordinal=ordinal,
                state=state,
            )
            session.add(job)
            jobs[name] = job
        await session.flush()

        attempts = {
            "claimed": _attempt(
                job_id=jobs["claimed"].id,
                deployment=deployment,
                ordinal=1,
                state=GenerationAttemptState.CREATED,
            ),
            "running": _attempt(
                job_id=jobs["running"].id,
                deployment=deployment,
                ordinal=2,
                state=GenerationAttemptState.RUNNING,
            ),
            "collecting": _attempt(
                job_id=jobs["collecting"].id,
                deployment=deployment,
                ordinal=3,
                state=GenerationAttemptState.SUCCEEDED,
            ),
        }
        session.add_all(attempts.values())
        session.add_all(
            [
                _available_asset(release_id=release.id, job_id=jobs["collecting"].id, ordinal=1),
                _available_asset(release_id=release.id, job_id=jobs["succeeded"].id, ordinal=2),
            ]
        )
        await session.commit()
        context = GenerationControlContext(
            database=database,
            release_id=release.id,
            release_version_id=version.id,
            deployment_id=deployment.id,
            job_ids={name: job.id for name, job in jobs.items()},
            attempt_ids={name: attempt.id for name, attempt in attempts.items()},
        )
    try:
        yield context
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_stop_fences_release_and_only_cancels_definitely_unsubmitted_work(
    generation_control_context: GenerationControlContext,
) -> None:
    async with generation_control_context.database.sessions() as session:
        result = await request_generation_stop(
            session,
            release_id=generation_control_context.release_id,
            actor="owner",
            correlation_id="browser-stop-request",
            now=NOW,
        )

    assert result.phase == ReleasePhase.PAUSED
    assert result.replayed is False
    assert set(result.cancelled_job_ids) == {
        generation_control_context.job_ids["queued"],
        generation_control_context.job_ids["retry"],
        generation_control_context.job_ids["claimed"],
    }
    assert result.cancelled_attempt_ids == (generation_control_context.attempt_ids["claimed"],)
    assert set(result.draining_job_ids) == {
        generation_control_context.job_ids["running"],
        generation_control_context.job_ids["collecting"],
        generation_control_context.job_ids["verifying"],
    }
    assert result.available_asset_count == 2
    assert result.desired_accepted_count == 5

    async with generation_control_context.database.sessions() as session:
        release = await session.get(Release, generation_control_context.release_id)
        assert release is not None
        assert release.phase == ReleasePhase.PAUSED
        jobs = {
            job.id: job
            for job in (
                await session.scalars(
                    select(GenerationJob).where(
                        GenerationJob.release_version_id
                        == generation_control_context.release_version_id
                    )
                )
            ).all()
        }
        for name in ("queued", "retry", "claimed"):
            job = jobs[generation_control_context.job_ids[name]]
            assert job.state == GenerationState.CANCELLED
            assert job.retry_at is None
            assert job.lease_owner is None
            assert job.lease_expires_at is None
            assert job.last_error_code == GENERATION_STOP_ERROR_CODE
        assert jobs[generation_control_context.job_ids["running"]].state == GenerationState.RUNNING
        assert (
            jobs[generation_control_context.job_ids["collecting"]].state
            == GenerationState.COLLECTING
        )
        assert (
            jobs[generation_control_context.job_ids["verifying"]].state == GenerationState.VERIFYING
        )
        created_attempt = await session.get(
            GenerationAttempt,
            generation_control_context.attempt_ids["claimed"],
        )
        assert created_attempt is not None
        assert created_attempt.state == GenerationAttemptState.FAILED
        assert created_attempt.completed_at is not None
        assert created_attempt.provider_external_id is None
        assert created_attempt.error_code == GENERATION_STOP_ERROR_CODE
        running_attempt = await session.get(
            GenerationAttempt,
            generation_control_context.attempt_ids["running"],
        )
        assert running_attempt is not None
        assert running_attempt.state == GenerationAttemptState.RUNNING
        marker_count = await session.scalar(
            select(func.count(AuditEvent.id)).where(
                AuditEvent.resource_id == generation_control_context.release_id,
                AuditEvent.action == GENERATION_STOP_REQUESTED_ACTION,
            )
        )
        assert marker_count == 1


@pytest.mark.asyncio
async def test_stop_request_is_idempotent_and_settlement_waits_for_active_work(
    generation_control_context: GenerationControlContext,
) -> None:
    async with generation_control_context.database.sessions() as session:
        await request_generation_stop(
            session,
            release_id=generation_control_context.release_id,
            now=NOW,
        )
    async with generation_control_context.database.sessions() as session:
        replay = await request_generation_stop(
            session,
            release_id=generation_control_context.release_id,
            now=NOW + timedelta(seconds=1),
        )
    assert replay.replayed is True
    assert replay.cancelled_job_ids == ()
    assert replay.cancelled_attempt_ids == ()

    async with generation_control_context.database.sessions() as session:
        waiting = await settle_stopped_generation_once(
            session,
            release_id=generation_control_context.release_id,
            now=NOW + timedelta(seconds=2),
        )
    assert waiting.settled is False
    assert waiting.phase == ReleasePhase.PAUSED
    assert waiting.desired_accepted_count == 5
    assert set(waiting.draining_job_ids) == {
        generation_control_context.job_ids["running"],
        generation_control_context.job_ids["collecting"],
        generation_control_context.job_ids["verifying"],
    }

    async with generation_control_context.database.sessions() as session:
        marker_count = await session.scalar(
            select(func.count(AuditEvent.id)).where(
                AuditEvent.resource_id == generation_control_context.release_id,
                AuditEvent.action == GENERATION_STOP_REQUESTED_ACTION,
            )
        )
        assert marker_count == 1


@pytest.mark.asyncio
async def test_settlement_preserves_partial_masters_and_clamps_review_target(
    generation_control_context: GenerationControlContext,
) -> None:
    async with generation_control_context.database.sessions() as session:
        await request_generation_stop(
            session,
            release_id=generation_control_context.release_id,
            now=NOW,
        )
    async with generation_control_context.database.sessions() as session:
        for name in ("running", "collecting", "verifying"):
            job = await session.get(GenerationJob, generation_control_context.job_ids[name])
            assert job is not None
            job.state = GenerationState.SUCCEEDED
            job.retry_at = None
            job.lease_owner = None
            job.lease_expires_at = None
        running_attempt = await session.get(
            GenerationAttempt,
            generation_control_context.attempt_ids["running"],
        )
        assert running_attempt is not None
        running_attempt.state = GenerationAttemptState.SUCCEEDED
        running_attempt.completed_at = NOW + timedelta(seconds=5)
        session.add(
            _available_asset(
                release_id=generation_control_context.release_id,
                job_id=generation_control_context.job_ids["running"],
                ordinal=3,
            )
        )
        await session.commit()

    async with generation_control_context.database.sessions() as session:
        settled = await settle_stopped_generation_once(
            session,
            release_id=generation_control_context.release_id,
            now=NOW + timedelta(seconds=6),
        )
    assert settled.settled is True
    assert settled.replayed is False
    assert settled.phase == ReleasePhase.REVIEWING
    assert settled.available_asset_count == 3
    assert settled.desired_accepted_count == 3
    assert settled.draining_job_ids == ()

    async with generation_control_context.database.sessions() as session:
        release = await session.get(Release, generation_control_context.release_id)
        assert release is not None
        assert release.phase == ReleasePhase.REVIEWING
        assert release.desired_accepted_count == 3
        stopped_count = await session.scalar(
            select(func.count(AuditEvent.id)).where(
                AuditEvent.resource_id == generation_control_context.release_id,
                AuditEvent.action == GENERATION_STOPPED_ACTION,
            )
        )
        assert stopped_count == 1

    async with generation_control_context.database.sessions() as session:
        replay = await settle_stopped_generation_once(
            session,
            release_id=generation_control_context.release_id,
            now=NOW + timedelta(seconds=7),
        )
    assert replay.settled is True
    assert replay.replayed is True
    assert replay.phase == ReleasePhase.REVIEWING
    assert replay.desired_accepted_count == 3

    async with generation_control_context.database.sessions() as session:
        release = await session.get(Release, generation_control_context.release_id)
        assert release is not None
        release.phase = ReleasePhase.PUBLISHED
        await session.commit()
    async with generation_control_context.database.sessions() as session:
        post_review_replay = await request_generation_stop(
            session,
            release_id=generation_control_context.release_id,
            now=NOW + timedelta(seconds=8),
        )
    assert post_review_replay.replayed is True
    assert post_review_replay.phase == ReleasePhase.PUBLISHED
    async with generation_control_context.database.sessions() as session:
        release = await session.get(Release, generation_control_context.release_id)
        assert release is not None
        assert release.phase == ReleasePhase.PUBLISHED


@pytest.mark.asyncio
async def test_late_retry_is_cancelled_instead_of_restarted_before_settlement(
    generation_control_context: GenerationControlContext,
) -> None:
    async with generation_control_context.database.sessions() as session:
        await request_generation_stop(
            session,
            release_id=generation_control_context.release_id,
            now=NOW,
        )
    async with generation_control_context.database.sessions() as session:
        running = await session.get(
            GenerationJob,
            generation_control_context.job_ids["running"],
        )
        collecting = await session.get(
            GenerationJob,
            generation_control_context.job_ids["collecting"],
        )
        verifying = await session.get(
            GenerationJob,
            generation_control_context.job_ids["verifying"],
        )
        running_attempt = await session.get(
            GenerationAttempt,
            generation_control_context.attempt_ids["running"],
        )
        assert running is not None
        assert collecting is not None
        assert verifying is not None
        assert running_attempt is not None
        running.state = GenerationState.RETRY_WAIT
        running.retry_at = NOW + timedelta(seconds=1)
        running_attempt.state = GenerationAttemptState.FAILED
        running_attempt.completed_at = NOW + timedelta(seconds=1)
        collecting.state = GenerationState.SUCCEEDED
        verifying.state = GenerationState.SUCCEEDED
        await session.commit()

    async with generation_control_context.database.sessions() as session:
        settled = await settle_stopped_generation_once(
            session,
            release_id=generation_control_context.release_id,
            now=NOW + timedelta(seconds=2),
        )
    assert settled.settled is True
    assert settled.phase == ReleasePhase.REVIEWING
    assert settled.cancelled_job_ids == (generation_control_context.job_ids["running"],)
    assert settled.available_asset_count == 2
    assert settled.desired_accepted_count == 2
    async with generation_control_context.database.sessions() as session:
        running = await session.get(
            GenerationJob,
            generation_control_context.job_ids["running"],
        )
        assert running is not None
        assert running.state == GenerationState.CANCELLED
        assert running.retry_at is None


@pytest.mark.asyncio
async def test_stopped_provider_failure_keeps_healthy_partial_set_reviewable(
    generation_control_context: GenerationControlContext,
) -> None:
    async with generation_control_context.database.sessions() as session:
        await request_generation_stop(
            session,
            release_id=generation_control_context.release_id,
            now=NOW,
        )

    async with generation_control_context.database.sessions() as session:
        for name in ("collecting", "verifying"):
            job = await session.get(GenerationJob, generation_control_context.job_ids[name])
            assert job is not None
            job.state = GenerationState.SUCCEEDED
            job.retry_at = None
            job.lease_owner = None
            job.lease_expires_at = None
        failed_job = await session.get(
            GenerationJob,
            generation_control_context.job_ids["running"],
        )
        failed_attempt = await session.get(
            GenerationAttempt,
            generation_control_context.attempt_ids["running"],
        )
        assert failed_job is not None and failed_attempt is not None
        failed_job.state = GenerationState.FAILED
        failed_job.last_error_code = "provider_worker_failed"
        failed_attempt.state = GenerationAttemptState.FAILED
        failed_attempt.completed_at = NOW + timedelta(seconds=1)
        failed_attempt.error_code = "provider_worker_failed"
        await session.commit()

    async with generation_control_context.database.sessions() as session:
        settled = await settle_stopped_generation_once(
            session,
            release_id=generation_control_context.release_id,
            now=NOW + timedelta(seconds=2),
        )
        status = await load_new_set_status(
            session,
            release_id=generation_control_context.release_id,
        )

    assert settled.settled is True
    assert settled.phase == ReleasePhase.REVIEWING
    assert status.phase == ReleasePhase.REVIEWING
    assert status.health == ResourceHealth.HEALTHY
    assert status.stage.key.value == "scoring"
    assert status.error is None
    assert status.jobs.failed == 1


@pytest.mark.asyncio
async def test_settlement_waits_for_pending_master_salvage(
    generation_control_context: GenerationControlContext,
) -> None:
    async with generation_control_context.database.sessions() as session:
        await request_generation_stop(
            session,
            release_id=generation_control_context.release_id,
            now=NOW,
        )

    async with generation_control_context.database.sessions() as session:
        for name in ("running", "collecting", "verifying"):
            job = await session.get(GenerationJob, generation_control_context.job_ids[name])
            assert job is not None
            job.state = GenerationState.SUCCEEDED
            job.retry_at = None
            job.lease_owner = None
            job.lease_expires_at = None
        running_attempt = await session.get(
            GenerationAttempt,
            generation_control_context.attempt_ids["running"],
        )
        assert running_attempt is not None
        running_attempt.state = GenerationAttemptState.SUCCEEDED
        running_attempt.completed_at = NOW + timedelta(seconds=1)
        pending_asset = Asset(
            release_id=generation_control_context.release_id,
            generation_job_id=generation_control_context.job_ids["running"],
            output_index=1,
            kind=AssetKind.RAW_MASTER,
            state=AssetState.UPLOADING,
            storage_backend="s3",
            storage_bucket="masters",
            staging_object_key=(
                f"staging/{generation_control_context.release_id}/pending-master.png"
            ),
            asset_metadata={},
        )
        session.add(pending_asset)
        await session.commit()
        pending_asset_id = pending_asset.id

    async with generation_control_context.database.sessions() as session:
        waiting = await settle_stopped_generation_once(
            session,
            release_id=generation_control_context.release_id,
            now=NOW + timedelta(seconds=2),
        )
    assert waiting.settled is False
    assert waiting.phase == ReleasePhase.PAUSED
    assert waiting.draining_job_ids == ()
    assert waiting.available_asset_count == 2

    async with generation_control_context.database.sessions() as session:
        stopped_count = await session.scalar(
            select(func.count(AuditEvent.id)).where(
                AuditEvent.resource_id == generation_control_context.release_id,
                AuditEvent.action == GENERATION_STOPPED_ACTION,
            )
        )
        assert stopped_count == 0
        pending_asset = await session.get(Asset, pending_asset_id)
        assert pending_asset is not None
        pending_asset.state = AssetState.QUARANTINED
        await session.commit()

    async with generation_control_context.database.sessions() as session:
        settled = await settle_stopped_generation_once(
            session,
            release_id=generation_control_context.release_id,
            now=NOW + timedelta(seconds=3),
        )
    assert settled.settled is True
    assert settled.phase == ReleasePhase.REVIEWING
    assert settled.available_asset_count == 2


@pytest.mark.asyncio
async def test_unhealthy_stop_settles_cancelled_without_hiding_block_or_discarding_assets(
    generation_control_context: GenerationControlContext,
) -> None:
    async with generation_control_context.database.sessions() as session:
        release = await session.get(Release, generation_control_context.release_id)
        assert release is not None
        release.health = ResourceHealth.BLOCKED
        await session.commit()

    async with generation_control_context.database.sessions() as session:
        await request_generation_stop(
            session,
            release_id=generation_control_context.release_id,
            now=NOW,
        )

    async with generation_control_context.database.sessions() as session:
        for name in ("running", "collecting", "verifying"):
            job = await session.get(GenerationJob, generation_control_context.job_ids[name])
            assert job is not None
            job.state = GenerationState.SUCCEEDED
            job.retry_at = None
            job.lease_owner = None
            job.lease_expires_at = None
        running_attempt = await session.get(
            GenerationAttempt,
            generation_control_context.attempt_ids["running"],
        )
        assert running_attempt is not None
        running_attempt.state = GenerationAttemptState.SUCCEEDED
        running_attempt.completed_at = NOW + timedelta(seconds=1)
        await session.commit()

    async with generation_control_context.database.sessions() as session:
        settled = await settle_stopped_generation_once(
            session,
            release_id=generation_control_context.release_id,
            now=NOW + timedelta(seconds=2),
        )
    assert settled.settled is True
    assert settled.phase == ReleasePhase.CANCELLED
    assert settled.available_asset_count == 2
    assert settled.desired_accepted_count == 5

    async with generation_control_context.database.sessions() as session:
        release = await session.get(Release, generation_control_context.release_id)
        assert release is not None
        assert release.phase == ReleasePhase.CANCELLED
        assert release.health == ResourceHealth.BLOCKED
        assert release.desired_accepted_count == 5
        retained_asset_count = await session.scalar(
            select(func.count(Asset.id)).where(
                Asset.release_id == generation_control_context.release_id,
                Asset.state == AssetState.AVAILABLE,
            )
        )
        assert retained_asset_count == 2

        status = await load_new_set_status(
            session,
            release_id=generation_control_context.release_id,
        )
        assert status.stop_requested is True
        assert status.stop_settled is True
        assert status.stage.key.value == "error"
        assert status.error is not None
        assert status.error.code == "release_blocked"


@pytest.mark.asyncio
async def test_stop_settled_status_uses_durable_audit_marker_instead_of_phase(
    generation_control_context: GenerationControlContext,
) -> None:
    async with generation_control_context.database.sessions() as session:
        await request_generation_stop(
            session,
            release_id=generation_control_context.release_id,
            now=NOW,
        )

    async with generation_control_context.database.sessions() as session:
        release = await session.get(Release, generation_control_context.release_id)
        assert release is not None
        release.phase = ReleasePhase.CANCELLED
        await session.commit()

    async with generation_control_context.database.sessions() as session:
        phase_only = await load_new_set_status(
            session,
            release_id=generation_control_context.release_id,
        )
        assert phase_only.stop_requested is True
        assert phase_only.stop_settled is False

        session.add(
            AuditEvent(
                actor="test",
                action=GENERATION_STOPPED_ACTION,
                resource_type="release",
                resource_id=generation_control_context.release_id,
                correlation_id=f"generation-stop:{generation_control_context.release_id}",
                detail={"test_marker": True},
                occurred_at=NOW + timedelta(seconds=1),
            )
        )
        release = await session.get(Release, generation_control_context.release_id)
        assert release is not None
        release.phase = ReleasePhase.PAUSED
        await session.commit()

    async with generation_control_context.database.sessions() as session:
        marker_backed = await load_new_set_status(
            session,
            release_id=generation_control_context.release_id,
        )
        assert marker_backed.stop_requested is True
        assert marker_backed.stop_settled is True


@pytest.mark.asyncio
async def test_zero_output_stop_finishes_cancelled_without_violating_positive_target(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'zero-output-stop.db').as_posix()}")
    await database.create_schema()
    try:
        async with database.sessions() as session:
            project = Project(slug="zero-output", name="Zero Output")
            session.add(project)
            await session.flush()
            release = Release(
                project_id=project.id,
                slug="release",
                title="Release",
                phase=ReleasePhase.READY,
                desired_accepted_count=5,
            )
            session.add(release)
            await session.flush()
            version = ReleaseVersion(
                release_id=release.id,
                version_no=1,
                specification={"schema_version": 1},
                specification_sha256="d" * 64,
                created_by="test",
                created_at=NOW,
            )
            session.add(version)
            await session.flush()
            session.add(
                _job(
                    release_version_id=version.id,
                    name="queued",
                    ordinal=99,
                    state=GenerationState.QUEUED,
                )
            )
            await session.commit()
            release_id = release.id

        async with database.sessions() as session:
            requested = await request_generation_stop(
                session,
                release_id=release_id,
                now=NOW,
            )
        assert requested.phase == ReleasePhase.PAUSED
        async with database.sessions() as session:
            settled = await settle_stopped_generation_once(
                session,
                release_id=release_id,
                now=NOW + timedelta(seconds=1),
            )
        assert settled.settled is True
        assert settled.phase == ReleasePhase.CANCELLED
        assert settled.available_asset_count == 0
        assert settled.desired_accepted_count == 5
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_stop_rejects_missing_or_non_generation_release(
    generation_control_context: GenerationControlContext,
) -> None:
    async with generation_control_context.database.sessions() as session:
        release = await session.get(Release, generation_control_context.release_id)
        assert release is not None
        release.phase = ReleasePhase.REVIEWING
        await session.commit()
    async with generation_control_context.database.sessions() as session:
        with pytest.raises(GenerationControlConflictError):
            await request_generation_stop(
                session,
                release_id=generation_control_context.release_id,
                now=NOW,
            )
        await session.rollback()

    async with generation_control_context.database.sessions() as session:
        with pytest.raises(GenerationControlNotFoundError):
            await request_generation_stop(
                session,
                release_id=UUID("00000000-0000-0000-0000-000000000001"),
                now=NOW,
            )
