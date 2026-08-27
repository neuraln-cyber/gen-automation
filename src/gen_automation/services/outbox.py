from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from re import fullmatch
from typing import Any
from uuid import UUID

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from gen_automation.db.models import (
    AuditEvent,
    GenerationAttempt,
    GenerationJob,
    OutboxEvent,
    ProviderBudgetGuard,
)
from gen_automation.domain.enums import (
    GenerationAttemptState,
    GenerationState,
    OutboxStatus,
)

SALAD_JOB_SUBMIT_TOPIC = "salad.job.submit"
GENERATION_ATTEMPT_AGGREGATE = "generation_attempt"

_UNKNOWNABLE_ATTEMPT_STATES = frozenset(
    {
        GenerationAttemptState.CREATED,
        GenerationAttemptState.SUBMITTING,
    }
)
_TERMINAL_JOB_STATES = frozenset(
    {
        GenerationState.SUCCEEDED,
        GenerationState.FAILED,
        GenerationState.DEAD_LETTER,
        GenerationState.CANCELLED,
    }
)
_DEFINITELY_UNSTARTED_JOB_STATES = frozenset(
    {
        GenerationState.QUEUED,
        GenerationState.CLAIMED,
        GenerationState.RETRY_WAIT,
    }
)
_NONTERMINAL_ATTEMPT_STATES = frozenset(
    {
        GenerationAttemptState.CREATED,
        GenerationAttemptState.SUBMITTING,
        GenerationAttemptState.SUBMITTED,
        GenerationAttemptState.RUNNING,
        GenerationAttemptState.UNKNOWN,
        GenerationAttemptState.CANCEL_REQUESTED,
    }
)
_DEFINITELY_UNSTARTED_EXHAUSTED_DETAIL = (
    "The Salad submission event exhausted before any provider request could start."
)
_DEFINITELY_UNSTARTED_EXPIRED_DETAIL = (
    "The Salad submission lease expired before any provider request could start."
)
_RUNTIME_ADMISSION_METADATA_VERSION = "v1"


class ExternalEffect(StrEnum):
    """What the worker knows about a failed external operation."""

    DEFINITELY_NOT_STARTED = "definitely_not_started"
    MAY_HAVE_STARTED = "may_have_started"


class OutboxError(Exception):
    pass


class OutboxValidationError(OutboxError):
    pass


class OutboxNotFoundError(OutboxError):
    pass


class OutboxConflictError(OutboxError):
    pass


class OutboxLeaseLostError(OutboxConflictError):
    pass


@dataclass(frozen=True)
class EnqueueResult:
    event_id: UUID
    created: bool


@dataclass(frozen=True)
class ClaimedOutboxEvent:
    id: UUID
    topic: str
    dedupe_key: str
    correlation_id: str
    aggregate_type: str
    aggregate_id: UUID
    payload: dict[str, Any]
    attempt: int
    max_attempts: int
    lease_expires_at: datetime


@dataclass(frozen=True)
class RecoverySummary:
    replayed: int
    dead_lettered: int
    attempts_marked_unknown: int


@dataclass(frozen=True)
class TransitionResult:
    status: OutboxStatus
    changed: bool


def _now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _validate_nonempty(value: str, *, name: str, max_length: int) -> str:
    normalized = value.strip()
    if not normalized:
        raise OutboxValidationError(f"{name} must not be empty")
    if len(normalized) > max_length:
        raise OutboxValidationError(f"{name} is too long")
    return normalized


def _validate_error_code(value: str) -> str:
    normalized = _validate_nonempty(value, name="error_code", max_length=100)
    if fullmatch(r"[a-z][a-z0-9_.-]*", normalized) is None:
        raise OutboxValidationError("error_code has an invalid format")
    return normalized


