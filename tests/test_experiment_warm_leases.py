from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import select

from gen_automation.db.models import (
    ExperimentWarmLease,
    GenerationAttempt,
    GenerationJob,
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
from gen_automation.services.salad import prepare_generation_attempt
from gen_automation.services.salad_deployments import _desired_queue_autoscaler

NOW = datetime(2026, 8, 5, 9, 0, tzinfo=UTC)
IMAGE_DIGEST = "registry.example.test/worker@sha256:" + "a" * 64


@dataclass(frozen=True, slots=True)
class WarmContext:
    database: Database
    deployment_id: UUID
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
            release_version_id=version.id,
        )
    try:
        yield context
    finally:
        await database.dispose()


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
