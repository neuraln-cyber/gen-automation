from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import exists, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from gen_automation.config import Settings
from gen_automation.db.models import (
    AuditEvent,
    ExperimentWarmLease,
    GenerationAttempt,
    SaladDeployment,
)
from gen_automation.domain.enums import (
    BudgetState,
    DesiredDeploymentState,
    ExperimentWarmLeaseState,
    GenerationAttemptState,
    SaladDeploymentPurpose,
    SaladDeploymentState,
)
from gen_automation.services.budgets import BudgetSnapshot, reevaluate_budget_guard
from gen_automation.services.managed_artifact_manifest import (
    ManagedArtifactManifestError,
    effective_artifact_manifest_from_settings,
)

DEFAULT_EXPERIMENT_WARM_LEASE_SECONDS = 15 * 60
MAX_EXPERIMENT_WARM_LEASE_SECONDS = 90 * 60
MIN_EXPERIMENT_WARM_LEASE_SECONDS = 60

_LIVE_STATES = (
    ExperimentWarmLeaseState.STARTING,
    ExperimentWarmLeaseState.ACTIVE,
    ExperimentWarmLeaseState.ENDING,
)
_USABLE_STATES = (
    ExperimentWarmLeaseState.STARTING,
    ExperimentWarmLeaseState.ACTIVE,
)
_BUSY_ATTEMPT_STATES = (
    GenerationAttemptState.SUBMITTING,
    GenerationAttemptState.SUBMITTED,
    GenerationAttemptState.RUNNING,
    GenerationAttemptState.UNKNOWN,
    GenerationAttemptState.CANCEL_REQUESTED,
)


class ExperimentWarmLeaseError(Exception):
    """Base error for the bounded experiment GPU warm lease."""


class ExperimentWarmLeaseNotFoundError(ExperimentWarmLeaseError):
    pass


class ExperimentWarmLeaseConflictError(ExperimentWarmLeaseError):
    pass


class ExperimentWarmLeaseBudgetError(ExperimentWarmLeaseError):
    pass


@dataclass(frozen=True, slots=True)
class ExperimentWarmLeaseStatus:
    lease_id: UUID
    salad_deployment_id: UUID
    state: ExperimentWarmLeaseState
    started_at: datetime
    expires_at: datetime
    hard_expires_at: datetime
    ended_at: datetime | None
    last_activity_at: datetime | None
    idle_ttl_seconds: int
    max_cost_microusd: int
    provider_version: int | None
    requested_checkpoint_sha256: str | None
    requested_lora_sha256s: tuple[str, ...] | None
    ready: bool
    usable: bool
    remaining_seconds: int
    hard_remaining_seconds: int


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _validate_actor(actor: str) -> str:
    normalized = actor.strip()
    if (
        not normalized
        or len(normalized) > 200
        or any(ord(character) < 32 for character in normalized)
    ):
        raise ValueError("actor is invalid")
    return normalized


def _validate_duration(duration_seconds: int) -> int:
    if (
        not isinstance(duration_seconds, int)
        or isinstance(duration_seconds, bool)
        or not MIN_EXPERIMENT_WARM_LEASE_SECONDS
        <= duration_seconds
        <= MAX_EXPERIMENT_WARM_LEASE_SECONDS
    ):
        raise ValueError("warm lease duration must be between 60 and 5400 seconds")
    return duration_seconds


