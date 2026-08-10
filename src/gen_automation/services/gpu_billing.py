"""Reload-safe, provider-authoritative GPU billing timer snapshots."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gen_automation.db.models import SaladDeployment
from gen_automation.domain.enums import (
    DesiredDeploymentState,
    SaladDeploymentPurpose,
    SaladDeploymentState,
)

_OBSERVATION_STALE_AFTER = timedelta(seconds=90)


class GpuBillingState(StrEnum):
    NOT_STARTED = "not_started"
    CHARGING = "charging"
    STOPPING = "stopping"
    PAUSED = "paused"
    ENDED = "ended"
    STALE = "stale"


@dataclass(frozen=True, slots=True)
class GpuBillingSnapshot:
    state: GpuBillingState
    elapsed_seconds: int
    running_instances: int
    session_started_at: datetime | None
    observed_at: datetime | None
    estimated: bool
    fresh_for_seconds: int
    session_ended_at: datetime | None = None

    @property
    def unresolved(self) -> bool:
        return self.state in {
            GpuBillingState.CHARGING,
            GpuBillingState.STOPPING,
            GpuBillingState.PAUSED,
            GpuBillingState.STALE,
        }


async def load_shared_gpu_billing_snapshot(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    purpose: SaladDeploymentPurpose = SaladDeploymentPurpose.IMAGE,
) -> GpuBillingSnapshot:
    """Load one purpose-specific worker session, including superseded deployments.

    Rollout marks the replacement current before the old provider group has
    necessarily stopped. An unresolved session therefore wins over
    ``is_current`` so paid shutdown time cannot disappear from the dashboard.
    """

    current_time = _as_utc(now or datetime.now(UTC))
    deployments = list(
        (
            await session.scalars(
                select(SaladDeployment)
                .where(SaladDeployment.purpose == purpose)
                .order_by(
                    SaladDeployment.version_no.desc(),
                    SaladDeployment.id.desc(),
                )
            )
        ).all()
    )
    deployment = _select_billing_deployment(deployments, now=current_time)
    if deployment is None:
        return GpuBillingSnapshot(
            state=GpuBillingState.NOT_STARTED,
            elapsed_seconds=0,
            running_instances=0,
            session_started_at=None,
            observed_at=None,
            estimated=False,
            fresh_for_seconds=0,
        )
    return _snapshot_for_deployment(deployment, now=current_time)


def gpu_billing_payload(snapshot: GpuBillingSnapshot) -> dict[str, object]:
    """Return the intentionally small, stable public progress contract."""

    return {
        "state": snapshot.state.value,
        "elapsed_seconds": snapshot.elapsed_seconds,
        "running_instances": snapshot.running_instances,
        "session_started_at": _isoformat_or_none(snapshot.session_started_at),
        "observed_at": _isoformat_or_none(snapshot.observed_at),
        "estimated": snapshot.estimated,
        "fresh_for_seconds": snapshot.fresh_for_seconds,
    }


def gpu_billing_poll_after_ms(
    snapshot: GpuBillingSnapshot,
    *,
    settled_poll_after_ms: int,
) -> int:
    if snapshot.unresolved:
        return max(3_000, settled_poll_after_ms)
    return settled_poll_after_ms


def _select_billing_deployment(
    deployments: list[SaladDeployment],
    *,
    now: datetime,
) -> SaladDeployment | None:
    unresolved = [
        deployment
        for deployment in deployments
        if (
            deployment.billing_session_started_at is not None
            and deployment.billing_session_ended_at is None
        )
        or (
            deployment.billing_observation_stale
            and deployment.state not in {SaladDeploymentState.STOPPED, SaladDeploymentState.FAILED}
        )
        or (
            _provider_group_can_restart(deployment)
            and _billing_observation_is_aged(deployment, now=now)
        )
    ]
    if unresolved:
        return max(
            unresolved,
            key=lambda deployment: (
                deployment.billing_active_started_at is not None,
                _timestamp_key(deployment.billing_session_started_at),
                deployment.version_no,
            ),
        )

    current = next((deployment for deployment in deployments if deployment.is_current), None)
    if current is not None:
        return current

    completed = [
        deployment
        for deployment in deployments
        if deployment.billing_session_started_at is not None
    ]
    if completed:
        return max(
            completed,
            key=lambda deployment: (
                _timestamp_key(deployment.billing_session_started_at),
                deployment.version_no,
            ),
        )

    return None


def _snapshot_for_deployment(
    deployment: SaladDeployment,
    *,
    now: datetime,
) -> GpuBillingSnapshot:
    session_started_at = _stored_as_utc_or_none(deployment.billing_session_started_at)
    session_ended_at = _stored_as_utc_or_none(deployment.billing_session_ended_at)
    observed_at = _stored_as_utc_or_none(deployment.billing_observed_at)
    active_started_at = _stored_as_utc_or_none(deployment.billing_active_started_at)
    accumulated_microseconds = max(0, deployment.billing_accumulated_microseconds or 0)
    provider_observation_stale = bool(
        deployment.billing_observation_stale
        or (
            _provider_group_can_restart(deployment)
            and _billing_observation_is_aged(deployment, now=now)
        )
    )

    if session_started_at is None:
        if provider_observation_stale:
            return GpuBillingSnapshot(
                state=GpuBillingState.STALE,
                elapsed_seconds=0,
                running_instances=(
                    1 if (deployment.ready_replicas or deployment.observed_replicas or 0) > 0 else 0
                ),
                session_started_at=None,
                observed_at=observed_at,
                estimated=True,
                fresh_for_seconds=0,
            )
        return GpuBillingSnapshot(
            state=GpuBillingState.NOT_STARTED,
            elapsed_seconds=0,
            running_instances=0,
            session_started_at=None,
            observed_at=observed_at,
            estimated=False,
            fresh_for_seconds=0,
        )

    if provider_observation_stale and session_ended_at is not None:
        return GpuBillingSnapshot(
            state=GpuBillingState.STALE,
            elapsed_seconds=accumulated_microseconds // 1_000_000,
            running_instances=(
                1 if (deployment.ready_replicas or deployment.observed_replicas or 0) > 0 else 0
            ),
            # After a completed prior session, this failed read may be the
            # first observation of a new scale-up; its exact start is unknown.
            session_started_at=(None if session_ended_at is not None else session_started_at),
            observed_at=observed_at,
            estimated=True,
            fresh_for_seconds=0,
        )

    if session_ended_at is not None:
        return GpuBillingSnapshot(
            state=GpuBillingState.ENDED,
            elapsed_seconds=accumulated_microseconds // 1_000_000,
            running_instances=0,
            session_started_at=session_started_at,
            observed_at=observed_at,
            estimated=bool(deployment.billing_estimated),
            fresh_for_seconds=0,
            session_ended_at=session_ended_at,
        )

    stale = bool(
        provider_observation_stale
        or observed_at is None
        or now - observed_at >= _OBSERVATION_STALE_AFTER
    )
    running_instances = 1 if active_started_at is not None else 0
    if active_started_at is not None:
        # A stale UI must freeze at the last successful provider observation;
        # it cannot continue claiming unsupported paid seconds.
        active_cutoff = observed_at if stale and observed_at is not None else now
        if active_cutoff > active_started_at:
            accumulated_microseconds += _timedelta_microseconds(active_cutoff - active_started_at)

    if stale:
        state = GpuBillingState.STALE
    elif active_started_at is None:
        state = GpuBillingState.PAUSED
    elif (
        deployment.desired_state == DesiredDeploymentState.STOPPED
        or deployment.state == SaladDeploymentState.DRAINING
    ):
        state = GpuBillingState.STOPPING
    else:
        state = GpuBillingState.CHARGING

    return GpuBillingSnapshot(
        state=state,
        elapsed_seconds=accumulated_microseconds // 1_000_000,
        running_instances=running_instances,
        session_started_at=session_started_at,
        observed_at=observed_at,
        estimated=bool(deployment.billing_estimated or stale),
        fresh_for_seconds=_fresh_for_seconds(now=now, observed_at=observed_at, stale=stale),
    )


def _timedelta_microseconds(value: timedelta) -> int:
    return ((value.days * 86_400 + value.seconds) * 1_000_000) + value.microseconds


def _timestamp_key(value: datetime | None) -> int:
    if value is None:
        return -1
    normalized = _stored_as_utc_or_none(value)
    assert normalized is not None
    delta = normalized - datetime(1970, 1, 1, tzinfo=UTC)
    return _timedelta_microseconds(delta)


def _fresh_for_seconds(
    *,
    now: datetime,
    observed_at: datetime | None,
    stale: bool,
) -> int:
    if stale or observed_at is None:
        return 0
    remaining = _OBSERVATION_STALE_AFTER - max(timedelta(), now - observed_at)
    return max(0, int(remaining.total_seconds()))


def _provider_group_can_restart(deployment: SaladDeployment) -> bool:
    return bool(
        deployment.provider_container_group_id is not None
        and deployment.state not in {SaladDeploymentState.STOPPED, SaladDeploymentState.FAILED}
    )


def _billing_observation_is_aged(
    deployment: SaladDeployment,
    *,
    now: datetime,
) -> bool:
    observed_at = _stored_as_utc_or_none(deployment.billing_observed_at)
    return bool(observed_at is None or now - observed_at >= _OBSERVATION_STALE_AFTER)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("GPU billing snapshot time must include a timezone")
    return value.astimezone(UTC)


def _stored_as_utc_or_none(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _isoformat_or_none(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value is not None else None
