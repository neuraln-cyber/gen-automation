from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest

from gen_automation.db.models import (
    SemanticAnatomyFeedback,
    SemanticAssessment,
    SemanticCalibrationArtifact,
)
from gen_automation.domain.enums import (
    SemanticFeedbackAgreement,
    SemanticGroundTruth,
    SemanticIssueCode,
    SemanticVerdict,
)
from gen_automation.services import semantic_feedback as feedback_service
from gen_automation.services.semantic_feedback import (
    LEGACY_SEMANTIC_CALIBRATION_SCHEMA_VERSION,
    SEMANTIC_CALIBRATION_FOLD_COUNT,
    SEMANTIC_CALIBRATION_SCHEMA_VERSION,
    SEMANTIC_INFERRED_ANATOMY_REJECT_NOTE,
    SEMANTIC_INFERRED_REVIEW_ACCEPT_NOTE,
    SemanticConfusionCounts,
    SemanticValidationMetrics,
    build_semantic_calibration_report,
    load_effective_semantic_threshold_micros,
)

_NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
_PROFILE = "a" * 64


class _Rows:
    def __init__(self, rows: list[tuple[SemanticAnatomyFeedback, SemanticAssessment]]) -> None:
        self._rows = rows

    def all(self) -> list[tuple[SemanticAnatomyFeedback, SemanticAssessment]]:
        return self._rows


class _ReportSession:
    def __init__(self, rows: list[tuple[SemanticAnatomyFeedback, SemanticAssessment]]) -> None:
        self._rows = rows

    async def execute(self, _statement: object) -> _Rows:
        return _Rows(self._rows)


def _sha_for_fold(fold: int, *, offset: int = 0) -> str:
    match_index = 0
    candidate = 1
    while True:
        digest = f"{candidate:064x}"
        if feedback_service._calibration_fold(digest) == fold:
            if match_index == offset:
                return digest
            match_index += 1
        candidate += 1


def _sample(
    number: int,
    *,
    asset_sha256: str,
    ground_truth: SemanticGroundTruth,
    confidence_micros: int,
) -> feedback_service._CalibrationSample:
    return feedback_service._CalibrationSample(
        feedback_id=UUID(int=number * 3),
        assessment_id=UUID(int=(number * 3) + 1),
        asset_id=UUID(int=(number * 3) + 2),
        asset_sha256=asset_sha256,
        agreement=SemanticFeedbackAgreement.CORRECT,
        ground_truth=ground_truth,
        issue_code=(
            SemanticIssueCode.IMPLAUSIBLE_PROPORTION
            if ground_truth == SemanticGroundTruth.ANATOMY_DEFECT
            else None
        ),
        verdict=SemanticVerdict.SEVERE,
        confidence_micros=confidence_micros,
        response_sha256=f"{number + 10_000:064x}",
        source="explicit",
    )


def _validation_metrics(
    *,
    true_positive: int,
    false_positive: int,
    true_negative: int,
    false_negative: int,
) -> SemanticValidationMetrics:
    return SemanticValidationMetrics(
        sample_count=true_positive + false_positive + true_negative + false_negative,
        true_positive=true_positive,
        false_positive=false_positive,
        true_negative=true_negative,
        false_negative=false_negative,
    )


