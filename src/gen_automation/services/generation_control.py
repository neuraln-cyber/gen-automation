from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from gen_automation.db.models import (
    Asset,
    AuditEvent,
    GenerationAttempt,
    GenerationJob,
    ProviderBudgetGuard,
    Release,
    ReleaseVersion,
)
from gen_automation.domain.enums import (
    AssetKind,
    AssetState,
    GenerationAttemptState,
    GenerationState,
    ReleasePhase,
    ResourceHealth,
)
from gen_automation.services.collection import load_stop_salvage_status

GENERATION_STOP_REQUESTED_ACTION = "release.generation_stop_requested"
GENERATION_STOPPED_ACTION = "release.generation_stopped"
GENERATION_STOP_ERROR_CODE = "operator_generation_stop"
GENERATION_STOP_ERROR_DETAIL = (
    "Generation was stopped by the operator before this work was submitted."
)

STOPPABLE_RELEASE_PHASES = frozenset(
    {
        ReleasePhase.READY,
        ReleasePhase.GENERATING,
        ReleasePhase.PAUSED,
    }
)
DRAINING_GENERATION_JOB_STATES = frozenset(
    {
        GenerationState.CLAIMED,
        GenerationState.SUBMITTING,
        GenerationState.RUNNING,
        GenerationState.COLLECTING,
        GenerationState.VERIFYING,
        GenerationState.UNKNOWN,
        GenerationState.CANCEL_REQUESTED,
    }
)
_ACTIVE_GENERATION_ATTEMPT_STATES = frozenset(
    {
        GenerationAttemptState.CREATED,
        GenerationAttemptState.SUBMITTING,
        GenerationAttemptState.SUBMITTED,
        GenerationAttemptState.RUNNING,
        GenerationAttemptState.UNKNOWN,
        GenerationAttemptState.CANCEL_REQUESTED,
    }
)
_REMOTE_OR_AMBIGUOUS_ATTEMPT_STATES = frozenset(
    _ACTIVE_GENERATION_ATTEMPT_STATES - {GenerationAttemptState.CREATED}
)


class GenerationControlError(Exception):
    """Base error for an operator-requested generation stop."""


class GenerationControlInputError(GenerationControlError, ValueError):
    pass


class GenerationControlNotFoundError(GenerationControlError, LookupError):
    pass


class GenerationControlConflictError(GenerationControlError):
    pass


@dataclass(frozen=True, slots=True)
class StopGenerationResult:
    release_id: UUID
    phase: ReleasePhase
    replayed: bool
    cancelled_job_ids: tuple[UUID, ...]
    cancelled_attempt_ids: tuple[UUID, ...]
    draining_job_ids: tuple[UUID, ...]
    available_asset_count: int
    desired_accepted_count: int


@dataclass(frozen=True, slots=True)
class StopSettlementResult:
    release_id: UUID
    phase: ReleasePhase
    settled: bool
    replayed: bool
    cancelled_job_ids: tuple[UUID, ...]
    cancelled_attempt_ids: tuple[UUID, ...]
    draining_job_ids: tuple[UUID, ...]
    available_asset_count: int
    desired_accepted_count: int


@dataclass(frozen=True, slots=True)
class _LocalCancellationResult:
    cancelled_job_ids: tuple[UUID, ...]
    cancelled_attempt_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class _LockedGenerationContext:
    release: Release
    version: ReleaseVersion
    jobs: tuple[GenerationJob, ...]
    attempts: tuple[GenerationAttempt, ...]


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _validated_text(value: str, *, name: str, max_length: int) -> str:
    normalized = " ".join(value.split())
    if not normalized:
        raise GenerationControlInputError(f"{name} must not be empty")
    if len(normalized) > max_length:
        raise GenerationControlInputError(f"{name} is too long")
    return normalized


