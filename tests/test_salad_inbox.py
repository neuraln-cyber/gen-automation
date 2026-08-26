from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import count
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from gen_automation.db.models import (
    AuditEvent,
    GenerationAttempt,
    GenerationJob,
    Project,
    ProviderBudgetGuard,
    Release,
    ReleaseVersion,
    SaladDeployment,
    WebhookReceipt,
)
from gen_automation.db.session import Database
from gen_automation.domain.enums import (
    BudgetState,
    GenerationAttemptState,
    GenerationState,
    InboxStatus,
    ReleasePhase,
)
from gen_automation.services.budgets import ensure_budget_guard
from gen_automation.services.generation_recovery import (
    INFRASTRUCTURE_RETRY_GRANT_ACTION,
    SALAD_PROVIDER_CANCELLED_ERROR_CODE,
)
from gen_automation.services.salad import (
    DEPLOYMENT_ROLLOVER_CANCEL_REQUESTED_ERROR_CODE,
    DEPLOYMENT_ROLLOVER_RETRY_ERROR_CODE,
    SALAD_ATTEMPT_WATCHDOG_CANCEL_REQUESTED_ERROR_CODE,
    SALAD_ATTEMPT_WATCHDOG_EXPIRED_ERROR_CODE,
)
from gen_automation.services.salad_inbox import (
    InboxDisposition,
    SaladInboxLeaseLostError,
    SaladInboxValidationError,
    claim_salad_webhook_receipts,
    extend_salad_webhook_receipt_lease,
    fail_salad_webhook_receipt,
    process_salad_webhook_receipt,
    recover_expired_salad_webhook_receipts,
)

NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
_VERSIONS = count(1)


@dataclass(frozen=True)
class InboxContext:
    receipt_id: UUID
    attempt_id: UUID
    job_id: UUID
    provider_external_id: str


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    value = Database(f"sqlite+aiosqlite:///{(tmp_path / 'salad-inbox.db').as_posix()}")
    await value.create_schema()
    try:
        yield value
    finally:
        await value.dispose()


