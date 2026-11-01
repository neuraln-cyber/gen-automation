from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from gen_automation.db.models import Asset, AuditEvent, GenerationAttempt, GenerationJob, Release
from gen_automation.db.session import Database
from gen_automation.domain.enums import (
    AssetState,
    GenerationAttemptState,
    GenerationState,
    ReleasePhase,
)
from gen_automation.integrations.salad.models import SaladJobStatus
from gen_automation.services.generation_recovery import (
    INFRASTRUCTURE_RETRY_GRANT_ACTION,
    STALLED_PROGRESS_RETRY_GRANT_ACTION,
    InfrastructureRetrySource,
    grant_infrastructure_retry,
    recover_dead_lettered_infrastructure_jobs,
)
from gen_automation.services.salad import (
    SALAD_ATTEMPT_WATCHDOG_CANCEL_REQUESTED_ERROR_CODE,
    SALAD_ATTEMPT_WATCHDOG_EXPIRED_ERROR_CODE,
    apply_salad_job_observation,
)
from tests.test_salad_service import (
    NOW,
    REMOTE_JOB_ID,
    add_exhausted_generic_infrastructure_grants,
    add_progress_watchdog_assets,
    prepared_attempt,
    remote_job,
    seed_context,
)
from tests.test_salad_service import database as database


async def seed_stalled_progress(
    session: AsyncSession,
) -> tuple[GenerationJob, GenerationAttempt, AuditEvent, UUID, UUID]:
    context = await seed_context(session)
    attempt_id = await prepared_attempt(session, context)
    attempt = await session.get(GenerationAttempt, attempt_id)
    job = await session.get(GenerationJob, context.job_id)
    assert attempt is not None and job is not None
    attempt.attempt_no = job.attempt_count = job.max_attempts = 6
    attempt.provider_external_id = str(REMOTE_JOB_ID)
    attempt.provider_state = "running"
    attempt.state = GenerationAttemptState.CANCEL_REQUESTED
    attempt.error_code = SALAD_ATTEMPT_WATCHDOG_CANCEL_REQUESTED_ERROR_CODE
    attempt.response_metadata = {"watchdog_reason": "accepted_output_progress_stalled"}
    attempt.started_at = NOW
    job.state = GenerationState.RUNNING
    historical = GenerationAttempt(
        job_id=job.id,
        salad_deployment_id=attempt.salad_deployment_id,
        attempt_no=1,
        provider="salad",
        provider_external_id=str(uuid4()),
        submission_key="a" * 64,
        request_sha256=attempt.request_sha256,
        state=GenerationAttemptState.FAILED,
        worker_image_digest=attempt.worker_image_digest,
        request_metadata={},
        completed_at=NOW - timedelta(hours=1),
        created_at=NOW - timedelta(hours=2),
    )
    session.add(historical)
    await session.flush()
    add_exhausted_generic_infrastructure_grants(session, attempt=historical)
    retained_id, staged_id = await add_progress_watchdog_assets(session, context)
    intent = AuditEvent(
        actor="salad-controller",
        action="generation_attempt.watchdog_cancel_requested",
        resource_type="generation_attempt",
        resource_id=attempt.id,
        correlation_id=str(attempt.id),
        detail={
            "reason": "accepted_output_progress_stalled",
            "provider_external_id": str(REMOTE_JOB_ID),
            "accepted_output_count": 1,
        },
        occurred_at=NOW + timedelta(minutes=12),
    )
    session.add(intent)
    await session.flush()
    return job, attempt, intent, retained_id, staged_id


def definitive_failure(job: GenerationJob, attempt: GenerationAttempt) -> None:
    attempt.state = GenerationAttemptState.FAILED
    attempt.provider_state = "cancelled"
    attempt.completed_at = NOW + timedelta(minutes=13)
    attempt.error_code = SALAD_ATTEMPT_WATCHDOG_EXPIRED_ERROR_CODE
    job.state = GenerationState.DEAD_LETTER
    job.last_error_code = attempt.error_code


