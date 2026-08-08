"""One-action handoff from a completed human review to derivative jobs."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from uuid import UUID

import PIL
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from gen_automation.db.models import ReviewXSelection
from gen_automation.services.derivative_pipeline import (
    DerivativePipelineConflictError,
    DerivativePlanResult,
    DerivativeRetryResult,
    create_derivative_recipe_and_plan,
    retry_failed_completed_review_target,
)
from gen_automation.services.derivative_runtime import derivative_recipe_configuration
from gen_automation.services.derivatives import (
    DERIVATIVE_RENDERER_VERSION,
    DerivativeRecipe,
    WatermarkPosition,
    WatermarkSpec,
)


async def prepare_completed_review_full_outputs(
    session: AsyncSession,
    *,
    review_task_id: UUID,
    actor_user_id: UUID,
    idempotency_key: str,
    max_attempts: int = 3,
    now: datetime | None = None,
) -> DerivativePlanResult:
    """Plan only clean full-resolution outputs, independent from X selections."""

    recipe = DerivativeRecipe(watermark=None)
    return await create_derivative_recipe_and_plan(
        session,
        review_task_id=review_task_id,
        configuration=derivative_recipe_configuration(recipe),
        recipe_version=1,
        renderer_version=DERIVATIVE_RENDERER_VERSION,
        pillow_version=PIL.__version__,
        created_by_user_id=actor_user_id,
        approved_by_user_id=actor_user_id,
        idempotency_key=idempotency_key,
        output_targets=("full",),
        watermark_asset_id=None,
        max_attempts=max_attempts,
        now=now,
    )


async def prepare_completed_review_x_teasers(
    session: AsyncSession,
    *,
    review_task_id: UUID,
    actor_user_id: UUID,
    idempotency_key: str,
    watermark_asset_id: UUID | None,
    watermark_position: WatermarkPosition = WatermarkPosition.BOTTOM_RIGHT,
    max_attempts: int = 3,
    now: datetime | None = None,
) -> DerivativePlanResult:
    """Plan only the explicitly selected, watermarked X teaser outputs."""

    if await _x_selected_count(session, review_task_id=review_task_id) == 0:
        raise DerivativePipelineConflictError("X teaser preparation requires selected X images")
    if watermark_asset_id is None:
        raise DerivativePipelineConflictError("selected X images require a registered watermark")
    recipe = DerivativeRecipe(watermark=WatermarkSpec(position=watermark_position))
    return await create_derivative_recipe_and_plan(
        session,
        review_task_id=review_task_id,
        configuration=derivative_recipe_configuration(recipe),
        recipe_version=1,
        renderer_version=DERIVATIVE_RENDERER_VERSION,
        pillow_version=PIL.__version__,
        created_by_user_id=actor_user_id,
        approved_by_user_id=actor_user_id,
        idempotency_key=idempotency_key,
        output_targets=("x_teaser",),
        watermark_asset_id=watermark_asset_id,
        max_attempts=max_attempts,
        now=now,
    )


async def retry_failed_completed_review_full_outputs(
    session: AsyncSession,
    *,
    review_task_id: UUID,
    actor_user_id: UUID,
    idempotency_key: str,
    retry_allowance: int = 3,
    expected_failed_job_ids: Sequence[UUID] | None = None,
    now: datetime | None = None,
) -> DerivativeRetryResult:
    """Re-arm only failed clean full-output jobs without changing their recipe."""

    return await retry_failed_completed_review_target(
        session,
        review_task_id=review_task_id,
        actor_user_id=actor_user_id,
        idempotency_key=idempotency_key,
        target="full",
        retry_allowance=retry_allowance,
        expected_failed_job_ids=expected_failed_job_ids,
        now=now,
    )


async def prepare_completed_review_derivatives(
    session: AsyncSession,
    *,
    review_task_id: UUID,
    actor_user_id: UUID,
    idempotency_key: str,
    watermark_asset_id: UUID | None = None,
    watermark_position: WatermarkPosition = WatermarkPosition.BOTTOM_RIGHT,
    max_attempts: int = 3,
    now: datetime | None = None,
) -> DerivativePlanResult:
    """Compatibility wrapper over the two independent derivative plans."""

    x_selected_count = await _x_selected_count(session, review_task_id=review_task_id)
    if x_selected_count and watermark_asset_id is None:
        raise DerivativePipelineConflictError("selected X images require a registered watermark")
    if not x_selected_count and watermark_asset_id is not None:
        raise DerivativePipelineConflictError(
            "a watermark is only accepted when the review has selected X images"
        )

    planned_at = now or datetime.now(UTC)
    full = await prepare_completed_review_full_outputs(
        session,
        review_task_id=review_task_id,
        actor_user_id=actor_user_id,
        idempotency_key=_target_idempotency_key(idempotency_key, "full"),
        max_attempts=max_attempts,
        now=planned_at,
    )
    if not x_selected_count:
        return full
    x_teasers = await prepare_completed_review_x_teasers(
        session,
        review_task_id=review_task_id,
        actor_user_id=actor_user_id,
        idempotency_key=_target_idempotency_key(idempotency_key, "x_teaser"),
        watermark_asset_id=watermark_asset_id,
        watermark_position=watermark_position,
        max_attempts=max_attempts,
        now=planned_at + timedelta(microseconds=1),
    )
    return DerivativePlanResult(
        review_task_id=full.review_task_id,
        recipe_id=full.recipe_id,
        release_version_id=full.release_version_id,
        job_ids=full.job_ids + x_teasers.job_ids,
        jobs_created=full.jobs_created + x_teasers.jobs_created,
        total_jobs=full.total_jobs + x_teasers.total_jobs,
        replayed=full.replayed and x_teasers.replayed,
    )


async def _x_selected_count(session: AsyncSession, *, review_task_id: UUID) -> int:
    return int(
        await session.scalar(
            select(func.count())
            .select_from(ReviewXSelection)
            .where(ReviewXSelection.review_task_id == review_task_id)
        )
        or 0
    )


def _target_idempotency_key(idempotency_key: str, target: str) -> str:
    candidate = f"{idempotency_key}:{target}"
    if len(candidate) <= 200:
        return candidate
    return hashlib.sha256(candidate.encode("utf-8")).hexdigest()
