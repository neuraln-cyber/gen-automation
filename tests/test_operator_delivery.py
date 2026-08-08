# ruff: noqa: F811

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlencode
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError
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
    DerivativeOutput,
    PublicationApproval,
    PublicationAttempt,
    PublicationInput,
    PublicationIntent,
    PublicationStep,
    Release,
    ReleaseVersion,
)
from gen_automation.domain.canonical import canonical_sha256
from gen_automation.domain.enums import (
    AdminRole,
    FinishedSetArchiveState,
    MegaDeliveryState,
    PublicationAttemptState,
    PublicationIntentState,
    PublicationStepKind,
    PublicationTarget,
    ReleasePhase,
)
from gen_automation.domain.release_spec import ReleaseSpecification
from gen_automation.services.authentication import AuthenticatedPrincipal
from gen_automation.services.operator_delivery import (
    OperatorDeliveryConflictError,
    OperatorDeliverySnapshot,
    _mega_destination,
    _ordered_outputs,
    load_operator_delivery,
    prepare_operator_destinations,
    prepare_operator_patreon_destination,
    prepare_operator_x_destination,
)
from gen_automation.services.publication import get_publication_guard, set_publication_guard
from tests.factories import seed_release_approvals, valid_release_payload
from tests.test_derivative_pipeline import ApprovedContext
from tests.test_derivative_pipeline import (
    approved_context as derivative_approved_context,  # noqa: F401
)
from tests.test_derivative_runtime import _cycle, _prepare


def test_ordered_outputs_ignore_outputs_from_another_review_selection() -> None:
    current_selection_id = uuid4()
    stale_selection_id = uuid4()
    source_asset_id = uuid4()
    current_output_id = uuid4()
    outputs = [
        SimpleNamespace(
            id=current_output_id,
            release_selection_id=current_selection_id,
            target="full",
            asset_object_key="derivatives/current.png",
            asset_object_version_id="current-version",
            asset_width=768,
            asset_height=1024,
        ),
        SimpleNamespace(
            id=uuid4(),
            release_selection_id=stale_selection_id,
            target="full",
            asset_object_key="derivatives/stale.png",
            asset_object_version_id="stale-version",
            asset_width=768,
            asset_height=1024,
        ),
    ]

    ordered = _ordered_outputs(  # type: ignore[arg-type]
        outputs,
        target="full",
        selection_order={current_selection_id: 3},
        selection_sources={current_selection_id: (source_asset_id, "a" * 64)},
    )

    assert len(ordered) == 1
    assert ordered[0].output_id == current_output_id
    assert ordered[0].source_asset_id == source_asset_id


async def _prepare_independent_destination_inputs(
    approved: ApprovedContext,
) -> OperatorDeliverySnapshot:
    prepared = await _prepare(
        approved,
        with_watermark=True,
        x_selected_asset_ids=(approved.raw_asset_ids[0],),
    )
    await _cycle(prepared, worker_id="independent-destination-derivative")
    await _cycle(prepared, worker_id="independent-destination-derivative")
    await _cycle(prepared, worker_id="independent-destination-derivative")

    payload = valid_release_payload()
    specification = ReleaseSpecification.model_validate(payload["specification"])
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
            reason="independent destination test",
            actor_user_id=approved.owner_id,
            actor_role=AdminRole.OWNER,
            idempotency_key=f"independent-destination-guard:{approved.review_task_id}",
        )
        snapshot = await load_operator_delivery(
            session,
            review_task_id=approved.review_task_id,
        )
    assert snapshot.progress.full_outputs_ready
    assert len(snapshot.x_outputs) == snapshot.x_selected_count == 1
    assert tuple(source.asset_id for source in snapshot.x_selected_sources) == (
        approved.raw_asset_ids[0],
    )
    assert snapshot.x_selected_sources[0].sha256
    return snapshot


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
async def test_mega_projection_keeps_unrequested_ready_archive_idle() -> None:
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

    assert destination.state == "not_prepared"
    assert destination.completed_items is None
    assert destination.total_items is None
    assert "not been requested" in destination.detail


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
        assert "Choose where this set goes" in html
        assert "ZIP, MEGA, Patreon, and X are separate" in html
        assert "Patreon" in html
        assert "MEGA" in html
        assert "X" in html
        assert "Stop publication" in html
        assert "status ready" in html
        assert "Account-wide publishing switch" in html


