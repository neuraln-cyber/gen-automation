import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from starlette.requests import Request

from gen_automation.api.routes import delivery_dashboard as delivery_routes
from gen_automation.config import Environment, Settings
from gen_automation.domain.enums import AdminRole, ReleasePhase, ReviewTaskState
from gen_automation.services.authentication import AuthenticatedPrincipal
from gen_automation.services.operator_delivery import (
    DeliveryPackagePart,
    DerivativeProgress,
    DestinationState,
    OperatorDeliveryNotFoundError,
    OperatorDeliverySnapshot,
)


def _progress(**overrides: object) -> DerivativeProgress:
    values: dict[str, object] = {
        "planned": True,
        "total_jobs": 3,
        "requested": 0,
        "running": 0,
        "retrying": 0,
        "succeeded": 3,
        "failed": 0,
        "expected_full_outputs": 2,
        "ready_full_outputs": 2,
        "expected_x_teasers": 1,
        "ready_x_teasers": 1,
        "ready_for_destinations": True,
    }
    values.update(overrides)
    return DerivativeProgress(**values)  # type: ignore[arg-type]


def _snapshot(
    *,
    progress: DerivativeProgress | None = None,
    patreon: DestinationState | None = None,
    publishing_guard_enabled: bool = True,
    x_selected_count: int = 0,
) -> OperatorDeliverySnapshot:
    return OperatorDeliverySnapshot(
        review_task_id=uuid4(),
        review_state=ReviewTaskState.COMPLETED,
        release_id=uuid4(),
        release_version_id=uuid4(),
        release_title="Ranked set",
        release_phase=ReleasePhase.RENDERING,
        x_selected_count=x_selected_count,
        progress=progress or _progress(),
        full_outputs=(),
        x_outputs=(),
        publishing_guard_enabled=publishing_guard_enabled,
        publishing_guard_epoch=4,
        publishing_guard_lock_version=8,
        publishing_guard_changed_at=datetime.now(UTC),
        destinations=(
            patreon
            or DestinationState(
                key="patreon",
                label="Patreon",
                state="not_prepared",
                detail="Not prepared.",
            ),
            DestinationState("mega", "MEGA", "not_prepared", "Not prepared."),
            DestinationState("x", "X", "not_prepared", "Not prepared."),
        ),
    )


def _settings(*, publishing_enabled: bool, x_configured: bool = False) -> Settings:
    settings = Settings(
        environment=Environment.TEST,
        auth_enabled=False,
        auth_development_bypass_enabled=True,
        session_secret="delivery-progress-test-secret-long-enough",  # noqa: S106
    )
    settings.publishing_enabled = publishing_enabled
    settings.x_oauth_secret_reference = "secret://x" if x_configured else None
    return settings


def _principal(role: AdminRole = AdminRole.OWNER) -> AuthenticatedPrincipal:
    now = datetime.now(UTC)
    return AuthenticatedPrincipal(
        session_id=uuid4(),
        user_id=uuid4(),
        username="delivery-owner",
        display_name="Delivery Owner",
        role=role,
        csrf_sha256="a" * 64,
        expires_at=now + timedelta(hours=1),
        idle_expires_at=now + timedelta(hours=1),
        reauthenticated_at=now,
        mfa_verified_at=now,
    )


def _request(settings: Settings, review_task_id: object) -> Request:
    app = FastAPI()
    app.state.settings = settings
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": f"/dashboard/review-tasks/{review_task_id}/delivery/progress",
            "headers": [],
            "app": app,
        }
    )


def _delivery_request(settings: Settings, review_task_id: object) -> Request:
    app = FastAPI()
    app.state.settings = settings
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": f"/dashboard/review-tasks/{review_task_id}/delivery",
            "headers": [],
            "app": app,
        }
    )


def test_derivative_progress_distinguishes_ready_failed_and_stalled_work() -> None:
    ready = _progress(ready_for_destinations=False)
    assert ready.active_jobs == 0
    assert ready.outputs_ready
    assert not ready.terminal_failures
    assert not ready.stalled

    active = _progress(
        total_jobs=3,
        requested=1,
        running=1,
        retrying=1,
        succeeded=0,
        ready_full_outputs=0,
        ready_x_teasers=0,
        ready_for_destinations=False,
    )
    assert active.active_jobs == 3
    assert not active.outputs_ready

    failed = replace(
        active,
        requested=0,
        running=0,
        retrying=0,
        failed=1,
    )
    assert failed.terminal_failures
    assert not failed.stalled

    stalled = replace(failed, failed=0, succeeded=2)
    assert not stalled.terminal_failures
    assert stalled.stalled


