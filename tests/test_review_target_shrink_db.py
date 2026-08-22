from datetime import timedelta

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from gen_automation.db.models import ReleaseSelection, ReviewTask
from gen_automation.domain.enums import ReviewDecisionValue, ReviewTaskState
from gen_automation.services.review import (
    _freeze_release_selections,
    append_review_decision,
)
from tests.test_review_service import (
    NOW,
    ReviewContext,
    _create_task,
    review_context,  # noqa: F401
)


@pytest.mark.asyncio
async def test_database_rejects_open_target_shrink(
    review_context: ReviewContext,  # noqa: F811
) -> None:
    task = await _create_task(review_context)

    async with review_context.database.sessions() as session:
        with pytest.raises(IntegrityError, match="open review target expansion is invalid"):
            await session.execute(
                update(ReviewTask)
                .where(ReviewTask.id == task.task_id)
                .values(desired_accepted_count=1, lock_version=2)
            )
        await session.rollback()


@pytest.mark.asyncio
async def test_database_allows_open_target_expansion_to_ranked_count(
    review_context: ReviewContext,  # noqa: F811
) -> None:
    task = await _create_task(review_context)

    async with review_context.database.sessions() as session:
        await session.execute(
            update(ReviewTask)
            .where(ReviewTask.id == task.task_id)
            .values(
                desired_accepted_count=len(review_context.ranked_asset_ids),
                lock_version=2,
            )
        )
        await session.commit()

    async with review_context.database.sessions() as session:
        stored = await session.get(ReviewTask, task.task_id)
        assert stored is not None
        assert stored.desired_accepted_count == len(review_context.ranked_asset_ids)
        assert stored.lock_version == 2


@pytest.mark.asyncio
async def test_database_completion_uses_atomic_shrunken_target(
    review_context: ReviewContext,  # noqa: F811
) -> None:
    task = await _create_task(review_context)
    async with review_context.database.sessions() as session:
        accepted = await append_review_decision(
            session,
            review_task_id=task.task_id,
            asset_id=review_context.ranked_asset_ids[0],
            decision=ReviewDecisionValue.ACCEPT,
            decided_by_user_id=review_context.reviewer_id,
            expected_lock_version=1,
            idempotency_key="target-shrink-accept",
            now=NOW + timedelta(minutes=3),
        )

    completed_at = NOW + timedelta(minutes=4)
    async with review_context.database.sessions() as session:
        stored = await session.get(ReviewTask, task.task_id)
        assert stored is not None
        assert stored.lock_version == accepted.task_lock_version == 2
        await _freeze_release_selections(
            session,
            task=stored,
            final_accepted_count=1,
            frozen_at=completed_at,
            actor_user_id=review_context.reviewer_id,
            correlation_id="atomic-target-shrink",
        )
        await session.execute(
            update(ReviewTask)
            .where(ReviewTask.id == task.task_id)
            .values(
                desired_accepted_count=1,
                state=ReviewTaskState.COMPLETED,
                lock_version=3,
                completed_by_user_id=review_context.reviewer_id,
                completed_at=completed_at,
            )
        )
        await session.commit()

    async with review_context.database.sessions() as session:
        stored = await session.get(ReviewTask, task.task_id)
        assert stored is not None
        assert stored.state == ReviewTaskState.COMPLETED
        assert stored.desired_accepted_count == 1
        assert (
            int(
                await session.scalar(
                    select(func.count())
                    .select_from(ReleaseSelection)
                    .where(ReleaseSelection.review_task_id == task.task_id)
                )
                or 0
            )
            == 1
        )
