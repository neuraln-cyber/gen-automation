# ruff: noqa: F811

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request
from starlette.responses import Response

from gen_automation.api.routes.delivery_dashboard import dashboard_review_delivery
from gen_automation.config import Environment, Settings
from gen_automation.db.models import (
    AuditEvent,
    PublicationAttempt,
    PublicationEffectEvent,
    PublicationIntent,
    PublicationReconciliation,
    PublicationStep,
    ReleaseVersion,
)
from gen_automation.domain.canonical import canonical_sha256
from gen_automation.domain.enums import (
    AdminRole,
    PublicationAttemptState,
    PublicationIntentState,
    PublicationStepKind,
    PublicationStepState,
)
from gen_automation.domain.release_spec import ReleaseSpecification
from gen_automation.integrations.patreon import (
    PatreonDriverOutcome,
    PatreonDriverRequest,
    PatreonDriverResult,
)
from gen_automation.services.authentication import AuthenticatedPrincipal
from gen_automation.services.operator_delivery import (
    load_operator_delivery,
    prepare_operator_destinations,
)
from gen_automation.services.publication import (
    PUBLICATION_CONFIRM_PATREON_ABSENT_ATTESTATION,
    PUBLICATION_CONFIRM_PRESENT_ATTESTATION,
    presign_patreon_package_download,
    reconcile_publication_absent,
    reconcile_publication_present,
    set_publication_guard,
)
from gen_automation.services.publication_runtime import run_publication_cycle
from tests.factories import seed_release_approvals, valid_release_payload
from tests.test_derivative_pipeline import ApprovedContext
from tests.test_derivative_pipeline import (
    approved_context as derivative_approved_context,  # noqa: F401
)
from tests.test_derivative_runtime import PreparedRuntime, _cycle, _prepare


@dataclass(frozen=True, slots=True)
class UnknownPatreonContext:
    approved: ApprovedContext
    prepared: PreparedRuntime
    intent_id: UUID
    intent_digest: str
    intent_lock_version: int
    action_at: datetime


@pytest.fixture
async def unknown_patreon_context(
    derivative_approved_context: ApprovedContext,
) -> AsyncIterator[UnknownPatreonContext]:
    approved = derivative_approved_context
    prepared = await _prepare(approved)
    await _cycle(prepared, worker_id="patreon-recovery-derivative")
    await _cycle(prepared, worker_id="patreon-recovery-derivative")

    payload = valid_release_payload()
    specification = ReleaseSpecification.model_validate(payload["specification"])
    action_at = datetime.now(UTC)
    async with approved.database.sessions() as session:
        await seed_release_approvals(session, payload)
        version = await session.get(ReleaseVersion, approved.release_version_id)
        assert version is not None
        version.specification = specification.model_dump(mode="json")
        version.specification_sha256 = canonical_sha256(specification)
        await session.commit()
        await set_publication_guard(
            session,
            enabled=True,
            expected_epoch=1,
            expected_lock_version=1,
            reason="Patreon recovery test",
            actor_user_id=approved.owner_id,
            actor_role=AdminRole.OWNER,
            idempotency_key="patreon-recovery-enable-guard",
            now=action_at,
        )
        snapshot = await load_operator_delivery(
            session,
            review_task_id=approved.review_task_id,
        )
        destinations = await prepare_operator_destinations(
            session,
            review_task_id=approved.review_task_id,
            patreon_title="Recovery set",
            patreon_body="Exact immutable set.",
            patreon_tier="Paid members",
            patreon_tags=("recovery",),
            public_preview_output_id=snapshot.full_outputs[0].output_id,
            public_preview_attester_name="Derivative Owner",
            public_preview_attested_at=action_at,
            x_text="",
            x_credential_reference=None,
            actor_user_id=approved.owner_id,
            actor_role=AdminRole.OWNER,
            idempotency_key="patreon-recovery-prepare",
            now=action_at,
        )

    class UnknownDriver:
        async def publish(self, request: PatreonDriverRequest) -> PatreonDriverResult:
            assert request.intent_id == destinations.patreon_intent_id
            assert request.package_path.is_file()
            return PatreonDriverResult(
                outcome=PatreonDriverOutcome.UNKNOWN,
                detail_code="confirmation_lost",
            )

    result = await run_publication_cycle(
        approved.database.sessions,
        prepared.store,
        worker_id="patreon-recovery-publication",
        x_oauth_provider=None,
        expected_x_creator_user_id=None,
        lease_seconds=600,
        retry_base_seconds=30,
        retry_max_seconds=900,
        patreon_driver=UnknownDriver(),
        patreon_browser_profile_reference="creator-main",
        now=action_at + timedelta(minutes=1),
    )
    assert result.state == PublicationAttemptState.UNKNOWN

    async with approved.database.sessions() as session:
        intent = await session.get(PublicationIntent, destinations.patreon_intent_id)
        assert intent is not None
        assert intent.state == PublicationIntentState.UNKNOWN
        context = UnknownPatreonContext(
            approved=approved,
            prepared=prepared,
            intent_id=intent.id,
            intent_digest=intent.intent_digest,
            intent_lock_version=intent.lock_version,
            action_at=action_at,
        )
    yield context


