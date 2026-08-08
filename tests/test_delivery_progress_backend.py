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
from gen_automation.domain.enums import (
    AdminRole,
    FinishedSetArchiveState,
    ReleasePhase,
    ReviewTaskState,
)
from gen_automation.services.authentication import AuthenticatedPrincipal
from gen_automation.services.finished_set_archives import (
    FinishedSetArchivePartSnapshot,
    FinishedSetArchiveSnapshot,
)
from gen_automation.services.operator_delivery import (
    DeliveryOutput,
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
    mega: DestinationState | None = None,
    x: DestinationState | None = None,
    publishing_guard_enabled: bool = True,
    x_selected_count: int = 0,
) -> OperatorDeliverySnapshot:
    resolved_progress = progress or _progress()
    return OperatorDeliverySnapshot(
        review_task_id=uuid4(),
        review_state=ReviewTaskState.COMPLETED,
        release_id=uuid4(),
        release_version_id=uuid4(),
        release_title="Ranked set",
        release_phase=ReleasePhase.RENDERING,
        x_selected_count=x_selected_count,
        progress=resolved_progress,
        full_outputs=(),
        x_outputs=tuple(
            DeliveryOutput(
                output_id=uuid4(),
                selection_id=uuid4(),
                display_order=index + 1,
                target="x_teaser",
                object_key=f"x/{index + 1}.png",
                object_version_id="version",
                width=1200,
                height=1600,
            )
            for index in range(min(x_selected_count, resolved_progress.ready_x_teasers))
        ),
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
            mega or DestinationState("mega", "MEGA", "not_prepared", "Not prepared."),
            x or DestinationState("x", "X", "not_prepared", "Not prepared."),
        ),
    )


