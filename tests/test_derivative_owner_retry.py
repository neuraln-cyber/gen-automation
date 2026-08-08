# ruff: noqa: F811

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from gen_automation.db.models import (
    AuditEvent,
    DerivativeJob,
    OutboxEvent,
    Release,
    ReleaseVersion,
)
from gen_automation.domain.enums import DerivativeJobState, ReleasePhase
from gen_automation.services.derivative_pipeline import (
    DerivativePipelineConflictError,
    claim_derivative_jobs,
    create_derivative_recipe_and_plan,
    fail_derivative_job,
    start_derivative_job,
)
from gen_automation.services.review_derivatives import (
    retry_failed_completed_review_full_outputs,
)
from tests.test_derivative_pipeline import PLAN_AT, ApprovedContext, _plan
from tests.test_derivative_pipeline import (
    approved_context as derivative_approved_context,  # noqa: F401
)


async def _fail_all_jobs(
    context: ApprovedContext,
    *,
    error_code: str = "render_failed",
    claim_at: datetime | None = None,
) -> tuple[DerivativeJob, ...]:
    claimed_at = claim_at or PLAN_AT + timedelta(minutes=1)
    async with context.database.sessions() as session:
        claims = await claim_derivative_jobs(
            session,
            worker_id="retry-test-worker",
            limit=100,
            lease_seconds=300,
            now=claimed_at,
        )
    for index, claim in enumerate(claims):
        transition_at = claimed_at + timedelta(seconds=index * 3 + 1)
        async with context.database.sessions() as session:
            started = await start_derivative_job(
                session,
                job_id=claim.job_id,
                worker_id="retry-test-worker",
                expected_lock_version=claim.lock_version,
                now=transition_at,
            )
        async with context.database.sessions() as session:
            await fail_derivative_job(
                session,
                job_id=claim.job_id,
                worker_id="retry-test-worker",
                expected_lock_version=started.lock_version,
                error_code=error_code,
                error_detail="Synthetic terminal failure for owner retry coverage.",
                now=transition_at + timedelta(seconds=1),
            )
    async with context.database.sessions() as session:
        return tuple(
            (await session.scalars(select(DerivativeJob).order_by(DerivativeJob.logical_key))).all()
        )


@pytest.mark.asyncio
async def test_owner_retry_rearms_only_failed_full_jobs_without_changing_identity(
    derivative_approved_context: ApprovedContext,
) -> None:
    context = derivative_approved_context
    await _plan(context)
    failed = await _fail_all_jobs(context)
    frozen = {
        job.id: (
            job.release_selection_id,
            job.derivative_recipe_id,
            job.release_version_id,
            job.logical_key,
            job.request_payload,
            job.request_sha256,
            job.attempt_count,
            job.lock_version,
        )
        for job in failed
    }
    retry_at = PLAN_AT + timedelta(minutes=3)

    async with context.database.sessions() as session:
        result = await retry_failed_completed_review_full_outputs(
            session,
            review_task_id=context.review_task_id,
            actor_user_id=context.owner_id,
            idempotency_key="retry-failed-full-set",
            retry_allowance=3,
            expected_failed_job_ids=tuple(job.id for job in failed),
            now=retry_at,
        )

    assert result.failed_jobs_found == 2
    assert result.jobs_retried == 2
    assert set(result.retried_job_ids) == {job.id for job in failed}
    assert result.replayed is False

    async with context.database.sessions() as session:
        jobs = tuple(
            (await session.scalars(select(DerivativeJob).order_by(DerivativeJob.logical_key))).all()
        )
        release = await session.get(Release, context.release_id)
        audits = tuple(
            (
                await session.scalars(
                    select(AuditEvent).where(
                        AuditEvent.action == "derivative.job_owner_retry_scheduled"
                    )
                )
            ).all()
        )
        outbox = await session.scalar(
            select(OutboxEvent).where(
                OutboxEvent.topic == "derivative.full_outputs.retry_requested"
            )
        )
    assert release is not None
    assert release.phase == ReleasePhase.RENDERING
    assert len(audits) == 2
    assert all(event.detail["previous_error_code"] == "render_failed" for event in audits)
    assert outbox is not None
    for job in jobs:
        identity = frozen[job.id]
        assert (
            job.release_selection_id,
            job.derivative_recipe_id,
            job.release_version_id,
            job.logical_key,
            job.request_payload,
            job.request_sha256,
        ) == identity[:6]
        assert job.attempt_count == identity[6]
        assert job.lock_version == identity[7] + 1
        assert job.max_attempts == 6
        assert job.state == DerivativeJobState.RETRY_WAIT
        assert job.retry_at is not None
        assert job.retry_at.replace(tzinfo=UTC) == retry_at
        assert job.completed_at is None
        assert job.last_error_code is None
        assert job.last_error_detail is None
        assert job.lease_owner is None
        assert job.lease_expires_at is None

    async with context.database.sessions() as session:
        replay = await retry_failed_completed_review_full_outputs(
            session,
            review_task_id=context.review_task_id,
            actor_user_id=context.owner_id,
            idempotency_key="retry-failed-full-set",
            retry_allowance=3,
            expected_failed_job_ids=tuple(job.id for job in failed),
            now=retry_at + timedelta(seconds=1),
        )
    assert replay.replayed is True
    assert replay.retried_job_ids == result.retried_job_ids

    async with context.database.sessions() as session:
        no_op = await retry_failed_completed_review_full_outputs(
            session,
            review_task_id=context.review_task_id,
            actor_user_id=context.owner_id,
            idempotency_key="retry-full-set-while-active",
            now=retry_at + timedelta(seconds=2),
        )
    assert no_op.jobs_retried == 0
    assert no_op.failed_jobs_found == 0
    assert no_op.replayed is False


