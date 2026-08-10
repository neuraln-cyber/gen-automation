from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from typing import cast
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from gen_automation.db.models import (
    AuditEvent,
    ExperimentWarmLease,
    GenerationAttempt,
    ProviderBudgetGuard,
    ProviderSpendEntry,
    SaladDeployment,
    VideoGenerationAttempt,
)
from gen_automation.domain.enums import (
    BudgetState,
    DesiredDeploymentState,
    ExperimentWarmLeaseState,
    GenerationAttemptState,
    SpendEntryType,
)
from gen_automation.domain.video import VideoGenerationAttemptState

MICROUSD_PER_USD = 1_000_000
MAX_BIGINT = (1 << 63) - 1
_PROVIDER_PATTERN = re.compile(r"[a-z][a-z0-9_-]{0,49}\Z")
_DEDUPE_KEY_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9:._/-]{0,199}\Z")
_TERMINAL_ATTEMPT_STATES = frozenset(
    {
        GenerationAttemptState.SUCCEEDED,
        GenerationAttemptState.FAILED,
        GenerationAttemptState.CANCELLED,
    }
)
_ACTIVE_VIDEO_ATTEMPT_STATES = frozenset(
    {
        VideoGenerationAttemptState.CREATED,
        VideoGenerationAttemptState.SUBMITTING,
        VideoGenerationAttemptState.SUBMITTED,
        VideoGenerationAttemptState.RUNNING,
        VideoGenerationAttemptState.UNKNOWN,
        VideoGenerationAttemptState.CANCEL_REQUESTED,
    }
)


class BudgetError(Exception):
    """Base class for fail-closed budget-service errors."""


class BudgetConfigurationError(BudgetError):
    """A budget value or identifier is invalid."""


class BudgetGuardNotFoundError(BudgetError):
    """The provider budget guard has not been configured."""


class BudgetAttemptNotFoundError(BudgetError):
    """The generation attempt does not exist."""


class BudgetConflictError(BudgetError):
    """Stored budget state conflicts with the requested operation."""


@dataclass(frozen=True)
class BudgetSnapshot:
    guard_id: UUID
    provider: str
    state: BudgetState
    daily_limit_microusd: int
    monthly_limit_microusd: int
    daily_spend_microusd: int
    monthly_spend_microusd: int
    active_reservations_microusd: int
    active_warm_leases_microusd: int
    daily_committed_microusd: int
    monthly_committed_microusd: int
    blocked_reason: str | None
    evaluated_at: datetime


@dataclass(frozen=True)
class ReservationDecision:
    accepted: bool
    replayed: bool
    reason: str | None
    requested_microusd: int
    snapshot: BudgetSnapshot


@dataclass(frozen=True)
class SpendEntryResult:
    entry_id: UUID
    replayed: bool
    snapshot: BudgetSnapshot


@dataclass(frozen=True)
class ReservationReleaseResult:
    released: bool
    replayed: bool
    snapshot: BudgetSnapshot


def usd_to_microusd(value: Decimal) -> int:
    """Convert USD to integer micro-USD without rounding."""

    if not isinstance(value, Decimal):
        raise BudgetConfigurationError("USD values must use Decimal")
    if not value.is_finite():
        raise BudgetConfigurationError("USD values must be finite")

    value_tuple = value.as_tuple()
    digits = len(value_tuple.digits)
    exponent = cast(int, value_tuple.exponent)
    with localcontext() as context:
        context.prec = max(50, digits + abs(exponent) + 10)
        scaled = value * Decimal(MICROUSD_PER_USD)
    integral = scaled.to_integral_value()
    if scaled != integral:
        raise BudgetConfigurationError("USD values must be exact to one micro-USD")

    result = int(integral)
    if not -MAX_BIGINT <= result <= MAX_BIGINT:
        raise BudgetConfigurationError("USD value exceeds the supported range")
    return result


