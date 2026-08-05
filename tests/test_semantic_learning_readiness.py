from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import select

from gen_automation.db.models import GenerationJob, SemanticAssessment
from gen_automation.domain.canonical import canonical_sha256
from gen_automation.domain.enums import (
    SemanticGroundTruth,
    SemanticIssueCode,
    SemanticVerdict,
)
from gen_automation.semantic import SemanticAssessmentResult, SemanticIssue
from gen_automation.services.semantic_anatomy import (
    SemanticAssessmentProfile,
    run_semantic_assessment_cycle,
)
from gen_automation.services.semantic_feedback import record_semantic_anatomy_feedback
from gen_automation.services.semantic_learning_readiness import (
    SOURCE_EXPLICIT,
    SOURCE_INFERRED_ANATOMY_REJECT,
    SOURCE_INFERRED_REVIEW_ACCEPT,
    SemanticLearningSample,
    SemanticPredictedIssue,
    build_semantic_learning_readiness_report,
    semantic_meta_feature_values,
    summarize_semantic_learning_readiness,
)
from tests.test_semantic_anatomy import (  # noqa: F401
    SemanticRuntimeContext,
    semantic_runtime_context,
)

_NOW = datetime(2026, 8, 6, 9, 0, tzinfo=UTC)
_PROFILE = "a" * 64


def _sample(
    index: int,
    *,
    truth: SemanticGroundTruth,
    issue: SemanticIssueCode | None = None,
    source: str = SOURCE_EXPLICIT,
    verdict: SemanticVerdict = SemanticVerdict.PASS,
    asset_sha256: str | None = None,
    release_number: int = 1,
    batch_number: int = 1,
    day: int = 0,
    completed_review: bool = True,
    predicted: tuple[SemanticPredictedIssue, ...] = (),
    owner_number: int = 40_000,
    checkpoint_cohort: str | None = None,
    lora_stack_cohort: str | None = None,
    workflow_cohort: str | None = None,
    style_cohort: str | None = None,
) -> SemanticLearningSample:
    return SemanticLearningSample(
        feedback_id=UUID(int=10_000 + index),
        assessment_id=UUID(int=20_000 + index),
        asset_id=UUID(int=30_000 + index),
        feedback_by_user_id=UUID(int=owner_number),
        profile_sha256=_PROFILE,
        asset_sha256=asset_sha256 or f"{index + 1:064x}",
        ground_truth=truth,
        owner_issue_code=issue,
        source=source,
        verdict=verdict,
        confidence_micros=900_000,
        predicted_issues=predicted,
        release_id=UUID(int=50_000 + release_number),
        generation_job_id=UUID(int=60_000 + batch_number),
        generated_at=_NOW + timedelta(days=day),
        labeled_at=_NOW + timedelta(days=day, minutes=index),
        completed_review=completed_review,
        checkpoint_cohort=checkpoint_cohort,
        lora_stack_cohort=lora_stack_cohort,
        workflow_cohort=workflow_cohort,
        style_cohort=style_cohort,
    )


def _counts(values: object) -> dict[str, int]:
    return {value.name: value.count for value in values}  # type: ignore[attr-defined]


def test_readiness_counts_sources_issues_conflicts_and_disagreement_priority() -> None:
    hand_prediction = (
        SemanticPredictedIssue(
            code=SemanticIssueCode.EXTRA_FINGER,
            confidence_micros=980_000,
            has_box=True,
        ),
        SemanticPredictedIssue(
            code=SemanticIssueCode.EXTRA_FINGER,
            confidence_micros=850_000,
        ),
    )
    samples = (
        _sample(1, truth=SemanticGroundTruth.ANATOMY_GOOD, verdict=SemanticVerdict.SEVERE),
        _sample(
            2,
            truth=SemanticGroundTruth.ANATOMY_DEFECT,
            issue=SemanticIssueCode.EXTRA_FINGER,
            verdict=SemanticVerdict.PASS,
            predicted=hand_prediction,
            release_number=2,
            batch_number=2,
            day=1,
        ),
        _sample(
            3,
            truth=SemanticGroundTruth.ANATOMY_DEFECT,
            source=SOURCE_INFERRED_ANATOMY_REJECT,
            verdict=SemanticVerdict.REVIEW,
            release_number=2,
            batch_number=3,
            day=1,
        ),
        _sample(
            4,
            truth=SemanticGroundTruth.ANATOMY_GOOD,
            asset_sha256=f"{2:064x}",
        ),
        _sample(5, truth=SemanticGroundTruth.ANATOMY_GOOD, asset_sha256="f" * 64),
        _sample(6, truth=SemanticGroundTruth.ANATOMY_DEFECT, asset_sha256="f" * 64),
    )

    report = summarize_semantic_learning_readiness(reversed(samples))
    profile = report.profiles[0]

    assert profile.raw_feedback_count == 6
    assert profile.unique_content_count == 3
    assert profile.binary_labeled_count == 3
    assert profile.duplicate_content_count == 2
    assert profile.conflicting_content_count == 1
    assert profile.excluded_conflicting_content_count == 1
    assert profile.resolved_conflicting_content_count == 0
    assert profile.anatomy_good_count == 1
    assert profile.anatomy_defect_count == 2
    assert profile.generic_defect_count == 1
    assert _counts(profile.source_counts)[SOURCE_INFERRED_ANATOMY_REJECT] == 1
    assert _counts(profile.owner_issue_counts)["extra_finger"] == 1
    assert _counts(profile.owner_issue_family_counts)["hand"] == 1
    assert _counts(profile.model_issue_counts)["extra_finger"] == 1
    assert _counts(profile.model_issue_occurrence_counts)["extra_finger"] == 2
    assert [item.kind for item in profile.disagreement_priority] == [
        "missed_defect",
        "false_severe",
    ]
    assert [item.kind for item in profile.audit_priority] == [
        "missed_defect",
        "false_severe",
        "confirmed_review",
    ]
    assert _counts(profile.audit_priority_counts)["confirmed_review"] == 1
    assert not profile.calibration.ready
    assert report.meta_feature_names
    assert (
        profile.dataset_sha256
        == summarize_semantic_learning_readiness(samples).profiles[0].dataset_sha256
    )


