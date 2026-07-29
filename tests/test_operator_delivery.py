# ruff: noqa: F811

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import FastAPI
from sqlalchemy import select
from starlette.requests import Request

from gen_automation.api.routes.delivery_dashboard import dashboard_review_delivery
from gen_automation.config import Environment, Settings
from gen_automation.db.models import (
    PublicationAttempt,
    PublicationInput,
    PublicationIntent,
    ReleaseVersion,
)
from gen_automation.domain.canonical import canonical_sha256
from gen_automation.domain.enums import (
    AdminRole,
    PublicationAttemptState,
    PublicationIntentState,
    PublicationTarget,
)
from gen_automation.domain.release_spec import ReleaseSpecification
from gen_automation.services.authentication import AuthenticatedPrincipal
from gen_automation.services.operator_delivery import (
    load_operator_delivery,
    prepare_operator_destinations,
)
from gen_automation.services.publication import set_publication_guard
from tests.factories import seed_release_approvals, valid_release_payload
from tests.test_derivative_pipeline import ApprovedContext
from tests.test_derivative_pipeline import (
    approved_context as derivative_approved_context,  # noqa: F401
)
from tests.test_derivative_runtime import _cycle, _prepare


@pytest.mark.asyncio
async def test_completed_review_prepares_exact_patreon_mega_and_x_destinations(
    derivative_approved_context: ApprovedContext,
) -> None:
    approved = derivative_approved_context
    prepared = await _prepare(
        approved,
        with_watermark=True,
        x_selected_asset_ids=(approved.raw_asset_ids[0],),
    )
    await _cycle(prepared, worker_id="delivery-derivative")
    await _cycle(prepared, worker_id="delivery-derivative")

    payload = valid_release_payload()
    specification = ReleaseSpecification.model_validate(payload["specification"])
    async with approved.database.sessions() as session:
        await seed_release_approvals(session, payload)
        version = await session.get(ReleaseVersion, approved.release_version_id)
        assert version is not None
        version.specification = specification.model_dump(mode="json")
        version.specification_sha256 = canonical_sha256(specification)
        await session.commit()

        guard = await set_publication_guard(
            session,
            enabled=True,
            expected_epoch=1,
            expected_lock_version=1,
            reason="focused delivery test",
            actor_user_id=approved.owner_id,
            actor_role=AdminRole.OWNER,
            idempotency_key="operator-delivery-enable-guard",
        )
        assert guard.enabled
        before = await load_operator_delivery(
            session,
            review_task_id=approved.review_task_id,
        )
        assert before.progress.ready_for_destinations
        assert [output.display_order for output in before.full_outputs] == [1, 2]
        assert len(before.x_outputs) == 1

        action_at = datetime.now(UTC)
        result = await prepare_operator_destinations(
            session,
            review_task_id=approved.review_task_id,
            patreon_title="Derivative release",
            patreon_body="The complete set.",
            patreon_tier="Paid members",
            patreon_tags=("art", "set"),
            public_preview_output_id=before.full_outputs[0].output_id,
            public_preview_attester_name="Derivative Owner",
            public_preview_attested_at=action_at,
            x_text="New set",
            x_credential_reference="test://x/oauth",
            actor_user_id=approved.owner_id,
            actor_role=AdminRole.OWNER,
            idempotency_key="operator-delivery-prepare",
            now=action_at,
        )
        assert result.x_intent_id is not None

        intents = tuple(
            (
                await session.scalars(select(PublicationIntent).order_by(PublicationIntent.target))
            ).all()
        )
        assert {intent.target for intent in intents} == {
            PublicationTarget.PATREON,
            PublicationTarget.X,
        }
        assert all(intent.state == PublicationIntentState.READY for intent in intents)
        attempts = tuple((await session.scalars(select(PublicationAttempt))).all())
        assert len(attempts) == 2
        assert all(attempt.state == PublicationAttemptState.QUEUED for attempt in attempts)

        patreon = next(intent for intent in intents if intent.target == PublicationTarget.PATREON)
        x = next(intent for intent in intents if intent.target == PublicationTarget.X)
        patreon_roles = tuple(
            (
                await session.scalars(
                    select(PublicationInput.role)
                    .where(PublicationInput.intent_id == patreon.id)
                    .order_by(PublicationInput.ordinal)
                )
            ).all()
        )
        assert patreon_roles == (
            "patreon_content",
            "patreon_content",
            "patreon_preview",
        )
        x_roles = tuple(
            (
                await session.scalars(
                    select(PublicationInput.role)
                    .where(PublicationInput.intent_id == x.id)
                    .order_by(PublicationInput.ordinal)
                )
            ).all()
        )
        assert x_roles == ("x_teaser",)

        replay = await prepare_operator_destinations(
            session,
            review_task_id=approved.review_task_id,
            patreon_title="Derivative release",
            patreon_body="The complete set.",
            patreon_tier="Paid members",
            patreon_tags=("art", "set"),
            public_preview_output_id=before.full_outputs[0].output_id,
            public_preview_attester_name="Derivative Owner",
            public_preview_attested_at=action_at,
            x_text="New set",
            x_credential_reference="test://x/oauth",
            actor_user_id=approved.owner_id,
            actor_role=AdminRole.OWNER,
            idempotency_key="operator-delivery-prepare",
            now=action_at,
        )
        assert replay.patreon_intent_id == result.patreon_intent_id
        assert replay.x_intent_id == result.x_intent_id
        assert replay.replayed

        app = FastAPI()
        app.state.settings = Settings(
            environment=Environment.TEST,
            auth_enabled=False,
            auth_development_bypass_enabled=True,
        )
        app.state.object_store = prepared.store
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": f"/dashboard/review-tasks/{approved.review_task_id}/delivery",
                "headers": [],
                "app": app,
            }
        )
        principal = AuthenticatedPrincipal(
            session_id=uuid4(),
            user_id=approved.owner_id,
            username="derivative-owner",
            display_name="Derivative Owner",
            role=AdminRole.OWNER,
            csrf_sha256="a" * 64,
            expires_at=action_at,
            idle_expires_at=action_at,
            reauthenticated_at=action_at,
            mfa_verified_at=action_at,
        )
        page = await dashboard_review_delivery(
            review_task_id=approved.review_task_id,
            request=request,
            session=session,
            principal=principal,
        )
        assert page.status_code == 200
        html = bytes(page.body).decode()
        assert "Prepare destinations" in html
        assert "Patreon" in html
        assert "MEGA" in html
        assert "X" in html
        assert "persistent signed-in Patreon session" in html