async def _seed(
    database: Database,
    *,
    provider_status: str = "running",
    observed_at: datetime = NOW,
    attempt_state: GenerationAttemptState = GenerationAttemptState.SUBMITTED,
    job_state: GenerationState = GenerationState.RUNNING,
    attempt_external_id: str | None = "same",
    link_receipt: bool = True,
    attempt_count: int = 1,
    job_max_attempts: int = 3,
    receipt_attempts: int = 0,
    receipt_max_attempts: int = 3,
    receipt_status: InboxStatus = InboxStatus.RECEIVED,
    lease_owner: str | None = None,
    lease_expires_at: datetime | None = None,
    last_observed_at: datetime | None = None,
    provider_state: str | None = None,
    reservation_microusd: int = 0,
    reservation_released_at: datetime | None = None,
    event_metadata: dict[str, object] | None = None,
) -> InboxContext:
    version_no = next(_VERSIONS)
    external_id = str(uuid4())
    resolved_attempt_external_id = (
        external_id if attempt_external_id == "same" else attempt_external_id
    )
    async with database.sessions() as session:
        if reservation_microusd > 0:
            await ensure_budget_guard(
                session,
                provider="salad",
                daily_limit_usd=Decimal("100"),
                monthly_limit_usd=Decimal("1000"),
                now=NOW,
            )
        project = Project(
            slug=f"inbox-{version_no}",
            name=f"Inbox {version_no}",
        )
        session.add(project)
        await session.flush()
        release = Release(
            project_id=project.id,
            slug="release",
            title="Release",
            desired_accepted_count=1,
            phase=ReleasePhase.GENERATING,
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
            version_no=version_no,
            config_sha256="b" * 64,
            worker_image_digest="registry.example/worker@sha256:" + "c" * 64,
            organization_name="organization",
            project_name="project",
            queue_name=f"queue-{version_no}",
            container_group_name=f"workers-{version_no}",
            max_hourly_cost_microusd=2_000_000,
        )
        session.add(deployment)
        job = GenerationJob(
            release_version_id=version.id,
            logical_key=f"{version_no:064x}",
            parameters={"seed": version_no},
            parameters_sha256="d" * 64,
            provider="salad",
            state=job_state,
            expected_output_count=1,
            attempt_count=attempt_count,
            max_attempts=job_max_attempts,
        )
        session.add(job)
        await session.flush()
        terminal = attempt_state in {
            GenerationAttemptState.SUCCEEDED,
            GenerationAttemptState.FAILED,
            GenerationAttemptState.CANCELLED,
        }
        attempt = GenerationAttempt(
            job_id=job.id,
            salad_deployment_id=deployment.id,
            attempt_no=max(attempt_count, 1),
            provider="salad",
            provider_external_id=resolved_attempt_external_id,
            submission_key=f"{version_no + 1:064x}",
            request_sha256="e" * 64,
            state=attempt_state,
            worker_image_digest=deployment.worker_image_digest,
            request_metadata={"generation_job_id": str(job.id)},
            completed_at=observed_at - timedelta(seconds=1) if terminal else None,
            last_observed_at=last_observed_at,
            unknown_since=(
                observed_at - timedelta(minutes=1)
                if attempt_state == GenerationAttemptState.UNKNOWN
                else None
            ),
            provider_state=provider_state,
            cost_reservation_microusd=reservation_microusd,
            reservation_released_at=reservation_released_at,
            created_at=observed_at - timedelta(minutes=2),
        )
        session.add(attempt)
        await session.flush()
        metadata = event_metadata or {
            "job_status": provider_status,
            "job_update_time": observed_at.isoformat(),
            "private_prompt": "must never be processed or audited",
            "output": {"signed_upload_url": "must never be processed or audited"},
        }
        receipt = WebhookReceipt(
            provider="salad",
            event_id=f"msg-{uuid4()}",
            event_type=f"queue_job.{provider_status}",
            payload_sha256="f" * 64,
            event_metadata=metadata,
            provider_external_job_id=external_id,
            generation_attempt_id=attempt.id if link_receipt else None,
            status=receipt_status,
            signature_timestamp=observed_at,
            verified_at=observed_at,
            received_at=observed_at,
            attempts=receipt_attempts,
            max_attempts=receipt_max_attempts,
            available_at=observed_at,
            lease_owner=lease_owner,
            lease_expires_at=lease_expires_at,
        )
        session.add(receipt)
        await session.commit()
        return InboxContext(
            receipt_id=receipt.id,
            attempt_id=attempt.id,
            job_id=job.id,
            provider_external_id=external_id,
        )


async def _claim_one(
    database: Database,
    *,
    now: datetime = NOW,
    worker_id: str = "inbox-worker",
) -> UUID:
    async with database.sessions() as session:
        claimed = await claim_salad_webhook_receipts(
            session,
            worker_id=worker_id,
            limit=1,
            lease_seconds=60,
            now=now,
        )
    assert len(claimed) == 1
    return claimed[0].receipt_id


async def test_claim_is_durable_and_processing_requires_lease_owner(
    database: Database,
) -> None:
    context = await _seed(database)

    claimed_id = await _claim_one(database)
    assert claimed_id == context.receipt_id

    async with database.sessions() as session:
        receipt = await session.get(WebhookReceipt, context.receipt_id)
        assert receipt is not None
        assert receipt.status == InboxStatus.PROCESSING
        assert receipt.attempts == 1
        assert receipt.lease_owner == "inbox-worker"

    async with database.sessions() as session:
        with pytest.raises(SaladInboxLeaseLostError):
            await process_salad_webhook_receipt(
                session,
                receipt_id=context.receipt_id,
                worker_id="different-worker",
                now=NOW + timedelta(seconds=1),
            )


