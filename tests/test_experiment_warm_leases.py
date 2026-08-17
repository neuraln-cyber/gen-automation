from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gen_automation.db.models import (
    AuditEvent,
    ExperimentWarmLease,
    GenerationAttempt,
    GenerationJob,
    OutboxEvent,
    Project,
    Release,
    ReleaseVersion,
    SaladDeployment,
)
from gen_automation.db.session import Database
from gen_automation.domain.canonical import canonical_sha256
from gen_automation.domain.enums import (
    BudgetState,
    DesiredDeploymentState,
    ExperimentWarmLeaseState,
    GenerationAttemptState,
    GenerationState,
    OutboxStatus,
    ReleasePhase,
    ResourceHealth,
    SaladDeploymentPurpose,
    SaladDeploymentState,
    SpendEntryType,
)
from gen_automation.services.budgets import ensure_budget_guard, record_spend_entry
from gen_automation.services.experiment_warm_leases import (
    ExperimentWarmLeaseBudgetError,
    ExperimentWarmLeaseConflictError,
    activate_ready_experiment_warm_leases,
    effective_experiment_min_replicas,
    ensure_experiment_warm_lease,
    expire_experiment_warm_leases,
    extend_experiment_warm_lease,
    get_current_experiment_warm_lease_status,
    mark_experiment_warm_runtime_refreshed_locked,
    start_experiment_warm_lease,
    touch_completed_experiment_warm_leases,
)
from gen_automation.services.outbox import SALAD_JOB_SUBMIT_TOPIC
from gen_automation.services.salad import prepare_generation_attempt
from gen_automation.services.salad_deployments import (
    _desired_queue_autoscaler,
    effective_worker_min_replicas,
)

NOW = datetime(2026, 8, 5, 9, 0, tzinfo=UTC)
IMAGE_DIGEST = "registry.example.test/worker@sha256:" + "a" * 64


@dataclass(frozen=True, slots=True)
class WarmContext:
    database: Database
    deployment_id: UUID
    release_id: UUID
    release_version_id: UUID


@pytest.fixture
async def warm_context(tmp_path: Path) -> AsyncIterator[WarmContext]:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'warm-lease.db').as_posix()}")
    await database.create_schema()
    async with database.sessions() as session:
        await ensure_budget_guard(
            session,
            provider="salad",
            daily_limit_usd=Decimal("100"),
            monthly_limit_usd=Decimal("1000"),
            now=NOW,
        )
        project = Project(slug="experiment-warm", name="Experiment Warm")
        session.add(project)
        await session.flush()
        release = Release(
            project_id=project.id,
            slug="warm-test",
            title="Warm test",
            desired_accepted_count=1,
            phase=ReleasePhase.READY,
        )
        session.add(release)
        await session.flush()
        version = ReleaseVersion(
            release_id=release.id,
            version_no=1,
            specification={"schema_version": 1},
            specification_sha256="b" * 64,
            created_by="test",
            created_at=NOW,
        )
        deployment = SaladDeployment(
            version_no=1,
            config_sha256="c" * 64,
            provider_configuration={
                "container": {},
                "queue_autoscaler": {"polling_period": 30},
            },
            worker_image_digest=IMAGE_DIGEST,
            organization_name="organization",
            project_name="project",
            queue_name="generation",
            provider_queue_id="queue-id",
            container_group_name="worker",
            provider_container_group_id="group-id",
            state=SaladDeploymentState.ACTIVE,
            desired_state=DesiredDeploymentState.ACTIVE,
            is_current=True,
            min_replicas=0,
            max_replicas=1,
            desired_queue_length=1,
            max_hourly_cost_microusd=360_000,
            observed_replicas=0,
            ready_replicas=0,
            created_at=NOW,
            updated_at=NOW,
        )
        session.add_all([version, deployment])
        await session.commit()
        context = WarmContext(
            database=database,
            deployment_id=deployment.id,
            release_id=release.id,
            release_version_id=version.id,
        )
    try:
        yield context
    finally:
        await database.dispose()


