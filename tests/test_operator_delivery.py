# ruff: noqa: F811

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlencode
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from sqlalchemy import select
from starlette.requests import Request

from gen_automation.api.browser_delivery_forms import (
    delivery_csrf_token,
    delivery_form_key,
)
from gen_automation.api.routes import delivery_dashboard as delivery_routes
from gen_automation.api.routes.delivery_dashboard import (
    dashboard_change_publication_guard,
    dashboard_review_delivery,
)
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
    FinishedSetArchiveState,
    MegaDeliveryState,
    PublicationAttemptState,
    PublicationIntentState,
    PublicationTarget,
)
from gen_automation.domain.release_spec import ReleaseSpecification
from gen_automation.services.authentication import AuthenticatedPrincipal
from gen_automation.services.operator_delivery import (
    _mega_destination,
    load_operator_delivery,
    prepare_operator_destinations,
)
from gen_automation.services.publication import get_publication_guard, set_publication_guard
from tests.factories import seed_release_approvals, valid_release_payload
from tests.test_derivative_pipeline import ApprovedContext
from tests.test_derivative_pipeline import (
    approved_context as derivative_approved_context,  # noqa: F401
)
from tests.test_derivative_runtime import _cycle, _prepare


def _owner_principal(owner_id: UUID) -> AuthenticatedPrincipal:
    now = datetime.now(UTC)
    return AuthenticatedPrincipal(
        session_id=uuid4(),
        user_id=owner_id,
        username="derivative-owner",
        display_name="Derivative Owner",
        role=AdminRole.OWNER,
        csrf_sha256="a" * 64,
        expires_at=now,
        idle_expires_at=now,
        reauthenticated_at=now,
        mfa_verified_at=now,
    )


def _publication_guard_request(
    app: FastAPI,
    *,
    review_task_id: UUID,
    fields: dict[str, str],
) -> Request:
    body = urlencode(fields).encode()

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": f"/dashboard/review-tasks/{review_task_id}/delivery:publication-guard",
            "headers": [
                (b"content-type", b"application/x-www-form-urlencoded"),
                (b"content-length", str(len(body)).encode()),
            ],
            "app": app,
        },
        receive,
    )


def _publication_guard_fields(
    settings: Settings,
    principal: AuthenticatedPrincipal,
    *,
    review_task_id: UUID,
    enabled: bool,
    epoch: int,
    lock_version: int,
    submission_id: UUID | None = None,
) -> dict[str, str]:
    submission = submission_id or uuid4()
    enabled_value = "true" if enabled else "false"
    return {
        "csrf_token": delivery_csrf_token(settings, session_id=principal.session_id),
        "idempotency_key": delivery_form_key(
            settings,
            session_id=principal.session_id,
            action="publication-guard",
            parts=(
                str(review_task_id),
                str(submission),
                str(epoch),
                str(lock_version),
                "enabled" if enabled else "stopped",
            ),
        ),
        "submission_id": str(submission),
        "enabled": enabled_value,
        "expected_epoch": str(epoch),
        "expected_lock_version": str(lock_version),
        "reason": "Focused browser publication guard test",
    }


@pytest.mark.asyncio
async def test_mega_projection_is_independent_and_reports_extracted_image_progress() -> None:
    archive_id = uuid4()
    session = SimpleNamespace(
        scalar=AsyncMock(
            side_effect=[
                SimpleNamespace(
                    id=archive_id,
                    state=FinishedSetArchiveState.READY,
                    selection_count=250,
                ),
                SimpleNamespace(
                    state=MegaDeliveryState.CLAIMED,
                    uploaded_item_count=90,
                    total_item_count=250,
                    remote_folder="/Finished Sets/Yoruichi/v01-deadbeef",
                ),
            ]
        )
    )

    destination = await _mega_destination(  # type: ignore[arg-type]
        session,
        release_version_id=uuid4(),
    )

    assert destination.state == "running"
    assert destination.completed_items == 90
    assert destination.total_items == 250
    assert destination.remote_path == "/Finished Sets/Yoruichi/v01-deadbeef"
    assert "90 / 250" in destination.detail
    assert destination.intent_id is None
    assert session.scalar.await_count == 2


@pytest.mark.asyncio
async def test_mega_projection_auto_queues_ready_archive_without_patreon() -> None:
    session = SimpleNamespace(
        scalar=AsyncMock(
            side_effect=[
                SimpleNamespace(
                    id=uuid4(),
                    state=FinishedSetArchiveState.READY,
                    selection_count=400,
                ),
                None,
            ]
        )
    )

    destination = await _mega_destination(  # type: ignore[arg-type]
        session,
        release_version_id=uuid4(),
    )

    assert destination.state == "queued"
    assert destination.completed_items == 0
    assert destination.total_items == 400
    assert "automatically" in destination.detail