async def test_expired_leases_are_replayed_but_bounded_by_max_attempts(
    database: Database,
) -> None:
    context = await _seed(
        database,
        receipt_status=InboxStatus.PROCESSING,
        receipt_attempts=1,
        receipt_max_attempts=2,
        lease_owner="dead-worker",
        lease_expires_at=NOW - timedelta(seconds=1),
    )

    claimed_id = await _claim_one(database)
    assert claimed_id == context.receipt_id

    async with database.sessions() as session:
        receipt = await session.get(WebhookReceipt, context.receipt_id)
        assert receipt is not None
        assert receipt.status == InboxStatus.PROCESSING
        assert receipt.attempts == 2

    async with database.sessions() as session:
        claimed = await claim_salad_webhook_receipts(
            session,
            worker_id="recovery-worker",
            limit=1,
            lease_seconds=60,
            now=NOW + timedelta(minutes=2),
        )
        assert claimed == []

    async with database.sessions() as session:
        receipt = await session.get(WebhookReceipt, context.receipt_id)
        assert receipt is not None
        assert receipt.status == InboxStatus.DEAD_LETTER
        assert receipt.last_error_code == "processing_attempts_exhausted"
        assert receipt.lease_owner is None


async def test_unmatched_receipt_retries_then_late_matches_without_content_use(
    database: Database,
) -> None:
    context = await _seed(
        database,
        attempt_state=GenerationAttemptState.UNKNOWN,
        job_state=GenerationState.UNKNOWN,
        attempt_external_id=None,
        link_receipt=False,
    )
    await _claim_one(database)

    async with database.sessions() as session:
        first = await process_salad_webhook_receipt(
            session,
            receipt_id=context.receipt_id,
            worker_id="inbox-worker",
            now=NOW + timedelta(seconds=1),
        )
        assert first.disposition == InboxDisposition.RETRY_SCHEDULED

    async with database.sessions() as session:
        attempt = await session.get(GenerationAttempt, context.attempt_id)
        assert attempt is not None
        attempt.provider_external_id = context.provider_external_id
        await session.commit()

    await _claim_one(
        database,
        now=NOW + timedelta(seconds=61),
    )
    async with database.sessions() as session:
        second = await process_salad_webhook_receipt(
            session,
            receipt_id=context.receipt_id,
            worker_id="inbox-worker",
            now=NOW + timedelta(seconds=62),
        )
        assert second.disposition == InboxDisposition.APPLIED
        assert second.attempt_state == GenerationAttemptState.RUNNING
        assert second.job_state == GenerationState.RUNNING

    async with database.sessions() as session:
        receipt = await session.get(WebhookReceipt, context.receipt_id)
        audits = list((await session.scalars(select(AuditEvent))).all())
        assert receipt is not None
        assert receipt.generation_attempt_id == context.attempt_id
        serialized_audits = repr([event.detail for event in audits])
        assert "private_prompt" not in serialized_audits
        assert "signed_upload_url" not in serialized_audits


async def test_signed_success_requires_output_reconciliation_and_holds_reservation(
    database: Database,
) -> None:
    context = await _seed(
        database,
        provider_status="succeeded",
        attempt_state=GenerationAttemptState.UNKNOWN,
        job_state=GenerationState.UNKNOWN,
        last_observed_at=NOW - timedelta(minutes=1),
        provider_state="running",
        reservation_microusd=750_000,
    )
    async with database.sessions() as session:
        guard = await session.scalar(
            select(ProviderBudgetGuard).where(ProviderBudgetGuard.provider == "salad")
        )
        assert guard is not None
        guard.state = BudgetState.BLOCKED
        guard.blocked_reason = "stale_reservation_pressure"
        guard.blocked_at = NOW - timedelta(seconds=1)
        await session.commit()
    await _claim_one(database)

    async with database.sessions() as session:
        result = await process_salad_webhook_receipt(
            session,
            receipt_id=context.receipt_id,
            worker_id="inbox-worker",
            now=NOW + timedelta(seconds=1),
        )

    assert result.disposition == InboxDisposition.APPLIED
    assert result.attempt_state == GenerationAttemptState.UNKNOWN
    assert result.job_state == GenerationState.UNKNOWN

    async with database.sessions() as session:
        attempt = await session.get(GenerationAttempt, context.attempt_id)
        actions = set(await session.scalars(select(AuditEvent.action)))
        assert attempt is not None
        assert attempt.unknown_since is not None
        assert attempt.completed_at is None
        assert attempt.error_code == "salad_worker_output_unverified"
        assert attempt.reservation_released_at is None
        assert "salad.webhook.reconciled_unknown" not in actions
        assert "provider_budget.reservation_released" not in actions
        guard_state = await session.scalar(
            select(ProviderBudgetGuard.state).where(ProviderBudgetGuard.provider == "salad")
        )
        assert guard_state == BudgetState.BLOCKED


