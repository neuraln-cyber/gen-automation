"""One-action handoff from a completed human review to derivative jobs."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from uuid import UUID

import PIL
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from gen_automation.db.models import (
    DerivativeJob,
    ReviewXSelection,
    XTeaserRevisionMember,
)
from gen_automation.domain.canonical import canonical_sha256
from gen_automation.domain.deliverability import (
    MAX_PIPELINE_MASTER_HEIGHT,
    MAX_PIPELINE_MASTER_WIDTH,
)
from gen_automation.domain.ids import uuid7
from gen_automation.services.derivative_pipeline import (
    DerivativePipelineConflictError,
    DerivativePipelineInputError,
    DerivativePlanResult,
    DerivativeRetryResult,
    create_derivative_recipe_and_plan,
    retry_failed_completed_review_target,
)
from gen_automation.services.derivative_runtime import derivative_recipe_configuration
from gen_automation.services.derivatives import (
    DERIVATIVE_RENDERER_VERSION,
    DerivativeRecipe,
    PngEncoding,
    TeaserFitMode,
    WatermarkPosition,
    WatermarkSpec,
    XTeaserSpec,
)
from gen_automation.services.x_teaser_revisions import (
    activate_ready_x_teaser_revision,
    create_pending_x_teaser_revision,
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
    watermark_positions_by_asset_id: Mapping[UUID, WatermarkPosition] | None = None,
    max_attempts: int = 3,
    now: datetime | None = None,
    require_active_revision: bool | None = None,
) -> DerivativePlanResult:
    """Plan only the explicitly selected, watermarked X teaser outputs."""

    selected_asset_ids = await _x_selected_asset_ids(
        session,
        review_task_id=review_task_id,
    )
    if not selected_asset_ids:
        raise DerivativePipelineConflictError("X teaser preparation requires selected X images")
    if watermark_asset_id is None:
        raise DerivativePipelineConflictError("selected X images require a registered watermark")
    positions = _resolved_watermark_positions(
        selected_asset_ids=selected_asset_ids,
        default_position=watermark_position,
        positions_by_asset_id=watermark_positions_by_asset_id,
    )
    groups: dict[WatermarkPosition, list[UUID]] = {}
    for asset_id in selected_asset_ids:
        groups.setdefault(positions[asset_id], []).append(asset_id)

    render_profile_sha256 = canonical_sha256(
        {
            "schema": "x-teaser-render-profile/v1",
            "recipe_version": 1,
            "renderer_version": DERIVATIVE_RENDERER_VERSION,
            "pillow_version": PIL.__version__,
            "configurations": {
                position.value: derivative_recipe_configuration(
                    _x_lossless_png_recipe(position=position)
                )
                for position in sorted(groups, key=lambda value: value.value)
            },
        }
    )

    planned_at = now or datetime.now(UTC)
    (
        revision,
        _revision_head,
        selections,
        revision_replayed,
        gates_release,
    ) = await create_pending_x_teaser_revision(
        session,
        review_task_id=review_task_id,
        watermark_asset_id=watermark_asset_id,
        positions_by_asset_id={
            asset_id: position.value for asset_id, position in positions.items()
        },
        actor_user_id=actor_user_id,
        idempotency_key=idempotency_key,
        created_at=planned_at,
        require_active_revision=require_active_revision,
        render_profile_sha256=render_profile_sha256,
    )
    if revision_replayed:
        members = tuple(
            (
                await session.scalars(
                    select(XTeaserRevisionMember)
                    .where(XTeaserRevisionMember.revision_id == revision.id)
                    .order_by(XTeaserRevisionMember.display_order)
                )
            ).all()
        )
        if not members:
            raise DerivativePipelineConflictError("replayed X teaser revision is incomplete")
        await session.commit()
        return DerivativePlanResult(
            review_task_id=review_task_id,
            recipe_id=members[0].derivative_recipe_id,
            release_version_id=revision.release_version_id,
            job_ids=tuple(
                member.derivative_job_id
                for member in members
                if member.derivative_job_id is not None
            ),
            jobs_created=0,
            total_jobs=sum(member.derivative_job_id is not None for member in members),
            replayed=True,
        )

    results: list[DerivativePlanResult] = []
    multiple_recipes = len(groups) > 1
    for offset, position in enumerate(WatermarkPosition):
        asset_ids = groups.get(position)
        if not asset_ids:
            continue
        recipe = _x_lossless_png_recipe(position=position)
        result = await create_derivative_recipe_and_plan(
            session,
            review_task_id=review_task_id,
            configuration=derivative_recipe_configuration(recipe),
            recipe_version=1,
            renderer_version=DERIVATIVE_RENDERER_VERSION,
            pillow_version=PIL.__version__,
            created_by_user_id=actor_user_id,
            approved_by_user_id=actor_user_id,
            idempotency_key=(
                _target_idempotency_key(idempotency_key, f"watermark-{position.value}")
                if multiple_recipes
                else idempotency_key
            ),
            output_targets=("x_teaser",),
            watermark_asset_id=watermark_asset_id,
            x_teaser_asset_ids=tuple(asset_ids),
            x_teaser_revision_id=revision.id,
            gates_release=gates_release,
            max_attempts=max_attempts,
            commit=False,
            now=planned_at + timedelta(microseconds=offset),
        )
        results.append(result)

    planned_jobs = tuple(
        (
            await session.scalars(
                select(DerivativeJob).where(DerivativeJob.x_teaser_revision_id == revision.id)
            )
        ).all()
    )
    jobs_by_asset = {UUID(job.request_payload["source"]["asset_id"]): job for job in planned_jobs}
    recipe_ids: list[UUID] = []
    for selection in selections:
        job = jobs_by_asset.get(selection.asset_id)
        if job is not None:
            recipe_id = job.derivative_recipe_id
            output_id = None
            job_id = job.id
        else:
            raise DerivativePipelineConflictError("X teaser revision member was not planned")
        recipe_ids.append(recipe_id)
        session.add(
            XTeaserRevisionMember(
                id=uuid7(),
                revision_id=revision.id,
                review_task_id=review_task_id,
                release_version_id=revision.release_version_id,
                release_selection_id=selection.id,
                source_asset_id=selection.asset_id,
                display_order=selection.display_order,
                watermark_position=positions[selection.asset_id].value,
                derivative_recipe_id=recipe_id,
                derivative_job_id=job_id,
                derivative_output_id=output_id,
                created_at=planned_at,
            )
        )
    await session.flush()
    if not planned_jobs:
        activated = await activate_ready_x_teaser_revision(
            session,
            revision_id=revision.id,
            activated_at=planned_at,
        )
        if not activated:
            raise DerivativePipelineConflictError("X teaser revision did not activate")
    await session.commit()
    if not recipe_ids:
        raise DerivativePipelineConflictError("X teaser revision has no recipes")
    first_result = results[0] if results else None
    return DerivativePlanResult(
        review_task_id=review_task_id,
        recipe_id=recipe_ids[0],
        release_version_id=revision.release_version_id,
        job_ids=tuple(job.id for job in planned_jobs),
        jobs_created=sum(result.jobs_created for result in results),
        total_jobs=len(planned_jobs),
        replayed=(first_result.replayed if first_result is not None else False),
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
    watermark_positions_by_asset_id: Mapping[UUID, WatermarkPosition] | None = None,
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
        watermark_positions_by_asset_id=watermark_positions_by_asset_id,
        max_attempts=max_attempts,
        now=planned_at + timedelta(microseconds=1),
        require_active_revision=False,
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


def _x_lossless_png_recipe(*, position: WatermarkPosition) -> DerivativeRecipe:
    """Build the frozen public X profile without a lossy or resizing fallback."""

    return DerivativeRecipe(
        x_teaser=XTeaserSpec(
            output_filename="x-teaser.png",
            width=MAX_PIPELINE_MASTER_WIDTH,
            height=MAX_PIPELINE_MASTER_HEIGHT,
            fit_mode=TeaserFitMode.DOWNSCALE,
            allow_upscale=False,
            encoding=PngEncoding(compress_level=6),
        ),
        watermark=WatermarkSpec(position=position),
    )


async def _x_selected_asset_ids(
    session: AsyncSession,
    *,
    review_task_id: UUID,
) -> tuple[UUID, ...]:
    return tuple(
        (
            await session.scalars(
                select(ReviewXSelection.asset_id)
                .where(ReviewXSelection.review_task_id == review_task_id)
                .order_by(ReviewXSelection.selected_at, ReviewXSelection.asset_id)
            )
        ).all()
    )


def _resolved_watermark_positions(
    *,
    selected_asset_ids: Sequence[UUID],
    default_position: WatermarkPosition,
    positions_by_asset_id: Mapping[UUID, WatermarkPosition] | None,
) -> dict[UUID, WatermarkPosition]:
    if not isinstance(default_position, WatermarkPosition):
        raise DerivativePipelineInputError("default watermark position is invalid")
    selected = frozenset(selected_asset_ids)
    overrides: dict[UUID, WatermarkPosition] = {}
    if positions_by_asset_id is not None:
        if not isinstance(positions_by_asset_id, Mapping):
            raise DerivativePipelineInputError("watermark placements must be a mapping")
        if len(positions_by_asset_id) > 4:
            raise DerivativePipelineInputError("at most four watermark placements are allowed")
        for asset_id, position in positions_by_asset_id.items():
            if not isinstance(asset_id, UUID) or not isinstance(position, WatermarkPosition):
                raise DerivativePipelineInputError("watermark placement is invalid")
            overrides[asset_id] = position
    if overrides and set(overrides) != selected:
        raise DerivativePipelineConflictError(
            "watermark placements must cover the frozen X selections exactly"
        )
    return {asset_id: overrides.get(asset_id, default_position) for asset_id in selected_asset_ids}


def _target_idempotency_key(idempotency_key: str, target: str) -> str:
    candidate = f"{idempotency_key}:{target}"
    if len(candidate) <= 200:
        return candidate
    return hashlib.sha256(candidate.encode("utf-8")).hexdigest()