async def _add_generation_job(
    session: AsyncSession,
    context: WarmContext,
    *,
    suffix: int,
    state: GenerationState,
    provider: str = "salad",
    retry_at: datetime | None = None,
) -> GenerationJob:
    parameters = {"seed": suffix}
    job = GenerationJob(
        release_version_id=context.release_version_id,
        logical_key=f"{suffix:064x}",
        parameters=parameters,
        parameters_sha256=canonical_sha256(parameters),
        provider=provider,
        state=state,
        expected_output_count=1,
        retry_at=retry_at,
    )
    session.add(job)
    await session.flush()
    return job


async def _prepare_attempt(
    session: AsyncSession,
    context: WarmContext,
    *,
    suffix: int,
    state: GenerationAttemptState,
    deployment_id: UUID | None = None,
) -> tuple[GenerationJob, GenerationAttempt]:
    job = await _add_generation_job(
        session,
        context,
        suffix=suffix,
        state=GenerationState.QUEUED,
    )
    prepared = await prepare_generation_attempt(
        session,
        generation_job_id=job.id,
        salad_deployment_id=deployment_id or context.deployment_id,
        idempotency_key=f"warm-attempt-{suffix}",
        now=NOW,
    )
    attempt = await session.get(GenerationAttempt, prepared.generation_attempt_id)
    assert attempt is not None
    attempt.state = state
    if state in {GenerationAttemptState.SUBMITTED, GenerationAttemptState.RUNNING}:
        job.state = GenerationState.RUNNING
    elif state == GenerationAttemptState.UNKNOWN:
        job.state = GenerationState.UNKNOWN
    elif state == GenerationAttemptState.SUBMITTING:
        job.state = GenerationState.SUBMITTING
    elif state == GenerationAttemptState.CANCEL_REQUESTED:
        job.state = GenerationState.CANCEL_REQUESTED
    if state in {
        GenerationAttemptState.SUBMITTED,
        GenerationAttemptState.RUNNING,
        GenerationAttemptState.UNKNOWN,
        GenerationAttemptState.CANCEL_REQUESTED,
    }:
        attempt.provider_external_id = f"provider-job-{suffix}"
    await session.flush()
    return job, attempt


async def _start_refresh_and_activate(
    context: WarmContext,
    *,
    activated_at: datetime,
) -> UUID:
    async with context.database.sessions() as session:
        status = await start_experiment_warm_lease(
            session,
            salad_deployment_id=context.deployment_id,
            actor="test",
            now=NOW,
        )
        lease = await session.get(ExperimentWarmLease, status.lease_id)
        assert lease is not None
        mark_experiment_warm_runtime_refreshed_locked(
            session,
            lease,
            provider_version=7,
            actor="test",
            now=NOW + timedelta(seconds=1),
        )
        deployment = await session.get(SaladDeployment, context.deployment_id)
        assert deployment is not None
        deployment.observed_replicas = 1
        deployment.ready_replicas = 1
        await session.commit()

    async with context.database.sessions() as session:
        activated = await activate_ready_experiment_warm_leases(
            session,
            actor="test",
            now=activated_at,
        )
        assert activated == (status.lease_id,)
        await session.commit()
    return status.lease_id