def test_grouped_oof_never_trains_on_validation_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    samples: list[feedback_service._CalibrationSample] = []
    for fold in range(SEMANTIC_CALIBRATION_FOLD_COUNT):
        samples.extend(
            (
                _sample(
                    (fold * 10) + 1,
                    asset_sha256=_sha_for_fold(fold, offset=0),
                    ground_truth=SemanticGroundTruth.ANATOMY_GOOD,
                    confidence_micros=850_000,
                ),
                _sample(
                    (fold * 10) + 2,
                    asset_sha256=_sha_for_fold(fold, offset=1),
                    ground_truth=SemanticGroundTruth.ANATOMY_DEFECT,
                    confidence_micros=950_000,
                ),
            )
        )
    # A duplicate-content sample has a distinct database identity but must stay
    # in the same fold as its twin.
    duplicate_sha = samples[0].asset_sha256
    samples.append(
        _sample(
            99,
            asset_sha256=duplicate_sha,
            ground_truth=SemanticGroundTruth.ANATOMY_GOOD,
            confidence_micros=850_000,
        )
    )

    training_groups: list[set[str]] = []

    def capture_training(
        training: tuple[feedback_service._CalibrationSample, ...],
        _threshold_step_micros: int,
    ) -> tuple[SemanticConfusionCounts, ...]:
        training_groups.append({sample.asset_sha256 for sample in training})
        return (
            SemanticConfusionCounts(
                threshold_micros=900_000,
                true_positive=1,
                false_positive=0,
                true_negative=1,
                false_negative=0,
            ),
        )

    monkeypatch.setattr(feedback_service, "_threshold_sweep", capture_training)
    configured, candidate, previous, fold_thresholds = feedback_service._out_of_fold_validation(
        tuple(samples),
        validation_labeled=tuple(samples),
        configured_baseline_threshold_micros=900_000,
        previous_policy_threshold_micros=900_000,
        threshold_step_micros=50_000,
    )

    assert len(training_groups) == SEMANTIC_CALIBRATION_FOLD_COUNT
    for fold, training_sha256 in enumerate(training_groups):
        validation_sha256 = {
            sample.asset_sha256
            for sample in samples
            if feedback_service._calibration_fold(sample.asset_sha256) == fold
        }
        assert training_sha256.isdisjoint(validation_sha256)
    assert configured is not None and configured.sample_count == len(samples)
    assert candidate is not None and candidate.sample_count == len(samples)
    assert previous is not None and previous.sample_count == len(samples)
    assert fold_thresholds == (900_000,) * SEMANTIC_CALIBRATION_FOLD_COUNT


