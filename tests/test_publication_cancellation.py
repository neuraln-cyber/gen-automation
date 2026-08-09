# ruff: noqa: F811

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from gen_automation.db.models import (
    PublicationApproval,
    PublicationAttempt,
    PublicationEffectEvent,
    PublicationIntent,
    PublicationStep,
    Release,
    SubjectApproval,
)
from gen_automation.domain.enums import (
    AdminRole,
    PublicationApprovalAction,
    PublicationAttemptState,
    PublicationIntentState,
    PublicationRetryClass,
    PublicationStepKind,
    PublicationStepState,
    PublicationTarget,
    ReleasePhase,
)
from gen_automation.services.operator_delivery import (
    OperatorDeliveryConflictError,
    prepare_operator_x_destination,
)
from gen_automation.services.publication import (
    PUBLICATION_CONFIRM_ABSENT_ATTESTATION,
    PUBLICATION_EFFECT_APPROVAL_ATTESTATION,
    PUBLICATION_PRE_EFFECT_CANCELLATION_ATTESTATION,
    PUBLICATION_REVOCATION_ATTESTATION,
    PublicationConflictError,
    approve_publication_intent,
    cancel_publication_intent,
    plan_publication_intent,
    reconcile_publication_absent,
    revoke_publication_intent,
)
from tests.test_derivative_pipeline import ApprovedContext
from tests.test_derivative_pipeline import (
    approved_context as derivative_approved_context,  # noqa: F401
)
from tests.test_operator_delivery import _prepare_independent_destination_inputs


async def _plan_x_intent(
    session,
    *,
    approved: ApprovedContext,
    output_ids: tuple,
    text: str,
    idempotency_key: str,
    now: datetime,
    scheduled_at: datetime | None = None,
):
    return await plan_publication_intent(
        session,
        release_version_id=approved.release_version_id,
        target=PublicationTarget.X,
        configuration={"text": text, "adult_content": False},
        derivative_output_ids=output_ids,
        planned_by_user_id=approved.owner_id,
        idempotency_key=idempotency_key,
        scheduled_at=scheduled_at,
        credential_reference="test://x/oauth",
        now=now,
    )


async def _plan_and_approve_x_intent(
    session,
    *,
    approved: ApprovedContext,
    output_ids: tuple,
    text: str,
    key_prefix: str,
    now: datetime,
    scheduled_at: datetime | None = None,
):
    planned = await _plan_x_intent(
        session,
        approved=approved,
        output_ids=output_ids,
        text=text,
        idempotency_key=f"{key_prefix}:plan",
        now=now,
        scheduled_at=scheduled_at,
    )
    approved_intent = await approve_publication_intent(
        session,
        intent_id=planned.intent_id,
        expected_intent_digest=planned.intent_digest,
        expected_lock_version=planned.lock_version,
        actor_user_id=approved.owner_id,
        actor_role=AdminRole.OWNER,
        attestation=PUBLICATION_EFFECT_APPROVAL_ATTESTATION,
        approval_seconds=900,
        idempotency_key=f"{key_prefix}:approve",
        now=now,
    )
    return planned, approved_intent