def _validate_sha256(value: str, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or value != value.lower()
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _requested_artifact_selection(
    *,
    requested_checkpoint_sha256: str | None,
    requested_lora_sha256s: Collection[str] | None,
) -> tuple[str | None, tuple[str, ...] | None]:
    if requested_checkpoint_sha256 is None:
        if requested_lora_sha256s is not None and len(requested_lora_sha256s) != 0:
            raise ValueError("requested LoRAs require a requested checkpoint")
        return None, None
    checkpoint_sha256 = _validate_sha256(
        requested_checkpoint_sha256,
        label="requested checkpoint",
    )
    lora_sha256s = tuple(requested_lora_sha256s or ())
    if len(lora_sha256s) > 8:
        raise ValueError("at most 8 requested LoRAs are supported")
    if len(lora_sha256s) != len(set(lora_sha256s)):
        raise ValueError("requested LoRA identities must be unique")
    validated_loras = tuple(
        sorted(_validate_sha256(value, label="requested LoRA") for value in lora_sha256s)
    )
    return checkpoint_sha256, validated_loras


async def _validate_requested_artifact_manifest(
    session: AsyncSession,
    *,
    settings: Settings | None,
    requested_checkpoint_sha256: str | None,
    requested_lora_sha256s: tuple[str, ...] | None,
) -> None:
    if requested_checkpoint_sha256 is None:
        return
    if settings is None:
        raise ValueError("settings are required for an explicit warm artifact selection")
    try:
        await effective_artifact_manifest_from_settings(
            session,
            settings=settings,
            required_checkpoint_sha256=requested_checkpoint_sha256,
            required_lora_sha256s=requested_lora_sha256s or (),
        )
    except ManagedArtifactManifestError as error:
        raise ExperimentWarmLeaseConflictError(
            "requested warm artifact selection is unavailable"
        ) from error


def _maximum_cost(max_hourly_cost_microusd: int, duration_seconds: int) -> int:
    if max_hourly_cost_microusd <= 0 or duration_seconds <= 0:
        raise ValueError("warm lease cost envelope is invalid")
    return max(
        1,
        (max_hourly_cost_microusd * duration_seconds + 3599) // 3600,
    )


def _preflight_budget(snapshot: BudgetSnapshot, *, additional_microusd: int) -> None:
    if snapshot.state != BudgetState.OPEN:
        raise ExperimentWarmLeaseBudgetError("Salad budget guard is blocked")
    if additional_microusd <= 0:
        raise ValueError("warm lease budget request must be positive")
    if snapshot.daily_committed_microusd + additional_microusd > snapshot.daily_limit_microusd:
        raise ExperimentWarmLeaseBudgetError("daily Salad budget cannot cover the warm lease")
    if snapshot.monthly_committed_microusd + additional_microusd > snapshot.monthly_limit_microusd:
        raise ExperimentWarmLeaseBudgetError("monthly Salad budget cannot cover the warm lease")


def _require_warmable_deployment(deployment: SaladDeployment) -> None:
    if (
        not deployment.is_current
        or deployment.state != SaladDeploymentState.ACTIVE
        or deployment.desired_state != DesiredDeploymentState.ACTIVE
        or deployment.provider_queue_id is None
        or deployment.provider_container_group_id is None
    ):
        raise ExperimentWarmLeaseConflictError("Salad deployment is not warmable")
    if (
        deployment.min_replicas != 0
        or deployment.max_replicas != 1
        or deployment.desired_queue_length != 1
    ):
        raise ExperimentWarmLeaseConflictError(
            "experiment warming requires the single-replica scale-to-zero deployment"
        )


def _is_usable(lease: ExperimentWarmLease, now: datetime) -> bool:
    if lease.state == ExperimentWarmLeaseState.STARTING:
        return _as_utc(lease.hard_expires_at) > now
    return (
        lease.state == ExperimentWarmLeaseState.ACTIVE
        and _as_utc(lease.expires_at) > now
        and _as_utc(lease.hard_expires_at) > now
    )


async def _has_busy_or_unobserved_completion(
    session: AsyncSession,
    lease: ExperimentWarmLease,
) -> bool:
    activity_floor = lease.last_activity_at or lease.started_at
    attempt_id = await session.scalar(
        select(GenerationAttempt.id)
        .where(
            GenerationAttempt.salad_deployment_id == lease.salad_deployment_id,
            or_(
                GenerationAttempt.state.in_(_BUSY_ATTEMPT_STATES),
                (
                    GenerationAttempt.completed_at.is_not(None)
                    & (GenerationAttempt.completed_at > activity_floor)
                ),
            ),
        )
        .limit(1)
    )
    return attempt_id is not None


def _status(
    lease: ExperimentWarmLease,
    deployment: SaladDeployment,
    *,
    now: datetime,
) -> ExperimentWarmLeaseStatus:
    expires_at = _as_utc(lease.expires_at)
    hard_expires_at = _as_utc(lease.hard_expires_at)
    usable = _is_usable(lease, now)
    return ExperimentWarmLeaseStatus(
        lease_id=lease.id,
        salad_deployment_id=lease.salad_deployment_id,
        state=lease.state,
        started_at=_as_utc(lease.started_at),
        expires_at=expires_at,
        hard_expires_at=hard_expires_at,
        ended_at=_as_utc(lease.ended_at) if lease.ended_at is not None else None,
        last_activity_at=(
            _as_utc(lease.last_activity_at) if lease.last_activity_at is not None else None
        ),
        idle_ttl_seconds=lease.idle_ttl_seconds,
        max_cost_microusd=lease.max_cost_microusd,
        provider_version=lease.provider_version,
        requested_checkpoint_sha256=lease.requested_checkpoint_sha256,
        requested_lora_sha256s=(
            tuple(lease.requested_lora_sha256s)
            if lease.requested_lora_sha256s is not None
            else None
        ),
        ready=(
            usable
            and lease.state == ExperimentWarmLeaseState.ACTIVE
            and (deployment.ready_replicas or 0) >= 1
        ),
        usable=usable,
        remaining_seconds=(
            max(0, int((expires_at - now).total_seconds()))
            if lease.state == ExperimentWarmLeaseState.ACTIVE
            else 0
        ),
        hard_remaining_seconds=max(0, int((hard_expires_at - now).total_seconds())),
    )


def _audit(
    session: AsyncSession,
    lease: ExperimentWarmLease,
    *,
    actor: str,
    action: str,
    occurred_at: datetime,
    detail: dict[str, object],
) -> None:
    session.add(
        AuditEvent(
            actor=actor,
            action=action,
            resource_type="experiment_warm_lease",
            resource_id=lease.id,
            correlation_id=f"experiment-warm-lease:{lease.id}",
            detail=detail,
            occurred_at=occurred_at,
        )
    )


async def start_experiment_warm_lease(
    session: AsyncSession,
    *,
    salad_deployment_id: UUID,
    actor: str,
    duration_seconds: int = DEFAULT_EXPERIMENT_WARM_LEASE_SECONDS,
    requested_checkpoint_sha256: str | None = None,
    requested_lora_sha256s: Collection[str] | None = None,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> ExperimentWarmLeaseStatus:
    """Create one cost-preflighted, absolute warm lease without mutating Salad yet."""

    requested_at = _as_utc(now or datetime.now(UTC))
    duration_seconds = _validate_duration(duration_seconds)
    actor = _validate_actor(actor)
    requested_checkpoint_sha256, normalized_lora_sha256s = _requested_artifact_selection(
        requested_checkpoint_sha256=requested_checkpoint_sha256,
        requested_lora_sha256s=requested_lora_sha256s,
    )
    await _validate_requested_artifact_manifest(
        session,
        settings=settings,
        requested_checkpoint_sha256=requested_checkpoint_sha256,
        requested_lora_sha256s=normalized_lora_sha256s,
    )
    budget = await reevaluate_budget_guard(session, provider="salad", now=requested_at)
    deployment = await session.scalar(
        select(SaladDeployment).where(SaladDeployment.id == salad_deployment_id).with_for_update()
    )
    if deployment is None:
        raise ExperimentWarmLeaseNotFoundError("Salad deployment was not found")
    _require_warmable_deployment(deployment)

    existing = await session.scalar(
        select(ExperimentWarmLease)
        .where(
            ExperimentWarmLease.salad_deployment_id == deployment.id,
            ExperimentWarmLease.state.in_(_LIVE_STATES),
        )
        .with_for_update()
    )
    if existing is not None:
        if _is_usable(existing, requested_at) or existing.state == ExperimentWarmLeaseState.ENDING:
            raise ExperimentWarmLeaseConflictError("an experiment warm lease is already live")
        existing.state = ExperimentWarmLeaseState.EXPIRED
        existing.ended_at = requested_at
        existing.lock_version += 1
        _audit(
            session,
            existing,
            actor="experiment-warm-runtime",
            action="experiment_warm_lease.expired",
            occurred_at=requested_at,
            detail={"reason": "superseded_expired_lease"},
        )
        await session.flush()
        # The guard snapshot above still contained the just-terminated lease's
        # commitment. Recompute under the same singleton lock before admitting
        # its replacement.
        budget = await reevaluate_budget_guard(
            session,
            provider="salad",
            now=requested_at,
        )

    # STARTING keeps one replica requested while Salad allocates and downloads the
    # immutable worker manifest. The allocation can be much longer than the idle
    # testing window, so the durable preflight covers the absolute 90-minute cap.
    maximum_cost = _maximum_cost(
        deployment.max_hourly_cost_microusd,
        MAX_EXPERIMENT_WARM_LEASE_SECONDS,
    )
    _preflight_budget(budget, additional_microusd=maximum_cost)
    hard_expires_at = requested_at + timedelta(seconds=MAX_EXPERIMENT_WARM_LEASE_SECONDS)
    lease = ExperimentWarmLease(
        salad_deployment_id=deployment.id,
        state=ExperimentWarmLeaseState.STARTING,
        started_at=requested_at,
        # The idle timer starts only after activation. Until then this sentinel is
        # the hard deadline, preserving the table's ordered expiry invariant.
        expires_at=hard_expires_at,
        hard_expires_at=hard_expires_at,
        idle_ttl_seconds=duration_seconds,
        max_cost_microusd=maximum_cost,
        requested_checkpoint_sha256=requested_checkpoint_sha256,
        requested_lora_sha256s=(
            list(normalized_lora_sha256s) if normalized_lora_sha256s is not None else None
        ),
        created_by=actor,
        lock_version=1,
    )
    session.add(lease)
    deployment.reconcile_after = requested_at
    await session.flush()
    _audit(
        session,
        lease,
        actor=actor,
        action="experiment_warm_lease.started",
        occurred_at=requested_at,
        detail={
            "duration_seconds": duration_seconds,
            "hard_max_seconds": MAX_EXPERIMENT_WARM_LEASE_SECONDS,
            "max_cost_microusd": maximum_cost,
            "requested_checkpoint_sha256": requested_checkpoint_sha256,
            "requested_lora_sha256s": (
                list(normalized_lora_sha256s) if normalized_lora_sha256s is not None else None
            ),
        },
    )
    await session.flush()
    return _status(lease, deployment, now=requested_at)


async def get_experiment_warm_lease_status(
    session: AsyncSession,
    *,
    lease_id: UUID,
    now: datetime | None = None,
) -> ExperimentWarmLeaseStatus:
    observed_at = _as_utc(now or datetime.now(UTC))
    row = (
        await session.execute(
            select(ExperimentWarmLease, SaladDeployment)
            .join(
                SaladDeployment,
                SaladDeployment.id == ExperimentWarmLease.salad_deployment_id,
            )
            .where(ExperimentWarmLease.id == lease_id)
        )
    ).one_or_none()
    if row is None:
        raise ExperimentWarmLeaseNotFoundError("experiment warm lease was not found")
    lease, deployment = row
    return _status(lease, deployment, now=observed_at)


async def get_current_experiment_warm_lease_status(
    session: AsyncSession,
    *,
    now: datetime | None = None,
) -> ExperimentWarmLeaseStatus | None:
    """Read the current deployment's live lease without renewing it."""

    observed_at = _as_utc(now or datetime.now(UTC))
    row = (
        await session.execute(
            select(ExperimentWarmLease, SaladDeployment)
            .join(
                SaladDeployment,
                SaladDeployment.id == ExperimentWarmLease.salad_deployment_id,
            )
            .where(
                SaladDeployment.is_current.is_(True),
                SaladDeployment.purpose == SaladDeploymentPurpose.IMAGE,
                ExperimentWarmLease.state.in_(_LIVE_STATES),
            )
            .order_by(ExperimentWarmLease.started_at.desc())
            .limit(1)
        )
    ).one_or_none()
    if row is None:
        return None
    lease, deployment = row
    return _status(lease, deployment, now=observed_at)


async def ensure_experiment_warm_lease(
    session: AsyncSession,
    *,
    actor: str,
    duration_seconds: int = DEFAULT_EXPERIMENT_WARM_LEASE_SECONDS,
    requested_checkpoint_sha256: str | None = None,
    requested_lora_sha256s: Collection[str] | None = None,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> ExperimentWarmLeaseStatus:
    """Idempotently start or renew the current deployment's experiment hold."""

    ensured_at = _as_utc(now or datetime.now(UTC))
    duration_seconds = _validate_duration(duration_seconds)
    actor = _validate_actor(actor)
    requested_checkpoint_sha256, normalized_lora_sha256s = _requested_artifact_selection(
        requested_checkpoint_sha256=requested_checkpoint_sha256,
        requested_lora_sha256s=requested_lora_sha256s,
    )
    deployment_id = await session.scalar(
        select(SaladDeployment.id)
        .where(
            SaladDeployment.is_current.is_(True),
            SaladDeployment.purpose == SaladDeploymentPurpose.IMAGE,
        )
        .limit(1)
    )
    if deployment_id is None:
        raise ExperimentWarmLeaseNotFoundError("current Salad deployment was not found")

    budget = await reevaluate_budget_guard(session, provider="salad", now=ensured_at)
    if budget.state != BudgetState.OPEN:
        raise ExperimentWarmLeaseBudgetError("Salad budget guard is blocked")
    deployment = await session.scalar(
        select(SaladDeployment).where(SaladDeployment.id == deployment_id).with_for_update()
    )
    if deployment is None:
        raise ExperimentWarmLeaseNotFoundError("current Salad deployment was not found")
    _require_warmable_deployment(deployment)
    lease = await session.scalar(
        select(ExperimentWarmLease)
        .where(
            ExperimentWarmLease.salad_deployment_id == deployment.id,
            ExperimentWarmLease.state.in_(_LIVE_STATES),
        )
        .with_for_update()
    )
    if lease is not None:
        await _validate_requested_artifact_manifest(
            session,
            settings=settings,
            requested_checkpoint_sha256=requested_checkpoint_sha256,
            requested_lora_sha256s=normalized_lora_sha256s,
        )
    durable_runtime_hold = (
        lease is not None
        and lease.state == ExperimentWarmLeaseState.ACTIVE
        and _as_utc(lease.hard_expires_at) > ensured_at
        and await _has_busy_or_unobserved_completion(session, lease)
    )
    if (
        lease is not None
        and requested_checkpoint_sha256 is not None
        and (
            lease.requested_checkpoint_sha256 != requested_checkpoint_sha256
            or tuple(lease.requested_lora_sha256s or ()) != normalized_lora_sha256s
        )
    ):
        raise ExperimentWarmLeaseConflictError(
            "a different experiment artifact stack is already warm"
        )
    if lease is not None and (_is_usable(lease, ensured_at) or durable_runtime_hold):
        if lease.state == ExperimentWarmLeaseState.ACTIVE:
            touch_experiment_warm_lease_locked(
                session,
                lease,
                actor=actor,
                now=ensured_at,
                allow_idle_expired=durable_runtime_hold,
            )
            await session.flush()
        return _status(lease, deployment, now=ensured_at)
    if lease is not None and lease.state == ExperimentWarmLeaseState.ENDING:
        raise ExperimentWarmLeaseConflictError("experiment warm lease is ending")
    return await start_experiment_warm_lease(
        session,
        salad_deployment_id=deployment.id,
        actor=actor,
        duration_seconds=duration_seconds,
        requested_checkpoint_sha256=requested_checkpoint_sha256,
        requested_lora_sha256s=normalized_lora_sha256s,
        settings=settings,
        now=ensured_at,
    )


async def extend_experiment_warm_lease(
    session: AsyncSession,
    *,
    lease_id: UUID,
    actor: str,
    extension_seconds: int = DEFAULT_EXPERIMENT_WARM_LEASE_SECONDS,
    now: datetime | None = None,
) -> ExperimentWarmLeaseStatus:
    extended_at = _as_utc(now or datetime.now(UTC))
    extension_seconds = _validate_duration(extension_seconds)
    actor = _validate_actor(actor)
    deployment_id = await session.scalar(
        select(ExperimentWarmLease.salad_deployment_id).where(ExperimentWarmLease.id == lease_id)
    )
    if deployment_id is None:
        raise ExperimentWarmLeaseNotFoundError("experiment warm lease was not found")
    budget = await reevaluate_budget_guard(session, provider="salad", now=extended_at)
    if budget.state != BudgetState.OPEN:
        raise ExperimentWarmLeaseBudgetError("Salad budget guard is blocked")
    deployment = await session.scalar(
        select(SaladDeployment).where(SaladDeployment.id == deployment_id).with_for_update()
    )
    lease = await session.scalar(
        select(ExperimentWarmLease).where(ExperimentWarmLease.id == lease_id).with_for_update()
    )
    if deployment is None or lease is None:
        raise ExperimentWarmLeaseNotFoundError("experiment warm lease was not found")
    _require_warmable_deployment(deployment)
    if lease.state != ExperimentWarmLeaseState.ACTIVE or not _is_usable(lease, extended_at):
        raise ExperimentWarmLeaseConflictError("experiment warm lease is no longer extendable")

    new_expiry = max(_as_utc(lease.expires_at), extended_at) + timedelta(seconds=extension_seconds)
    hard_expiry = _as_utc(lease.hard_expires_at)
    if new_expiry > hard_expiry:
        raise ExperimentWarmLeaseConflictError("experiment warm lease reached its 90-minute cap")
    lease.expires_at = new_expiry
    lease.lock_version += 1
    _audit(
        session,
        lease,
        actor=actor,
        action="experiment_warm_lease.extended",
        occurred_at=extended_at,
        detail={
            "extension_seconds": extension_seconds,
            "expires_at": new_expiry.isoformat(),
            "max_cost_microusd": lease.max_cost_microusd,
        },
    )
    await session.flush()
    return _status(lease, deployment, now=extended_at)


async def end_experiment_warm_lease(
    session: AsyncSession,
    *,
    lease_id: UUID,
    actor: str,
    now: datetime | None = None,
) -> ExperimentWarmLeaseStatus:
    requested_at = _as_utc(now or datetime.now(UTC))
    actor = _validate_actor(actor)
    deployment_id = await session.scalar(
        select(ExperimentWarmLease.salad_deployment_id).where(ExperimentWarmLease.id == lease_id)
    )
    if deployment_id is None:
        raise ExperimentWarmLeaseNotFoundError("experiment warm lease was not found")
    deployment = await session.scalar(
        select(SaladDeployment).where(SaladDeployment.id == deployment_id).with_for_update()
    )
    lease = await session.scalar(
        select(ExperimentWarmLease).where(ExperimentWarmLease.id == lease_id).with_for_update()
    )
    if deployment is None or lease is None:
        raise ExperimentWarmLeaseNotFoundError("experiment warm lease was not found")
    if lease.state == ExperimentWarmLeaseState.ENDING:
        return _status(lease, deployment, now=requested_at)
    if lease.state not in _USABLE_STATES:
        raise ExperimentWarmLeaseConflictError("experiment warm lease is already terminal")

    lease.state = ExperimentWarmLeaseState.ENDING
    lease.lock_version += 1
    deployment.reconcile_after = requested_at
    _audit(
        session,
        lease,
        actor=actor,
        action="experiment_warm_lease.end_requested",
        occurred_at=requested_at,
        detail={},
    )
    await session.flush()
    return _status(lease, deployment, now=requested_at)


async def expire_experiment_warm_leases(
    session: AsyncSession,
    *,
    actor: str = "experiment-warm-runtime",
    limit: int = 25,
    now: datetime | None = None,
) -> tuple[UUID, ...]:
    if limit <= 0 or limit > 1000:
        raise ValueError("warm lease expiry limit must be between 1 and 1000")
    expired_at = _as_utc(now or datetime.now(UTC))
    actor = _validate_actor(actor)
    leases = list(
        (
            await session.scalars(
                select(ExperimentWarmLease)
                .where(
                    or_(
                        (ExperimentWarmLease.state == ExperimentWarmLeaseState.STARTING)
                        & (ExperimentWarmLease.hard_expires_at <= expired_at),
                        (ExperimentWarmLease.state == ExperimentWarmLeaseState.ACTIVE)
                        & (
                            (ExperimentWarmLease.expires_at <= expired_at)
                            | (ExperimentWarmLease.hard_expires_at <= expired_at)
                        ),
                    ),
                )
                .order_by(ExperimentWarmLease.expires_at, ExperimentWarmLease.id)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        ).all()
    )
    expired: list[ExperimentWarmLease] = []
    for lease in leases:
        if (
            lease.state == ExperimentWarmLeaseState.ACTIVE
            and _as_utc(lease.hard_expires_at) > expired_at
            and await _has_busy_or_unobserved_completion(session, lease)
        ):
            continue
        lease.state = ExperimentWarmLeaseState.EXPIRED
        lease.ended_at = expired_at
        lease.lock_version += 1
        _audit(
            session,
            lease,
            actor=actor,
            action="experiment_warm_lease.expired",
            occurred_at=expired_at,
            detail={"reason": "absolute_expiry"},
        )
        expired.append(lease)
    if expired:
        await session.execute(
            update(SaladDeployment)
            .where(SaladDeployment.id.in_(tuple(lease.salad_deployment_id for lease in expired)))
            .values(reconcile_after=expired_at)
        )
    await session.flush()
    return tuple(lease.id for lease in expired)


async def complete_ending_experiment_warm_leases(
    session: AsyncSession,
    *,
    actor: str = "experiment-warm-runtime",
    limit: int = 25,
    now: datetime | None = None,
) -> tuple[UUID, ...]:
    if limit <= 0 or limit > 1000:
        raise ValueError("warm lease completion limit must be between 1 and 1000")
    completed_at = _as_utc(now or datetime.now(UTC))
    actor = _validate_actor(actor)
    leases = list(
        (
            await session.scalars(
                select(ExperimentWarmLease)
                .join(
                    SaladDeployment,
                    SaladDeployment.id == ExperimentWarmLease.salad_deployment_id,
                )
                .where(
                    ExperimentWarmLease.state == ExperimentWarmLeaseState.ENDING,
                    SaladDeployment.observed_replicas == 0,
                    SaladDeployment.ready_replicas == 0,
                )
                .order_by(ExperimentWarmLease.updated_at, ExperimentWarmLease.id)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        ).all()
    )
    for lease in leases:
        lease.state = ExperimentWarmLeaseState.ENDED
        lease.ended_at = completed_at
        lease.lock_version += 1
        _audit(
            session,
            lease,
            actor=actor,
            action="experiment_warm_lease.ended",
            occurred_at=completed_at,
            detail={},
        )
    await session.flush()
    return tuple(lease.id for lease in leases)


async def next_starting_experiment_warm_lease_id(
    session: AsyncSession,
    *,
    now: datetime | None = None,
) -> UUID | None:
    observed_at = _as_utc(now or datetime.now(UTC))
    return await session.scalar(
        select(ExperimentWarmLease.id)
        .where(
            ExperimentWarmLease.state == ExperimentWarmLeaseState.STARTING,
            ExperimentWarmLease.provider_version.is_(None),
            ExperimentWarmLease.hard_expires_at > observed_at,
        )
        .order_by(ExperimentWarmLease.started_at, ExperimentWarmLease.id)
        .limit(1)
    )


async def load_live_experiment_warm_lease_for_update(
    session: AsyncSession,
    *,
    salad_deployment_id: UUID,
    now: datetime | None = None,
) -> ExperimentWarmLease | None:
    observed_at = _as_utc(now or datetime.now(UTC))
    lease: ExperimentWarmLease | None = await session.scalar(
        select(ExperimentWarmLease)
        .where(
            ExperimentWarmLease.salad_deployment_id == salad_deployment_id,
            or_(
                (ExperimentWarmLease.state == ExperimentWarmLeaseState.STARTING)
                & (ExperimentWarmLease.hard_expires_at > observed_at),
                (ExperimentWarmLease.state == ExperimentWarmLeaseState.ACTIVE)
                & (ExperimentWarmLease.expires_at > observed_at)
                & (ExperimentWarmLease.hard_expires_at > observed_at),
            ),
        )
        .with_for_update()
    )
    return lease


def activate_experiment_warm_lease_locked(
    session: AsyncSession,
    lease: ExperimentWarmLease,
    *,
    provider_version: int | None,
    actor: str = "experiment-warm-runtime",
    now: datetime | None = None,
) -> None:
    activated_at = _as_utc(now or datetime.now(UTC))
    actor = _validate_actor(actor)
    if lease.state == ExperimentWarmLeaseState.ACTIVE:
        return
    if lease.state != ExperimentWarmLeaseState.STARTING or not _is_usable(lease, activated_at):
        raise ExperimentWarmLeaseConflictError("experiment warm lease is not activatable")
    resolved_provider_version = provider_version or lease.provider_version
    if resolved_provider_version is None:
        raise ExperimentWarmLeaseConflictError("experiment warm runtime has not been refreshed")
    if resolved_provider_version <= 0:
        raise ValueError("provider version must be positive")
    idle_expires_at = min(
        activated_at + timedelta(seconds=lease.idle_ttl_seconds),
        _as_utc(lease.hard_expires_at),
    )
    if idle_expires_at <= activated_at:
        raise ExperimentWarmLeaseConflictError("experiment warm lease reached its hard cap")
    lease.state = ExperimentWarmLeaseState.ACTIVE
    lease.expires_at = idle_expires_at
    lease.last_activity_at = activated_at
    lease.provider_version = resolved_provider_version
    lease.lock_version += 1
    _audit(
        session,
        lease,
        actor=actor,
        action="experiment_warm_lease.activated",
        occurred_at=activated_at,
        detail={
            "provider_version": resolved_provider_version,
            "expires_at": idle_expires_at.isoformat(),
        },
    )


def mark_experiment_warm_runtime_refreshed_locked(
    session: AsyncSession,
    lease: ExperimentWarmLease,
    *,
    provider_version: int,
    actor: str = "experiment-warm-runtime",
    now: datetime | None = None,
) -> None:
    """Persist the one runtime-binding rollout required before warm allocation."""

    refreshed_at = _as_utc(now or datetime.now(UTC))
    actor = _validate_actor(actor)
    if provider_version <= 0:
        raise ValueError("provider version must be positive")
    if lease.state != ExperimentWarmLeaseState.STARTING or not _is_usable(lease, refreshed_at):
        raise ExperimentWarmLeaseConflictError("experiment warm lease is not refreshable")
    if lease.provider_version is not None:
        return
    lease.provider_version = provider_version
    lease.lock_version += 1
    _audit(
        session,
        lease,
        actor=actor,
        action="experiment_warm_lease.runtime_refreshed",
        occurred_at=refreshed_at,
        detail={"provider_version": provider_version},
    )


def touch_experiment_warm_lease_locked(
    session: AsyncSession,
    lease: ExperimentWarmLease,
    *,
    actor: str = "experiment-warm-runtime",
    now: datetime | None = None,
    allow_idle_expired: bool = False,
) -> bool:
    """Reset the idle window after validated experiment activity, within the hard cap."""

    touched_at = _as_utc(now or datetime.now(UTC))
    actor = _validate_actor(actor)
    if (
        lease.state != ExperimentWarmLeaseState.ACTIVE
        or _as_utc(lease.hard_expires_at) <= touched_at
        or (not allow_idle_expired and _as_utc(lease.expires_at) <= touched_at)
    ):
        return False
    new_expiry = min(
        touched_at + timedelta(seconds=lease.idle_ttl_seconds),
        _as_utc(lease.hard_expires_at),
    )
    previous_activity = (
        _as_utc(lease.last_activity_at) if lease.last_activity_at is not None else None
    )
    if previous_activity is not None and touched_at <= previous_activity:
        return False
    lease.expires_at = max(_as_utc(lease.expires_at), new_expiry)
    lease.last_activity_at = touched_at
    lease.lock_version += 1
    _audit(
        session,
        lease,
        actor=actor,
        action="experiment_warm_lease.activity_touched",
        occurred_at=touched_at,
        detail={"expires_at": _as_utc(lease.expires_at).isoformat()},
    )
    return True


async def activate_ready_experiment_warm_leases(
    session: AsyncSession,
    *,
    actor: str = "experiment-warm-runtime",
    limit: int = 25,
    now: datetime | None = None,
) -> tuple[UUID, ...]:
    """Start the idle timer only after reconciliation observes a ready replica."""

    if limit <= 0 or limit > 1000:
        raise ValueError("warm lease activation limit must be between 1 and 1000")
    activated_at = _as_utc(now or datetime.now(UTC))
    actor = _validate_actor(actor)
    candidates = list(
        (
            await session.execute(
                select(
                    ExperimentWarmLease.id,
                    ExperimentWarmLease.salad_deployment_id,
                )
                .join(
                    SaladDeployment,
                    SaladDeployment.id == ExperimentWarmLease.salad_deployment_id,
                )
                .where(
                    ExperimentWarmLease.state == ExperimentWarmLeaseState.STARTING,
                    ExperimentWarmLease.provider_version.is_not(None),
                    ExperimentWarmLease.hard_expires_at > activated_at,
                    SaladDeployment.ready_replicas >= 1,
                    SaladDeployment.is_current.is_(True),
                    SaladDeployment.purpose == SaladDeploymentPurpose.IMAGE,
                    SaladDeployment.state == SaladDeploymentState.ACTIVE,
                    SaladDeployment.desired_state == DesiredDeploymentState.ACTIVE,
                )
                .order_by(ExperimentWarmLease.started_at, ExperimentWarmLease.id)
                .limit(limit)
            )
        ).all()
    )
    activated: list[UUID] = []
    for lease_id, deployment_id in candidates:
        deployment = await session.scalar(
            select(SaladDeployment).where(SaladDeployment.id == deployment_id).with_for_update()
        )
        lease = await session.scalar(
            select(ExperimentWarmLease).where(ExperimentWarmLease.id == lease_id).with_for_update()
        )
        if (
            deployment is None
            or lease is None
            or (deployment.ready_replicas or 0) < 1
            or lease.state != ExperimentWarmLeaseState.STARTING
            or lease.provider_version is None
            or not _is_usable(lease, activated_at)
        ):
            continue
        activate_experiment_warm_lease_locked(
            session,
            lease,
            provider_version=lease.provider_version,
            actor=actor,
            now=activated_at,
        )
        activated.append(lease.id)
    await session.flush()
    return tuple(activated)


async def touch_completed_experiment_warm_leases(
    session: AsyncSession,
    *,
    actor: str = "experiment-warm-runtime",
    limit: int = 25,
    now: datetime | None = None,
) -> tuple[UUID, ...]:
    """Grant a fresh idle editing window once for each newly completed provider job."""

    if limit <= 0 or limit > 1000:
        raise ValueError("warm lease activity limit must be between 1 and 1000")
    touched_at = _as_utc(now or datetime.now(UTC))
    actor = _validate_actor(actor)
    activity_floor = func.coalesce(
        ExperimentWarmLease.last_activity_at,
        ExperimentWarmLease.started_at,
    )
    candidates = list(
        (
            await session.execute(
                select(
                    ExperimentWarmLease.id,
                    ExperimentWarmLease.salad_deployment_id,
                    func.max(GenerationAttempt.completed_at),
                )
                .join(
                    GenerationAttempt,
                    GenerationAttempt.salad_deployment_id
                    == ExperimentWarmLease.salad_deployment_id,
                )
                .where(
                    ExperimentWarmLease.state == ExperimentWarmLeaseState.ACTIVE,
                    ExperimentWarmLease.hard_expires_at > touched_at,
                    GenerationAttempt.completed_at.is_not(None),
                    GenerationAttempt.completed_at > activity_floor,
                )
                .group_by(
                    ExperimentWarmLease.id,
                    ExperimentWarmLease.salad_deployment_id,
                )
                .order_by(ExperimentWarmLease.id)
                .limit(limit)
            )
        ).all()
    )
    touched: list[UUID] = []
    for lease_id, deployment_id, completed_at in candidates:
        if completed_at is None:
            continue
        await session.scalar(
            select(SaladDeployment.id).where(SaladDeployment.id == deployment_id).with_for_update()
        )
        lease = await session.scalar(
            select(ExperimentWarmLease).where(ExperimentWarmLease.id == lease_id).with_for_update()
        )
        if lease is None:
            continue
        # Mark the provider completion as observed while starting the editing
        # window from controller time, so webhook delivery latency is not charged
        # against the user's idle testing interval.
        if touch_experiment_warm_lease_locked(
            session,
            lease,
            actor=actor,
            now=touched_at,
            allow_idle_expired=True,
        ):
            touched.append(lease.id)
    await session.flush()
    return tuple(touched)


async def effective_experiment_min_replicas(
    session: AsyncSession,
    *,
    salad_deployment_id: UUID,
    now: datetime | None = None,
) -> int:
    observed_at = _as_utc(now or datetime.now(UTC))
    busy_attempt_exists = exists(
        select(GenerationAttempt.id).where(
            GenerationAttempt.salad_deployment_id == ExperimentWarmLease.salad_deployment_id,
            GenerationAttempt.state.in_(_BUSY_ATTEMPT_STATES),
        )
    )
    completion_floor = func.coalesce(
        ExperimentWarmLease.last_activity_at,
        ExperimentWarmLease.started_at,
    )
    unobserved_completion_exists = exists(
        select(GenerationAttempt.id).where(
            GenerationAttempt.salad_deployment_id == ExperimentWarmLease.salad_deployment_id,
            GenerationAttempt.completed_at.is_not(None),
            GenerationAttempt.completed_at > completion_floor,
        )
    )
    lease_id = await session.scalar(
        select(ExperimentWarmLease.id)
        .where(
            ExperimentWarmLease.salad_deployment_id == salad_deployment_id,
            or_(
                (ExperimentWarmLease.state == ExperimentWarmLeaseState.STARTING)
                & (ExperimentWarmLease.provider_version.is_not(None))
                & (ExperimentWarmLease.hard_expires_at > observed_at),
                (ExperimentWarmLease.state == ExperimentWarmLeaseState.ACTIVE)
                & (ExperimentWarmLease.provider_version.is_not(None))
                & (ExperimentWarmLease.hard_expires_at > observed_at)
                & (
                    (ExperimentWarmLease.expires_at > observed_at)
                    | busy_attempt_exists
                    | unobserved_completion_exists
                ),
            ),
        )
        .limit(1)
    )
    return 1 if lease_id is not None else 0
