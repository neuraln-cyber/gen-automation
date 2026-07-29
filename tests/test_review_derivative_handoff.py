# ruff: noqa: F811

from datetime import timedelta

import pytest
from sqlalchemy import select

from gen_automation.db.models import (
    DerivativeJob,
    Release,
    ReviewXSelection,
)
from gen_automation.domain.enums import ReleasePhase
from gen_automation.domain.ids import uuid7
from gen_automation.services.review_derivatives import (
    prepare_completed_review_derivatives,
)
from gen_automation.services.watermarks import (
    WatermarkInputError,
    list_registered_watermarks,
    register_watermark,
)
from gen_automation.storage.memory import MemoryObjectStore
from tests.test_derivative_pipeline import PLAN_AT, ApprovedContext
from tests.test_derivative_pipeline import (
    approved_context as derivative_approved_context,  # noqa: F401
)
from tests.test_derivative_runtime import _watermark_png


@pytest.mark.asyncio
async def test_one_action_plans_full_set_and_only_selected_watermarked_teaser(
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

        plan = await prepare_completed_review_derivatives(
            session,
            review_task_id=approved.review_task_id,
            actor_user_id=approved.owner_id,
            idempotency_key="prepare-reviewed-set",
            watermark_asset_id=watermark.asset_id,
            now=PLAN_AT + timedelta(minutes=1),
        )
        assert plan.jobs_created == 2

        jobs = tuple(
            (await session.scalars(select(DerivativeJob).order_by(DerivativeJob.logical_key))).all()
        )
        targets_by_source = {
            job.release_selection_id: tuple(job.request_payload["output_targets"]) for job in jobs
        }
        selected_job = next(
            job
            for job in jobs
            if job.request_payload["source"]["asset_id"] == str(approved.raw_asset_ids[0])
        )
        clean_job = next(
            job
            for job in jobs
            if job.request_payload["source"]["asset_id"] == str(approved.raw_asset_ids[1])
        )
        assert targets_by_source[selected_job.release_selection_id] == (
            "full",
            "x_teaser",
        )
        assert targets_by_source[clean_job.release_selection_id] == ("full",)
        assert selected_job.request_payload["recipe"]["watermark_asset_id"] == str(
            watermark.asset_id
        )


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
