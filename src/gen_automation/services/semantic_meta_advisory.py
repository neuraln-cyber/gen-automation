"""Owner-scoped advisory inference from a promoted CPU meta-classifier.

This layer consumes only already-persisted VLM results.  It never calls the
semantic gateway, never changes the calibrated VLM verdict, and never participates
in enforcement.  A missing model yields no advice; invalid or cross-scope model
artifacts raise so callers can fail closed to the original review experience.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from gen_automation.domain.enums import SemanticAssessmentState
from gen_automation.services.semantic_anatomy import SemanticReviewAssessment
from gen_automation.services.semantic_learning import load_effective_semantic_meta_model
from gen_automation.services.semantic_learning_readiness import (
    SemanticPredictedIssue,
    semantic_meta_prediction_feature_values,
)
from gen_automation.services.semantic_meta_classifier import (
    SemanticMetaClassifierError,
    SemanticMetaTriage,
    predict_semantic_meta_probability_micros,
    semantic_meta_triage,
)


@dataclass(frozen=True, slots=True)
class SemanticMetaAdvisory:
    """One non-enforcing personalized interpretation of frozen VLM evidence."""

    probability_micros: int
    triage: SemanticMetaTriage
    model_artifact_sha256: str
    training_dataset_sha256: str

    @property
    def probability_percent(self) -> str:
        return f"{self.probability_micros / 10_000:.1f}%"

    @property
    def label(self) -> str:
        return {
            SemanticMetaTriage.KEEP: "likely keep",
            SemanticMetaTriage.REVIEW: "review manually",
            SemanticMetaTriage.REJECT: "likely reject",
        }[self.triage]


async def load_semantic_meta_advisories(
    session: AsyncSession,
    *,
    owner_user_id: UUID,
    profile_sha256: str,
    assessments: Mapping[UUID, SemanticReviewAssessment],
) -> dict[UUID, SemanticMetaAdvisory]:
    """Score completed assessments with the promoted model for this exact scope.

    The mapping remains empty until a champion is promoted.  Scope mismatches and
    malformed stored results are rejected instead of falling back to a model from
    another owner or semantic profile.
    """

    model = await load_effective_semantic_meta_model(
        session,
        owner_user_id=owner_user_id,
        profile_sha256=profile_sha256,
    )
    if model is None:
        return {}
    if model.owner_user_id != str(owner_user_id):
        raise SemanticMetaClassifierError("promoted meta-classifier belongs to another owner")
    if model.profile_sha256 != profile_sha256:
        raise SemanticMetaClassifierError("promoted meta-classifier uses another profile")

    result: dict[UUID, SemanticMetaAdvisory] = {}
    for asset_id, assessment in assessments.items():
        if assessment.asset_id != asset_id:
            raise SemanticMetaClassifierError("semantic assessment asset mapping is invalid")
        if assessment.state != SemanticAssessmentState.COMPLETED:
            continue
        if assessment.verdict is None or assessment.confidence_micros is None:
            raise SemanticMetaClassifierError("completed semantic assessment is incomplete")
        features = semantic_meta_prediction_feature_values(
            verdict=assessment.verdict,
            confidence_micros=assessment.confidence_micros,
            predicted_issues=tuple(
                SemanticPredictedIssue(
                    code=issue.code,
                    confidence_micros=issue.confidence_micros,
                    has_box=issue.box is not None,
                )
                for issue in assessment.issues
            ),
        )
        probability = predict_semantic_meta_probability_micros(model, features)
        result[asset_id] = SemanticMetaAdvisory(
            probability_micros=probability,
            triage=semantic_meta_triage(
                probability,
                keep_threshold_micros=model.keep_threshold_micros,
                reject_threshold_micros=model.reject_threshold_micros,
            ),
            model_artifact_sha256=model.artifact_sha256,
            training_dataset_sha256=model.training_dataset_sha256,
        )
    return result


__all__ = ("SemanticMetaAdvisory", "load_semantic_meta_advisories")