@pytest.mark.asyncio
async def test_patreon_only_preparation_creates_no_x_intent(
    derivative_approved_context: ApprovedContext,
) -> None:
    approved = derivative_approved_context
    snapshot = await _prepare_independent_destination_inputs(approved)
    action_at = datetime.now(UTC)

    async with approved.database.sessions() as session:
        result = await prepare_operator_patreon_destination(
            session,
            review_task_id=approved.review_task_id,
            patreon_title="Patreon only",
            patreon_body="This must not post to X.",
            patreon_tier="Paid members",
            patreon_tags=("patreon-only",),
            public_preview_output_id=snapshot.full_outputs[0].output_id,
            public_preview_attester_name="Derivative Owner",
            public_preview_attested_at=action_at,
            actor_user_id=approved.owner_id,
            actor_role=AdminRole.OWNER,
            idempotency_key="independent-patreon-only",
            now=action_at,
        )
        intents = tuple(
            (await session.scalars(select(PublicationIntent).order_by(PublicationIntent.id))).all()
        )
        attempts = tuple((await session.scalars(select(PublicationAttempt))).all())

    assert len(intents) == 1
    assert intents[0].id == result.intent_id
    assert intents[0].target == PublicationTarget.PATREON
    assert len(attempts) == 1
    assert attempts[0].intent_id == result.intent_id


@pytest.mark.asyncio
async def test_x_only_preparation_creates_no_patreon_intent(
    derivative_approved_context: ApprovedContext,
) -> None:
    approved = derivative_approved_context
    await _prepare_independent_destination_inputs(approved)
    action_at = datetime.now(UTC)
    scheduled_at = action_at + timedelta(days=14)

    async with approved.database.sessions() as session:
        result = await prepare_operator_x_destination(
            session,
            review_task_id=approved.review_task_id,
            x_text="X only",
            x_adult_content=False,
            scheduled_at=scheduled_at,
            x_credential_reference="test://x/oauth",
            actor_user_id=approved.owner_id,
            actor_role=AdminRole.OWNER,
            idempotency_key="independent-x-only",
            now=action_at,
        )
        intents = tuple(
            (await session.scalars(select(PublicationIntent).order_by(PublicationIntent.id))).all()
        )
        attempts = tuple((await session.scalars(select(PublicationAttempt))).all())
        approvals = tuple((await session.scalars(select(PublicationApproval))).all())
        steps = tuple((await session.scalars(select(PublicationStep))).all())
        roles = tuple(
            (
                await session.scalars(
                    select(PublicationInput.role)
                    .where(PublicationInput.intent_id == result.intent_id)
                    .order_by(PublicationInput.ordinal)
                )
            ).all()
        )

    assert len(intents) == 1
    assert intents[0].id == result.intent_id
    assert intents[0].target == PublicationTarget.X
    assert intents[0].configuration == {"text": "X only", "adult_content": False}
    assert intents[0].scheduled_at is not None
    assert intents[0].scheduled_at.replace(tzinfo=UTC) == scheduled_at
    assert len(attempts) == 1
    assert attempts[0].intent_id == result.intent_id
    assert attempts[0].available_at.replace(tzinfo=UTC) == scheduled_at
    assert len(approvals) == 1
    assert approvals[0].expires_at.replace(tzinfo=UTC) > scheduled_at
    create_step = next(step for step in steps if step.kind == PublicationStepKind.X_CREATE_POST)
    assert create_step.max_retries == 3
    assert roles == ("x_teaser",)