async def _mark_pre_provider_retry(
    session,
    *,
    intent_id,
    attempt_id,
    error_code: str,
    now: datetime,
    step_kind: PublicationStepKind | None = None,
):
    intent = await session.get(PublicationIntent, intent_id)
    attempt = await session.get(PublicationAttempt, attempt_id)
    step = await session.scalar(
        select(PublicationStep)
        .where(
            PublicationStep.attempt_id == attempt_id,
            *((PublicationStep.kind == step_kind,) if step_kind is not None else ()),
        )
        .order_by(PublicationStep.ordinal)
        .limit(1)
    )
    assert intent is not None and attempt is not None and step is not None
    step_id = step.id
    retry_at = now + timedelta(minutes=5)
    intent.state = PublicationIntentState.READY
    intent.last_error_code = error_code
    attempt.state = PublicationAttemptState.RETRY_WAIT
    attempt.attempt_count = 1
    attempt.retry_at = retry_at
    attempt.last_error_code = error_code
    step.state = PublicationStepState.RETRY_WAIT
    step.retry_class = PublicationRetryClass.SAFE_RETRY
    step.retry_count = 1
    step.retry_at = retry_at
    step.effect_started_at = now
    step.last_error_code = error_code
    await session.commit()
    effect_event_count = int(
        await session.scalar(
            select(func.count(PublicationEffectEvent.id)).where(
                PublicationEffectEvent.step_id == step_id
            )
        )
        or 0
    )
    assert effect_event_count == 0
    return step_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("intent_state", "attempt_state", "step_state"),
    (
        (
            PublicationIntentState.UNKNOWN,
            PublicationAttemptState.UNKNOWN,
            PublicationStepState.UNKNOWN,
        ),
        (
            PublicationIntentState.AWAITING_HUMAN,
            PublicationAttemptState.AWAITING_HUMAN,
            PublicationStepState.AWAITING_HUMAN,
        ),
        (
            PublicationIntentState.READY,
            PublicationAttemptState.UNKNOWN,
            PublicationStepState.UNKNOWN,
        ),
        (
            PublicationIntentState.READY,
            PublicationAttemptState.AWAITING_HUMAN,
            PublicationStepState.AWAITING_HUMAN,
        ),
    ),
)
async def test_revocation_cannot_reset_an_unresolved_outcome_for_reapproval(
    derivative_approved_context: ApprovedContext,
    intent_state: PublicationIntentState,
    attempt_state: PublicationAttemptState,
    step_state: PublicationStepState,
) -> None:
    approved = derivative_approved_context
    snapshot = await _prepare_independent_destination_inputs(approved)
    output_ids = tuple(output.output_id for output in snapshot.x_outputs)
    action_at = datetime.now(UTC)

    async with approved.database.sessions() as session:
        planned, approval = await _plan_and_approve_x_intent(
            session,
            approved=approved,
            output_ids=output_ids,
            text="Unresolved outcome must stay blocked",
            key_prefix=f"unresolved-revocation:{intent_state.value}",
            now=action_at,
        )
        intent = await session.get(PublicationIntent, planned.intent_id)
        attempt = await session.get(PublicationAttempt, approval.attempt_id)
        step = await session.scalar(
            select(PublicationStep)
            .where(PublicationStep.attempt_id == approval.attempt_id)
            .order_by(PublicationStep.ordinal.desc())
            .limit(1)
        )
        assert intent is not None and attempt is not None and step is not None
        intent_id = planned.intent_id
        attempt_id = approval.attempt_id
        step_id = step.id
        intent.state = intent_state
        attempt.state = attempt_state
        step.state = step_state
        await session.commit()
        original_lock = approval.intent_lock_version
        original_approval_count = int(
            await session.scalar(
                select(func.count(PublicationApproval.id)).where(
                    PublicationApproval.intent_id == intent_id
                )
            )
            or 0
        )

        with pytest.raises(PublicationConflictError, match="requires reconciliation"):
            await revoke_publication_intent(
                session,
                intent_id=intent_id,
                expected_intent_digest=planned.intent_digest,
                expected_lock_version=approval.intent_lock_version,
                actor_user_id=approved.owner_id,
                actor_role=AdminRole.OWNER,
                attestation=PUBLICATION_REVOCATION_ATTESTATION,
                idempotency_key=f"unresolved-revocation:{intent_state.value}:revoke",
                now=action_at + timedelta(seconds=1),
            )
        await session.rollback()

        with pytest.raises(PublicationConflictError):
            await cancel_publication_intent(
                session,
                intent_id=intent_id,
                expected_intent_digest=planned.intent_digest,
                expected_lock_version=approval.intent_lock_version,
                actor_user_id=approved.owner_id,
                actor_role=AdminRole.OWNER,
                attestation=PUBLICATION_PRE_EFFECT_CANCELLATION_ATTESTATION,
                idempotency_key=f"unresolved-revocation:{intent_state.value}:cancel",
                now=action_at + timedelta(seconds=2),
            )
        await session.rollback()

        unchanged_intent = await session.get(PublicationIntent, intent_id)
        unchanged_attempt = await session.get(PublicationAttempt, attempt_id)
        unchanged_step = await session.get(PublicationStep, step_id)
        approval_count = int(
            await session.scalar(
                select(func.count(PublicationApproval.id)).where(
                    PublicationApproval.intent_id == intent_id
                )
            )
            or 0
        )

    assert unchanged_intent is not None and unchanged_intent.state == intent_state
    assert unchanged_intent.lock_version == original_lock
    assert unchanged_attempt is not None and unchanged_attempt.state == attempt_state
    assert unchanged_step is not None and unchanged_step.state == step_state
    assert approval_count == original_approval_count == 1


