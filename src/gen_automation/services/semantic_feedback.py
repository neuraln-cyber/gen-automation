"""Authoritative anatomy feedback and deterministic threshold calibration."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from gen_automation.db.models import (
    SemanticAnatomyFeedback,
    SemanticAssessment,
    SemanticCalibrationArtifact,
)
from gen_automation.domain.canonical import canonical_sha256
from gen_automation.domain.enums import (
    SemanticAssessmentState,
    SemanticFeedbackAgreement,
    SemanticGroundTruth,
    SemanticIssueCode,
    SemanticVerdict,
)

SEMANTIC_CALIBRATION_SCHEMA_VERSION = "semantic-anatomy-calibration/v3"
LEGACY_SEMANTIC_CALIBRATION_SCHEMA_VERSION = "semantic-anatomy-calibration/v2"
DEFAULT_CALIBRATION_MINIMUM_SAMPLES = 100
DEFAULT_CALIBRATION_MINIMUM_PER_CLASS = 20
DEFAULT_CALIBRATION_THRESHOLD_STEP_MICROS = 50_000
DEFAULT_CONFIGURED_BASELINE_THRESHOLD_MICROS = 900_000
SEMANTIC_CALIBRATION_FOLD_COUNT = 5

# These values are reserved for system-derived feedback.  Human-entered notes
# never need to use them; keeping the source marker in the immutable note lets
# v3 reports distinguish explicit owner labels from frictionless review signals
# without a schema migration.
SEMANTIC_INFERRED_REVIEW_ACCEPT_NOTE = "system:inferred-review-accept"
SEMANTIC_INFERRED_ANATOMY_REJECT_NOTE = "system:inferred-anatomy-reject"
_INFERRED_NOTES = frozenset(
    {
        SEMANTIC_INFERRED_REVIEW_ACCEPT_NOTE,
        SEMANTIC_INFERRED_ANATOMY_REJECT_NOTE,
    }
)
_LEARNING_STATUSES = frozenset({"collecting", "calibrating", "improved", "stable", "regressed"})


class SemanticFeedbackSource(StrEnum):
    """Trusted source used to derive the immutable feedback source marker."""

    EXPLICIT = "explicit"
    INFERRED_REVIEW_ACCEPT = "inferred_review_accept"
    INFERRED_ANATOMY_REJECT = "inferred_anatomy_reject"


_INFERRED_NOTE_BY_SOURCE = {
    SemanticFeedbackSource.INFERRED_REVIEW_ACCEPT: SEMANTIC_INFERRED_REVIEW_ACCEPT_NOTE,
    SemanticFeedbackSource.INFERRED_ANATOMY_REJECT: SEMANTIC_INFERRED_ANATOMY_REJECT_NOTE,
}


class SemanticFeedbackError(Exception):
    """Base error for anatomy feedback and calibration."""


class SemanticFeedbackNotFoundError(SemanticFeedbackError):
    """The assessment being labelled does not exist."""


class SemanticFeedbackAssessmentNotReadyError(SemanticFeedbackError):
    """Only a completed, immutable assessment can receive feedback."""


class SemanticFeedbackConflictError(SemanticFeedbackError):
    """The owner already recorded a different immutable label."""


class SemanticFeedbackValidationError(SemanticFeedbackError):
    """The requested feedback is internally inconsistent."""


@dataclass(frozen=True, slots=True)
class SemanticAnatomyFeedbackResult:
    feedback_id: UUID
    assessment_id: UUID
    asset_id: UUID
    user_id: UUID
    agreement: SemanticFeedbackAgreement
    ground_truth: SemanticGroundTruth
    issue_code: SemanticIssueCode | None
    note: str | None
    created_at: datetime
    created: bool


@dataclass(frozen=True, slots=True)
class SemanticConfusionCounts:
    threshold_micros: int
    true_positive: int
    false_positive: int
    true_negative: int
    false_negative: int

    @property
    def precision_micros(self) -> int | None:
        return _ratio_micros(self.true_positive, self.true_positive + self.false_positive)

    @property
    def recall_micros(self) -> int | None:
        return _ratio_micros(self.true_positive, self.true_positive + self.false_negative)

    @property
    def specificity_micros(self) -> int | None:
        return _ratio_micros(self.true_negative, self.true_negative + self.false_positive)

    @property
    def false_positive_rate_micros(self) -> int | None:
        return _ratio_micros(self.false_positive, self.false_positive + self.true_negative)

    @property
    def f1_micros(self) -> int | None:
        return _ratio_micros(
            2 * self.true_positive,
            (2 * self.true_positive) + self.false_positive + self.false_negative,
        )

    @property
    def balanced_accuracy_micros(self) -> int | None:
        recall = self.recall_micros
        specificity = self.specificity_micros
        if recall is None or specificity is None:
            return None
        return round((recall + specificity) / 2)

    def to_wire(self) -> dict[str, int | None]:
        return {
            "threshold_micros": self.threshold_micros,
            "true_positive": self.true_positive,
            "false_positive": self.false_positive,
            "true_negative": self.true_negative,
            "false_negative": self.false_negative,
            "precision_micros": self.precision_micros,
            "recall_micros": self.recall_micros,
            "specificity_micros": self.specificity_micros,
            "false_positive_rate_micros": self.false_positive_rate_micros,
            "f1_micros": self.f1_micros,
            "balanced_accuracy_micros": self.balanced_accuracy_micros,
        }


@dataclass(frozen=True, slots=True)
class SemanticValidationMetrics:
    sample_count: int
    true_positive: int
    false_positive: int
    true_negative: int
    false_negative: int

    @property
    def precision_micros(self) -> int | None:
        return _ratio_micros(self.true_positive, self.true_positive + self.false_positive)

    @property
    def recall_micros(self) -> int | None:
        return _ratio_micros(self.true_positive, self.true_positive + self.false_negative)

    @property
    def specificity_micros(self) -> int | None:
        return _ratio_micros(self.true_negative, self.true_negative + self.false_positive)

    @property
    def false_positive_rate_micros(self) -> int | None:
        return _ratio_micros(self.false_positive, self.false_positive + self.true_negative)

    @property
    def f1_micros(self) -> int | None:
        return _ratio_micros(
            2 * self.true_positive,
            (2 * self.true_positive) + self.false_positive + self.false_negative,
        )

    @property
    def balanced_accuracy_micros(self) -> int | None:
        recall = self.recall_micros
        specificity = self.specificity_micros
        if recall is None or specificity is None:
            return None
        return round((recall + specificity) / 2)

    def to_wire(self) -> dict[str, int | None]:
        return {
            "sample_count": self.sample_count,
            "true_positive": self.true_positive,
            "false_positive": self.false_positive,
            "true_negative": self.true_negative,
            "false_negative": self.false_negative,
            "precision_micros": self.precision_micros,
            "recall_micros": self.recall_micros,
            "specificity_micros": self.specificity_micros,
            "false_positive_rate_micros": self.false_positive_rate_micros,
            "f1_micros": self.f1_micros,
            "balanced_accuracy_micros": self.balanced_accuracy_micros,
        }


@dataclass(frozen=True, slots=True)
class SemanticCalibrationReport:
    profile_sha256: str
    dataset_sha256: str
    feedback_count: int
    labeled_sample_count: int
    anatomy_good_count: int
    anatomy_defect_count: int
    unjudgeable_count: int
    agreement_correct_count: int
    agreement_incorrect_count: int
    agreement_unsure_count: int
    minimum_samples: int
    minimum_per_class: int
    threshold_step_micros: int
    threshold_sweep: tuple[SemanticConfusionCounts, ...]
    recommended_threshold_micros: int | None
    ready_for_enforcement: bool
    explicit_label_count: int
    inferred_label_count: int
    inferred_accept_count: int
    inferred_anatomy_reject_count: int
    configured_baseline_threshold_micros: int
    previous_policy_threshold_micros: int
    previous_artifact_version: int | None
    previous_active_policy_version: int | None
    candidate_threshold_micros: int | None
    effective_threshold_micros: int
    active_policy_changed: bool
    learning_status: str
    validation_fold_thresholds_micros: tuple[int | None, ...]
    configured_baseline_validation: SemanticValidationMetrics | None
    current_candidate_validation: SemanticValidationMetrics | None
    previous_policy_validation: SemanticValidationMetrics | None

    def to_wire(self) -> dict[str, Any]:
        return {
            "schema_version": SEMANTIC_CALIBRATION_SCHEMA_VERSION,
            "profile_sha256": self.profile_sha256,
            "dataset_sha256": self.dataset_sha256,
            "feedback_count": self.feedback_count,
            "labeled_sample_count": self.labeled_sample_count,
            "ground_truth_counts": {
                "anatomy_good": self.anatomy_good_count,
                "anatomy_defect": self.anatomy_defect_count,
                "unjudgeable": self.unjudgeable_count,
            },
            "agreement_counts": {
                "correct": self.agreement_correct_count,
                "incorrect": self.agreement_incorrect_count,
                "unsure": self.agreement_unsure_count,
            },
            "minimum_samples": self.minimum_samples,
            "minimum_per_class": self.minimum_per_class,
            "threshold_step_micros": self.threshold_step_micros,
            "threshold_sweep": [item.to_wire() for item in self.threshold_sweep],
            "recommended_threshold_micros": self.recommended_threshold_micros,
            "ready_for_enforcement": self.ready_for_enforcement,
            "source_counts": {
                "explicit": self.explicit_label_count,
                "inferred": self.inferred_label_count,
                "inferred_review_accept": self.inferred_accept_count,
                "inferred_anatomy_reject": self.inferred_anatomy_reject_count,
            },
            "configured_baseline_threshold_micros": (self.configured_baseline_threshold_micros),
            "previous_policy_threshold_micros": self.previous_policy_threshold_micros,
            "previous_artifact_version": self.previous_artifact_version,
            "previous_active_policy_version": self.previous_active_policy_version,
            "candidate_threshold_micros": self.candidate_threshold_micros,
            "effective_threshold_micros": self.effective_threshold_micros,
            "active_policy_changed": self.active_policy_changed,
            "learning_status": self.learning_status,
            "validation": {
                "fold_count": SEMANTIC_CALIBRATION_FOLD_COUNT,
                "group_key": "asset_sha256",
                "fold_thresholds_micros": list(self.validation_fold_thresholds_micros),
                "configured_baseline": (
                    self.configured_baseline_validation.to_wire()
                    if self.configured_baseline_validation is not None
                    else None
                ),
                "current_candidate": (
                    self.current_candidate_validation.to_wire()
                    if self.current_candidate_validation is not None
                    else None
                ),
                "previous_policy": (
                    self.previous_policy_validation.to_wire()
                    if self.previous_policy_validation is not None
                    else None
                ),
            },
        }


@dataclass(frozen=True, slots=True)
class SemanticCalibrationArtifactResult:
    artifact_id: UUID
    profile_sha256: str
    version: int
    dataset_sha256: str
    report_sha256: str
    sample_count: int
    recommended_threshold_micros: int | None
    ready_for_enforcement: bool
    created_at: datetime
    created: bool
    calibration_schema_version: str
    learning_status: str
    minimum_samples: int
    minimum_per_class: int
    anatomy_good_count: int
    anatomy_defect_count: int
    unjudgeable_count: int
    explicit_label_count: int
    inferred_label_count: int
    validation_sample_count: int
    validation_f1_micros: int | None
    previous_validation_f1_micros: int | None
    validation_f1_delta_micros: int | None
    validation_recall_micros: int | None
    previous_validation_recall_micros: int | None
    validation_false_positive_rate_micros: int | None
    previous_validation_false_positive_rate_micros: int | None
    configured_baseline_validation_f1_micros: int | None
    effective_threshold_micros: int | None
    candidate_threshold_micros: int | None
    configured_baseline_threshold_micros: int | None
    active_policy_changed: bool
    previous_artifact_version: int | None
    active_policy_version: int | None


@dataclass(frozen=True, slots=True)
class _CalibrationSample:
    feedback_id: UUID
    assessment_id: UUID
    asset_id: UUID
    asset_sha256: str
    agreement: SemanticFeedbackAgreement
    ground_truth: SemanticGroundTruth
    issue_code: SemanticIssueCode | None
    verdict: SemanticVerdict
    confidence_micros: int
    response_sha256: str
    source: str

    def identity_wire(self) -> dict[str, str | int | None]:
        return {
            "feedback_id": str(self.feedback_id),
            "assessment_id": str(self.assessment_id),
            "asset_id": str(self.asset_id),
            "asset_sha256": self.asset_sha256,
            "agreement": self.agreement.value,
            "ground_truth": self.ground_truth.value,
            "issue_code": self.issue_code.value if self.issue_code is not None else None,
            "verdict": self.verdict.value,
            "confidence_micros": self.confidence_micros,
            "response_sha256": self.response_sha256,
            "source": self.source,
        }


def agreement_for_ground_truth(
    verdict: SemanticVerdict,
    ground_truth: SemanticGroundTruth,
) -> SemanticFeedbackAgreement:
    """Derive binary assessment agreement from an authoritative owner label."""

    normalized_verdict = SemanticVerdict(verdict)
    normalized_truth = SemanticGroundTruth(ground_truth)
    if normalized_truth == SemanticGroundTruth.UNJUDGEABLE:
        return SemanticFeedbackAgreement.UNSURE
    predicts_defect = normalized_verdict in (
        SemanticVerdict.REVIEW,
        SemanticVerdict.SEVERE,
    )
    is_defect = normalized_truth == SemanticGroundTruth.ANATOMY_DEFECT
    if predicts_defect == is_defect:
        return SemanticFeedbackAgreement.CORRECT
    return SemanticFeedbackAgreement.INCORRECT


async def record_semantic_anatomy_feedback(
    session: AsyncSession,
    *,
    assessment_id: UUID,
    user_id: UUID,
    ground_truth: SemanticGroundTruth,
    agreement: SemanticFeedbackAgreement | None = None,
    issue_code: SemanticIssueCode | None = None,
    note: str | None = None,
    source: SemanticFeedbackSource = SemanticFeedbackSource.EXPLICIT,
    now: datetime | None = None,
) -> SemanticAnatomyFeedbackResult:
    """Record one immutable, idempotent owner label for an exact assessment."""

    normalized_truth = _ground_truth(ground_truth)
    normalized_issue = _issue_code(issue_code)
    normalized_source = _normalize_feedback_source(source)
    normalized_note = _feedback_note(note, source=normalized_source)
    assessment = await session.get(SemanticAssessment, assessment_id)
    if assessment is None:
        raise SemanticFeedbackNotFoundError("semantic assessment does not exist")
    if (
        assessment.state != SemanticAssessmentState.COMPLETED
        or assessment.verdict is None
        or assessment.confidence_micros is None
        or assessment.response_sha256 is None
    ):
        raise SemanticFeedbackAssessmentNotReadyError("semantic assessment is not completed")
    derived_agreement = agreement_for_ground_truth(assessment.verdict, normalized_truth)
    if agreement is not None and _agreement(agreement) != derived_agreement:
        raise SemanticFeedbackValidationError(
            "feedback agreement does not match the assessment and ground truth"
        )
    if normalized_truth != SemanticGroundTruth.ANATOMY_DEFECT and normalized_issue is not None:
        raise SemanticFeedbackValidationError(
            "an issue code may only accompany an anatomy defect label"
        )

    existing = await _feedback_for_owner(
        session,
        assessment_id=assessment.id,
        user_id=user_id,
    )
    if existing is not None:
        return _idempotent_feedback_result(
            existing,
            agreement=derived_agreement,
            ground_truth=normalized_truth,
            issue_code=normalized_issue,
            note=normalized_note,
        )

    feedback = SemanticAnatomyFeedback(
        semantic_assessment_id=assessment.id,
        asset_id=assessment.asset_id,
        profile_sha256=assessment.profile_sha256,
        feedback_by_user_id=user_id,
        agreement=derived_agreement,
        ground_truth=normalized_truth,
        issue_code=normalized_issue,
        note=normalized_note,
        created_at=_as_utc(now or datetime.now(UTC)),
    )
    try:
        async with session.begin_nested():
            session.add(feedback)
            await session.flush()
    except IntegrityError:
        existing = await _feedback_for_owner(
            session,
            assessment_id=assessment.id,
            user_id=user_id,
        )
        if existing is None:
            raise
        return _idempotent_feedback_result(
            existing,
            agreement=derived_agreement,
            ground_truth=normalized_truth,
            issue_code=normalized_issue,
            note=normalized_note,
        )
    return _feedback_result(feedback, created=True)


async def load_semantic_anatomy_feedback(
    session: AsyncSession,
    *,
    assessment_ids: tuple[UUID, ...],
    user_id: UUID,
) -> dict[UUID, SemanticAnatomyFeedbackResult]:
    """Load the current owner's immutable labels keyed by assessment id."""

    if not assessment_ids:
        return {}
    rows = (
        await session.scalars(
            select(SemanticAnatomyFeedback).where(
                SemanticAnatomyFeedback.semantic_assessment_id.in_(assessment_ids),
                SemanticAnatomyFeedback.feedback_by_user_id == user_id,
            )
        )
    ).all()
    return {row.semantic_assessment_id: _feedback_result(row, created=False) for row in rows}