@pytest.mark.asyncio
async def test_repreparing_patreon_does_not_change_an_existing_x_destination(
    derivative_approved_context: ApprovedContext,
) -> None:
    approved = derivative_approved_context
    snapshot = await _prepare_independent_destination_inputs(approved)
    action_at = datetime.now(UTC)

    async with approved.database.sessions() as session:
        x_result = await prepare_operator_x_destination(
            session,
            review_task_id=approved.review_task_id,
            x_text="Keep this X intent unchanged",
            x_credential_reference="test://x/oauth",
            actor_user_id=approved.owner_id,
            actor_role=AdminRole.OWNER,
            idempotency_key="independent-retry-x",
            now=action_at,
        )
        await prepare_operator_patreon_destination(
            session,
            review_task_id=approved.review_task_id,
            patreon_title="Retry one target",
            patreon_body="Patreon is independent.",
            patreon_tier="Paid members",
            patreon_tags=("target-isolation",),
            public_preview_output_id=snapshot.full_outputs[0].output_id,
            public_preview_attester_name="Derivative Owner",
            public_preview_attested_at=action_at,
            actor_user_id=approved.owner_id,
            actor_role=AdminRole.OWNER,
            idempotency_key="independent-retry-patreon-first",
            now=action_at,
        )
        x_before = await session.get(PublicationIntent, x_result.intent_id)
        assert x_before is not None
        x_identity = (
            x_before.id,
            x_before.intent_digest,
            x_before.state,
            x_before.lock_version,
        )
        x_attempt_ids = tuple(
            (
                await session.scalars(
                    select(PublicationAttempt.id)
                    .where(PublicationAttempt.intent_id == x_result.intent_id)
                    .order_by(PublicationAttempt.attempt_no)
                )
            ).all()
        )

        replay = await prepare_operator_patreon_destination(
            session,
            review_task_id=approved.review_task_id,
            patreon_title="Retry one target",
            patreon_body="Patreon is independent.",
            patreon_tier="Paid members",
            patreon_tags=("target-isolation",),
            public_preview_output_id=snapshot.full_outputs[0].output_id,
            public_preview_attester_name="Derivative Owner",
            public_preview_attested_at=action_at,
            actor_user_id=approved.owner_id,
            actor_role=AdminRole.OWNER,
            idempotency_key="independent-retry-patreon-second",
            now=action_at,
        )
        x_after = await session.get(PublicationIntent, x_result.intent_id)
        assert x_after is not None
        x_attempt_ids_after = tuple(
            (
                await session.scalars(
                    select(PublicationAttempt.id)
                    .where(PublicationAttempt.intent_id == x_result.intent_id)
                    .order_by(PublicationAttempt.attempt_no)
                )
            ).all()
        )

    assert replay.replayed
    assert (
        x_after.id,
        x_after.intent_digest,
        x_after.state,
        x_after.lock_version,
    ) == x_identity
    assert x_attempt_ids_after == x_attempt_ids


@pytest.mark.asyncio
async def test_changed_x_configuration_cannot_create_a_second_active_intent(
    derivative_approved_context: ApprovedContext,
) -> None:
    approved = derivative_approved_context
    await _prepare_independent_destination_inputs(approved)
    action_at = datetime.now(UTC)

    async with approved.database.sessions() as session:
        first = await prepare_operator_x_destination(
            session,
            review_task_id=approved.review_task_id,
            x_text="The canonical X post",
            x_credential_reference="test://x/oauth",
            actor_user_id=approved.owner_id,
            actor_role=AdminRole.OWNER,
            idempotency_key="exclusive-x-first",
            now=action_at,
        )
        with pytest.raises(
            OperatorDeliveryConflictError,
            match="another publication intent already owns this release target",
        ):
            await prepare_operator_x_destination(
                session,
                review_task_id=approved.review_task_id,
                x_text="A competing X post",
                x_credential_reference="test://x/oauth",
                actor_user_id=approved.owner_id,
                actor_role=AdminRole.OWNER,
                idempotency_key="exclusive-x-second",
                now=action_at,
            )
        intents = tuple((await session.scalars(select(PublicationIntent))).all())
        attempts = tuple((await session.scalars(select(PublicationAttempt))).all())

    assert [intent.id for intent in intents] == [first.intent_id]
    assert len(attempts) == 1
    assert attempts[0].intent_id == first.intent_id


