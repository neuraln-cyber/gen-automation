"""Narrow route-level ownership check for anatomy feedback targets."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gen_automation.db.models import ReviewTask, SemanticAssessment
from gen_automation.domain.enums import ReviewTaskState


async def semantic_feedback_target_belongs_to_review(
    session: AsyncSession,
    *,
    review_task_id: UUID,
    assessment_id: UUID,
    profile_sha256: str,
) -> bool:
    """Return whether the assessment belongs to an already completed review snapshot."""

    found = await session.scalar(
        select(SemanticAssessment.id)
        .join(
            ReviewTask,
            ReviewTask.scoring_run_id == SemanticAssessment.scoring_run_id,
        )
        .where(
            ReviewTask.id == review_task_id,
            ReviewTask.state == ReviewTaskState.COMPLETED,
            SemanticAssessment.id == assessment_id,
            SemanticAssessment.profile_sha256 == profile_sha256,
        )
        .limit(1)
    )
    return found is not None