@pytest.mark.asyncio
async def test_owner_retry_refuses_immutable_output_object_conflict(
    derivative_approved_context: ApprovedContext,
) -> None:
    context = derivative_approved_context
    await _plan(context)
    failed = await _fail_all_jobs(context, error_code="output_object_conflict")

    async with context.database.sessions() as session:
        with pytest.raises(
            DerivativePipelineConflictError,
            match="immutable derivative output object conflicts",
        ):
            await retry_failed_completed_review_full_outputs(
                session,
                review_task_id=context.review_task_id,
                actor_user_id=context.owner_id,
                idempotency_key="do-not-retry-object-conflict",
                now=PLAN_AT + timedelta(minutes=3),
            )
        await session.rollback()

    async with context.database.sessions() as session:
        jobs = tuple((await session.scalars(select(DerivativeJob))).all())
    assert {job.id for job in jobs} == {job.id for job in failed}
    assert all(job.state == DerivativeJobState.FAILED for job in jobs)
    assert all(job.max_attempts == 3 for job in jobs)


@pytest.mark.asyncio
async def test_owner_retry_is_bounded_by_hard_total_attempt_cap(
    derivative_approved_context: ApprovedContext,
) -> None:
    context = derivative_approved_context
    async with context.database.sessions() as session:
        await create_derivative_recipe_and_plan(
            session,
            review_task_id=context.review_task_id,
            configuration={"full": {"format": "PNG"}},
            recipe_version=1,
            renderer_version="renderer-v1",
            pillow_version="12.0.0",
            created_by_user_id=context.owner_id,
            approved_by_user_id=context.owner_id,
            idempotency_key="plan-bounded-owner-retry",
            output_targets=("full",),
            max_attempts=7,
            now=PLAN_AT,
        )
    await _fail_all_jobs(context)

    async with context.database.sessions() as session:
        first = await retry_failed_completed_review_full_outputs(
            session,
            review_task_id=context.review_task_id,
            actor_user_id=context.owner_id,
            idempotency_key="bounded-owner-retry-first",
            now=PLAN_AT + timedelta(minutes=3),
        )
    assert first.jobs_retried == 2
    async with context.database.sessions() as session:
        jobs = tuple((await session.scalars(select(DerivativeJob))).all())
    assert all(job.max_attempts == 10 for job in jobs)

    await _fail_all_jobs(context, claim_at=PLAN_AT + timedelta(minutes=4))
    async with context.database.sessions() as session:
        with pytest.raises(
            DerivativePipelineConflictError,
            match="owner retry attempt cap",
        ):
            await retry_failed_completed_review_full_outputs(
                session,
                review_task_id=context.review_task_id,
                actor_user_id=context.owner_id,
                idempotency_key="bounded-owner-retry-second",
                now=PLAN_AT + timedelta(minutes=6),
            )
        await session.rollback()


