"""Authoritative anatomy feedback and deterministic threshold calibration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
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

SEMANTIC_CALIBRATION_SCHEMA_VERSION = "semantic-anatomy-calibration/v1"
DEFAULT_CALIBRATION_MINIMUM_SAMPLES = 100
DEFAULT_CALIBRATION_MINIMUM_PER_CLASS = 20
DEFAULT_CALIBRATION_THRESHOLD_STEP_MICROS = 50_000


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
    def f1_micros(self) -> int | None:
        return _ratio_micros(
            2 * self.true_positive,
            (2 * self.true_positive) + self.false_positive + self.false_negative,
        )

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
            "f1_micros": self.f1_micros,
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


@dataclass(frozen=True, slots=True)
class _CalibrationSample:
    feedback_id: UUID
    assessment_id: UUID
    asset_id: UUID
    agreement: SemanticFeedbackAgreement
    ground_truth: SemanticGroundTruth
    issue_code: SemanticIssueCode | None
    verdict: SemanticVerdict
    confidence_micros: int
    response_sha256: str

    def identity_wire(self) -> dict[str, str | int | None]:
        return {
            "feedback_id": str(self.feedback_id),
            "assessment_id": str(self.assessment_id),
            "asset_id": str(self.asset_id),
            "agreement": self.agreement.value,
            "ground_truth": self.ground_truth.value,
            "issue_code": self.issue_code.value if self.issue_code is not None else None,
            "verdict": self.verdict.value,
            "confidence_micros": self.confidence_micros,
            "response_sha256": self.response_sha256,
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
    now: datetime | None = None,
) -> SemanticAnatomyFeedbackResult:
    """Record one immutable, idempotent owner label for an exact assessment."""

    normalized_truth = _ground_truth(ground_truth)
    normalized_issue = _issue_code(issue_code)
    normalized_note = _note(note)
    assessment = await session.get(SemanticAssessment, assessment_id)
    if assessment is None:
        raise SemanticFeedbackNotFoundError("semantic assessment does not exist")
    if (
        assessment.state != SemanticAssessmentState.COMPLETED
        or assessment.verdict is None
        or assessment.confidence_micros is None
        or assessment.response_sha256 is None
    ):
        raise SemanticFeedbackAssessmentNotReadyError(
            "semantic assessment is not completed"
        )
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
    return {
        row.semantic_assessment_id: _feedback_result(row, created=False)
        for row in rows
    }


async def build_semantic_calibration_report(
    session: AsyncSession,
    *,
    profile_sha256: str,
    minimum_samples: int = DEFAULT_CALIBRATION_MINIMUM_SAMPLES,
    minimum_per_class: int = DEFAULT_CALIBRATION_MINIMUM_PER_CLASS,
    threshold_step_micros: int = DEFAULT_CALIBRATION_THRESHOLD_STEP_MICROS,
) -> SemanticCalibrationReport:
    """Build a deterministic severe-verdict threshold sweep from owner labels."""

    profile_digest = _sha256(profile_sha256, label="semantic profile")
    _positive_int(minimum_samples, label="minimum samples", maximum=1_000_000)
    _positive_int(minimum_per_class, label="minimum per class", maximum=1_000_000)
    _positive_int(
        threshold_step_micros,
        label="threshold step",
        maximum=1_000_000,
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
                SemanticAssessment.state == SemanticAssessmentState.COMPLETED,
            )
            .order_by(
                SemanticAnatomyFeedback.created_at,
                SemanticAnatomyFeedback.id,
            )
        )
    ).all()
    samples = tuple(_calibration_sample(feedback, assessment) for feedback, assessment in rows)
    dataset_digest = canonical_sha256(
        {
            "schema_version": SEMANTIC_CALIBRATION_SCHEMA_VERSION,
            "profile_sha256": profile_digest,
            "samples": [sample.identity_wire() for sample in samples],
        }
    )
    good_count = sum(
        sample.ground_truth == SemanticGroundTruth.ANATOMY_GOOD for sample in samples
    )
    defect_count = sum(
        sample.ground_truth == SemanticGroundTruth.ANATOMY_DEFECT for sample in samples
    )
    unjudgeable_count = len(samples) - good_count - defect_count
    labeled = tuple(
        sample
        for sample in samples
        if sample.ground_truth != SemanticGroundTruth.UNJUDGEABLE
    )
    thresholds = list(range(0, 1_000_001, threshold_step_micros))
    if thresholds[-1] != 1_000_000:
        thresholds.append(1_000_000)
    sweep = tuple(_confusion_counts(labeled, threshold) for threshold in thresholds)
    recommended = _recommended_threshold(
        sweep,
        has_good=good_count > 0,
        has_defect=defect_count > 0,
    )
    ready = (
        len(labeled) >= minimum_samples
        and good_count >= minimum_per_class
        and defect_count >= minimum_per_class
        and recommended is not None
    )
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
    artifact = await session.scalar(
        select(SemanticCalibrationArtifact)
        .where(SemanticCalibrationArtifact.profile_sha256 == profile_digest)
        .order_by(SemanticCalibrationArtifact.version.desc())
        .limit(1)
    )
    return _artifact_result(artifact, created=False) if artifact is not None else None


async def refresh_semantic_calibration_artifact(
    session: AsyncSession,
    *,
    profile_sha256: str,
    created_by_user_id: UUID,
    now: datetime | None = None,
) -> SemanticCalibrationArtifactResult:
    """Rebuild and persist the exact calibration snapshot after owner feedback."""

    report = await build_semantic_calibration_report(
        session,
        profile_sha256=profile_sha256,
    )
    return await persist_semantic_calibration_artifact(
        session,
        report=report,
        created_by_user_id=created_by_user_id,
        now=now,
    )


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
        agreement=feedback.agreement,
        ground_truth=feedback.ground_truth,
        issue_code=feedback.issue_code,
        verdict=assessment.verdict,
        confidence_micros=assessment.confidence_micros,
        response_sha256=assessment.response_sha256,
    )


def _confusion_counts(
    samples: tuple[_CalibrationSample, ...],
    threshold_micros: int,
) -> SemanticConfusionCounts:
    true_positive = false_positive = true_negative = false_negative = 0
    for sample in samples:
        actual_defect = sample.ground_truth == SemanticGroundTruth.ANATOMY_DEFECT
        predicted_defect = (
            sample.verdict == SemanticVerdict.SEVERE
            and sample.confidence_micros >= threshold_micros
        )
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
    )


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


def _sha256(value: str, *, label: str) -> str:
    normalized = value.strip().lower()
    invalid_character = any(
        character not in "0123456789abcdef" for character in normalized
    )
    if len(normalized) != 64 or invalid_character:
        raise SemanticFeedbackValidationError(f"{label} must be a lowercase SHA-256 digest")
    return normalized


def _positive_int(value: int, *, label: str, maximum: int) -> None:
    if isinstance(value, bool) or value <= 0 or value > maximum:
        raise SemanticFeedbackValidationError(f"{label} must be between 1 and {maximum}")


def _ratio_micros(numerator: int, denominator: int) -> int | None:
    if denominator == 0:
        return None
    return round((numerator * 1_000_000) / denominator)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise SemanticFeedbackValidationError("feedback timestamps must be timezone-aware")
    return value.astimezone(UTC)