def _validate_safe_error_detail(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = " ".join(value.split())
    if not normalized:
        return None
    if len(normalized) > 2_000:
        raise OutboxValidationError("safe_error_detail is too long")
    return normalized


def _add_audit(
    session: AsyncSession,
    *,
    event: OutboxEvent,
    actor: str,
    action: str,
    detail: dict[str, Any],
    occurred_at: datetime,
) -> None:
    session.add(
        AuditEvent(
            actor=actor,
            action=action,
            resource_type="outbox_event",
            resource_id=event.id,
            correlation_id=event.correlation_id,
            detail=detail,
            occurred_at=occurred_at,
        )
    )


def _same_logical_event(
    event: OutboxEvent,
    *,
    correlation_id: str,
    aggregate_type: str,
    aggregate_id: UUID,
    payload: dict[str, Any],
    max_attempts: int,
) -> bool:
    return (
        event.correlation_id == correlation_id
        and event.aggregate_type == aggregate_type
        and event.aggregate_id == aggregate_id
        and event.payload == payload
        and event.max_attempts == max_attempts
    )


async def enqueue_outbox_event(
    session: AsyncSession,
    *,
    topic: str,
    dedupe_key: str,
    correlation_id: str,
    aggregate_type: str,
    aggregate_id: UUID,
    payload: Mapping[str, Any],
    max_attempts: int = 10,
    available_at: datetime | None = None,
    actor: str = "controller",
    now: datetime | None = None,
) -> EnqueueResult:
    """Add an event to the caller's transaction, idempotently by topic/dedupe key.

    This function deliberately flushes but does not commit, so a business-state
    mutation and its outbox event can be committed atomically by the caller.
    """

    normalized_topic = _validate_nonempty(topic, name="topic", max_length=200)
    normalized_dedupe_key = _validate_nonempty(
        dedupe_key,
        name="dedupe_key",
        max_length=200,
    )
    normalized_correlation_id = _validate_nonempty(
        correlation_id,
        name="correlation_id",
        max_length=200,
    )
    normalized_aggregate_type = _validate_nonempty(
        aggregate_type,
        name="aggregate_type",
        max_length=100,
    )
    normalized_actor = _validate_nonempty(actor, name="actor", max_length=200)
    if max_attempts <= 0:
        raise OutboxValidationError("max_attempts must be positive")

    created_at = _as_utc(now or _now())
    ready_at = _as_utc(available_at or created_at)
    normalized_payload = dict(payload)
    identity = (
        OutboxEvent.topic == normalized_topic,
        OutboxEvent.dedupe_key == normalized_dedupe_key,
    )
    existing = await session.scalar(select(OutboxEvent).where(*identity))
    if existing is not None:
        if not _same_logical_event(
            existing,
            correlation_id=normalized_correlation_id,
            aggregate_type=normalized_aggregate_type,
            aggregate_id=aggregate_id,
            payload=normalized_payload,
            max_attempts=max_attempts,
        ):
            raise OutboxConflictError(
                "topic and dedupe_key already identify a different outbox event"
            )
        return EnqueueResult(event_id=existing.id, created=False)

    event = OutboxEvent(
        topic=normalized_topic,
        dedupe_key=normalized_dedupe_key,
        correlation_id=normalized_correlation_id,
        aggregate_type=normalized_aggregate_type,
        aggregate_id=aggregate_id,
        payload=normalized_payload,
        status=OutboxStatus.PENDING,
        attempts=0,
        max_attempts=max_attempts,
        available_at=ready_at,
        created_at=created_at,
    )
    try:
        async with session.begin_nested():
            session.add(event)
            await session.flush()
    except IntegrityError:
        existing = await session.scalar(select(OutboxEvent).where(*identity))
        if existing is None:
            raise
        if not _same_logical_event(
            existing,
            correlation_id=normalized_correlation_id,
            aggregate_type=normalized_aggregate_type,
            aggregate_id=aggregate_id,
            payload=normalized_payload,
            max_attempts=max_attempts,
        ):
            raise OutboxConflictError(
                "topic and dedupe_key already identify a different outbox event"
            ) from None
        return EnqueueResult(event_id=existing.id, created=False)

    _add_audit(
        session,
        event=event,
        actor=normalized_actor,
        action="outbox.enqueued",
        detail={
            "topic": event.topic,
            "aggregate_type": event.aggregate_type,
            "aggregate_id": str(event.aggregate_id),
        },
        occurred_at=created_at,
    )
    return EnqueueResult(event_id=event.id, created=True)


async def _mark_salad_attempt_unknown(
    session: AsyncSession,
    *,
    event: OutboxEvent,
    occurred_at: datetime,
    actor: str,
) -> bool:
    if (
        event.topic != SALAD_JOB_SUBMIT_TOPIC
        or event.aggregate_type != GENERATION_ATTEMPT_AGGREGATE
    ):
        return False

    job, attempt = await _lock_salad_job_then_attempt(session, event=event)
    if (
        attempt is None
        or attempt.provider != "salad"
        or attempt.state not in _UNKNOWNABLE_ATTEMPT_STATES
    ):
        return False

    attempt.state = GenerationAttemptState.UNKNOWN
    attempt.unknown_since = attempt.unknown_since or occurred_at
    attempt.error_code = "submit_outcome_unknown"
    attempt.error_detail = "Submission lease expired before a durable provider result."
    attempt.lock_version += 1
    if job is not None and job.state not in _TERMINAL_JOB_STATES:
        job.state = GenerationState.UNKNOWN
        job.last_error_code = "submit_outcome_unknown"
        job.last_error_detail = (
            "The Salad submission outcome is ambiguous; reconciliation is required."
        )
        job.lock_version += 1
    session.add(
        AuditEvent(
            actor=actor,
            action="generation_attempt.submit_outcome_unknown",
            resource_type="generation_attempt",
            resource_id=attempt.id,
            correlation_id=event.correlation_id,
            detail={"outbox_event_id": str(event.id)},
            occurred_at=occurred_at,
        )
    )
    return True


async def _lock_salad_job_then_attempt(
    session: AsyncSession,
    *,
    event: OutboxEvent,
) -> tuple[GenerationJob | None, GenerationAttempt | None]:
    """Serialize Salad aggregate repair in the operator-stop lock order."""

    await session.scalar(
        select(ProviderBudgetGuard.id)
        .where(ProviderBudgetGuard.provider == "salad")
        .with_for_update()
    )
    # The immutable foreign-key lookup is intentionally non-locking. Row locks
    # then follow the canonical job -> attempt order used by operator stop.
    job_id = await session.scalar(
        select(GenerationAttempt.job_id).where(GenerationAttempt.id == event.aggregate_id)
    )
    if job_id is None:
        return None, None
    job = await session.scalar(
        select(GenerationJob).where(GenerationJob.id == job_id).with_for_update()
    )
    attempt = await session.scalar(
        select(GenerationAttempt)
        .where(GenerationAttempt.id == event.aggregate_id)
        .with_for_update()
    )
    if attempt is not None and attempt.job_id != job_id:
        return None, attempt
    return job, attempt


async def _fail_exhausted_salad_attempt_before_submission(
    session: AsyncSession,
    *,
    event: OutboxEvent,
    occurred_at: datetime,
    actor: str,
    error_code: str,
    safe_error_detail: str | None,
    audit_action: str = "generation_attempt.outbox_exhausted_before_submission",
) -> bool:
    """Repair only a Salad aggregate that is provably still unsubmitted."""

    if (
        event.topic != SALAD_JOB_SUBMIT_TOPIC
        or event.aggregate_type != GENERATION_ATTEMPT_AGGREGATE
    ):
        return False

    job, attempt = await _lock_salad_job_then_attempt(session, event=event)
    if (
        attempt is None
        or attempt.provider != "salad"
        or attempt.state
        not in {
            GenerationAttemptState.CREATED,
            GenerationAttemptState.SUBMITTING,
        }
        or attempt.provider_external_id is not None
        or attempt.submit_started_at is not None
        or attempt.submitted_at is not None
        or attempt.started_at is not None
        or attempt.last_observed_at is not None
        or attempt.unknown_since is not None
        or attempt.provider_state is not None
        or attempt.response_metadata is not None
        or attempt.cost_reservation_microusd != 0
        or attempt.reservation_released_at is not None
    ):
        return False

    if job is None:
        return False
    if attempt.state == GenerationAttemptState.SUBMITTING and job.state not in _TERMINAL_JOB_STATES:
        # Markerless SUBMITTING is an impossible modern state. It is safe to
        # repair only when the owning lifecycle is already terminal; otherwise
        # preserve the ambiguous-state reconciliation path.
        return False
    other_active_attempt_id = await session.scalar(
        select(GenerationAttempt.id)
        .where(
            GenerationAttempt.job_id == attempt.job_id,
            GenerationAttempt.id != attempt.id,
            GenerationAttempt.state.in_(_NONTERMINAL_ATTEMPT_STATES),
        )
        .limit(1)
    )
    owns_current_local_attempt = (
        attempt.state == GenerationAttemptState.CREATED
        and job.attempt_count == attempt.attempt_no
        and job.state in _DEFINITELY_UNSTARTED_JOB_STATES
        and other_active_attempt_id is None
    )
    if (
        job.state not in _TERMINAL_JOB_STATES
        and job.attempt_count == attempt.attempt_no
        and not owns_current_local_attempt
    ):
        # A current CREATED row paired with a remote/ambiguous job state is not
        # enough proof to release capacity. Leave it for explicit reconciliation.
        return False

    detail = safe_error_detail or _DEFINITELY_UNSTARTED_EXHAUSTED_DETAIL
    attempt.state = GenerationAttemptState.FAILED
    attempt.completed_at = occurred_at
    attempt.error_code = error_code
    attempt.error_detail = detail
    attempt.lock_version += 1

    if owns_current_local_attempt:
        retryable = job.attempt_count < job.max_attempts
        job.state = GenerationState.RETRY_WAIT if retryable else GenerationState.DEAD_LETTER
        job.retry_at = occurred_at if retryable else None
        job.lease_owner = None
        job.lease_expires_at = None
        job.last_error_code = error_code
        job.last_error_detail = detail
        job.lock_version += 1

    session.add(
        AuditEvent(
            actor=actor,
            action=audit_action,
            resource_type="generation_attempt",
            resource_id=attempt.id,
            correlation_id=event.correlation_id,
            detail={
                "outbox_event_id": str(event.id),
                "error_code": error_code,
                "provider_contacted": False,
            },
            occurred_at=occurred_at,
        )
    )
    return True


async def _recover_expired_in_transaction(
    session: AsyncSession,
    *,
    now: datetime,
    definitely_safe_to_replay_topics: Collection[str],
    limit: int,
    actor: str,
) -> RecoverySummary:
    safe_topics = set(definitely_safe_to_replay_topics)
    safe_topics.discard(SALAD_JOB_SUBMIT_TOPIC)
    events = list(
        (
            await session.scalars(
                select(OutboxEvent)
                .where(
                    OutboxEvent.status == OutboxStatus.PROCESSING,
                    OutboxEvent.lease_expires_at.is_not(None),
                    OutboxEvent.lease_expires_at <= now,
                )
                .order_by(OutboxEvent.lease_expires_at, OutboxEvent.created_at, OutboxEvent.id)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        ).all()
    )

    replayed = 0
    dead_lettered = 0
    attempts_marked_unknown = 0
    for event in events:
        runtime_refresh_replay = await _runtime_refresh_replay_is_safe(session, event)
        can_replay = event.attempts < event.max_attempts and (
            event.topic in safe_topics or runtime_refresh_replay
        )
        if can_replay:
            event.status = OutboxStatus.PENDING
            event.available_at = now
            event.lease_owner = None
            event.lease_expires_at = None
            event.processed_at = None
            event.last_error_code = "lease_expired_before_effect"
            event.last_error_detail = (
                "The handler established that the external effect did not begin."
            )
            replayed += 1
            _add_audit(
                session,
                event=event,
                actor=actor,
                action=(
                    "outbox.runtime_refresh_lease_recovered"
                    if runtime_refresh_replay
                    else "outbox.lease_recovered"
                ),
                detail=(
                    {
                        "attempt": event.attempts,
                        "runtime_refresh_replay": True,
                    }
                    if runtime_refresh_replay
                    else {"attempt": event.attempts}
                ),
                occurred_at=now,
            )
            continue

        event.status = OutboxStatus.DEAD_LETTER
        event.lease_owner = None
        event.lease_expires_at = None
        event.processed_at = now
        marked_failed = await _fail_exhausted_salad_attempt_before_submission(
            session,
            event=event,
            occurred_at=now,
            actor=actor,
            error_code="outbox_lease_expired_before_submission",
            safe_error_detail=_DEFINITELY_UNSTARTED_EXPIRED_DETAIL,
            audit_action="generation_attempt.outbox_lease_expired_before_submission",
        )
        if marked_failed:
            event.last_error_code = "lease_expired_before_effect"
            event.last_error_detail = _DEFINITELY_UNSTARTED_EXPIRED_DETAIL
            reason = "lease_expired_before_effect"
            marked_unknown = False
        else:
            event.last_error_code = "ambiguous_external_effect"
            event.last_error_detail = (
                "The processing lease expired and replay could duplicate an external effect."
            )
            reason = "ambiguous_external_effect"
            marked_unknown = await _mark_salad_attempt_unknown(
                session,
                event=event,
                occurred_at=now,
                actor=actor,
            )
        attempts_marked_unknown += int(marked_unknown)
        dead_lettered += 1
        _add_audit(
            session,
            event=event,
            actor=actor,
            action="outbox.dead_lettered",
            detail={
                "attempt": event.attempts,
                "reason": reason,
                "attempt_marked_unknown": marked_unknown,
                "attempt_marked_failed": marked_failed,
            },
            occurred_at=now,
        )

    return RecoverySummary(
        replayed=replayed,
        dead_lettered=dead_lettered,
        attempts_marked_unknown=attempts_marked_unknown,
    )


async def _runtime_refresh_replay_is_safe(
    session: AsyncSession,
    event: OutboxEvent,
) -> bool:
    """Prove an expired Salad claim stopped inside an idempotent runtime rollout.

    The controller persists a version-fenced refresh plan before its first
    deployment PATCH and replaces that plan with an exact admission target
    before any queue POST. A CREATED attempt with no budget/submission evidence
    can therefore replay either phase without duplicating a provider job. All
    older or malformed submit events retain the conservative dead-letter path.
    """

    if (
        event.topic != SALAD_JOB_SUBMIT_TOPIC
        or event.aggregate_type != GENERATION_ATTEMPT_AGGREGATE
    ):
        return False
    job, attempt = await _lock_salad_job_then_attempt(session, event=event)
    if job is None or attempt is None or attempt.provider != "salad":
        return False
    if (
        attempt.state != GenerationAttemptState.CREATED
        or job.state != GenerationState.CLAIMED
        or job.attempt_count != attempt.attempt_no
        or attempt.provider_external_id is not None
        or attempt.submit_started_at is not None
        or attempt.submitted_at is not None
        or attempt.started_at is not None
        or attempt.last_observed_at is not None
        or attempt.unknown_since is not None
        or attempt.provider_state is not None
        or attempt.response_metadata is not None
        or attempt.cost_reservation_microusd != 0
        or attempt.reservation_released_at is not None
    ):
        return False
    metadata = attempt.request_metadata
    return valid_runtime_refresh_plan(metadata.get("runtime_admission_refresh_plan")) or (
        valid_runtime_admission_target(metadata.get("runtime_admission"))
    )


def valid_runtime_refresh_plan(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "version",
        "provider_group_version_before_refresh",
        "artifact_manifest_sha256",
        "rollout_id",
    }:
        return False
    return bool(
        value.get("version") == _RUNTIME_ADMISSION_METADATA_VERSION
        and _positive_int(value.get("provider_group_version_before_refresh"))
        and _lower_hex(value.get("artifact_manifest_sha256"), length=64)
        and _lower_hex(value.get("rollout_id"), length=32)
    )


def valid_runtime_admission_target(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "version",
        "provider_group_version",
        "artifact_manifest_sha256",
        "rollout_id",
        "worker_instance_id",
    }:
        return False
    return bool(
        value.get("version") == _RUNTIME_ADMISSION_METADATA_VERSION
        and _positive_int(value.get("provider_group_version"))
        and _lower_hex(value.get("artifact_manifest_sha256"), length=64)
        and _lower_hex(value.get("rollout_id"), length=32)
        and _optional_runtime_worker_instance_id(value.get("worker_instance_id"))
    )


def _positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _lower_hex(value: object, *, length: int) -> bool:
    return bool(
        isinstance(value, str)
        and len(value) == length
        and value == value.lower()
        and all(character in "0123456789abcdef" for character in value)
    )


def _optional_runtime_worker_instance_id(value: object) -> bool:
    return value is None or bool(
        isinstance(value, str)
        and 1 <= len(value) <= 128
        and all(
            character in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:-"
            for character in value
        )
    )


async def recover_expired_outbox_events(
    session: AsyncSession,
    *,
    definitely_safe_to_replay_topics: Collection[str] = (),
    limit: int = 100,
    actor: str = "outbox-recovery",
    now: datetime | None = None,
) -> RecoverySummary:
    """Recover expired leases without assuming an external call was harmless.

    Topics are replayed only when the caller explicitly supplies them as proven
    replay-safe. Salad job submission is replayed only for the narrower durable
    runtime-plan/target contract proved by ``_runtime_refresh_replay_is_safe``.
    """

    if limit <= 0 or limit > 1_000:
        raise OutboxValidationError("limit must be between 1 and 1000")
    normalized_actor = _validate_nonempty(actor, name="actor", max_length=200)
    occurred_at = _as_utc(now or _now())
    summary = await _recover_expired_in_transaction(
        session,
        now=occurred_at,
        definitely_safe_to_replay_topics=definitely_safe_to_replay_topics,
        limit=limit,
        actor=normalized_actor,
    )
    await session.commit()
    return summary


async def _dead_letter_exhausted_pending(
    session: AsyncSession,
    *,
    now: datetime,
    limit: int,
    actor: str,
) -> None:
    exhausted = list(
        (
            await session.scalars(
                select(OutboxEvent)
                .where(
                    OutboxEvent.status == OutboxStatus.PENDING,
                    OutboxEvent.attempts >= OutboxEvent.max_attempts,
                )
                .order_by(OutboxEvent.created_at, OutboxEvent.id)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        ).all()
    )
    for event in exhausted:
        event.status = OutboxStatus.DEAD_LETTER
        event.processed_at = now
        event.last_error_code = "attempt_limit_exhausted"
        event.last_error_detail = "The outbox event exhausted its processing attempt limit."
        attempt_marked_failed = await _fail_exhausted_salad_attempt_before_submission(
            session,
            event=event,
            occurred_at=now,
            actor=actor,
            error_code="outbox_attempt_limit_exhausted",
            safe_error_detail=None,
        )
        _add_audit(
            session,
            event=event,
            actor=actor,
            action="outbox.dead_lettered",
            detail={
                "attempt": event.attempts,
                "reason": "attempt_limit_exhausted",
                "attempt_marked_failed": attempt_marked_failed,
            },
            occurred_at=now,
        )


async def _repair_dead_lettered_unstarted_salad_attempts(
    session: AsyncSession,
    *,
    now: datetime,
    limit: int,
    actor: str,
) -> None:
    """Repair legacy dead letters whose aggregate proves no provider effect began."""

    other_attempt = aliased(GenerationAttempt)
    events = list(
        (
            await session.scalars(
                select(OutboxEvent)
                .join(
                    GenerationAttempt,
                    GenerationAttempt.id == OutboxEvent.aggregate_id,
                )
                .join(GenerationJob, GenerationJob.id == GenerationAttempt.job_id)
                .where(
                    OutboxEvent.status == OutboxStatus.DEAD_LETTER,
                    OutboxEvent.topic == SALAD_JOB_SUBMIT_TOPIC,
                    OutboxEvent.aggregate_type == GENERATION_ATTEMPT_AGGREGATE,
                    GenerationAttempt.provider == "salad",
                    GenerationAttempt.state.in_(
                        {
                            GenerationAttemptState.CREATED,
                            GenerationAttemptState.SUBMITTING,
                        }
                    ),
                    GenerationAttempt.provider_external_id.is_(None),
                    GenerationAttempt.submit_started_at.is_(None),
                    GenerationAttempt.submitted_at.is_(None),
                    GenerationAttempt.started_at.is_(None),
                    GenerationAttempt.last_observed_at.is_(None),
                    GenerationAttempt.unknown_since.is_(None),
                    GenerationAttempt.provider_state.is_(None),
                    GenerationAttempt.response_metadata.is_(None),
                    GenerationAttempt.cost_reservation_microusd == 0,
                    GenerationAttempt.reservation_released_at.is_(None),
                    or_(
                        and_(
                            GenerationAttempt.state == GenerationAttemptState.SUBMITTING,
                            GenerationJob.state.in_(_TERMINAL_JOB_STATES),
                        ),
                        and_(
                            GenerationAttempt.state == GenerationAttemptState.CREATED,
                            or_(
                                GenerationJob.state.in_(_TERMINAL_JOB_STATES),
                                GenerationJob.attempt_count != GenerationAttempt.attempt_no,
                                and_(
                                    GenerationJob.state.in_(_DEFINITELY_UNSTARTED_JOB_STATES),
                                    ~exists(
                                        select(other_attempt.id).where(
                                            other_attempt.job_id == GenerationAttempt.job_id,
                                            other_attempt.id != GenerationAttempt.id,
                                            other_attempt.state.in_(_NONTERMINAL_ATTEMPT_STATES),
                                        )
                                    ),
                                ),
                            ),
                        ),
                    ),
                )
                .order_by(OutboxEvent.processed_at, OutboxEvent.created_at, OutboxEvent.id)
                .limit(limit)
                .with_for_update(of=OutboxEvent, skip_locked=True)
            )
        ).all()
    )
    for event in events:
        await _fail_exhausted_salad_attempt_before_submission(
            session,
            event=event,
            occurred_at=now,
            actor=actor,
            error_code="outbox_dead_lettered_before_submission",
            safe_error_detail=None,
        )


async def claim_outbox_events(
    session: AsyncSession,
    *,
    worker_id: str,
    limit: int,
    lease_seconds: int,
    topics: Collection[str] | None = None,
    definitely_safe_to_replay_topics: Collection[str] = (),
    now: datetime | None = None,
) -> list[ClaimedOutboxEvent]:
    """Lease ready events and durably commit the claims before returning."""

    normalized_worker_id = _validate_nonempty(worker_id, name="worker_id", max_length=200)
    if limit <= 0 or limit > 1_000:
        raise OutboxValidationError("limit must be between 1 and 1000")
    if lease_seconds <= 0 or lease_seconds > 86_400:
        raise OutboxValidationError("lease_seconds must be between 1 and 86400")
    normalized_topics = (
        {_validate_nonempty(topic, name="topic", max_length=200) for topic in topics}
        if topics is not None
        else None
    )
    if normalized_topics == set():
        return []

    claimed_at = _as_utc(now or _now())
    await _recover_expired_in_transaction(
        session,
        now=claimed_at,
        definitely_safe_to_replay_topics=definitely_safe_to_replay_topics,
        limit=max(limit * 4, 100),
        actor=normalized_worker_id,
    )
    await _dead_letter_exhausted_pending(
        session,
        now=claimed_at,
        limit=max(limit * 4, 100),
        actor=normalized_worker_id,
    )
    if normalized_topics is None or SALAD_JOB_SUBMIT_TOPIC in normalized_topics:
        await _repair_dead_lettered_unstarted_salad_attempts(
            session,
            now=claimed_at,
            limit=max(limit * 4, 100),
            actor=normalized_worker_id,
        )

    query = (
        select(OutboxEvent)
        .where(
            OutboxEvent.status == OutboxStatus.PENDING,
            OutboxEvent.available_at <= claimed_at,
            OutboxEvent.attempts < OutboxEvent.max_attempts,
        )
        .order_by(OutboxEvent.available_at, OutboxEvent.created_at, OutboxEvent.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    if normalized_topics is not None:
        query = query.where(OutboxEvent.topic.in_(normalized_topics))
    events = list((await session.scalars(query)).all())

    lease_expires_at = claimed_at + timedelta(seconds=lease_seconds)
    claimed: list[ClaimedOutboxEvent] = []
    for event in events:
        event.status = OutboxStatus.PROCESSING
        event.attempts += 1
        event.lease_owner = normalized_worker_id
        event.lease_expires_at = lease_expires_at
        event.processed_at = None
        _add_audit(
            session,
            event=event,
            actor=normalized_worker_id,
            action="outbox.claimed",
            detail={"attempt": event.attempts},
            occurred_at=claimed_at,
        )
        claimed.append(
            ClaimedOutboxEvent(
                id=event.id,
                topic=event.topic,
                dedupe_key=event.dedupe_key,
                correlation_id=event.correlation_id,
                aggregate_type=event.aggregate_type,
                aggregate_id=event.aggregate_id,
                payload=dict(event.payload),
                attempt=event.attempts,
                max_attempts=event.max_attempts,
                lease_expires_at=lease_expires_at,
            )
        )

    await session.commit()
    return claimed


async def _locked_event(session: AsyncSession, event_id: UUID) -> OutboxEvent:
    event = await session.scalar(
        select(OutboxEvent).where(OutboxEvent.id == event_id).with_for_update()
    )
    if event is None:
        raise OutboxNotFoundError("outbox event not found")
    return event


def _require_active_lease(
    event: OutboxEvent,
    *,
    worker_id: str,
    now: datetime,
) -> None:
    if event.status != OutboxStatus.PROCESSING or event.lease_owner != worker_id:
        raise OutboxLeaseLostError("outbox processing lease is not owned by this worker")
    if event.lease_expires_at is None or _as_utc(event.lease_expires_at) <= now:
        raise OutboxLeaseLostError("outbox processing lease has expired")


async def succeed_outbox_event(
    session: AsyncSession,
    *,
    event_id: UUID,
    worker_id: str,
    now: datetime | None = None,
) -> TransitionResult:
    normalized_worker_id = _validate_nonempty(worker_id, name="worker_id", max_length=200)
    completed_at = _as_utc(now or _now())
    event = await _locked_event(session, event_id)
    if event.status == OutboxStatus.SUCCEEDED:
        await session.rollback()
        return TransitionResult(status=OutboxStatus.SUCCEEDED, changed=False)
    _require_active_lease(event, worker_id=normalized_worker_id, now=completed_at)

    event.status = OutboxStatus.SUCCEEDED
    event.lease_owner = None
    event.lease_expires_at = None
    event.processed_at = completed_at
    event.last_error_code = None
    event.last_error_detail = None
    _add_audit(
        session,
        event=event,
        actor=normalized_worker_id,
        action="outbox.succeeded",
        detail={"attempt": event.attempts},
        occurred_at=completed_at,
    )
    await session.commit()
    return TransitionResult(status=OutboxStatus.SUCCEEDED, changed=True)


async def defer_unstarted_outbox_event(
    session: AsyncSession,
    *,
    event_id: UUID,
    worker_id: str,
    retry_not_before: datetime,
    reason_code: str,
    now: datetime | None = None,
) -> TransitionResult:
    """Release a lease without consuming an attempt before any effect can start.

    This is intentionally narrower than a retryable failure. Salad submission
    uses it while a controller precondition (such as an artifact boundary or
    provider budget) prevents the request from starting. The prepared attempt
    must remain in CREATED. The claim increment is returned so waiting on that
    precondition cannot exhaust the outbox.
    """

    normalized_worker_id = _validate_nonempty(worker_id, name="worker_id", max_length=200)
    normalized_reason = _validate_error_code(reason_code)
    deferred_at = _as_utc(now or _now())
    normalized_retry_at = _as_utc(retry_not_before)
    if normalized_retry_at <= deferred_at:
        raise OutboxValidationError("retry_not_before must be in the future")

    event = await _locked_event(session, event_id)
    _require_active_lease(event, worker_id=normalized_worker_id, now=deferred_at)
    if (
        event.topic != SALAD_JOB_SUBMIT_TOPIC
        or event.aggregate_type != GENERATION_ATTEMPT_AGGREGATE
    ):
        raise OutboxConflictError("only a prepared Salad submission can be deferred")
    attempt = await session.scalar(
        select(GenerationAttempt)
        .where(GenerationAttempt.id == event.aggregate_id)
        .with_for_update()
    )
    if attempt is None or attempt.state != GenerationAttemptState.CREATED:
        raise OutboxConflictError("Salad submission is no longer definitely unstarted")
    if event.attempts <= 0:
        raise OutboxConflictError("outbox claim attempt is invalid")

    claimed_attempt = event.attempts
    event.status = OutboxStatus.PENDING
    event.attempts -= 1
    event.available_at = normalized_retry_at
    event.lease_owner = None
    event.lease_expires_at = None
    event.processed_at = None
    event.last_error_code = normalized_reason
    event.last_error_detail = "Submission is waiting for a controller precondition."
    _add_audit(
        session,
        event=event,
        actor=normalized_worker_id,
        action="outbox.deferred_before_effect",
        detail={
            "claimed_attempt": claimed_attempt,
            "reason_code": normalized_reason,
            "retry_not_before": normalized_retry_at.isoformat(),
        },
        occurred_at=deferred_at,
    )
    await session.commit()
    return TransitionResult(status=OutboxStatus.PENDING, changed=True)


async def defer_staged_runtime_admission_outbox_event(
    session: AsyncSession,
    *,
    event_id: UUID,
    worker_id: str,
    retry_not_before: datetime,
    reason_code: str,
    now: datetime | None = None,
) -> TransitionResult | None:
    """Re-defer an interrupted admission phase only with exact no-POST proof."""

    normalized_worker_id = _validate_nonempty(worker_id, name="worker_id", max_length=200)
    observed_at = _as_utc(now or _now())
    event = await _locked_event(session, event_id)
    _require_active_lease(event, worker_id=normalized_worker_id, now=observed_at)
    if not await _runtime_refresh_replay_is_safe(session, event):
        await session.rollback()
        return None
    return await defer_unstarted_outbox_event(
        session,
        event_id=event_id,
        worker_id=normalized_worker_id,
        retry_not_before=retry_not_before,
        reason_code=reason_code,
        now=observed_at,
    )


async def fail_outbox_event(
    session: AsyncSession,
    *,
    event_id: UUID,
    worker_id: str,
    error_code: str,
    safe_error_detail: str | None,
    external_effect: ExternalEffect,
    retry_not_before: datetime | None,
    now: datetime | None = None,
) -> TransitionResult:
    """Fail a leased event, retrying only a definitely unstarted external effect."""

    normalized_worker_id = _validate_nonempty(worker_id, name="worker_id", max_length=200)
    normalized_error_code = _validate_error_code(error_code)
    normalized_error_detail = _validate_safe_error_detail(safe_error_detail)
    failed_at = _as_utc(now or _now())
    normalized_retry_at = _as_utc(retry_not_before) if retry_not_before is not None else None
    if normalized_retry_at is not None and normalized_retry_at < failed_at:
        raise OutboxValidationError("retry_not_before must not be in the past")

    event = await _locked_event(session, event_id)
    if event.status == OutboxStatus.DEAD_LETTER:
        await session.rollback()
        return TransitionResult(status=OutboxStatus.DEAD_LETTER, changed=False)
    _require_active_lease(event, worker_id=normalized_worker_id, now=failed_at)

    event.lease_owner = None
    event.lease_expires_at = None
    event.last_error_code = normalized_error_code
    event.last_error_detail = normalized_error_detail
    if (
        normalized_retry_at is not None
        and external_effect == ExternalEffect.DEFINITELY_NOT_STARTED
        and event.attempts < event.max_attempts
    ):
        event.status = OutboxStatus.PENDING
        event.available_at = normalized_retry_at
        event.processed_at = None
        _add_audit(
            session,
            event=event,
            actor=normalized_worker_id,
            action="outbox.retry_scheduled",
            detail={
                "attempt": event.attempts,
                "error_code": normalized_error_code,
                "retry_not_before": normalized_retry_at.isoformat(),
            },
            occurred_at=failed_at,
        )
        await session.commit()
        return TransitionResult(status=OutboxStatus.PENDING, changed=True)

    event.status = OutboxStatus.DEAD_LETTER
    event.processed_at = failed_at
    marked_unknown = False
    marked_failed = False
    if external_effect == ExternalEffect.MAY_HAVE_STARTED:
        marked_unknown = await _mark_salad_attempt_unknown(
            session,
            event=event,
            occurred_at=failed_at,
            actor=normalized_worker_id,
        )
    else:
        marked_failed = await _fail_exhausted_salad_attempt_before_submission(
            session,
            event=event,
            occurred_at=failed_at,
            actor=normalized_worker_id,
            error_code=normalized_error_code,
            safe_error_detail=normalized_error_detail,
        )
    _add_audit(
        session,
        event=event,
        actor=normalized_worker_id,
        action="outbox.dead_lettered",
        detail={
            "attempt": event.attempts,
            "error_code": normalized_error_code,
            "external_effect": external_effect.value,
            "attempt_marked_unknown": marked_unknown,
            "attempt_marked_failed": marked_failed,
        },
        occurred_at=failed_at,
    )
    await session.commit()
    return TransitionResult(status=OutboxStatus.DEAD_LETTER, changed=True)


async def extend_outbox_lease(
    session: AsyncSession,
    *,
    event_id: UUID,
    worker_id: str,
    lease_seconds: int,
    now: datetime | None = None,
) -> datetime:
    normalized_worker_id = _validate_nonempty(worker_id, name="worker_id", max_length=200)
    if lease_seconds <= 0 or lease_seconds > 86_400:
        raise OutboxValidationError("lease_seconds must be between 1 and 86400")
    extended_at = _as_utc(now or _now())
    event = await _locked_event(session, event_id)
    _require_active_lease(event, worker_id=normalized_worker_id, now=extended_at)

    new_expiry = extended_at + timedelta(seconds=lease_seconds)
    current_expiry = _as_utc(event.lease_expires_at)  # type: ignore[arg-type]
    event.lease_expires_at = max(current_expiry, new_expiry)
    await session.commit()
    return _as_utc(event.lease_expires_at)