async def build_semantic_calibration_report(
    session: AsyncSession,
    *,
    profile_sha256: str,
    minimum_samples: int = DEFAULT_CALIBRATION_MINIMUM_SAMPLES,
    minimum_per_class: int = DEFAULT_CALIBRATION_MINIMUM_PER_CLASS,
    threshold_step_micros: int = DEFAULT_CALIBRATION_THRESHOLD_STEP_MICROS,
    configured_baseline_threshold_micros: int = (DEFAULT_CONFIGURED_BASELINE_THRESHOLD_MICROS),
) -> SemanticCalibrationReport:
    """Build deterministic grouped out-of-fold calibration from owner labels."""

    profile_digest = _sha256(profile_sha256, label="semantic profile")
    _positive_int(minimum_samples, label="minimum samples", maximum=1_000_000)
    _positive_int(minimum_per_class, label="minimum per class", maximum=1_000_000)
    _positive_int(
        threshold_step_micros,
        label="threshold step",
        maximum=1_000_000,
    )
    configured_baseline = _confidence_threshold(
        configured_baseline_threshold_micros,
        label="configured baseline threshold",
    )
    previous_artifact = await _latest_artifact_record(
        session,
        profile_sha256=profile_digest,
    )
    previous_result = (
        _artifact_result(previous_artifact, created=False)
        if previous_artifact is not None
        else None
    )
    previous_policy_threshold = (
        previous_result.effective_threshold_micros
        if previous_result is not None and previous_result.effective_threshold_micros is not None
        else configured_baseline
    )
    previous_artifact_version = previous_result.version if previous_result is not None else None
    previous_active_policy_version = (
        previous_result.active_policy_version if previous_result is not None else None
    )
    rows = (
        await session.execute(
            select(SemanticAnatomyFeedback, SemanticAssessment)
            .join(
                SemanticAssessment,
                SemanticAssessment.id == SemanticAnatomyFeedback.semantic_assessment_id,
            )
            .where(
                SemanticAnatomyFeedback.profile_sha256 == profile_digest,
                SemanticAssessment.profile_sha256 == profile_digest,
                SemanticAssessment.state == SemanticAssessmentState.COMPLETED,
            )
            .order_by(
                SemanticAnatomyFeedback.created_at,
                SemanticAnatomyFeedback.id,
            )
        )
    ).all()
    ordered_rows = tuple((feedback, assessment) for feedback, assessment in rows)
    samples = tuple(
        _calibration_sample(feedback, assessment)
        for feedback, assessment in _deduplicate_calibration_rows(ordered_rows)
    )
    dataset_digest = canonical_sha256(
        {
            "schema_version": SEMANTIC_CALIBRATION_SCHEMA_VERSION,
            "profile_sha256": profile_digest,
            "samples": [sample.identity_wire() for sample in samples],
        }
    )
    good_count = sum(sample.ground_truth == SemanticGroundTruth.ANATOMY_GOOD for sample in samples)
    defect_count = sum(
        sample.ground_truth == SemanticGroundTruth.ANATOMY_DEFECT for sample in samples
    )
    unjudgeable_count = len(samples) - good_count - defect_count
    labeled = tuple(
        sample for sample in samples if sample.ground_truth != SemanticGroundTruth.UNJUDGEABLE
    )
    explicit_samples = tuple(sample for sample in samples if sample.source == "explicit")
    thresholds = list(range(0, 1_000_001, threshold_step_micros))
    if thresholds[-1] != 1_000_000:
        thresholds.append(1_000_000)
    sweep = tuple(_confusion_counts(labeled, threshold) for threshold in thresholds)
    recommended = _recommended_threshold(
        sweep,
        has_good=good_count > 0,
        has_defect=defect_count > 0,
    )
    (
        configured_validation,
        candidate_validation,
        previous_validation,
        fold_thresholds,
    ) = _out_of_fold_validation(
        labeled,
        validation_labeled=labeled,
        configured_baseline_threshold_micros=configured_baseline,
        previous_policy_threshold_micros=previous_policy_threshold,
        threshold_step_micros=threshold_step_micros,
    )
    enough_labels = (
        len(labeled) >= minimum_samples
        and good_count >= minimum_per_class
        and defect_count >= minimum_per_class
    )
    validation_complete = (
        candidate_validation is not None and candidate_validation.sample_count == len(labeled)
    )
    ready = enough_labels and validation_complete and recommended is not None
    learning_status, effective_threshold, active_policy_changed = _learning_outcome(
        enough_labels=enough_labels,
        validation_complete=validation_complete,
        has_previous_active_policy=previous_active_policy_version is not None,
        candidate_threshold_micros=recommended,
        previous_policy_threshold_micros=previous_policy_threshold,
        current_candidate_validation=candidate_validation,
        previous_policy_validation=previous_validation,
    )
    inferred_accept_count = sum(sample.source == "inferred_review_accept" for sample in samples)
    inferred_anatomy_reject_count = sum(
        sample.source == "inferred_anatomy_reject" for sample in samples
    )
    inferred_count = inferred_accept_count + inferred_anatomy_reject_count
    return SemanticCalibrationReport(
        profile_sha256=profile_digest,
        dataset_sha256=dataset_digest,
        feedback_count=len(samples),
        labeled_sample_count=len(labeled),
        anatomy_good_count=good_count,
        anatomy_defect_count=defect_count,
        unjudgeable_count=unjudgeable_count,
        agreement_correct_count=sum(
            sample.agreement == SemanticFeedbackAgreement.CORRECT for sample in samples
        ),
        agreement_incorrect_count=sum(
            sample.agreement == SemanticFeedbackAgreement.INCORRECT for sample in samples
        ),
        agreement_unsure_count=sum(
            sample.agreement == SemanticFeedbackAgreement.UNSURE for sample in samples
        ),
        minimum_samples=minimum_samples,
        minimum_per_class=minimum_per_class,
        threshold_step_micros=threshold_step_micros,
        threshold_sweep=sweep,
        recommended_threshold_micros=recommended,
        ready_for_enforcement=ready,
        explicit_label_count=len(explicit_samples),
        inferred_label_count=inferred_count,
        inferred_accept_count=inferred_accept_count,
        inferred_anatomy_reject_count=inferred_anatomy_reject_count,
        configured_baseline_threshold_micros=configured_baseline,
        previous_policy_threshold_micros=previous_policy_threshold,
        previous_artifact_version=previous_artifact_version,
        previous_active_policy_version=previous_active_policy_version,
        candidate_threshold_micros=recommended,
        effective_threshold_micros=effective_threshold,
        active_policy_changed=active_policy_changed,
        learning_status=learning_status,
        validation_fold_thresholds_micros=fold_thresholds,
        configured_baseline_validation=configured_validation,
        current_candidate_validation=candidate_validation,
        previous_policy_validation=previous_validation,
    )