@pytest.mark.asyncio
async def test_unapproved_intent_cancellation_is_final_idempotent_and_frees_target_slot(
    derivative_approved_context: ApprovedContext,
) -> None:
    approved = derivative_approved_context
    snapshot = await _prepare_independent_destination_inputs(approved)
    output_ids = tuple(output.output_id for output in snapshot.x_outputs)
    action_at = datetime.now(UTC)
    scheduled_at = action_at + timedelta(days=14)

    async with approved.database.sessions() as session:
        planned = await _plan_x_intent(
            session,
            approved=approved,
            output_ids=output_ids,
            text="Cancel before approval",
            idempotency_key="cancel-unapproved:plan",
            now=action_at,
            scheduled_at=scheduled_at,
        )
        cancellation = await cancel_publication_intent(
            session,
            intent_id=planned.intent_id,
            expected_intent_digest=planned.intent_digest,
            expected_lock_version=planned.lock_version,
            actor_user_id=approved.owner_id,
            actor_role=AdminRole.OWNER,
            attestation=PUBLICATION_PRE_EFFECT_CANCELLATION_ATTESTATION,
            idempotency_key="cancel-unapproved:cancel",
            now=action_at + timedelta(seconds=1),
        )
        replay = await cancel_publication_intent(
            session,
            intent_id=planned.intent_id,
            expected_intent_digest=planned.intent_digest,
            expected_lock_version=planned.lock_version,
            actor_user_id=approved.owner_id,
            actor_role=AdminRole.OWNER,
            attestation=PUBLICATION_PRE_EFFECT_CANCELLATION_ATTESTATION,
            idempotency_key="cancel-unapproved:cancel",
            now=action_at + timedelta(seconds=2),
        )
        cancelled_intent = await session.get(PublicationIntent, planned.intent_id)
        assert cancelled_intent is not None

        with pytest.raises(PublicationConflictError, match="not awaiting approval"):
            await approve_publication_intent(
                session,
                intent_id=cancelled_intent.id,
                expected_intent_digest=cancelled_intent.intent_digest,
                expected_lock_version=cancelled_intent.lock_version,
                actor_user_id=approved.owner_id,
                actor_role=AdminRole.OWNER,
                attestation=PUBLICATION_EFFECT_APPROVAL_ATTESTATION,
                approval_seconds=900,
                idempotency_key="cancel-unapproved:forbidden-reapproval",
                now=action_at + timedelta(seconds=3),
            )
        await session.rollback()

        with pytest.raises(OperatorDeliveryConflictError, match="not awaiting approval"):
            await prepare_operator_x_destination(
                session,
                review_task_id=approved.review_task_id,
                x_text="Cancel before approval",
                x_adult_content=False,
                x_made_with_ai=True,
                scheduled_at=scheduled_at,
                x_credential_reference="test://x/oauth",
                actor_user_id=approved.owner_id,
                actor_role=AdminRole.OWNER,
                idempotency_key="cancel-unapproved:forbidden-exact-operator-retry",
                now=action_at + timedelta(seconds=4),
            )
        await session.rollback()

        replacement = await _plan_x_intent(
            session,
            approved=approved,
            output_ids=output_ids,
            text="Replacement with a changed schedule",
            idempotency_key="cancel-unapproved:replacement-plan",
            now=action_at + timedelta(seconds=5),
            scheduled_at=scheduled_at + timedelta(days=1),
        )
        intents = tuple(
            (
                await session.scalars(
                    select(PublicationIntent).order_by(PublicationIntent.planned_at)
                )
            ).all()
        )
        approvals = tuple(
            (
                await session.scalars(
                    select(PublicationApproval).where(
                        PublicationApproval.intent_id == planned.intent_id
                    )
                )
            ).all()
        )

    assert cancellation.state == PublicationIntentState.CANCELLED
    assert not cancellation.replayed
    assert replay.replayed and replay.intent_id == cancellation.intent_id
    assert replacement.intent_id != planned.intent_id
    assert [intent.state for intent in intents] == [
        PublicationIntentState.CANCELLED,
        PublicationIntentState.AWAITING_APPROVAL,
    ]
    assert approvals == ()


@pytest.mark.asyncio
async def test_scheduled_ready_intent_can_be_cancelled_after_release_and_compliance_drift(
    derivative_approved_context: ApprovedContext,
) -> None:
    approved = derivative_approved_context
    snapshot = await _prepare_independent_destination_inputs(approved)
    output_ids = tuple(output.output_id for output in snapshot.x_outputs)
    action_at = datetime.now(UTC)

    async with approved.database.sessions() as session:
        planned, approval = await _plan_and_approve_x_intent(
            session,
            approved=approved,
            output_ids=output_ids,
            text="Scheduled cancellation",
            key_prefix="cancel-scheduled-ready",
            now=action_at,
            scheduled_at=action_at + timedelta(days=21),
        )
        release = await session.get(Release, approved.release_id)
        subject_approval = await session.scalar(
            select(SubjectApproval).where(SubjectApproval.is_current.is_(True)).limit(1)
        )
        assert release is not None and subject_approval is not None
        release.phase = ReleasePhase.PAUSED
        subject_approval.is_current = False
        await session.commit()

        cancellation = await cancel_publication_intent(
            session,
            intent_id=planned.intent_id,
            expected_intent_digest=planned.intent_digest,
            expected_lock_version=approval.intent_lock_version,
            actor_user_id=approved.owner_id,
            actor_role=AdminRole.OWNER,
            attestation=PUBLICATION_PRE_EFFECT_CANCELLATION_ATTESTATION,
            idempotency_key="cancel-scheduled-ready:cancel",
            now=action_at + timedelta(seconds=1),
        )
        intent = await session.get(PublicationIntent, planned.intent_id)
        attempt = await session.get(PublicationAttempt, approval.attempt_id)
        steps = tuple(
            (
                await session.scalars(
                    select(PublicationStep)
                    .where(PublicationStep.attempt_id == approval.attempt_id)
                    .order_by(PublicationStep.ordinal)
                )
            ).all()
        )
        approval_actions = tuple(
            (
                await session.scalars(
                    select(PublicationApproval.action)
                    .where(PublicationApproval.intent_id == planned.intent_id)
                    .order_by(PublicationApproval.revision)
                )
            ).all()
        )

    assert cancellation.state == PublicationIntentState.CANCELLED
    assert intent is not None and intent.state == PublicationIntentState.CANCELLED
    assert intent.completed_at is not None
    assert attempt is not None and attempt.state == PublicationAttemptState.CANCELLED
    assert attempt.completed_at is not None
    assert steps and all(step.state == PublicationStepState.CANCELLED for step in steps)
    assert approval_actions == (PublicationApprovalAction.APPROVE,)