def test_delivery_progress_payload_reports_ready_archive_parts_in_global_order() -> None:
    intent_id = uuid4()
    snapshot = _snapshot(
        patreon=DestinationState(
            key="patreon",
            label="Patreon",
            state="published",
            detail="Published.",
            intent_id=intent_id,
            package_parts=(
                DeliveryPackagePart(uuid4(), 1, 2, 1, 100),
                DeliveryPackagePart(uuid4(), 2, 2, 101, 125),
            ),
        ),
        publishing_guard_enabled=False,
    )

    payload = delivery_routes._delivery_progress_payload(
        snapshot,
        publishing_enabled=False,
    )

    assert payload == {
        "schema": "delivery-progress/v1",
        "review_task_id": str(snapshot.review_task_id),
        "outputs": {
            "state": "ready",
            "planned": True,
            "total_jobs": 3,
            "requested": 0,
            "running": 0,
            "retrying": 0,
            "succeeded": 3,
            "failed": 0,
            "active_jobs": 0,
            "expected_full_outputs": 2,
            "ready_full_outputs": 2,
            "expected_x_teasers": 1,
            "ready_x_teasers": 1,
        },
        "archive": {
            "state": "ready",
            "detail": None,
            "part_count": 2,
            "parts": [
                {
                    "part_number": 1,
                    "part_count": 2,
                    "first_ordinal": 1,
                    "last_ordinal": 100,
                },
                {
                    "part_number": 2,
                    "part_count": 2,
                    "first_ordinal": 101,
                    "last_ordinal": 125,
                },
            ],
        },
        "poll_after_ms": None,
    }


@pytest.mark.parametrize(
    ("publishing_enabled", "guard_enabled", "destination_state", "expected", "poll"),
    [
        (True, True, "queued", "preparing", 3000),
        (True, True, "running", "preparing", 3000),
        (True, True, "failed", "failed", None),
        (False, False, "failed", "failed", None),
        (False, True, "queued", "blocked", None),
        (True, False, "running", "blocked", None),
    ],
)
def test_delivery_progress_archive_state_stops_polling_when_blocked(
    publishing_enabled: bool,
    guard_enabled: bool,
    destination_state: str,
    expected: str,
    poll: int | None,
) -> None:
    snapshot = _snapshot(
        patreon=DestinationState(
            key="patreon",
            label="Patreon",
            state=destination_state,
            detail="Destination detail.",
            intent_id=uuid4(),
        ),
        publishing_guard_enabled=guard_enabled,
    )

    payload = delivery_routes._delivery_progress_payload(
        snapshot,
        publishing_enabled=publishing_enabled,
    )

    assert isinstance(payload["archive"], dict)
    assert payload["archive"]["state"] == expected
    assert payload["poll_after_ms"] == poll