@pytest.mark.parametrize(
    ("attempt_count", "max_attempts", "expected_job_state"),
    [
        (1, 3, GenerationState.RETRY_WAIT),
        (3, 3, GenerationState.RETRY_WAIT),
    ],
)
async def test_definitive_failure_retries_or_dead_letters_generation_job(
    database: Database,
    attempt_count: int,
    max_attempts: int,
    expected_job_state: GenerationState,
) -> None:
    context = await _seed(
        database,
        provider_status="failed",
        attempt_state=GenerationAttemptState.RUNNING,
        job_state=GenerationState.RUNNING,
        attempt_count=attempt_count,
        job_max_attempts=max_attempts,
        reservation_microusd=500_000,
    )
    await _claim_one(database)

    async with database.sessions() as session:
        result = await process_salad_webhook_receipt(
            session,
            receipt_id=context.receipt_id,
            worker_id="inbox-worker",
            retry_delay_seconds=90,
            now=NOW + timedelta(seconds=1),
        )

    assert result.attempt_state == GenerationAttemptState.FAILED
    assert result.job_state == expected_job_state
    async with database.sessions() as session:
        attempt = await session.get(GenerationAttempt, context.attempt_id)
        job = await session.get(GenerationJob, context.job_id)
        assert attempt is not None
        assert job is not None
        assert attempt.reservation_released_at is not None
        if expected_job_state == GenerationState.RETRY_WAIT:
            assert job.retry_at is not None
        else:
            assert job.retry_at is None


async def test_unrequested_provider_cancel_is_bounded_infrastructure_retry(
    database: Database,
) -> None:
    context = await _seed(
        database,
        provider_status="cancelled",
        attempt_state=GenerationAttemptState.CANCEL_REQUESTED,
        job_state=GenerationState.CANCEL_REQUESTED,
        reservation_microusd=100_000,
    )
    await _claim_one(database)

    async with database.sessions() as session:
        result = await process_salad_webhook_receipt(
            session,
            receipt_id=context.receipt_id,
            worker_id="inbox-worker",
            now=NOW + timedelta(seconds=1),
        )

    assert result.attempt_state == GenerationAttemptState.FAILED
    assert result.job_state == GenerationState.RETRY_WAIT
    async with database.sessions() as session:
        attempt = await session.get(GenerationAttempt, context.attempt_id)
        assert attempt is not None
        assert attempt.error_code == SALAD_PROVIDER_CANCELLED_ERROR_CODE


async def test_watchdog_cancel_confirmation_retries_generation_job(
    database: Database,
) -> None:
    context = await _seed(
        database,
        provider_status="cancelled",
        attempt_state=GenerationAttemptState.CANCEL_REQUESTED,
        job_state=GenerationState.RUNNING,
        reservation_microusd=100_000,
    )
    async with database.sessions() as session:
        attempt = await session.get(GenerationAttempt, context.attempt_id)
        assert attempt is not None
        attempt.error_code = SALAD_ATTEMPT_WATCHDOG_CANCEL_REQUESTED_ERROR_CODE
        attempt.error_detail = "watchdog cancellation pending"
        await session.commit()
    await _claim_one(database)

    async with database.sessions() as session:
        result = await process_salad_webhook_receipt(
            session,
            receipt_id=context.receipt_id,
            worker_id="inbox-worker",
            retry_delay_seconds=90,
            now=NOW + timedelta(seconds=1),
        )

    assert result.attempt_state == GenerationAttemptState.FAILED
    assert result.job_state == GenerationState.RETRY_WAIT
    async with database.sessions() as session:
        attempt = await session.get(GenerationAttempt, context.attempt_id)
        job = await session.get(GenerationJob, context.job_id)
        assert attempt is not None
        assert job is not None
        assert attempt.error_code == SALAD_ATTEMPT_WATCHDOG_EXPIRED_ERROR_CODE
        assert attempt.reservation_released_at is not None
        assert job.last_error_code == SALAD_ATTEMPT_WATCHDOG_EXPIRED_ERROR_CODE
        assert job.retry_at is not None
        assert job.retry_at.replace(tzinfo=UTC) == NOW + timedelta(seconds=91)