async def ensure_budget_guard(
    session: AsyncSession,
    *,
    provider: str,
    daily_limit_usd: Decimal,
    monthly_limit_usd: Decimal,
    now: datetime | None = None,
) -> BudgetSnapshot:
    """Create or update a provider's singleton guard and reevaluate it.

    The caller owns the surrounding transaction. This function flushes its changes
    so the configured limits and evaluated state are part of that transaction.
    """

    provider = _validated_provider(provider)
    evaluated_at = _as_utc(now or datetime.now(UTC))
    daily_limit = usd_to_microusd(daily_limit_usd)
    monthly_limit = usd_to_microusd(monthly_limit_usd)
    _validate_limits(daily_limit, monthly_limit)

    guard = await _get_guard_locked(session, provider)
    created = guard is None
    if guard is None:
        candidate = ProviderBudgetGuard(
            provider=provider,
            currency="USD",
            daily_limit_microusd=daily_limit,
            monthly_limit_microusd=monthly_limit,
            state=BudgetState.OPEN,
            lock_version=1,
            updated_at=evaluated_at,
        )
        try:
            async with session.begin_nested():
                session.add(candidate)
                await session.flush()
            guard = candidate
        except IntegrityError:
            guard = await _get_guard_locked(session, provider)
            if guard is None:
                raise BudgetConflictError("budget guard could not be initialized") from None
            created = False

    limits_changed = (
        guard.daily_limit_microusd != daily_limit or guard.monthly_limit_microusd != monthly_limit
    )
    if limits_changed:
        guard.daily_limit_microusd = daily_limit
        guard.monthly_limit_microusd = monthly_limit
        guard.lock_version += 1
        guard.updated_at = evaluated_at

    await session.flush()
    snapshot = await _evaluate_locked(session, guard, evaluated_at)
    snapshot = await _apply_evaluation(session, guard, snapshot)
    if created or limits_changed:
        session.add(
            _audit_event(
                action="provider_budget.configured",
                resource_type="provider_budget_guard",
                resource_id=guard.id,
                correlation_id=f"budget-guard:{guard.id}",
                detail={
                    "daily_limit_microusd": daily_limit,
                    "monthly_limit_microusd": monthly_limit,
                },
                occurred_at=evaluated_at,
            )
        )
    await session.flush()
    return replace(
        snapshot,
        state=guard.state,
        blocked_reason=guard.blocked_reason,
    )


async def reevaluate_budget_guard(
    session: AsyncSession,
    *,
    provider: str,
    now: datetime | None = None,
) -> BudgetSnapshot:
    """Recompute a guard and persist its blocked/open state."""

    provider = _validated_provider(provider)
    evaluated_at = _as_utc(now or datetime.now(UTC))
    guard = await _require_guard_locked(session, provider)
    snapshot = await _evaluate_locked(session, guard, evaluated_at)
    snapshot = await _apply_evaluation(session, guard, snapshot)
    await session.flush()
    return replace(
        snapshot,
        state=guard.state,
        blocked_reason=guard.blocked_reason,
    )


