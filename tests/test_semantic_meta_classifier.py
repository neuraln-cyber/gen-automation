from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid5

import pytest

from gen_automation.domain.enums import SemanticGroundTruth, SemanticVerdict
from gen_automation.services.semantic_learning_readiness import (
    SEMANTIC_META_FEATURE_NAMES,
    SOURCE_EXPLICIT,
    SemanticLearningSample,
)
from gen_automation.services.semantic_meta_classifier import (
    NO_KEEP_THRESHOLD_MICROS,
    NO_REJECT_THRESHOLD_MICROS,
    SemanticMetaClassifierError,
    SemanticMetaExample,
    SemanticMetaGroupBy,
    SemanticMetaPromotionPolicy,
    SemanticMetaTrainingParameters,
    SemanticMetaTriage,
    binomial_safety_bounds,
    build_semantic_meta_dataset_split,
    build_semantic_meta_split_from_learning_samples,
    chronological_semantic_meta_split,
    compare_semantic_meta_challenger,
    deserialize_semantic_meta_model,
    evaluate_semantic_meta_model,
    evaluate_semantic_meta_model_on_learning_samples,
    evaluate_semantic_meta_predictions,
    fit_semantic_meta_classifier,
    fit_semantic_meta_classifier_from_learning_samples,
    predict_semantic_meta_probability_micros,
    score_semantic_learning_sample,
    select_semantic_meta_thresholds,
    semantic_meta_triage,
)

OWNER_ID = UUID("12345678-1234-5678-1234-567812345678")
OTHER_OWNER_ID = UUID("22345678-1234-5678-1234-567812345678")
PROFILE_SHA256 = "a" * 64
BASE_TIME = datetime(2026, 1, 1, tzinfo=UTC)
NAMESPACE = UUID("32345678-1234-5678-1234-567812345678")


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _features(*, defect: bool, variant: int = 0) -> tuple[int, ...]:
    values = [0] * len(SEMANTIC_META_FEATURE_NAMES)
    values[0 if not defect else 2] = 1
    values[3] = (850_000 + variant) if defect else (100_000 + variant)
    values[4] = 2 if defect else 0
    values[5] = (900_000 + variant) if defect else 0
    values[6] = 1 if defect else 0
    if defect:
        values[7] = 1
        values[8] = 900_000
    return tuple(values)


def _example(
    name: str,
    *,
    defect: bool,
    group: str,
    day: int,
) -> SemanticMetaExample:
    return SemanticMetaExample(
        sample_id=name,
        asset_sha256=_sha(name),
        group_key=group,
        occurred_at=BASE_TIME + timedelta(days=day),
        is_defect=defect,
        feature_values=_features(defect=defect, variant=day),
    )


