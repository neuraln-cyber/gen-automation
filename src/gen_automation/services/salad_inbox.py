from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from re import fullmatch
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gen_automation.db.models import (
    AuditEvent,
    GenerationAttempt,
    GenerationJob,
    ProviderBudgetGuard,
    ReleaseVersion,
    WebhookReceipt,
)
from gen_automation.domain.enums import (
    GenerationAttemptState,
    GenerationState,
    InboxStatus,
)
from gen_automation.integrations.salad.models import SaladJobStatus
from gen_automation.services.budgets import release_attempt_reservation
from gen_automation.services.generation_control import GENERATION_STOP_REQUESTED_ACTION
from gen_automation.services.generation_recovery import (
    SALAD_PROVIDER_CANCELLED_ERROR_CODE,
    SALAD_WEBHOOK_JOB_FAILED_ERROR_CODE,
    InfrastructureRetrySource,
    grant_infrastructure_retry,
)
from gen_automation.services.salad import (
    DEPLOYMENT_ROLLOVER_CANCEL_REQUESTED_ERROR_CODE,
    DEPLOYMENT_ROLLOVER_CANCEL_REQUESTED_METADATA_KEY,
    DEPLOYMENT_ROLLOVER_RETRY_ERROR_CODE,
    SALAD_ATTEMPT_WATCHDOG_CANCEL_REQUESTED_ERROR_CODE,
    SALAD_ATTEMPT_WATCHDOG_EXPIRED_ERROR_CODE,
    SALAD_OUTPUT_PROGRESS_WATCHDOG_REASON,
)