@pytest.mark.asyncio
async def test_pre_effect_cancellation_rejects_durable_provider_request_evidence(
    derivative_approved_context: ApprovedContext,
) -> None:
    approved = derivative_approved_context
    snapshot = await _prepare_independent_destination_inputs(approved)
    output_ids = tuple(output.output_id for output in snapshot.x_outputs)
    action_at = datetime.now(UTC)

    async with approved.database.sessions() as session:
        planned, approval = await _plan_and_approve_x_intent(
            session,
            approved=approved,
            output_ids=output_ids,
            text="Provider request boundary",
            key_prefix="cancel-provider-boundary",
            now=action_at,
            scheduled_at=action_at + timedelta(days=7),
        )
        step = await session.scalar(
            select(PublicationStep)
            .where(
                PublicationStep.attempt_id == approval.attempt_id,
                PublicationStep.kind == PublicationStepKind.X_CREATE_POST,
            )
            .limit(1)
        )
        assert step is not None
        step_id = step.id
        session.add(
            PublicationEffectEvent(
                id=uuid4(),
                step_id=step.id,
                request_no=1,
                step_kind=step.kind,
                event_type="started",
                is_completion=False,
                guard_epoch=1,
                recorded_at=action_at + timedelta(seconds=1),
            )
        )
        await session.commit()

        with pytest.raises(PublicationConflictError, match="provider effect has started"):
            await cancel_publication_intent(
                session,
                intent_id=planned.intent_id,
                expected_intent_digest=planned.intent_digest,
                expected_lock_version=approval.intent_lock_version,
                actor_user_id=approved.owner_id,
                actor_role=AdminRole.OWNER,
                attestation=PUBLICATION_PRE_EFFECT_CANCELLATION_ATTESTATION,
                idempotency_key="cancel-provider-boundary:cancel",
                now=action_at + timedelta(seconds=2),
            )
        await session.rollback()

        intent = await session.get(PublicationIntent, planned.intent_id)
        attempt = await session.get(PublicationAttempt, approval.attempt_id)
        unchanged_step = await session.get(PublicationStep, step_id)
        approval_count = int(
            await session.scalar(
                select(func.count(PublicationApproval.id)).where(
                    PublicationApproval.intent_id == planned.intent_id
                )
            )
            or 0
        )

    assert intent is not None and intent.state == PublicationIntentState.READY
    assert attempt is not None and attempt.state == PublicationAttemptState.QUEUED
    assert unchanged_step is not None and unchanged_step.state == PublicationStepState.PENDING
    assert approval_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "error_code"),
    (
        ("cancel", "x_credentials_unavailable"),
        ("revoke", "x_credentials_unavailable"),
        ("cancel", "publication_guard_stopped"),
        ("revoke", "publication_guard_stopped"),
    ),
)
async def test_pre_provider_retry_timestamp_does_not_block_safe_stop(
    derivative_approved_context: ApprovedContext,
    operation: str,
    error_code: str,
) -> None:
    approved = derivative_approved_context
    snapshot = await _prepare_independent_destination_inputs(approved)
    output_ids = tuple(output.output_id for output in snapshot.x_outputs)
    action_at = datetime.now(UTC)

    async with approved.database.sessions() as session:
        planned, approval = await _plan_and_approve_x_intent(
            session,
            approved=approved,
            output_ids=output_ids,
            text=f"Safe stop after {error_code}",
            key_prefix=f"safe-pre-provider-stop:{operation}:{error_code}",
            now=action_at,
            scheduled_at=action_at + timedelta(days=3),
        )
        step_id = await _mark_pre_provider_retry(
            session,
            intent_id=planned.intent_id,
            attempt_id=approval.attempt_id,
            error_code=error_code,
            now=action_at + timedelta(seconds=1),
        )
        if operation == "cancel":
            result = await cancel_publication_intent(
                session,
                intent_id=planned.intent_id,
                expected_intent_digest=planned.intent_digest,
                expected_lock_version=approval.intent_lock_version,
                actor_user_id=approved.owner_id,
                actor_role=AdminRole.OWNER,
                attestation=PUBLICATION_PRE_EFFECT_CANCELLATION_ATTESTATION,
                idempotency_key=f"safe-pre-provider-stop:{error_code}:cancel",
                now=action_at + timedelta(seconds=2),
            )
            expected_intent_state = PublicationIntentState.CANCELLED
            expected_approval_count = 1
        else:
            result = await revoke_publication_intent(
                session,
                intent_id=planned.intent_id,
                expected_intent_digest=planned.intent_digest,
                expected_lock_version=approval.intent_lock_version,
                actor_user_id=approved.owner_id,
                actor_role=AdminRole.OWNER,
                attestation=PUBLICATION_REVOCATION_ATTESTATION,
                idempotency_key=f"safe-pre-provider-stop:{error_code}:revoke",
                now=action_at + timedelta(seconds=2),
            )
            expected_intent_state = PublicationIntentState.AWAITING_APPROVAL
            expected_approval_count = 2

        intent = await session.get(PublicationIntent, planned.intent_id)
        attempt = await session.get(PublicationAttempt, approval.attempt_id)
        step = await session.get(PublicationStep, step_id)
        effect_event_count = int(
            await session.scalar(
                select(func.count(PublicationEffectEvent.id)).where(
                    PublicationEffectEvent.step_id == step_id
                )
            )
            or 0
        )
        approval_count = int(
            await session.scalar(
                select(func.count(PublicationApproval.id)).where(
                    PublicationApproval.intent_id == planned.intent_id
                )
            )
            or 0
        )

    assert result.state == expected_intent_state
    assert intent is not None and intent.state == expected_intent_state
    assert attempt is not None and attempt.state == PublicationAttemptState.CANCELLED
    assert step is not None and step.state == PublicationStepState.CANCELLED
    assert step.effect_started_at is not None
    assert effect_event_count == 0
    assert approval_count == expected_approval_count