async def reserve_attempt_budget(
    session: AsyncSession,
    *,
    provider: str,
    attempt_id: UUID,
    amount_microusd: int,
    now: datetime | None = None,
) -> ReservationDecision:
    """Reserve budget and move a new attempt to SUBMITTING atomically.

    A rejected decision is returned instead of raised so the caller can commit the
    guard's blocked state in its existing transaction.
    """

    provider = _validated_provider(provider)
    _validate_positive_microusd(amount_microusd)
    evaluated_at = _as_utc(now or datetime.now(UTC))
    guard = await _require_guard_locked(session, provider)
    attempt = await session.scalar(
        select(GenerationAttempt).where(GenerationAttempt.id == attempt_id).with_for_update()
    )
    if attempt is None:
        raise BudgetAttemptNotFoundError("generation attempt was not found")
    if attempt.provider != provider:
        raise BudgetConflictError("generation attempt belongs to another provider")

    if (
        attempt.cost_reservation_microusd == amount_microusd
        and attempt.reservation_released_at is None
        and attempt.state == GenerationAttemptState.SUBMITTING
    ):
        snapshot = await _evaluate_locked(session, guard, evaluated_at)
        snapshot = await _apply_evaluation(session, guard, snapshot)
        await session.flush()
        return ReservationDecision(
            accepted=True,
            replayed=True,
            reason=None,
            requested_microusd=amount_microusd,
            snapshot=replace(
                snapshot,
                state=guard.state,
                blocked_reason=guard.blocked_reason,
            ),
        )

    if (
        attempt.state != GenerationAttemptState.CREATED
        or attempt.cost_reservation_microusd != 0
        or attempt.reservation_released_at is not None
    ):
        raise BudgetConflictError("generation attempt is not reservable")

    snapshot = await _evaluate_locked(session, guard, evaluated_at)
    reason = _limit_reason(snapshot, additional_microusd=amount_microusd)
    if reason is not None:
        _set_guard_state(
            session,
            guard,
            reason=reason,
            evaluated_at=evaluated_at,
        )
        await session.flush()
        return ReservationDecision(
            accepted=False,
            replayed=False,
            reason=reason,
            requested_microusd=amount_microusd,
            snapshot=replace(
                snapshot,
                state=guard.state,
                blocked_reason=guard.blocked_reason,
            ),
        )

    _set_guard_state(
        session,
        guard,
        reason=None,
        evaluated_at=evaluated_at,
    )
    attempt.cost_reservation_microusd = amount_microusd
    attempt.state = GenerationAttemptState.SUBMITTING
    attempt.submit_started_at = evaluated_at
    attempt.lock_version += 1
    session.add(
        _audit_event(
            action="provider_budget.reserved",
            resource_type="generation_attempt",
            resource_id=attempt.id,
            correlation_id=f"generation-attempt:{attempt.id}",
            detail={"amount_microusd": amount_microusd},
            occurred_at=evaluated_at,
        )
    )
    await session.flush()
    accepted_snapshot = await _evaluate_locked(session, guard, evaluated_at)
    return ReservationDecision(
        accepted=True,
        replayed=False,
        reason=None,
        requested_microusd=amount_microusd,
        snapshot=replace(
            accepted_snapshot,
            state=guard.state,
            blocked_reason=guard.blocked_reason,
        ),
    )