async def persist_semantic_calibration_artifact(
    session: AsyncSession,
    *,
    report: SemanticCalibrationReport,
    created_by_user_id: UUID,
    now: datetime | None = None,
) -> SemanticCalibrationArtifactResult:
    """Persist a versioned immutable report, idempotent for the same dataset."""

    report_wire = report.to_wire()
    if report_wire["dataset_sha256"] != report.dataset_sha256:
        raise SemanticFeedbackValidationError("calibration report identity is invalid")
    report_digest = canonical_sha256(report_wire)
    existing = await session.scalar(
        select(SemanticCalibrationArtifact).where(
            SemanticCalibrationArtifact.profile_sha256 == report.profile_sha256,
            SemanticCalibrationArtifact.report_sha256 == report_digest,
        )
    )
    if existing is not None:
        return _artifact_result(existing, created=False)
    current_version = await session.scalar(
        select(func.max(SemanticCalibrationArtifact.version)).where(
            SemanticCalibrationArtifact.profile_sha256 == report.profile_sha256
        )
    )
    artifact = SemanticCalibrationArtifact(
        profile_sha256=report.profile_sha256,
        version=(current_version or 0) + 1,
        calibration_schema_version=SEMANTIC_CALIBRATION_SCHEMA_VERSION,
        dataset_sha256=report.dataset_sha256,
        sample_count=report.labeled_sample_count,
        recommended_threshold_micros=report.recommended_threshold_micros,
        ready_for_enforcement=report.ready_for_enforcement,
        report=report_wire,
        report_sha256=report_digest,
        created_by_user_id=created_by_user_id,
        created_at=_as_utc(now or datetime.now(UTC)),
    )
    try:
        async with session.begin_nested():
            session.add(artifact)
            await session.flush()
    except IntegrityError:
        existing = await session.scalar(
            select(SemanticCalibrationArtifact).where(
                SemanticCalibrationArtifact.profile_sha256 == report.profile_sha256,
                SemanticCalibrationArtifact.report_sha256 == report_digest,
            )
        )
        if existing is None:
            raise
        return _artifact_result(existing, created=False)
    return _artifact_result(artifact, created=True)