async def test_deployment_rollover_cancel_confirmation_retries_generation_job(
    database: Database,
) -> None:
    context = await _seed(
        database,
        provider_status="cancelled",
        attempt_state=GenerationAttemptState.CANCEL_REQUESTED,
        job_state=GenerationState.RUNNING,
        reservation_microusd=100_000,
    )
    async with database.sessions() as session:
        attempt = await session.get(GenerationAttempt, context.attempt_id)
        assert attempt is not None
        attempt.error_code = DEPLOYMENT_ROLLOVER_CANCEL_REQUESTED_ERROR_CODE
        attempt.error_detail = "deployment rollover cancellation pending"
        await session.commit()
    await _claim_one(database)

    async with database.sessions() as session:
        result = await process_salad_webhook_receipt(
            session,
            receipt_id=context.receipt_id,
            worker_id="inbox-worker",
            retry_delay_seconds=90,
            now=NOW + timedelta(seconds=1),
        )

    assert result.attempt_state == GenerationAttemptState.FAILED
    assert result.job_state == GenerationState.RETRY_WAIT
    async with database.sessions() as session:
        attempt = await session.get(GenerationAttempt, context.attempt_id)
        job = await session.get(GenerationJob, context.job_id)
        assert attempt is not None
        assert job is not None
        assert attempt.error_code == DEPLOYMENT_ROLLOVER_RETRY_ERROR_CODE
        assert attempt.reservation_released_at is not None
        assert job.last_error_code == DEPLOYMENT_ROLLOVER_RETRY_ERROR_CODE
        assert job.retry_at is not None
        assert job.retry_at.replace(tzinfo=UTC) == NOW + timedelta(seconds=91)


async def test_operator_stop_wins_over_deployment_rollover_retry(
    database: Database,
) -> None:
    context = await _seed(
        database,
        provider_status="cancelled",
        attempt_state=GenerationAttemptState.CANCEL_REQUESTED,
        job_state=GenerationState.RUNNING,
        reservation_microusd=100_000,
    )
    async with database.sessions() as session:
        attempt = await session.get(GenerationAttempt, context.attempt_id)
        job = await session.get(GenerationJob, context.job_id)
        assert attempt is not None
        assert job is not None
        version = await session.get(ReleaseVersion, job.release_version_id)
        assert version is not None
        attempt.error_code = DEPLOYMENT_ROLLOVER_CANCEL_REQUESTED_ERROR_CODE
        attempt.response_metadata = {"deployment_rollover_cancel_requested": True}
        session.add(
            AuditEvent(
                actor="test-owner",
                action="release.generation_stop_requested",
                resource_type="release",
                resource_id=version.release_id,
                correlation_id=f"generation-stop:{version.release_id}",
                detail={"assets_retained": True},
                occurred_at=NOW,
            )
        )
        await session.commit()
    await _claim_one(database)

    async with database.sessions() as session:
        result = await process_salad_webhook_receipt(
            session,
            receipt_id=context.receipt_id,
            worker_id="inbox-worker",
            now=NOW + timedelta(seconds=1),
        )

    assert result.attempt_state == GenerationAttemptState.CANCELLED
    assert result.job_state == GenerationState.CANCELLED


