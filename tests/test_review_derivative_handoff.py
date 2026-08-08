# ruff: noqa: F811

import io
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from fastapi import FastAPI, HTTPException, Response, UploadFile
from PIL import Image
from sqlalchemy import select
from starlette.datastructures import Headers
from starlette.requests import Request

from gen_automation.api.routes.derivatives import (
    PrepareDerivativesRequest,
    _derivative_http_error,
    _watermark_http_error,
    get_watermarks,
    post_review_derivative_plan,
    post_watermark,
)
from gen_automation.db.models import (
    DerivativeJob,
    Release,
    ReviewXSelection,
)
from gen_automation.domain.enums import AdminRole, ReleasePhase
from gen_automation.domain.ids import uuid7
from gen_automation.services.authentication import AuthenticatedPrincipal
from gen_automation.services.derivative_pipeline import (
    DerivativePipelineConflictError,
    DerivativePipelineInputError,
    DerivativePipelineNotFoundError,
)
from gen_automation.services.review_derivatives import (
    prepare_completed_review_derivatives,
    prepare_completed_review_full_outputs,
    prepare_completed_review_x_teasers,
)
from gen_automation.services.watermarks import (
    WatermarkConflictError,
    WatermarkInputError,
    WatermarkNotFoundError,
    WatermarkStorageError,
    list_registered_watermarks,
    register_watermark,
)
from gen_automation.storage.memory import MemoryObjectStore
from tests.test_derivative_pipeline import PLAN_AT, ApprovedContext
from tests.test_derivative_pipeline import (
    approved_context as derivative_approved_context,  # noqa: F401
)
from tests.test_derivative_runtime import _watermark_png


def _principal(user_id: UUID) -> AuthenticatedPrincipal:
    now = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
    return AuthenticatedPrincipal(
        session_id=uuid7(),
        user_id=user_id,
        username="owner",
        display_name="Owner",
        role=AdminRole.OWNER,
        csrf_sha256="a" * 64,
        expires_at=now + timedelta(hours=1),
        idle_expires_at=now + timedelta(hours=1),
        reauthenticated_at=now,
        mfa_verified_at=now,
    )


def _request(store: MemoryObjectStore | None) -> Request:
    app = FastAPI()
    app.state.object_store = store
    return Request({"type": "http", "app": app})


def _upload(payload: bytes, *, content_type: str = "image/png") -> UploadFile:
    return UploadFile(
        file=io.BytesIO(payload),
        filename="watermark.png",
        size=len(payload),
        headers=Headers({"content-type": content_type}),
    )