@pytest.mark.asyncio
async def test_retryable_provider_completion_can_be_cancelled_without_reconciliation(
    derivative_approved_context: ApprovedContext,
) -> None:
    approved = derivative_approved_context
    snapshot = await _prepare_independent_destination_inputs(approved)
    output_ids = tuple(output.output_id for output in snapshot.x_outputs)
    action_at = datetime.now(UTC)

    async with approved.database.sessions() as session:
        planned, approval = await _plan_and_approve_x_intent(
            session,
            approved=approved,
            output_ids=output_ids,
            text="Cancel after a proven retryable request",
            key_prefix="cancel-retryable-provider-request",
            now=action_at,
        )
        request_at = action_at + timedelta(seconds=1)
        step_id = await _mark_pre_provider_retry(
            session,
            intent_id=planned.intent_id,
            attempt_id=approval.attempt_id,
            error_code="x_media_upload_transport_retryable",
            now=request_at,
        )
        step = await session.get(PublicationStep, step_id)
        assert step is not None
        session.add_all(
            [
                PublicationEffectEvent(
                    id=uuid4(),
                    step_id=step_id,
                    request_no=1,
                    step_kind=step.kind,
                    event_type="started",
                    is_completion=False,
                    guard_epoch=1,
                    recorded_at=request_at,
                ),
                PublicationEffectEvent(
                    id=uuid4(),
                    step_id=step_id,
                    request_no=1,
                    step_kind=step.kind,
                    event_type="retryable",
                    is_completion=True,
                    guard_epoch=1,
                    error_code="x_media_upload_transport_retryable",
                    recorded_at=request_at + timedelta(milliseconds=1),
                ),
            ]
        )
        await session.commit()

        result = await cancel_publication_intent(
            session,
            intent_id=planned.intent_id,
            expected_intent_digest=planned.intent_digest,
            expected_lock_version=approval.intent_lock_version,
            actor_user_id=approved.owner_id,
            actor_role=AdminRole.OWNER,
            attestation=PUBLICATION_PRE_EFFECT_CANCELLATION_ATTESTATION,
            idempotency_key="cancel-retryable-provider-request:cancel",
            now=action_at + timedelta(seconds=2),
        )
        attempt = await session.get(PublicationAttempt, approval.attempt_id)
        cancelled_step = await session.get(PublicationStep, step_id)

    assert result.state == PublicationIntentState.CANCELLED
    assert attempt is not None and attempt.state == PublicationAttemptState.CANCELLED
    assert cancelled_step is not None
    assert cancelled_step.state == PublicationStepState.CANCELLED