@pytest.mark.asyncio
async def test_allocation_does_not_consume_the_idle_testing_window(
    warm_context: WarmContext,
) -> None:
    async with warm_context.database.sessions() as session:
        started = await start_experiment_warm_lease(
            session,
            salad_deployment_id=warm_context.deployment_id,
            actor="test",
            now=NOW,
        )
        await session.commit()
        assert started.state == ExperimentWarmLeaseState.STARTING
        assert started.remaining_seconds == 0
        assert started.hard_remaining_seconds == 90 * 60
        assert started.expires_at == started.hard_expires_at
        assert started.idle_ttl_seconds == 15 * 60
        assert started.max_cost_microusd == 540_000
        assert (
            await effective_experiment_min_replicas(
                session,
                salad_deployment_id=warm_context.deployment_id,
                now=NOW,
            )
            == 0
        )

        lease = await session.get(ExperimentWarmLease, started.lease_id)
        assert lease is not None
        mark_experiment_warm_runtime_refreshed_locked(
            session,
            lease,
            provider_version=9,
            actor="test",
            now=NOW + timedelta(seconds=1),
        )
        assert (
            await effective_experiment_min_replicas(
                session,
                salad_deployment_id=warm_context.deployment_id,
                now=NOW + timedelta(seconds=1),
            )
            == 1
        )
        deployment = await session.get(SaladDeployment, warm_context.deployment_id)
        assert deployment is not None
        deployment.observed_replicas = 1
        deployment.ready_replicas = 1
        await session.commit()

    activated_at = NOW + timedelta(minutes=45)
    async with warm_context.database.sessions() as session:
        assert await activate_ready_experiment_warm_leases(
            session,
            actor="test",
            now=activated_at,
        ) == (started.lease_id,)
        await session.commit()
        active = await get_current_experiment_warm_lease_status(
            session,
            now=activated_at,
        )
        assert active is not None
        assert active.state == ExperimentWarmLeaseState.ACTIVE
        assert active.provider_version == 9
        assert active.expires_at == activated_at + timedelta(minutes=15)
        assert active.remaining_seconds == 15 * 60


@pytest.mark.asyncio
async def test_ensure_is_idempotent_and_explicit_extension_respects_hard_cap(
    warm_context: WarmContext,
) -> None:
    lease_id = await _start_refresh_and_activate(
        warm_context,
        activated_at=NOW + timedelta(minutes=30),
    )
    async with warm_context.database.sessions() as session:
        ensured = await ensure_experiment_warm_lease(
            session,
            actor="lab-post",
            now=NOW + timedelta(minutes=31),
        )
        assert ensured.lease_id == lease_id
        assert ensured.expires_at == NOW + timedelta(minutes=46)
        extended = await extend_experiment_warm_lease(
            session,
            lease_id=lease_id,
            actor="test",
            now=NOW + timedelta(minutes=31),
        )
        assert extended.expires_at == NOW + timedelta(minutes=61)
        await session.commit()

        with pytest.raises(ExperimentWarmLeaseConflictError, match="90-minute"):
            await extend_experiment_warm_lease(
                session,
                lease_id=lease_id,
                actor="test",
                extension_seconds=30 * 60,
                now=NOW + timedelta(minutes=31),
            )


@pytest.mark.asyncio
async def test_dispatchable_generation_does_not_prestart_worker_before_runtime_refresh(
    warm_context: WarmContext,
) -> None:
    async with warm_context.database.sessions() as session:
        job = await _add_generation_job(
            session,
            warm_context,
            suffix=100,
            state=GenerationState.QUEUED,
        )
        assert (
            await effective_worker_min_replicas(
                session,
                salad_deployment_id=warm_context.deployment_id,
                now=NOW,
            )
            == 0
        )

        job.state = GenerationState.SUCCEEDED
        await session.flush()
        assert (
            await effective_worker_min_replicas(
                session,
                salad_deployment_id=warm_context.deployment_id,
                now=NOW,
            )
            == 0
        )