@pytest.mark.asyncio
async def test_inferred_choices_count_toward_readiness_and_are_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows: list[tuple[SemanticAnatomyFeedback, SemanticAssessment]] = []
    for fold in range(SEMANTIC_CALIBRATION_FOLD_COUNT):
        for class_index, ground_truth in enumerate(
            (SemanticGroundTruth.ANATOMY_GOOD, SemanticGroundTruth.ANATOMY_DEFECT)
        ):
            number = (fold * 2) + class_index + 1
            asset_id = UUID(int=10_000 + number)
            assessment_id = UUID(int=20_000 + number)
            source_note = None
            if ground_truth == SemanticGroundTruth.ANATOMY_GOOD and fold >= 2:
                source_note = SEMANTIC_INFERRED_REVIEW_ACCEPT_NOTE
            elif ground_truth == SemanticGroundTruth.ANATOMY_DEFECT and fold >= 2:
                source_note = SEMANTIC_INFERRED_ANATOMY_REJECT_NOTE
            assessment = SemanticAssessment(
                id=assessment_id,
                asset_id=asset_id,
                asset_sha256=_sha_for_fold(fold, offset=class_index),
                verdict=SemanticVerdict.SEVERE,
                confidence_micros=(
                    850_000 if ground_truth == SemanticGroundTruth.ANATOMY_GOOD else 950_000
                ),
                response_sha256=f"{30_000 + number:064x}",
            )
            feedback = SemanticAnatomyFeedback(
                id=UUID(int=40_000 + number),
                semantic_assessment_id=assessment_id,
                asset_id=asset_id,
                profile_sha256=_PROFILE,
                feedback_by_user_id=UUID(int=50_000 + number),
                agreement=SemanticFeedbackAgreement.CORRECT,
                ground_truth=ground_truth,
                issue_code=(
                    SemanticIssueCode.IMPLAUSIBLE_PROPORTION
                    if ground_truth == SemanticGroundTruth.ANATOMY_DEFECT
                    else None
                ),
                note=source_note,
                created_at=_NOW + timedelta(seconds=number),
            )
            rows.append((feedback, assessment))

    async def no_previous_artifact(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(feedback_service, "_latest_artifact_record", no_previous_artifact)
    report = await build_semantic_calibration_report(
        _ReportSession(rows),  # type: ignore[arg-type]
        profile_sha256=_PROFILE,
        minimum_samples=10,
        minimum_per_class=5,
        threshold_step_micros=50_000,
        configured_baseline_threshold_micros=900_000,
    )

    assert report.ready_for_enforcement
    assert report.labeled_sample_count == 10
    assert report.explicit_label_count == 4
    assert report.inferred_label_count == 6
    assert report.inferred_accept_count == 3
    assert report.inferred_anatomy_reject_count == 3
    assert report.current_candidate_validation is not None
    assert report.current_candidate_validation.sample_count == 10
    assert report.learning_status == "stable"
    assert report.effective_threshold_micros == 950_000
    assert report.active_policy_changed is True


def test_candidate_policy_only_activates_on_non_regressing_validation() -> None:
    previous = _validation_metrics(
        true_positive=7,
        false_positive=2,
        true_negative=8,
        false_negative=3,
    )
    improved = _validation_metrics(
        true_positive=8,
        false_positive=1,
        true_negative=9,
        false_negative=2,
    )
    regressed = _validation_metrics(
        true_positive=6,
        false_positive=3,
        true_negative=7,
        false_negative=4,
    )

    assert feedback_service._learning_outcome(
        enough_labels=True,
        validation_complete=True,
        has_previous_active_policy=True,
        candidate_threshold_micros=950_000,
        previous_policy_threshold_micros=900_000,
        current_candidate_validation=improved,
        previous_policy_validation=previous,
    ) == ("improved", 950_000, True)
    assert feedback_service._learning_outcome(
        enough_labels=True,
        validation_complete=True,
        has_previous_active_policy=True,
        candidate_threshold_micros=850_000,
        previous_policy_threshold_micros=900_000,
        current_candidate_validation=regressed,
        previous_policy_validation=previous,
    ) == ("regressed", 900_000, False)


def test_first_validated_baseline_pins_an_immutable_champion_version() -> None:
    validation = _validation_metrics(
        true_positive=8,
        false_positive=2,
        true_negative=8,
        false_negative=2,
    )

    assert feedback_service._learning_outcome(
        enough_labels=True,
        validation_complete=True,
        has_previous_active_policy=False,
        candidate_threshold_micros=900_000,
        previous_policy_threshold_micros=900_000,
        current_candidate_validation=validation,
        previous_policy_validation=validation,
    ) == ("stable", 900_000, True)


@pytest.mark.parametrize(
    ("ready_for_enforcement", "learning_status"),
    (
        (False, "improved"),
        (True, "collecting"),
        (True, "regressed"),
    ),
)
def test_v3_rejects_inconsistent_activation_claims_and_retains_previous_champion(
    ready_for_enforcement: bool,
    learning_status: str,
) -> None:
    artifact = SemanticCalibrationArtifact(
        id=UUID(int=5),
        profile_sha256=_PROFILE,
        version=8,
        calibration_schema_version=SEMANTIC_CALIBRATION_SCHEMA_VERSION,
        dataset_sha256="f" * 64,
        sample_count=200,
        recommended_threshold_micros=100_000,
        ready_for_enforcement=ready_for_enforcement,
        report={
            "learning_status": learning_status,
            "active_policy_changed": True,
            "previous_artifact_version": 7,
            "previous_active_policy_version": 6,
            "previous_policy_threshold_micros": 900_000,
            "candidate_threshold_micros": 100_000,
            "effective_threshold_micros": 100_000,
            "ground_truth_counts": {
                "anatomy_good": 100,
                "anatomy_defect": 100,
                "unjudgeable": 0,
            },
        },
        report_sha256="1" * 64,
        created_by_user_id=UUID(int=6),
        created_at=_NOW,
    )

    result = feedback_service._artifact_result(artifact, created=False)

    assert result.active_policy_changed is False
    assert result.active_policy_version == 6
    assert result.effective_threshold_micros == 900_000


def test_v2_artifact_loads_without_claiming_unmeasured_improvement() -> None:
    artifact = SemanticCalibrationArtifact(
        id=UUID(int=1),
        profile_sha256=_PROFILE,
        version=7,
        calibration_schema_version=LEGACY_SEMANTIC_CALIBRATION_SCHEMA_VERSION,
        dataset_sha256="b" * 64,
        sample_count=120,
        recommended_threshold_micros=850_000,
        ready_for_enforcement=True,
        report={
            "feedback_count": 125,
            "minimum_samples": 100,
            "minimum_per_class": 20,
            "ground_truth_counts": {
                "anatomy_good": 70,
                "anatomy_defect": 50,
                "unjudgeable": 5,
            },
        },
        report_sha256="c" * 64,
        created_by_user_id=UUID(int=2),
        created_at=_NOW,
    )

    result = feedback_service._artifact_result(artifact, created=False)

    assert result.learning_status == "calibrating"
    assert result.validation_sample_count == 0
    assert result.validation_f1_micros is None
    assert result.explicit_label_count == 125
    assert result.inferred_label_count == 0
    assert result.effective_threshold_micros == 850_000
    assert result.active_policy_version == 7
    assert result.minimum_samples == 100
    assert result.minimum_per_class == 20


def test_unknown_calibration_schema_fails_closed_even_when_marked_ready() -> None:
    artifact = SemanticCalibrationArtifact(
        id=UUID(int=3),
        profile_sha256=_PROFILE,
        version=8,
        calibration_schema_version="semantic-anatomy-calibration/v999",
        dataset_sha256="d" * 64,
        sample_count=1_000,
        recommended_threshold_micros=100_000,
        ready_for_enforcement=True,
        report={
            "feedback_count": 1_000,
            "effective_threshold_micros": 100_000,
            "active_policy_changed": True,
            "ground_truth_counts": {
                "anatomy_good": 500,
                "anatomy_defect": 500,
                "unjudgeable": 0,
            },
        },
        report_sha256="e" * 64,
        created_by_user_id=UUID(int=4),
        created_at=_NOW,
    )

    result = feedback_service._artifact_result(artifact, created=False)

    assert result.learning_status == "collecting"
    assert result.active_policy_changed is False
    assert result.active_policy_version is None
    assert result.candidate_threshold_micros is None
    assert result.effective_threshold_micros is None


@pytest.mark.asyncio
async def test_effective_threshold_helper_uses_active_policy_or_configured_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_artifact(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(feedback_service, "load_latest_semantic_calibration_artifact", no_artifact)
    assert (
        await load_effective_semantic_threshold_micros(
            SimpleNamespace(),  # type: ignore[arg-type]
            profile_sha256=_PROFILE,
            configured_fallback_micros=875_000,
        )
        == 875_000
    )

    async def learned_artifact(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            active_policy_version=4,
            effective_threshold_micros=925_000,
        )

    monkeypatch.setattr(
        feedback_service,
        "load_latest_semantic_calibration_artifact",
        learned_artifact,
    )
    assert (
        await load_effective_semantic_threshold_micros(
            SimpleNamespace(),  # type: ignore[arg-type]
            profile_sha256=_PROFILE,
            configured_fallback_micros=875_000,
        )
        == 925_000
    )

    async def collecting_artifact(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            active_policy_version=None,
            effective_threshold_micros=950_000,
        )

    monkeypatch.setattr(
        feedback_service,
        "load_latest_semantic_calibration_artifact",
        collecting_artifact,
    )
    assert (
        await load_effective_semantic_threshold_micros(
            SimpleNamespace(),  # type: ignore[arg-type]
            profile_sha256=_PROFILE,
            configured_fallback_micros=875_000,
        )
        == 875_000
    )
