"""Deterministic CPU meta-classifier for personalized anatomy triage.

The model deliberately uses only Python's standard library.  It learns from the
versioned, structured VLM feature vector rather than pixels, so it can be fitted
cheaply and reproducibly in the control plane.  An untouched chronological
holdout remains mandatory for promotion.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from gen_automation.domain.canonical import canonical_json_bytes, canonical_sha256
from gen_automation.domain.enums import SemanticGroundTruth
from gen_automation.services.semantic_learning_readiness import (
    SEMANTIC_META_FEATURE_NAMES,
    SEMANTIC_META_FEATURE_SCHEMA_VERSION,
    SOURCE_EXPLICIT,
    SemanticLearningSample,
    semantic_meta_feature_values,
)

SEMANTIC_META_MODEL_SCHEMA_VERSION = "semantic-anatomy-meta-classifier/v1"
SEMANTIC_META_SPLIT_SCHEMA_VERSION = "semantic-anatomy-meta-split/v1"
MICROS = 1_000_000
NO_KEEP_THRESHOLD_MICROS = -1
NO_REJECT_THRESHOLD_MICROS = MICROS + 1
DEFAULT_CONFIDENCE_ALPHA = 0.05

__all__ = (
    "NO_KEEP_THRESHOLD_MICROS",
    "NO_REJECT_THRESHOLD_MICROS",
    "SEMANTIC_META_MODEL_SCHEMA_VERSION",
    "SEMANTIC_META_SPLIT_SCHEMA_VERSION",
    "BinomialSafetyBounds",
    "SemanticMetaClassifierError",
    "SemanticMetaDatasetSplit",
    "SemanticMetaEvaluation",
    "SemanticMetaExample",
    "SemanticMetaGroupBy",
    "SemanticMetaModel",
    "SemanticMetaPrediction",
    "SemanticMetaPromotionDecision",
    "SemanticMetaPromotionPolicy",
    "SemanticMetaScore",
    "SemanticMetaTrainingParameters",
    "SemanticMetaTriage",
    "binomial_safety_bounds",
    "build_semantic_meta_dataset_split",
    "build_semantic_meta_split_from_learning_samples",
    "chronological_semantic_meta_split",
    "chronological_semantic_meta_split_from_learning_samples",
    "compare_semantic_meta_challenger",
    "deserialize_semantic_meta_model",
    "evaluate_semantic_meta_model",
    "evaluate_semantic_meta_model_on_learning_samples",
    "evaluate_semantic_meta_predictions",
    "fit_semantic_meta_classifier",
    "fit_semantic_meta_classifier_from_learning_samples",
    "predict_semantic_meta_probability",
    "predict_semantic_meta_probability_micros",
    "score_semantic_learning_sample",
    "select_semantic_meta_thresholds",
    "semantic_meta_triage",
)


class SemanticMetaClassifierError(ValueError):
    """The dataset, artifact, or prediction input violates the pinned contract."""


class SemanticMetaTriage(StrEnum):
    KEEP = "keep"
    REVIEW = "review"
    REJECT = "reject"


class SemanticMetaGroupBy(StrEnum):
    RELEASE_SET = "release_set"
    GENERATION_BATCH = "generation_batch"
    ASSET_CONTENT = "asset_content"


@dataclass(frozen=True, slots=True)
class SemanticMetaExample:
    """One deduplicated binary label and its immutable leakage boundaries."""

    sample_id: str
    asset_sha256: str
    group_key: str
    occurred_at: datetime
    is_defect: bool
    feature_values: tuple[int, ...]

    def identity_wire(self) -> dict[str, object]:
        return {
            "sample_id": self.sample_id,
            "asset_sha256": self.asset_sha256,
            "group_key": self.group_key,
            "occurred_at": _utc_wire(self.occurred_at),
            "is_defect": self.is_defect,
            "feature_values": list(self.feature_values),
        }


@dataclass(frozen=True, slots=True)
class SemanticMetaDatasetSplit:
    """Explicit fit/holdout contract; content and groups may not cross the split."""

    training: tuple[SemanticMetaExample, ...]
    holdout: tuple[SemanticMetaExample, ...]
    owner_user_id: str
    profile_sha256: str
    feature_schema_version: str = SEMANTIC_META_FEATURE_SCHEMA_VERSION
    feature_names: tuple[str, ...] = SEMANTIC_META_FEATURE_NAMES

    @property
    def dataset_sha256(self) -> str:
        return canonical_sha256(self.dataset_wire())

    @property
    def training_dataset_sha256(self) -> str:
        return canonical_sha256(
            {
                "schema_version": SEMANTIC_META_SPLIT_SCHEMA_VERSION,
                "feature_schema_version": self.feature_schema_version,
                "feature_names": list(self.feature_names),
                "owner_user_id": self.owner_user_id,
                "profile_sha256": self.profile_sha256,
                "training": [example.identity_wire() for example in self.training],
            }
        )

    @property
    def manifest_sha256(self) -> str:
        return canonical_sha256(self.manifest_wire())

    def dataset_wire(self) -> dict[str, object]:
        return {
            "schema_version": SEMANTIC_META_SPLIT_SCHEMA_VERSION,
            "owner_user_id": self.owner_user_id,
            "profile_sha256": self.profile_sha256,
            "feature_schema_version": self.feature_schema_version,
            "feature_names": list(self.feature_names),
            "training": [example.identity_wire() for example in self.training],
            "holdout": [example.identity_wire() for example in self.holdout],
        }

    def manifest_wire(self) -> dict[str, object]:
        def rows(examples: tuple[SemanticMetaExample, ...]) -> list[dict[str, object]]:
            return [
                {
                    "sample_id": example.sample_id,
                    "asset_sha256": example.asset_sha256,
                    "group_key": example.group_key,
                    "occurred_at": _utc_wire(example.occurred_at),
                    "is_defect": example.is_defect,
                }
                for example in examples
            ]

        return {
            "schema_version": SEMANTIC_META_SPLIT_SCHEMA_VERSION,
            "owner_user_id": self.owner_user_id,
            "profile_sha256": self.profile_sha256,
            "training": rows(self.training),
            "holdout": rows(self.holdout),
        }


@dataclass(frozen=True, slots=True)
class SemanticMetaTrainingParameters:
    iterations: int = 2_000
    learning_rate: float = 0.15
    l2_strength: float = 0.01
    maximum_good_to_defect_ratio: int = 2
    minimum_threshold_samples: int = 5
    target_reject_precision_micros: int = 950_000
    target_keep_npv_micros: int = 950_000

    def to_wire(self) -> dict[str, object]:
        return {
            "iterations": self.iterations,
            "learning_rate_hex": self.learning_rate.hex(),
            "l2_strength_hex": self.l2_strength.hex(),
            "maximum_good_to_defect_ratio": self.maximum_good_to_defect_ratio,
            "minimum_threshold_samples": self.minimum_threshold_samples,
            "target_reject_precision_micros": self.target_reject_precision_micros,
            "target_keep_npv_micros": self.target_keep_npv_micros,
        }


@dataclass(frozen=True, slots=True)
class SemanticMetaModel:
    """Immutable, self-contained linear model artifact."""

    feature_schema_version: str
    feature_names: tuple[str, ...]
    owner_user_id: str
    profile_sha256: str
    training_dataset_sha256: str
    split_manifest_sha256: str
    fit_sample_sha256: str
    fit_sample_count: int
    parameters: SemanticMetaTrainingParameters
    feature_means: tuple[float, ...]
    feature_scales: tuple[float, ...]
    weights: tuple[float, ...]
    intercept: float
    keep_threshold_micros: int
    reject_threshold_micros: int

    @property
    def artifact_sha256(self) -> str:
        return canonical_sha256(self.artifact_wire())

    def artifact_wire(self) -> dict[str, object]:
        return {
            "schema_version": SEMANTIC_META_MODEL_SCHEMA_VERSION,
            "feature_schema_version": self.feature_schema_version,
            "feature_names": list(self.feature_names),
            "owner_user_id": self.owner_user_id,
            "profile_sha256": self.profile_sha256,
            "training_dataset_sha256": self.training_dataset_sha256,
            "split_manifest_sha256": self.split_manifest_sha256,
            "fit_sample_sha256": self.fit_sample_sha256,
            "fit_sample_count": self.fit_sample_count,
            "parameters": self.parameters.to_wire(),
            "feature_means_hex": [value.hex() for value in self.feature_means],
            "feature_scales_hex": [value.hex() for value in self.feature_scales],
            "weights_hex": [value.hex() for value in self.weights],
            "intercept_hex": self.intercept.hex(),
            "keep_threshold_micros": self.keep_threshold_micros,
            "reject_threshold_micros": self.reject_threshold_micros,
        }

    def serialize(self) -> bytes:
        artifact = self.artifact_wire()
        return canonical_json_bytes(
            {"artifact": artifact, "artifact_sha256": canonical_sha256(artifact)}
        )


@dataclass(frozen=True, slots=True)
class SemanticMetaPrediction:
    sample_id: str
    is_defect: bool
    probability_micros: int
    triage: SemanticMetaTriage


@dataclass(frozen=True, slots=True)
class SemanticMetaScore:
    probability_micros: int
    triage: SemanticMetaTriage


@dataclass(frozen=True, slots=True)
class BinomialSafetyBounds:
    lower_micros: int
    upper_micros: int
    method: str


@dataclass(frozen=True, slots=True)
class SemanticMetaEvaluation:
    evaluation_sha256: str
    model_sha256: str
    sample_count: int
    good_count: int
    defect_count: int
    keep_count: int
    review_count: int
    reject_count: int
    good_rejected_count: int
    defect_rejected_count: int
    good_kept_count: int
    defect_kept_count: int
    binary_true_positive: int
    binary_false_positive: int
    binary_true_negative: int
    binary_false_negative: int
    false_reject_rate_micros: int | None
    false_reject_rate_bounds: BinomialSafetyBounds | None
    reject_precision_micros: int | None
    reject_precision_bounds: BinomialSafetyBounds | None
    reject_defect_recall_micros: int | None
    keep_npv_micros: int | None
    keep_npv_bounds: BinomialSafetyBounds | None
    kept_defect_rate_micros: int | None
    binary_f1_micros: int | None
    balanced_accuracy_micros: int | None
    automated_coverage_micros: int
    manual_review_fraction_micros: int


@dataclass(frozen=True, slots=True)
class SemanticMetaPromotionPolicy:
    maximum_false_reject_upper_micros: int = 20_000
    minimum_reject_precision_micros: int = 950_000
    minimum_keep_npv_micros: int = 950_000
    maximum_reject_recall_regression_micros: int = 0
    minimum_improvement_micros: int = 1


@dataclass(frozen=True, slots=True)
class SemanticMetaPromotionDecision:
    promote: bool
    blockers: tuple[str, ...]
    improvements: tuple[str, ...]


def build_semantic_meta_dataset_split(
    *,
    training: Iterable[SemanticMetaExample],
    holdout: Iterable[SemanticMetaExample],
    owner_user_id: UUID,
    profile_sha256: str,
) -> SemanticMetaDatasetSplit:
    """Validate and freeze an externally selected grouped split."""

    split = SemanticMetaDatasetSplit(
        training=_ordered_examples(training),
        holdout=_ordered_examples(holdout),
        owner_user_id=str(owner_user_id),
        profile_sha256=profile_sha256,
    )
    _validate_split(split)
    return split


def chronological_semantic_meta_split(
    examples: Iterable[SemanticMetaExample],
    *,
    owner_user_id: UUID,
    profile_sha256: str,
    holdout_fraction: float = 0.30,
    minimum_training_good: int = 1,
    minimum_training_defect: int = 1,
    minimum_holdout_good: int = 1,
    minimum_holdout_defect: int = 1,
) -> SemanticMetaDatasetSplit:
    """Choose the nearest eligible chronological tail of whole groups.

    This helper never divides a group or content digest.  A manual split can be
    supplied with :func:`build_semantic_meta_dataset_split` when an upstream
    readiness report has already selected the cutoff.
    """

    if not 0 < holdout_fraction < 1:
        raise SemanticMetaClassifierError("holdout fraction must be between zero and one")
    minimums = (
        minimum_training_good,
        minimum_training_defect,
        minimum_holdout_good,
        minimum_holdout_defect,
    )
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 1
        for value in minimums
    ):
        raise SemanticMetaClassifierError("split class minimums must be positive integers")
    ordered = _ordered_examples(examples)
    _validate_example_collection(ordered)
    if len(ordered) < 2:
        raise SemanticMetaClassifierError("at least two examples are required")

    grouped: dict[str, list[SemanticMetaExample]] = defaultdict(list)
    for example in ordered:
        grouped[example.group_key].append(example)
    groups = tuple(
        sorted(
            grouped.values(),
            key=lambda rows: (
                min(_as_utc(row.occurred_at) for row in rows),
                rows[0].group_key,
            ),
        )
    )
    target_holdout = round(len(ordered) * holdout_fraction)
    candidates: list[
        tuple[int, datetime, str, tuple[SemanticMetaExample, ...], tuple[SemanticMetaExample, ...]]
    ] = []
    for boundary in range(1, len(groups)):
        training = _ordered_examples(row for group in groups[:boundary] for row in group)
        holdout = _ordered_examples(row for group in groups[boundary:] for row in group)
        if max(_as_utc(row.occurred_at) for row in training) > min(
            _as_utc(row.occurred_at) for row in holdout
        ):
            continue
        training_good, training_defect = _class_counts(training)
        holdout_good, holdout_defect = _class_counts(holdout)
        if (
            training_good < minimum_training_good
            or training_defect < minimum_training_defect
            or holdout_good < minimum_holdout_good
            or holdout_defect < minimum_holdout_defect
        ):
            continue
        candidates.append(
            (
                abs(len(holdout) - target_holdout),
                min(_as_utc(row.occurred_at) for row in holdout),
                holdout[0].group_key,
                training,
                holdout,
            )
        )
    if not candidates:
        raise SemanticMetaClassifierError(
            "no chronological whole-group split satisfies the requested class minimums"
        )
    _, _, _, training, holdout = min(candidates, key=lambda candidate: candidate[:3])
    return build_semantic_meta_dataset_split(
        training=training,
        holdout=holdout,
        owner_user_id=owner_user_id,
        profile_sha256=profile_sha256,
    )


def build_semantic_meta_split_from_learning_samples(
    *,
    training: Iterable[SemanticLearningSample],
    holdout: Iterable[SemanticLearningSample],
    group_by: SemanticMetaGroupBy = SemanticMetaGroupBy.RELEASE_SET,
) -> SemanticMetaDatasetSplit:
    """Adapt already-selected readiness samples into the strict split contract."""

    training_samples = _deduplicated_binary_learning_samples(training)
    holdout_samples = _deduplicated_binary_learning_samples(holdout)
    owner_user_id, profile_sha256 = _learning_scope(
        (*training_samples, *holdout_samples)
    )
    return build_semantic_meta_dataset_split(
        training=(
            _meta_example_from_learning_sample(sample, group_by=group_by)
            for sample in training_samples
        ),
        holdout=(
            _meta_example_from_learning_sample(sample, group_by=group_by)
            for sample in holdout_samples
        ),
        owner_user_id=owner_user_id,
        profile_sha256=profile_sha256,
    )


def chronological_semantic_meta_split_from_learning_samples(
    samples: Iterable[SemanticLearningSample],
    *,
    group_by: SemanticMetaGroupBy = SemanticMetaGroupBy.RELEASE_SET,
    holdout_fraction: float = 0.30,
    minimum_training_good: int = 1,
    minimum_training_defect: int = 1,
    minimum_holdout_good: int = 1,
    minimum_holdout_defect: int = 1,
) -> SemanticMetaDatasetSplit:
    """Deduplicate owner labels and select an untouched whole-group tail."""

    selected = _deduplicated_binary_learning_samples(samples)
    owner_user_id, profile_sha256 = _learning_scope(selected)
    return chronological_semantic_meta_split(
        (
            _meta_example_from_learning_sample(sample, group_by=group_by)
            for sample in selected
        ),
        owner_user_id=owner_user_id,
        profile_sha256=profile_sha256,
        holdout_fraction=holdout_fraction,
        minimum_training_good=minimum_training_good,
        minimum_training_defect=minimum_training_defect,
        minimum_holdout_good=minimum_holdout_good,
        minimum_holdout_defect=minimum_holdout_defect,
    )


def fit_semantic_meta_classifier_from_learning_samples(
    *,
    training: Iterable[SemanticLearningSample],
    holdout: Iterable[SemanticLearningSample],
    group_by: SemanticMetaGroupBy = SemanticMetaGroupBy.RELEASE_SET,
    parameters: SemanticMetaTrainingParameters | None = None,
) -> SemanticMetaModel:
    """Fit directly from the immutable readiness sample type."""

    split = build_semantic_meta_split_from_learning_samples(
        training=training,
        holdout=holdout,
        group_by=group_by,
    )
    return fit_semantic_meta_classifier(split, parameters=parameters)


def score_semantic_learning_sample(
    model: SemanticMetaModel,
    sample: SemanticLearningSample,
) -> SemanticMetaScore:
    """Score one stored VLM assessment, enforcing owner/profile isolation."""

    _validate_model_learning_scope(model, sample)
    probability = predict_semantic_meta_probability_micros(
        model,
        semantic_meta_feature_values(sample),
    )
    return SemanticMetaScore(
        probability_micros=probability,
        triage=semantic_meta_triage(
            probability,
            keep_threshold_micros=model.keep_threshold_micros,
            reject_threshold_micros=model.reject_threshold_micros,
        ),
    )


def evaluate_semantic_meta_model_on_learning_samples(
    model: SemanticMetaModel,
    samples: Iterable[SemanticLearningSample],
    *,
    group_by: SemanticMetaGroupBy = SemanticMetaGroupBy.RELEASE_SET,
) -> SemanticMetaEvaluation:
    """Evaluate a challenger on deduplicated binary owner truth."""

    selected = _deduplicated_binary_learning_samples(samples)
    for sample in selected:
        _validate_model_learning_scope(model, sample)
    return evaluate_semantic_meta_model(
        model,
        (
            _meta_example_from_learning_sample(sample, group_by=group_by)
            for sample in selected
        ),
    )


def fit_semantic_meta_classifier(
    split: SemanticMetaDatasetSplit,
    *,
    parameters: SemanticMetaTrainingParameters | None = None,
) -> SemanticMetaModel:
    """Fit a deterministic L2-regularized logistic regression challenger."""

    _validate_split(split)
    selected_parameters = parameters or SemanticMetaTrainingParameters()
    _validate_training_parameters(selected_parameters)
    fit_examples = _balanced_fit_examples(
        split.training,
        maximum_good_to_defect_ratio=selected_parameters.maximum_good_to_defect_ratio,
    )
    means, scales = _feature_standardization(fit_examples)
    rows = tuple(
        tuple(
            (value - means[index]) / scales[index]
            for index, value in enumerate(example.feature_values)
        )
        for example in fit_examples
    )
    labels = tuple(float(example.is_defect) for example in fit_examples)
    weights = [0.0] * len(split.feature_names)
    intercept = 0.0
    sample_count = len(rows)
    for iteration in range(selected_parameters.iterations):
        gradient = [0.0] * len(weights)
        intercept_gradient = 0.0
        for row, label in zip(rows, labels, strict=True):
            score = intercept + math.fsum(
                weight * value for weight, value in zip(weights, row, strict=True)
            )
            error = _sigmoid(score) - label
            intercept_gradient += error
            for index, value in enumerate(row):
                gradient[index] += error * value
        rate = selected_parameters.learning_rate / math.sqrt(1.0 + (iteration / 100.0))
        intercept -= rate * (intercept_gradient / sample_count)
        for index in range(len(weights)):
            regularized = (gradient[index] / sample_count) + (
                selected_parameters.l2_strength * weights[index]
            )
            weights[index] -= rate * regularized

    provisional = SemanticMetaModel(
        feature_schema_version=split.feature_schema_version,
        feature_names=split.feature_names,
        owner_user_id=split.owner_user_id,
        profile_sha256=split.profile_sha256,
        training_dataset_sha256=split.training_dataset_sha256,
        split_manifest_sha256=split.manifest_sha256,
        fit_sample_sha256=canonical_sha256(
            [example.identity_wire() for example in fit_examples]
        ),
        fit_sample_count=len(fit_examples),
        parameters=selected_parameters,
        feature_means=means,
        feature_scales=scales,
        weights=tuple(weights),
        intercept=intercept,
        keep_threshold_micros=NO_KEEP_THRESHOLD_MICROS,
        reject_threshold_micros=NO_REJECT_THRESHOLD_MICROS,
    )
    probabilities = tuple(
        (
            example.is_defect,
            predict_semantic_meta_probability_micros(
                provisional,
                example.feature_values,
            ),
        )
        for example in split.training
    )
    keep_threshold, reject_threshold = select_semantic_meta_thresholds(
        probabilities,
        target_reject_precision_micros=selected_parameters.target_reject_precision_micros,
        target_keep_npv_micros=selected_parameters.target_keep_npv_micros,
        minimum_samples=selected_parameters.minimum_threshold_samples,
    )
    return SemanticMetaModel(
        feature_schema_version=provisional.feature_schema_version,
        feature_names=provisional.feature_names,
        owner_user_id=provisional.owner_user_id,
        profile_sha256=provisional.profile_sha256,
        training_dataset_sha256=provisional.training_dataset_sha256,
        split_manifest_sha256=provisional.split_manifest_sha256,
        fit_sample_sha256=provisional.fit_sample_sha256,
        fit_sample_count=provisional.fit_sample_count,
        parameters=provisional.parameters,
        feature_means=provisional.feature_means,
        feature_scales=provisional.feature_scales,
        weights=provisional.weights,
        intercept=provisional.intercept,
        keep_threshold_micros=keep_threshold,
        reject_threshold_micros=reject_threshold,
    )


def predict_semantic_meta_probability(
    model: SemanticMetaModel,
    feature_values: Sequence[int],
) -> float:
    _validate_model(model)
    values = _validated_feature_values(feature_values)
    score = model.intercept + math.fsum(
        weight * ((value - mean) / scale)
        for weight, value, mean, scale in zip(
            model.weights,
            values,
            model.feature_means,
            model.feature_scales,
            strict=True,
        )
    )
    return _sigmoid(score)


def predict_semantic_meta_probability_micros(
    model: SemanticMetaModel,
    feature_values: Sequence[int],
) -> int:
    return _probability_micros(predict_semantic_meta_probability(model, feature_values))


def semantic_meta_triage(
    probability_micros: int,
    *,
    keep_threshold_micros: int,
    reject_threshold_micros: int,
) -> SemanticMetaTriage:
    probability = _micros(probability_micros, label="probability")
    keep = _threshold_micros(keep_threshold_micros, label="keep threshold")
    reject = _threshold_micros(reject_threshold_micros, label="reject threshold")
    if keep >= reject:
        raise SemanticMetaClassifierError("keep threshold must be lower than reject threshold")
    if probability >= reject:
        return SemanticMetaTriage.REJECT
    if probability <= keep:
        return SemanticMetaTriage.KEEP
    return SemanticMetaTriage.REVIEW


def select_semantic_meta_thresholds(
    labeled_probabilities: Iterable[tuple[bool, int]],
    *,
    target_reject_precision_micros: int = 950_000,
    target_keep_npv_micros: int = 950_000,
    minimum_samples: int = 5,
) -> tuple[int, int]:
    """Select maximum-coverage point-estimate thresholds from training data only."""

    reject_target = _micros(target_reject_precision_micros, label="reject precision target")
    keep_target = _micros(target_keep_npv_micros, label="keep NPV target")
    if (
        isinstance(minimum_samples, bool)
        or not isinstance(minimum_samples, int)
        or minimum_samples < 1
    ):
        raise SemanticMetaClassifierError("minimum threshold samples must be positive")
    raw_rows = tuple(labeled_probabilities)
    if any(not isinstance(label, bool) for label, _ in raw_rows):
        raise SemanticMetaClassifierError("threshold labels must be booleans")
    rows = tuple(
        (label, _micros(value, label="probability")) for label, value in raw_rows
    )
    if not rows:
        raise SemanticMetaClassifierError("threshold selection requires labeled probabilities")
    if len({label for label, _ in rows}) != 2:
        raise SemanticMetaClassifierError("threshold selection requires both binary classes")

    reject_candidates: list[tuple[int, int]] = []
    for threshold in sorted({max(1, probability) for _, probability in rows}):
        rejected = tuple(label for label, probability in rows if probability >= threshold)
        precision = _ratio_micros(sum(rejected), len(rejected))
        if (
            len(rejected) >= minimum_samples
            and precision is not None
            and precision >= reject_target
        ):
            reject_candidates.append((len(rejected), threshold))
    reject_threshold = (
        min(reject_candidates, key=lambda item: (-item[0], item[1]))[1]
        if reject_candidates
        else NO_REJECT_THRESHOLD_MICROS
    )

    keep_candidates: list[tuple[int, int]] = []
    for threshold in sorted(
        {probability for _, probability in rows if probability < reject_threshold}
    ):
        kept = tuple(label for label, probability in rows if probability <= threshold)
        npv = _ratio_micros(sum(not label for label in kept), len(kept))
        if len(kept) >= minimum_samples and npv is not None and npv >= keep_target:
            keep_candidates.append((len(kept), threshold))
    keep_threshold = (
        min(keep_candidates, key=lambda item: (-item[0], -item[1]))[1]
        if keep_candidates
        else NO_KEEP_THRESHOLD_MICROS
    )
    if keep_threshold >= reject_threshold:
        keep_threshold = reject_threshold - 1
    return keep_threshold, reject_threshold


def evaluate_semantic_meta_model(
    model: SemanticMetaModel,
    examples: Iterable[SemanticMetaExample],
) -> SemanticMetaEvaluation:
    ordered = _ordered_examples(examples)
    _validate_example_collection(ordered)
    probabilities = tuple(
        predict_semantic_meta_probability_micros(model, example.feature_values)
        for example in ordered
    )
    return evaluate_semantic_meta_predictions(
        ordered,
        {
            example.sample_id: probability
            for example, probability in zip(ordered, probabilities, strict=True)
        },
        keep_threshold_micros=model.keep_threshold_micros,
        reject_threshold_micros=model.reject_threshold_micros,
        model_sha256=model.artifact_sha256,
    )


def evaluate_semantic_meta_predictions(
    examples: Iterable[SemanticMetaExample],
    probabilities_micros: Mapping[str, int],
    *,
    keep_threshold_micros: int,
    reject_threshold_micros: int,
    model_sha256: str,
) -> SemanticMetaEvaluation:
    ordered = _ordered_examples(examples)
    _validate_example_collection(ordered)
    if not ordered:
        raise SemanticMetaClassifierError("evaluation requires at least one example")
    expected_sample_ids = {example.sample_id for example in ordered}
    if set(probabilities_micros) != expected_sample_ids:
        raise SemanticMetaClassifierError("predictions do not match evaluation sample IDs")
    probabilities = tuple(
        _micros(probabilities_micros[example.sample_id], label="probability")
        for example in ordered
    )
    _sha256(model_sha256, label="model")
    predictions = tuple(
        SemanticMetaPrediction(
            sample_id=example.sample_id,
            is_defect=example.is_defect,
            probability_micros=probability,
            triage=semantic_meta_triage(
                probability,
                keep_threshold_micros=keep_threshold_micros,
                reject_threshold_micros=reject_threshold_micros,
            ),
        )
        for example, probability in zip(ordered, probabilities, strict=True)
    )
    good_count, defect_count = _class_counts(ordered)
    keep = tuple(item for item in predictions if item.triage == SemanticMetaTriage.KEEP)
    review = tuple(item for item in predictions if item.triage == SemanticMetaTriage.REVIEW)
    reject = tuple(item for item in predictions if item.triage == SemanticMetaTriage.REJECT)
    good_rejected = sum(not item.is_defect for item in reject)
    defect_rejected = len(reject) - good_rejected
    good_kept = sum(not item.is_defect for item in keep)
    defect_kept = len(keep) - good_kept
    binary_tp = sum(
        example.is_defect and probability >= 500_000
        for example, probability in zip(ordered, probabilities, strict=True)
    )
    binary_fp = sum(
        not example.is_defect and probability >= 500_000
        for example, probability in zip(ordered, probabilities, strict=True)
    )
    binary_fn = defect_count - binary_tp
    binary_tn = good_count - binary_fp
    false_reject_bounds = (
        binomial_safety_bounds(good_rejected, good_count) if good_count else None
    )
    reject_precision_bounds = (
        binomial_safety_bounds(defect_rejected, len(reject)) if reject else None
    )
    keep_npv_bounds = binomial_safety_bounds(good_kept, len(keep)) if keep else None
    evaluation_wire = {"examples": [example.identity_wire() for example in ordered]}
    return SemanticMetaEvaluation(
        evaluation_sha256=canonical_sha256(evaluation_wire),
        model_sha256=model_sha256,
        sample_count=len(ordered),
        good_count=good_count,
        defect_count=defect_count,
        keep_count=len(keep),
        review_count=len(review),
        reject_count=len(reject),
        good_rejected_count=good_rejected,
        defect_rejected_count=defect_rejected,
        good_kept_count=good_kept,
        defect_kept_count=defect_kept,
        binary_true_positive=binary_tp,
        binary_false_positive=binary_fp,
        binary_true_negative=binary_tn,
        binary_false_negative=binary_fn,
        false_reject_rate_micros=_ratio_micros(good_rejected, good_count),
        false_reject_rate_bounds=false_reject_bounds,
        reject_precision_micros=_ratio_micros(defect_rejected, len(reject)),
        reject_precision_bounds=reject_precision_bounds,
        reject_defect_recall_micros=_ratio_micros(defect_rejected, defect_count),
        keep_npv_micros=_ratio_micros(good_kept, len(keep)),
        keep_npv_bounds=keep_npv_bounds,
        kept_defect_rate_micros=_ratio_micros(defect_kept, len(keep)),
        binary_f1_micros=_ratio_micros(
            2 * binary_tp,
            (2 * binary_tp) + binary_fp + binary_fn,
        ),
        balanced_accuracy_micros=_balanced_accuracy_micros(
            true_positive=binary_tp,
            false_positive=binary_fp,
            true_negative=binary_tn,
            false_negative=binary_fn,
        ),
        automated_coverage_micros=_ratio_micros(len(keep) + len(reject), len(ordered)) or 0,
        manual_review_fraction_micros=_ratio_micros(len(review), len(ordered)) or 0,
    )


def binomial_safety_bounds(
    event_count: int,
    sample_count: int,
    *,
    alpha: float = DEFAULT_CONFIDENCE_ALPHA,
) -> BinomialSafetyBounds:
    """One-sided exact edge bounds, Wilson bounds for interior observations."""

    if (
        isinstance(event_count, bool)
        or isinstance(sample_count, bool)
        or not isinstance(event_count, int)
        or not isinstance(sample_count, int)
        or sample_count <= 0
        or not 0 <= event_count <= sample_count
    ):
        raise SemanticMetaClassifierError("binomial counts are invalid")
    if not math.isfinite(alpha) or not 0 < alpha < 0.5:
        raise SemanticMetaClassifierError("confidence alpha must be between zero and 0.5")
    if event_count == 0:
        upper = 1.0 - (alpha ** (1.0 / sample_count))
        return BinomialSafetyBounds(0, math.ceil(upper * MICROS), "exact_zero")
    if event_count == sample_count:
        lower = alpha ** (1.0 / sample_count)
        return BinomialSafetyBounds(math.floor(lower * MICROS), MICROS, "exact_all")
    # 95% one-sided normal quantile.  Alpha remains configurable for the exact
    # edges; interior bounds intentionally pin the operational 95% contract.
    if not math.isclose(alpha, DEFAULT_CONFIDENCE_ALPHA, rel_tol=0.0, abs_tol=1e-15):
        raise SemanticMetaClassifierError("interior Wilson bounds currently require alpha=0.05")
    z = 1.6448536269514722
    proportion = event_count / sample_count
    z2 = z * z
    denominator = 1.0 + (z2 / sample_count)
    center = (proportion + (z2 / (2 * sample_count))) / denominator
    margin = (
        z
        * math.sqrt(
            (proportion * (1.0 - proportion) / sample_count)
            + (z2 / (4 * sample_count * sample_count))
        )
        / denominator
    )
    return BinomialSafetyBounds(
        max(0, math.floor((center - margin) * MICROS)),
        min(MICROS, math.ceil((center + margin) * MICROS)),
        "wilson",
    )


def compare_semantic_meta_challenger(
    champion: SemanticMetaEvaluation,
    challenger: SemanticMetaEvaluation,
    *,
    policy: SemanticMetaPromotionPolicy | None = None,
) -> SemanticMetaPromotionDecision:
    """Conservatively compare two models on the exact same untouched holdout."""

    selected_policy = policy or SemanticMetaPromotionPolicy()
    _validate_promotion_policy(selected_policy)
    blockers: list[str] = []
    if champion.evaluation_sha256 != challenger.evaluation_sha256:
        blockers.append("champion and challenger were not evaluated on the same holdout")
    if champion.sample_count != challenger.sample_count:
        blockers.append("champion and challenger sample counts differ")
    false_reject_bounds = challenger.false_reject_rate_bounds
    if false_reject_bounds is None:
        blockers.append("challenger has no good-image false-reject safety bound")
    elif (
        false_reject_bounds.upper_micros
        > selected_policy.maximum_false_reject_upper_micros
    ):
        blockers.append("challenger false-reject upper bound exceeds the safety target")
    if (
        challenger.reject_precision_micros is None
        or challenger.reject_precision_micros
        < selected_policy.minimum_reject_precision_micros
    ):
        blockers.append("challenger auto-reject precision is below the safety target")
    if (
        challenger.keep_npv_micros is None
        or challenger.keep_npv_micros < selected_policy.minimum_keep_npv_micros
    ):
        blockers.append("challenger auto-keep NPV is below the safety target")
    champion_recall = champion.reject_defect_recall_micros
    challenger_recall = challenger.reject_defect_recall_micros
    if champion_recall is None or challenger_recall is None:
        blockers.append("defect recall cannot be compared")
    elif (
        challenger_recall + selected_policy.maximum_reject_recall_regression_micros
        < champion_recall
    ):
        blockers.append("challenger regresses defect recall")

    improvements: list[str] = []
    minimum = selected_policy.minimum_improvement_micros
    if challenger.automated_coverage_micros >= champion.automated_coverage_micros + minimum:
        improvements.append("automated_coverage")
    if _improved(champion.binary_f1_micros, challenger.binary_f1_micros, minimum):
        improvements.append("binary_f1")
    if _improved(
        champion.balanced_accuracy_micros,
        challenger.balanced_accuracy_micros,
        minimum,
    ):
        improvements.append("balanced_accuracy")
    if not improvements:
        blockers.append("challenger does not improve safe coverage or a quality metric")
    return SemanticMetaPromotionDecision(
        promote=not blockers,
        blockers=tuple(blockers),
        improvements=tuple(improvements),
    )


def deserialize_semantic_meta_model(payload: bytes) -> SemanticMetaModel:
    """Load an artifact only when its canonical digest and pinned schema match."""

    try:
        value: Any = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SemanticMetaClassifierError("meta-classifier artifact is not valid JSON") from exc
    root = _mapping(value, label="artifact envelope")
    artifact = _mapping(root.get("artifact"), label="artifact")
    digest = root.get("artifact_sha256")
    if not isinstance(digest, str) or digest != canonical_sha256(artifact):
        raise SemanticMetaClassifierError("meta-classifier artifact digest is invalid")
    if artifact.get("schema_version") != SEMANTIC_META_MODEL_SCHEMA_VERSION:
        raise SemanticMetaClassifierError("meta-classifier artifact schema is unsupported")
    parameters_wire = _mapping(artifact.get("parameters"), label="training parameters")
    model = SemanticMetaModel(
        feature_schema_version=_string(
            artifact.get("feature_schema_version"), label="feature schema version"
        ),
        feature_names=_string_tuple(artifact.get("feature_names"), label="feature names"),
        owner_user_id=_string(artifact.get("owner_user_id"), label="owner user ID"),
        profile_sha256=_string(artifact.get("profile_sha256"), label="semantic profile digest"),
        training_dataset_sha256=_string(
            artifact.get("training_dataset_sha256"), label="training dataset digest"
        ),
        split_manifest_sha256=_string(
            artifact.get("split_manifest_sha256"), label="split manifest digest"
        ),
        fit_sample_sha256=_string(
            artifact.get("fit_sample_sha256"), label="fit sample digest"
        ),
        fit_sample_count=_integer(artifact.get("fit_sample_count"), label="fit sample count"),
        parameters=SemanticMetaTrainingParameters(
            iterations=_integer(parameters_wire.get("iterations"), label="iterations"),
            learning_rate=_hex_float(
                parameters_wire.get("learning_rate_hex"), label="learning rate"
            ),
            l2_strength=_hex_float(
                parameters_wire.get("l2_strength_hex"), label="L2 strength"
            ),
            maximum_good_to_defect_ratio=_integer(
                parameters_wire.get("maximum_good_to_defect_ratio"),
                label="maximum good-to-defect ratio",
            ),
            minimum_threshold_samples=_integer(
                parameters_wire.get("minimum_threshold_samples"),
                label="minimum threshold samples",
            ),
            target_reject_precision_micros=_integer(
                parameters_wire.get("target_reject_precision_micros"),
                label="reject precision target",
            ),
            target_keep_npv_micros=_integer(
                parameters_wire.get("target_keep_npv_micros"),
                label="keep NPV target",
            ),
        ),
        feature_means=_hex_float_tuple(
            artifact.get("feature_means_hex"), label="feature means"
        ),
        feature_scales=_hex_float_tuple(
            artifact.get("feature_scales_hex"), label="feature scales"
        ),
        weights=_hex_float_tuple(artifact.get("weights_hex"), label="weights"),
        intercept=_hex_float(artifact.get("intercept_hex"), label="intercept"),
        keep_threshold_micros=_integer(
            artifact.get("keep_threshold_micros"), label="keep threshold"
        ),
        reject_threshold_micros=_integer(
            artifact.get("reject_threshold_micros"), label="reject threshold"
        ),
    )
    _validate_model(model)
    if model.serialize() != payload:
        raise SemanticMetaClassifierError("meta-classifier artifact is not canonical")
    return model


def _balanced_fit_examples(
    examples: tuple[SemanticMetaExample, ...],
    *,
    maximum_good_to_defect_ratio: int,
) -> tuple[SemanticMetaExample, ...]:
    defects = tuple(example for example in examples if example.is_defect)
    good = tuple(example for example in examples if not example.is_defect)
    maximum_good = len(defects) * maximum_good_to_defect_ratio
    if len(good) > maximum_good:
        # The content digest makes the cap independent of query or insertion order.
        good = tuple(
            sorted(good, key=lambda example: (example.asset_sha256, example.sample_id))[
                :maximum_good
            ]
        )
    return _ordered_examples((*defects, *good))


def _deduplicated_binary_learning_samples(
    samples: Iterable[SemanticLearningSample],
) -> tuple[SemanticLearningSample, ...]:
    raw = tuple(samples)
    _learning_scope(raw)
    by_content: dict[str, list[SemanticLearningSample]] = defaultdict(list)
    for sample in raw:
        _sha256(sample.asset_sha256, label="asset")
        by_content[sample.asset_sha256].append(sample)
    selected: list[SemanticLearningSample] = []
    for asset_sha256 in sorted(by_content):
        content = by_content[asset_sha256]
        strongest_rank = max(_learning_source_rank(sample.source) for sample in content)
        strongest = tuple(
            sample
            for sample in content
            if _learning_source_rank(sample.source) == strongest_rank
        )
        if len({sample.ground_truth for sample in strongest}) != 1:
            continue
        chosen = min(
            strongest,
            key=lambda sample: (
                -int(sample.owner_issue_code is not None),
                _as_utc(sample.labeled_at),
                str(sample.feedback_id),
            ),
        )
        if chosen.ground_truth in (
            SemanticGroundTruth.ANATOMY_GOOD,
            SemanticGroundTruth.ANATOMY_DEFECT,
        ):
            selected.append(chosen)
    result = tuple(
        sorted(
            selected,
            key=lambda sample: (
                _as_utc(sample.generated_at),
                sample.asset_sha256,
                str(sample.feedback_id),
            ),
        )
    )
    if not result:
        raise SemanticMetaClassifierError("no binary semantic learning samples are available")
    return result


def _learning_scope(
    samples: Sequence[SemanticLearningSample],
) -> tuple[UUID, str]:
    if not samples:
        raise SemanticMetaClassifierError("semantic learning samples are required")
    owner_ids = {sample.feedback_by_user_id for sample in samples}
    profiles = {sample.profile_sha256 for sample in samples}
    if len(owner_ids) != 1:
        raise SemanticMetaClassifierError("semantic learning samples mix owners")
    if len(profiles) != 1:
        raise SemanticMetaClassifierError("semantic learning samples mix profiles")
    owner_user_id = next(iter(owner_ids))
    profile_sha256 = next(iter(profiles))
    _sha256(profile_sha256, label="semantic profile")
    return owner_user_id, profile_sha256


def _meta_example_from_learning_sample(
    sample: SemanticLearningSample,
    *,
    group_by: SemanticMetaGroupBy,
) -> SemanticMetaExample:
    if sample.ground_truth not in (
        SemanticGroundTruth.ANATOMY_GOOD,
        SemanticGroundTruth.ANATOMY_DEFECT,
    ):
        raise SemanticMetaClassifierError("meta-classifier examples require binary truth")
    if group_by == SemanticMetaGroupBy.RELEASE_SET:
        group_key = f"release:{sample.release_id}"
    elif group_by == SemanticMetaGroupBy.GENERATION_BATCH:
        group_key = (
            f"generation:{sample.generation_job_id}"
            if sample.generation_job_id is not None
            else f"release:{sample.release_id}"
        )
    elif group_by == SemanticMetaGroupBy.ASSET_CONTENT:
        group_key = f"asset:{sample.asset_sha256}"
    else:
        raise SemanticMetaClassifierError("semantic learning group mode is unsupported")
    return SemanticMetaExample(
        sample_id=str(sample.feedback_id),
        asset_sha256=sample.asset_sha256,
        group_key=group_key,
        occurred_at=sample.generated_at,
        is_defect=sample.ground_truth == SemanticGroundTruth.ANATOMY_DEFECT,
        feature_values=semantic_meta_feature_values(sample),
    )


def _learning_source_rank(source: str) -> int:
    return 2 if source == SOURCE_EXPLICIT else 1


def _validate_model_learning_scope(
    model: SemanticMetaModel,
    sample: SemanticLearningSample,
) -> None:
    if str(sample.feedback_by_user_id) != model.owner_user_id:
        raise SemanticMetaClassifierError("semantic learning sample belongs to another owner")
    if sample.profile_sha256 != model.profile_sha256:
        raise SemanticMetaClassifierError("semantic learning sample uses another profile")


def _feature_standardization(
    examples: tuple[SemanticMetaExample, ...],
) -> tuple[tuple[float, ...], tuple[float, ...]]:
    columns = tuple(zip(*(example.feature_values for example in examples), strict=True))
    means = tuple(math.fsum(column) / len(column) for column in columns)
    variances = tuple(
        math.fsum((value - mean) ** 2 for value in column) / len(column)
        for column, mean in zip(columns, means, strict=True)
    )
    scales = tuple(math.sqrt(value) if value > 1e-24 else 1.0 for value in variances)
    return means, scales


def _validate_split(split: SemanticMetaDatasetSplit) -> None:
    if split.feature_schema_version != SEMANTIC_META_FEATURE_SCHEMA_VERSION:
        raise SemanticMetaClassifierError("feature schema version is unsupported")
    if split.feature_names != SEMANTIC_META_FEATURE_NAMES:
        raise SemanticMetaClassifierError("feature names do not match the pinned schema")
    try:
        UUID(split.owner_user_id)
    except (ValueError, AttributeError, TypeError) as exc:
        raise SemanticMetaClassifierError("owner user ID is invalid") from exc
    _sha256(split.profile_sha256, label="semantic profile")
    if not split.training or not split.holdout:
        raise SemanticMetaClassifierError("training and holdout must both be non-empty")
    _validate_example_collection(split.training)
    _validate_example_collection(split.holdout)
    for name, examples in (("training", split.training), ("holdout", split.holdout)):
        if len({example.is_defect for example in examples}) != 2:
            raise SemanticMetaClassifierError(f"{name} split requires both binary classes")
    training_assets = {example.asset_sha256 for example in split.training}
    holdout_assets = {example.asset_sha256 for example in split.holdout}
    if training_assets & holdout_assets:
        raise SemanticMetaClassifierError("content digest crosses training and holdout")
    training_sample_ids = {example.sample_id for example in split.training}
    holdout_sample_ids = {example.sample_id for example in split.holdout}
    if training_sample_ids & holdout_sample_ids:
        raise SemanticMetaClassifierError("sample ID crosses training and holdout")
    training_groups = {example.group_key for example in split.training}
    holdout_groups = {example.group_key for example in split.holdout}
    if training_groups & holdout_groups:
        raise SemanticMetaClassifierError("group crosses training and holdout")
    if max(_as_utc(example.occurred_at) for example in split.training) > min(
        _as_utc(example.occurred_at) for example in split.holdout
    ):
        raise SemanticMetaClassifierError("holdout is not a chronological tail")


def _validate_example_collection(examples: tuple[SemanticMetaExample, ...]) -> None:
    sample_ids: set[str] = set()
    asset_digests: set[str] = set()
    for example in examples:
        if not example.sample_id or len(example.sample_id) > 256:
            raise SemanticMetaClassifierError("sample ID is invalid")
        if not example.group_key or len(example.group_key) > 512:
            raise SemanticMetaClassifierError("group key is invalid")
        _sha256(example.asset_sha256, label="asset")
        if example.occurred_at.tzinfo is None or example.occurred_at.utcoffset() is None:
            raise SemanticMetaClassifierError("example timestamp must be timezone-aware")
        if not isinstance(example.is_defect, bool):
            raise SemanticMetaClassifierError("example truth must be boolean")
        _validated_feature_values(example.feature_values)
        if example.sample_id in sample_ids:
            raise SemanticMetaClassifierError("sample ID is duplicated")
        if example.asset_sha256 in asset_digests:
            raise SemanticMetaClassifierError("asset content is duplicated")
        sample_ids.add(example.sample_id)
        asset_digests.add(example.asset_sha256)


def _validate_training_parameters(parameters: SemanticMetaTrainingParameters) -> None:
    if (
        isinstance(parameters.iterations, bool)
        or not isinstance(parameters.iterations, int)
        or not 1 <= parameters.iterations <= 100_000
    ):
        raise SemanticMetaClassifierError("training iterations are invalid")
    if not math.isfinite(parameters.learning_rate) or not 0 < parameters.learning_rate <= 1:
        raise SemanticMetaClassifierError("learning rate is invalid")
    if not math.isfinite(parameters.l2_strength) or not 0 <= parameters.l2_strength <= 100:
        raise SemanticMetaClassifierError("L2 strength is invalid")
    if (
        isinstance(parameters.maximum_good_to_defect_ratio, bool)
        or not isinstance(parameters.maximum_good_to_defect_ratio, int)
        or not 1 <= parameters.maximum_good_to_defect_ratio <= 100
    ):
        raise SemanticMetaClassifierError("maximum good-to-defect ratio is invalid")
    if (
        isinstance(parameters.minimum_threshold_samples, bool)
        or not isinstance(parameters.minimum_threshold_samples, int)
        or not 1 <= parameters.minimum_threshold_samples <= 1_000_000
    ):
        raise SemanticMetaClassifierError("minimum threshold samples is invalid")
    _micros(parameters.target_reject_precision_micros, label="reject precision target")
    _micros(parameters.target_keep_npv_micros, label="keep NPV target")


def _validate_model(model: SemanticMetaModel) -> None:
    if model.feature_schema_version != SEMANTIC_META_FEATURE_SCHEMA_VERSION:
        raise SemanticMetaClassifierError("model feature schema is unsupported")
    if model.feature_names != SEMANTIC_META_FEATURE_NAMES:
        raise SemanticMetaClassifierError("model feature names do not match the pinned schema")
    try:
        UUID(model.owner_user_id)
    except (ValueError, AttributeError, TypeError) as exc:
        raise SemanticMetaClassifierError("model owner user ID is invalid") from exc
    _sha256(model.profile_sha256, label="semantic profile")
    feature_count = len(SEMANTIC_META_FEATURE_NAMES)
    if not (
        len(model.feature_means)
        == len(model.feature_scales)
        == len(model.weights)
        == feature_count
    ):
        raise SemanticMetaClassifierError("model coefficient dimensions are invalid")
    _sha256(model.training_dataset_sha256, label="training dataset")
    _sha256(model.split_manifest_sha256, label="split manifest")
    _sha256(model.fit_sample_sha256, label="fit sample")
    if (
        isinstance(model.fit_sample_count, bool)
        or not isinstance(model.fit_sample_count, int)
        or model.fit_sample_count < 2
    ):
        raise SemanticMetaClassifierError("fit sample count is invalid")
    _validate_training_parameters(model.parameters)
    values = (*model.feature_means, *model.feature_scales, *model.weights, model.intercept)
    if any(not math.isfinite(value) for value in values):
        raise SemanticMetaClassifierError("model contains non-finite coefficients")
    if any(value <= 0 for value in model.feature_scales):
        raise SemanticMetaClassifierError("model feature scales must be positive")
    keep = _threshold_micros(model.keep_threshold_micros, label="keep threshold")
    reject = _threshold_micros(model.reject_threshold_micros, label="reject threshold")
    if keep >= reject:
        raise SemanticMetaClassifierError("model thresholds overlap")


def _validate_promotion_policy(policy: SemanticMetaPromotionPolicy) -> None:
    _micros(policy.maximum_false_reject_upper_micros, label="false reject upper target")
    _micros(policy.minimum_reject_precision_micros, label="reject precision target")
    _micros(policy.minimum_keep_npv_micros, label="keep NPV target")
    _micros(
        policy.maximum_reject_recall_regression_micros,
        label="recall regression allowance",
    )
    if (
        isinstance(policy.minimum_improvement_micros, bool)
        or not isinstance(policy.minimum_improvement_micros, int)
        or not 1 <= policy.minimum_improvement_micros <= MICROS
    ):
        raise SemanticMetaClassifierError("minimum promotion improvement is invalid")


def _ordered_examples(
    examples: Iterable[SemanticMetaExample],
) -> tuple[SemanticMetaExample, ...]:
    return tuple(
        sorted(
            examples,
            key=lambda example: (
                _as_utc(example.occurred_at),
                example.group_key,
                example.asset_sha256,
                example.sample_id,
            ),
        )
    )


def _validated_feature_values(values: Sequence[int]) -> tuple[int, ...]:
    result = tuple(values)
    if len(result) != len(SEMANTIC_META_FEATURE_NAMES):
        raise SemanticMetaClassifierError("feature vector length does not match pinned schema")
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in result):
        raise SemanticMetaClassifierError("feature values must be non-negative integers")
    return result


def _class_counts(examples: tuple[SemanticMetaExample, ...]) -> tuple[int, int]:
    defect = sum(example.is_defect for example in examples)
    return len(examples) - defect, defect


def _balanced_accuracy_micros(
    *,
    true_positive: int,
    false_positive: int,
    true_negative: int,
    false_negative: int,
) -> int | None:
    recall = _ratio_micros(true_positive, true_positive + false_negative)
    specificity = _ratio_micros(true_negative, true_negative + false_positive)
    if recall is None or specificity is None:
        return None
    return round((recall + specificity) / 2)


def _improved(previous: int | None, candidate: int | None, minimum: int) -> bool:
    return previous is not None and candidate is not None and candidate >= previous + minimum


def _ratio_micros(numerator: int, denominator: int) -> int | None:
    if denominator <= 0:
        return None
    return round((numerator / denominator) * MICROS)


def _probability_micros(value: float) -> int:
    return min(MICROS, max(0, round(value * MICROS)))


def _micros(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MICROS:
        raise SemanticMetaClassifierError(f"{label} is invalid")
    return value


def _threshold_micros(value: object, *, label: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not NO_KEEP_THRESHOLD_MICROS <= value <= NO_REJECT_THRESHOLD_MICROS
    ):
        raise SemanticMetaClassifierError(f"{label} is invalid")
    return value


def _sigmoid(value: float) -> float:
    if value >= 0:
        return 1.0 / (1.0 + math.exp(-value))
    exponential = math.exp(value)
    return exponential / (1.0 + exponential)


def _sha256(value: str, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise SemanticMetaClassifierError(f"{label} digest is invalid")
    return value


def _as_utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


def _utc_wire(value: datetime) -> str:
    return _as_utc(value).isoformat().replace("+00:00", "Z")


def _mapping(value: object, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise SemanticMetaClassifierError(f"{label} is invalid")
    return value


def _string(value: object, *, label: str) -> str:
    if not isinstance(value, str):
        raise SemanticMetaClassifierError(f"{label} is invalid")
    return value


def _integer(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SemanticMetaClassifierError(f"{label} is invalid")
    return value


def _string_tuple(value: object, *, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise SemanticMetaClassifierError(f"{label} is invalid")
    return tuple(value)


def _hex_float(value: object, *, label: str) -> float:
    if not isinstance(value, str):
        raise SemanticMetaClassifierError(f"{label} is invalid")
    try:
        result = float.fromhex(value)
    except ValueError as exc:
        raise SemanticMetaClassifierError(f"{label} is invalid") from exc
    if not math.isfinite(result):
        raise SemanticMetaClassifierError(f"{label} is invalid")
    return result


def _hex_float_tuple(value: object, *, label: str) -> tuple[float, ...]:
    if not isinstance(value, list):
        raise SemanticMetaClassifierError(f"{label} is invalid")
    return tuple(_hex_float(item, label=label) for item in value)