async def load_latest_semantic_calibration_artifact(
    session: AsyncSession,
    *,
    profile_sha256: str,
) -> SemanticCalibrationArtifactResult | None:
    profile_digest = _sha256(profile_sha256, label="semantic profile")
    artifact = await _latest_artifact_record(
        session,
        profile_sha256=profile_digest,
    )
    return _artifact_result(artifact, created=False) if artifact is not None else None


async def load_effective_semantic_threshold_micros(
    session: AsyncSession,
    *,
    profile_sha256: str,
    configured_fallback_micros: int = DEFAULT_CONFIGURED_BASELINE_THRESHOLD_MICROS,
) -> int:
    """Return the non-regressing learned threshold, or the configured fallback."""

    fallback = _confidence_threshold(
        configured_fallback_micros,
        label="configured fallback threshold",
    )
    artifact = await load_latest_semantic_calibration_artifact(
        session,
        profile_sha256=profile_sha256,
    )
    if (
        artifact is None
        or artifact.active_policy_version is None
        or artifact.effective_threshold_micros is None
    ):
        return fallback
    return artifact.effective_threshold_micros


async def refresh_semantic_calibration_artifact(
    session: AsyncSession,
    *,
    profile_sha256: str,
    created_by_user_id: UUID,
    now: datetime | None = None,
    configured_baseline_threshold_micros: int = (DEFAULT_CONFIGURED_BASELINE_THRESHOLD_MICROS),
) -> SemanticCalibrationArtifactResult:
    """Rebuild and persist the exact calibration snapshot after owner feedback."""

    report = await build_semantic_calibration_report(
        session,
        profile_sha256=profile_sha256,
        configured_baseline_threshold_micros=configured_baseline_threshold_micros,
    )
    return await persist_semantic_calibration_artifact(
        session,
        report=report,
        created_by_user_id=created_by_user_id,
        now=now,
    )


