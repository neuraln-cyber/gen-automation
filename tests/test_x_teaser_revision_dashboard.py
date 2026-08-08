from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from starlette.requests import Request
from starlette.responses import HTMLResponse

from gen_automation.api.browser_delivery_forms import delivery_form_key
from gen_automation.api.routes import delivery_dashboard as delivery_routes
from gen_automation.config import Environment, Settings
from gen_automation.domain.enums import AdminRole, ReleasePhase, ReviewTaskState
from gen_automation.services.authentication import AuthenticatedPrincipal
from gen_automation.services.operator_delivery import (
    DeliveryOutput,
    DeliverySource,
    DerivativeProgress,
    DestinationState,
    OperatorDeliverySnapshot,
)
from gen_automation.services.watermarks import RegisteredWatermark
from gen_automation.services.x_teaser_revisions import XTeaserRevisionStatus


def _settings() -> Settings:
    return Settings(
        environment=Environment.TEST,
        auth_enabled=False,
        auth_development_bypass_enabled=True,
        session_secret="x-revision-dashboard-test-secret-long-enough",  # noqa: S106
    )


def _owner() -> AuthenticatedPrincipal:
    now = datetime.now(UTC)
    return AuthenticatedPrincipal(
        session_id=uuid4(),
        user_id=uuid4(),
        username="x-revision-owner",
        display_name="X Revision Owner",
        role=AdminRole.OWNER,
        csrf_sha256="a" * 64,
        expires_at=now + timedelta(hours=1),
        idle_expires_at=now + timedelta(hours=1),
        reauthenticated_at=now,
        mfa_verified_at=now,
    )


def _x_output(asset_id: UUID, order: int) -> DeliveryOutput:
    return DeliveryOutput(
        output_id=uuid4(),
        selection_id=uuid4(),
        display_order=order,
        target="x_teaser",
        object_key=f"x/{order}.png",
        object_version_id=f"x-version-{order}",
        width=1150,
        height=1487,
        source_asset_id=asset_id,
        source_sha256=f"{order}" * 64,
    )


@pytest.mark.asyncio
async def test_delivery_route_keeps_active_downloads_while_replacement_is_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    owner = _owner()
    app = FastAPI()
    app.state.settings = settings
    app.state.object_store = object()
    review_task_id = uuid4()
    release_version_id = uuid4()
    watermark_asset_id = uuid4()
    source_ids = tuple(uuid4() for _ in range(4))
    positions = dict(
        zip(
            source_ids,
            ("top_left", "top_right", "bottom_left", "bottom_right"),
            strict=True,
        )
    )
    x_outputs = tuple(
        _x_output(asset_id, order) for order, asset_id in enumerate(source_ids, start=1)
    )
    snapshot = OperatorDeliverySnapshot(
        review_task_id=review_task_id,
        review_state=ReviewTaskState.COMPLETED,
        release_id=uuid4(),
        release_version_id=release_version_id,
        release_title="Akali - League of Legends",
        release_phase=ReleasePhase.READY_TO_PUBLISH,
        x_selected_count=4,
        progress=DerivativeProgress(
            planned=True,
            total_jobs=4,
            requested=0,
            running=0,
            retrying=0,
            succeeded=4,
            failed=0,
            expected_full_outputs=199,
            ready_full_outputs=0,
            expected_x_teasers=4,
            ready_x_teasers=4,
            ready_for_destinations=False,
        ),
        full_outputs=(),
        x_outputs=x_outputs,
        publishing_guard_enabled=False,
        publishing_guard_epoch=1,
        publishing_guard_lock_version=1,
        publishing_guard_changed_at=datetime.now(UTC),
        destinations=(
            DestinationState("mega", "MEGA", "not_prepared", "Not prepared."),
            DestinationState("patreon", "Patreon", "not_prepared", "Not prepared."),
            DestinationState("x", "X", "not_prepared", "Not prepared."),
        ),
        x_selected_sources=tuple(
            DeliverySource(
                asset_id=asset_id,
                display_order=order,
                width=1150,
                height=1487,
                sha256=f"{order}" * 64,
            )
            for order, asset_id in enumerate(source_ids, start=1)
        ),
    )
    revision_status = XTeaserRevisionStatus(
        active_revision_id=uuid4(),
        active_revision_no=1,
        pending_revision_id=uuid4(),
        pending_state="running",
        pending_total=4,
        pending_succeeded=2,
        pending_failed=0,
        can_replace=False,
        blocked_reason="A replacement teaser render is already in progress.",
        current_watermark_asset_id=watermark_asset_id,
        current_positions_by_asset_id=positions,
    )
    watermark = RegisteredWatermark(
        asset_id=watermark_asset_id,
        display_name="Neural Nymphs",
        sha256="f" * 64,
        storage_backend="s3",
        storage_bucket="private",
        object_key="watermarks/neural-nymphs.png",
        object_version_id="version-1",
        width=320,
        height=160,
        byte_size=4096,
        registered_at=datetime.now(UTC),
        replayed=False,
    )
    captured: dict[str, object] = {}

    async def load_snapshot(*_args: object, **_kwargs: object) -> OperatorDeliverySnapshot:
        return snapshot

    async def load_archive(*_args: object, **_kwargs: object) -> None:
        return None

    async def list_watermarks(*_args: object, **_kwargs: object):
        return (watermark,)

    async def load_revision(*_args: object, **_kwargs: object) -> XTeaserRevisionStatus:
        return revision_status

    def template_response(*, request: Request, name: str, context: dict[str, object]):
        captured.update(context)
        captured["template_name"] = name
        return HTMLResponse("ok")

    monkeypatch.setattr(delivery_routes, "load_operator_delivery", load_snapshot)
    monkeypatch.setattr(delivery_routes, "load_finished_set_archive", load_archive)
    monkeypatch.setattr(delivery_routes, "list_registered_watermarks", list_watermarks)
    monkeypatch.setattr(
        delivery_routes,
        "x_teaser_revision_status",
        load_revision,
        raising=False,
    )
    monkeypatch.setattr(delivery_routes.templates, "TemplateResponse", template_response)
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": f"/dashboard/review-tasks/{review_task_id}/delivery",
            "headers": [],
            "app": app,
        }
    )

    response = await delivery_routes.dashboard_review_delivery(
        review_task_id,
        request,
        SimpleNamespace(),  # type: ignore[arg-type]
        owner,
    )

    assert response.status_code == 200
    assert captured["template_name"] == "dashboard/delivery.html"
    assert captured["x_teaser_revision"] == revision_status
    assert revision_status.pending_state == "running"
    assert revision_status.pending_succeeded == 2
    assert len(captured["x_output_downloads"]) == 4  # type: ignore[arg-type]
    replacement_submission_id = captured["x_replace_output_submission_id"]
    assert isinstance(replacement_submission_id, UUID)
    assert captured["x_replace_output_idempotency_key"] == delivery_form_key(
        settings,
        session_id=owner.session_id,
        action="replace-x-outputs",
        parts=(str(review_task_id), str(replacement_submission_id)),
    )