@pytest.mark.asyncio
@pytest.mark.parametrize("completion_type", ("unknown", "succeeded"))
async def test_revocation_requires_reconciliation_for_unsafe_provider_completion(
    derivative_approved_context: ApprovedContext,
    completion_type: str,
) -> None:
    approved = derivative_approved_context
    snapshot = await _prepare_independent_destination_inputs(approved)
    output_ids = tuple(output.output_id for output in snapshot.x_outputs)
    action_at = datetime.now(UTC)

    async with approved.database.sessions() as session:
        planned, approval = await _plan_and_approve_x_intent(
            session,
            approved=approved,
            output_ids=output_ids,
            text=f"Unsafe {completion_type} completion",
            key_prefix=f"unsafe-provider-completion:{completion_type}",
            now=action_at,
        )
        request_at = action_at + timedelta(seconds=1)
        step_id = await _mark_pre_provider_retry(
            session,
            intent_id=planned.intent_id,
            attempt_id=approval.attempt_id,
            error_code=f"x_create_post_{completion_type}",
            now=request_at,
            step_kind=PublicationStepKind.X_CREATE_POST,
        )
        step = await session.get(PublicationStep, step_id)
        assert step is not None
        session.add_all(
            [
                PublicationEffectEvent(
                    id=uuid4(),
                    step_id=step_id,
                    request_no=1,
                    step_kind=step.kind,
                    event_type="started",
                    is_completion=False,
                    guard_epoch=1,
                    recorded_at=request_at,
                ),
                PublicationEffectEvent(
                    id=uuid4(),
                    step_id=step_id,
                    request_no=1,
                    step_kind=step.kind,
                    event_type=completion_type,
                    is_completion=True,
                    guard_epoch=1,
                    remote_identifier="123456789" if completion_type == "succeeded" else None,
                    error_code=(
                        "x_create_post_outcome_unknown" if completion_type == "unknown" else None
                    ),
                    recorded_at=request_at + timedelta(milliseconds=1),
                ),
            ]
        )
        await session.commit()

        result = await revoke_publication_intent(
            session,
            intent_id=planned.intent_id,
            expected_intent_digest=planned.intent_digest,
            expected_lock_version=approval.intent_lock_version,
            actor_user_id=approved.owner_id,
            actor_role=AdminRole.OWNER,
            attestation=PUBLICATION_REVOCATION_ATTESTATION,
            idempotency_key=f"unsafe-provider-completion:{completion_type}:revoke",
            now=action_at + timedelta(seconds=2),
        )
        attempt = await session.get(PublicationAttempt, approval.attempt_id)
        unsafe_step = await session.get(PublicationStep, step_id)

    assert result.state == PublicationIntentState.UNKNOWN
    assert attempt is not None and attempt.state == PublicationAttemptState.UNKNOWN
    assert unsafe_step is not None and unsafe_step.state == PublicationStepState.UNKNOWN


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ("cancel", "revoke"))
async def test_ambiguous_media_upload_does_not_require_post_reconciliation(
    derivative_approved_context: ApprovedContext,
    operation: str,
) -> None:
    approved = derivative_approved_context
    snapshot = await _prepare_independent_destination_inputs(approved)
    output_ids = tuple(output.output_id for output in snapshot.x_outputs)
    action_at = datetime.now(UTC)

    async with approved.database.sessions() as session:
        planned, approval = await _plan_and_approve_x_intent(
            session,
            approved=approved,
            output_ids=output_ids,
            text=f"Stop after ambiguous media upload via {operation}",
            key_prefix=f"ambiguous-media-upload:{operation}",
            now=action_at,
        )
        request_at = action_at + timedelta(seconds=1)
        upload_step_id = await _mark_pre_provider_retry(
            session,
            intent_id=planned.intent_id,
            attempt_id=approval.attempt_id,
            error_code="x_media_upload_outcome_unknown",
            now=request_at,
            step_kind=PublicationStepKind.X_MEDIA_UPLOAD,
        )
        upload_step = await session.get(PublicationStep, upload_step_id)
        assert upload_step is not None
        session.add_all(
            [
                PublicationEffectEvent(
                    id=uuid4(),
                    step_id=upload_step_id,
                    request_no=1,
                    step_kind=upload_step.kind,
                    event_type="started",
                    is_completion=False,
                    guard_epoch=1,
                    recorded_at=request_at,
                ),
                PublicationEffectEvent(
                    id=uuid4(),
                    step_id=upload_step_id,
                    request_no=1,
                    step_kind=upload_step.kind,
                    event_type="unknown",
                    is_completion=True,
                    guard_epoch=1,
                    error_code="x_media_upload_outcome_unknown",
                    recorded_at=request_at + timedelta(milliseconds=1),
                ),
            ]
        )
        await session.commit()

        if operation == "cancel":
            result = await cancel_publication_intent(
                session,
                intent_id=planned.intent_id,
                expected_intent_digest=planned.intent_digest,
                expected_lock_version=approval.intent_lock_version,
                actor_user_id=approved.owner_id,
                actor_role=AdminRole.OWNER,
                attestation=PUBLICATION_PRE_EFFECT_CANCELLATION_ATTESTATION,
                idempotency_key="ambiguous-media-upload:cancel",
                now=action_at + timedelta(seconds=2),
            )
            expected_intent_state = PublicationIntentState.CANCELLED
        else:
            result = await revoke_publication_intent(
                session,
                intent_id=planned.intent_id,
                expected_intent_digest=planned.intent_digest,
                expected_lock_version=approval.intent_lock_version,
                actor_user_id=approved.owner_id,
                actor_role=AdminRole.OWNER,
                attestation=PUBLICATION_REVOCATION_ATTESTATION,
                idempotency_key="ambiguous-media-upload:revoke",
                now=action_at + timedelta(seconds=2),
            )
            expected_intent_state = PublicationIntentState.AWAITING_APPROVAL
        attempt = await session.get(PublicationAttempt, approval.attempt_id)
        stopped_upload = await session.get(PublicationStep, upload_step_id)

    assert result.state == expected_intent_state
    assert attempt is not None and attempt.state == PublicationAttemptState.CANCELLED
    assert stopped_upload is not None
    assert stopped_upload.state == PublicationStepState.CANCELLED