@pytest.mark.asyncio
async def test_owner_can_toggle_publication_guard_from_delivery_dashboard(
    derivative_approved_context: ApprovedContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    approved = derivative_approved_context
    settings = Settings(
        environment=Environment.TEST,
        auth_enabled=False,
        auth_development_bypass_enabled=True,
    )
    settings.publishing_enabled = True
    app = FastAPI()
    app.state.settings = settings
    principal = _owner_principal(approved.owner_id)
    recent_auth_calls = 0
    mutation_auth_calls = 0

    async def verified_owner(*_args: object, **_kwargs: object) -> AuthenticatedPrincipal:
        nonlocal recent_auth_calls
        recent_auth_calls += 1
        return principal

    async def verified_mutation_owner(
        *_args: object,
        **_kwargs: object,
    ) -> AuthenticatedPrincipal:
        nonlocal mutation_auth_calls
        mutation_auth_calls += 1
        return principal

    monkeypatch.setattr(delivery_routes, "require_publication_owner", verified_owner)
    monkeypatch.setattr(
        delivery_routes,
        "require_publication_mutation_owner",
        verified_mutation_owner,
    )

    async with approved.database.sessions() as session:
        initial = await get_publication_guard(session)
        fields = _publication_guard_fields(
            settings,
            principal,
            review_task_id=approved.review_task_id,
            enabled=True,
            epoch=initial.epoch,
            lock_version=initial.lock_version,
        )
        response = await dashboard_change_publication_guard(
            approved.review_task_id,
            _publication_guard_request(
                app,
                review_task_id=approved.review_task_id,
                fields=fields,
            ),
            session,
            principal,
        )
        assert response.status_code == 303
        assert response.headers["location"].endswith(f"/{approved.review_task_id}/delivery")

        changed = await get_publication_guard(session)
        assert changed.enabled
        assert changed.epoch == initial.epoch + 1
        assert changed.lock_version == initial.lock_version + 1

        replay = await dashboard_change_publication_guard(
            approved.review_task_id,
            _publication_guard_request(
                app,
                review_task_id=approved.review_task_id,
                fields=fields,
            ),
            session,
            principal,
        )
        assert replay.status_code == 303
        replayed_state = await get_publication_guard(session)
        assert replayed_state.epoch == changed.epoch
        assert replayed_state.lock_version == changed.lock_version

        stale_fields = _publication_guard_fields(
            settings,
            principal,
            review_task_id=approved.review_task_id,
            enabled=False,
            epoch=initial.epoch,
            lock_version=initial.lock_version,
        )
        stale = await dashboard_change_publication_guard(
            approved.review_task_id,
            _publication_guard_request(
                app,
                review_task_id=approved.review_task_id,
                fields=stale_fields,
            ),
            session,
            principal,
        )
        assert stale.status_code == 409
        assert b"changed in another session" in stale.body
        unchanged = await get_publication_guard(session)
        assert unchanged.enabled
        assert unchanged.epoch == changed.epoch
        assert recent_auth_calls == 2
        assert mutation_auth_calls == 1


@pytest.mark.asyncio
async def test_dashboard_blocks_enable_when_workers_are_disabled_but_allows_stop(
    derivative_approved_context: ApprovedContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    approved = derivative_approved_context
    settings = Settings(
        environment=Environment.TEST,
        auth_enabled=False,
        auth_development_bypass_enabled=True,
    )
    app = FastAPI()
    app.state.settings = settings
    principal = _owner_principal(approved.owner_id)
    recent_auth_calls = 0
    mutation_auth_calls = 0

    async def verified_owner(*_args: object, **_kwargs: object) -> AuthenticatedPrincipal:
        nonlocal recent_auth_calls
        recent_auth_calls += 1
        return principal

    async def verified_mutation_owner(
        *_args: object,
        **_kwargs: object,
    ) -> AuthenticatedPrincipal:
        nonlocal mutation_auth_calls
        mutation_auth_calls += 1
        return principal

    monkeypatch.setattr(delivery_routes, "require_publication_owner", verified_owner)
    monkeypatch.setattr(
        delivery_routes,
        "require_publication_mutation_owner",
        verified_mutation_owner,
    )

    async with approved.database.sessions() as session:
        initial = await get_publication_guard(session)
        blocked_fields = _publication_guard_fields(
            settings,
            principal,
            review_task_id=approved.review_task_id,
            enabled=True,
            epoch=initial.epoch,
            lock_version=initial.lock_version,
        )
        blocked = await dashboard_change_publication_guard(
            approved.review_task_id,
            _publication_guard_request(
                app,
                review_task_id=approved.review_task_id,
                fields=blocked_fields,
            ),
            session,
            principal,
        )
        assert blocked.status_code == 409
        assert b"Publication workers are disabled" in blocked.body
        still_stopped = await get_publication_guard(session)
        assert not still_stopped.enabled
        assert still_stopped.epoch == initial.epoch

        enabled = await set_publication_guard(
            session,
            enabled=True,
            expected_epoch=initial.epoch,
            expected_lock_version=initial.lock_version,
            reason="Prepare the emergency-stop dashboard path",
            actor_user_id=approved.owner_id,
            actor_role=AdminRole.OWNER,
            idempotency_key="operator-delivery-enable-before-browser-stop",
        )
        stop_fields = _publication_guard_fields(
            settings,
            principal,
            review_task_id=approved.review_task_id,
            enabled=False,
            epoch=enabled.epoch,
            lock_version=enabled.lock_version,
        )
        stopped = await dashboard_change_publication_guard(
            approved.review_task_id,
            _publication_guard_request(
                app,
                review_task_id=approved.review_task_id,
                fields=stop_fields,
            ),
            session,
            principal,
        )
        assert stopped.status_code == 303
        final = await get_publication_guard(session)
        assert not final.enabled
        assert final.epoch == enabled.epoch + 1
        assert recent_auth_calls == 1
        assert mutation_auth_calls == 1


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
        assert before.publishing_guard_epoch == guard.epoch
        assert before.publishing_guard_lock_version == guard.lock_version
        assert before.publishing_guard_changed_at == guard.changed_at
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
        assert "Delivery destinations" in html
        assert "MEGA receives the clean, full-resolution images automatically" in html
        assert "Patreon" in html
        assert "MEGA" in html
        assert "X" in html
        assert "Stop publication" in html
        assert "status ready" in html
        assert "persistent signed-in browser publisher" in html
