from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from gen_automation.db.models import (
    AuditEvent,
    GenerationAttempt,
    GenerationJob,
    Project,
    ProviderBudgetGuard,
    ProviderSpendEntry,
    Release,
    ReleaseVersion,
    SaladDeployment,
)
from gen_automation.db.session import Database
from gen_automation.domain.enums import (
    BudgetState,
    DesiredDeploymentState,
    GenerationAttemptState,
    SpendEntryType,
)
from gen_automation.services.budgets import (
    BudgetAttemptNotFoundError,
    BudgetConfigurationError,
    BudgetConflictError,
    BudgetGuardNotFoundError,
    ensure_budget_guard,
    record_spend_entry,
    reevaluate_budget_guard,
    release_attempt_reservation,
    reserve_attempt_budget,
    usd_to_microusd,
)

NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)


@dataclass(frozen=True)
class BudgetContext:
    database: Database
    first_attempt_id: UUID
    second_attempt_id: UUID
    deployment_id: UUID


@pytest.fixture
async def budget_context(tmp_path: Path) -> AsyncIterator[BudgetContext]:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'budgets.db').as_posix()}")
    await database.create_schema()
    async with database.sessions() as session:
        project = Project(slug="budget-tests", name="Budget Tests")
        session.add(project)
        await session.flush()
        release = Release(
            project_id=project.id,
            slug="release",
            title="Release",
            desired_accepted_count=1,
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
        deployment = SaladDeployment(
            version_no=1,
            config_sha256="b" * 64,
            worker_image_digest="registry.example/worker@sha256:" + "c" * 64,
            organization_name="organization",
            project_name="project",
            queue_name="queue",
            container_group_name="workers",
            is_current=True,
            max_hourly_cost_microusd=2_000_000,
        )
        session.add(deployment)
        job = GenerationJob(
            release_version_id=version.id,
            logical_key="d" * 64,
            parameters={"seed": 1},
            parameters_sha256="e" * 64,
            provider="salad",
            expected_output_count=1,
        )
        session.add(job)
        await session.flush()
        first_attempt = GenerationAttempt(
            job_id=job.id,
            salad_deployment_id=deployment.id,
            attempt_no=1,
            provider="salad",
            submission_key="f" * 64,
            request_sha256="1" * 64,
            worker_image_digest=deployment.worker_image_digest,
            request_metadata={"job_id": str(job.id)},
            created_at=NOW,
        )
        second_attempt = GenerationAttempt(
            job_id=job.id,
            salad_deployment_id=deployment.id,
            attempt_no=2,
            provider="salad",
            submission_key="2" * 64,
            request_sha256="3" * 64,
            worker_image_digest=deployment.worker_image_digest,
            request_metadata={"job_id": str(job.id)},
            created_at=NOW,
        )
        session.add_all([first_attempt, second_attempt])
        await session.commit()
        context = BudgetContext(
            database=database,
            first_attempt_id=first_attempt.id,
            second_attempt_id=second_attempt.id,
            deployment_id=deployment.id,
        )
    try:
        yield context
    finally:
        await database.dispose()


def test_usd_conversion_is_exact_and_never_rounds() -> None:
    assert usd_to_microusd(Decimal("25")) == 25_000_000
    assert usd_to_microusd(Decimal("0.000001")) == 1
    assert usd_to_microusd(Decimal("-1.25")) == -1_250_000

    for invalid in (
        Decimal("0.0000001"),
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal("9223372036854.775808"),
    ):
        with pytest.raises(BudgetConfigurationError):
            usd_to_microusd(invalid)


async def test_guard_is_singleton_and_limit_updates_are_reevaluated(
    budget_context: BudgetContext,
) -> None:
    async with budget_context.database.sessions() as session:
        created = await ensure_budget_guard(
            session,
            provider="SALAD",
            daily_limit_usd=Decimal("10"),
            monthly_limit_usd=Decimal("20"),
            now=NOW,
        )
        await session.commit()

        assert created.provider == "salad"
        assert created.daily_limit_microusd == 10_000_000
        assert created.state == BudgetState.OPEN

    async with budget_context.database.sessions() as session:
        updated = await ensure_budget_guard(
            session,
            provider="salad",
            daily_limit_usd=Decimal("8"),
            monthly_limit_usd=Decimal("18"),
            now=NOW,
        )
        count = await session.scalar(select(func.count()).select_from(ProviderBudgetGuard))
        await session.commit()

        assert count == 1
        assert updated.daily_limit_microusd == 8_000_000

        with pytest.raises(BudgetConfigurationError):
            await ensure_budget_guard(
                session,
                provider="salad",
                daily_limit_usd=Decimal("20"),
                monthly_limit_usd=Decimal("10"),
                now=NOW,
            )


async def test_reservations_are_atomic_idempotent_and_fail_closed(
    budget_context: BudgetContext,
) -> None:
    async with budget_context.database.sessions() as session:
        await ensure_budget_guard(
            session,
            provider="salad",
            daily_limit_usd=Decimal("10"),
            monthly_limit_usd=Decimal("20"),
            now=NOW,
        )
        await record_spend_entry(
            session,
            provider="salad",
            dedupe_key="meter:initial",
            entry_type=SpendEntryType.USAGE,
            amount_microusd=2_500_000,
            effective_at=NOW,
            now=NOW,
        )
        accepted = await reserve_attempt_budget(
            session,
            provider="salad",
            attempt_id=budget_context.first_attempt_id,
            amount_microusd=3_000_000,
            now=NOW,
        )
        replay = await reserve_attempt_budget(
            session,
            provider="salad",
            attempt_id=budget_context.first_attempt_id,
            amount_microusd=3_000_000,
            now=NOW,
        )
        rejected = await reserve_attempt_budget(
            session,
            provider="salad",
            attempt_id=budget_context.second_attempt_id,
            amount_microusd=5_000_000,
            now=NOW,
        )
        await session.commit()

        assert accepted.accepted is True
        assert accepted.replayed is False
        assert accepted.snapshot.daily_committed_microusd == 5_500_000
        assert replay.accepted is True
        assert replay.replayed is True
        assert rejected.accepted is False
        assert rejected.reason == "daily_limit_exceeded"
        assert rejected.snapshot.state == BudgetState.BLOCKED

    async with budget_context.database.sessions() as session:
        first = await session.get(GenerationAttempt, budget_context.first_attempt_id)
        second = await session.get(GenerationAttempt, budget_context.second_attempt_id)
        guard = await session.scalar(
            select(ProviderBudgetGuard).where(ProviderBudgetGuard.provider == "salad")
        )
        assert first is not None
        assert second is not None
        assert guard is not None
        assert first.state == GenerationAttemptState.SUBMITTING
        assert first.cost_reservation_microusd == 3_000_000
        assert first.submit_started_at is not None
        assert second.state == GenerationAttemptState.CREATED
        assert second.cost_reservation_microusd == 0
        assert guard.state == BudgetState.BLOCKED


async def test_monthly_commitment_blocks_even_when_daily_budget_has_room(
    budget_context: BudgetContext,
) -> None:
    async with budget_context.database.sessions() as session:
        await ensure_budget_guard(
            session,
            provider="salad",
            daily_limit_usd=Decimal("5"),
            monthly_limit_usd=Decimal("6"),
            now=NOW,
        )
        await record_spend_entry(
            session,
            provider="salad",
            dedupe_key="meter:month",
            entry_type=SpendEntryType.USAGE,
            amount_microusd=5_000_000,
            effective_at=datetime(2026, 7, 1, 12, 0, tzinfo=UTC),
            now=NOW,
        )
        decision = await reserve_attempt_budget(
            session,
            provider="salad",
            attempt_id=budget_context.first_attempt_id,
            amount_microusd=2_000_000,
            now=NOW,
        )
        await session.commit()

        assert decision.accepted is False
        assert decision.reason == "monthly_limit_exceeded"
        assert decision.snapshot.daily_spend_microusd == 0
        assert decision.snapshot.monthly_spend_microusd == 5_000_000


async def test_spend_ledger_is_idempotent_and_adjustment_can_reopen_guard(
    budget_context: BudgetContext,
) -> None:
    async with budget_context.database.sessions() as session:
        superseded = SaladDeployment(
            version_no=2,
            config_sha256="4" * 64,
            worker_image_digest="registry.example/worker@sha256:" + "5" * 64,
            organization_name="organization",
            project_name="project",
            queue_name="old-queue",
            container_group_name="old-workers",
            is_current=False,
            max_hourly_cost_microusd=2_000_000,
        )
        session.add(superseded)
        await ensure_budget_guard(
            session,
            provider="salad",
            daily_limit_usd=Decimal("10"),
            monthly_limit_usd=Decimal("20"),
            now=NOW,
        )
        first = await record_spend_entry(
            session,
            provider="salad",
            dedupe_key="meter:one",
            entry_type=SpendEntryType.USAGE,
            amount_microusd=8_000_000,
            effective_at=NOW,
            salad_deployment_id=budget_context.deployment_id,
            now=NOW,
        )
        replay = await record_spend_entry(
            session,
            provider="salad",
            dedupe_key="meter:one",
            entry_type=SpendEntryType.USAGE,
            amount_microusd=8_000_000,
            effective_at=NOW,
            salad_deployment_id=budget_context.deployment_id,
            now=NOW,
        )
        overage = await record_spend_entry(
            session,
            provider="salad",
            dedupe_key="meter:two",
            entry_type=SpendEntryType.USAGE,
            amount_microusd=3_000_000,
            effective_at=NOW,
            now=NOW,
        )

        with pytest.raises(BudgetConflictError):
            await record_spend_entry(
                session,
                provider="salad",
                dedupe_key="meter:one",
                entry_type=SpendEntryType.USAGE,
                amount_microusd=7_000_000,
                effective_at=NOW,
                salad_deployment_id=budget_context.deployment_id,
                now=NOW,
            )

        adjustment = await record_spend_entry(
            session,
            provider="salad",
            dedupe_key="adjustment:one",
            entry_type=SpendEntryType.ADJUSTMENT,
            amount_microusd=-2_000_000,
            effective_at=NOW,
            now=NOW,
        )
        entry_count = await session.scalar(select(func.count()).select_from(ProviderSpendEntry))
        deployment = await session.get(SaladDeployment, budget_context.deployment_id)
        superseded = await session.get(SaladDeployment, superseded.id)
        await session.commit()

        assert first.replayed is False
        assert replay.replayed is True
        assert replay.entry_id == first.entry_id
        assert overage.snapshot.state == BudgetState.BLOCKED
        assert deployment is not None
        assert deployment.desired_state == DesiredDeploymentState.STOPPED
        assert superseded is not None
        assert superseded.desired_state == DesiredDeploymentState.STOPPED
        assert adjustment.snapshot.state == BudgetState.OPEN
        assert adjustment.snapshot.daily_spend_microusd == 9_000_000
        assert entry_count == 3


async def test_only_definitive_attempts_release_reservations_and_reopen(
    budget_context: BudgetContext,
) -> None:
    async with budget_context.database.sessions() as session:
        await ensure_budget_guard(
            session,
            provider="salad",
            daily_limit_usd=Decimal("4"),
            monthly_limit_usd=Decimal("8"),
            now=NOW,
        )
        await reserve_attempt_budget(
            session,
            provider="salad",
            attempt_id=budget_context.first_attempt_id,
            amount_microusd=3_000_000,
            now=NOW,
        )
        blocked = await reserve_attempt_budget(
            session,
            provider="salad",
            attempt_id=budget_context.second_attempt_id,
            amount_microusd=2_000_000,
            now=NOW,
        )
        assert blocked.snapshot.state == BudgetState.BLOCKED

        with pytest.raises(BudgetConflictError):
            await release_attempt_reservation(
                session,
                provider="salad",
                attempt_id=budget_context.first_attempt_id,
                now=NOW,
            )

        attempt = await session.get(GenerationAttempt, budget_context.first_attempt_id)
        assert attempt is not None
        attempt.state = GenerationAttemptState.FAILED
        attempt.completed_at = NOW
        released = await release_attempt_reservation(
            session,
            provider="salad",
            attempt_id=budget_context.first_attempt_id,
            now=NOW,
        )
        replay = await release_attempt_reservation(
            session,
            provider="salad",
            attempt_id=budget_context.first_attempt_id,
            now=NOW,
        )
        await session.commit()

        assert released.released is True
        assert released.snapshot.active_reservations_microusd == 0
        assert released.snapshot.state == BudgetState.OPEN
        assert replay.released is False
        assert replay.replayed is True


async def test_terminal_release_preserves_authoritative_runtime_spend_exactly_once(
    budget_context: BudgetContext,
) -> None:
    async with budget_context.database.sessions() as session:
        await ensure_budget_guard(
            session,
            provider="salad",
            daily_limit_usd=Decimal("10"),
            monthly_limit_usd=Decimal("20"),
            now=NOW,
        )
        await reserve_attempt_budget(
            session,
            provider="salad",
            attempt_id=budget_context.first_attempt_id,
            amount_microusd=2_000_000,
            now=NOW,
        )
        runtime = await record_spend_entry(
            session,
            provider="salad",
            dedupe_key="salad-runtime:authoritative",
            entry_type=SpendEntryType.USAGE,
            amount_microusd=250_000,
            effective_at=NOW,
            salad_deployment_id=budget_context.deployment_id,
            now=NOW,
        )
        runtime_replay = await record_spend_entry(
            session,
            provider="salad",
            dedupe_key="salad-runtime:authoritative",
            entry_type=SpendEntryType.USAGE,
            amount_microusd=250_000,
            effective_at=NOW,
            salad_deployment_id=budget_context.deployment_id,
            now=NOW,
        )
        attempt = await session.get(GenerationAttempt, budget_context.first_attempt_id)
        assert attempt is not None
        attempt.provider_external_id = "provider-job-1"
        attempt.state = GenerationAttemptState.SUCCEEDED
        attempt.completed_at = NOW

        released = await release_attempt_reservation(
            session,
            provider="salad",
            attempt_id=attempt.id,
            now=NOW,
        )
        replay = await release_attempt_reservation(
            session,
            provider="salad",
            attempt_id=attempt.id,
            now=NOW,
        )
        entries = list((await session.scalars(select(ProviderSpendEntry))).all())
        attempt_entries = [entry for entry in entries if entry.generation_attempt_id == attempt.id]

    assert runtime.replayed is False
    assert runtime_replay.replayed is True
    assert released.snapshot.active_reservations_microusd == 0
    assert released.snapshot.daily_spend_microusd == 250_000
    assert released.snapshot.daily_committed_microusd == 250_000
    assert replay.replayed is True
    assert len(entries) == 1
    assert attempt_entries == []


async def test_reopen_requires_a_successful_reevaluation(
    budget_context: BudgetContext,
) -> None:
    async with budget_context.database.sessions() as session:
        await ensure_budget_guard(
            session,
            provider="salad",
            daily_limit_usd=Decimal("1"),
            monthly_limit_usd=Decimal("2"),
            now=NOW,
        )
        await record_spend_entry(
            session,
            provider="salad",
            dedupe_key="meter:block",
            entry_type=SpendEntryType.USAGE,
            amount_microusd=1_500_000,
            effective_at=NOW,
            now=NOW,
        )
        guard = await session.scalar(
            select(ProviderBudgetGuard).where(ProviderBudgetGuard.provider == "salad")
        )
        assert guard is not None
        assert guard.state == BudgetState.BLOCKED

        guard.daily_limit_microusd = 2_000_000
        await session.flush()
        assert guard.state == BudgetState.BLOCKED

        snapshot = await reevaluate_budget_guard(session, provider="salad", now=NOW)
        audit_details = [
            event.detail
            for event in (
                await session.scalars(
                    select(AuditEvent).where(
                        AuditEvent.resource_id == guard.id,
                    )
                )
            ).all()
        ]
        await session.commit()

        assert snapshot.state == BudgetState.OPEN
        assert all("prompt" not in str(detail).lower() for detail in audit_details)
        assert all("secret" not in str(detail).lower() for detail in audit_details)


async def test_invalid_operations_fail_closed(budget_context: BudgetContext) -> None:
    async with budget_context.database.sessions() as session:
        with pytest.raises(BudgetConfigurationError):
            usd_to_microusd(cast(Decimal, "1"))
        with pytest.raises(BudgetGuardNotFoundError):
            await reevaluate_budget_guard(session, provider="salad", now=NOW)
        with pytest.raises(BudgetConfigurationError):
            await ensure_budget_guard(
                session,
                provider="not a provider",
                daily_limit_usd=Decimal("1"),
                monthly_limit_usd=Decimal("2"),
                now=NOW,
            )
        with pytest.raises(BudgetConfigurationError):
            await ensure_budget_guard(
                session,
                provider="salad",
                daily_limit_usd=Decimal("0"),
                monthly_limit_usd=Decimal("2"),
                now=NOW,
            )
        with pytest.raises(BudgetConfigurationError):
            await ensure_budget_guard(
                session,
                provider="salad",
                daily_limit_usd=Decimal("1"),
                monthly_limit_usd=Decimal("2"),
                now=datetime(2026, 7, 28),
            )

        await ensure_budget_guard(
            session,
            provider="salad",
            daily_limit_usd=Decimal("5"),
            monthly_limit_usd=Decimal("10"),
            now=NOW,
        )
        await ensure_budget_guard(
            session,
            provider="runpod",
            daily_limit_usd=Decimal("5"),
            monthly_limit_usd=Decimal("10"),
            now=NOW,
        )

        for invalid_amount in (0, -1, cast(int, True)):
            with pytest.raises(BudgetConfigurationError):
                await reserve_attempt_budget(
                    session,
                    provider="salad",
                    attempt_id=budget_context.first_attempt_id,
                    amount_microusd=invalid_amount,
                    now=NOW,
                )

        with pytest.raises(BudgetAttemptNotFoundError):
            await reserve_attempt_budget(
                session,
                provider="salad",
                attempt_id=uuid4(),
                amount_microusd=1,
                now=NOW,
            )
        with pytest.raises(BudgetConflictError):
            await reserve_attempt_budget(
                session,
                provider="runpod",
                attempt_id=budget_context.first_attempt_id,
                amount_microusd=1,
                now=NOW,
            )

        await reserve_attempt_budget(
            session,
            provider="salad",
            attempt_id=budget_context.first_attempt_id,
            amount_microusd=1_000_000,
            now=NOW,
        )
        with pytest.raises(BudgetConflictError):
            await reserve_attempt_budget(
                session,
                provider="salad",
                attempt_id=budget_context.first_attempt_id,
                amount_microusd=2_000_000,
                now=NOW,
            )

        with pytest.raises(BudgetConfigurationError):
            await record_spend_entry(
                session,
                provider="salad",
                dedupe_key="contains a prompt",
                entry_type=SpendEntryType.USAGE,
                amount_microusd=1,
                effective_at=NOW,
                now=NOW,
            )
        invalid_spend_values = (
            (cast(SpendEntryType, "usage"), 1),
            (SpendEntryType.USAGE, cast(int, True)),
            (SpendEntryType.USAGE, 0),
            (SpendEntryType.USAGE, -1),
        )
        for entry_type, amount in invalid_spend_values:
            with pytest.raises(BudgetConfigurationError):
                await record_spend_entry(
                    session,
                    provider="salad",
                    dedupe_key="meter:invalid",
                    entry_type=entry_type,
                    amount_microusd=amount,
                    effective_at=NOW,
                    now=NOW,
                )
        with pytest.raises(BudgetConfigurationError):
            await record_spend_entry(
                session,
                provider="salad",
                dedupe_key="meter:naive-time",
                entry_type=SpendEntryType.USAGE,
                amount_microusd=1,
                effective_at=datetime(2026, 7, 28),
                now=NOW,
            )
        with pytest.raises(BudgetAttemptNotFoundError):
            await record_spend_entry(
                session,
                provider="salad",
                dedupe_key="meter:missing-attempt",
                entry_type=SpendEntryType.USAGE,
                amount_microusd=1,
                effective_at=NOW,
                generation_attempt_id=uuid4(),
                now=NOW,
            )
        with pytest.raises(BudgetConflictError):
            await record_spend_entry(
                session,
                provider="runpod",
                dedupe_key="meter:wrong-provider",
                entry_type=SpendEntryType.USAGE,
                amount_microusd=1,
                effective_at=NOW,
                generation_attempt_id=budget_context.first_attempt_id,
                now=NOW,
            )

        associated = await record_spend_entry(
            session,
            provider="salad",
            dedupe_key="meter:associated",
            entry_type=SpendEntryType.USAGE,
            amount_microusd=1,
            effective_at=NOW,
            generation_attempt_id=budget_context.first_attempt_id,
            now=NOW,
        )
        assert associated.replayed is False

        with pytest.raises(BudgetAttemptNotFoundError):
            await release_attempt_reservation(
                session,
                provider="salad",
                attempt_id=uuid4(),
                now=NOW,
            )
        with pytest.raises(BudgetConflictError):
            await release_attempt_reservation(
                session,
                provider="runpod",
                attempt_id=budget_context.first_attempt_id,
                now=NOW,
            )

        second = await session.get(GenerationAttempt, budget_context.second_attempt_id)
        assert second is not None
        second.state = GenerationAttemptState.FAILED
        second.completed_at = NOW
        with pytest.raises(BudgetConflictError):
            await release_attempt_reservation(
                session,
                provider="salad",
                attempt_id=budget_context.second_attempt_id,
                now=NOW,
            )

        december = await reevaluate_budget_guard(
            session,
            provider="salad",
            now=datetime(2026, 12, 15, tzinfo=UTC),
        )
        await session.commit()

        assert december.monthly_spend_microusd == 0
