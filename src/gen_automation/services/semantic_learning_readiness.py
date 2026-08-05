"""Read-only readiness reporting for personalized semantic anatomy learning.

This module deliberately contains no model fitting or inference dependencies.  It
turns the existing immutable assessment/feedback records into a deterministic
dataset inventory that a later CPU meta-classifier or VLM fine-tune can consume.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from gen_automation.db.models import (
    AdminUser,
    Asset,
    GenerationJob,
    ReviewTask,
    SemanticAnatomyFeedback,
    SemanticAssessment,
)
from gen_automation.domain.canonical import canonical_sha256
from gen_automation.domain.enums import (
    AdminRole,
    ReviewTaskState,
    SemanticAssessmentState,
    SemanticGroundTruth,
    SemanticIssueCode,
    SemanticVerdict,
)
from gen_automation.services.semantic_feedback import (
    DEFAULT_CALIBRATION_MINIMUM_PER_CLASS,
    DEFAULT_CALIBRATION_MINIMUM_SAMPLES,
    SEMANTIC_INFERRED_ANATOMY_REJECT_NOTE,
    SEMANTIC_INFERRED_REVIEW_ACCEPT_NOTE,
)

SEMANTIC_LEARNING_READINESS_SCHEMA_VERSION = "semantic-learning-readiness/v1"
SEMANTIC_META_FEATURE_SCHEMA_VERSION = "semantic-anatomy-meta-features/v1"

SOURCE_EXPLICIT = "explicit"
SOURCE_INFERRED_REVIEW_ACCEPT = "inferred_review_accept"
SOURCE_INFERRED_ANATOMY_REJECT = "inferred_anatomy_reject"

META_CLASSIFIER_MINIMUM_SAMPLES = 500
META_CLASSIFIER_MINIMUM_GOOD = 200
META_CLASSIFIER_MINIMUM_DEFECT = 150
META_CLASSIFIER_MINIMUM_COMPLETED_REVIEW_SETS = 3
META_CLASSIFIER_MINIMUM_BATCHES = 5
META_EVALUATION_MINIMUM_TRAINING_GOOD = 200
META_EVALUATION_MINIMUM_TRAINING_DEFECT = 100
META_EVALUATION_MINIMUM_HOLDOUT_DEFECT = 50
META_EVALUATION_MAXIMUM_ZERO_ERROR_FALSE_REJECT_UPPER_MICROS = 20_000
META_EVALUATION_CONFIDENCE_ALPHA = 0.05

LORA_MINIMUM_SAMPLES = 2_000
LORA_MINIMUM_GOOD = 1_000
LORA_MINIMUM_DEFECT = 500
LORA_MINIMUM_EXPLICIT = 500
LORA_MINIMUM_COMPLETED_REVIEW_SETS = 5
LORA_MINIMUM_BATCHES = 10
LORA_MINIMUM_READY_ISSUE_FAMILIES = 3
LORA_MINIMUM_SAMPLES_PER_ISSUE_FAMILY = 50

ISSUE_FAMILIES: tuple[tuple[str, tuple[SemanticIssueCode, ...]], ...] = (
    (
        "hand",
        (
            SemanticIssueCode.EXTRA_FINGER,
            SemanticIssueCode.MISSING_FINGER,
            SemanticIssueCode.MALFORMED_HAND,
        ),
    ),
    (
        "foot",
        (
            SemanticIssueCode.EXTRA_TOE,
            SemanticIssueCode.MISSING_TOE,
            SemanticIssueCode.MALFORMED_FOOT,
        ),
    ),
    (
        "limb_or_duplicate",
        (
            SemanticIssueCode.EXTRA_LIMB,
            SemanticIssueCode.MISSING_LIMB,
            SemanticIssueCode.DUPLICATE_BODY_PART,
        ),
    ),
    (
        "joint_or_proportion",
        (
            SemanticIssueCode.IMPOSSIBLE_JOINT,
            SemanticIssueCode.IMPLAUSIBLE_PROPORTION,
        ),
    ),
    ("face", (SemanticIssueCode.SEVERE_FACE_DEFORMATION,)),
)
_ISSUE_FAMILY_BY_CODE = {
    code: family for family, codes in ISSUE_FAMILIES for code in codes
}

SEMANTIC_META_FEATURE_NAMES: tuple[str, ...] = (
    "verdict_pass",
    "verdict_review",
    "verdict_severe",
    "assessment_confidence_micros",
    "issue_count",
    "maximum_issue_confidence_micros",
    "boxed_issue_count",
    *(
        name
        for code in SemanticIssueCode
        for name in (f"{code.value}_present", f"{code.value}_confidence_micros")
    ),
)


@dataclass(frozen=True, slots=True)
class SemanticPredictedIssue:
    code: SemanticIssueCode
    confidence_micros: int
    has_box: bool = False


@dataclass(frozen=True, slots=True)
class _GenerationCohorts:
    checkpoint_cohort: str | None = None
    lora_stack_cohort: str | None = None
    workflow_cohort: str | None = None
    style_cohort: str | None = None


@dataclass(frozen=True, slots=True)
class SemanticLearningSample:
    feedback_id: UUID
    assessment_id: UUID
    asset_id: UUID
    feedback_by_user_id: UUID
    profile_sha256: str
    asset_sha256: str
    ground_truth: SemanticGroundTruth
    owner_issue_code: SemanticIssueCode | None
    source: str
    verdict: SemanticVerdict
    confidence_micros: int
    predicted_issues: tuple[SemanticPredictedIssue, ...]
    release_id: UUID
    generation_job_id: UUID | None
    generated_at: datetime
    labeled_at: datetime
    completed_review: bool
    checkpoint_cohort: str | None = None
    lora_stack_cohort: str | None = None
    workflow_cohort: str | None = None
    style_cohort: str | None = None


@dataclass(frozen=True, slots=True)
class NamedCount:
    name: str
    count: int


@dataclass(frozen=True, slots=True)
class SemanticSplitReadiness:
    release_set_count: int
    completed_review_set_count: int
    generation_batch_count: int
    generated_utc_date_count: int
    set_group_split_eligible: bool
    batch_group_split_eligible: bool
    temporal_split_eligible: bool
    temporal_cut_count: int
    recommended_group_key: str
    evaluation_holdout: SemanticTemporalHoldout | None


@dataclass(frozen=True, slots=True)
class SemanticTemporalHoldout:
    group_key: str
    cutoff_at: datetime
    training_count: int
    training_good_count: int
    training_defect_count: int
    holdout_count: int
    holdout_good_count: int
    holdout_defect_count: int
    zero_false_reject_upper_micros: int | None


@dataclass(frozen=True, slots=True)
class SemanticGenerationCohortDiversity:
    checkpoint_count: int
    lora_stack_count: int
    workflow_count: int
    style_stack_count: int
    missing_style_metadata_count: int


@dataclass(frozen=True, slots=True)
class SemanticPhaseReadiness:
    phase: str
    ready: bool
    blockers: tuple[str, ...]
    operational_target: str


@dataclass(frozen=True, slots=True)
class SemanticDisagreementPriority:
    asset_id: UUID
    assessment_id: UUID
    asset_sha256: str
    kind: str
    priority: int
    ground_truth: SemanticGroundTruth
    verdict: SemanticVerdict


@dataclass(frozen=True, slots=True)
class SemanticProfileLearningReadiness:
    owner_user_id: UUID
    profile_sha256: str
    dataset_sha256: str
    raw_feedback_count: int
    unique_content_count: int
    binary_labeled_count: int
    duplicate_content_count: int
    conflicting_content_count: int
    resolved_conflicting_content_count: int
    excluded_conflicting_content_count: int
    anatomy_good_count: int
    anatomy_defect_count: int
    unjudgeable_count: int
    explicit_label_count: int
    generic_defect_count: int
    ground_truth_counts: tuple[NamedCount, ...]
    source_counts: tuple[NamedCount, ...]
    owner_issue_counts: tuple[NamedCount, ...]
    owner_issue_family_counts: tuple[NamedCount, ...]
    model_issue_counts: tuple[NamedCount, ...]
    model_issue_occurrence_counts: tuple[NamedCount, ...]
    disagreement_counts: tuple[NamedCount, ...]
    audit_priority_counts: tuple[NamedCount, ...]
    disagreement_priority: tuple[SemanticDisagreementPriority, ...]
    audit_priority: tuple[SemanticDisagreementPriority, ...]
    cohorts: SemanticGenerationCohortDiversity
    split: SemanticSplitReadiness
    calibration: SemanticPhaseReadiness
    meta_classifier: SemanticPhaseReadiness
    meta_evaluation: SemanticPhaseReadiness
    lora: SemanticPhaseReadiness


@dataclass(frozen=True, slots=True)
class SemanticLearningReadinessReport:
    schema_version: str
    meta_feature_schema_version: str
    meta_feature_names: tuple[str, ...]
    profiles: tuple[SemanticProfileLearningReadiness, ...]


async def load_semantic_learning_samples(
    session: AsyncSession,
    *,
    owner_user_id: UUID,
    profile_sha256: str | None = None,
) -> tuple[SemanticLearningSample, ...]:
    """Load exact completed assessment/feedback identities without mutating state."""

    if not isinstance(owner_user_id, UUID):
        raise ValueError("owner user ID is invalid")
    if profile_sha256 is not None:
        _validate_sha256(profile_sha256, label="semantic profile")
    completed_review_exists = exists(
        select(ReviewTask.id).where(
            ReviewTask.scoring_run_id == SemanticAssessment.scoring_run_id,
            ReviewTask.state == ReviewTaskState.COMPLETED,
        )
    )
    statement = (
        select(
            SemanticAnatomyFeedback,
            SemanticAssessment,
            Asset,
            GenerationJob,
            completed_review_exists.label("completed_review"),
        )
        .join(
            SemanticAssessment,
            SemanticAssessment.id == SemanticAnatomyFeedback.semantic_assessment_id,
        )
        .join(
            AdminUser,
            AdminUser.id == SemanticAnatomyFeedback.feedback_by_user_id,
        )
        .join(Asset, Asset.id == SemanticAnatomyFeedback.asset_id)
        .outerjoin(GenerationJob, GenerationJob.id == Asset.generation_job_id)
        .where(
            SemanticAssessment.state == SemanticAssessmentState.COMPLETED,
            SemanticAnatomyFeedback.feedback_by_user_id == owner_user_id,
            AdminUser.role == AdminRole.OWNER,
        )
        .order_by(
            SemanticAnatomyFeedback.profile_sha256,
            SemanticAnatomyFeedback.created_at,
            SemanticAnatomyFeedback.id,
        )
    )
    if profile_sha256 is not None:
        statement = statement.where(SemanticAnatomyFeedback.profile_sha256 == profile_sha256)

    rows = (await session.execute(statement)).all()
    samples: list[SemanticLearningSample] = []
    for feedback, assessment, asset, job, completed_review in rows:
        if assessment.verdict is None or assessment.confidence_micros is None:
            continue
        generated_at = job.created_at if job is not None else asset.created_at
        cohorts = _generation_cohorts(job.parameters if job is not None else None)
        samples.append(
            SemanticLearningSample(
                feedback_id=feedback.id,
                assessment_id=assessment.id,
                asset_id=assessment.asset_id,
                feedback_by_user_id=feedback.feedback_by_user_id,
                profile_sha256=assessment.profile_sha256,
                asset_sha256=assessment.asset_sha256,
                ground_truth=feedback.ground_truth,
                owner_issue_code=feedback.issue_code,
                source=_feedback_source(feedback.note),
                verdict=assessment.verdict,
                confidence_micros=assessment.confidence_micros,
                predicted_issues=_predicted_issues(assessment.issues),
                release_id=asset.release_id,
                generation_job_id=asset.generation_job_id,
                generated_at=_as_utc(generated_at),
                labeled_at=_as_utc(feedback.created_at),
                completed_review=bool(completed_review),
                checkpoint_cohort=cohorts.checkpoint_cohort,
                lora_stack_cohort=cohorts.lora_stack_cohort,
                workflow_cohort=cohorts.workflow_cohort,
                style_cohort=cohorts.style_cohort,
            )
        )
    return tuple(samples)


async def build_semantic_learning_readiness_report(
    session: AsyncSession,
    *,
    owner_user_id: UUID,
    profile_sha256: str | None = None,
) -> SemanticLearningReadinessReport:
    samples = await load_semantic_learning_samples(
        session,
        owner_user_id=owner_user_id,
        profile_sha256=profile_sha256,
    )
    return summarize_semantic_learning_readiness(samples)


def summarize_semantic_learning_readiness(
    samples: Iterable[SemanticLearningSample],
) -> SemanticLearningReadinessReport:
    """Build a deterministic per-profile inventory and conservative phase gates."""

    grouped: dict[tuple[UUID, str], list[SemanticLearningSample]] = defaultdict(list)
    for sample in samples:
        _validate_sample(sample)
        grouped[(sample.feedback_by_user_id, sample.profile_sha256)].append(sample)
    profiles = tuple(
        _profile_readiness(
            owner_user_id,
            profile_sha256,
            tuple(grouped[(owner_user_id, profile_sha256)]),
        )
        for owner_user_id, profile_sha256 in sorted(
            grouped,
            key=lambda item: (str(item[0]), item[1]),
        )
    )
    return SemanticLearningReadinessReport(
        schema_version=SEMANTIC_LEARNING_READINESS_SCHEMA_VERSION,
        meta_feature_schema_version=SEMANTIC_META_FEATURE_SCHEMA_VERSION,
        meta_feature_names=SEMANTIC_META_FEATURE_NAMES,
        profiles=profiles,
    )


def semantic_meta_feature_values(sample: SemanticLearningSample) -> tuple[int, ...]:
    """Extract the pinned v1 CPU meta-classifier feature row from VLM output."""

    by_code: dict[SemanticIssueCode, list[int]] = defaultdict(list)
    for issue in sample.predicted_issues:
        by_code[issue.code].append(issue.confidence_micros)
    values: list[int] = [
        int(sample.verdict == SemanticVerdict.PASS),
        int(sample.verdict == SemanticVerdict.REVIEW),
        int(sample.verdict == SemanticVerdict.SEVERE),
        sample.confidence_micros,
        len(sample.predicted_issues),
        max((issue.confidence_micros for issue in sample.predicted_issues), default=0),
        sum(issue.has_box for issue in sample.predicted_issues),
    ]
    for code in SemanticIssueCode:
        confidences = by_code.get(code, [])
        values.extend((int(bool(confidences)), max(confidences, default=0)))
    return tuple(values)


def _profile_readiness(
    owner_user_id: UUID,
    profile_sha256: str,
    raw_samples: tuple[SemanticLearningSample, ...],
) -> SemanticProfileLearningReadiness:
    ordered = tuple(sorted(raw_samples, key=_sample_sort_key))
    by_content: dict[str, list[SemanticLearningSample]] = defaultdict(list)
    for sample in ordered:
        by_content[sample.asset_sha256].append(sample)

    unique: list[SemanticLearningSample] = []
    duplicate_count = 0
    conflicting_count = 0
    resolved_conflicting_count = 0
    excluded_conflicting_count = 0
    for asset_sha256 in sorted(by_content):
        content_samples = by_content[asset_sha256]
        duplicate_count += max(0, len(content_samples) - 1)
        labels = {sample.ground_truth for sample in content_samples}
        strongest_source_rank = max(_source_rank(sample.source) for sample in content_samples)
        strongest = tuple(
            sample
            for sample in content_samples
            if _source_rank(sample.source) == strongest_source_rank
        )
        strongest_labels = {sample.ground_truth for sample in strongest}
        if len(labels) != 1:
            conflicting_count += 1
            if len(strongest_labels) != 1:
                excluded_conflicting_count += 1
                continue
            resolved_conflicting_count += 1
        unique.append(min(strongest, key=_evidence_tiebreak_key))

    eligible = tuple(unique)
    binary = tuple(
        sample
        for sample in eligible
        if sample.ground_truth
        in (SemanticGroundTruth.ANATOMY_GOOD, SemanticGroundTruth.ANATOMY_DEFECT)
    )
    good_count = sum(
        sample.ground_truth == SemanticGroundTruth.ANATOMY_GOOD for sample in binary
    )
    defect_count = len(binary) - good_count
    unjudgeable_count = sum(
        sample.ground_truth == SemanticGroundTruth.UNJUDGEABLE for sample in eligible
    )
    explicit_count = sum(sample.source == SOURCE_EXPLICIT for sample in binary)
    generic_defect_count = sum(
        sample.ground_truth == SemanticGroundTruth.ANATOMY_DEFECT
        and sample.owner_issue_code is None
        for sample in binary
    )

    owner_issues = Counter(
        sample.owner_issue_code.value
        for sample in binary
        if sample.owner_issue_code is not None
    )
    family_counts = Counter(
        _ISSUE_FAMILY_BY_CODE[sample.owner_issue_code]
        for sample in binary
        if sample.owner_issue_code is not None
    )
    model_issue_occurrences = Counter(
        issue.code.value for sample in eligible for issue in sample.predicted_issues
    )
    model_issue_images = Counter(
        code
        for sample in eligible
        for code in {issue.code.value for issue in sample.predicted_issues}
    )
    priorities = tuple(
        sorted(
            filter(None, (_disagreement_priority(sample) for sample in binary)),
            key=lambda item: (-item.priority, item.asset_sha256, str(item.assessment_id)),
        )
    )
    disagreement_kinds = frozenset({"missed_defect", "false_severe", "false_review"})
    disagreements = tuple(item for item in priorities if item.kind in disagreement_kinds)
    disagreement_counts = Counter(item.kind for item in disagreements)
    audit_priority_counts = Counter(item.kind for item in priorities)
    split = _split_readiness(binary)
    ready_families = sum(
        count >= LORA_MINIMUM_SAMPLES_PER_ISSUE_FAMILY for count in family_counts.values()
    )

    calibration_blockers = _minimum_blockers(
        binary_count=len(binary),
        good_count=good_count,
        defect_count=defect_count,
        minimum_samples=DEFAULT_CALIBRATION_MINIMUM_SAMPLES,
        minimum_good=DEFAULT_CALIBRATION_MINIMUM_PER_CLASS,
        minimum_defect=DEFAULT_CALIBRATION_MINIMUM_PER_CLASS,
    )
    meta_blockers = list(
        _minimum_blockers(
            binary_count=len(binary),
            good_count=good_count,
            defect_count=defect_count,
            minimum_samples=META_CLASSIFIER_MINIMUM_SAMPLES,
            minimum_good=META_CLASSIFIER_MINIMUM_GOOD,
            minimum_defect=META_CLASSIFIER_MINIMUM_DEFECT,
        )
    )
    if split.completed_review_set_count < META_CLASSIFIER_MINIMUM_COMPLETED_REVIEW_SETS:
        meta_blockers.append(
            f"need {META_CLASSIFIER_MINIMUM_COMPLETED_REVIEW_SETS} completed review sets"
        )
    if split.generation_batch_count < META_CLASSIFIER_MINIMUM_BATCHES:
        meta_blockers.append(f"need {META_CLASSIFIER_MINIMUM_BATCHES} generation batches")
    if not split.temporal_split_eligible:
        meta_blockers.append("need a class-complete temporal holdout split")

    evaluation_blockers = _meta_evaluation_blockers(split.evaluation_holdout)

    lora_blockers = list(
        _minimum_blockers(
            binary_count=len(binary),
            good_count=good_count,
            defect_count=defect_count,
            minimum_samples=LORA_MINIMUM_SAMPLES,
            minimum_good=LORA_MINIMUM_GOOD,
            minimum_defect=LORA_MINIMUM_DEFECT,
        )
    )
    if explicit_count < LORA_MINIMUM_EXPLICIT:
        lora_blockers.append(f"need {LORA_MINIMUM_EXPLICIT} explicit owner labels")
    if split.completed_review_set_count < LORA_MINIMUM_COMPLETED_REVIEW_SETS:
        lora_blockers.append(f"need {LORA_MINIMUM_COMPLETED_REVIEW_SETS} completed review sets")
    if split.generation_batch_count < LORA_MINIMUM_BATCHES:
        lora_blockers.append(f"need {LORA_MINIMUM_BATCHES} generation batches")
    if ready_families < LORA_MINIMUM_READY_ISSUE_FAMILIES:
        lora_blockers.append(
            "need "
            f"{LORA_MINIMUM_READY_ISSUE_FAMILIES} issue families with "
            f"{LORA_MINIMUM_SAMPLES_PER_ISSUE_FAMILY} labels each"
        )
    if not split.temporal_split_eligible:
        lora_blockers.append("need a class-complete temporal holdout split")
    if evaluation_blockers:
        lora_blockers.append("need a promotion-capable untouched temporal holdout")

    cohort_diversity = _cohort_diversity(binary)

    identity = {
        "schema_version": SEMANTIC_LEARNING_READINESS_SCHEMA_VERSION,
        "owner_user_id": str(owner_user_id),
        "profile_sha256": profile_sha256,
        "samples": [
            {
                "feedback_id": str(sample.feedback_id),
                "asset_sha256": sample.asset_sha256,
                "ground_truth": sample.ground_truth.value,
                "owner_issue_code": (
                    sample.owner_issue_code.value if sample.owner_issue_code is not None else None
                ),
                "source": sample.source,
                "release_id": str(sample.release_id),
                "generation_job_id": (
                    str(sample.generation_job_id)
                    if sample.generation_job_id is not None
                    else None
                ),
            }
            for sample in eligible
        ],
    }
    return SemanticProfileLearningReadiness(
        owner_user_id=owner_user_id,
        profile_sha256=profile_sha256,
        dataset_sha256=canonical_sha256(identity),
        raw_feedback_count=len(raw_samples),
        unique_content_count=len(eligible),
        binary_labeled_count=len(binary),
        duplicate_content_count=duplicate_count,
        conflicting_content_count=conflicting_count,
        resolved_conflicting_content_count=resolved_conflicting_count,
        excluded_conflicting_content_count=excluded_conflicting_count,
        anatomy_good_count=good_count,
        anatomy_defect_count=defect_count,
        unjudgeable_count=unjudgeable_count,
        explicit_label_count=explicit_count,
        generic_defect_count=generic_defect_count,
        ground_truth_counts=_named_counts(
            Counter(sample.ground_truth.value for sample in eligible),
            names=tuple(item.value for item in SemanticGroundTruth),
        ),
        source_counts=_named_counts(
            Counter(sample.source for sample in eligible),
            names=(
                SOURCE_EXPLICIT,
                SOURCE_INFERRED_REVIEW_ACCEPT,
                SOURCE_INFERRED_ANATOMY_REJECT,
            ),
        ),
        owner_issue_counts=_named_counts(
            owner_issues,
            names=tuple(item.value for item in SemanticIssueCode),
        ),
        owner_issue_family_counts=_named_counts(
            family_counts,
            names=tuple(family for family, _codes in ISSUE_FAMILIES),
        ),
        model_issue_counts=_named_counts(
            model_issue_images,
            names=tuple(item.value for item in SemanticIssueCode),
        ),
        model_issue_occurrence_counts=_named_counts(
            model_issue_occurrences,
            names=tuple(item.value for item in SemanticIssueCode),
        ),
        disagreement_counts=_named_counts(disagreement_counts),
        audit_priority_counts=_named_counts(audit_priority_counts),
        disagreement_priority=disagreements[:50],
        audit_priority=priorities[:50],
        cohorts=cohort_diversity,
        split=split,
        calibration=_phase(
            "calibration",
            calibration_blockers,
            "100 binary labels with at least 20 good and 20 defective",
        ),
        meta_classifier=_phase(
            "meta_classifier",
            tuple(meta_blockers),
            "500 binary labels, 150 defects, multiple completed sets, grouped temporal holdout",
        ),
        meta_evaluation=_phase(
            "meta_evaluation",
            evaluation_blockers,
            "untouched temporal holdout with a <=2% zero-error false-reject upper bound",
        ),
        lora=_phase(
            "lora",
            tuple(lora_blockers),
            "2000 binary labels, 500 defects, explicit and issue-family diversity",
        ),
    )


def _split_readiness(samples: tuple[SemanticLearningSample, ...]) -> SemanticSplitReadiness:
    sets = {sample.release_id for sample in samples}
    completed_sets = {sample.release_id for sample in samples if sample.completed_review}
    batches = {
        sample.generation_job_id
        for sample in samples
        if sample.generation_job_id is not None
    }
    dates = {sample.generated_at.date() for sample in samples}
    set_eligible = _class_spans_multiple_groups(samples, lambda sample: sample.release_id)
    batch_eligible = _class_spans_multiple_groups(
        samples,
        lambda sample: sample.generation_job_id,
        reject_none=True,
    )
    recommended = (
        "release_id"
        if set_eligible
        else "generation_job_id"
        if batch_eligible
        else "asset_sha256"
    )
    temporal_splits = _temporal_group_splits(samples, group_key=recommended)
    return SemanticSplitReadiness(
        release_set_count=len(sets),
        completed_review_set_count=len(completed_sets),
        generation_batch_count=len(batches),
        generated_utc_date_count=len(dates),
        set_group_split_eligible=set_eligible,
        batch_group_split_eligible=batch_eligible,
        temporal_split_eligible=bool(temporal_splits),
        temporal_cut_count=len(temporal_splits),
        recommended_group_key=recommended,
        evaluation_holdout=_select_evaluation_holdout(temporal_splits),
    )


def _class_spans_multiple_groups(
    samples: tuple[SemanticLearningSample, ...],
    group_key: Callable[[SemanticLearningSample], object],
    *,
    reject_none: bool = False,
) -> bool:
    groups_by_class: dict[SemanticGroundTruth, set[object]] = defaultdict(set)
    for sample in samples:
        key = group_key(sample)
        if reject_none and key is None:
            continue
        groups_by_class[sample.ground_truth].add(key)
    return all(
        len(groups_by_class[label]) >= 2
        for label in (SemanticGroundTruth.ANATOMY_GOOD, SemanticGroundTruth.ANATOMY_DEFECT)
    )


def _temporal_group_splits(
    samples: tuple[SemanticLearningSample, ...],
    *,
    group_key: str,
) -> tuple[SemanticTemporalHoldout, ...]:
    if len(samples) < 4 or group_key == "asset_sha256":
        return ()
    grouped: dict[object, list[SemanticLearningSample]] = defaultdict(list)
    for sample in samples:
        key: object = (
            sample.release_id
            if group_key == "release_id"
            else sample.generation_job_id
        )
        if key is not None:
            grouped[key].append(sample)
    cohorts: dict[datetime, list[SemanticLearningSample]] = defaultdict(list)
    for group in grouped.values():
        cohorts[min(sample.generated_at for sample in group)].extend(group)
    timestamps = sorted(cohorts)
    result: list[SemanticTemporalHoldout] = []
    for cutoff in timestamps[1:]:
        training = tuple(
            sample
            for timestamp in timestamps
            if timestamp < cutoff
            for sample in cohorts[timestamp]
        )
        holdout = tuple(
            sample
            for timestamp in timestamps
            if timestamp >= cutoff
            for sample in cohorts[timestamp]
        )
        if _has_both_binary_classes(training) and _has_both_binary_classes(holdout):
            training_good, training_defect = _binary_class_counts(training)
            holdout_good, holdout_defect = _binary_class_counts(holdout)
            result.append(
                SemanticTemporalHoldout(
                    group_key=group_key,
                    cutoff_at=cutoff,
                    training_count=len(training),
                    training_good_count=training_good,
                    training_defect_count=training_defect,
                    holdout_count=len(holdout),
                    holdout_good_count=holdout_good,
                    holdout_defect_count=holdout_defect,
                    zero_false_reject_upper_micros=(
                        _zero_error_upper_micros(holdout_good)
                        if holdout_good > 0
                        else None
                    ),
                )
            )
    return tuple(result)


def _select_evaluation_holdout(
    candidates: tuple[SemanticTemporalHoldout, ...],
) -> SemanticTemporalHoldout | None:
    if not candidates:
        return None

    def shortfall(candidate: SemanticTemporalHoldout) -> int:
        bound = candidate.zero_false_reject_upper_micros or 1_000_000
        return sum(
            (
                max(
                    0,
                    META_EVALUATION_MINIMUM_TRAINING_GOOD
                    - candidate.training_good_count,
                ),
                max(
                    0,
                    META_EVALUATION_MINIMUM_TRAINING_DEFECT
                    - candidate.training_defect_count,
                ),
                max(
                    0,
                    META_EVALUATION_MINIMUM_HOLDOUT_DEFECT
                    - candidate.holdout_defect_count,
                ),
                max(
                    0,
                    bound - META_EVALUATION_MAXIMUM_ZERO_ERROR_FALSE_REJECT_UPPER_MICROS,
                ),
            )
        )

    return min(
        candidates,
        key=lambda candidate: (
            len(_meta_evaluation_blockers(candidate)),
            shortfall(candidate),
            abs(
                (
                    candidate.holdout_count
                    / (candidate.training_count + candidate.holdout_count)
                )
                - 0.2
            ),
            candidate.cutoff_at,
        ),
    )


def _meta_evaluation_blockers(
    holdout: SemanticTemporalHoldout | None,
) -> tuple[str, ...]:
    if holdout is None:
        return ("need an untouched class-complete chronological group holdout",)
    blockers: list[str] = []
    if holdout.training_good_count < META_EVALUATION_MINIMUM_TRAINING_GOOD:
        blockers.append(
            f"need {META_EVALUATION_MINIMUM_TRAINING_GOOD} training anatomy-good labels"
        )
    if holdout.training_defect_count < META_EVALUATION_MINIMUM_TRAINING_DEFECT:
        blockers.append(
            f"need {META_EVALUATION_MINIMUM_TRAINING_DEFECT} training anatomy-defect labels"
        )
    if holdout.holdout_defect_count < META_EVALUATION_MINIMUM_HOLDOUT_DEFECT:
        blockers.append(
            f"need {META_EVALUATION_MINIMUM_HOLDOUT_DEFECT} holdout anatomy-defect labels"
        )
    upper = holdout.zero_false_reject_upper_micros
    if (
        upper is None
        or upper > META_EVALUATION_MAXIMUM_ZERO_ERROR_FALSE_REJECT_UPPER_MICROS
    ):
        blockers.append("need enough holdout anatomy-good labels for a <=2% 95% upper bound")
    return tuple(blockers)


def _zero_error_upper_micros(sample_count: int) -> int:
    """Exact one-sided 95% binomial upper bound when zero errors are observed."""

    if sample_count <= 0:
        raise ValueError("zero-error bound requires a positive sample count")
    value = (1 - (META_EVALUATION_CONFIDENCE_ALPHA ** (1 / sample_count))) * 1_000_000
    return math.ceil(float(value))


def _binary_class_counts(
    samples: tuple[SemanticLearningSample, ...],
) -> tuple[int, int]:
    good = sum(sample.ground_truth == SemanticGroundTruth.ANATOMY_GOOD for sample in samples)
    return good, len(samples) - good


def _has_both_binary_classes(samples: tuple[SemanticLearningSample, ...]) -> bool:
    labels = {sample.ground_truth for sample in samples}
    return {
        SemanticGroundTruth.ANATOMY_GOOD,
        SemanticGroundTruth.ANATOMY_DEFECT,
    }.issubset(labels)


def _minimum_blockers(
    *,
    binary_count: int,
    good_count: int,
    defect_count: int,
    minimum_samples: int,
    minimum_good: int,
    minimum_defect: int,
) -> tuple[str, ...]:
    blockers: list[str] = []
    if binary_count < minimum_samples:
        blockers.append(f"need {minimum_samples} binary labels")
    if good_count < minimum_good:
        blockers.append(f"need {minimum_good} anatomy-good labels")
    if defect_count < minimum_defect:
        blockers.append(f"need {minimum_defect} anatomy-defect labels")
    return tuple(blockers)


def _phase(
    name: str,
    blockers: tuple[str, ...],
    operational_target: str,
) -> SemanticPhaseReadiness:
    return SemanticPhaseReadiness(
        phase=name,
        ready=not blockers,
        blockers=blockers,
        operational_target=operational_target,
    )


def _cohort_diversity(
    samples: tuple[SemanticLearningSample, ...],
) -> SemanticGenerationCohortDiversity:
    return SemanticGenerationCohortDiversity(
        checkpoint_count=len(
            {sample.checkpoint_cohort for sample in samples if sample.checkpoint_cohort}
        ),
        lora_stack_count=len(
            {sample.lora_stack_cohort for sample in samples if sample.lora_stack_cohort}
        ),
        workflow_count=len(
            {sample.workflow_cohort for sample in samples if sample.workflow_cohort}
        ),
        style_stack_count=len(
            {sample.style_cohort for sample in samples if sample.style_cohort}
        ),
        missing_style_metadata_count=sum(sample.style_cohort is None for sample in samples),
    )


def _disagreement_priority(
    sample: SemanticLearningSample,
) -> SemanticDisagreementPriority | None:
    kind: str | None = None
    priority = 0
    if (
        sample.ground_truth == SemanticGroundTruth.ANATOMY_DEFECT
        and sample.verdict == SemanticVerdict.PASS
    ):
        kind, priority = "missed_defect", 100
    elif (
        sample.ground_truth == SemanticGroundTruth.ANATOMY_GOOD
        and sample.verdict == SemanticVerdict.SEVERE
    ):
        kind, priority = "false_severe", 95
    elif (
        sample.ground_truth == SemanticGroundTruth.ANATOMY_GOOD
        and sample.verdict == SemanticVerdict.REVIEW
    ):
        kind, priority = "false_review", 85
    elif (
        sample.ground_truth == SemanticGroundTruth.ANATOMY_DEFECT
        and sample.verdict == SemanticVerdict.REVIEW
    ):
        kind, priority = "confirmed_review", 70
    elif (
        sample.ground_truth == SemanticGroundTruth.ANATOMY_DEFECT
        and sample.owner_issue_code is None
    ):
        kind, priority = "unspecified_defect", 50
    if kind is None:
        return None
    return SemanticDisagreementPriority(
        asset_id=sample.asset_id,
        assessment_id=sample.assessment_id,
        asset_sha256=sample.asset_sha256,
        kind=kind,
        priority=priority,
        ground_truth=sample.ground_truth,
        verdict=sample.verdict,
    )


def _named_counts(
    counts: Counter[str],
    *,
    names: tuple[str, ...] = (),
) -> tuple[NamedCount, ...]:
    ordered_names = tuple(dict.fromkeys((*names, *sorted(counts))))
    return tuple(NamedCount(name=name, count=counts.get(name, 0)) for name in ordered_names)


def _generation_cohorts(parameters: object) -> _GenerationCohorts:
    """Extract only stable, versioned artifact identities from generation schema v2."""

    if not isinstance(parameters, dict) or parameters.get("schema_version") != 2:
        return _GenerationCohorts()
    checkpoint = parameters.get("checkpoint")
    workflow = parameters.get("workflow")
    loras = parameters.get("loras")
    checkpoint_sha256 = _mapping_sha256(checkpoint)
    workflow_sha256 = _mapping_sha256(workflow)
    if not isinstance(loras, list):
        return _GenerationCohorts(
            checkpoint_cohort=checkpoint_sha256,
            workflow_cohort=workflow_sha256,
        )
    normalized_loras: list[dict[str, object]] = []
    for lora in loras:
        if not isinstance(lora, dict):
            return _GenerationCohorts(
                checkpoint_cohort=checkpoint_sha256,
                workflow_cohort=workflow_sha256,
            )
        digest = _mapping_sha256(lora)
        weight = lora.get("weight")
        if digest is None or isinstance(weight, bool) or not isinstance(weight, (int, float)):
            return _GenerationCohorts(
                checkpoint_cohort=checkpoint_sha256,
                workflow_cohort=workflow_sha256,
            )
        normalized_loras.append({"sha256": digest, "weight": weight})
    lora_stack = canonical_sha256(normalized_loras)
    style_stack = (
        canonical_sha256(
            {
                "checkpoint_sha256": checkpoint_sha256,
                "lora_stack_sha256": lora_stack,
                "workflow_sha256": workflow_sha256,
            }
        )
        if checkpoint_sha256 is not None and workflow_sha256 is not None
        else None
    )
    return _GenerationCohorts(
        checkpoint_cohort=checkpoint_sha256,
        lora_stack_cohort=lora_stack,
        workflow_cohort=workflow_sha256,
        style_cohort=style_stack,
    )


def _mapping_sha256(value: object) -> str | None:
    if not isinstance(value, dict):
        return None
    digest = value.get("sha256")
    if not isinstance(digest, str):
        return None
    try:
        _validate_sha256(digest, label="generation artifact")
    except ValueError:
        return None
    return digest


def _predicted_issues(value: object) -> tuple[SemanticPredictedIssue, ...]:
    if not isinstance(value, list):
        return ()
    result: list[SemanticPredictedIssue] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        try:
            code = SemanticIssueCode(str(item.get("code")))
        except ValueError:
            continue
        confidence = item.get("confidence_micros")
        if isinstance(confidence, bool) or not isinstance(confidence, int):
            continue
        if not 0 <= confidence <= 1_000_000:
            continue
        result.append(
            SemanticPredictedIssue(
                code=code,
                confidence_micros=confidence,
                has_box=isinstance(item.get("box"), dict),
            )
        )
    return tuple(result)


def _feedback_source(note: str | None) -> str:
    if note == SEMANTIC_INFERRED_REVIEW_ACCEPT_NOTE:
        return SOURCE_INFERRED_REVIEW_ACCEPT
    if note == SEMANTIC_INFERRED_ANATOMY_REJECT_NOTE:
        return SOURCE_INFERRED_ANATOMY_REJECT
    return SOURCE_EXPLICIT


def _sample_sort_key(sample: SemanticLearningSample) -> tuple[datetime, str]:
    return sample.labeled_at, str(sample.feedback_id)


def _source_rank(source: str) -> int:
    return 2 if source == SOURCE_EXPLICIT else 1


def _evidence_tiebreak_key(
    sample: SemanticLearningSample,
) -> tuple[int, datetime, str]:
    return (
        -int(sample.owner_issue_code is not None),
        sample.labeled_at,
        str(sample.feedback_id),
    )


def _validate_sample(sample: SemanticLearningSample) -> None:
    _validate_sha256(sample.profile_sha256, label="semantic profile")
    _validate_sha256(sample.asset_sha256, label="asset")
    if not 0 <= sample.confidence_micros <= 1_000_000:
        raise ValueError("semantic confidence is invalid")
    if sample.generated_at.tzinfo is None or sample.labeled_at.tzinfo is None:
        raise ValueError("semantic learning timestamps must be timezone-aware")


def _validate_sha256(value: str, *, label: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{label} digest is invalid")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
