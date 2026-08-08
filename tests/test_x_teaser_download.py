# ruff: noqa: F811

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from sqlalchemy import delete
from starlette.requests import Request

from gen_automation.api.routes.delivery_dashboard import (
    dashboard_delivery_watermark_preview,
    dashboard_delivery_x_teaser_download,
    dashboard_delivery_x_teaser_preview,
)
from gen_automation.config import Environment, Settings
from gen_automation.db.models import ReviewXSelection
from gen_automation.domain.enums import AdminRole
from gen_automation.services.authentication import AuthenticatedPrincipal
from gen_automation.services.operator_delivery import load_operator_delivery
from gen_automation.services.outbound_image_privacy import require_metadata_free_image
from gen_automation.services.watermarks import register_watermark
from gen_automation.services.x_teaser_access import (
    XTeaserAccessNotFoundError,
    read_review_x_teaser,
)
from tests.test_derivative_pipeline import ApprovedContext
from tests.test_derivative_pipeline import (
    approved_context as derivative_approved_context,  # noqa: F401
)
from tests.test_derivative_runtime import _cycle, _prepare, _watermark_png


def _principal(user_id: UUID, *, role: AdminRole = AdminRole.OWNER) -> AuthenticatedPrincipal:
    now = datetime.now(UTC)
    return AuthenticatedPrincipal(
        session_id=uuid4(),
        user_id=user_id,
        username="teaser-owner",
        display_name="Teaser Owner",
        role=role,
        csrf_sha256="a" * 64,
        expires_at=now,
        idle_expires_at=now,
        reauthenticated_at=now,
        mfa_verified_at=now,
    )


def _request(store: object, path: str) -> Request:
    app = FastAPI()
    app.state.settings = Settings(
        environment=Environment.TEST,
        auth_enabled=False,
        auth_development_bypass_enabled=True,
    )
    app.state.object_store = store
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "headers": [],
            "app": app,
        }
    )


async def _render_one_x_teaser(approved: ApprovedContext):
    prepared = await _prepare(
        approved,
        with_watermark=True,
        x_selected_asset_ids=(approved.raw_asset_ids[0],),
    )
    for _ in range(3):
        await _cycle(prepared, worker_id="x-teaser-download-worker")
    async with approved.database.sessions() as session:
        snapshot = await load_operator_delivery(session, review_task_id=approved.review_task_id)
    assert len(snapshot.x_outputs) == 1
    return prepared, snapshot.x_outputs[0]


@pytest.mark.asyncio
async def test_read_x_teaser_is_exact_metadata_free_and_review_scoped(
    derivative_approved_context: ApprovedContext,
) -> None:
    approved = derivative_approved_context
    prepared, output = await _render_one_x_teaser(approved)

    async with approved.database.sessions() as session:
        payload = await read_review_x_teaser(
            session,
            prepared.store,
            review_task_id=approved.review_task_id,
            output_id=output.output_id,
        )
        with pytest.raises(XTeaserAccessNotFoundError):
            await read_review_x_teaser(
                session,
                prepared.store,
                review_task_id=uuid4(),
                output_id=output.output_id,
            )
        await session.execute(
            delete(ReviewXSelection).where(
                ReviewXSelection.review_task_id == approved.review_task_id,
                ReviewXSelection.asset_id == approved.raw_asset_ids[0],
            )
        )
        await session.commit()
        with pytest.raises(XTeaserAccessNotFoundError):
            await read_review_x_teaser(
                session,
                prepared.store,
                review_task_id=approved.review_task_id,
                output_id=output.output_id,
            )

    assert payload.output_id == output.output_id
    assert payload.display_order == output.display_order
    assert payload.width == output.width
    assert payload.height == output.height
    require_metadata_free_image(payload.data, content_type=payload.content_type)


@pytest.mark.asyncio
async def test_owner_can_preview_and_download_exact_x_teaser_independently(
    derivative_approved_context: ApprovedContext,
) -> None:
    approved = derivative_approved_context
    prepared, output = await _render_one_x_teaser(approved)
    path = (
        f"/dashboard/review-tasks/{approved.review_task_id}/delivery/x-teasers/{output.output_id}"
    )
    principal = _principal(approved.owner_id)

    async with approved.database.sessions() as session:
        preview = await dashboard_delivery_x_teaser_preview(
            approved.review_task_id,
            output.output_id,
            _request(prepared.store, f"{path}/preview"),
            session,
            principal,
        )
        download = await dashboard_delivery_x_teaser_download(
            approved.review_task_id,
            output.output_id,
            _request(prepared.store, f"{path}/download"),
            session,
            principal,
        )
        forbidden = await dashboard_delivery_x_teaser_download(
            approved.review_task_id,
            output.output_id,
            _request(prepared.store, f"{path}/download"),
            session,
            _principal(approved.owner_id, role=AdminRole.PUBLISHER),
        )

    assert preview.status_code == 200
    assert download.status_code == 200
    assert preview.body == download.body
    assert preview.headers["content-disposition"].startswith("inline;")
    assert download.headers["content-disposition"].startswith("attachment;")
    assert download.headers["cache-control"] == "private, no-store"
    assert download.headers["x-content-type-options"] == "nosniff"
    assert forbidden.status_code == 403


@pytest.mark.asyncio
async def test_saved_watermark_preview_is_owner_only_and_exact(
    derivative_approved_context: ApprovedContext,
) -> None:
    approved = derivative_approved_context
    prepared = await _prepare(approved)
    source = _watermark_png()
    async with approved.database.sessions() as session:
        registered = await register_watermark(
            session,
            prepared.store,
            release_id=approved.release_id,
            display_name="Reusable brand",
            png_bytes=source,
            registered_by_user_id=approved.owner_id,
            idempotency_key="saved-watermark-preview",
        )
        path = (
            f"/dashboard/review-tasks/{approved.review_task_id}/delivery/watermarks/"
            f"{registered.asset_id}/preview"
        )
        response = await dashboard_delivery_watermark_preview(
            approved.review_task_id,
            registered.asset_id,
            _request(prepared.store, path),
            session,
            _principal(approved.owner_id),
        )
        arbitrary = await dashboard_delivery_watermark_preview(
            approved.review_task_id,
            approved.raw_asset_ids[0],
            _request(prepared.store, path),
            session,
            _principal(approved.owner_id),
        )

    assert response.status_code == 200
    assert response.body == source
    assert response.media_type == "image/png"
    assert response.headers["content-disposition"].startswith("inline;")
    assert response.headers["cross-origin-resource-policy"] == "same-origin"
    assert arbitrary.status_code == 404