def _deduplicate_calibration_rows(
    rows: tuple[tuple[SemanticAnatomyFeedback, SemanticAssessment], ...],
) -> tuple[tuple[SemanticAnatomyFeedback, SemanticAssessment], ...]:
    """Keep the earliest profile-scoped label for each asset and owner."""

    deduplicated: list[tuple[SemanticAnatomyFeedback, SemanticAssessment]] = []
    seen: set[tuple[UUID, UUID]] = set()
    for feedback, assessment in rows:
        identity = (feedback.asset_id, feedback.feedback_by_user_id)
        if identity in seen:
            continue
        seen.add(identity)
        deduplicated.append((feedback, assessment))
    return tuple(deduplicated)


def _calibration_sample(
    feedback: SemanticAnatomyFeedback,
    assessment: SemanticAssessment,
) -> _CalibrationSample:
    if (
        assessment.verdict is None
        or assessment.confidence_micros is None
        or assessment.response_sha256 is None
    ):
        raise SemanticFeedbackAssessmentNotReadyError(
            "calibration contains an incomplete assessment"
        )
    return _CalibrationSample(
        feedback_id=feedback.id,
        assessment_id=assessment.id,
        asset_id=assessment.asset_id,
        asset_sha256=_sha256(assessment.asset_sha256, label="asset"),
        agreement=feedback.agreement,
        ground_truth=feedback.ground_truth,
        issue_code=feedback.issue_code,
        verdict=assessment.verdict,
        confidence_micros=assessment.confidence_micros,
        response_sha256=assessment.response_sha256,
        source=_feedback_source(feedback.note),
    )


def _feedback_source(note: str | None) -> str:
    if note not in _INFERRED_NOTES:
        return "explicit"
    if note == SEMANTIC_INFERRED_REVIEW_ACCEPT_NOTE:
        return "inferred_review_accept"
    return "inferred_anatomy_reject"


def _calibration_fold(asset_sha256: str) -> int:
    """Assign duplicate content to one stable validation fold."""

    digest = _sha256(asset_sha256, label="asset")
    grouped_digest = hashlib.sha256(f"semantic-calibration-fold-v1:{digest}".encode()).digest()
    return int.from_bytes(grouped_digest[:8], "big") % SEMANTIC_CALIBRATION_FOLD_COUNT