def _split_examples() -> tuple[tuple[SemanticMetaExample, ...], tuple[SemanticMetaExample, ...]]:
    training = tuple(
        _example(
            f"train-{index}",
            defect=index % 2 == 0,
            group=f"train-group-{index // 6}",
            day=index // 6,
        )
        for index in range(24)
    )
    holdout = tuple(
        _example(
            f"holdout-{index}",
            defect=index % 2 == 0,
            group=f"holdout-group-{index // 6}",
            day=10 + (index // 6),
        )
        for index in range(12)
    )
    return training, holdout


def _parameters() -> SemanticMetaTrainingParameters:
    return SemanticMetaTrainingParameters(
        iterations=600,
        learning_rate=0.2,
        l2_strength=0.01,
        minimum_threshold_samples=2,
    )


def _learning_sample(
    name: str,
    *,
    defect: bool,
    release: str,
    day: int,
    owner_id: UUID = OWNER_ID,
) -> SemanticLearningSample:
    item_id = uuid5(NAMESPACE, name)
    release_id = uuid5(NAMESPACE, release)
    return SemanticLearningSample(
        feedback_id=item_id,
        assessment_id=uuid5(NAMESPACE, f"assessment-{name}"),
        asset_id=uuid5(NAMESPACE, f"asset-{name}"),
        feedback_by_user_id=owner_id,
        profile_sha256=PROFILE_SHA256,
        asset_sha256=_sha(name),
        ground_truth=(
            SemanticGroundTruth.ANATOMY_DEFECT
            if defect
            else SemanticGroundTruth.ANATOMY_GOOD
        ),
        owner_issue_code=None,
        source=SOURCE_EXPLICIT,
        verdict=SemanticVerdict.SEVERE if defect else SemanticVerdict.PASS,
        confidence_micros=900_000 if defect else 100_000,
        predicted_issues=(),
        release_id=release_id,
        generation_job_id=uuid5(NAMESPACE, f"job-{release}"),
        generated_at=BASE_TIME + timedelta(days=day),
        labeled_at=BASE_TIME + timedelta(days=day, hours=1),
        completed_review=True,
    )


def test_chronological_split_is_deterministic_and_keeps_groups_whole() -> None:
    examples = tuple(
        _example(
            f"sample-{group}-{index}",
            defect=index % 2 == 0,
            group=f"group-{group}",
            day=group,
        )
        for group in range(4)
        for index in range(4)
    )

    first = chronological_semantic_meta_split(
        reversed(examples),
        owner_user_id=OWNER_ID,
        profile_sha256=PROFILE_SHA256,
        holdout_fraction=0.5,
    )
    second = chronological_semantic_meta_split(
        examples,
        owner_user_id=OWNER_ID,
        profile_sha256=PROFILE_SHA256,
        holdout_fraction=0.5,
    )

    assert first.dataset_sha256 == second.dataset_sha256
    assert {row.group_key for row in first.training} == {"group-0", "group-1"}
    assert {row.group_key for row in first.holdout} == {"group-2", "group-3"}
    assert first.manifest_sha256 != first.dataset_sha256


def test_manual_split_rejects_group_or_content_leakage() -> None:
    training, holdout = _split_examples()
    leaked = SemanticMetaExample(
        sample_id="leaked",
        asset_sha256=training[0].asset_sha256,
        group_key=holdout[0].group_key,
        occurred_at=holdout[0].occurred_at,
        is_defect=False,
        feature_values=_features(defect=False),
    )

    with pytest.raises(SemanticMetaClassifierError, match="content digest crosses"):
        build_semantic_meta_dataset_split(
            training=training,
            holdout=(*holdout, leaked),
            owner_user_id=OWNER_ID,
            profile_sha256=PROFILE_SHA256,
        )

    crossed_group = SemanticMetaExample(
        sample_id="crossed-group",
        asset_sha256=_sha("crossed-group"),
        group_key=training[0].group_key,
        occurred_at=holdout[0].occurred_at,
        is_defect=False,
        feature_values=_features(defect=False),
    )
    with pytest.raises(SemanticMetaClassifierError, match="group crosses"):
        build_semantic_meta_dataset_split(
            training=training,
            holdout=(*holdout, crossed_group),
            owner_user_id=OWNER_ID,
            profile_sha256=PROFILE_SHA256,
        )


def test_training_serialization_and_inference_are_byte_reproducible() -> None:
    training, holdout = _split_examples()
    split = build_semantic_meta_dataset_split(
        training=reversed(training),
        holdout=reversed(holdout),
        owner_user_id=OWNER_ID,
        profile_sha256=PROFILE_SHA256,
    )

    first = fit_semantic_meta_classifier(split, parameters=_parameters())
    second = fit_semantic_meta_classifier(split, parameters=_parameters())

    assert first.serialize() == second.serialize()
    assert first.artifact_sha256 == second.artifact_sha256
    restored = deserialize_semantic_meta_model(first.serialize())
    assert restored == first
    good_probability = predict_semantic_meta_probability_micros(
        restored, _features(defect=False)
    )
    defect_probability = predict_semantic_meta_probability_micros(
        restored, _features(defect=True)
    )
    assert good_probability < defect_probability
    assert semantic_meta_triage(
        good_probability,
        keep_threshold_micros=restored.keep_threshold_micros,
        reject_threshold_micros=restored.reject_threshold_micros,
    ) == SemanticMetaTriage.KEEP
    assert semantic_meta_triage(
        defect_probability,
        keep_threshold_micros=restored.keep_threshold_micros,
        reject_threshold_micros=restored.reject_threshold_micros,
    ) == SemanticMetaTriage.REJECT

    envelope = json.loads(first.serialize())
    envelope["artifact"]["weights_hex"][0] = float(123).hex()
    with pytest.raises(SemanticMetaClassifierError, match="digest"):
        deserialize_semantic_meta_model(json.dumps(envelope).encode())


def test_evaluation_reports_operational_and_binary_metrics() -> None:
    training, holdout = _split_examples()
    split = build_semantic_meta_dataset_split(
        training=training,
        holdout=holdout,
        owner_user_id=OWNER_ID,
        profile_sha256=PROFILE_SHA256,
    )
    model = fit_semantic_meta_classifier(split, parameters=_parameters())

    metrics = evaluate_semantic_meta_model(model, holdout)

    assert metrics.sample_count == 12
    assert metrics.good_count == metrics.defect_count == 6
    assert metrics.good_rejected_count == 0
    assert metrics.defect_kept_count == 0
    assert metrics.reject_precision_micros == 1_000_000
    assert metrics.keep_npv_micros == 1_000_000
    assert metrics.binary_f1_micros == 1_000_000
    assert metrics.balanced_accuracy_micros == 1_000_000


def test_unsafe_threshold_fit_abstains_instead_of_leaking_edge_probabilities() -> None:
    keep, reject = select_semantic_meta_thresholds(
        (
            *((False, 900_000 + index) for index in range(5)),
            *((True, 100_000 + index) for index in range(5)),
        ),
        minimum_samples=2,
    )

    assert keep == NO_KEEP_THRESHOLD_MICROS
    assert reject == NO_REJECT_THRESHOLD_MICROS
    assert semantic_meta_triage(
        0,
        keep_threshold_micros=keep,
        reject_threshold_micros=reject,
    ) == SemanticMetaTriage.REVIEW
    assert semantic_meta_triage(
        1_000_000,
        keep_threshold_micros=keep,
        reject_threshold_micros=reject,
    ) == SemanticMetaTriage.REVIEW


def test_binomial_bounds_use_exact_edges_and_wilson_interior() -> None:
    zero = binomial_safety_bounds(0, 150)
    all_events = binomial_safety_bounds(150, 150)
    interior = binomial_safety_bounds(2, 150)

    assert zero.method == "exact_zero"
    assert 19_000 <= zero.upper_micros <= 20_000
    assert all_events.method == "exact_all"
    assert all_events.lower_micros >= 980_000
    assert interior.method == "wilson"
    assert interior.lower_micros < round((2 / 150) * 1_000_000) < interior.upper_micros


def test_challenger_promotes_only_on_same_safe_holdout_with_improvement() -> None:
    examples = tuple(
        _example(
            f"good-{index}",
            defect=False,
            group=f"g-{index}",
            day=0,
        )
        for index in range(200)
    ) + tuple(
        _example(
            f"defect-{index}",
            defect=True,
            group=f"d-{index}",
            day=0,
        )
        for index in range(100)
    )
    champion_probabilities = {
        example.sample_id: (
            300_000
            if not example.is_defect
            else (900_000 if int(example.sample_id.removeprefix("defect-")) < 50 else 400_000)
        )
        for example in examples
    }
    challenger_probabilities = {
        example.sample_id: 950_000 if example.is_defect else 50_000
        for example in examples
    }
    champion = evaluate_semantic_meta_predictions(
        examples,
        champion_probabilities,
        keep_threshold_micros=100_000,
        reject_threshold_micros=800_000,
        model_sha256="b" * 64,
    )
    challenger = evaluate_semantic_meta_predictions(
        reversed(examples),
        challenger_probabilities,
        keep_threshold_micros=100_000,
        reject_threshold_micros=800_000,
        model_sha256="c" * 64,
    )

    decision = compare_semantic_meta_challenger(champion, challenger)

    assert decision.promote
    assert decision.blockers == ()
    assert "automated_coverage" in decision.improvements

    unsafe_probabilities = dict(challenger_probabilities)
    unsafe_probabilities["good-0"] = 950_000
    unsafe = evaluate_semantic_meta_predictions(
        examples,
        unsafe_probabilities,
        keep_threshold_micros=100_000,
        reject_threshold_micros=800_000,
        model_sha256="d" * 64,
    )
    blocked = compare_semantic_meta_challenger(champion, unsafe)
    assert not blocked.promote
    assert any("false-reject upper bound" in blocker for blocker in blocked.blockers)


def test_clean_learning_sample_apis_fit_score_and_evaluate() -> None:
    training = tuple(
        _learning_sample(
            f"learning-train-{index}",
            defect=index % 2 == 0,
            release=f"training-release-{index // 6}",
            day=index // 6,
        )
        for index in range(24)
    )
    holdout = tuple(
        _learning_sample(
            f"learning-holdout-{index}",
            defect=index % 2 == 0,
            release=f"holdout-release-{index // 6}",
            day=10 + (index // 6),
        )
        for index in range(12)
    )
    split = build_semantic_meta_split_from_learning_samples(
        training=training,
        holdout=holdout,
        group_by=SemanticMetaGroupBy.RELEASE_SET,
    )
    model = fit_semantic_meta_classifier_from_learning_samples(
        training=reversed(training),
        holdout=reversed(holdout),
        parameters=_parameters(),
    )

    assert split.owner_user_id == str(OWNER_ID)
    assert model.training_dataset_sha256 == split.training_dataset_sha256
    assert score_semantic_learning_sample(model, holdout[0]).triage == SemanticMetaTriage.REJECT
    metrics = evaluate_semantic_meta_model_on_learning_samples(model, holdout)
    assert metrics.balanced_accuracy_micros == 1_000_000

    foreign = _learning_sample(
        "foreign",
        defect=True,
        release="foreign-release",
        day=20,
        owner_id=OTHER_OWNER_ID,
    )
    with pytest.raises(SemanticMetaClassifierError, match="another owner"):
        score_semantic_learning_sample(model, foreign)


def test_promotion_policy_can_be_tightened_without_changing_model_artifact() -> None:
    examples = tuple(
        _example(f"p-{index}", defect=index >= 200, group=f"p-{index}", day=0)
        for index in range(300)
    )
    probabilities = {
        example.sample_id: 950_000 if example.is_defect else 50_000
        for example in examples
    }
    evaluation = evaluate_semantic_meta_predictions(
        examples,
        probabilities,
        keep_threshold_micros=100_000,
        reject_threshold_micros=800_000,
        model_sha256="e" * 64,
    )
    decision = compare_semantic_meta_challenger(
        evaluation,
        evaluation,
        policy=SemanticMetaPromotionPolicy(minimum_improvement_micros=1),
    )

    assert not decision.promote
    assert decision.blockers[-1] == (
        "challenger does not improve safe coverage or a quality metric"
    )