async def test_stale_and_regressive_callbacks_cannot_move_state_backwards(
    database: Database,
) -> None:
    stale = await _seed(
        database,
        provider_status="pending",
        observed_at=NOW - timedelta(minutes=2),
        attempt_state=GenerationAttemptState.RUNNING,
        job_state=GenerationState.RUNNING,
        last_observed_at=NOW - timedelta(minutes=1),
        provider_state="running",
        reservation_microusd=100_000,
    )
    regression = await _seed(
        database,
        provider_status="pending",
        observed_at=NOW,
        attempt_state=GenerationAttemptState.RUNNING,
        job_state=GenerationState.RUNNING,
        last_observed_at=NOW - timedelta(minutes=1),
        provider_state="running",
        reservation_microusd=100_000,
    )

    async with database.sessions() as session:
        claimed = await claim_salad_webhook_receipts(
            session,
            worker_id="inbox-worker",
            limit=2,
            lease_seconds=60,
            now=NOW,
        )
        assert {item.receipt_id for item in claimed} == {
            stale.receipt_id,
            regression.receipt_id,
        }

    for context in (stale, regression):
        async with database.sessions() as session:
            result = await process_salad_webhook_receipt(
                session,
                receipt_id=context.receipt_id,
                worker_id="inbox-worker",
                now=NOW + timedelta(seconds=1),
            )
            assert result.disposition == InboxDisposition.STALE_IGNORED
            assert result.attempt_state == GenerationAttemptState.RUNNING
            assert result.job_state == GenerationState.RUNNING

    async with database.sessions() as session:
        attempts = list(
            (
                await session.scalars(
                    select(GenerationAttempt).where(
                        GenerationAttempt.id.in_((stale.attempt_id, regression.attempt_id))
                    )
                )
            ).all()
        )
        assert all(item.reservation_released_at is None for item in attempts)


async def test_conflicting_terminal_callback_is_dead_lettered_without_regression(
    database: Database,
) -> None:
    context = await _seed(
        database,
        provider_status="failed",
        attempt_state=GenerationAttemptState.SUCCEEDED,
        job_state=GenerationState.COLLECTING,
        last_observed_at=NOW - timedelta(seconds=1),
        provider_state="succeeded",
        reservation_microusd=100_000,
        reservation_released_at=NOW - timedelta(seconds=1),
    )
    await _claim_one(database)

    async with database.sessions() as session:
        result = await process_salad_webhook_receipt(
            session,
            receipt_id=context.receipt_id,
            worker_id="inbox-worker",
            now=NOW + timedelta(seconds=1),
        )

    assert result.disposition == InboxDisposition.TERMINAL_CONFLICT
    assert result.receipt_status == InboxStatus.DEAD_LETTER
    assert result.attempt_state == GenerationAttemptState.SUCCEEDED
    assert result.job_state == GenerationState.COLLECTING


async def test_terminal_confirmation_releases_stranded_reservation_without_job_regression(
    database: Database,
) -> None:
    context = await _seed(
        database,
        provider_status="succeeded",
        attempt_state=GenerationAttemptState.SUCCEEDED,
        job_state=GenerationState.VERIFYING,
        last_observed_at=NOW - timedelta(seconds=1),
        provider_state="succeeded",
        reservation_microusd=100_000,
    )
    await _claim_one(database)

    async with database.sessions() as session:
        result = await process_salad_webhook_receipt(
            session,
            receipt_id=context.receipt_id,
            worker_id="inbox-worker",
            now=NOW + timedelta(seconds=1),
        )

    assert result.disposition == InboxDisposition.NO_CHANGE
    assert result.job_state == GenerationState.VERIFYING
    async with database.sessions() as session:
        attempt = await session.get(GenerationAttempt, context.attempt_id)
        assert attempt is not None
        assert attempt.reservation_released_at is not None


async def test_invalid_sanitized_contract_is_dead_lettered(
    database: Database,
) -> None:
    context = await _seed(
        database,
        event_metadata={
            "job_status": "unpublished-provider-state",
            "job_update_time": NOW.isoformat(),
            "input": "private",
        },
    )
    await _claim_one(database)

    async with database.sessions() as session:
        result = await process_salad_webhook_receipt(
            session,
            receipt_id=context.receipt_id,
            worker_id="inbox-worker",
            now=NOW + timedelta(seconds=1),
        )

    assert result.disposition == InboxDisposition.DEAD_LETTERED
    assert result.receipt_status == InboxStatus.DEAD_LETTER
    async with database.sessions() as session:
        receipt = await session.get(WebhookReceipt, context.receipt_id)
        assert receipt is not None
        assert receipt.last_error_code == "invalid_event_contract"