def _out_of_fold_validation(
    labeled: tuple[_CalibrationSample, ...],
    *,
    validation_labeled: tuple[_CalibrationSample, ...],
    configured_baseline_threshold_micros: int,
    previous_policy_threshold_micros: int,
    threshold_step_micros: int,
) -> tuple[
    SemanticValidationMetrics | None,
    SemanticValidationMetrics | None,
    SemanticValidationMetrics | None,
    tuple[int | None, ...],
]:
    configured_predictions: list[tuple[bool, bool]] = []
    candidate_predictions: list[tuple[bool, bool]] = []
    previous_predictions: list[tuple[bool, bool]] = []
    fold_thresholds: list[int | None] = [None] * SEMANTIC_CALIBRATION_FOLD_COUNT

    for fold in range(SEMANTIC_CALIBRATION_FOLD_COUNT):
        training = tuple(
            sample for sample in labeled if _calibration_fold(sample.asset_sha256) != fold
        )
        validation = tuple(
            sample
            for sample in validation_labeled
            if _calibration_fold(sample.asset_sha256) == fold
        )
        if not validation:
            continue
        training_good = any(
            sample.ground_truth == SemanticGroundTruth.ANATOMY_GOOD for sample in training
        )
        training_defect = any(
            sample.ground_truth == SemanticGroundTruth.ANATOMY_DEFECT for sample in training
        )
        sweep = _threshold_sweep(training, threshold_step_micros)
        threshold = _recommended_threshold(
            sweep,
            has_good=training_good,
            has_defect=training_defect,
        )
        if threshold is None:
            continue
        fold_thresholds[fold] = threshold
        for sample in validation:
            actual_defect = sample.ground_truth == SemanticGroundTruth.ANATOMY_DEFECT
            configured_predictions.append(
                (
                    actual_defect,
                    _predicts_defect(sample, configured_baseline_threshold_micros),
                )
            )
            candidate_predictions.append((actual_defect, _predicts_defect(sample, threshold)))
            previous_predictions.append(
                (
                    actual_defect,
                    _predicts_defect(sample, previous_policy_threshold_micros),
                )
            )

    return (
        _validation_metrics(configured_predictions),
        _validation_metrics(candidate_predictions),
        _validation_metrics(previous_predictions),
        tuple(fold_thresholds),
    )


def _threshold_sweep(
    samples: tuple[_CalibrationSample, ...],
    threshold_step_micros: int,
) -> tuple[SemanticConfusionCounts, ...]:
    thresholds = list(range(0, 1_000_001, threshold_step_micros))
    if thresholds[-1] != 1_000_000:
        thresholds.append(1_000_000)
    return tuple(_confusion_counts(samples, threshold) for threshold in thresholds)


def _validation_metrics(
    predictions: list[tuple[bool, bool]],
) -> SemanticValidationMetrics | None:
    if not predictions:
        return None
    true_positive = false_positive = true_negative = false_negative = 0
    for actual_defect, predicted_defect in predictions:
        if actual_defect and predicted_defect:
            true_positive += 1
        elif not actual_defect and predicted_defect:
            false_positive += 1
        elif not actual_defect:
            true_negative += 1
        else:
            false_negative += 1
    return SemanticValidationMetrics(
        sample_count=len(predictions),
        true_positive=true_positive,
        false_positive=false_positive,
        true_negative=true_negative,
        false_negative=false_negative,
    )


def _learning_outcome(
    *,
    enough_labels: bool,
    validation_complete: bool,
    has_previous_active_policy: bool,
    candidate_threshold_micros: int | None,
    previous_policy_threshold_micros: int,
    current_candidate_validation: SemanticValidationMetrics | None,
    previous_policy_validation: SemanticValidationMetrics | None,
) -> tuple[str, int, bool]:
    if not enough_labels:
        return "collecting", previous_policy_threshold_micros, False
    if (
        not validation_complete
        or candidate_threshold_micros is None
        or current_candidate_validation is None
        or previous_policy_validation is None
    ):
        return "calibrating", previous_policy_threshold_micros, False

    candidate_f1 = current_candidate_validation.f1_micros
    previous_f1 = previous_policy_validation.f1_micros
    candidate_recall = current_candidate_validation.recall_micros
    previous_recall = previous_policy_validation.recall_micros
    candidate_fpr = current_candidate_validation.false_positive_rate_micros
    previous_fpr = previous_policy_validation.false_positive_rate_micros
    if None in (
        candidate_f1,
        previous_f1,
        candidate_recall,
        previous_recall,
        candidate_fpr,
        previous_fpr,
    ):
        return "calibrating", previous_policy_threshold_micros, False

    assert candidate_f1 is not None
    assert previous_f1 is not None
    assert candidate_recall is not None
    assert previous_recall is not None
    assert candidate_fpr is not None
    assert previous_fpr is not None
    no_regression = (
        candidate_f1 >= previous_f1
        and candidate_recall >= previous_recall
        and candidate_fpr <= previous_fpr
    )
    measurable_gain = (
        candidate_f1 > previous_f1
        or candidate_recall > previous_recall
        or candidate_fpr < previous_fpr
    )
    if no_regression and measurable_gain:
        return (
            "improved",
            candidate_threshold_micros,
            not has_previous_active_policy
            or candidate_threshold_micros != previous_policy_threshold_micros,
        )
    if no_regression:
        if not has_previous_active_policy:
            # The configured baseline is only a fallback until the first
            # adequately validated snapshot pins an immutable champion version.
            return "stable", candidate_threshold_micros, True
        return "stable", previous_policy_threshold_micros, False
    return "regressed", previous_policy_threshold_micros, False


def _predicts_defect(sample: _CalibrationSample, threshold_micros: int) -> bool:
    return sample.verdict == SemanticVerdict.SEVERE and sample.confidence_micros >= threshold_micros


def _confusion_counts(
    samples: tuple[_CalibrationSample, ...],
    threshold_micros: int,
) -> SemanticConfusionCounts:
    true_positive = false_positive = true_negative = false_negative = 0
    for sample in samples:
        actual_defect = sample.ground_truth == SemanticGroundTruth.ANATOMY_DEFECT
        predicted_defect = _predicts_defect(sample, threshold_micros)
        if actual_defect and predicted_defect:
            true_positive += 1
        elif not actual_defect and predicted_defect:
            false_positive += 1
        elif not actual_defect:
            true_negative += 1
        else:
            false_negative += 1
    return SemanticConfusionCounts(
        threshold_micros=threshold_micros,
        true_positive=true_positive,
        false_positive=false_positive,
        true_negative=true_negative,
        false_negative=false_negative,
    )