@pytest.mark.asyncio
async def test_delivery_progress_endpoint_is_owner_only_private_and_no_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _snapshot(
        progress=_progress(
            requested=1,
            succeeded=2,
            ready_full_outputs=1,
            ready_x_teasers=1,
            ready_for_destinations=False,
        )
    )

    async def load(*_args: object, **_kwargs: object) -> OperatorDeliverySnapshot:
        return snapshot

    monkeypatch.setattr(delivery_routes, "load_operator_delivery", load)
    settings = _settings(publishing_enabled=True)
    response = await delivery_routes.dashboard_review_delivery_progress(
        snapshot.review_task_id,
        _request(settings, snapshot.review_task_id),
        SimpleNamespace(),  # type: ignore[arg-type]
        _principal(),
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    payload = json.loads(response.body)
    assert payload["outputs"]["state"] == "rendering"
    assert payload["outputs"]["active_jobs"] == 1
    assert payload["poll_after_ms"] == 3000

    forbidden = await delivery_routes.dashboard_review_delivery_progress(
        snapshot.review_task_id,
        _request(settings, snapshot.review_task_id),
        SimpleNamespace(),  # type: ignore[arg-type]
        _principal(AdminRole.PUBLISHER),
    )
    assert forbidden.status_code == 403
    assert forbidden.headers["cache-control"] == "private, no-store"


@pytest.mark.asyncio
async def test_delivery_progress_endpoint_returns_private_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    review_task_id = uuid4()

    async def missing(*_args: object, **_kwargs: object) -> None:
        raise OperatorDeliveryNotFoundError

    monkeypatch.setattr(delivery_routes, "load_operator_delivery", missing)
    settings = _settings(publishing_enabled=False)
    response = await delivery_routes.dashboard_review_delivery_progress(
        review_task_id,
        _request(settings, review_task_id),
        SimpleNamespace(),  # type: ignore[arg-type]
        _principal(),
    )

    assert response.status_code == 404
    assert response.headers["cache-control"] == "private, no-store"


@pytest.mark.asyncio
async def test_delivery_page_uses_shared_failed_archive_state_without_polling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _snapshot(
        patreon=DestinationState(
            key="patreon",
            label="Patreon",
            state="failed",
            detail="Archive worker failed.",
            intent_id=uuid4(),
        )
    )

    async def load(*_args: object, **_kwargs: object) -> OperatorDeliverySnapshot:
        return snapshot

    async def no_watermarks(*_args: object, **_kwargs: object) -> tuple[()]:
        return ()

    monkeypatch.setattr(delivery_routes, "load_operator_delivery", load)
    monkeypatch.setattr(delivery_routes, "list_registered_watermarks", no_watermarks)
    response = await delivery_routes.dashboard_review_delivery(
        snapshot.review_task_id,
        _delivery_request(
            _settings(publishing_enabled=False),
            snapshot.review_task_id,
        ),
        SimpleNamespace(),  # type: ignore[arg-type]
        _principal(),
    )

    html = bytes(response.body).decode()
    assert response.status_code == 200
    assert "ZIP creation failed and no archive job is active." in html
    assert "data-delivery-progress-url=" not in html


@pytest.mark.asyncio
async def test_delivery_page_uses_shared_ready_archive_state_without_polling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _snapshot(
        patreon=DestinationState(
            key="patreon",
            label="Patreon",
            state="published",
            detail="Published.",
            intent_id=uuid4(),
            intent_digest="b" * 64,
            intent_lock_version=5,
            package_parts=(
                DeliveryPackagePart(uuid4(), 1, 2, 1, 100),
                DeliveryPackagePart(uuid4(), 2, 2, 101, 125),
            ),
        ),
        publishing_guard_enabled=False,
    )

    async def load(*_args: object, **_kwargs: object) -> OperatorDeliverySnapshot:
        return snapshot

    async def no_watermarks(*_args: object, **_kwargs: object) -> tuple[()]:
        return ()

    monkeypatch.setattr(delivery_routes, "load_operator_delivery", load)
    monkeypatch.setattr(delivery_routes, "list_registered_watermarks", no_watermarks)
    response = await delivery_routes.dashboard_review_delivery(
        snapshot.review_task_id,
        _delivery_request(
            _settings(publishing_enabled=False),
            snapshot.review_task_id,
        ),
        SimpleNamespace(),  # type: ignore[arg-type]
        _principal(),
    )

    html = bytes(response.body).decode()
    assert response.status_code == 200
    assert "Download ZIP part 1 / 2" in html
    assert "Download ZIP part 2 / 2" in html
    assert "data-delivery-progress-url=" not in html


@pytest.mark.parametrize(
    (
        "publishing_enabled",
        "guard_enabled",
        "x_selected_count",
        "x_configured",
        "ready",
        "destination_state",
        "has_intent",
        "expected",
    ),
    [
        (True, True, 0, False, True, "not_prepared", False, True),
        (False, True, 0, False, True, "not_prepared", False, False),
        (True, False, 0, False, True, "not_prepared", False, False),
        (True, True, 1, False, True, "not_prepared", False, False),
        (True, True, 1, True, True, "not_prepared", False, True),
        (True, True, 0, False, False, "not_prepared", False, False),
        (True, True, 0, False, True, "queued", True, False),
        (True, True, 0, False, True, "failed", True, True),
    ],
)
def test_destination_previews_are_only_needed_when_the_form_can_render(
    publishing_enabled: bool,
    guard_enabled: bool,
    x_selected_count: int,
    x_configured: bool,
    ready: bool,
    destination_state: str,
    has_intent: bool,
    expected: bool,
) -> None:
    snapshot = _snapshot(
        progress=_progress(ready_for_destinations=ready),
        patreon=DestinationState(
            key="patreon",
            label="Patreon",
            state=destination_state,
            detail="Destination detail.",
            intent_id=uuid4() if has_intent else None,
        ),
        publishing_guard_enabled=guard_enabled,
        x_selected_count=x_selected_count,
    )
    settings = _settings(
        publishing_enabled=publishing_enabled,
        x_configured=x_configured,
    )

    assert delivery_routes._can_render_destination_form(snapshot, settings=settings) is expected