_TERMINAL_ATTEMPT_STATES = frozenset(
    {
        GenerationAttemptState.SUCCEEDED,
        GenerationAttemptState.FAILED,
        GenerationAttemptState.CANCELLED,
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
_SALAD_TERMINAL_TARGETS = {
    SaladJobStatus.SUCCEEDED: GenerationAttemptState.SUCCEEDED,
    SaladJobStatus.FAILED: GenerationAttemptState.FAILED,
    SaladJobStatus.CANCELLED: GenerationAttemptState.CANCELLED,
}
_PROVIDER_CLOCK_SKEW_SECONDS = 5 * 60


class SaladInboxError(Exception):
    """Base error for durable Salad webhook processing."""


class SaladInboxValidationError(SaladInboxError):
    pass


class SaladInboxNotFoundError(SaladInboxError):
    pass


class SaladInboxLeaseLostError(SaladInboxError):
    pass


class InboxDisposition(StrEnum):
    APPLIED = "applied"
    NO_CHANGE = "no_change"
    STALE_IGNORED = "stale_ignored"
    RETRY_SCHEDULED = "retry_scheduled"
    DEAD_LETTERED = "dead_lettered"
    TERMINAL_CONFLICT = "terminal_conflict"
    ALREADY_PROCESSED = "already_processed"


@dataclass(frozen=True)
class ClaimedSaladWebhook:
    receipt_id: UUID
    attempt: int
    max_attempts: int
    lease_expires_at: datetime


@dataclass(frozen=True)
class InboxRecoverySummary:
    retried: int
    dead_lettered: int


@dataclass(frozen=True)
class InboxProcessingResult:
    receipt_status: InboxStatus
    disposition: InboxDisposition
    generation_attempt_id: UUID | None
    attempt_state: GenerationAttemptState | None
    job_state: GenerationState | None


def _now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _bounded_stored_observation_time(
    value: datetime,
    *,
    processed_at: datetime,
) -> datetime | None:
    normalized = _as_utc(value)
    try:
        skew_limit = processed_at + timedelta(seconds=_PROVIDER_CLOCK_SKEW_SECONDS)
    except OverflowError:
        skew_limit = processed_at
    if normalized > skew_limit:
        return None
    return min(normalized, processed_at)


def _validate_nonempty(value: str, *, name: str, max_length: int) -> str:
    normalized = value.strip()
    if not normalized:
        raise SaladInboxValidationError(f"{name} must not be empty")
    if len(normalized) > max_length:
        raise SaladInboxValidationError(f"{name} is too long")
    return normalized


def _validate_error_code(value: str) -> str:
    normalized = _validate_nonempty(value, name="error_code", max_length=100)
    if fullmatch(r"[a-z][a-z0-9_.-]*", normalized) is None:
        raise SaladInboxValidationError("error_code has an invalid format")
    return normalized


def _validate_safe_error_detail(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = " ".join(value.split())
    if not normalized:
        return None
    if len(normalized) > 2_000:
        raise SaladInboxValidationError("safe_error_detail is too long")
    return normalized


def _audit(
    session: AsyncSession,
    *,
    receipt: WebhookReceipt,
    actor: str,
    action: str,
    detail: dict[str, object],
    occurred_at: datetime,
) -> None:
    session.add(
        AuditEvent(
            actor=actor,
            action=action,
            resource_type="webhook_receipt",
            resource_id=receipt.id,
            correlation_id=f"salad-webhook-receipt:{receipt.id}",
            detail=detail,
            occurred_at=occurred_at,
        )
    )


def _clear_lease(receipt: WebhookReceipt) -> None:
    receipt.lease_owner = None
    receipt.lease_expires_at = None


def _dead_letter_locked(
    session: AsyncSession,
    *,
    receipt: WebhookReceipt,
    actor: str,
    error_code: str,
    safe_error_detail: str,
    occurred_at: datetime,
    action: str = "salad.webhook.dead_lettered",
) -> None:
    receipt.status = InboxStatus.DEAD_LETTER
    _clear_lease(receipt)
    receipt.processed_at = occurred_at
    receipt.last_error_code = error_code
    receipt.last_error_detail = safe_error_detail
    _audit(
        session,
        receipt=receipt,
        actor=actor,
        action=action,
        detail={"attempt": receipt.attempts, "error_code": error_code},
        occurred_at=occurred_at,
    )


async def _recover_expired_in_transaction(
    session: AsyncSession,
    *,
    now: datetime,
    actor: str,
    limit: int,
) -> InboxRecoverySummary:
    receipts = list(
        (
            await session.scalars(
                select(WebhookReceipt)
                .where(
                    WebhookReceipt.provider == "salad",
                    WebhookReceipt.status == InboxStatus.PROCESSING,
                    WebhookReceipt.lease_expires_at.is_not(None),
                    WebhookReceipt.lease_expires_at <= now,
                )
                .order_by(
                    WebhookReceipt.lease_expires_at,
                    WebhookReceipt.received_at,
                    WebhookReceipt.id,
                )
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        ).all()
    )

    retried = 0
    dead_lettered = 0
    for receipt in receipts:
        if receipt.attempts < receipt.max_attempts:
            receipt.status = InboxStatus.RETRY_WAIT
            receipt.available_at = now
            _clear_lease(receipt)
            receipt.processed_at = None
            receipt.last_error_code = "processing_lease_expired"
            receipt.last_error_detail = (
                "The inbox processing lease expired before its transaction completed."
            )
            _audit(
                session,
                receipt=receipt,
                actor=actor,
                action="salad.webhook.lease_recovered",
                detail={"attempt": receipt.attempts},
                occurred_at=now,
            )
            retried += 1
            continue

        _dead_letter_locked(
            session,
            receipt=receipt,
            actor=actor,
            error_code="processing_attempts_exhausted",
            safe_error_detail="The inbox processing attempt limit was exhausted.",
            occurred_at=now,
        )
        dead_lettered += 1

    return InboxRecoverySummary(retried=retried, dead_lettered=dead_lettered)


async def _dead_letter_exhausted_ready(
    session: AsyncSession,
    *,
    now: datetime,
    actor: str,
    limit: int,
) -> int:
    receipts = list(
        (
            await session.scalars(
                select(WebhookReceipt)
                .where(
                    WebhookReceipt.provider == "salad",
                    WebhookReceipt.status.in_((InboxStatus.RECEIVED, InboxStatus.RETRY_WAIT)),
                    WebhookReceipt.attempts >= WebhookReceipt.max_attempts,
                )
                .order_by(
                    WebhookReceipt.available_at,
                    WebhookReceipt.received_at,
                    WebhookReceipt.id,
                )
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        ).all()
    )
    for receipt in receipts:
        _dead_letter_locked(
            session,
            receipt=receipt,
            actor=actor,
            error_code="processing_attempts_exhausted",
            safe_error_detail="The inbox processing attempt limit was exhausted.",
            occurred_at=now,
        )
    return len(receipts)


async def recover_expired_salad_webhook_receipts(
    session: AsyncSession,
    *,
    limit: int = 100,
    actor: str = "salad-inbox-recovery",
    now: datetime | None = None,
) -> InboxRecoverySummary:
    """Make expired inbox work available again and durably commit the recovery."""

    if limit <= 0 or limit > 1_000:
        raise SaladInboxValidationError("limit must be between 1 and 1000")
    normalized_actor = _validate_nonempty(actor, name="actor", max_length=200)
    recovered_at = _as_utc(now or _now())
    summary = await _recover_expired_in_transaction(
        session,
        now=recovered_at,
        actor=normalized_actor,
        limit=limit,
    )
    exhausted_ready = await _dead_letter_exhausted_ready(
        session,
        now=recovered_at,
        actor=normalized_actor,
        limit=limit,
    )
    await session.commit()
    return InboxRecoverySummary(
        retried=summary.retried,
        dead_lettered=summary.dead_lettered + exhausted_ready,
    )


async def claim_salad_webhook_receipts(
    session: AsyncSession,
    *,
    worker_id: str,
    limit: int,
    lease_seconds: int,
    now: datetime | None = None,
) -> list[ClaimedSaladWebhook]:
    """Claim ready callbacks with PostgreSQL SKIP LOCKED leases.

    The commit happens before results are returned so another worker cannot see
    the claimed rows as ready work.
    """

    normalized_worker_id = _validate_nonempty(
        worker_id,
        name="worker_id",
        max_length=200,
    )
    if limit <= 0 or limit > 1_000:
        raise SaladInboxValidationError("limit must be between 1 and 1000")
    if lease_seconds <= 0 or lease_seconds > 86_400:
        raise SaladInboxValidationError("lease_seconds must be between 1 and 86400")

    claimed_at = _as_utc(now or _now())
    recovery_limit = min(max(limit * 4, 100), 1_000)
    await _recover_expired_in_transaction(
        session,
        now=claimed_at,
        actor=normalized_worker_id,
        limit=recovery_limit,
    )
    await _dead_letter_exhausted_ready(
        session,
        now=claimed_at,
        actor=normalized_worker_id,
        limit=recovery_limit,
    )
    receipts = list(
        (
            await session.scalars(
                select(WebhookReceipt)
                .where(
                    WebhookReceipt.provider == "salad",
                    WebhookReceipt.status.in_((InboxStatus.RECEIVED, InboxStatus.RETRY_WAIT)),
                    WebhookReceipt.available_at <= claimed_at,
                    WebhookReceipt.attempts < WebhookReceipt.max_attempts,
                )
                .order_by(
                    WebhookReceipt.available_at,
                    WebhookReceipt.received_at,
                    WebhookReceipt.id,
                )
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        ).all()
    )

    lease_expires_at = claimed_at + timedelta(seconds=lease_seconds)
    claimed: list[ClaimedSaladWebhook] = []
    for receipt in receipts:
        receipt.status = InboxStatus.PROCESSING
        receipt.attempts += 1
        receipt.lease_owner = normalized_worker_id
        receipt.lease_expires_at = lease_expires_at
        receipt.processed_at = None
        _audit(
            session,
            receipt=receipt,
            actor=normalized_worker_id,
            action="salad.webhook.claimed",
            detail={"attempt": receipt.attempts},
            occurred_at=claimed_at,
        )
        claimed.append(
            ClaimedSaladWebhook(
                receipt_id=receipt.id,
                attempt=receipt.attempts,
                max_attempts=receipt.max_attempts,
                lease_expires_at=lease_expires_at,
            )
        )

    await session.commit()
    return claimed


async def _locked_receipt(
    session: AsyncSession,
    receipt_id: UUID,
) -> WebhookReceipt:
    receipt = await session.scalar(
        select(WebhookReceipt)
        .where(
            WebhookReceipt.id == receipt_id,
            WebhookReceipt.provider == "salad",
        )
        .with_for_update()
    )
    if receipt is None:
        raise SaladInboxNotFoundError("Salad webhook receipt was not found")
    return receipt


def _require_active_lease(
    receipt: WebhookReceipt,
    *,
    worker_id: str,
    now: datetime,
) -> None:
    if receipt.status != InboxStatus.PROCESSING or receipt.lease_owner != worker_id:
        raise SaladInboxLeaseLostError("Salad webhook processing lease is not owned by this worker")
    if receipt.lease_expires_at is None or _as_utc(receipt.lease_expires_at) <= now:
        raise SaladInboxLeaseLostError("Salad webhook processing lease has expired")


def _parse_sanitized_event(
    receipt: WebhookReceipt,
) -> tuple[SaladJobStatus, datetime] | None:
    status_value = receipt.event_metadata.get("job_status")
    update_time_value = receipt.event_metadata.get("job_update_time")
    if not isinstance(status_value, str) or not isinstance(update_time_value, str):
        return None
    try:
        status = SaladJobStatus(status_value)
        parsed_time = datetime.fromisoformat(update_time_value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed_time.tzinfo is None:
        return None
    return status, parsed_time.astimezone(UTC)


async def _matching_attempt(
    session: AsyncSession,
    receipt: WebhookReceipt,
) -> tuple[GenerationAttempt | None, bool]:
    external_id = receipt.provider_external_job_id
    if external_id is None:
        return None, False

    if receipt.generation_attempt_id is not None:
        attempt = await session.scalar(
            select(GenerationAttempt)
            .where(GenerationAttempt.id == receipt.generation_attempt_id)
            .with_for_update()
        )
        if (
            attempt is None
            or attempt.provider != "salad"
            or attempt.provider_external_id != external_id
        ):
            return None, True
        return attempt, False

    attempt = await session.scalar(
        select(GenerationAttempt)
        .where(
            GenerationAttempt.provider == "salad",
            GenerationAttempt.provider_external_id == external_id,
        )
        .with_for_update()
    )
    if attempt is not None:
        receipt.generation_attempt_id = attempt.id
    return attempt, False


def _complete_receipt(
    receipt: WebhookReceipt,
    *,
    processed_at: datetime,
) -> None:
    receipt.status = InboxStatus.SUCCEEDED
    _clear_lease(receipt)
    receipt.processed_at = processed_at
    receipt.last_error_code = None
    receipt.last_error_detail = None


def _result(
    *,
    receipt: WebhookReceipt,
    disposition: InboxDisposition,
    attempt: GenerationAttempt | None = None,
    job: GenerationJob | None = None,
) -> InboxProcessingResult:
    return InboxProcessingResult(
        receipt_status=receipt.status,
        disposition=disposition,
        generation_attempt_id=attempt.id if attempt is not None else None,
        attempt_state=attempt.state if attempt is not None else None,
        job_state=job.state if job is not None else None,
    )


def _schedule_retry_or_dead_letter(
    session: AsyncSession,
    *,
    receipt: WebhookReceipt,
    actor: str,
    error_code: str,
    safe_error_detail: str | None,
    retry_not_before: datetime,
    failed_at: datetime,
    retryable: bool,
) -> InboxDisposition:
    _clear_lease(receipt)
    receipt.last_error_code = error_code
    receipt.last_error_detail = safe_error_detail
    if retryable and receipt.attempts < receipt.max_attempts:
        receipt.status = InboxStatus.RETRY_WAIT
        receipt.available_at = retry_not_before
        receipt.processed_at = None
        _audit(
            session,
            receipt=receipt,
            actor=actor,
            action="salad.webhook.retry_scheduled",
            detail={
                "attempt": receipt.attempts,
                "error_code": error_code,
                "retry_not_before": retry_not_before.isoformat(),
            },
            occurred_at=failed_at,
        )
        return InboxDisposition.RETRY_SCHEDULED

    receipt.status = InboxStatus.DEAD_LETTER
    receipt.processed_at = failed_at
    _audit(
        session,
        receipt=receipt,
        actor=actor,
        action="salad.webhook.dead_lettered",
        detail={"attempt": receipt.attempts, "error_code": error_code},
        occurred_at=failed_at,
    )
    return InboxDisposition.DEAD_LETTERED


async def fail_salad_webhook_receipt(
    session: AsyncSession,
    *,
    receipt_id: UUID,
    worker_id: str,
    error_code: str,
    safe_error_detail: str | None = None,
    retryable: bool = True,
    retry_not_before: datetime | None = None,
    now: datetime | None = None,
) -> InboxProcessingResult:
    """Record a sanitized processing failure under the worker's active lease."""

    normalized_worker_id = _validate_nonempty(
        worker_id,
        name="worker_id",
        max_length=200,
    )
    normalized_error_code = _validate_error_code(error_code)
    normalized_error_detail = _validate_safe_error_detail(safe_error_detail)
    failed_at = _as_utc(now or _now())
    normalized_retry_at = _as_utc(retry_not_before or (failed_at + timedelta(seconds=60)))
    if normalized_retry_at < failed_at:
        raise SaladInboxValidationError("retry_not_before must not be in the past")

    receipt = await _locked_receipt(session, receipt_id)
    if receipt.status in {InboxStatus.SUCCEEDED, InboxStatus.DEAD_LETTER}:
        result = _result(
            receipt=receipt,
            disposition=InboxDisposition.ALREADY_PROCESSED,
        )
        await session.rollback()
        return result
    _require_active_lease(
        receipt,
        worker_id=normalized_worker_id,
        now=failed_at,
    )
    disposition = _schedule_retry_or_dead_letter(
        session,
        receipt=receipt,
        actor=normalized_worker_id,
        error_code=normalized_error_code,
        safe_error_detail=normalized_error_detail,
        retry_not_before=normalized_retry_at,
        failed_at=failed_at,
        retryable=retryable,
    )
    await session.commit()
    return _result(receipt=receipt, disposition=disposition)


def _is_nonterminal_regression(
    attempt: GenerationAttempt,
    incoming_status: SaladJobStatus,
) -> bool:
    if incoming_status != SaladJobStatus.PENDING:
        return False
    return attempt.provider_state == SaladJobStatus.RUNNING.value or attempt.state in {
        GenerationAttemptState.RUNNING,
        GenerationAttemptState.CANCEL_REQUESTED,
    }


def _terminal_status_matches_attempt(
    attempt: GenerationAttempt,
    status: SaladJobStatus,
) -> bool:
    target = _SALAD_TERMINAL_TARGETS[status]
    if attempt.state == target:
        return True
    return (
        status == SaladJobStatus.CANCELLED
        and attempt.state == GenerationAttemptState.FAILED
        and attempt.error_code
        in {
            SALAD_ATTEMPT_WATCHDOG_EXPIRED_ERROR_CODE,
            DEPLOYMENT_ROLLOVER_RETRY_ERROR_CODE,
            SALAD_PROVIDER_CANCELLED_ERROR_CODE,
        }
    )


def _set_job_state(
    job: GenerationJob,
    *,
    state: GenerationState,
    retry_at: datetime | None = None,
    error_code: str | None = None,
    error_detail: str | None = None,
) -> bool:
    changed = (
        job.state != state
        or job.retry_at != retry_at
        or job.last_error_code != error_code
        or job.last_error_detail != error_detail
    )
    if changed:
        job.state = state
        job.retry_at = retry_at
        job.last_error_code = error_code
        job.last_error_detail = error_detail
        job.lock_version += 1
    return changed


async def _release_reservation(
    session: AsyncSession,
    *,
    attempt: GenerationAttempt,
    released_at: datetime,
) -> bool:
    if attempt.cost_reservation_microusd <= 0:
        return False
    result = await release_attempt_reservation(
        session,
        provider="salad",
        attempt_id=attempt.id,
        now=released_at,
    )
    return result.released


async def _apply_provider_state(
    session: AsyncSession,
    *,
    receipt: WebhookReceipt,
    attempt: GenerationAttempt,
    job: GenerationJob,
    status: SaladJobStatus,
    observed_at: datetime,
    processed_at: datetime,
    retry_delay_seconds: int,
    actor: str,
) -> bool:
    previous_attempt_state = attempt.state
    was_unknown = previous_attempt_state == GenerationAttemptState.UNKNOWN
    watchdog_cancel_requested = (
        previous_attempt_state == GenerationAttemptState.CANCEL_REQUESTED
        and attempt.error_code == SALAD_ATTEMPT_WATCHDOG_CANCEL_REQUESTED_ERROR_CODE
    )
    watchdog_reason = (
        (attempt.response_metadata or {}).get("watchdog_reason")
        if watchdog_cancel_requested
        else None
    )
    operator_stop_requested = (
        status == SaladJobStatus.CANCELLED
        and await _operator_generation_stop_requested(session, job=job)
    )
    deployment_rollover_cancel_requested = (
        (
            previous_attempt_state == GenerationAttemptState.CANCEL_REQUESTED
            and attempt.error_code == DEPLOYMENT_ROLLOVER_CANCEL_REQUESTED_ERROR_CODE
        )
        or (
            (attempt.response_metadata or {}).get(DEPLOYMENT_ROLLOVER_CANCEL_REQUESTED_METADATA_KEY)
            is True
        )
    ) and not operator_stop_requested
    spontaneous_provider_cancel = (
        status == SaladJobStatus.CANCELLED
        and not operator_stop_requested
        and not watchdog_cancel_requested
        and not deployment_rollover_cancel_requested
    )
    attempt_changed = attempt.provider_state != status.value or (
        attempt.last_observed_at is None or _as_utc(attempt.last_observed_at) != observed_at
    )
    attempt.provider_state = status.value
    attempt.last_observed_at = observed_at

    if status == SaladJobStatus.PENDING:
        if attempt.state != GenerationAttemptState.CANCEL_REQUESTED:
            attempt_changed = attempt_changed or (attempt.state != GenerationAttemptState.SUBMITTED)
            attempt.state = GenerationAttemptState.SUBMITTED
            attempt.submitted_at = attempt.submitted_at or observed_at
            attempt.unknown_since = None
            attempt.error_code = None
            attempt.error_detail = None
            _set_job_state(job, state=GenerationState.RUNNING)
    elif status == SaladJobStatus.RUNNING:
        if attempt.state != GenerationAttemptState.CANCEL_REQUESTED:
            attempt_changed = attempt_changed or (attempt.state != GenerationAttemptState.RUNNING)
            attempt.state = GenerationAttemptState.RUNNING
            attempt.submitted_at = attempt.submitted_at or observed_at
            attempt.started_at = attempt.started_at or observed_at
            attempt.unknown_since = None
            attempt.error_code = None
            attempt.error_detail = None
            _set_job_state(job, state=GenerationState.RUNNING)
    elif status == SaladJobStatus.SUCCEEDED:
        attempt_changed = attempt_changed or (attempt.state != GenerationAttemptState.UNKNOWN)
        attempt.state = GenerationAttemptState.UNKNOWN
        attempt.submitted_at = attempt.submitted_at or observed_at
        attempt.completed_at = None
        attempt.unknown_since = attempt.unknown_since or processed_at
        attempt.error_code = "salad_worker_output_unverified"
        attempt.error_detail = (
            "Salad reported success; the worker output contract requires provider reconciliation."
        )
        _set_job_state(
            job,
            state=GenerationState.UNKNOWN,
            error_code="salad_worker_output_unverified",
            error_detail=(
                "Salad reported success; the worker output contract "
                "requires provider reconciliation."
            ),
        )
    elif status == SaladJobStatus.FAILED:
        attempt_changed = attempt_changed or (attempt.state != GenerationAttemptState.FAILED)
        attempt.state = GenerationAttemptState.FAILED
        attempt.completed_at = observed_at
        attempt.unknown_since = None
        attempt.error_code = SALAD_WEBHOOK_JOB_FAILED_ERROR_CODE
        attempt.error_detail = "Salad reported a definitive failed job."
        retry_at = processed_at + timedelta(seconds=retry_delay_seconds)
        await grant_infrastructure_retry(
            session,
            attempt=attempt,
            job=job,
            source=InfrastructureRetrySource.WEBHOOK,
            actor=actor,
            retry_at=retry_at,
            occurred_at=processed_at,
        )
        effective_attempt_count = max(job.attempt_count, attempt.attempt_no)
        if effective_attempt_count < job.max_attempts:
            _set_job_state(
                job,
                state=GenerationState.RETRY_WAIT,
                retry_at=retry_at,
                error_code=SALAD_WEBHOOK_JOB_FAILED_ERROR_CODE,
                error_detail="Salad reported a definitive failed job.",
            )
        else:
            _set_job_state(
                job,
                state=GenerationState.DEAD_LETTER,
                error_code="generation_attempts_exhausted",
                error_detail="The generation attempt limit was exhausted.",
            )
        attempt_changed = (
            await _release_reservation(
                session,
                attempt=attempt,
                released_at=processed_at,
            )
            or attempt_changed
        )
    elif (
        watchdog_cancel_requested
        or deployment_rollover_cancel_requested
        or spontaneous_provider_cancel
    ):
        retry_error_code = (
            SALAD_ATTEMPT_WATCHDOG_EXPIRED_ERROR_CODE
            if watchdog_cancel_requested
            else (
                DEPLOYMENT_ROLLOVER_RETRY_ERROR_CODE
                if deployment_rollover_cancel_requested
                else SALAD_PROVIDER_CANCELLED_ERROR_CODE
            )
        )
        retry_error_detail = (
            (
                "The manifest-bound Salad attempt stopped producing accepted output progress; "
                "it was cancelled for retry."
                if watchdog_reason == SALAD_OUTPUT_PROGRESS_WATCHDOG_REASON
                else (
                    "The Salad attempt exceeded its runtime envelope and was cancelled for retry."
                )
            )
            if watchdog_cancel_requested
            else (
                "The Salad deployment was superseded; the cancelled job will retry on the "
                "current deployment."
                if deployment_rollover_cancel_requested
                else (
                    "Salad cancelled the provider job without an operator stop; "
                    "generation will retry."
                )
            )
        )
        attempt_changed = attempt_changed or (attempt.state != GenerationAttemptState.FAILED)
        attempt.state = GenerationAttemptState.FAILED
        attempt.completed_at = observed_at
        attempt.unknown_since = None
        attempt.error_code = retry_error_code
        attempt.error_detail = retry_error_detail
        retry_at = processed_at + timedelta(seconds=retry_delay_seconds)
        await grant_infrastructure_retry(
            session,
            attempt=attempt,
            job=job,
            source=InfrastructureRetrySource.WEBHOOK,
            actor=actor,
            retry_at=retry_at,
            occurred_at=processed_at,
        )
        effective_attempt_count = max(job.attempt_count, attempt.attempt_no)
        if effective_attempt_count < job.max_attempts:
            _set_job_state(
                job,
                state=GenerationState.RETRY_WAIT,
                retry_at=retry_at,
                error_code=retry_error_code,
                error_detail=retry_error_detail,
            )
        else:
            _set_job_state(
                job,
                state=GenerationState.DEAD_LETTER,
                error_code="generation_attempts_exhausted",
                error_detail="The generation attempt limit was exhausted.",
            )
        attempt_changed = (
            await _release_reservation(
                session,
                attempt=attempt,
                released_at=processed_at,
            )
            or attempt_changed
        )
    else:
        attempt_changed = attempt_changed or (attempt.state != GenerationAttemptState.CANCELLED)
        attempt.state = GenerationAttemptState.CANCELLED
        attempt.completed_at = observed_at
        attempt.unknown_since = None
        attempt.error_code = None
        attempt.error_detail = None
        _set_job_state(job, state=GenerationState.CANCELLED)
        attempt_changed = (
            await _release_reservation(
                session,
                attempt=attempt,
                released_at=processed_at,
            )
            or attempt_changed
        )

    if attempt_changed:
        attempt.lock_version += 1
    if was_unknown and attempt.state != GenerationAttemptState.UNKNOWN:
        _audit(
            session,
            receipt=receipt,
            actor=actor,
            action="salad.webhook.reconciled_unknown",
            detail={
                "generation_attempt_id": str(attempt.id),
                "provider_status": status.value,
            },
            occurred_at=processed_at,
        )
    return attempt_changed


async def _operator_generation_stop_requested(
    session: AsyncSession,
    *,
    job: GenerationJob,
) -> bool:
    release_id = await session.scalar(
        select(ReleaseVersion.release_id).where(ReleaseVersion.id == job.release_version_id)
    )
    if release_id is None:
        return False
    marker_id = await session.scalar(
        select(AuditEvent.id)
        .where(
            AuditEvent.resource_type == "release",
            AuditEvent.resource_id == release_id,
            AuditEvent.action == GENERATION_STOP_REQUESTED_ACTION,
        )
        .limit(1)
    )
    return marker_id is not None


async def process_salad_webhook_receipt(
    session: AsyncSession,
    *,
    receipt_id: UUID,
    worker_id: str,
    retry_delay_seconds: int = 60,
    now: datetime | None = None,
) -> InboxProcessingResult:
    """Apply a signed, sanitized Salad callback under an active inbox lease."""

    normalized_worker_id = _validate_nonempty(
        worker_id,
        name="worker_id",
        max_length=200,
    )
    if retry_delay_seconds <= 0 or retry_delay_seconds > 86_400:
        raise SaladInboxValidationError("retry_delay_seconds must be between 1 and 86400")
    processed_at = _as_utc(now or _now())
    receipt = await _locked_receipt(session, receipt_id)
    if receipt.status in {InboxStatus.SUCCEEDED, InboxStatus.DEAD_LETTER}:
        result = _result(
            receipt=receipt,
            disposition=InboxDisposition.ALREADY_PROCESSED,
        )
        await session.rollback()
        return result
    _require_active_lease(
        receipt,
        worker_id=normalized_worker_id,
        now=processed_at,
    )

    event = _parse_sanitized_event(receipt)
    if event is None or receipt.provider_external_job_id is None:
        _dead_letter_locked(
            session,
            receipt=receipt,
            actor=normalized_worker_id,
            error_code="invalid_event_contract",
            safe_error_detail=(
                "The sanitized webhook event did not match the Salad status contract."
            ),
            occurred_at=processed_at,
        )
        await session.commit()
        return _result(
            receipt=receipt,
            disposition=InboxDisposition.DEAD_LETTERED,
        )
    provider_status, observed_at = event
    # The signed provider event remains authoritative for status, but its clock
    # cannot advance controller-owned attempt timestamps or watchdog anchors.
    observed_at = min(observed_at, processed_at)

    if provider_status in _SALAD_TERMINAL_TARGETS:
        # Budget reservation and submission both use guard -> attempt lock
        # ordering. Take the singleton guard before matching/locking the
        # attempt so a terminal callback cannot invert that order.
        await session.scalar(
            select(ProviderBudgetGuard.id)
            .where(ProviderBudgetGuard.provider == "salad")
            .with_for_update()
        )

    attempt, invalid_link = await _matching_attempt(session, receipt)
    if invalid_link:
        _dead_letter_locked(
            session,
            receipt=receipt,
            actor=normalized_worker_id,
            error_code="attempt_link_conflict",
            safe_error_detail=(
                "The webhook receipt link did not match its Salad generation attempt."
            ),
            occurred_at=processed_at,
        )
        await session.commit()
        return _result(
            receipt=receipt,
            disposition=InboxDisposition.DEAD_LETTERED,
        )
    if attempt is None:
        disposition = _schedule_retry_or_dead_letter(
            session,
            receipt=receipt,
            actor=normalized_worker_id,
            error_code="generation_attempt_not_found",
            safe_error_detail=("No matching Salad generation attempt is currently available."),
            retry_not_before=processed_at + timedelta(seconds=retry_delay_seconds),
            failed_at=processed_at,
            retryable=True,
        )
        await session.commit()
        return _result(receipt=receipt, disposition=disposition)

    if receipt.generation_attempt_id == attempt.id:
        _audit(
            session,
            receipt=receipt,
            actor=normalized_worker_id,
            action="salad.webhook.attempt_matched",
            detail={"generation_attempt_id": str(attempt.id)},
            occurred_at=processed_at,
        )

    job = await session.scalar(
        select(GenerationJob).where(GenerationJob.id == attempt.job_id).with_for_update()
    )
    if job is None:
        _dead_letter_locked(
            session,
            receipt=receipt,
            actor=normalized_worker_id,
            error_code="generation_job_not_found",
            safe_error_detail="The matched generation attempt had no generation job.",
            occurred_at=processed_at,
        )
        await session.commit()
        return _result(
            receipt=receipt,
            disposition=InboxDisposition.DEAD_LETTERED,
            attempt=attempt,
        )

    if attempt.attempt_no != job.attempt_count:
        _complete_receipt(receipt, processed_at=processed_at)
        _audit(
            session,
            receipt=receipt,
            actor=normalized_worker_id,
            action="salad.webhook.superseded_attempt_ignored",
            detail={
                "generation_attempt_id": str(attempt.id),
                "attempt_no": attempt.attempt_no,
                "current_attempt_no": job.attempt_count,
                "provider_status": provider_status.value,
            },
            occurred_at=processed_at,
        )
        await session.commit()
        return _result(
            receipt=receipt,
            disposition=InboxDisposition.STALE_IGNORED,
            attempt=attempt,
            job=job,
        )

    last_observed_at = (
        _bounded_stored_observation_time(
            attempt.last_observed_at,
            processed_at=processed_at,
        )
        if attempt.last_observed_at is not None
        else None
    )
    if last_observed_at is not None and observed_at < last_observed_at:
        _complete_receipt(receipt, processed_at=processed_at)
        _audit(
            session,
            receipt=receipt,
            actor=normalized_worker_id,
            action="salad.webhook.stale_ignored",
            detail={"provider_status": provider_status.value},
            occurred_at=processed_at,
        )
        await session.commit()
        return _result(
            receipt=receipt,
            disposition=InboxDisposition.STALE_IGNORED,
            attempt=attempt,
            job=job,
        )

    terminal_target = _SALAD_TERMINAL_TARGETS.get(provider_status)
    if attempt.state in _TERMINAL_ATTEMPT_STATES:
        if terminal_target is None or not _terminal_status_matches_attempt(
            attempt,
            provider_status,
        ):
            _dead_letter_locked(
                session,
                receipt=receipt,
                actor=normalized_worker_id,
                error_code="terminal_state_conflict",
                safe_error_detail=(
                    "The Salad status conflicted with a definitive generation result."
                ),
                occurred_at=processed_at,
                action="salad.webhook.terminal_conflict",
            )
            await session.commit()
            return _result(
                receipt=receipt,
                disposition=InboxDisposition.TERMINAL_CONFLICT,
                attempt=attempt,
                job=job,
            )

        attempt.provider_state = provider_status.value
        attempt.last_observed_at = observed_at
        reservation_released = await _release_reservation(
            session,
            attempt=attempt,
            released_at=processed_at,
        )
        attempt.lock_version += 1
        _complete_receipt(receipt, processed_at=processed_at)
        _audit(
            session,
            receipt=receipt,
            actor=normalized_worker_id,
            action="salad.webhook.terminal_confirmed",
            detail={
                "provider_status": provider_status.value,
                "reservation_released": reservation_released,
            },
            occurred_at=processed_at,
        )
        await session.commit()
        return _result(
            receipt=receipt,
            disposition=InboxDisposition.NO_CHANGE,
            attempt=attempt,
            job=job,
        )

    equal_timestamp_forward_terminal = (
        last_observed_at is not None
        and observed_at == last_observed_at
        and terminal_target is not None
        and attempt.state not in _TERMINAL_ATTEMPT_STATES
    )
    nonterminal_regression = _is_nonterminal_regression(attempt, provider_status)
    if (
        last_observed_at is not None
        and observed_at == last_observed_at
        and attempt.provider_state is not None
        and attempt.provider_state != provider_status.value
        and not equal_timestamp_forward_terminal
        and not nonterminal_regression
    ):
        _dead_letter_locked(
            session,
            receipt=receipt,
            actor=normalized_worker_id,
            error_code="same_version_state_conflict",
            safe_error_detail=("Salad reported conflicting statuses at the same update time."),
            occurred_at=processed_at,
        )
        await session.commit()
        return _result(
            receipt=receipt,
            disposition=InboxDisposition.TERMINAL_CONFLICT,
            attempt=attempt,
            job=job,
        )

    if nonterminal_regression:
        _complete_receipt(receipt, processed_at=processed_at)
        _audit(
            session,
            receipt=receipt,
            actor=normalized_worker_id,
            action="salad.webhook.regression_ignored",
            detail={"provider_status": provider_status.value},
            occurred_at=processed_at,
        )
        await session.commit()
        return _result(
            receipt=receipt,
            disposition=InboxDisposition.STALE_IGNORED,
            attempt=attempt,
            job=job,
        )

    unknown_can_reconcile_dead_letter = (
        attempt.state == GenerationAttemptState.UNKNOWN and job.state == GenerationState.DEAD_LETTER
    )
    if job.state in _TERMINAL_JOB_STATES and not unknown_can_reconcile_dead_letter:
        _dead_letter_locked(
            session,
            receipt=receipt,
            actor=normalized_worker_id,
            error_code="terminal_job_state_conflict",
            safe_error_detail=(
                "The Salad status conflicted with a definitive generation job result."
            ),
            occurred_at=processed_at,
            action="salad.webhook.terminal_conflict",
        )
        await session.commit()
        return _result(
            receipt=receipt,
            disposition=InboxDisposition.TERMINAL_CONFLICT,
            attempt=attempt,
            job=job,
        )

    previous_attempt_state = attempt.state
    previous_job_state = job.state
    changed = await _apply_provider_state(
        session,
        receipt=receipt,
        attempt=attempt,
        job=job,
        status=provider_status,
        observed_at=observed_at,
        processed_at=processed_at,
        retry_delay_seconds=retry_delay_seconds,
        actor=normalized_worker_id,
    )
    _complete_receipt(receipt, processed_at=processed_at)
    _audit(
        session,
        receipt=receipt,
        actor=normalized_worker_id,
        action="salad.webhook.applied",
        detail={
            "provider_status": provider_status.value,
            "previous_attempt_state": previous_attempt_state.value,
            "attempt_state": attempt.state.value,
            "previous_job_state": previous_job_state.value,
            "job_state": job.state.value,
        },
        occurred_at=processed_at,
    )
    await session.commit()
    return _result(
        receipt=receipt,
        disposition=(InboxDisposition.APPLIED if changed else InboxDisposition.NO_CHANGE),
        attempt=attempt,
        job=job,
    )


async def extend_salad_webhook_receipt_lease(
    session: AsyncSession,
    *,
    receipt_id: UUID,
    worker_id: str,
    lease_seconds: int,
    now: datetime | None = None,
) -> datetime:
    normalized_worker_id = _validate_nonempty(
        worker_id,
        name="worker_id",
        max_length=200,
    )
    if lease_seconds <= 0 or lease_seconds > 86_400:
        raise SaladInboxValidationError("lease_seconds must be between 1 and 86400")
    extended_at = _as_utc(now or _now())
    receipt = await _locked_receipt(session, receipt_id)
    _require_active_lease(
        receipt,
        worker_id=normalized_worker_id,
        now=extended_at,
    )
    new_expiry = extended_at + timedelta(seconds=lease_seconds)
    current_expiry = _as_utc(receipt.lease_expires_at)  # type: ignore[arg-type]
    receipt.lease_expires_at = max(current_expiry, new_expiry)
    await session.commit()
    return _as_utc(receipt.lease_expires_at)