@pytest.mark.asyncio
async def test_revocation_preserves_resolved_upload_and_cancels_remaining_post(
    derivative_approved_context: ApprovedContext,
) -> None:
    approved = derivative_approved_context
    snapshot = await _prepare_independent_destination_inputs(approved)
    output_ids = tuple(output.output_id for output in snapshot.x_outputs)
    action_at = datetime.now(UTC)

    async with approved.database.sessions() as session:
        planned, approval = await _plan_and_approve_x_intent(
            session,
            approved=approved,
            output_ids=output_ids,
            text="Preserve completed upload while revoking post",
            key_prefix="revoke-after-resolved-upload",
            now=action_at,
        )
        attempt = await session.get(PublicationAttempt, approval.attempt_id)
        upload_step = await session.scalar(
            select(PublicationStep).where(
                PublicationStep.attempt_id == approval.attempt_id,
                PublicationStep.kind == PublicationStepKind.X_MEDIA_UPLOAD,
            )
        )
        post_step = await session.scalar(
            select(PublicationStep).where(
                PublicationStep.attempt_id == approval.attempt_id,
                PublicationStep.kind == PublicationStepKind.X_CREATE_POST,
            )
        )
        assert attempt is not None and upload_step is not None and post_step is not None
        upload_step_id = upload_step.id
        post_step_id = post_step.id
        request_at = action_at + timedelta(seconds=1)
        retry_at = action_at + timedelta(minutes=5)
        attempt.state = PublicationAttemptState.RETRY_WAIT
        attempt.attempt_count = 1
        attempt.retry_at = retry_at
        attempt.last_error_code = "awaiting_post_retry"
        upload_step.state = PublicationStepState.SUCCEEDED
        upload_step.effect_started_at = request_at
        upload_step.effect_completed_at = request_at + timedelta(milliseconds=1)
        upload_step.guard_epoch = 1
        upload_step.remote_identifier = "987654321"
        upload_step.last_error_code = None
        upload_step.last_error_detail = None
        upload_step.updated_at = request_at + timedelta(milliseconds=1)
        session.add_all(
            [
                PublicationEffectEvent(
                    id=uuid4(),
                    step_id=upload_step.id,
                    request_no=1,
                    step_kind=upload_step.kind,
                    event_type="started",
                    is_completion=False,
                    guard_epoch=1,
                    recorded_at=request_at,
                ),
                PublicationEffectEvent(
                    id=uuid4(),
                    step_id=upload_step.id,
                    request_no=1,
                    step_kind=upload_step.kind,
                    event_type="succeeded",
                    is_completion=True,
                    guard_epoch=1,
                    remote_identifier="987654321",
                    recorded_at=request_at + timedelta(milliseconds=1),
                ),
            ]
        )
        await session.commit()

        result = await revoke_publication_intent(
            session,
            intent_id=planned.intent_id,
            expected_intent_digest=planned.intent_digest,
            expected_lock_version=approval.intent_lock_version,
            actor_user_id=approved.owner_id,
            actor_role=AdminRole.OWNER,
            attestation=PUBLICATION_REVOCATION_ATTESTATION,
            idempotency_key="revoke-after-resolved-upload:revoke",
            now=action_at + timedelta(seconds=2),
        )
        revoked_attempt = await session.get(PublicationAttempt, approval.attempt_id)
        preserved_upload = await session.get(PublicationStep, upload_step_id)
        cancelled_post = await session.get(PublicationStep, post_step_id)

    assert result.state == PublicationIntentState.AWAITING_APPROVAL
    assert revoked_attempt is not None
    assert revoked_attempt.state == PublicationAttemptState.CANCELLED
    assert preserved_upload is not None
    assert preserved_upload.state == PublicationStepState.SUCCEEDED
    assert preserved_upload.remote_identifier == "987654321"
    assert cancelled_post is not None
    assert cancelled_post.state == PublicationStepState.CANCELLED