async def request_generation_stop(
    session: AsyncSession,
    *,
    release_id: UUID,
    actor: str = "owner",
    correlation_id: str | None = None,
    now: datetime | None = None,
) -> StopGenerationResult:
    """Fence a release and cancel only work proven not to have reached a provider.

    Provider-submitted, running, ambiguous, and collection work deliberately keeps
    draining. This preserves every output from an already-active worker rather than
    interrupting the image currently being generated or uploaded.
    """

    requested_at = _as_utc(now or datetime.now(UTC))
    normalized_actor = _validated_text(actor, name="actor", max_length=200)
    normalized_correlation_id = _validated_text(
        correlation_id or f"generation-stop:{release_id}",
        name="correlation_id",
        max_length=200,
    )
    context = await _load_generation_stop_context(session, release_id=release_id)
    release = context.release
    version = context.version
    marker_exists = await _stop_marker_exists(session, release_id=release.id)
    stopped_marker_exists = await _audit_marker_exists(
        session,
        release_id=release.id,
        action=GENERATION_STOPPED_ACTION,
    )
    if not marker_exists and release.phase not in STOPPABLE_RELEASE_PHASES:
        raise GenerationControlConflictError(
            "generation can only be stopped while the release is ready, generating, or paused"
        )

    if stopped_marker_exists:
        available_asset_count = await _available_asset_count(
            session,
            release_id=release.id,
            release_version_id=version.id,
        )
        settled_release_id = release.id
        settled_phase = release.phase
        settled_target = release.desired_accepted_count
        await session.rollback()
        return StopGenerationResult(
            release_id=settled_release_id,
            phase=settled_phase,
            replayed=True,
            cancelled_job_ids=(),
            cancelled_attempt_ids=(),
            draining_job_ids=(),
            available_asset_count=available_asset_count,
            desired_accepted_count=settled_target,
        )

    previous_phase = release.phase
    if release.phase != ReleasePhase.PAUSED:
        release.phase = ReleasePhase.PAUSED
        release.lock_version += 1

    cancellation = await _cancel_unsubmitted_work(
        session,
        jobs=context.jobs,
        attempts=context.attempts,
        actor=normalized_actor,
        correlation_id=normalized_correlation_id,
        occurred_at=requested_at,
    )
    draining_job_ids = await _draining_job_ids(
        session,
        release_version_id=version.id,
    )
    available_asset_count = await _available_asset_count(
        session,
        release_id=release.id,
        release_version_id=version.id,
    )
    if not marker_exists:
        session.add(
            AuditEvent(
                actor=normalized_actor,
                action=GENERATION_STOP_REQUESTED_ACTION,
                resource_type="release",
                resource_id=release.id,
                correlation_id=normalized_correlation_id,
                detail={
                    "release_version_id": str(version.id),
                    "previous_phase": previous_phase.value,
                    "cancelled_job_ids": [str(job_id) for job_id in cancellation.cancelled_job_ids],
                    "cancelled_attempt_ids": [
                        str(attempt_id) for attempt_id in cancellation.cancelled_attempt_ids
                    ],
                    "draining_job_ids": [str(job_id) for job_id in draining_job_ids],
                    "available_asset_count": available_asset_count,
                    "safe_drain": True,
                },
                occurred_at=requested_at,
            )
        )
    await session.commit()
    return StopGenerationResult(
        release_id=release.id,
        phase=release.phase,
        replayed=marker_exists,
        cancelled_job_ids=cancellation.cancelled_job_ids,
        cancelled_attempt_ids=cancellation.cancelled_attempt_ids,
        draining_job_ids=draining_job_ids,
        available_asset_count=available_asset_count,
        desired_accepted_count=release.desired_accepted_count,
    )