@pytest.mark.asyncio
async def test_database_rejects_two_canonical_intents_for_one_release_target(
    derivative_approved_context: ApprovedContext,
) -> None:
    approved = derivative_approved_context
    await _prepare_independent_destination_inputs(approved)
    action_at = datetime.now(UTC)

    async with approved.database.sessions() as session:
        first = await prepare_operator_x_destination(
            session,
            review_task_id=approved.review_task_id,
            x_text="The canonical database owner",
            x_credential_reference="test://x/oauth",
            actor_user_id=approved.owner_id,
            actor_role=AdminRole.OWNER,
            idempotency_key="canonical-index-first",
            now=action_at,
        )
        first_intent = await session.get(PublicationIntent, first.intent_id)
        assert first_intent is not None
        session.add(
            PublicationIntent(
                id=uuid4(),
                release_id=first_intent.release_id,
                release_version_id=first_intent.release_version_id,
                target=first_intent.target,
                state=PublicationIntentState.AWAITING_APPROVAL,
                configuration={"text": "A forbidden duplicate"},
                configuration_sha256="a" * 64,
                input_manifest_sha256="b" * 64,
                intent_digest="c" * 64,
                input_count=1,
                credential_reference="test://x/oauth",
                planned_by_user_id=approved.owner_id,
                planned_at=action_at,
                lock_version=1,
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "blocking_state",
    (PublicationIntentState.UNKNOWN, PublicationIntentState.PUBLISHED),
)
async def test_ambiguous_or_published_x_intent_blocks_a_changed_request(
    derivative_approved_context: ApprovedContext,
    blocking_state: PublicationIntentState,
) -> None:
    approved = derivative_approved_context
    await _prepare_independent_destination_inputs(approved)
    action_at = datetime.now(UTC)

    async with approved.database.sessions() as session:
        first = await prepare_operator_x_destination(
            session,
            review_task_id=approved.review_task_id,
            x_text="The externally owned X post",
            x_credential_reference="test://x/oauth",
            actor_user_id=approved.owner_id,
            actor_role=AdminRole.OWNER,
            idempotency_key=f"exclusive-x-blocking:{blocking_state.value}",
            now=action_at,
        )
        first_intent = await session.get(PublicationIntent, first.intent_id)
        assert first_intent is not None
        first_intent.state = blocking_state
        first_intent.completed_at = action_at
        await session.commit()

        with pytest.raises(
            OperatorDeliveryConflictError,
            match="another publication intent already owns this release target",
        ):
            await prepare_operator_x_destination(
                session,
                review_task_id=approved.review_task_id,
                x_text="A changed X post must not duplicate the external effect",
                x_credential_reference="test://x/oauth",
                actor_user_id=approved.owner_id,
                actor_role=AdminRole.OWNER,
                idempotency_key=f"exclusive-x-blocked:{blocking_state.value}",
                now=action_at,
            )

        assert len(tuple((await session.scalars(select(PublicationIntent))).all())) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("intent_state", "attempt_state"),
    (
        (PublicationIntentState.FAILED, PublicationAttemptState.FAILED),
        (PublicationIntentState.CANCELLED, PublicationAttemptState.CANCELLED),
    ),
)
async def test_terminal_x_intent_can_be_safely_superseded(
    derivative_approved_context: ApprovedContext,
    intent_state: PublicationIntentState,
    attempt_state: PublicationAttemptState,
) -> None:
    approved = derivative_approved_context
    await _prepare_independent_destination_inputs(approved)
    action_at = datetime.now(UTC)

    async with approved.database.sessions() as session:
        first = await prepare_operator_x_destination(
            session,
            review_task_id=approved.review_task_id,
            x_text="Terminal X post",
            x_credential_reference="test://x/oauth",
            actor_user_id=approved.owner_id,
            actor_role=AdminRole.OWNER,
            idempotency_key=f"terminal-x-first:{intent_state.value}",
            now=action_at,
        )
        first_intent = await session.get(PublicationIntent, first.intent_id)
        first_attempt = await session.scalar(
            select(PublicationAttempt).where(PublicationAttempt.intent_id == first.intent_id)
        )
        assert first_intent is not None and first_attempt is not None
        first_intent.state = intent_state
        first_intent.completed_at = action_at
        first_attempt.state = attempt_state
        first_attempt.completed_at = action_at
        await session.commit()

        replacement = await prepare_operator_x_destination(
            session,
            review_task_id=approved.review_task_id,
            x_text="Replacement X post",
            x_credential_reference="test://x/oauth",
            actor_user_id=approved.owner_id,
            actor_role=AdminRole.OWNER,
            idempotency_key=f"terminal-x-replacement:{intent_state.value}",
            now=action_at,
        )
        intents = tuple(
            (
                await session.scalars(
                    select(PublicationIntent).order_by(PublicationIntent.planned_at)
                )
            ).all()
        )

    assert replacement.intent_id != first.intent_id
    assert len(intents) == 2
    assert intents[0].state == intent_state
    assert intents[1].state == PublicationIntentState.READY


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("intent_state", "attempt_state"),
    (
        (PublicationIntentState.FAILED, PublicationAttemptState.FAILED),
        (PublicationIntentState.CANCELLED, PublicationAttemptState.CANCELLED),
    ),
)
async def test_exact_terminal_x_intent_retries_in_place(
    derivative_approved_context: ApprovedContext,
    intent_state: PublicationIntentState,
    attempt_state: PublicationAttemptState,
) -> None:
    approved = derivative_approved_context
    await _prepare_independent_destination_inputs(approved)
    action_at = datetime.now(UTC)

    async with approved.database.sessions() as session:
        first = await prepare_operator_x_destination(
            session,
            review_task_id=approved.review_task_id,
            x_text="Retry this exact X post",
            x_credential_reference="test://x/oauth",
            actor_user_id=approved.owner_id,
            actor_role=AdminRole.OWNER,
            idempotency_key=f"exact-terminal-x-first:{intent_state.value}",
            now=action_at,
        )
        first_intent = await session.get(PublicationIntent, first.intent_id)
        first_attempt = await session.scalar(
            select(PublicationAttempt).where(PublicationAttempt.intent_id == first.intent_id)
        )
        assert first_intent is not None and first_attempt is not None
        first_intent.state = intent_state
        first_intent.completed_at = action_at
        first_attempt.state = attempt_state
        first_attempt.completed_at = action_at
        await session.commit()

        retry = await prepare_operator_x_destination(
            session,
            review_task_id=approved.review_task_id,
            x_text="Retry this exact X post",
            x_credential_reference="test://x/oauth",
            actor_user_id=approved.owner_id,
            actor_role=AdminRole.OWNER,
            idempotency_key=f"exact-terminal-x-retry:{intent_state.value}",
            now=action_at,
        )
        retried_intent = await session.get(PublicationIntent, first.intent_id)
        attempts = tuple(
            (
                await session.scalars(
                    select(PublicationAttempt)
                    .where(PublicationAttempt.intent_id == first.intent_id)
                    .order_by(PublicationAttempt.attempt_no)
                )
            ).all()
        )

    assert retry.intent_id == first.intent_id
    assert not retry.replayed
    assert retried_intent is not None
    assert retried_intent.state == PublicationIntentState.READY
    assert retried_intent.completed_at is None
    assert [attempt.attempt_no for attempt in attempts] == [1, 2]
    assert attempts[-1].state == PublicationAttemptState.QUEUED