@pytest.mark.asyncio
async def test_unknown_patreon_can_be_confirmed_present_idempotently(
    unknown_patreon_context: UnknownPatreonContext,
) -> None:
    context = unknown_patreon_context
    service_kwargs = {
        "intent_id": context.intent_id,
        "expected_intent_digest": context.intent_digest,
        "expected_lock_version": context.intent_lock_version,
        "remote_identifier": "12345",
        "remote_url": "https://www.patreon.com/posts/recovered-12345",
        "evidence": "Creator page and paid post contents checked.",
        "attestation": PUBLICATION_CONFIRM_PRESENT_ATTESTATION,
        "actor_user_id": context.approved.owner_id,
        "actor_role": AdminRole.OWNER,
        "idempotency_key": "patreon-confirm-present-recovery",
        "now": context.action_at + timedelta(minutes=2),
    }
    async with context.approved.database.sessions() as session:
        result = await reconcile_publication_present(session, **service_kwargs)
        replay = await reconcile_publication_present(session, **service_kwargs)
        intent = await session.get(PublicationIntent, context.intent_id)
        attempt = await session.scalar(
            select(PublicationAttempt).where(PublicationAttempt.intent_id == context.intent_id)
        )
        step = await session.scalar(
            select(PublicationStep).where(
                PublicationStep.attempt_id == attempt.id,
                PublicationStep.kind == PublicationStepKind.PATREON_HANDOFF,
            )
        )
        reconciliation_count = await session.scalar(
            select(func.count(PublicationReconciliation.id)).where(
                PublicationReconciliation.intent_id == context.intent_id
            )
        )

    assert result.state == PublicationIntentState.PUBLISHED
    assert replay.replayed
    assert replay.reconciliation_id == result.reconciliation_id
    assert reconciliation_count == 1
    assert intent is not None and intent.state == PublicationIntentState.PUBLISHED
    assert attempt is not None and attempt.state == PublicationAttemptState.SUCCEEDED
    assert step is not None and step.state == PublicationStepState.SUCCEEDED
    assert step.remote_identifier == "12345"
    assert step.remote_url == "https://www.patreon.com/posts/recovered-12345"