@pytest.mark.asyncio
async def test_confirmed_partial_watchdog_has_one_independent_reserve(database: Database) -> None:
    async with database.sessions() as session:
        job, attempt, _, retained_id, _ = await seed_stalled_progress(session)
        original_parameters = dict(job.parameters)
        original_digest = job.parameters_sha256
        # Cancellation intent alone must never authorize concurrent work.
        premature = await grant_infrastructure_retry(
            session,
            attempt=attempt,
            job=job,
            source=InfrastructureRetrySource.RECONCILER,
            actor="test",
            retry_at=NOW,
            occurred_at=NOW,
        )
        assert not premature.granted
        cancelled = remote_job(
            status=SaladJobStatus.CANCELLED,
            metadata={},
            update_time=NOW + timedelta(minutes=13),
        )
        result = await apply_salad_job_observation(
            session,
            generation_attempt_id=attempt.id,
            remote_job=cancelled,
            observed_at=NOW + timedelta(minutes=14),
        )
        assert result.generation_job_state == GenerationState.RETRY_WAIT
        assert job.max_attempts == 7 and job.attempt_count == 6
        replay = await grant_infrastructure_retry(
            session,
            attempt=attempt,
            job=job,
            source=InfrastructureRetrySource.RECONCILER,
            actor="test",
            retry_at=NOW,
            occurred_at=NOW,
        )
        assert not replay.granted
        assert job.max_attempts == 7
        assert job.parameters == original_parameters and job.parameters_sha256 == original_digest
        retained = await session.get(Asset, retained_id)
        assert retained is not None and retained.state == AssetState.AVAILABLE
        assert retained.object_version_id == "retained-version"
        grants = list(
            await session.scalars(
                select(AuditEvent).where(
                    AuditEvent.action == STALLED_PROGRESS_RETRY_GRANT_ACTION,
                )
            )
        )
        assert len(grants) == 1
        assert grants[0].detail["retained_output_count"] == 1
        assert grants[0].detail["grant_limit"] == 1


@pytest.mark.asyncio
async def test_dead_letter_sweep_recovers_partial_progress_after_generic_budget_exhaustion(
    database: Database,
) -> None:
    async with database.sessions() as session:
        job, attempt, _, _, _ = await seed_stalled_progress(session)
        definitive_failure(job, attempt)
        job_id = job.id
        await session.commit()
    async with database.sessions() as session:
        result = await recover_dead_lettered_infrastructure_jobs(
            session,
            actor="test",
            now=NOW + timedelta(minutes=14),
        )
        assert result.recovered_job_ids == (job_id,)
        await session.commit()
    async with database.sessions() as session:
        repeated = await recover_dead_lettered_infrastructure_jobs(
            session,
            actor="test",
            now=NOW + timedelta(minutes=15),
        )
        assert repeated.recovered_job_ids == ()
        job = await session.get(GenerationJob, job_id)
        assert job is not None and job.max_attempts == 7