@pytest.mark.asyncio
async def test_patreon_preparation_ignores_missing_x_derivative_when_full_set_is_ready(
    derivative_approved_context: ApprovedContext,
) -> None:
    approved = derivative_approved_context
    initial = await _prepare_independent_destination_inputs(approved)
    action_at = datetime.now(UTC)

    async with approved.database.sessions() as session:
        await session.execute(text("DROP TRIGGER derivative_outputs_reject_delete"))
        await session.execute(delete(DerivativeOutput).where(DerivativeOutput.target == "x_teaser"))
        release = await session.get(Release, approved.release_id)
        assert release is not None
        release.phase = ReleasePhase.RENDERING
        await session.commit()

        snapshot = await load_operator_delivery(
            session,
            review_task_id=approved.review_task_id,
        )
        assert snapshot.progress.full_outputs_ready
        assert not snapshot.progress.ready_for_destinations
        assert snapshot.x_outputs == ()

        result = await prepare_operator_patreon_destination(
            session,
            review_task_id=approved.review_task_id,
            patreon_title="Patreon independent of X",
            patreon_body="The clean set is complete.",
            patreon_tier="Paid members",
            patreon_tags=("target-ready",),
            public_preview_output_id=initial.full_outputs[0].output_id,
            public_preview_attester_name="Derivative Owner",
            public_preview_attested_at=action_at,
            actor_user_id=approved.owner_id,
            actor_role=AdminRole.OWNER,
            idempotency_key="patreon-with-missing-x",
            now=action_at,
        )
        intent = await session.get(PublicationIntent, result.intent_id)

    assert intent is not None
    assert intent.target == PublicationTarget.PATREON
    assert intent.state == PublicationIntentState.READY