@pytest.mark.asyncio
async def test_full_outputs_and_x_teasers_are_planned_independently(
    derivative_approved_context: ApprovedContext,
) -> None:
    approved = derivative_approved_context
    store = MemoryObjectStore(bucket="watermark-registry")
    async with approved.database.sessions() as session:
        release = await session.get(Release, approved.release_id)
        assert release is not None
        library = Release(
            project_id=release.project_id,
            slug="watermark-library",
            title="Watermark library",
            phase=ReleasePhase.DRAFT,
            current_version_no=1,
            desired_accepted_count=1,
            lock_version=1,
        )
        session.add(library)
        await session.commit()

        watermark = await register_watermark(
            session,
            store,
            release_id=library.id,
            display_name="Primary X watermark",
            png_bytes=_watermark_png(),
            registered_by_user_id=approved.owner_id,
            idempotency_key="register-primary-watermark",
            now=PLAN_AT,
        )
        replay = await register_watermark(
            session,
            store,
            release_id=library.id,
            display_name="Primary X watermark",
            png_bytes=_watermark_png(),
            registered_by_user_id=approved.owner_id,
            idempotency_key="register-primary-watermark",
            now=PLAN_AT,
        )
        assert replay.replayed is True
        assert replay.asset_id == watermark.asset_id
        assert watermark.object_key.startswith("watermarks/")
        assert tuple(item.asset_id for item in await list_registered_watermarks(session)) == (
            watermark.asset_id,
        )
        alternate_payload = io.BytesIO()
        with Image.open(io.BytesIO(_watermark_png())) as alternate_image:
            alternate_image = alternate_image.convert("RGBA")
            alternate_image.putpixel((4, 3), (254, 255, 255, 220))
            alternate_image.save(alternate_payload, format="PNG")
        alternate_watermark = await register_watermark(
            session,
            store,
            release_id=library.id,
            display_name="Alternate X watermark",
            png_bytes=alternate_payload.getvalue(),
            registered_by_user_id=approved.owner_id,
            idempotency_key="register-alternate-watermark",
            now=PLAN_AT,
        )

        session.add(
            ReviewXSelection(
                id=uuid7(),
                review_task_id=approved.review_task_id,
                asset_id=approved.raw_asset_ids[0],
                selected_by_user_id=approved.owner_id,
                selected_at=PLAN_AT,
            )
        )
        await session.commit()

        full_plan = await prepare_completed_review_full_outputs(
            session,
            review_task_id=approved.review_task_id,
            actor_user_id=approved.owner_id,
            idempotency_key="prepare-reviewed-full-set",
            now=PLAN_AT + timedelta(minutes=1),
        )
        assert full_plan.jobs_created == 2
        assert full_plan.total_jobs == 2
        full_replay = await prepare_completed_review_full_outputs(
            session,
            review_task_id=approved.review_task_id,
            actor_user_id=approved.owner_id,
            idempotency_key="prepare-reviewed-full-set",
            now=PLAN_AT + timedelta(minutes=1),
        )
        assert full_replay.replayed is True
        assert full_replay.job_ids == full_plan.job_ids

        release = await session.get(Release, approved.release_id)
        assert release is not None
        release.phase = ReleasePhase.READY_TO_PUBLISH
        release.lock_version += 1
        await session.commit()

        x_plan = await prepare_completed_review_x_teasers(
            session,
            review_task_id=approved.review_task_id,
            actor_user_id=approved.owner_id,
            idempotency_key="prepare-reviewed-x-teasers",
            watermark_asset_id=watermark.asset_id,
            now=PLAN_AT + timedelta(minutes=2),
        )
        assert x_plan.jobs_created == 1
        assert x_plan.total_jobs == 1
        release = await session.get(Release, approved.release_id)
        assert release is not None
        assert release.phase == ReleasePhase.RENDERING
        replay_plan = await prepare_completed_review_x_teasers(
            session,
            review_task_id=approved.review_task_id,
            actor_user_id=approved.owner_id,
            idempotency_key="prepare-reviewed-x-teasers",
            watermark_asset_id=watermark.asset_id,
            now=PLAN_AT + timedelta(minutes=2),
        )
        assert replay_plan.replayed is True
        assert replay_plan.job_ids == x_plan.job_ids
        assert replay_plan.total_jobs == 1
        with pytest.raises(
            DerivativePipelineConflictError,
            match="selection target already has a different frozen recipe",
        ):
            await prepare_completed_review_x_teasers(
                session,
                review_task_id=approved.review_task_id,
                actor_user_id=approved.owner_id,
                idempotency_key="prepare-reviewed-x-teasers-alternate",
                watermark_asset_id=alternate_watermark.asset_id,
                now=PLAN_AT + timedelta(minutes=3),
            )
        await session.rollback()

        compatibility_plan = await prepare_completed_review_derivatives(
            session,
            review_task_id=approved.review_task_id,
            actor_user_id=approved.owner_id,
            idempotency_key="prepare-reviewed-compatible",
            watermark_asset_id=watermark.asset_id,
            now=PLAN_AT + timedelta(minutes=4),
        )
        assert compatibility_plan.jobs_created == 0
        assert compatibility_plan.total_jobs == 3

        jobs = tuple(
            (await session.scalars(select(DerivativeJob).order_by(DerivativeJob.logical_key))).all()
        )
        jobs_by_asset: dict[UUID, list[DerivativeJob]] = {}
        for job in jobs:
            source_asset_id = UUID(job.request_payload["source"]["asset_id"])
            jobs_by_asset.setdefault(source_asset_id, []).append(job)
        selected_jobs = jobs_by_asset[approved.raw_asset_ids[0]]
        clean_jobs = jobs_by_asset[approved.raw_asset_ids[1]]
        selected_x_job = next(
            job
            for job in selected_jobs
            if tuple(job.request_payload["output_targets"]) == ("x_teaser",)
        )
        assert {tuple(job.request_payload["output_targets"]) for job in selected_jobs} == {
            ("full",),
            ("x_teaser",),
        }
        assert [tuple(job.request_payload["output_targets"]) for job in clean_jobs] == [("full",)]
        assert selected_x_job.request_payload["recipe"]["watermark_asset_id"] == str(
            watermark.asset_id
        )
        full_jobs = [
            job for job in jobs if tuple(job.request_payload["output_targets"]) == ("full",)
        ]
        assert all(job.request_payload["recipe"]["watermark_asset_id"] is None for job in full_jobs)
        assert all(job.available_at < selected_x_job.available_at for job in full_jobs)


@pytest.mark.asyncio
async def test_x_teaser_plan_requires_selection_and_registered_watermark(
    derivative_approved_context: ApprovedContext,
) -> None:
    approved = derivative_approved_context
    async with approved.database.sessions() as session:
        with pytest.raises(
            DerivativePipelineConflictError,
            match="requires selected X images",
        ):
            await prepare_completed_review_x_teasers(
                session,
                review_task_id=approved.review_task_id,
                actor_user_id=approved.owner_id,
                idempotency_key="x-without-selection",
                watermark_asset_id=None,
                now=PLAN_AT,
            )

        session.add(
            ReviewXSelection(
                id=uuid7(),
                review_task_id=approved.review_task_id,
                asset_id=approved.raw_asset_ids[0],
                selected_by_user_id=approved.owner_id,
                selected_at=PLAN_AT,
            )
        )
        await session.commit()
        with pytest.raises(
            DerivativePipelineConflictError,
            match="require a registered watermark",
        ):
            await prepare_completed_review_x_teasers(
                session,
                review_task_id=approved.review_task_id,
                actor_user_id=approved.owner_id,
                idempotency_key="x-without-watermark",
                watermark_asset_id=None,
                now=PLAN_AT,
            )