def _recommended_threshold(
    sweep: tuple[SemanticConfusionCounts, ...],
    *,
    has_good: bool,
    has_defect: bool,
) -> int | None:
    if not has_good or not has_defect:
        return None
    best = max(
        sweep,
        key=lambda item: (
            item.f1_micros or 0,
            item.precision_micros or 0,
            item.specificity_micros or 0,
            item.threshold_micros,
        ),
    )
    return best.threshold_micros


def _idempotent_feedback_result(
    existing: SemanticAnatomyFeedback,
    *,
    agreement: SemanticFeedbackAgreement,
    ground_truth: SemanticGroundTruth,
    issue_code: SemanticIssueCode | None,
    note: str | None,
) -> SemanticAnatomyFeedbackResult:
    if (
        existing.agreement != agreement
        or existing.ground_truth != ground_truth
        or existing.issue_code != issue_code
        or existing.note != note
    ):
        raise SemanticFeedbackConflictError(
            "different anatomy feedback already exists for this assessment and owner"
        )
    return _feedback_result(existing, created=False)


def _feedback_result(
    feedback: SemanticAnatomyFeedback,
    *,
    created: bool,
) -> SemanticAnatomyFeedbackResult:
    return SemanticAnatomyFeedbackResult(
        feedback_id=feedback.id,
        assessment_id=feedback.semantic_assessment_id,
        asset_id=feedback.asset_id,
        user_id=feedback.feedback_by_user_id,
        agreement=feedback.agreement,
        ground_truth=feedback.ground_truth,
        issue_code=feedback.issue_code,
        note=feedback.note,
        created_at=feedback.created_at,
        created=created,
    )


def _artifact_result(
    artifact: SemanticCalibrationArtifact,
    *,
    created: bool,
) -> SemanticCalibrationArtifactResult:
    report = artifact.report if isinstance(artifact.report, dict) else {}
    ground_truth_counts = _wire_mapping(report.get("ground_truth_counts"))
    anatomy_good_count = _wire_int(ground_truth_counts.get("anatomy_good")) or 0
    anatomy_defect_count = _wire_int(ground_truth_counts.get("anatomy_defect")) or 0
    unjudgeable_count = _wire_int(ground_truth_counts.get("unjudgeable")) or 0
    schema_version = artifact.calibration_schema_version
    active_policy_version: int | None
    effective_threshold: int | None

    if schema_version == SEMANTIC_CALIBRATION_SCHEMA_VERSION:
        source_counts = _wire_mapping(report.get("source_counts"))
        validation = _wire_mapping(report.get("validation"))
        configured_validation = _wire_mapping(validation.get("configured_baseline"))
        candidate_validation = _wire_mapping(validation.get("current_candidate"))
        previous_validation = _wire_mapping(validation.get("previous_policy"))
        learning_status = _wire_learning_status(report.get("learning_status"))
        previous_artifact_version = _wire_positive_int(report.get("previous_artifact_version"))
        previous_active_policy_version = _wire_positive_int(
            report.get("previous_active_policy_version")
        )
        if (
            previous_active_policy_version is not None
            and previous_active_policy_version >= artifact.version
        ):
            previous_active_policy_version = None
        explicit_label_count = _wire_int(source_counts.get("explicit")) or 0
        inferred_label_count = _wire_int(source_counts.get("inferred")) or 0
        configured_baseline_threshold = _wire_threshold(
            report.get("configured_baseline_threshold_micros")
        )
        candidate_threshold = _wire_threshold(report.get("candidate_threshold_micros"))
        previous_policy_threshold = _wire_threshold(report.get("previous_policy_threshold_micros"))
        reported_effective_threshold = _wire_threshold(report.get("effective_threshold_micros"))
        active_policy_changed = (
            report.get("active_policy_changed") is True
            and artifact.ready_for_enforcement
            and learning_status in {"improved", "stable"}
            and candidate_threshold is not None
            and reported_effective_threshold == candidate_threshold
        )
        if active_policy_changed:
            active_policy_version = artifact.version
            effective_threshold = candidate_threshold
        else:
            active_policy_version = previous_active_policy_version
            effective_threshold = (
                previous_policy_threshold if active_policy_version is not None else None
            )
    elif schema_version == LEGACY_SEMANTIC_CALIBRATION_SCHEMA_VERSION:
        # v2 artifacts remain readable.  They did not contain out-of-fold
        # evidence, so they can provide a threshold but never an improvement
        # claim.  A ready v2 threshold is treated as the prior champion.
        configured_validation = {}
        candidate_validation = {}
        previous_validation = {}
        learning_status = "calibrating" if artifact.ready_for_enforcement else "collecting"
        active_policy_changed = False
        previous_artifact_version = None
        explicit_label_count = (
            _wire_int(report.get("feedback_count"))
            or anatomy_good_count + anatomy_defect_count + unjudgeable_count
        )
        inferred_label_count = 0
        configured_baseline_threshold = None
        candidate_threshold = artifact.recommended_threshold_micros
        effective_threshold = (
            artifact.recommended_threshold_micros if artifact.ready_for_enforcement else None
        )
        active_policy_version = artifact.version if effective_threshold is not None else None
    else:
        # Unsupported report schemas must not inherit v2's activation rules.
        # They remain visible for diagnostics, but enforcement falls back to
        # the operator's configured threshold until the schema is understood.
        configured_validation = {}
        candidate_validation = {}
        previous_validation = {}
        learning_status = "collecting"
        active_policy_changed = False
        previous_artifact_version = None
        explicit_label_count = (
            _wire_int(report.get("feedback_count"))
            or anatomy_good_count + anatomy_defect_count + unjudgeable_count
        )
        inferred_label_count = 0
        configured_baseline_threshold = None
        candidate_threshold = None
        effective_threshold = None
        active_policy_version = None

    validation_f1 = _wire_ratio(candidate_validation.get("f1_micros"))
    previous_validation_f1 = _wire_ratio(previous_validation.get("f1_micros"))
    validation_f1_delta = (
        validation_f1 - previous_validation_f1
        if validation_f1 is not None and previous_validation_f1 is not None
        else None
    )
    return SemanticCalibrationArtifactResult(
        artifact_id=artifact.id,
        profile_sha256=artifact.profile_sha256,
        version=artifact.version,
        dataset_sha256=artifact.dataset_sha256,
        report_sha256=artifact.report_sha256,
        sample_count=artifact.sample_count,
        recommended_threshold_micros=artifact.recommended_threshold_micros,
        ready_for_enforcement=artifact.ready_for_enforcement,
        created_at=artifact.created_at,
        created=created,
        calibration_schema_version=schema_version,
        learning_status=learning_status,
        minimum_samples=(
            _wire_positive_int(report.get("minimum_samples")) or DEFAULT_CALIBRATION_MINIMUM_SAMPLES
        ),
        minimum_per_class=(
            _wire_positive_int(report.get("minimum_per_class"))
            or DEFAULT_CALIBRATION_MINIMUM_PER_CLASS
        ),
        anatomy_good_count=anatomy_good_count,
        anatomy_defect_count=anatomy_defect_count,
        unjudgeable_count=unjudgeable_count,
        explicit_label_count=explicit_label_count,
        inferred_label_count=inferred_label_count,
        validation_sample_count=_wire_int(candidate_validation.get("sample_count")) or 0,
        validation_f1_micros=validation_f1,
        previous_validation_f1_micros=previous_validation_f1,
        validation_f1_delta_micros=validation_f1_delta,
        validation_recall_micros=_wire_ratio(candidate_validation.get("recall_micros")),
        previous_validation_recall_micros=_wire_ratio(previous_validation.get("recall_micros")),
        validation_false_positive_rate_micros=_wire_ratio(
            candidate_validation.get("false_positive_rate_micros")
        ),
        previous_validation_false_positive_rate_micros=_wire_ratio(
            previous_validation.get("false_positive_rate_micros")
        ),
        configured_baseline_validation_f1_micros=_wire_ratio(
            configured_validation.get("f1_micros")
        ),
        effective_threshold_micros=effective_threshold,
        candidate_threshold_micros=candidate_threshold,
        configured_baseline_threshold_micros=configured_baseline_threshold,
        active_policy_changed=active_policy_changed,
        previous_artifact_version=previous_artifact_version,
        active_policy_version=active_policy_version,
    )