async def test_explicit_recovery_commits_retryable_expired_receipt(
    database: Database,
) -> None:
    context = await _seed(
        database,
        receipt_status=InboxStatus.PROCESSING,
        receipt_attempts=1,
        receipt_max_attempts=3,
        lease_owner="dead-worker",
        lease_expires_at=NOW - timedelta(seconds=1),
    )

    async with database.sessions() as session:
        summary = await recover_expired_salad_webhook_receipts(
            session,
            now=NOW,
        )
        assert summary.retried == 1
        assert summary.dead_lettered == 0

    async with database.sessions() as session:
        receipt = await session.get(WebhookReceipt, context.receipt_id)
        assert receipt is not None
        assert receipt.status == InboxStatus.RETRY_WAIT
        assert receipt.lease_owner is None


async def test_explicit_failure_is_sanitized_retryable_and_can_be_dead_lettered(
    database: Database,
) -> None:
    context = await _seed(database)
    await _claim_one(database)

    async with database.sessions() as session:
        retry = await fail_salad_webhook_receipt(
            session,
            receipt_id=context.receipt_id,
            worker_id="inbox-worker",
            error_code="transient_database_error",
            safe_error_detail="  safe   internal detail  ",
            retry_not_before=NOW + timedelta(minutes=1),
            now=NOW + timedelta(seconds=1),
        )
        assert retry.disposition == InboxDisposition.RETRY_SCHEDULED

    await _claim_one(database, now=NOW + timedelta(minutes=1))
    async with database.sessions() as session:
        dead = await fail_salad_webhook_receipt(
            session,
            receipt_id=context.receipt_id,
            worker_id="inbox-worker",
            error_code="permanent_processing_error",
            retryable=False,
            now=NOW + timedelta(minutes=1, seconds=1),
        )
        assert dead.disposition == InboxDisposition.DEAD_LETTERED

    async with database.sessions() as session:
        replay = await fail_salad_webhook_receipt(
            session,
            receipt_id=context.receipt_id,
            worker_id="inbox-worker",
            error_code="permanent_processing_error",
            retryable=False,
            now=NOW + timedelta(minutes=1, seconds=2),
        )
        receipt = await session.get(WebhookReceipt, context.receipt_id)
        assert replay.disposition == InboxDisposition.ALREADY_PROCESSED
        assert receipt is not None
        assert receipt.last_error_detail is None


async def test_lease_extension_and_public_argument_validation(
    database: Database,
) -> None:
    context = await _seed(database)
    await _claim_one(database)

    async with database.sessions() as session:
        extended = await extend_salad_webhook_receipt_lease(
            session,
            receipt_id=context.receipt_id,
            worker_id="inbox-worker",
            lease_seconds=120,
            now=NOW + timedelta(seconds=1),
        )
        assert extended == NOW + timedelta(seconds=121)

    async with database.sessions() as session:
        with pytest.raises(SaladInboxValidationError):
            await claim_salad_webhook_receipts(
                session,
                worker_id="worker",
                limit=0,
                lease_seconds=60,
                now=NOW,
            )
        with pytest.raises(SaladInboxValidationError):
            await claim_salad_webhook_receipts(
                session,
                worker_id="worker",
                limit=1,
                lease_seconds=0,
                now=NOW,
            )
        with pytest.raises(SaladInboxValidationError):
            await recover_expired_salad_webhook_receipts(
                session,
                limit=0,
                now=NOW,
            )
        with pytest.raises(SaladInboxValidationError):
            await process_salad_webhook_receipt(
                session,
                receipt_id=context.receipt_id,
                worker_id="worker",
                retry_delay_seconds=0,
                now=NOW,
            )


async def test_pending_status_records_remote_acceptance(
    database: Database,
) -> None:
    context = await _seed(
        database,
        provider_status="pending",
        attempt_state=GenerationAttemptState.SUBMITTING,
        job_state=GenerationState.SUBMITTING,
    )
    await _claim_one(database)

    async with database.sessions() as session:
        result = await process_salad_webhook_receipt(
            session,
            receipt_id=context.receipt_id,
            worker_id="inbox-worker",
            now=NOW + timedelta(seconds=1),
        )

    assert result.disposition == InboxDisposition.APPLIED
    assert result.attempt_state == GenerationAttemptState.SUBMITTED
    assert result.job_state == GenerationState.RUNNING