@pytest.mark.asyncio
async def test_dispatchable_image_job_never_retains_a_retired_lane(
    warm_context: WarmContext,
) -> None:
    async with warm_context.database.sessions() as session:
        await _add_generation_job(
            session,
            warm_context,
            suffix=101,
            state=GenerationState.QUEUED,
        )
        retired_deployment = SaladDeployment(
            purpose=SaladDeploymentPurpose.VIDEO,
            version_no=2,
            config_sha256="d" * 64,
            provider_configuration={
                "container": {},
                "queue_autoscaler": {"polling_period": 15},
            },
            worker_image_digest="registry.example.test/retired-worker@sha256:" + "e" * 64,
            organization_name="organization",
            project_name="project",
            queue_name="retired-generation",
            provider_queue_id="retired-queue-id",
            container_group_name="retired-worker",
            provider_container_group_id="retired-group-id",
            state=SaladDeploymentState.ACTIVE,
            desired_state=DesiredDeploymentState.ACTIVE,
            is_current=True,
            min_replicas=0,
            max_replicas=1,
            desired_queue_length=1,
            max_hourly_cost_microusd=350_000,
            observed_replicas=0,
            ready_replicas=0,
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(retired_deployment)
        await session.flush()

        assert (
            await effective_worker_min_replicas(
                session,
                salad_deployment_id=retired_deployment.id,
                now=NOW,
            )
            == 0
        )


@pytest.mark.asyncio
async def test_retry_wait_never_prestarts_worker_before_runtime_refresh(
    warm_context: WarmContext,
) -> None:
    async with warm_context.database.sessions() as session:
        job = await _add_generation_job(
            session,
            warm_context,
            suffix=101,
            state=GenerationState.RETRY_WAIT,
            retry_at=NOW + timedelta(minutes=5),
        )
        assert (
            await effective_worker_min_replicas(
                session,
                salad_deployment_id=warm_context.deployment_id,
                now=NOW,
            )
            == 0
        )

        job.retry_at = None
        await session.flush()
        assert (
            await effective_worker_min_replicas(
                session,
                salad_deployment_id=warm_context.deployment_id,
                now=NOW,
            )
            == 0
        )

        job.retry_at = NOW - timedelta(seconds=1)
        await session.flush()
        assert (
            await effective_worker_min_replicas(
                session,
                salad_deployment_id=warm_context.deployment_id,
                now=NOW,
            )
            == 0
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    [
        GenerationState.CANCEL_REQUESTED,
        GenerationState.COLLECTING,
        GenerationState.VERIFYING,
        GenerationState.RUNNING,
        GenerationState.SUCCEEDED,
        GenerationState.FAILED,
        GenerationState.CANCELLED,
    ],
)
async def test_non_dispatchable_or_post_gpu_job_does_not_hold_worker(
    warm_context: WarmContext,
    state: GenerationState,
) -> None:
    async with warm_context.database.sessions() as session:
        await _add_generation_job(
            session,
            warm_context,
            suffix=102,
            state=state,
        )
        assert (
            await effective_worker_min_replicas(
                session,
                salad_deployment_id=warm_context.deployment_id,
                now=NOW,
            )
            == 0
        )


@pytest.mark.asyncio
async def test_other_provider_job_does_not_hold_salad_worker(
    warm_context: WarmContext,
) -> None:
    async with warm_context.database.sessions() as session:
        await _add_generation_job(
            session,
            warm_context,
            suffix=103,
            state=GenerationState.QUEUED,
            provider="runpod",
        )
        assert (
            await effective_worker_min_replicas(
                session,
                salad_deployment_id=warm_context.deployment_id,
                now=NOW,
            )
            == 0
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", [ReleasePhase.PAUSED, ReleasePhase.CANCELLED])
async def test_inactive_release_does_not_hold_worker_for_queued_work(
    warm_context: WarmContext,
    phase: ReleasePhase,
) -> None:
    async with warm_context.database.sessions() as session:
        await _add_generation_job(
            session,
            warm_context,
            suffix=104,
            state=GenerationState.QUEUED,
        )
        release = await session.get(Release, warm_context.release_id)
        assert release is not None
        release.phase = phase
        await session.flush()
        assert (
            await effective_worker_min_replicas(
                session,
                salad_deployment_id=warm_context.deployment_id,
                now=NOW,
            )
            == 0
        )


@pytest.mark.asyncio
async def test_durable_stop_marker_wins_over_stale_dispatchable_phase(
    warm_context: WarmContext,
) -> None:
    async with warm_context.database.sessions() as session:
        await _add_generation_job(
            session,
            warm_context,
            suffix=105,
            state=GenerationState.QUEUED,
        )
        session.add(
            AuditEvent(
                actor="test",
                action="release.generation_stop_requested",
                resource_type="release",
                resource_id=warm_context.release_id,
                correlation_id=f"generation-stop:{warm_context.release_id}",
                detail={},
                occurred_at=NOW,
            )
        )
        await session.flush()
        assert (
            await effective_worker_min_replicas(
                session,
                salad_deployment_id=warm_context.deployment_id,
                now=NOW,
            )
            == 0
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("health", [ResourceHealth.WARNING, ResourceHealth.BLOCKED])
async def test_unhealthy_release_does_not_hold_worker_for_queued_work(
    warm_context: WarmContext,
    health: ResourceHealth,
) -> None:
    async with warm_context.database.sessions() as session:
        await _add_generation_job(
            session,
            warm_context,
            suffix=106,
            state=GenerationState.QUEUED,
        )
        release = await session.get(Release, warm_context.release_id)
        assert release is not None
        release.health = health
        await session.flush()
        assert (
            await effective_worker_min_replicas(
                session,
                salad_deployment_id=warm_context.deployment_id,
                now=NOW,
            )
            == 0
        )


@pytest.mark.asyncio
async def test_noncurrent_release_version_does_not_hold_worker(
    warm_context: WarmContext,
) -> None:
    async with warm_context.database.sessions() as session:
        await _add_generation_job(
            session,
            warm_context,
            suffix=107,
            state=GenerationState.QUEUED,
        )
        release = await session.get(Release, warm_context.release_id)
        assert release is not None
        release.current_version_no = 2
        await session.flush()
        assert (
            await effective_worker_min_replicas(
                session,
                salad_deployment_id=warm_context.deployment_id,
                now=NOW,
            )
            == 0
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "attempt_state",
    [
        GenerationAttemptState.SUBMITTED,
        GenerationAttemptState.RUNNING,
        GenerationAttemptState.UNKNOWN,
    ],
)
async def test_active_gpu_attempt_holds_only_its_salad_deployment(
    warm_context: WarmContext,
    attempt_state: GenerationAttemptState,
) -> None:
    async with warm_context.database.sessions() as session:
        _, attempt = await _prepare_attempt(
            session,
            warm_context,
            suffix=108,
            state=attempt_state,
        )
        assert (
            await effective_worker_min_replicas(
                session,
                salad_deployment_id=warm_context.deployment_id,
                now=NOW,
            )
            == 1
        )

        attempt.state = GenerationAttemptState.SUCCEEDED
        attempt.completed_at = NOW
        await session.flush()
        assert (
            await effective_worker_min_replicas(
                session,
                salad_deployment_id=warm_context.deployment_id,
                now=NOW,
            )
            == 0
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "attempt_state",
    [
        GenerationAttemptState.CREATED,
        GenerationAttemptState.SUBMITTING,
        GenerationAttemptState.CANCEL_REQUESTED,
    ],
)
async def test_nonexecuting_attempt_does_not_hold_worker(
    warm_context: WarmContext,
    attempt_state: GenerationAttemptState,
) -> None:
    async with warm_context.database.sessions() as session:
        await _prepare_attempt(
            session,
            warm_context,
            suffix=109,
            state=attempt_state,
        )
        assert (
            await effective_worker_min_replicas(
                session,
                salad_deployment_id=warm_context.deployment_id,
                now=NOW,
            )
            == 0
        )


@pytest.mark.asyncio
async def test_claimed_submit_event_holds_worker_during_cold_admission(
    warm_context: WarmContext,
) -> None:
    async with warm_context.database.sessions() as session:
        _, attempt = await _prepare_attempt(
            session,
            warm_context,
            suffix=112,
            state=GenerationAttemptState.CREATED,
        )
        event = await session.scalar(
            select(OutboxEvent).where(
                OutboxEvent.topic == SALAD_JOB_SUBMIT_TOPIC,
                OutboxEvent.aggregate_id == attempt.id,
            )
        )
        assert event is not None
        event.status = OutboxStatus.PROCESSING
        event.attempts = 1
        event.lease_owner = "controller-submit"
        event.lease_expires_at = NOW + timedelta(minutes=5)
        await session.flush()

        assert (
            await effective_worker_min_replicas(
                session,
                salad_deployment_id=warm_context.deployment_id,
                now=NOW,
            )
            == 1
        )

        event.lease_expires_at = NOW
        await session.flush()
        assert (
            await effective_worker_min_replicas(
                session,
                salad_deployment_id=warm_context.deployment_id,
                now=NOW,
            )
            == 0
        )


@pytest.mark.asyncio
async def test_other_provider_attempt_does_not_hold_salad_worker(
    warm_context: WarmContext,
) -> None:
    async with warm_context.database.sessions() as session:
        _, attempt = await _prepare_attempt(
            session,
            warm_context,
            suffix=110,
            state=GenerationAttemptState.RUNNING,
        )
        attempt.provider = "runpod"
        await session.flush()
        assert (
            await effective_worker_min_replicas(
                session,
                salad_deployment_id=warm_context.deployment_id,
                now=NOW,
            )
            == 0
        )


@pytest.mark.asyncio
async def test_running_attempt_for_cancelled_release_does_not_hold_worker(
    warm_context: WarmContext,
) -> None:
    async with warm_context.database.sessions() as session:
        await _prepare_attempt(
            session,
            warm_context,
            suffix=111,
            state=GenerationAttemptState.RUNNING,
        )
        release = await session.get(Release, warm_context.release_id)
        assert release is not None
        release.phase = ReleasePhase.CANCELLED
        await session.flush()
        assert (
            await effective_worker_min_replicas(
                session,
                salad_deployment_id=warm_context.deployment_id,
                now=NOW,
            )
            == 0
        )


@pytest.mark.asyncio
async def test_terminal_job_with_stale_running_attempt_does_not_hold_worker(
    warm_context: WarmContext,
) -> None:
    async with warm_context.database.sessions() as session:
        job, _ = await _prepare_attempt(
            session,
            warm_context,
            suffix=113,
            state=GenerationAttemptState.RUNNING,
        )
        job.state = GenerationState.SUCCEEDED
        await session.flush()
        assert (
            await effective_worker_min_replicas(
                session,
                salad_deployment_id=warm_context.deployment_id,
                now=NOW,
            )
            == 0
        )


@pytest.mark.asyncio
async def test_attempt_on_another_salad_deployment_does_not_hold_this_worker(
    warm_context: WarmContext,
) -> None:
    async with warm_context.database.sessions() as session:
        original = await session.get(SaladDeployment, warm_context.deployment_id)
        assert original is not None
        original.is_current = False
        await session.flush()
        other = SaladDeployment(
            version_no=2,
            config_sha256="d" * 64,
            provider_configuration={"container": {}, "queue_autoscaler": {}},
            worker_image_digest=IMAGE_DIGEST,
            organization_name="organization",
            project_name="project",
            queue_name="generation-v2",
            provider_queue_id="queue-id-v2",
            container_group_name="worker-v2",
            provider_container_group_id="group-id-v2",
            state=SaladDeploymentState.ACTIVE,
            desired_state=DesiredDeploymentState.ACTIVE,
            is_current=True,
            min_replicas=0,
            max_replicas=1,
            desired_queue_length=1,
            max_hourly_cost_microusd=360_000,
            observed_replicas=0,
            ready_replicas=0,
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(other)
        await session.flush()
        await _prepare_attempt(
            session,
            warm_context,
            suffix=112,
            state=GenerationAttemptState.RUNNING,
            deployment_id=other.id,
        )
        assert (
            await effective_worker_min_replicas(
                session,
                salad_deployment_id=warm_context.deployment_id,
                now=NOW,
            )
            == 0
        )


@pytest.mark.asyncio
async def test_busy_attempt_and_unobserved_completion_hold_minimum_until_touch(
    warm_context: WarmContext,
) -> None:
    activated_at = NOW + timedelta(minutes=1)
    lease_id = await _start_refresh_and_activate(
        warm_context,
        activated_at=activated_at,
    )
    async with warm_context.database.sessions() as session:
        job = GenerationJob(
            release_version_id=warm_context.release_version_id,
            logical_key="d" * 64,
            parameters={"seed": 1},
            parameters_sha256=canonical_sha256({"seed": 1}),
            provider="salad",
            state=GenerationState.QUEUED,
            expected_output_count=1,
        )
        session.add(job)
        await session.flush()
        prepared = await prepare_generation_attempt(
            session,
            generation_job_id=job.id,
            salad_deployment_id=warm_context.deployment_id,
            idempotency_key="warm-busy-attempt",
            now=activated_at + timedelta(seconds=1),
        )
        attempt = await session.get(GenerationAttempt, prepared.generation_attempt_id)
        assert attempt is not None
        attempt.state = GenerationAttemptState.RUNNING
        attempt.provider_external_id = "provider-job"
        attempt.started_at = activated_at + timedelta(seconds=1)
        await session.commit()

    after_idle_expiry = activated_at + timedelta(minutes=16)
    async with warm_context.database.sessions() as session:
        assert (
            await expire_experiment_warm_leases(
                session,
                now=after_idle_expiry,
            )
            == ()
        )
        assert (
            await effective_experiment_min_replicas(
                session,
                salad_deployment_id=warm_context.deployment_id,
                now=after_idle_expiry,
            )
            == 1
        )
        attempt = await session.get(GenerationAttempt, prepared.generation_attempt_id)
        assert attempt is not None
        attempt.state = GenerationAttemptState.SUCCEEDED
        attempt.completed_at = after_idle_expiry
        await session.commit()

    before_completion_touch = after_idle_expiry + timedelta(seconds=1)
    async with warm_context.database.sessions() as session:
        # The terminal transition cannot race a deployment reconcile to min=0.
        assert (
            await effective_experiment_min_replicas(
                session,
                salad_deployment_id=warm_context.deployment_id,
                now=before_completion_touch,
            )
            == 1
        )
        assert (
            await expire_experiment_warm_leases(
                session,
                now=before_completion_touch,
            )
            == ()
        )
        assert await touch_completed_experiment_warm_leases(
            session,
            actor="completion",
            now=before_completion_touch,
        ) == (lease_id,)
        await session.commit()
        current = await get_current_experiment_warm_lease_status(
            session,
            now=before_completion_touch,
        )
        assert current is not None
        assert current.expires_at == before_completion_touch + timedelta(minutes=15)


@pytest.mark.asyncio
async def test_hard_expiry_wins_even_with_busy_attempt(
    warm_context: WarmContext,
) -> None:
    lease_id = await _start_refresh_and_activate(
        warm_context,
        activated_at=NOW + timedelta(minutes=1),
    )
    async with warm_context.database.sessions() as session:
        job = GenerationJob(
            release_version_id=warm_context.release_version_id,
            logical_key="e" * 64,
            parameters={"seed": 2},
            parameters_sha256=canonical_sha256({"seed": 2}),
            provider="salad",
            state=GenerationState.QUEUED,
            expected_output_count=1,
        )
        session.add(job)
        await session.flush()
        prepared = await prepare_generation_attempt(
            session,
            generation_job_id=job.id,
            salad_deployment_id=warm_context.deployment_id,
            idempotency_key="warm-hard-expiry",
            now=NOW + timedelta(minutes=2),
        )
        attempt = await session.get(GenerationAttempt, prepared.generation_attempt_id)
        assert attempt is not None
        attempt.state = GenerationAttemptState.RUNNING
        attempt.provider_external_id = "hard-expiry-provider-job"
        await session.commit()

    hard_expired_at = NOW + timedelta(minutes=90, seconds=1)
    async with warm_context.database.sessions() as session:
        assert await expire_experiment_warm_leases(
            session,
            now=hard_expired_at,
        ) == (lease_id,)
        assert (
            await effective_experiment_min_replicas(
                session,
                salad_deployment_id=warm_context.deployment_id,
                now=hard_expired_at,
            )
            == 0
        )


@pytest.mark.asyncio
async def test_start_fails_closed_when_budget_cannot_cover_absolute_cap(
    warm_context: WarmContext,
) -> None:
    async with warm_context.database.sessions() as session:
        guard = await ensure_budget_guard(
            session,
            provider="salad",
            daily_limit_usd=Decimal("0.01"),
            monthly_limit_usd=Decimal("0.01"),
            now=NOW,
        )
        assert guard.daily_limit_microusd == 10_000
        with pytest.raises(ExperimentWarmLeaseBudgetError, match="budget"):
            await start_experiment_warm_lease(
                session,
                salad_deployment_id=warm_context.deployment_id,
                actor="test",
                now=NOW,
            )


@pytest.mark.asyncio
async def test_metered_usage_consumes_warm_envelope_without_double_counting(
    warm_context: WarmContext,
) -> None:
    async with warm_context.database.sessions() as session:
        await ensure_budget_guard(
            session,
            provider="salad",
            daily_limit_usd=Decimal("0.54"),
            monthly_limit_usd=Decimal("0.54"),
            now=NOW,
        )
        await start_experiment_warm_lease(
            session,
            salad_deployment_id=warm_context.deployment_id,
            actor="test",
            now=NOW,
        )
        await session.flush()
        result = await record_spend_entry(
            session,
            provider="salad",
            dedupe_key="warm-first-metered-interval",
            entry_type=SpendEntryType.USAGE,
            amount_microusd=10_000,
            effective_at=NOW + timedelta(minutes=1),
            salad_deployment_id=warm_context.deployment_id,
            now=NOW + timedelta(minutes=1),
        )
        assert result.snapshot.state == BudgetState.OPEN
        assert result.snapshot.daily_spend_microusd == 10_000
        assert result.snapshot.active_warm_leases_microusd == 530_000
        assert result.snapshot.daily_committed_microusd == 540_000
        deployment = await session.get(SaladDeployment, warm_context.deployment_id)
        assert deployment is not None
        assert deployment.desired_state == DesiredDeploymentState.ACTIVE


@pytest.mark.asyncio
async def test_budget_kill_switch_terminates_live_warm_lease(
    warm_context: WarmContext,
) -> None:
    async with warm_context.database.sessions() as session:
        await ensure_budget_guard(
            session,
            provider="salad",
            daily_limit_usd=Decimal("0.54"),
            monthly_limit_usd=Decimal("0.54"),
            now=NOW,
        )
        started = await start_experiment_warm_lease(
            session,
            salad_deployment_id=warm_context.deployment_id,
            actor="test",
            now=NOW,
        )
        result = await record_spend_entry(
            session,
            provider="salad",
            dedupe_key="warm-over-envelope",
            entry_type=SpendEntryType.USAGE,
            amount_microusd=550_000,
            effective_at=NOW + timedelta(minutes=1),
            salad_deployment_id=warm_context.deployment_id,
            now=NOW + timedelta(minutes=1),
        )
        assert result.snapshot.state == BudgetState.BLOCKED
        lease = await session.get(ExperimentWarmLease, started.lease_id)
        assert lease is not None
        assert lease.state == ExperimentWarmLeaseState.FAILED
        assert lease.ended_at is not None
        deployment = await session.get(SaladDeployment, warm_context.deployment_id)
        assert deployment is not None
        assert deployment.desired_state == DesiredDeploymentState.STOPPED


@pytest.mark.asyncio
async def test_dynamic_autoscaler_override_never_raises_replica_cap(
    warm_context: WarmContext,
) -> None:
    async with warm_context.database.sessions() as session:
        deployment = await session.get(SaladDeployment, warm_context.deployment_id)
        assert deployment is not None
        desired = _desired_queue_autoscaler(deployment, min_replicas=1)
        assert desired["min_replicas"] == 1
        assert desired["max_replicas"] == 1
        assert desired["desired_queue_length"] == 1
        assert await session.scalar(select(SaladDeployment.max_replicas)) == 1