async def _latest_artifact_record(
    session: AsyncSession,
    *,
    profile_sha256: str,
) -> SemanticCalibrationArtifact | None:
    artifact: SemanticCalibrationArtifact | None = await session.scalar(
        select(SemanticCalibrationArtifact)
        .where(SemanticCalibrationArtifact.profile_sha256 == profile_sha256)
        .order_by(SemanticCalibrationArtifact.version.desc())
        .limit(1)
    )
    return artifact


def _wire_mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _wire_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _wire_positive_int(value: object) -> int | None:
    parsed = _wire_int(value)
    return parsed if parsed is not None and parsed > 0 else None


def _wire_ratio(value: object) -> int | None:
    parsed = _wire_int(value)
    return parsed if parsed is not None and parsed <= 1_000_000 else None


def _wire_threshold(value: object) -> int | None:
    return _wire_ratio(value)


def _wire_learning_status(value: object) -> str:
    return value if isinstance(value, str) and value in _LEARNING_STATUSES else "calibrating"


async def _feedback_for_owner(
    session: AsyncSession,
    *,
    assessment_id: UUID,
    user_id: UUID,
) -> SemanticAnatomyFeedback | None:
    feedback: SemanticAnatomyFeedback | None = await session.scalar(
        select(SemanticAnatomyFeedback).where(
            SemanticAnatomyFeedback.semantic_assessment_id == assessment_id,
            SemanticAnatomyFeedback.feedback_by_user_id == user_id,
        )
    )
    return feedback


def _agreement(value: SemanticFeedbackAgreement) -> SemanticFeedbackAgreement:
    try:
        return SemanticFeedbackAgreement(value)
    except ValueError as exc:
        raise SemanticFeedbackValidationError("invalid feedback agreement") from exc


def _ground_truth(value: SemanticGroundTruth) -> SemanticGroundTruth:
    try:
        return SemanticGroundTruth(value)
    except ValueError as exc:
        raise SemanticFeedbackValidationError("invalid anatomy ground truth") from exc


def _issue_code(value: SemanticIssueCode | None) -> SemanticIssueCode | None:
    if value is None:
        return None
    try:
        return SemanticIssueCode(value)
    except ValueError as exc:
        raise SemanticFeedbackValidationError("invalid semantic issue code") from exc


def _note(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        raise SemanticFeedbackValidationError("feedback note cannot be blank")
    if len(normalized) > 2_000:
        raise SemanticFeedbackValidationError("feedback note exceeds 2000 characters")
    return normalized


def _normalize_feedback_source(value: SemanticFeedbackSource) -> SemanticFeedbackSource:
    try:
        return SemanticFeedbackSource(value)
    except ValueError as exc:
        raise SemanticFeedbackValidationError("invalid semantic feedback source") from exc


def _feedback_note(value: str | None, *, source: SemanticFeedbackSource) -> str | None:
    if source == SemanticFeedbackSource.EXPLICIT:
        normalized = _note(value)
        if normalized is not None and normalized.casefold().startswith("system:"):
            raise SemanticFeedbackValidationError(
                "feedback notes beginning with 'system:' are reserved"
            )
        return normalized
    if value is not None:
        raise SemanticFeedbackValidationError(
            "system-inferred feedback cannot include a caller-provided note"
        )
    return _INFERRED_NOTE_BY_SOURCE[source]


def _sha256(value: str, *, label: str) -> str:
    normalized = value.strip().lower()
    invalid_character = any(character not in "0123456789abcdef" for character in normalized)
    if len(normalized) != 64 or invalid_character:
        raise SemanticFeedbackValidationError(f"{label} must be a lowercase SHA-256 digest")
    return normalized


def _positive_int(value: int, *, label: str, maximum: int) -> None:
    if isinstance(value, bool) or value <= 0 or value > maximum:
        raise SemanticFeedbackValidationError(f"{label} must be between 1 and {maximum}")


def _confidence_threshold(value: int, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 1_000_000:
        raise SemanticFeedbackValidationError(f"{label} must be between 0 and 1000000")
    return value


def _ratio_micros(numerator: int, denominator: int) -> int | None:
    if denominator == 0:
        return None
    return round((numerator * 1_000_000) / denominator)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise SemanticFeedbackValidationError("feedback timestamps must be timezone-aware")
    return value.astimezone(UTC)