@pytest.mark.asyncio
async def test_confirmed_absent_unknown_can_be_reapproved_revoked_and_cancelled(
    derivative_approved_context: ApprovedContext,
) -> None:
    approved = derivative_approved_context
    snapshot = await _prepare_independent_destination_inputs(approved)
    output_ids = tuple(output.output_id for output in snapshot.x_outputs)
    action_at = datetime.now(UTC)

    async with approved.database.sessions() as session:
        planned, first_approval = await _plan_and_approve_x_intent(
            session,
            approved=approved,
            output_ids=output_ids,
            text="Reconcile absent before retry",
            key_prefix="confirmed-absent-retry",
            now=action_at,
        )
        intent = await session.get(PublicationIntent, planned.intent_id)
        first_attempt = await session.get(PublicationAttempt, first_approval.attempt_id)
        unknown_step = await session.scalar(
            select(PublicationStep)
            .where(
                PublicationStep.attempt_id == first_approval.attempt_id,
                PublicationStep.kind == PublicationStepKind.X_CREATE_POST,
            )
            .limit(1)
        )
        assert intent is not None and first_attempt is not None and unknown_step is not None
        unknown_step_id = unknown_step.id
        unknown_at = action_at + timedelta(seconds=1)
        intent.state = PublicationIntentState.UNKNOWN
        intent.last_error_code = "x_create_post_outcome_unknown"
        first_attempt.state = PublicationAttemptState.UNKNOWN
        first_attempt.last_error_code = "x_create_post_outcome_unknown"
        unknown_step.state = PublicationStepState.UNKNOWN
        unknown_step.retry_class = PublicationRetryClass.UNKNOWN
        unknown_step.effect_started_at = unknown_at
        unknown_step.last_error_code = "x_create_post_outcome_unknown"
        session.add_all(
            [
                PublicationEffectEvent(
                    id=uuid4(),
                    step_id=unknown_step.id,
                    request_no=1,
                    step_kind=unknown_step.kind,
                    event_type="started",
                    is_completion=False,
                    guard_epoch=1,
                    recorded_at=unknown_at,
                ),
                PublicationEffectEvent(
                    id=uuid4(),
                    step_id=unknown_step.id,
                    request_no=1,
                    step_kind=unknown_step.kind,
                    event_type="unknown",
                    is_completion=True,
                    guard_epoch=1,
                    error_code="x_create_post_outcome_unknown",
                    recorded_at=unknown_at + timedelta(milliseconds=1),
                ),
            ]
        )
        await session.commit()

        reconciled = await reconcile_publication_absent(
            session,
            intent_id=planned.intent_id,
            expected_intent_digest=planned.intent_digest,
            expected_lock_version=first_approval.intent_lock_version,
            evidence="Manual provider history search found no matching post.",
            attestation=PUBLICATION_CONFIRM_ABSENT_ATTESTATION,
            actor_user_id=approved.owner_id,
            actor_role=AdminRole.OWNER,
            idempotency_key="confirmed-absent-retry:reconcile",
            now=action_at + timedelta(seconds=2),
        )
        resolved_attempt = await session.get(PublicationAttempt, first_approval.attempt_id)
        resolved_steps = tuple(
            (
                await session.scalars(
                    select(PublicationStep)
                    .where(PublicationStep.attempt_id == first_approval.attempt_id)
                    .order_by(PublicationStep.ordinal)
                )
            ).all()
        )
        resolved_unknown_step = await session.get(PublicationStep, unknown_step_id)

        second_approval = await approve_publication_intent(
            session,
            intent_id=planned.intent_id,
            expected_intent_digest=planned.intent_digest,
            expected_lock_version=reconciled.lock_version,
            actor_user_id=approved.owner_id,
            actor_role=AdminRole.OWNER,
            attestation=PUBLICATION_EFFECT_APPROVAL_ATTESTATION,
            approval_seconds=900,
            idempotency_key="confirmed-absent-retry:second-approval",
            now=action_at + timedelta(seconds=3),
        )
        revoked = await revoke_publication_intent(
            session,
            intent_id=planned.intent_id,
            expected_intent_digest=planned.intent_digest,
            expected_lock_version=second_approval.intent_lock_version,
            actor_user_id=approved.owner_id,
            actor_role=AdminRole.OWNER,
            attestation=PUBLICATION_REVOCATION_ATTESTATION,
            idempotency_key="confirmed-absent-retry:revoke",
            now=action_at + timedelta(seconds=4),
        )
        third_approval = await approve_publication_intent(
            session,
            intent_id=planned.intent_id,
            expected_intent_digest=planned.intent_digest,
            expected_lock_version=revoked.intent_lock_version,
            actor_user_id=approved.owner_id,
            actor_role=AdminRole.OWNER,
            attestation=PUBLICATION_EFFECT_APPROVAL_ATTESTATION,
            approval_seconds=900,
            idempotency_key="confirmed-absent-retry:third-approval",
            now=action_at + timedelta(seconds=5),
        )
        cancelled = await cancel_publication_intent(
            session,
            intent_id=planned.intent_id,
            expected_intent_digest=planned.intent_digest,
            expected_lock_version=third_approval.intent_lock_version,
            actor_user_id=approved.owner_id,
            actor_role=AdminRole.OWNER,
            attestation=PUBLICATION_PRE_EFFECT_CANCELLATION_ATTESTATION,
            idempotency_key="confirmed-absent-retry:cancel",
            now=action_at + timedelta(seconds=6),
        )
        attempts = tuple(
            (
                await session.scalars(
                    select(PublicationAttempt)
                    .where(PublicationAttempt.intent_id == planned.intent_id)
                    .order_by(PublicationAttempt.attempt_no)
                )
            ).all()
        )

    assert reconciled.state == PublicationIntentState.AWAITING_APPROVAL
    assert resolved_attempt is not None
    assert resolved_attempt.state == PublicationAttemptState.FAILED
    assert resolved_attempt.completed_at is not None
    assert resolved_unknown_step is not None
    assert resolved_unknown_step.state == PublicationStepState.FAILED
    assert resolved_unknown_step.effect_completed_at is not None
    assert resolved_steps
    assert all(
        step.state
        in {
            PublicationStepState.SUCCEEDED,
            PublicationStepState.FAILED,
            PublicationStepState.CANCELLED,
        }
        for step in resolved_steps
    )
    assert revoked.state == PublicationIntentState.AWAITING_APPROVAL
    assert cancelled.state == PublicationIntentState.CANCELLED
    assert [attempt.state for attempt in attempts] == [
        PublicationAttemptState.FAILED,
        PublicationAttemptState.CANCELLED,
        PublicationAttemptState.CANCELLED,
    ]