@pytest.mark.asyncio
async def test_unknown_patreon_absent_opens_download_without_retry(
    unknown_patreon_context: UnknownPatreonContext,
) -> None:
    context = unknown_patreon_context
    async with context.approved.database.sessions() as session:
        attempts_before = await session.scalar(select(func.count(PublicationAttempt.id)))
        effects_before = await session.scalar(select(func.count(PublicationEffectEvent.id)))
        before = await load_operator_delivery(
            session,
            review_task_id=context.approved.review_task_id,
        )
        assert next(item for item in before.destinations if item.key == "patreon").state == (
            "unknown"
        )
        page = await _delivery_page(context, session)
        html = bytes(page.body).decode()
        assert f"/patreon/{context.intent_id}:confirm-present" in html
        assert f"/patreon/{context.intent_id}:confirm-absent" in html

        service_kwargs = {
            "intent_id": context.intent_id,
            "expected_intent_digest": context.intent_digest,
            "expected_lock_version": context.intent_lock_version,
            "evidence": "Creator posts list, drafts, and scheduled posts checked.",
            "attestation": PUBLICATION_CONFIRM_PATREON_ABSENT_ATTESTATION,
            "actor_user_id": context.approved.owner_id,
            "actor_role": AdminRole.OWNER,
            "idempotency_key": "patreon-confirm-absent-recovery",
            "now": context.action_at + timedelta(minutes=2),
        }
        result = await reconcile_publication_absent(session, **service_kwargs)
        replay = await reconcile_publication_absent(session, **service_kwargs)
        assert replay.replayed

        download = await presign_patreon_package_download(
            session,
            context.prepared.store,
            intent_id=context.intent_id,
            expected_intent_digest=context.intent_digest,
            expected_lock_version=result.lock_version,
            actor_user_id=context.approved.owner_id,
            actor_role=AdminRole.OWNER,
            now=context.action_at + timedelta(minutes=3),
        )
        intent = await session.get(PublicationIntent, context.intent_id)
        attempt = await session.scalar(
            select(PublicationAttempt).where(PublicationAttempt.intent_id == context.intent_id)
        )
        assert attempt is not None
        step = await session.scalar(
            select(PublicationStep).where(
                PublicationStep.attempt_id == attempt.id,
                PublicationStep.kind == PublicationStepKind.PATREON_HANDOFF,
            )
        )
        attempts_after = await session.scalar(select(func.count(PublicationAttempt.id)))
        effects_after = await session.scalar(select(func.count(PublicationEffectEvent.id)))
        audit = await session.scalar(
            select(AuditEvent)
            .where(
                AuditEvent.resource_id == context.intent_id,
                AuditEvent.action == "publication.patreon_package_download_authorized",
            )
            .order_by(AuditEvent.occurred_at.desc())
            .limit(1)
        )
        after = await load_operator_delivery(
            session,
            review_task_id=context.approved.review_task_id,
        )
        page = await _delivery_page(context, session)
        recovered_html = bytes(page.body).decode()

    assert result.state == PublicationIntentState.AWAITING_HUMAN
    assert intent is not None and intent.state == PublicationIntentState.AWAITING_HUMAN
    assert attempt.state == PublicationAttemptState.AWAITING_HUMAN
    assert step is not None and step.state == PublicationStepState.AWAITING_HUMAN
    assert attempts_after == attempts_before
    assert effects_after == effects_before
    assert f"version={download.package_id}" not in download.url
    assert "version=" in download.url
    assert audit is not None
    assert audit.detail["authorization_basis"] == "confirmed_absent_reconciliation"
    assert next(item for item in after.destinations if item.key == "patreon").state == "ready"
    assert f"/finished-set/{context.intent_id}:download" in recovered_html
    assert "Download finished ranked set (.zip)" in recovered_html
    assert f"/patreon/{context.intent_id}:confirm-present" in recovered_html
    assert f"/patreon/{context.intent_id}:confirm-absent" not in recovered_html

    no_retry = await run_publication_cycle(
        context.approved.database.sessions,
        context.prepared.store,
        worker_id="patreon-recovery-no-retry",
        x_oauth_provider=None,
        expected_x_creator_user_id=None,
        lease_seconds=600,
        retry_base_seconds=30,
        retry_max_seconds=900,
        patreon_driver=None,
        patreon_browser_profile_reference=None,
        now=context.action_at + timedelta(minutes=4),
    )
    assert not no_retry.did_work


async def _delivery_page(
    context: UnknownPatreonContext,
    session: AsyncSession,
) -> Response:
    app = FastAPI()
    app.state.settings = Settings(
        environment=Environment.TEST,
        auth_enabled=False,
        auth_development_bypass_enabled=True,
    )
    app.state.object_store = context.prepared.store
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": (f"/dashboard/review-tasks/{context.approved.review_task_id}/delivery"),
            "headers": [],
            "app": app,
        }
    )
    principal = AuthenticatedPrincipal(
        session_id=uuid4(),
        user_id=context.approved.owner_id,
        username="derivative-owner",
        display_name="Derivative Owner",
        role=AdminRole.OWNER,
        csrf_sha256="a" * 64,
        expires_at=context.action_at + timedelta(hours=1),
        idle_expires_at=context.action_at + timedelta(hours=1),
        reauthenticated_at=context.action_at,
        mfa_verified_at=context.action_at,
    )
    return await dashboard_review_delivery(
        review_task_id=context.approved.review_task_id,
        request=request,
        session=session,
        principal=principal,
    )