async def record_spend_entry(
    session: AsyncSession,
    *,
    provider: str,
    dedupe_key: str,
    entry_type: SpendEntryType,
    amount_microusd: int,
    effective_at: datetime,
    generation_attempt_id: UUID | None = None,
    salad_deployment_id: UUID | None = None,
    now: datetime | None = None,
) -> SpendEntryResult:
    """Record provider metering exactly once and reevaluate the hard guard."""

    provider = _validated_provider(provider)
    dedupe_key = _validated_dedupe_key(dedupe_key)
    _validate_spend_amount(entry_type, amount_microusd)
    effective_at = _as_utc(effective_at)
    evaluated_at = _as_utc(now or datetime.now(UTC))
    guard = await _require_guard_locked(session, provider)

    existing = await session.scalar(
        select(ProviderSpendEntry).where(
            ProviderSpendEntry.budget_guard_id == guard.id,
            ProviderSpendEntry.dedupe_key == dedupe_key,
        )
    )
    if existing is not None:
        if not _same_spend_entry(
            existing,
            entry_type=entry_type,
            amount_microusd=amount_microusd,
            effective_at=effective_at,
            generation_attempt_id=generation_attempt_id,
            salad_deployment_id=salad_deployment_id,
        ):
            raise BudgetConflictError("spend idempotency key conflicts with stored data")
        snapshot = await _evaluate_locked(session, guard, evaluated_at)
        snapshot = await _apply_evaluation(session, guard, snapshot)
        await session.flush()
        return SpendEntryResult(
            entry_id=existing.id,
            replayed=True,
            snapshot=replace(
                snapshot,
                state=guard.state,
                blocked_reason=guard.blocked_reason,
            ),
        )

    if generation_attempt_id is not None:
        attempt_provider = await session.scalar(
            select(GenerationAttempt.provider).where(GenerationAttempt.id == generation_attempt_id)
        )
        if attempt_provider is None:
            raise BudgetAttemptNotFoundError("generation attempt was not found")
        if attempt_provider != provider:
            raise BudgetConflictError("generation attempt belongs to another provider")

    entry = ProviderSpendEntry(
        budget_guard_id=guard.id,
        dedupe_key=dedupe_key,
        entry_type=entry_type,
        amount_microusd=amount_microusd,
        effective_at=effective_at,
        salad_deployment_id=salad_deployment_id,
        generation_attempt_id=generation_attempt_id,
        source_reference=None,
        detail={},
        created_at=evaluated_at,
    )
    session.add(entry)
    await session.flush()
    session.add(
        _audit_event(
            action="provider_budget.spend_recorded",
            resource_type="provider_spend_entry",
            resource_id=entry.id,
            correlation_id=f"provider-spend:{entry.id}",
            detail={
                "amount_microusd": amount_microusd,
                "entry_type": entry_type.value,
            },
            occurred_at=evaluated_at,
        )
    )
    snapshot = await _evaluate_locked(session, guard, evaluated_at)
    snapshot = await _apply_evaluation(session, guard, snapshot)
    await session.flush()
    return SpendEntryResult(
        entry_id=entry.id,
        replayed=False,
        snapshot=replace(
            snapshot,
            state=guard.state,
            blocked_reason=guard.blocked_reason,
        ),
    )


async def release_attempt_reservation(
    session: AsyncSession,
    *,
    provider: str,
    attempt_id: UUID,
    now: datetime | None = None,
) -> ReservationReleaseResult:
    """Release a reservation after a definitive outcome.

    Reservations are temporary budget commitments, never provider spend.
    Deployment runtime metering is the authoritative usage ledger, so terminal
    attempts only release their commitment.
    """

    provider = _validated_provider(provider)
    evaluated_at = _as_utc(now or datetime.now(UTC))
    guard = await _require_guard_locked(session, provider)
    attempt = await session.scalar(
        select(GenerationAttempt).where(GenerationAttempt.id == attempt_id).with_for_update()
    )
    if attempt is None:
        raise BudgetAttemptNotFoundError("generation attempt was not found")
    if attempt.provider != provider:
        raise BudgetConflictError("generation attempt belongs to another provider")
    if attempt.state not in _TERMINAL_ATTEMPT_STATES or attempt.completed_at is None:
        raise BudgetConflictError("reservation requires a definitive terminal attempt")
    if attempt.cost_reservation_microusd <= 0:
        raise BudgetConflictError("generation attempt has no active reservation")

    if attempt.reservation_released_at is not None:
        snapshot = await _evaluate_locked(session, guard, evaluated_at)
        snapshot = await _apply_evaluation(session, guard, snapshot)
        await session.flush()
        return ReservationReleaseResult(
            released=False,
            replayed=True,
            snapshot=replace(
                snapshot,
                state=guard.state,
                blocked_reason=guard.blocked_reason,
            ),
        )
    amount_microusd = attempt.cost_reservation_microusd
    attempt.reservation_released_at = evaluated_at
    attempt.lock_version += 1
    session.add(
        _audit_event(
            action="provider_budget.reservation_released",
            resource_type="generation_attempt",
            resource_id=attempt.id,
            correlation_id=f"generation-attempt:{attempt.id}",
            detail={"amount_microusd": amount_microusd},
            occurred_at=evaluated_at,
        )
    )
    await session.flush()
    snapshot = await _evaluate_locked(session, guard, evaluated_at)
    snapshot = await _apply_evaluation(session, guard, snapshot)
    await session.flush()
    return ReservationReleaseResult(
        released=True,
        replayed=False,
        snapshot=replace(
            snapshot,
            state=guard.state,
            blocked_reason=guard.blocked_reason,
        ),
    )