async def settle_stopped_generation_once(
    session: AsyncSession,
    *,
    release_id: UUID,
    actor: str = "generation-controller",
    now: datetime | None = None,
) -> StopSettlementResult:
    """Advance one stopped release after active provider and collection work drains."""

    settled_at = _as_utc(now or datetime.now(UTC))
    normalized_actor = _validated_text(actor, name="actor", max_length=200)
    context = await _load_generation_stop_context(session, release_id=release_id)
    release = context.release
    version = context.version
    if not await _stop_marker_exists(session, release_id=release.id):
        raise GenerationControlConflictError("generation stop was not requested for this release")
    correlation_id = f"generation-stop:{release.id}"

    stopped_marker_exists = await _audit_marker_exists(
        session,
        release_id=release.id,
        action=GENERATION_STOPPED_ACTION,
    )
    if stopped_marker_exists:
        available_asset_count = await _available_asset_count(
            session,
            release_id=release.id,
            release_version_id=version.id,
        )
        settled_release_id = release.id
        settled_phase = release.phase
        settled_target = release.desired_accepted_count
        await session.rollback()
        return StopSettlementResult(
            release_id=settled_release_id,
            phase=settled_phase,
            settled=True,
            replayed=True,
            cancelled_job_ids=(),
            cancelled_attempt_ids=(),
            draining_job_ids=(),
            available_asset_count=available_asset_count,
            desired_accepted_count=settled_target,
        )

    # A durable stop request remains authoritative even if stale controller work
    # temporarily moves the release back to a dispatchable phase.
    if release.phase != ReleasePhase.PAUSED:
        release.phase = ReleasePhase.PAUSED
        release.lock_version += 1

    cancellation = await _cancel_unsubmitted_work(
        session,
        jobs=context.jobs,
        attempts=context.attempts,
        actor=normalized_actor,
        correlation_id=correlation_id,
        occurred_at=settled_at,
    )
    draining_job_ids = await _draining_job_ids(
        session,
        release_version_id=version.id,
    )
    available_asset_count = await _available_asset_count(
        session,
        release_id=release.id,
        release_version_id=version.id,
    )
    if draining_job_ids:
        await session.commit()
        return StopSettlementResult(
            release_id=release.id,
            phase=release.phase,
            settled=False,
            replayed=False,
            cancelled_job_ids=cancellation.cancelled_job_ids,
            cancelled_attempt_ids=cancellation.cancelled_attempt_ids,
            draining_job_ids=draining_job_ids,
            available_asset_count=available_asset_count,
            desired_accepted_count=release.desired_accepted_count,
        )

    salvage_status = await load_stop_salvage_status(
        session,
        release_version_id=version.id,
    )
    if salvage_status.pending:
        # Uploaded or concurrently verifying masters remain recoverable. Keep the
        # durable release fence in PAUSED until collection resolves every intent;
        # otherwise the quality snapshot could permanently omit valid images.
        await session.commit()
        return StopSettlementResult(
            release_id=release.id,
            phase=release.phase,
            settled=False,
            replayed=False,
            cancelled_job_ids=cancellation.cancelled_job_ids,
            cancelled_attempt_ids=cancellation.cancelled_attempt_ids,
            draining_job_ids=(),
            available_asset_count=available_asset_count,
            desired_accepted_count=release.desired_accepted_count,
        )

    previous_target = release.desired_accepted_count
    if release.health == ResourceHealth.HEALTHY and available_asset_count > 0:
        release.desired_accepted_count = min(previous_target, available_asset_count)
        release.phase = ReleasePhase.REVIEWING
    else:
        # Release.desired_accepted_count has a positive database constraint, so a
        # zero-output or unhealthy stop keeps the original target and terminates as
        # CANCELLED. Existing WARNING/BLOCKED health is deliberately preserved so
        # an operator stop cannot bypass a fail-closed integrity decision.
        release.phase = ReleasePhase.CANCELLED
    release.lock_version += 1
    session.add(
        AuditEvent(
            actor=normalized_actor,
            action=GENERATION_STOPPED_ACTION,
            resource_type="release",
            resource_id=release.id,
            correlation_id=correlation_id,
            detail={
                "release_version_id": str(version.id),
                "available_asset_count": available_asset_count,
                "previous_desired_accepted_count": previous_target,
                "desired_accepted_count": release.desired_accepted_count,
                "phase": release.phase.value,
                "health": release.health.value,
                "assets_retained": True,
                "cancelled_job_ids": [str(job_id) for job_id in cancellation.cancelled_job_ids],
                "cancelled_attempt_ids": [
                    str(attempt_id) for attempt_id in cancellation.cancelled_attempt_ids
                ],
                "safe_drain": True,
            },
            occurred_at=settled_at,
        )
    )
    await session.commit()
    return StopSettlementResult(
        release_id=release.id,
        phase=release.phase,
        settled=True,
        replayed=False,
        cancelled_job_ids=cancellation.cancelled_job_ids,
        cancelled_attempt_ids=cancellation.cancelled_attempt_ids,
        draining_job_ids=(),
        available_asset_count=available_asset_count,
        desired_accepted_count=release.desired_accepted_count,
    )