@pytest.mark.asyncio
async def test_operator_api_onboards_watermark_and_prepares_completed_review(
    derivative_approved_context: ApprovedContext,
) -> None:
    approved = derivative_approved_context
    store = MemoryObjectStore(bucket="watermark-api")
    principal = _principal(approved.owner_id)
    async with approved.database.sessions() as session:
        upload_response = Response()
        watermark = await post_watermark(
            request=_request(store),
            release_id=approved.release_id,
            display_name="API watermark",
            file=_upload(_watermark_png()),
            session=session,
            principal=principal,
            idempotency_key="api-register-watermark",
            response=upload_response,
        )
        assert upload_response.headers["Idempotency-Replayed"] == "false"
        replay_response = Response()
        replay = await post_watermark(
            request=_request(store),
            release_id=approved.release_id,
            display_name="API watermark",
            file=_upload(_watermark_png()),
            session=session,
            principal=principal,
            idempotency_key="api-register-watermark",
            response=replay_response,
        )
        assert replay.asset_id == watermark.asset_id
        assert replay_response.status_code == 200
        assert replay_response.headers["Idempotency-Replayed"] == "true"
        listed = await get_watermarks(session=session, _principal=principal)
        assert tuple(item.asset_id for item in listed) == (watermark.asset_id,)

        plan_response = Response()
        plan = await post_review_derivative_plan(
            review_task_id=approved.review_task_id,
            command=PrepareDerivativesRequest(),
            session=session,
            principal=principal,
            idempotency_key="api-prepare-derivatives",
            response=plan_response,
        )
        assert plan.jobs_created == 2
        assert plan_response.headers["Idempotency-Replayed"] == "false"
        replay_plan_response = Response()
        replay_plan = await post_review_derivative_plan(
            review_task_id=approved.review_task_id,
            command=PrepareDerivativesRequest(),
            session=session,
            principal=principal,
            idempotency_key="api-prepare-derivatives",
            response=replay_plan_response,
        )
        assert replay_plan.recipe_id == plan.recipe_id
        assert replay_plan_response.status_code == 200
        assert replay_plan_response.headers["Idempotency-Replayed"] == "true"


@pytest.mark.asyncio
async def test_watermark_upload_api_fails_closed_without_storage_or_png(
    derivative_approved_context: ApprovedContext,
) -> None:
    approved = derivative_approved_context
    principal = _principal(approved.owner_id)
    async with approved.database.sessions() as session:
        with pytest.raises(HTTPException) as unavailable:
            await post_watermark(
                request=_request(None),
                release_id=approved.release_id,
                display_name="Unavailable",
                file=_upload(_watermark_png()),
                session=session,
                principal=principal,
                idempotency_key="api-storage-unavailable",
                response=Response(),
            )
        assert unavailable.value.status_code == 503

        with pytest.raises(HTTPException) as invalid:
            await post_watermark(
                request=_request(MemoryObjectStore()),
                release_id=approved.release_id,
                display_name="Wrong type",
                file=_upload(_watermark_png(), content_type="image/jpeg"),
                session=session,
                principal=principal,
                idempotency_key="api-invalid-content-type",
                response=Response(),
            )
        assert invalid.value.status_code == 422


@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        (WatermarkInputError("sensitive-internal-detail"), 422),
        (WatermarkNotFoundError("sensitive-internal-detail"), 404),
        (WatermarkStorageError("sensitive-internal-detail"), 503),
        (WatermarkConflictError("sensitive-internal-detail"), 409),
    ],
)
def test_watermark_api_has_bounded_error_mapping(
    error: Exception,
    expected_status: int,
) -> None:
    response = _watermark_http_error(error)
    assert response.status_code == expected_status
    assert str(error) not in str(response.detail)


@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        (DerivativePipelineInputError("sensitive-internal-detail"), 422),
        (DerivativePipelineNotFoundError("sensitive-internal-detail"), 404),
        (DerivativePipelineConflictError("sensitive-internal-detail"), 409),
    ],
)
def test_derivative_handoff_api_has_bounded_error_mapping(
    error: Exception,
    expected_status: int,
) -> None:
    response = _derivative_http_error(error)
    assert response.status_code == expected_status
    assert str(error) not in str(response.detail)


@pytest.mark.asyncio
async def test_registered_watermark_requires_safe_alpha_png(
    derivative_approved_context: ApprovedContext,
) -> None:
    approved = derivative_approved_context
    store = MemoryObjectStore(bucket="watermark-registry")
    async with approved.database.sessions() as session:
        with pytest.raises(WatermarkInputError, match="transparent PNG"):
            await register_watermark(
                session,
                store,
                release_id=approved.release_id,
                display_name="Invalid",
                png_bytes=b"not-a-png",
                registered_by_user_id=approved.owner_id,
                idempotency_key="register-invalid-watermark",
                now=PLAN_AT,
            )
