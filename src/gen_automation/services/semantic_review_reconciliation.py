"""Idempotent learning reconciliation for anatomy results that finish after review."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from gen_automation.db.models import (
    AdminUser,
    ReviewAssetInspection,
    ReviewDecision,
    ReviewTask,
    SemanticAnatomyFeedback,
    SemanticAssessment,
)
from gen_automation.domain.enums import (
    AdminRole,
    ReviewDecisionValue,
    ReviewTaskState,
    SemanticAssessmentState,
)
from gen_automation.services.review import SEMANTIC_SEVERE_OVERRIDE_REASON_CODE
from gen_automation.services.semantic_review_learning import (
    ANATOMY_REASON_CODES,
    SemanticReviewChoice,
    SemanticReviewLearningResult,
    learn_semantic_anatomy_from_final_review,
)


@dataclass(frozen=True, slots=True)
class SemanticReviewReconciliationResult:
    review_task_id: UUID | None
    learning: SemanticReviewLearningResult | None

    @property
    def did_work(self) -> bool:
        return self.learning is not None and self.learning.inferred_count > 0


async def reconcile_one_completed_semantic_review(
    session: AsyncSession,
    *,
    profile_sha256: str,
    baseline_threshold_micros: int = 900_000,
    now: datetime | None = None,
) -> SemanticReviewReconciliationResult:
    """Learn safe final-owner choices for late completed anatomy assessments.

    Only reviews completed by an active owner and containing at least one safe,
    unlabeled final choice are selected. Generic rejects, holds, non-owner choices,
    and severe-override accepts are excluded before choosing work, so they cannot
    repeatedly starve useful reconciliation.
    """

    learned_at = _as_utc(now or datetime.now(UTC))
    latest_revisions = (
        select(
            ReviewDecision.review_task_id.label("review_task_id"),
            ReviewDecision.asset_id.label("asset_id"),
            func.max(ReviewDecision.revision).label("revision"),
        )
        .group_by(ReviewDecision.review_task_id, ReviewDecision.asset_id)
        .subquery()
    )
    normalized_reason = func.lower(func.trim(ReviewDecision.reason_code))
    owner_inspection_exists = exists(
        select(ReviewAssetInspection.id).where(
            ReviewAssetInspection.review_task_id == ReviewTask.id,
            ReviewAssetInspection.asset_id == ReviewDecision.asset_id,
            ReviewAssetInspection.inspected_by_user_id == ReviewTask.completed_by_user_id,
        )
    )
    safe_choice = or_(
        and_(
            ReviewDecision.decision == ReviewDecisionValue.ACCEPT,
            owner_inspection_exists,
            or_(
                ReviewDecision.reason_code.is_(None),
                normalized_reason != SEMANTIC_SEVERE_OVERRIDE_REASON_CODE,
            ),
        ),
        and_(
            ReviewDecision.decision == ReviewDecisionValue.REJECT,
            normalized_reason.in_(tuple(sorted(ANATOMY_REASON_CODES))),
        ),
    )
    owner_feedback_exists = exists(
        select(SemanticAnatomyFeedback.id).where(
            SemanticAnatomyFeedback.semantic_assessment_id == SemanticAssessment.id,
            SemanticAnatomyFeedback.feedback_by_user_id == ReviewTask.completed_by_user_id,
        )
    )
    task_id = await session.scalar(
        select(ReviewTask.id)
        .join(AdminUser, AdminUser.id == ReviewTask.completed_by_user_id)
        .join(
            SemanticAssessment,
            and_(
                SemanticAssessment.scoring_run_id == ReviewTask.scoring_run_id,
                SemanticAssessment.profile_sha256 == profile_sha256,
                SemanticAssessment.state == SemanticAssessmentState.COMPLETED,
            ),
        )
        .join(
            latest_revisions,
            and_(
                latest_revisions.c.review_task_id == ReviewTask.id,
                latest_revisions.c.asset_id == SemanticAssessment.asset_id,
            ),
        )
        .join(
            ReviewDecision,
            and_(
                ReviewDecision.review_task_id == latest_revisions.c.review_task_id,
                ReviewDecision.asset_id == latest_revisions.c.asset_id,
                ReviewDecision.revision == latest_revisions.c.revision,
            ),
        )
        .where(
            ReviewTask.state == ReviewTaskState.COMPLETED,
            ReviewTask.completed_by_user_id.is_not(None),
            AdminUser.role == AdminRole.OWNER,
            AdminUser.is_active.is_(True),
            ReviewDecision.decided_by_user_id == ReviewTask.completed_by_user_id,
            safe_choice,
            ~owner_feedback_exists,
        )
        .order_by(ReviewTask.completed_at, ReviewTask.id)
        .limit(1)
        .with_for_update(skip_locked=True, of=ReviewTask)
    )
    if task_id is None:
        return SemanticReviewReconciliationResult(review_task_id=None, learning=None)

    task = await session.get(ReviewTask, task_id)
    if task is None or task.completed_by_user_id is None:
        return SemanticReviewReconciliationResult(review_task_id=None, learning=None)
    decisions = (
        await session.scalars(
            select(ReviewDecision)
            .join(
                latest_revisions,
                and_(
                    latest_revisions.c.review_task_id == ReviewDecision.review_task_id,
                    latest_revisions.c.asset_id == ReviewDecision.asset_id,
                    latest_revisions.c.revision == ReviewDecision.revision,
                ),
            )
            .where(ReviewDecision.review_task_id == task.id)
            .order_by(ReviewDecision.asset_id)
        )
    ).all()
    inspected_asset_ids = set(
        await session.scalars(
            select(ReviewAssetInspection.asset_id).where(
                ReviewAssetInspection.review_task_id == task.id,
                ReviewAssetInspection.inspected_by_user_id == task.completed_by_user_id,
            )
        )
    )
    choices = tuple(
        SemanticReviewChoice(
            asset_id=decision.asset_id,
            decision=decision.decision,
            reason_code=decision.reason_code,
            decided_by_user_id=decision.decided_by_user_id,
            semantic_severe_override_attested=(
                (decision.reason_code or "").strip().lower() == SEMANTIC_SEVERE_OVERRIDE_REASON_CODE
            ),
            inspected=decision.asset_id in inspected_asset_ids,
        )
        for decision in decisions
    )
    learning = await learn_semantic_anatomy_from_final_review(
        session,
        scoring_run_id=task.scoring_run_id,
        profile_sha256=profile_sha256,
        owner_user_id=task.completed_by_user_id,
        choices=choices,
        baseline_threshold_micros=baseline_threshold_micros,
        now=learned_at,
    )
    return SemanticReviewReconciliationResult(review_task_id=task.id, learning=learning)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("semantic reconciliation timestamps must be timezone-aware")
    return value.astimezone(UTC)