async def _get_guard_locked(
    session: AsyncSession,
    provider: str,
) -> ProviderBudgetGuard | None:
    result = await session.execute(
        select(ProviderBudgetGuard)
        .where(ProviderBudgetGuard.provider == provider)
        .with_for_update()
    )
    return result.scalar_one_or_none()


async def _require_guard_locked(
    session: AsyncSession,
    provider: str,
) -> ProviderBudgetGuard:
    guard = await _get_guard_locked(session, provider)
    if guard is None:
        raise BudgetGuardNotFoundError("provider budget guard is not configured")
    return guard


async def _evaluate_locked(
    session: AsyncSession,
    guard: ProviderBudgetGuard,
    evaluated_at: datetime,
) -> BudgetSnapshot:
    day_start = evaluated_at.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    month_start = day_start.replace(day=1)
    if month_start.month == 12:
        month_end = month_start.replace(
            year=month_start.year + 1,
            month=1,
        )
    else:
        month_end = month_start.replace(month=month_start.month + 1)

    async def spend_between(start: datetime, end: datetime) -> int:
        value = await session.scalar(
            select(func.coalesce(func.sum(ProviderSpendEntry.amount_microusd), 0)).where(
                ProviderSpendEntry.budget_guard_id == guard.id,
                ProviderSpendEntry.effective_at >= start,
                ProviderSpendEntry.effective_at < end,
            )
        )
        return int(value or 0)

    daily_spend = await spend_between(day_start, day_end)
    monthly_spend = await spend_between(month_start, month_end)
    reservations = await session.scalar(
        select(func.coalesce(func.sum(GenerationAttempt.cost_reservation_microusd), 0)).where(
            GenerationAttempt.provider == guard.provider,
            GenerationAttempt.cost_reservation_microusd > 0,
            GenerationAttempt.reservation_released_at.is_(None),
        )
    )
    video_reservations = await session.scalar(
        select(
            func.coalesce(
                func.sum(VideoGenerationAttempt.reserved_cost_microusd),
                0,
            )
        ).where(
            VideoGenerationAttempt.provider == guard.provider,
            VideoGenerationAttempt.reserved_cost_microusd > 0,
            VideoGenerationAttempt.state.in_(_ACTIVE_VIDEO_ATTEMPT_STATES),
        )
    )
    active_reservations = int(reservations or 0) + int(video_reservations or 0)
    active_warm_leases = 0
    if guard.provider == "salad":
        warm_leases = list(
            (
                await session.scalars(
                    select(ExperimentWarmLease).where(
                        ExperimentWarmLease.state.in_(
                            (
                                ExperimentWarmLeaseState.STARTING,
                                ExperimentWarmLeaseState.ACTIVE,
                                ExperimentWarmLeaseState.ENDING,
                            )
                        )
                    )
                )
            ).all()
        )
        for lease in warm_leases:
            realized = await session.scalar(
                select(func.coalesce(func.sum(ProviderSpendEntry.amount_microusd), 0)).where(
                    ProviderSpendEntry.salad_deployment_id == lease.salad_deployment_id,
                    ProviderSpendEntry.entry_type == SpendEntryType.USAGE,
                    ProviderSpendEntry.effective_at >= lease.started_at,
                )
            )
            active_warm_leases += max(
                lease.max_cost_microusd - int(realized or 0),
                0,
            )
    active_commitments = active_reservations + active_warm_leases
    return BudgetSnapshot(
        guard_id=guard.id,
        provider=guard.provider,
        state=guard.state,
        daily_limit_microusd=guard.daily_limit_microusd,
        monthly_limit_microusd=guard.monthly_limit_microusd,
        daily_spend_microusd=daily_spend,
        monthly_spend_microusd=monthly_spend,
        active_reservations_microusd=active_reservations,
        active_warm_leases_microusd=active_warm_leases,
        daily_committed_microusd=daily_spend + active_commitments,
        monthly_committed_microusd=monthly_spend + active_commitments,
        blocked_reason=guard.blocked_reason,
        evaluated_at=evaluated_at,
    )


