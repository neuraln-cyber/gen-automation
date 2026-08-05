"""Low-friction anatomy labels derived from finalized owner review choices."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gen_automation.db.models import AdminUser, SemanticAnatomyFeedback, SemanticAssessment
from gen_automation.domain.enums import (
    AdminRole,
    ReviewDecisionValue,
    SemanticAssessmentState,
    SemanticGroundTruth,
    SemanticIssueCode,
)
from gen_automation.services.semantic_feedback import (
    SemanticCalibrationArtifactResult,
    SemanticFeedbackSource,
    record_semantic_anatomy_feedback,
    refresh_semantic_calibration_artifact,
)

GENERIC_ANATOMY_REASON_CODES = frozenset({"anatomy", "anatomy_defect", "bad_anatomy"})
ANATOMY_REASON_CODES = GENERIC_ANATOMY_REASON_CODES | frozenset(
    issue.value for issue in SemanticIssueCode
)


@dataclass(frozen=True, slots=True)
class SemanticReviewChoice:
    """The final, revision-resolved portion of a review choice used for learning."""

    asset_id: UUID
    decision: ReviewDecisionValue | None
    reason_code: str | None
    decided_by_user_id: UUID | None
    semantic_severe_override_attested: bool
    inspected: bool = False


@dataclass(frozen=True, slots=True)
class SemanticReviewLearningResult:
    """Summary of safely inferred labels and the resulting immutable policy snapshot."""

    inferred_good_count: int
    inferred_defect_count: int
    skipped_existing_count: int
    skipped_unsafe_count: int
    calibration: SemanticCalibrationArtifactResult | None

    @property
    def inferred_count(self) -> int:
        return self.inferred_good_count + self.inferred_defect_count


async def learn_semantic_anatomy_from_final_review(
    session: AsyncSession,
    *,
    scoring_run_id: UUID,
    profile_sha256: str,
    owner_user_id: UUID,
    choices: tuple[SemanticReviewChoice, ...],
    baseline_threshold_micros: int = 900_000,
    now: datetime | None = None,
) -> SemanticReviewLearningResult:
    """Infer only anatomy-safe labels from a completed owner's final decisions.

    A final owner acceptance is a useful positive example unless it is an explicit
    severe-anatomy override. A rejection is useful only when its reason explicitly
    says anatomy; ordinary exclusions may be about composition, style, duplication,
    or publishing preference and must not contaminate anatomy calibration.
    """

    owner = await session.get(AdminUser, owner_user_id)
    if owner is None or owner.role != AdminRole.OWNER or not owner.is_active:
        return _empty_result(skipped_unsafe_count=len(choices))

    eligible = tuple(choice for choice in choices if choice.decided_by_user_id == owner_user_id)
    if not eligible:
        return _empty_result(skipped_unsafe_count=len(choices))

    assessments = (
        await session.scalars(
            select(SemanticAssessment).where(
                SemanticAssessment.scoring_run_id == scoring_run_id,
                SemanticAssessment.profile_sha256 == profile_sha256,
                SemanticAssessment.state == SemanticAssessmentState.COMPLETED,
                SemanticAssessment.asset_id.in_(tuple(choice.asset_id for choice in eligible)),
            )
        )
    ).all()
    assessment_by_asset = {assessment.asset_id: assessment for assessment in assessments}
    assessment_ids = tuple(assessment.id for assessment in assessments)
    existing_ids: set[UUID] = set()
    if assessment_ids:
        existing_ids = set(
            await session.scalars(
                select(SemanticAnatomyFeedback.semantic_assessment_id).where(
                    SemanticAnatomyFeedback.semantic_assessment_id.in_(assessment_ids),
                    SemanticAnatomyFeedback.feedback_by_user_id == owner_user_id,
                )
            )
        )

    inferred_good = inferred_defect = skipped_existing = skipped_unsafe = 0
    learned_at = _as_utc(now or datetime.now(UTC))
    for choice in eligible:
        assessment = assessment_by_asset.get(choice.asset_id)
        if assessment is None:
            skipped_unsafe += 1
            continue
        if assessment.id in existing_ids:
            skipped_existing += 1
            continue

        label = _inferred_label(choice)
        if label is None:
            skipped_unsafe += 1
            continue
        ground_truth, issue_code, source = label
        result = await record_semantic_anatomy_feedback(
            session,
            assessment_id=assessment.id,
            user_id=owner_user_id,
            ground_truth=ground_truth,
            issue_code=issue_code,
            source=source,
            now=learned_at,
        )
        if not result.created:
            skipped_existing += 1
            continue
        existing_ids.add(assessment.id)
        if ground_truth == SemanticGroundTruth.ANATOMY_GOOD:
            inferred_good += 1
        else:
            inferred_defect += 1

    calibration = None
    if inferred_good or inferred_defect:
        calibration = await refresh_semantic_calibration_artifact(
            session,
            profile_sha256=profile_sha256,
            created_by_user_id=owner_user_id,
            configured_baseline_threshold_micros=baseline_threshold_micros,
            now=learned_at,
        )
    return SemanticReviewLearningResult(
        inferred_good_count=inferred_good,
        inferred_defect_count=inferred_defect,
        skipped_existing_count=skipped_existing,
        skipped_unsafe_count=skipped_unsafe + (len(choices) - len(eligible)),
        calibration=calibration,
    )


def _inferred_label(
    choice: SemanticReviewChoice,
) -> tuple[SemanticGroundTruth, SemanticIssueCode | None, SemanticFeedbackSource] | None:
    if choice.decision == ReviewDecisionValue.ACCEPT:
        if choice.semantic_severe_override_attested or not choice.inspected:
            return None
        return (
            SemanticGroundTruth.ANATOMY_GOOD,
            None,
            SemanticFeedbackSource.INFERRED_REVIEW_ACCEPT,
        )
    normalized_reason = (choice.reason_code or "").strip().lower()
    if (
        choice.decision != ReviewDecisionValue.REJECT
        or normalized_reason not in ANATOMY_REASON_CODES
    ):
        return None
    issue_code = None
    try:
        issue_code = SemanticIssueCode(normalized_reason)
    except ValueError:
        pass
    return (
        SemanticGroundTruth.ANATOMY_DEFECT,
        issue_code,
        SemanticFeedbackSource.INFERRED_ANATOMY_REJECT,
    )


def _empty_result(*, skipped_unsafe_count: int) -> SemanticReviewLearningResult:
    return SemanticReviewLearningResult(
        inferred_good_count=0,
        inferred_defect_count=0,
        skipped_existing_count=0,
        skipped_unsafe_count=skipped_unsafe_count,
        calibration=None,
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("semantic learning timestamps must be timezone-aware")
    return value.astimezone(UTC)