async def test_same_timestamp_conflict_and_terminal_job_conflict_are_quarantined(
    database: Database,
) -> None:
    same_timestamp = await _seed(
        database,
        provider_status="failed",
        observed_at=NOW,
        attempt_state=GenerationAttemptState.RUNNING,
        job_state=GenerationState.RUNNING,
        last_observed_at=NOW,
        provider_state="running",
    )
    terminal_job = await _seed(
        database,
        provider_status="running",
        observed_at=NOW,
        attempt_state=GenerationAttemptState.SUBMITTED,
        job_state=GenerationState.SUCCEEDED,
    )

    async with database.sessions() as session:
        claimed = await claim_salad_webhook_receipts(
            session,
            worker_id="inbox-worker",
            limit=2,
            lease_seconds=60,
            now=NOW,
        )
        assert len(claimed) == 2

    async with database.sessions() as session:
        forward = await process_salad_webhook_receipt(
            session,
            receipt_id=same_timestamp.receipt_id,
            worker_id="inbox-worker",
            now=NOW + timedelta(seconds=1),
        )
    assert forward.disposition == InboxDisposition.APPLIED
    assert forward.attempt_state == GenerationAttemptState.FAILED

    async with database.sessions() as session:
        conflict = await process_salad_webhook_receipt(
            session,
            receipt_id=terminal_job.receipt_id,
            worker_id="inbox-worker",
            now=NOW + timedelta(seconds=1),
        )
    assert conflict.disposition == InboxDisposition.TERMINAL_CONFLICT
    assert conflict.receipt_status == InboxStatus.DEAD_LETTER


async def test_prelinked_attempt_must_match_provider_external_id(
    database: Database,
) -> None:
    context = await _seed(
        database,
        attempt_external_id=str(uuid4()),
        link_receipt=True,
    )
    await _claim_one(database)

    async with database.sessions() as session:
        result = await process_salad_webhook_receipt(
            session,
            receipt_id=context.receipt_id,
            worker_id="inbox-worker",
            now=NOW + timedelta(seconds=1),
        )

    assert result.disposition == InboxDisposition.DEAD_LETTERED
    assert result.receipt_status == InboxStatus.DEAD_LETTER


async def test_final_webhook_failure_grants_once_and_replay_is_idempotent(
    database: Database,
) -> None:
    context = await _seed(
        database,
        provider_status="failed",
        attempt_count=1,
        job_max_attempts=1,
    )
    await _claim_one(database)
    async with database.sessions() as session:
        first = await process_salad_webhook_receipt(
            session,
            receipt_id=context.receipt_id,
            worker_id="inbox-worker",
            now=NOW + timedelta(seconds=1),
        )
        replay = await process_salad_webhook_receipt(
            session,
            receipt_id=context.receipt_id,
            worker_id="inbox-worker",
            now=NOW + timedelta(seconds=2),
        )
        job = await session.get(GenerationJob, context.job_id)
        grants = int(
            await session.scalar(
                select(func.count(AuditEvent.id)).where(
                    AuditEvent.action == INFRASTRUCTURE_RETRY_GRANT_ACTION
                )
            )
            or 0
        )

    assert first.job_state == GenerationState.RETRY_WAIT
    assert replay.disposition == InboxDisposition.ALREADY_PROCESSED
    assert job is not None
    assert job.max_attempts == 2
    assert grants == 1


async def test_webhook_for_superseded_attempt_cannot_rearm_current_job(
    database: Database,
) -> None:
    context = await _seed(database, provider_status="failed")
    async with database.sessions() as session:
        job = await session.get(GenerationJob, context.job_id)
        assert job is not None
        job.attempt_count = 2
        await session.commit()
    await _claim_one(database)
    async with database.sessions() as session:
        result = await process_salad_webhook_receipt(
            session,
            receipt_id=context.receipt_id,
            worker_id="inbox-worker",
            now=NOW + timedelta(seconds=1),
        )
        job = await session.get(GenerationJob, context.job_id)
        grants = int(
            await session.scalar(
                select(func.count(AuditEvent.id)).where(
                    AuditEvent.action == INFRASTRUCTURE_RETRY_GRANT_ACTION
                )
            )
            or 0
        )

    assert result.disposition == InboxDisposition.STALE_IGNORED
    assert job is not None
    assert job.state == GenerationState.RUNNING
    assert job.max_attempts == 3
    assert grants == 0