async def _apply_evaluation(
    session: AsyncSession,
    guard: ProviderBudgetGuard,
    snapshot: BudgetSnapshot,
) -> BudgetSnapshot:
    reason = _limit_reason(snapshot)
    _set_guard_state(
        session,
        guard,
        reason=reason,
        evaluated_at=snapshot.evaluated_at,
    )
    if reason is not None and guard.provider == "salad":
        await _engage_salad_kill_switch(
            session,
            reason=reason,
            evaluated_at=snapshot.evaluated_at,
        )
    return replace(snapshot, state=guard.state, blocked_reason=guard.blocked_reason)


async def _engage_salad_kill_switch(
    session: AsyncSession,
    *,
    reason: str,
    evaluated_at: datetime,
) -> None:
    deployments = list(
        (
            await session.scalars(
                select(SaladDeployment)
                .where(SaladDeployment.desired_state != DesiredDeploymentState.STOPPED)
                .order_by(SaladDeployment.version_no)
                .with_for_update()
            )
        ).all()
    )
    for deployment in deployments:
        deployment.desired_state = DesiredDeploymentState.STOPPED
        deployment.reconcile_after = evaluated_at
        deployment.last_error_code = "budget_limit_exceeded"
        deployment.last_error_detail = "The hard provider budget guard engaged."
        deployment.lock_version += 1
        session.add(
            _audit_event(
                action="salad_deployment.budget_kill_switch_engaged",
                resource_type="salad_deployment",
                resource_id=deployment.id,
                correlation_id=f"salad-deployment:{deployment.id}",
                detail={"reason": reason},
                occurred_at=evaluated_at,
            )
        )
    leases = list(
        (
            await session.scalars(
                select(ExperimentWarmLease)
                .where(
                    ExperimentWarmLease.state.in_(
                        (
                            ExperimentWarmLeaseState.STARTING,
                            ExperimentWarmLeaseState.ACTIVE,
                            ExperimentWarmLeaseState.ENDING,
                        )
                    )
                )
                .order_by(ExperimentWarmLease.started_at, ExperimentWarmLease.id)
                .with_for_update()
            )
        ).all()
    )
    for lease in leases:
        lease.state = ExperimentWarmLeaseState.FAILED
        lease.ended_at = evaluated_at
        lease.lock_version += 1
        session.add(
            _audit_event(
                action="experiment_warm_lease.budget_kill_switch_engaged",
                resource_type="experiment_warm_lease",
                resource_id=lease.id,
                correlation_id=f"experiment-warm-lease:{lease.id}",
                detail={"reason": reason},
                occurred_at=evaluated_at,
            )
        )


def _limit_reason(
    snapshot: BudgetSnapshot,
    *,
    additional_microusd: int = 0,
) -> str | None:
    if snapshot.daily_committed_microusd + additional_microusd > (snapshot.daily_limit_microusd):
        return "daily_limit_exceeded"
    if snapshot.monthly_committed_microusd + additional_microusd > (
        snapshot.monthly_limit_microusd
    ):
        return "monthly_limit_exceeded"
    return None