async def _load_generation_stop_context(
    session: AsyncSession,
    *,
    release_id: UUID,
) -> _LockedGenerationContext:
    # Salad mutations serialize on this singleton before locking attempt/job rows.
    # Stop follows that contract. Release/version/job rows are then acquired in one
    # ordered query, matching scheduler/collector joined locks, and attempts last.
    # This removes the former Release -> Job -> Attempt inversion without weakening
    # the durable release fence or the definitely-unsubmitted cancellation checks.
    await session.scalar(
        select(ProviderBudgetGuard.id)
        .where(ProviderBudgetGuard.provider == "salad")
        .with_for_update()
    )

    current_version_no = await session.scalar(
        select(Release.current_version_no).where(Release.id == release_id)
    )
    if current_version_no is None:
        raise GenerationControlNotFoundError("release was not found")
    version = await session.scalar(
        select(ReleaseVersion)
        .where(
            ReleaseVersion.release_id == release_id,
            ReleaseVersion.version_no == current_version_no,
        )
        .with_for_update()
    )
    if version is None:
        raise GenerationControlConflictError("current release version is unavailable")
    release = await session.scalar(
        select(Release)
        .where(Release.id == release_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if release is None:
        raise GenerationControlNotFoundError("release was not found")
    if release.current_version_no != version.version_no:
        raise GenerationControlConflictError("current release version changed concurrently")
    jobs = tuple(
        (
            await session.scalars(
                select(GenerationJob)
                .where(GenerationJob.release_version_id == version.id)
                .order_by(GenerationJob.id)
                .with_for_update()
            )
        ).all()
    )

    job_ids = tuple(job.id for job in jobs)
    attempts = (
        tuple(
            (
                await session.scalars(
                    select(GenerationAttempt)
                    .where(GenerationAttempt.job_id.in_(job_ids))
                    .order_by(GenerationAttempt.job_id, GenerationAttempt.attempt_no)
                    .with_for_update()
                )
            ).all()
        )
        if job_ids
        else ()
    )
    return _LockedGenerationContext(
        release=release,
        version=version,
        jobs=jobs,
        attempts=attempts,
    )


async def _stop_marker_exists(session: AsyncSession, *, release_id: UUID) -> bool:
    return await _audit_marker_exists(
        session,
        release_id=release_id,
        action=GENERATION_STOP_REQUESTED_ACTION,
    )


async def _audit_marker_exists(
    session: AsyncSession,
    *,
    release_id: UUID,
    action: str,
) -> bool:
    return bool(
        await session.scalar(
            select(AuditEvent.id)
            .where(
                AuditEvent.resource_type == "release",
                AuditEvent.resource_id == release_id,
                AuditEvent.action == action,
            )
            .limit(1)
        )
    )


async def _cancel_unsubmitted_work(
    session: AsyncSession,
    *,
    jobs: tuple[GenerationJob, ...],
    attempts: tuple[GenerationAttempt, ...],
    actor: str,
    correlation_id: str,
    occurred_at: datetime,
) -> _LocalCancellationResult:
    if not jobs:
        return _LocalCancellationResult(cancelled_job_ids=(), cancelled_attempt_ids=())
    attempts_by_job: dict[UUID, list[GenerationAttempt]] = {}
    for attempt in attempts:
        attempts_by_job.setdefault(attempt.job_id, []).append(attempt)

    cancelled_job_ids: list[UUID] = []
    cancelled_attempt_ids: list[UUID] = []
    for job in jobs:
        job_attempts = attempts_by_job.get(job.id, [])
        has_remote_or_ambiguous_attempt = any(
            attempt.state in _REMOTE_OR_AMBIGUOUS_ATTEMPT_STATES for attempt in job_attempts
        )
        created_attempts = [
            attempt for attempt in job_attempts if attempt.state == GenerationAttemptState.CREATED
        ]
        can_cancel_job = (
            job.state in {GenerationState.QUEUED, GenerationState.RETRY_WAIT}
            and not has_remote_or_ambiguous_attempt
        ) or (
            job.state == GenerationState.CLAIMED
            and bool(created_attempts)
            and not has_remote_or_ambiguous_attempt
        )
        if not can_cancel_job:
            continue

        for attempt in created_attempts:
            # CREATED attempts are unreserved by contract. If a corrupt row says
            # otherwise, leave it active for reconciliation instead of hiding a
            # budget commitment or pretending the provider was never contacted.
            if attempt.cost_reservation_microusd > 0 and attempt.reservation_released_at is None:
                can_cancel_job = False
                break
        if not can_cancel_job:
            continue

        for attempt in created_attempts:
            attempt.state = GenerationAttemptState.FAILED
            attempt.completed_at = occurred_at
            attempt.error_code = GENERATION_STOP_ERROR_CODE
            attempt.error_detail = GENERATION_STOP_ERROR_DETAIL
            attempt.lock_version += 1
            cancelled_attempt_ids.append(attempt.id)
            session.add(
                AuditEvent(
                    actor=actor,
                    action="generation_attempt.cancelled_before_submission",
                    resource_type="generation_attempt",
                    resource_id=attempt.id,
                    correlation_id=correlation_id,
                    detail={
                        "generation_job_id": str(job.id),
                        "recorded_state": GenerationAttemptState.FAILED.value,
                        "reason_code": GENERATION_STOP_ERROR_CODE,
                        "provider_contacted": False,
                    },
                    occurred_at=occurred_at,
                )
            )

        job.state = GenerationState.CANCELLED
        job.retry_at = None
        job.lease_owner = None
        job.lease_expires_at = None
        job.last_error_code = GENERATION_STOP_ERROR_CODE
        job.last_error_detail = GENERATION_STOP_ERROR_DETAIL
        job.lock_version += 1
        cancelled_job_ids.append(job.id)
        session.add(
            AuditEvent(
                actor=actor,
                action="generation_job.cancelled_before_submission",
                resource_type="generation_job",
                resource_id=job.id,
                correlation_id=correlation_id,
                detail={
                    "cancelled_attempt_ids": [str(attempt.id) for attempt in created_attempts],
                    "reason_code": GENERATION_STOP_ERROR_CODE,
                    "provider_contacted": False,
                },
                occurred_at=occurred_at,
            )
        )
    await session.flush()
    return _LocalCancellationResult(
        cancelled_job_ids=tuple(cancelled_job_ids),
        cancelled_attempt_ids=tuple(cancelled_attempt_ids),
    )


async def _draining_job_ids(
    session: AsyncSession,
    *,
    release_version_id: UUID,
) -> tuple[UUID, ...]:
    job_state_ids = set(
        (
            await session.scalars(
                select(GenerationJob.id).where(
                    GenerationJob.release_version_id == release_version_id,
                    GenerationJob.state.in_(DRAINING_GENERATION_JOB_STATES),
                )
            )
        ).all()
    )
    active_attempt_job_ids = set(
        (
            await session.scalars(
                select(GenerationAttempt.job_id)
                .join(GenerationJob, GenerationJob.id == GenerationAttempt.job_id)
                .where(
                    GenerationJob.release_version_id == release_version_id,
                    GenerationAttempt.state.in_(_ACTIVE_GENERATION_ATTEMPT_STATES),
                )
            )
        ).all()
    )
    return tuple(sorted(job_state_ids | active_attempt_job_ids, key=str))


async def _available_asset_count(
    session: AsyncSession,
    *,
    release_id: UUID,
    release_version_id: UUID,
) -> int:
    return int(
        await session.scalar(
            select(func.count(Asset.id))
            .join(GenerationJob, GenerationJob.id == Asset.generation_job_id)
            .where(
                GenerationJob.release_version_id == release_version_id,
                Asset.release_id == release_id,
                Asset.kind == AssetKind.RAW_MASTER,
                Asset.state == AssetState.AVAILABLE,
            )
        )
        or 0
    )