@pytest.mark.asyncio
async def test_owner_retry_rejects_a_stale_failed_job_snapshot(
    derivative_approved_context: ApprovedContext,
) -> None:
    context = derivative_approved_context
    await _plan(context)
    failed = await _fail_all_jobs(context)

    async with context.database.sessions() as session:
        with pytest.raises(
            DerivativePipelineConflictError,
            match="snapshot changed",
        ):
            await retry_failed_completed_review_full_outputs(
                session,
                review_task_id=context.review_task_id,
                actor_user_id=context.owner_id,
                idempotency_key="stale-failed-snapshot",
                expected_failed_job_ids=(failed[0].id,),
                now=PLAN_AT + timedelta(minutes=3),
            )
        await session.rollback()


@pytest.mark.asyncio
async def test_sqlite_guard_rejects_direct_rearm_of_output_object_conflict(
    derivative_approved_context: ApprovedContext,
) -> None:
    context = derivative_approved_context
    await _plan(context)
    failed = await _fail_all_jobs(context, error_code="output_object_conflict")

    async with context.database.sessions() as session:
        job = await session.get(DerivativeJob, failed[0].id)
        assert job is not None
        job.state = DerivativeJobState.RETRY_WAIT
        job.max_attempts = 6
        job.retry_at = PLAN_AT + timedelta(minutes=3)
        job.completed_at = None
        job.last_error_code = None
        job.last_error_detail = None
        job.lock_version += 1
        with pytest.raises(IntegrityError, match="failed derivative job rearm is invalid"):
            await session.commit()
        await session.rollback()


@pytest.mark.asyncio
async def test_sqlite_guard_rejects_failed_rearm_without_more_attempts(
    derivative_approved_context: ApprovedContext,
) -> None:
    context = derivative_approved_context
    await _plan(context)
    failed = await _fail_all_jobs(context)

    async with context.database.sessions() as session:
        job = await session.get(DerivativeJob, failed[0].id)
        assert job is not None
        job.state = DerivativeJobState.RETRY_WAIT
        job.retry_at = PLAN_AT + timedelta(minutes=3)
        job.completed_at = None
        job.last_error_code = None
        job.last_error_detail = None
        job.lock_version += 1
        with pytest.raises(IntegrityError, match="failed derivative job rearm is invalid"):
            await session.commit()
        await session.rollback()


@pytest.mark.asyncio
async def test_sqlite_guard_rejects_every_other_failed_job_resurrection(
    derivative_approved_context: ApprovedContext,
) -> None:
    context = derivative_approved_context
    await _plan(context)
    failed = await _fail_all_jobs(context)

    async with context.database.sessions() as session:
        job = await session.get(DerivativeJob, failed[0].id)
        assert job is not None
        job.state = DerivativeJobState.REQUESTED
        job.completed_at = None
        job.last_error_code = None
        job.last_error_detail = None
        job.lock_version += 1
        with pytest.raises(IntegrityError, match="derivative job state transition is invalid"):
            await session.commit()
        await session.rollback()


@pytest.mark.asyncio
async def test_owner_retry_rejects_noncurrent_completed_review(
    derivative_approved_context: ApprovedContext,
) -> None:
    context = derivative_approved_context
    await _plan(context)
    await _fail_all_jobs(context)
    async with context.database.sessions() as session:
        release = await session.get(Release, context.release_id)
        assert release is not None
        session.add(
            ReleaseVersion(
                release_id=release.id,
                version_no=2,
                specification={"schema_version": 1, "revision": 2},
                specification_sha256="f" * 64,
                created_by="retry-stale-test",
                created_at=PLAN_AT + timedelta(minutes=3),
            )
        )
        release.current_version_no = 2
        await session.commit()

    async with context.database.sessions() as session:
        with pytest.raises(
            DerivativePipelineConflictError,
            match="current release version",
        ):
            await retry_failed_completed_review_full_outputs(
                session,
                review_task_id=context.review_task_id,
                actor_user_id=context.owner_id,
                idempotency_key="retry-stale-review-version",
                now=PLAN_AT + timedelta(minutes=4),
            )
        await session.rollback()
