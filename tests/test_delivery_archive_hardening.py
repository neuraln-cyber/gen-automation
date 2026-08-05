from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast
from uuid import uuid4

import pytest
from fastapi import FastAPI
from starlette.requests import Request

from gen_automation.api.routes import delivery_dashboard as delivery_routes
from gen_automation.config import Environment, Settings
from gen_automation.db.models import PublicationIntent
from gen_automation.domain.enums import (
    AdminRole,
    PublicationIntentState,
    ReleasePhase,
    ReviewTaskState,
)
from gen_automation.services.authentication import AuthenticatedPrincipal
from gen_automation.services.operator_delivery import (
    DeliveryPackagePart,
    DerivativeProgress,
    DestinationState,
    OperatorDeliverySnapshot,
    _publication_state,
    package_parts_ready,
)


def _principal() -> AuthenticatedPrincipal:
    now = datetime.now(UTC)
    return AuthenticatedPrincipal(
        session_id=uuid4(),
        user_id=uuid4(),
        username="archive-owner",
        display_name="Archive Owner",
        role=AdminRole.OWNER,
        csrf_sha256="a" * 64,
        expires_at=now + timedelta(hours=1),
        idle_expires_at=now + timedelta(hours=1),
        reauthenticated_at=now,
        mfa_verified_at=now,
    )


def _settings() -> Settings:
    settings = Settings(
        environment=Environment.TEST,
        auth_enabled=False,
        auth_development_bypass_enabled=True,
        session_secret="archive-hardening-test-secret-long-enough",  # noqa: S106
    )
    settings.publishing_enabled = True
    return settings


def _snapshot(patreon: DestinationState) -> OperatorDeliverySnapshot:
    return OperatorDeliverySnapshot(
        review_task_id=uuid4(),
        review_state=ReviewTaskState.COMPLETED,
        release_id=uuid4(),
        release_version_id=uuid4(),
        release_title="Ranked archive",
        release_phase=ReleasePhase.RENDERING,
        x_selected_count=0,
        progress=DerivativeProgress(
            planned=True,
            total_jobs=2,
            requested=0,
            running=0,
            retrying=0,
            succeeded=2,
            failed=0,
            expected_full_outputs=2,
            ready_full_outputs=2,
            expected_x_teasers=0,
            ready_x_teasers=0,
            ready_for_destinations=True,
        ),
        full_outputs=(),
        x_outputs=(),
        publishing_guard_enabled=True,
        publishing_guard_epoch=3,
        publishing_guard_lock_version=5,
        publishing_guard_changed_at=datetime.now(UTC),
        destinations=(
            patreon,
            DestinationState("mega", "MEGA", "not_prepared", "Not prepared."),
            DestinationState("x", "X", "not_prepared", "No teasers selected."),
        ),
    )


def _part(part_number: int, part_count: int) -> DeliveryPackagePart:
    first_ordinal = ((part_number - 1) * 100) + 1
    return DeliveryPackagePart(
        package_id=uuid4(),
        part_number=part_number,
        part_count=part_count,
        first_ordinal=first_ordinal,
        last_ordinal=first_ordinal + 99,
    )


def test_awaiting_approval_is_action_required_and_does_not_poll() -> None:
    intent = cast(
        PublicationIntent,
        SimpleNamespace(state=PublicationIntentState.AWAITING_APPROVAL),
    )
    state, detail = _publication_state(intent, None)

    assert state == "failed"
    assert "fresh approval" in detail

    destination = DestinationState(
        key="patreon",
        label="Patreon",
        state=state,
        detail=detail,
        intent_id=uuid4(),
        intent_digest="d" * 64,
        intent_lock_version=7,
    )
    snapshot = _snapshot(destination)
    settings = _settings()

    assert snapshot.destinations_need_retry
    assert delivery_routes._can_render_destination_form(snapshot, settings=settings)
    payload = delivery_routes._delivery_progress_payload(
        snapshot,
        publishing_enabled=True,
    )
    assert payload["archive"] == {
        "state": "failed",
        "detail": detail,
        "part_count": 0,
        "parts": [],
    }
    assert payload["poll_after_ms"] is None


@pytest.mark.parametrize(
    ("parts", "expected"),
    [
        ((), False),
        ((_part(1, 1),), True),
        ((_part(1, 2),), False),
        ((_part(1, 2), _part(1, 2)), False),
        ((_part(1, 2), _part(2, 3)), False),
        ((_part(2, 2), _part(1, 2)), True),
    ],
)
def test_package_parts_ready_requires_one_contiguous_consistent_set(
    parts: tuple[DeliveryPackagePart, ...],
    expected: bool,
) -> None:
    assert package_parts_ready(parts) is expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("parts", "shows_download"),
    [
        ((_part(1, 2),), False),
        ((_part(1, 2), _part(2, 3)), False),
        ((_part(2, 2), _part(1, 2)), True),
    ],
)
async def test_dashboard_only_offers_complete_archive_parts(
    monkeypatch: pytest.MonkeyPatch,
    parts: tuple[DeliveryPackagePart, ...],
    shows_download: bool,
) -> None:
    destination = DestinationState(
        key="patreon",
        label="Patreon",
        state="published",
        detail="Published.",
        intent_id=uuid4(),
        intent_digest="e" * 64,
        intent_lock_version=11,
        package_parts=parts,
    )
    snapshot = _snapshot(destination)

    async def load_snapshot(*_args: object, **_kwargs: object) -> OperatorDeliverySnapshot:
        return snapshot

    async def no_watermarks(*_args: object, **_kwargs: object) -> tuple[()]:
        return ()

    monkeypatch.setattr(delivery_routes, "load_operator_delivery", load_snapshot)
    monkeypatch.setattr(delivery_routes, "list_registered_watermarks", no_watermarks)
    app = FastAPI()
    app.state.settings = _settings()
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": f"/dashboard/review-tasks/{snapshot.review_task_id}/delivery",
            "headers": [],
            "app": app,
        }
    )

    response = await delivery_routes.dashboard_review_delivery(
        snapshot.review_task_id,
        request,
        SimpleNamespace(),  # type: ignore[arg-type]
        _principal(),
    )

    assert response.status_code == 200
    html = bytes(response.body).decode()
    download_action = (
        f"/dashboard/review-tasks/{snapshot.review_task_id}/delivery/finished-set/"
        f"{destination.intent_id}:download"
    )
    assert (download_action in html) is shows_download
    progress = delivery_routes._delivery_progress_payload(
        snapshot,
        publishing_enabled=True,
    )
    assert progress["archive"]["state"] == ("ready" if shows_download else "not_started")
    assert progress["poll_after_ms"] is None