@pytest.mark.asyncio
async def test_generic_grant_does_not_also_consume_partial_progress_reserve(
    database: Database,
) -> None:
    async with database.sessions() as session:
        job, attempt, _, _, _ = await seed_stalled_progress(session)
        old_grant = await session.scalar(
            select(AuditEvent).where(AuditEvent.action == INFRASTRUCTURE_RETRY_GRANT_ACTION)
        )
        assert old_grant is not None
        await session.delete(old_grant)
        definitive_failure(job, attempt)
        await session.flush()
        for expected in (True, False):
            result = await grant_infrastructure_retry(
                session,
                attempt=attempt,
                job=job,
                source=InfrastructureRetrySource.RECONCILER,
                actor="test",
                retry_at=NOW,
                occurred_at=NOW,
            )
            assert result.granted is expected
        assert job.max_attempts == 7
        assert not await session.scalar(
            select(AuditEvent.id).where(AuditEvent.action == STALLED_PROGRESS_RETRY_GRANT_ACTION)
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "missing_intent",
        "wrong_provider",
        "runtime_timeout",
        "zero_audited_progress",
        "boolean_progress",
        "malformed_progress",
        "no_retained_output",
        "missing_available_time",
        "all_outputs_saved",
        "changed_parameters",
        "operator_stop",
        "reviewing",
        "cancelled_job",
        "stale_attempt",
        "not_confirmed_cancelled",
        "hard_cap",
        "reserve_exhausted",
        "generic_already_granted",
    ],
)
async def test_partial_progress_reserve_fails_closed(database: Database, case: str) -> None:
    async with database.sessions() as session:
        job, attempt, intent, retained_id, staged_id = await seed_stalled_progress(session)
        definitive_failure(job, attempt)
        retained = await session.get(Asset, retained_id)
        staged = await session.get(Asset, staged_id)
        assert retained is not None and staged is not None
        detail = dict(intent.detail)
        if case == "missing_intent":
            await session.delete(intent)
        elif case == "wrong_provider":
            detail["provider_external_id"] = str(uuid4())
        elif case == "runtime_timeout":
            detail["reason"] = "runtime_envelope_expired"
        elif case == "zero_audited_progress":
            detail["accepted_output_count"] = 0
        elif case == "boolean_progress":
            detail["accepted_output_count"] = True
        elif case == "malformed_progress":
            detail["accepted_output_count"] = "unknown"
        elif case == "no_retained_output":
            retained.state = AssetState.UPLOADING
        elif case == "missing_available_time":
            retained.available_at = None
        elif case == "all_outputs_saved":
            staged.state = AssetState.AVAILABLE
            staged.available_at = NOW
            staged.object_key = f"masters/{job.id}/1.png"
            staged.object_version_id = "second-version"
            staged.sha256 = "9" * 64
            staged.content_type = "image/png"
            staged.image_format = "PNG"
            staged.width = staged.height = 1024
            staged.byte_size = 4096
        elif case == "changed_parameters":
            job.parameters_sha256 = "0" * 64
        elif case in {"operator_stop", "reviewing"}:
            release = await session.get(Release, retained.release_id)
            assert release is not None
            if case == "reviewing":
                release.phase = ReleasePhase.REVIEWING
            else:
                session.add(
                    AuditEvent(
                        actor="test",
                        action="release.generation_stop_requested",
                        resource_type="release",
                        resource_id=release.id,
                        correlation_id=str(release.id),
                        detail={},
                        occurred_at=NOW,
                    )
                )
        elif case == "cancelled_job":
            job.state = GenerationState.CANCELLED
        elif case == "stale_attempt":
            job.attempt_count = job.max_attempts = 7
        elif case == "not_confirmed_cancelled":
            attempt.provider_state = "running"
        elif case == "hard_cap":
            job.max_attempts = 10
        elif case in {"reserve_exhausted", "generic_already_granted"}:
            grant_attempt = attempt
            if case == "reserve_exhausted":
                historical = await session.scalar(
                    select(GenerationAttempt).where(
                        GenerationAttempt.job_id == job.id,
                        GenerationAttempt.attempt_no == 1,
                    )
                )
                assert historical is not None
                grant_attempt = historical
            session.add(
                AuditEvent(
                    actor="test",
                    action=(
                        STALLED_PROGRESS_RETRY_GRANT_ACTION
                        if case == "reserve_exhausted"
                        else INFRASTRUCTURE_RETRY_GRANT_ACTION
                    ),
                    resource_type="generation_attempt",
                    resource_id=grant_attempt.id,
                    correlation_id=str(grant_attempt.id),
                    detail={},
                    occurred_at=NOW,
                )
            )
        intent.detail = detail
        await session.flush()
        old_max = job.max_attempts
        result = await grant_infrastructure_retry(
            session,
            attempt=attempt,
            job=job,
            source=InfrastructureRetrySource.RECONCILER,
            actor="test",
            retry_at=NOW,
            occurred_at=NOW,
        )
        assert not result.granted
        assert job.max_attempts == old_max
        # The historical sweep must respect the same fences (not resurrect Stop).
        swept = await recover_dead_lettered_infrastructure_jobs(session, actor="test", now=NOW)
        assert swept.recovered_job_ids == ()
        count = await session.scalar(
            select(func.count(AuditEvent.id)).where(
                AuditEvent.action == STALLED_PROGRESS_RETRY_GRANT_ACTION,
            )
        )
        assert count == (1 if case == "reserve_exhausted" else 0)