def test_readiness_is_per_owner_and_dedupes_by_strongest_evidence() -> None:
    first_digest = "d" * 64
    second_digest = "e" * 64
    samples = (
        _sample(
            10,
            truth=SemanticGroundTruth.ANATOMY_DEFECT,
            source=SOURCE_INFERRED_ANATOMY_REJECT,
            asset_sha256=first_digest,
        ),
        _sample(
            11,
            truth=SemanticGroundTruth.ANATOMY_DEFECT,
            issue=SemanticIssueCode.MALFORMED_HAND,
            source=SOURCE_EXPLICIT,
            asset_sha256=first_digest,
        ),
        _sample(
            12,
            truth=SemanticGroundTruth.ANATOMY_GOOD,
            source=SOURCE_INFERRED_REVIEW_ACCEPT,
            asset_sha256=second_digest,
        ),
        _sample(
            13,
            truth=SemanticGroundTruth.ANATOMY_DEFECT,
            issue=SemanticIssueCode.EXTRA_LIMB,
            source=SOURCE_EXPLICIT,
            asset_sha256=second_digest,
            checkpoint_cohort="1" * 64,
            lora_stack_cohort="2" * 64,
            workflow_cohort="3" * 64,
            style_cohort="4" * 64,
        ),
        _sample(
            14,
            truth=SemanticGroundTruth.ANATOMY_GOOD,
            owner_number=40_001,
        ),
    )

    report = summarize_semantic_learning_readiness(samples)
    assert len(report.profiles) == 2
    first_owner = next(item for item in report.profiles if item.owner_user_id == UUID(int=40_000))
    second_owner = next(item for item in report.profiles if item.owner_user_id == UUID(int=40_001))

    assert first_owner.unique_content_count == 2
    assert first_owner.anatomy_defect_count == 2
    assert first_owner.explicit_label_count == 2
    assert first_owner.conflicting_content_count == 1
    assert first_owner.resolved_conflicting_content_count == 1
    assert first_owner.excluded_conflicting_content_count == 0
    assert _counts(first_owner.owner_issue_counts)["malformed_hand"] == 1
    assert _counts(first_owner.owner_issue_counts)["extra_limb"] == 1
    assert first_owner.cohorts.style_stack_count == 1
    assert second_owner.anatomy_good_count == 1


def test_phase_gates_require_diverse_grouped_temporal_data() -> None:
    meta_samples = tuple(
        _sample(
            index,
            truth=(
                SemanticGroundTruth.ANATOMY_GOOD
                if index < 350
                else SemanticGroundTruth.ANATOMY_DEFECT
            ),
            issue=(SemanticIssueCode.EXTRA_FINGER if index >= 350 else None),
            verdict=(SemanticVerdict.PASS if index < 350 else SemanticVerdict.SEVERE),
            release_number=index % 3,
            batch_number=index % 5,
            day=index % 3,
        )
        for index in range(500)
    )
    meta = summarize_semantic_learning_readiness(meta_samples).profiles[0]
    assert meta.calibration.ready
    assert meta.meta_classifier.ready
    assert not meta.meta_evaluation.ready
    assert not meta.lora.ready
    assert meta.split.set_group_split_eligible
    assert meta.split.batch_group_split_eligible
    assert meta.split.temporal_split_eligible
    assert meta.split.recommended_group_key == "release_id"
    assert meta.split.evaluation_holdout is not None
    assert meta.split.evaluation_holdout.zero_false_reject_upper_micros is not None
    assert meta.split.evaluation_holdout.zero_false_reject_upper_micros > 20_000

    issue_codes = (
        SemanticIssueCode.EXTRA_FINGER,
        SemanticIssueCode.EXTRA_TOE,
        SemanticIssueCode.EXTRA_LIMB,
        SemanticIssueCode.IMPOSSIBLE_JOINT,
    )
    lora_samples = tuple(
        _sample(
            3_000 + index,
            truth=(
                SemanticGroundTruth.ANATOMY_GOOD
                if index < 1_500
                else SemanticGroundTruth.ANATOMY_DEFECT
            ),
            issue=(issue_codes[index % len(issue_codes)] if index >= 1_500 else None),
            verdict=(SemanticVerdict.PASS if index < 1_500 else SemanticVerdict.SEVERE),
            release_number=index % 5,
            batch_number=index % 10,
            day=index % 5,
        )
        for index in range(2_000)
    )
    lora = summarize_semantic_learning_readiness(lora_samples).profiles[0]
    assert lora.meta_evaluation.ready
    assert lora.split.evaluation_holdout is not None
    assert lora.split.evaluation_holdout.zero_false_reject_upper_micros is not None
    assert lora.split.evaluation_holdout.zero_false_reject_upper_micros <= 20_000
    assert lora.lora.ready
    assert not lora.lora.blockers