def _set_guard_state(
    session: AsyncSession,
    guard: ProviderBudgetGuard,
    *,
    reason: str | None,
    evaluated_at: datetime,
) -> None:
    desired_state = BudgetState.BLOCKED if reason is not None else BudgetState.OPEN
    if guard.state == desired_state and guard.blocked_reason == reason:
        return

    guard.state = desired_state
    guard.blocked_reason = reason
    guard.blocked_at = evaluated_at if reason is not None else None
    guard.updated_at = evaluated_at
    guard.lock_version += 1
    action = (
        "provider_budget.blocked"
        if desired_state == BudgetState.BLOCKED
        else "provider_budget.opened"
    )
    detail: dict[str, str] = {}
    if reason is not None:
        detail["reason"] = reason
    session.add(
        _audit_event(
            action=action,
            resource_type="provider_budget_guard",
            resource_id=guard.id,
            correlation_id=f"budget-guard:{guard.id}",
            detail=detail,
            occurred_at=evaluated_at,
        )
    )


def _same_spend_entry(
    existing: ProviderSpendEntry,
    *,
    entry_type: SpendEntryType,
    amount_microusd: int,
    effective_at: datetime,
    generation_attempt_id: UUID | None,
    salad_deployment_id: UUID | None,
) -> bool:
    return (
        existing.entry_type == entry_type
        and existing.amount_microusd == amount_microusd
        and _stored_as_utc(existing.effective_at) == effective_at
        and existing.generation_attempt_id == generation_attempt_id
        and existing.salad_deployment_id == salad_deployment_id
    )


def _validated_provider(provider: str) -> str:
    normalized = provider.strip().lower()
    if not _PROVIDER_PATTERN.fullmatch(normalized):
        raise BudgetConfigurationError("provider identifier is invalid")
    return normalized


def _validated_dedupe_key(dedupe_key: str) -> str:
    if not _DEDUPE_KEY_PATTERN.fullmatch(dedupe_key):
        raise BudgetConfigurationError("spend dedupe key is invalid")
    return dedupe_key


def _validate_limits(daily_limit: int, monthly_limit: int) -> None:
    if daily_limit <= 0 or monthly_limit <= 0:
        raise BudgetConfigurationError("budget limits must be positive")
    if daily_limit > monthly_limit:
        raise BudgetConfigurationError("daily budget cannot exceed monthly budget")


def _validate_positive_microusd(amount_microusd: int) -> None:
    if isinstance(amount_microusd, bool) or not isinstance(amount_microusd, int):
        raise BudgetConfigurationError("budget amount must be integer micro-USD")
    if amount_microusd <= 0 or amount_microusd > MAX_BIGINT:
        raise BudgetConfigurationError("budget reservation amount is invalid")


def _validate_spend_amount(
    entry_type: SpendEntryType,
    amount_microusd: int,
) -> None:
    if not isinstance(entry_type, SpendEntryType):
        raise BudgetConfigurationError("spend entry type is invalid")
    if isinstance(amount_microusd, bool) or not isinstance(amount_microusd, int):
        raise BudgetConfigurationError("spend amount must be integer micro-USD")
    if amount_microusd == 0 or not -MAX_BIGINT <= amount_microusd <= MAX_BIGINT:
        raise BudgetConfigurationError("spend amount is invalid")
    if entry_type == SpendEntryType.USAGE and amount_microusd < 0:
        raise BudgetConfigurationError("usage spend cannot be negative")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise BudgetConfigurationError("budget timestamps must include a timezone")
    return value.astimezone(UTC)


def _stored_as_utc(value: datetime) -> datetime:
    """Normalize a DB timestamp; SQLite discards timezone metadata."""

    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _audit_event(
    *,
    action: str,
    resource_type: str,
    resource_id: UUID,
    correlation_id: str,
    detail: Mapping[str, str | int],
    occurred_at: datetime,
) -> AuditEvent:
    audit_detail: dict[str, object] = dict(detail)
    return AuditEvent(
        actor="system",
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        correlation_id=correlation_id,
        detail=audit_detail,
        occurred_at=occurred_at,
    )
