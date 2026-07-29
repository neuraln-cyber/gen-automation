"""One-action handoff from a completed human review to derivative jobs."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

import PIL
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from gen_automation.db.models import ReviewXSelection
from gen_automation.services.derivative_pipeline import (
    DerivativePipelineConflictError,
    DerivativePlanResult,
    create_derivative_recipe_and_plan,
)
from gen_automation.services.derivative_runtime import derivative_recipe_configuration
from gen_automation.services.derivatives import (
    DERIVATIVE_RENDERER_VERSION,
    DerivativeRecipe,
    WatermarkSpec,
)


async def prepare_completed_review_derivatives(
    session: AsyncSession,
    *,
    review_task_id: UUID,
    actor_user_id: UUID,
    idempotency_key: str,
    watermark_asset_id: UUID | None = None,
    max_attempts: int = 3,
    now: datetime | None = None,
) -> DerivativePlanResult:
    """Create every clean full output and only selected, watermarked X teasers."""

    x_selected_count = int(
        await session.scalar(
            select(func.count())
            .select_from(ReviewXSelection)
            .where(ReviewXSelection.review_task_id == review_task_id)
        )
        or 0
    )
    if x_selected_count and watermark_asset_id is None:
        raise DerivativePipelineConflictError("selected X images require a registered watermark")
    if not x_selected_count and watermark_asset_id is not None:
        raise DerivativePipelineConflictError(
            "a watermark is only accepted when the review has selected X images"
        )

    recipe = DerivativeRecipe(
        watermark=WatermarkSpec() if x_selected_count else None,
    )
    output_targets = ("full", "x_teaser") if x_selected_count else ("full",)
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
        output_targets=output_targets,
        watermark_asset_id=watermark_asset_id,
        max_attempts=max_attempts,
        now=now,
    )
