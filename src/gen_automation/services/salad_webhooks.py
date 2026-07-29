from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from gen_automation.db.models import AuditEvent, GenerationAttempt, WebhookReceipt
from gen_automation.domain.canonical import canonical_sha256
from gen_automation.domain.enums import InboxStatus
from gen_automation.domain.ids import uuid7
from gen_automation.integrations.salad.models import (
    SaladQueueJob,
    parse_queue_job,
)
from gen_automation.integrations.salad.webhooks import VerifiedSaladWebhook


class SaladWebhookReceiptError(Exception):
    """Base error for a verified callback that cannot be recorded."""


class SaladWebhookPayloadContractError(SaladWebhookReceiptError):
    pass


class SaladWebhookReplayConflictError(SaladWebhookReceiptError):
    pass


@dataclass(frozen=True)
class RecordedWebhook:
    receipt_id: UUID
    replayed: bool


def _signature_datetime(timestamp: int) -> datetime:
    try:
        return datetime.fromtimestamp(timestamp, tz=UTC)
    except (OverflowError, OSError, ValueError):
        raise SaladWebhookPayloadContractError("invalid webhook timestamp") from None


def _parse_job(callback: VerifiedSaladWebhook) -> SaladQueueJob:
    try:
        return parse_queue_job(callback.payload)
    except ValueError:
        # Never echo parser details because the callback can contain prompts,
        # signed upload grants, or worker output.
        raise SaladWebhookPayloadContractError(
            "webhook payload does not match the Salad queue-job contract"
        ) from None


def _metadata_uuid(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return str(UUID(value))
    except ValueError:
        return None


def _receipt_metadata(job: SaladQueueJob) -> dict[str, object]:
    """Return the deliberately small, non-content subset retained in the inbox."""
    internal_attempt_id = job.metadata.get("generation_attempt_id")
    internal_job_id = job.metadata.get("generation_job_id")
    return {
        "job_status": job.status.value,
        "job_update_time": job.update_time.isoformat(),
        "event_count": len(job.events),
        "generation_attempt_id": _metadata_uuid(internal_attempt_id),
        "generation_job_id": _metadata_uuid(internal_job_id),
    }


async def _matching_attempt_id(
    session: AsyncSession,
    *,
    provider_external_job_id: str,
) -> UUID | None:
    return await session.scalar(
        select(GenerationAttempt.id).where(
            GenerationAttempt.provider == "salad",
            GenerationAttempt.provider_external_id == provider_external_job_id,
        )
    )


async def _record_replay_conflict_audit(
    session: AsyncSession,
    *,
    job_id: UUID,
    webhook_id: str,
) -> None:
    session.add(
        AuditEvent(
            id=uuid7(),
            actor="salad-webhook",
            action="salad.webhook.replay_conflict",
            resource_type="salad_queue_job",
            resource_id=job_id,
            correlation_id=webhook_id,
            detail={"provider": "salad", "reason": "event_id_payload_mismatch"},
            occurred_at=datetime.now(UTC),
        )
    )
    await session.commit()


async def record_verified_salad_webhook(
    session: AsyncSession,
    callback: VerifiedSaladWebhook,
    *,
    received_at: datetime | None = None,
) -> RecordedWebhook:
    """Durably record a verified callback before any state-machine processing.

    Raw callback bytes, job input, job output, and arbitrary provider metadata
    are intentionally not persisted in the inbox.
    """
    job = _parse_job(callback)
    payload_sha256 = canonical_sha256(callback.payload)
    now = received_at or datetime.now(UTC)

    existing = await session.scalar(
        select(WebhookReceipt).where(
            WebhookReceipt.provider == "salad",
            WebhookReceipt.event_id == callback.webhook_id,
        )
    )
    if existing is not None:
        if existing.payload_sha256 == payload_sha256:
            return RecordedWebhook(receipt_id=existing.id, replayed=True)
        await _record_replay_conflict_audit(
            session,
            job_id=job.id,
            webhook_id=callback.webhook_id,
        )
        raise SaladWebhookReplayConflictError("webhook event ID was reused")

    attempt_id = await _matching_attempt_id(
        session,
        provider_external_job_id=str(job.id),
    )
    receipt = WebhookReceipt(
        id=uuid7(),
        provider="salad",
        event_id=callback.webhook_id,
        event_type=f"queue_job.{job.status.value}",
        payload_sha256=payload_sha256,
        event_metadata=_receipt_metadata(job),
        provider_external_job_id=str(job.id),
        generation_attempt_id=attempt_id,
        status=InboxStatus.RECEIVED,
        signature_timestamp=_signature_datetime(callback.webhook_timestamp),
        verified_at=now,
        received_at=now,
        attempts=0,
        max_attempts=10,
        available_at=now,
    )
    session.add(receipt)
    try:
        await session.commit()
    except IntegrityError:
        # A concurrent delivery may win the unique constraint between the
        # initial read and commit. Resolve it without accepting a changed body.
        await session.rollback()
        concurrent = await session.scalar(
            select(WebhookReceipt).where(
                WebhookReceipt.provider == "salad",
                WebhookReceipt.event_id == callback.webhook_id,
            )
        )
        if concurrent is not None and concurrent.payload_sha256 == payload_sha256:
            return RecordedWebhook(receipt_id=concurrent.id, replayed=True)
        await _record_replay_conflict_audit(
            session,
            job_id=job.id,
            webhook_id=callback.webhook_id,
        )
        raise SaladWebhookReplayConflictError("webhook event ID was reused") from None

    return RecordedWebhook(receipt_id=receipt.id, replayed=False)