def test_meta_feature_schema_extracts_bounded_structured_vlm_signals() -> None:
    sample = _sample(
        1,
        truth=SemanticGroundTruth.ANATOMY_DEFECT,
        verdict=SemanticVerdict.SEVERE,
        predicted=(
            SemanticPredictedIssue(
                code=SemanticIssueCode.EXTRA_FINGER,
                confidence_micros=980_000,
                has_box=True,
            ),
            SemanticPredictedIssue(
                code=SemanticIssueCode.EXTRA_LIMB,
                confidence_micros=810_000,
            ),
        ),
    )
    report = summarize_semantic_learning_readiness((sample,))
    values = semantic_meta_feature_values(sample)
    features = dict(zip(report.meta_feature_names, values, strict=True))

    assert features["verdict_severe"] == 1
    assert features["assessment_confidence_micros"] == 900_000
    assert features["issue_count"] == 2
    assert features["maximum_issue_confidence_micros"] == 980_000
    assert features["boxed_issue_count"] == 1
    assert features["extra_finger_present"] == 1
    assert features["extra_finger_confidence_micros"] == 980_000
    assert features["missing_toe_present"] == 0


@pytest.mark.asyncio
async def test_readiness_loader_joins_profile_to_generation_groups_without_writes(
    semantic_runtime_context: SemanticRuntimeContext,  # noqa: F811
) -> None:
    profile = SemanticAssessmentProfile(
        model_name="Qwen/Qwen3-VL-8B-Instruct",
        model_revision="readiness-fixture-v1",
    )

    async def assess(
        _payload: bytes,
        _content_type: str,
        _sha256: str,
    ) -> SemanticAssessmentResult:
        return SemanticAssessmentResult(
            verdict=SemanticVerdict.SEVERE,
            confidence_micros=960_000,
            issues=(
                SemanticIssue(
                    code=SemanticIssueCode.EXTRA_FINGER,
                    confidence_micros=980_000,
                ),
            ),
        )

    cycle = await run_semantic_assessment_cycle(
        semantic_runtime_context.database.sessions,
        semantic_runtime_context.store,
        worker_id="semantic:readiness-test",
        profile=profile,
        analyzer=assess,
        max_assessments_per_profile=1,
        asset_allowlist=(),
        max_attempts=2,
        lease_seconds=120,
        retry_base_seconds=5,
        retry_max_seconds=30,
        now=_NOW,
    )
    assert cycle.processed_assessment

    async with semantic_runtime_context.database.sessions() as session:
        job = await session.scalar(select(GenerationJob))
        assert job is not None
        job.parameters = {
            "schema_version": 2,
            "checkpoint": {"sha256": "1" * 64},
            "loras": [{"sha256": "2" * 64, "weight": 0.5}],
            "workflow": {"sha256": "3" * 64},
        }
        job.parameters_sha256 = canonical_sha256(job.parameters)
        assessment = await session.scalar(
            select(SemanticAssessment).where(
                SemanticAssessment.profile_sha256 == profile.profile_sha256
            )
        )
        assert assessment is not None
        await record_semantic_anatomy_feedback(
            session,
            assessment_id=assessment.id,
            user_id=semantic_runtime_context.owner_id,
            ground_truth=SemanticGroundTruth.ANATOMY_DEFECT,
            issue_code=SemanticIssueCode.EXTRA_FINGER,
            now=_NOW + timedelta(minutes=1),
        )
        await session.commit()
        report = await build_semantic_learning_readiness_report(
            session,
            owner_user_id=semantic_runtime_context.owner_id,
            profile_sha256=profile.profile_sha256,
        )

    result = report.profiles[0]
    assert result.raw_feedback_count == 1
    assert result.owner_user_id == semantic_runtime_context.owner_id
    assert result.anatomy_defect_count == 1
    assert result.split.release_set_count == 1
    assert result.split.generation_batch_count == 1
    assert result.split.completed_review_set_count == 0
    assert result.cohorts.checkpoint_count == 1
    assert result.cohorts.lora_stack_count == 1
    assert result.cohorts.workflow_count == 1
    assert result.cohorts.style_stack_count == 1
    assert result.cohorts.missing_style_metadata_count == 0
    assert _counts(result.owner_issue_counts)["extra_finger"] == 1
    assert _counts(result.model_issue_counts)["extra_finger"] == 1