def _archive(
    snapshot: OperatorDeliverySnapshot,
    *,
    state: FinishedSetArchiveState = FinishedSetArchiveState.READY,
    selection_count: int = 125,
    part_count: int = 2,
) -> FinishedSetArchiveSnapshot:
    now = datetime.now(UTC)
    manifest_sha256 = "d" * 64 if state == FinishedSetArchiveState.READY else None
    parts = (
        (
            FinishedSetArchivePartSnapshot(
                part_id=uuid4(),
                part_number=1,
                part_count=2,
                first_ordinal=1,
                last_ordinal=100,
                sha256="a" * 64,
                manifest_sha256="d" * 64,
                byte_size=1234,
            ),
            FinishedSetArchivePartSnapshot(
                part_id=uuid4(),
                part_number=2,
                part_count=2,
                first_ordinal=101,
                last_ordinal=selection_count,
                sha256="b" * 64,
                manifest_sha256="d" * 64,
                byte_size=567,
            ),
        )
        if state == FinishedSetArchiveState.READY and part_count == 2
        else ()
    )
    return FinishedSetArchiveSnapshot(
        archive_id=uuid4(),
        review_task_id=snapshot.review_task_id,
        release_version_id=snapshot.release_version_id,
        state=state,
        selection_count=selection_count,
        manifest_sha256=manifest_sha256,
        part_count=part_count if parts else 0,
        attempts=0,
        max_attempts=3,
        available_at=now,
        created_at=now,
        started_at=now if state == FinishedSetArchiveState.PROCESSING else None,
        completed_at=(
            now
            if state in {FinishedSetArchiveState.READY, FinishedSetArchiveState.FAILED}
            else None
        ),
        last_error_code="archive_failed" if state == FinishedSetArchiveState.FAILED else None,
        last_error_detail=(
            "Archive worker failed." if state == FinishedSetArchiveState.FAILED else None
        ),
        parts=parts,
        requested_by_user_id=uuid4(),
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

    cancelled = replace(failed, cancelled=1)
    assert not cancelled.terminal_failures
    assert cancelled.stalled

    stalled = replace(failed, failed=0, succeeded=2)
    assert not stalled.terminal_failures
    assert stalled.stalled


def test_delivery_progress_payload_reports_ready_archive_parts_in_global_order() -> None:
    snapshot = _snapshot(publishing_guard_enabled=False)
    archive = _archive(snapshot)

    payload = delivery_routes._delivery_progress_payload(
        snapshot,
        finished_set_archive=archive,
    )

    assert payload == {
        "schema": "delivery-progress/v1",
        "review_task_id": str(snapshot.review_task_id),
        "outputs": {
            "state": "ready",
            "full_outputs_ready": True,
            "x_outputs_ready": True,
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
            "archive_id": str(archive.archive_id),
            "worker_state": "ready",
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
        "mega": {
            "state": "not_prepared",
            "active": False,
            "detail": "Not prepared.",
            "completed_items": None,
            "total_items": None,
            "remote_path": None,
        },
        "patreon": {
            "state": "not_prepared",
            "active": False,
            "detail": "Not prepared.",
        },
        "x": {
            "state": "not_prepared",
            "active": False,
            "detail": "Not prepared.",
        },
        "poll_after_ms": None,
    }


def test_delivery_progress_keeps_polling_for_active_extracted_mega_upload() -> None:
    snapshot = _snapshot(
        publishing_guard_enabled=False,
        mega=DestinationState(
            "mega",
            "MEGA",
            "running",
            "Uploading full-resolution images (20 / 125).",
            completed_items=20,
            total_items=125,
            remote_path="/Finished Sets/Example/v01-deadbeef",
        ),
    )

    payload = delivery_routes._delivery_progress_payload(
        snapshot,
        finished_set_archive=_archive(snapshot),
        mega_enabled=True,
    )

    assert payload["mega"] == {
        "state": "running",
        "active": True,
        "detail": "Uploading full-resolution images (20 / 125).",
        "completed_items": 20,
        "total_items": 125,
        "remote_path": "/Finished Sets/Example/v01-deadbeef",
    }
    assert payload["poll_after_ms"] == 3000


@pytest.mark.parametrize(
    ("target", "publishing_enabled", "guard_enabled", "expected_detail"),
    [
        ("patreon", False, True, "Queued; Patreon publishing is paused."),
        ("patreon", True, False, "Queued; Patreon publishing is paused."),
        ("x", False, True, "Queued; X publishing is paused."),
        ("x", True, False, "Queued; X publishing is paused."),
    ],
)
def test_delivery_progress_does_not_poll_for_paused_publication_workers(
    target: str,
    publishing_enabled: bool,
    guard_enabled: bool,
    expected_detail: str,
) -> None:
    queued = DestinationState(
        target,
        target.title(),
        "queued",
        "Approved publication is queued.",
    )
    snapshot = _snapshot(
        patreon=queued if target == "patreon" else None,
        x=queued if target == "x" else None,
        publishing_guard_enabled=guard_enabled,
    )

    payload = delivery_routes._delivery_progress_payload(
        snapshot,
        finished_set_archive=None,
        publishing_enabled=publishing_enabled,
    )

    destination = payload[target]
    assert isinstance(destination, dict)
    assert destination["active"] is False
    assert destination["detail"] == expected_detail
    assert payload["poll_after_ms"] is None


def test_delivery_progress_does_not_poll_for_paused_mega_worker() -> None:
    snapshot = _snapshot(
        mega=DestinationState(
            "mega",
            "MEGA",
            "queued",
            "MEGA upload is queued.",
            completed_items=0,
            total_items=125,
            remote_path="/Future/Example",
        ),
    )

    payload = delivery_routes._delivery_progress_payload(
        snapshot,
        finished_set_archive=_archive(snapshot),
        mega_enabled=False,
    )

    assert payload["mega"] == {
        "state": "queued",
        "active": False,
        "detail": "Queued; the MEGA delivery worker is paused.",
        "completed_items": 0,
        "total_items": 125,
        "remote_path": "/Future/Example",
    }
    assert payload["poll_after_ms"] is None


@pytest.mark.parametrize(
    ("archive_state", "expected", "poll"),
    [
        (FinishedSetArchiveState.PENDING, "preparing", 3000),
        (FinishedSetArchiveState.PROCESSING, "preparing", 3000),
        (FinishedSetArchiveState.RETRY_WAIT, "preparing", 3000),
        (FinishedSetArchiveState.FAILED, "failed", None),
    ],
)
def test_delivery_progress_archive_state_is_independent_of_publication_destinations(
    archive_state: FinishedSetArchiveState,
    expected: str,
    poll: int | None,
) -> None:
    snapshot = _snapshot(publishing_guard_enabled=False)
    archive = _archive(snapshot, state=archive_state, part_count=0)

    payload = delivery_routes._delivery_progress_payload(
        snapshot,
        finished_set_archive=archive,
    )

    assert isinstance(payload["archive"], dict)
    assert payload["archive"]["state"] == expected
    assert payload["poll_after_ms"] == poll


def test_delivery_progress_does_not_poll_for_unrequested_archive() -> None:
    snapshot = _snapshot(publishing_guard_enabled=False)

    payload = delivery_routes._delivery_progress_payload(
        snapshot,
        finished_set_archive=None,
    )

    assert payload["archive"]["state"] == "not_started"
    assert payload["archive"]["archive_id"] is None
    assert payload["archive"]["detail"] == "ZIP preparation has not been requested."
    assert payload["poll_after_ms"] is None


def test_mega_internal_archive_is_not_projected_as_a_requested_zip() -> None:
    snapshot = _snapshot(publishing_guard_enabled=False)
    internal_archive = replace(_archive(snapshot), requested_by_user_id=None)

    assert delivery_routes._requested_zip_archive(internal_archive) is None
    assert delivery_routes._requested_zip_archive(_archive(snapshot)) is not None


def test_finished_set_archive_does_not_wait_for_failed_x_teasers() -> None:
    progress = _progress(
        total_jobs=3,
        succeeded=2,
        failed=1,
        ready_full_outputs=2,
        ready_x_teasers=0,
        ready_for_destinations=False,
    )
    snapshot = _snapshot(progress=progress, publishing_guard_enabled=False)

    payload = delivery_routes._delivery_progress_payload(
        snapshot,
        finished_set_archive=None,
    )

    assert progress.full_outputs_ready
    assert not progress.outputs_ready
    assert payload["outputs"]["state"] == "ready"
    assert payload["outputs"]["full_outputs_ready"] is True
    assert payload["outputs"]["x_outputs_ready"] is False
    assert payload["archive"]["state"] == "not_started"
    assert payload["poll_after_ms"] is None


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

    async def no_archive(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(delivery_routes, "load_operator_delivery", load)
    monkeypatch.setattr(delivery_routes, "load_finished_set_archive", no_archive)
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
async def test_delivery_page_renders_prepare_zip_fallback_without_archive_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _snapshot(publishing_guard_enabled=False)

    async def load(*_args: object, **_kwargs: object) -> OperatorDeliverySnapshot:
        return snapshot

    async def no_archive(*_args: object, **_kwargs: object) -> None:
        return None

    async def no_watermarks(*_args: object, **_kwargs: object) -> tuple[()]:
        return ()

    monkeypatch.setattr(delivery_routes, "load_operator_delivery", load)
    monkeypatch.setattr(delivery_routes, "load_finished_set_archive", no_archive)
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
    assert "ZIP preparation has not started" in html
    assert "delivery:prepare-archive" in html
    assert "Prepare ZIP download" in html
    assert "data-delivery-progress-url=" in html


@pytest.mark.asyncio
async def test_delivery_page_exposes_zip_queue_while_destination_copies_render(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _snapshot(
        progress=_progress(
            requested=1,
            succeeded=2,
            ready_full_outputs=1,
            ready_x_teasers=1,
            ready_for_destinations=False,
        ),
        publishing_guard_enabled=False,
    )

    async def load(*_args: object, **_kwargs: object) -> OperatorDeliverySnapshot:
        return snapshot

    async def no_archive(*_args: object, **_kwargs: object) -> None:
        return None

    async def no_watermarks(*_args: object, **_kwargs: object) -> tuple[()]:
        return ()

    monkeypatch.setattr(delivery_routes, "load_operator_delivery", load)
    monkeypatch.setattr(delivery_routes, "load_finished_set_archive", no_archive)
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
    assert "Queue the ZIP now" in html
    assert "without" in html
    assert "starting Patreon, MEGA, or X" in html
    assert "delivery:prepare-archive" in html
    assert "Prepare ZIP download" in html


@pytest.mark.asyncio
async def test_delivery_page_uses_shared_failed_archive_state_without_polling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _snapshot()
    archive = _archive(snapshot, state=FinishedSetArchiveState.FAILED, part_count=0)

    async def load(*_args: object, **_kwargs: object) -> OperatorDeliverySnapshot:
        return snapshot

    async def no_watermarks(*_args: object, **_kwargs: object) -> tuple[()]:
        return ()

    async def load_archive(*_args: object, **_kwargs: object) -> FinishedSetArchiveSnapshot:
        return archive

    monkeypatch.setattr(delivery_routes, "load_operator_delivery", load)
    monkeypatch.setattr(delivery_routes, "load_finished_set_archive", load_archive)
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
    assert "ZIP creation failed. Request a clean retry" in html
    assert "Retry ZIP preparation" in html
    assert "data-delivery-progress-url=" in html


@pytest.mark.asyncio
async def test_delivery_page_uses_shared_ready_archive_state_without_polling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _snapshot(publishing_guard_enabled=False)
    archive = _archive(snapshot)

    async def load(*_args: object, **_kwargs: object) -> OperatorDeliverySnapshot:
        return snapshot

    async def no_watermarks(*_args: object, **_kwargs: object) -> tuple[()]:
        return ()

    async def load_archive(*_args: object, **_kwargs: object) -> FinishedSetArchiveSnapshot:
        return archive

    monkeypatch.setattr(delivery_routes, "load_operator_delivery", load)
    monkeypatch.setattr(delivery_routes, "load_finished_set_archive", load_archive)
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
    assert "data-delivery-progress-url=" in html


@pytest.mark.parametrize(
    (
        "target",
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
        ("patreon", True, True, 0, False, True, "not_prepared", False, True),
        ("patreon", False, True, 0, False, True, "not_prepared", False, False),
        ("patreon", True, False, 0, False, True, "not_prepared", False, False),
        ("patreon", True, True, 0, False, False, "not_prepared", False, False),
        ("patreon", True, True, 0, False, True, "queued", True, False),
        ("patreon", True, True, 0, False, True, "failed", True, True),
        ("x", True, True, 0, True, True, "not_prepared", False, False),
        ("x", True, True, 1, False, True, "not_prepared", False, False),
        ("x", True, True, 1, True, True, "not_prepared", False, True),
        ("x", True, True, 1, True, True, "queued", True, False),
        ("x", True, True, 1, True, True, "failed", True, True),
    ],
)
def test_each_destination_form_is_available_only_for_its_target(
    target: str,
    publishing_enabled: bool,
    guard_enabled: bool,
    x_selected_count: int,
    x_configured: bool,
    ready: bool,
    destination_state: str,
    has_intent: bool,
    expected: bool,
) -> None:
    destination = DestinationState(
        key=target,
        label=target.title(),
        state=destination_state,
        detail="Destination detail.",
        intent_id=uuid4() if has_intent else None,
    )
    progress = _progress(
        ready_for_destinations=ready,
        ready_full_outputs=2 if ready or target == "x" else 0,
        ready_x_teasers=x_selected_count if ready and target == "x" else 0,
    )
    snapshot = _snapshot(
        progress=progress,
        patreon=destination if target == "patreon" else None,
        x=destination if target == "x" else None,
        publishing_guard_enabled=guard_enabled,
        x_selected_count=x_selected_count,
    )
    settings = _settings(
        publishing_enabled=publishing_enabled,
        x_configured=x_configured,
    )

    assert (
        delivery_routes._can_prepare_target_destination(
            snapshot,
            settings=settings,
            target=target,
        )
        is expected
    )
