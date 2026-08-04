from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.exc import DatabaseError

from gen_automation.db.models import (
    AdminUser,
    AssetRanking,
    AssetScore,
    ScoringRun,
    SemanticAnatomyFeedback,
    SemanticAssessment,
    SemanticCalibrationArtifact,
)
from gen_automation.db.session import Database
from gen_automation.domain.canonical import canonical_sha256
from gen_automation.domain.enums import (
    AdminRole,
    AssetScoreState,
    RankingDisposition,
    ScoringRunState,
    SemanticAssessmentState,
    SemanticFeedbackAgreement,
    SemanticGroundTruth,
    SemanticIssueCode,
    SemanticVerdict,
)
from gen_automation.semantic import SemanticAssessmentResult, SemanticIssue
from gen_automation.services.semantic_anatomy import (
    SemanticAssessmentProfile,
    run_semantic_assessment_cycle,
)
from gen_automation.services.semantic_feedback import (
    SEMANTIC_CALIBRATION_SCHEMA_VERSION,
    SemanticFeedbackConflictError,
    SemanticFeedbackValidationError,
    agreement_for_ground_truth,
    build_semantic_calibration_report,
    load_latest_semantic_calibration_artifact,
    persist_semantic_calibration_artifact,
    record_semantic_anatomy_feedback,
)
from tests.test_semantic_anatomy import (  # noqa: F401
    SemanticRuntimeContext,
    semantic_runtime_context,
)

_NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
_PROFILE = SemanticAssessmentProfile(
    model_name="Qwen/Qwen3-VL-8B-Instruct",
    model_revision="60595ebc30ec8e3b1d3b9e65d4943ca011c0006a",
)


@dataclass(frozen=True, slots=True)
class FeedbackContext:
    database: Database
    assessment_id: UUID
    owner_ids: tuple[UUID, UUID, UUID]


async def _add_cross_run_assessment(
    context: FeedbackContext,
    *,
    run_number: int,
    profile_sha256: str | None = None,
    verdict: SemanticVerdict = SemanticVerdict.SEVERE,
    confidence_micros: int = 960_000,
) -> UUID:
    async with context.database.sessions() as session:
        original = await session.get(SemanticAssessment, context.assessment_id)
        assert original is not None
        original_score = await session.get(AssetScore, original.asset_score_id)
        original_run = await session.get(ScoringRun, original.scoring_run_id)
        assert original_score is not None
        assert original_run is not None
        run = ScoringRun(
            release_version_id=original_run.release_version_id,
            configuration={"calibration_dedupe_run": run_number},
            config_sha256=f"{run_number:064x}",
            input_manifest_sha256=f"{run_number + 100:064x}",
            ranking_manifest_sha256=None,
            scorer_version=original_run.scorer_version,
            pillow_version=original_run.pillow_version,
            state=ScoringRunState.RUNNING,
            asset_count=1,
            max_attempts=3,
            created_at=_NOW + timedelta(hours=run_number),
            started_at=_NOW + timedelta(hours=run_number),
            completed_at=None,
        )
        session.add(run)
        await session.flush()
        score = AssetScore(
            scoring_run_id=run.id,
            asset_id=original.asset_id,
            asset_storage_backend=original_score.asset_storage_backend,
            asset_storage_bucket=original_score.asset_storage_bucket,
            asset_sha256=original_score.asset_sha256,
            asset_object_key=original_score.asset_object_key,
            asset_object_version_id=original_score.asset_object_version_id,
            asset_byte_size=original_score.asset_byte_size,
            asset_image_format=original_score.asset_image_format,
            asset_width=original_score.asset_width,
            asset_height=original_score.asset_height,
            state=AssetScoreState.FLAGGED_CORRUPT,
            attempts=1,
            max_attempts=3,
            available_at=_NOW,
            aggregate_score_micros=original_score.aggregate_score_micros,
            signal_detail={"classification": "calibration-dedupe-fixture"},
            scorer_version=run.scorer_version,
            pillow_version=run.pillow_version,
            config_sha256=run.config_sha256,
            completed_at=_NOW + timedelta(hours=run_number, seconds=1),
            created_at=run.created_at,
        )
        session.add(score)
        await session.flush()
        session.add(
            AssetRanking(
                scoring_run_id=run.id,
                asset_score_id=score.id,
                asset_id=original.asset_id,
                rank=1,
                aggregate_score_micros=score.aggregate_score_micros,
                disposition=RankingDisposition.FLAGGED_REVIEW,
                explanation={"fixture": True},
                is_duplicate_representative=False,
                scorer_version=run.scorer_version,
                pillow_version=run.pillow_version,
                config_sha256=run.config_sha256,
                frozen_at=_NOW + timedelta(hours=run_number, seconds=1),
            )
        )
        await session.flush()
        run.ranking_manifest_sha256 = f"{run_number + 200:064x}"
        run.state = ScoringRunState.COMPLETED
        run.completed_at = _NOW + timedelta(hours=run_number, seconds=1)
        await session.flush()
        response = {
            "verdict": verdict.value,
            "confidence_micros": confidence_micros,
            "run_number": run_number,
        }
        assessment = SemanticAssessment(
            scoring_run_id=run.id,
            asset_score_id=score.id,
            asset_id=original.asset_id,
            asset_storage_backend=original.asset_storage_backend,
            asset_storage_bucket=original.asset_storage_bucket,
            asset_object_key=original.asset_object_key,
            asset_object_version_id=original.asset_object_version_id,
            asset_sha256=original.asset_sha256,
            asset_content_type=original.asset_content_type,
            asset_byte_size=original.asset_byte_size,
            profile_sha256=profile_sha256 or original.profile_sha256,
            model_name=original.model_name,
            model_revision=original.model_revision,
            prompt_sha256=original.prompt_sha256,
            schema_sha256=original.schema_sha256,
            state=SemanticAssessmentState.COMPLETED,
            attempts=1,
            max_attempts=3,
            available_at=run.created_at,
            verdict=verdict,
            confidence_micros=confidence_micros,
            issues=[],
            response_sha256=canonical_sha256(response),
            created_at=run.created_at,
            started_at=run.started_at,
            completed_at=run.completed_at,
        )
        session.add(assessment)
        await session.flush()
        assessment_id = assessment.id
        await session.commit()
        return assessment_id


@pytest.fixture
async def feedback_context(
    semantic_runtime_context: SemanticRuntimeContext,  # noqa: F811
) -> FeedbackContext:
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

    result = await run_semantic_assessment_cycle(
        semantic_runtime_context.database.sessions,
        semantic_runtime_context.store,
        worker_id="semantic:feedback-test",
        profile=_PROFILE,
        analyzer=assess,
        max_assessments_per_profile=1,
        asset_allowlist=(),
        max_attempts=2,
        lease_seconds=120,
        retry_base_seconds=5,
        retry_max_seconds=30,
        now=_NOW,
    )
    assert result.processed_assessment
    async with semantic_runtime_context.database.sessions() as session:
        owners = tuple(
            AdminUser(
                username_normalized=f"feedback-owner-{index}",
                display_name=f"Feedback Owner {index}",
                password_hash="disabled-feedback-test-password",  # noqa: S106
                role=AdminRole.OWNER,
                is_active=True,
                failed_login_count=0,
                password_changed_at=_NOW,
                credential_version=1,
                lock_version=1,
            )
            for index in range(3)
        )
        session.add_all(owners)
        await session.flush()
        assessment = await session.scalar(select(SemanticAssessment))
        assert assessment is not None
        owner_ids = tuple(owner.id for owner in owners)
        await session.commit()
    return FeedbackContext(
        database=semantic_runtime_context.database,
        assessment_id=assessment.id,
        owner_ids=(owner_ids[0], owner_ids[1], owner_ids[2]),
    )


def test_agreement_is_derived_from_assessment_and_owner_ground_truth() -> None:
    assert (
        agreement_for_ground_truth(
            SemanticVerdict.PASS,
            SemanticGroundTruth.ANATOMY_GOOD,
        )
        == SemanticFeedbackAgreement.CORRECT
    )
    assert (
        agreement_for_ground_truth(
            SemanticVerdict.SEVERE,
            SemanticGroundTruth.ANATOMY_GOOD,
        )
        == SemanticFeedbackAgreement.INCORRECT
    )
    assert (
        agreement_for_ground_truth(
            SemanticVerdict.REVIEW,
            SemanticGroundTruth.UNJUDGEABLE,
        )
        == SemanticFeedbackAgreement.UNSURE
    )


@pytest.mark.asyncio
async def test_feedback_is_assessment_bound_idempotent_and_immutable(
    feedback_context: FeedbackContext,
) -> None:
    async with feedback_context.database.sessions() as session:
        first = await record_semantic_anatomy_feedback(
            session,
            assessment_id=feedback_context.assessment_id,
            user_id=feedback_context.owner_ids[0],
            ground_truth=SemanticGroundTruth.ANATOMY_DEFECT,
            issue_code=SemanticIssueCode.EXTRA_FINGER,
            note="Visible extra finger.",
            now=_NOW + timedelta(minutes=1),
        )
        repeated = await record_semantic_anatomy_feedback(
            session,
            assessment_id=feedback_context.assessment_id,
            user_id=feedback_context.owner_ids[0],
            ground_truth=SemanticGroundTruth.ANATOMY_DEFECT,
            issue_code=SemanticIssueCode.EXTRA_FINGER,
            note="Visible extra finger.",
            now=_NOW + timedelta(minutes=2),
        )
        assert first.created
        assert not repeated.created
        assert repeated.feedback_id == first.feedback_id
        assert first.agreement == SemanticFeedbackAgreement.CORRECT
        with pytest.raises(SemanticFeedbackConflictError):
            await record_semantic_anatomy_feedback(
                session,
                assessment_id=feedback_context.assessment_id,
                user_id=feedback_context.owner_ids[0],
                ground_truth=SemanticGroundTruth.ANATOMY_GOOD,
                now=_NOW + timedelta(minutes=3),
            )
        with pytest.raises(SemanticFeedbackValidationError):
            await record_semantic_anatomy_feedback(
                session,
                assessment_id=feedback_context.assessment_id,
                user_id=feedback_context.owner_ids[1],
                ground_truth=SemanticGroundTruth.ANATOMY_GOOD,
                agreement=SemanticFeedbackAgreement.CORRECT,
                now=_NOW + timedelta(minutes=3),
            )
        await session.commit()

    async with feedback_context.database.sessions() as session:
        with pytest.raises(DatabaseError, match="append-only"):
            await session.execute(
                update(SemanticAnatomyFeedback)
                .where(SemanticAnatomyFeedback.id == first.feedback_id)
                .values(note="mutated")
            )
        await session.rollback()
        with pytest.raises(DatabaseError, match="append-only"):
            await session.execute(
                delete(SemanticAnatomyFeedback).where(
                    SemanticAnatomyFeedback.id == first.feedback_id
                )
            )


@pytest.mark.asyncio
async def test_calibration_deduplicates_cross_run_owner_labels_by_earliest_feedback(
    feedback_context: FeedbackContext,
) -> None:
    duplicate_assessment_id = await _add_cross_run_assessment(
        feedback_context,
        run_number=1,
    )
    async with feedback_context.database.sessions() as session:
        # Insert in reverse chronological order to prove insertion order is irrelevant.
        await record_semantic_anatomy_feedback(
            session,
            assessment_id=feedback_context.assessment_id,
            user_id=feedback_context.owner_ids[0],
            ground_truth=SemanticGroundTruth.ANATOMY_GOOD,
            now=_NOW + timedelta(minutes=10),
        )
        await record_semantic_anatomy_feedback(
            session,
            assessment_id=duplicate_assessment_id,
            user_id=feedback_context.owner_ids[0],
            ground_truth=SemanticGroundTruth.ANATOMY_DEFECT,
            issue_code=SemanticIssueCode.EXTRA_FINGER,
            now=_NOW + timedelta(minutes=5),
        )

        report = await build_semantic_calibration_report(
            session,
            profile_sha256=_PROFILE.profile_sha256,
        )
        repeated = await build_semantic_calibration_report(
            session,
            profile_sha256=_PROFILE.profile_sha256,
        )

    assert report.feedback_count == 1
    assert report.anatomy_good_count == 0
    assert report.anatomy_defect_count == 1
    assert report.dataset_sha256 == repeated.dataset_sha256
    assert report.to_wire()["schema_version"] == SEMANTIC_CALIBRATION_SCHEMA_VERSION
    assert SEMANTIC_CALIBRATION_SCHEMA_VERSION == "semantic-anatomy-calibration/v2"


@pytest.mark.asyncio
async def test_calibration_keeps_different_owners_independent_after_deduplication(
    feedback_context: FeedbackContext,
) -> None:
    duplicate_assessment_id = await _add_cross_run_assessment(
        feedback_context,
        run_number=2,
    )
    async with feedback_context.database.sessions() as session:
        await record_semantic_anatomy_feedback(
            session,
            assessment_id=feedback_context.assessment_id,
            user_id=feedback_context.owner_ids[0],
            ground_truth=SemanticGroundTruth.ANATOMY_GOOD,
            now=_NOW + timedelta(minutes=1),
        )
        await record_semantic_anatomy_feedback(
            session,
            assessment_id=duplicate_assessment_id,
            user_id=feedback_context.owner_ids[0],
            ground_truth=SemanticGroundTruth.ANATOMY_DEFECT,
            issue_code=SemanticIssueCode.EXTRA_FINGER,
            now=_NOW + timedelta(minutes=2),
        )
        await record_semantic_anatomy_feedback(
            session,
            assessment_id=duplicate_assessment_id,
            user_id=feedback_context.owner_ids[1],
            ground_truth=SemanticGroundTruth.ANATOMY_DEFECT,
            issue_code=SemanticIssueCode.EXTRA_FINGER,
            now=_NOW + timedelta(minutes=3),
        )
        report = await build_semantic_calibration_report(
            session,
            profile_sha256=_PROFILE.profile_sha256,
        )

    assert report.feedback_count == 2
    assert report.anatomy_good_count == 1
    assert report.anatomy_defect_count == 1


@pytest.mark.asyncio
async def test_calibration_deduplication_is_scoped_to_one_profile(
    feedback_context: FeedbackContext,
) -> None:
    other_profile = "f" * 64
    other_assessment_id = await _add_cross_run_assessment(
        feedback_context,
        run_number=3,
        profile_sha256=other_profile,
    )
    async with feedback_context.database.sessions() as session:
        await record_semantic_anatomy_feedback(
            session,
            assessment_id=feedback_context.assessment_id,
            user_id=feedback_context.owner_ids[0],
            ground_truth=SemanticGroundTruth.ANATOMY_GOOD,
            now=_NOW + timedelta(minutes=1),
        )
        await record_semantic_anatomy_feedback(
            session,
            assessment_id=other_assessment_id,
            user_id=feedback_context.owner_ids[0],
            ground_truth=SemanticGroundTruth.ANATOMY_DEFECT,
            issue_code=SemanticIssueCode.EXTRA_FINGER,
            now=_NOW + timedelta(minutes=2),
        )
        original_report = await build_semantic_calibration_report(
            session,
            profile_sha256=_PROFILE.profile_sha256,
        )
        other_report = await build_semantic_calibration_report(
            session,
            profile_sha256=other_profile,
        )

    assert original_report.feedback_count == 1
    assert original_report.anatomy_good_count == 1
    assert other_report.feedback_count == 1
    assert other_report.anatomy_defect_count == 1


@pytest.mark.asyncio
async def test_calibration_uses_feedback_id_as_equal_timestamp_tiebreak(
    feedback_context: FeedbackContext,
) -> None:
    duplicate_assessment_id = await _add_cross_run_assessment(
        feedback_context,
        run_number=4,
    )
    async with feedback_context.database.sessions() as session:
        original = await session.get(SemanticAssessment, feedback_context.assessment_id)
        duplicate = await session.get(SemanticAssessment, duplicate_assessment_id)
        assert original is not None
        assert duplicate is not None
        timestamp = _NOW + timedelta(minutes=1)
        session.add_all(
            (
                SemanticAnatomyFeedback(
                    id=UUID(int=2),
                    semantic_assessment_id=original.id,
                    asset_id=original.asset_id,
                    profile_sha256=original.profile_sha256,
                    feedback_by_user_id=feedback_context.owner_ids[0],
                    agreement=SemanticFeedbackAgreement.INCORRECT,
                    ground_truth=SemanticGroundTruth.ANATOMY_GOOD,
                    issue_code=None,
                    note=None,
                    created_at=timestamp,
                ),
                SemanticAnatomyFeedback(
                    id=UUID(int=1),
                    semantic_assessment_id=duplicate.id,
                    asset_id=duplicate.asset_id,
                    profile_sha256=duplicate.profile_sha256,
                    feedback_by_user_id=feedback_context.owner_ids[0],
                    agreement=SemanticFeedbackAgreement.CORRECT,
                    ground_truth=SemanticGroundTruth.ANATOMY_DEFECT,
                    issue_code=SemanticIssueCode.EXTRA_FINGER,
                    note=None,
                    created_at=timestamp,
                ),
            )
        )
        await session.flush()
        report = await build_semantic_calibration_report(
            session,
            profile_sha256=_PROFILE.profile_sha256,
        )

    assert report.feedback_count == 1
    assert report.anatomy_good_count == 0
    assert report.anatomy_defect_count == 1


@pytest.mark.asyncio
async def test_calibration_sweeps_thresholds_and_versions_immutable_artifacts(
    feedback_context: FeedbackContext,
) -> None:
    async with feedback_context.database.sessions() as session:
        await record_semantic_anatomy_feedback(
            session,
            assessment_id=feedback_context.assessment_id,
            user_id=feedback_context.owner_ids[0],
            ground_truth=SemanticGroundTruth.ANATOMY_DEFECT,
            issue_code=SemanticIssueCode.EXTRA_FINGER,
            now=_NOW + timedelta(minutes=1),
        )
        await record_semantic_anatomy_feedback(
            session,
            assessment_id=feedback_context.assessment_id,
            user_id=feedback_context.owner_ids[1],
            ground_truth=SemanticGroundTruth.ANATOMY_GOOD,
            now=_NOW + timedelta(minutes=2),
        )
        report_one = await build_semantic_calibration_report(
            session,
            profile_sha256=_PROFILE.profile_sha256,
            minimum_samples=2,
            minimum_per_class=1,
            threshold_step_micros=50_000,
        )
        assert report_one.feedback_count == 2
        assert report_one.labeled_sample_count == 2
        assert report_one.anatomy_good_count == 1
        assert report_one.anatomy_defect_count == 1
        assert report_one.agreement_correct_count == 1
        assert report_one.agreement_incorrect_count == 1
        assert report_one.recommended_threshold_micros == 950_000
        assert report_one.ready_for_enforcement
        at_950 = next(
            item for item in report_one.threshold_sweep if item.threshold_micros == 950_000
        )
        assert (at_950.true_positive, at_950.false_positive) == (1, 1)

        artifact_one = await persist_semantic_calibration_artifact(
            session,
            report=report_one,
            created_by_user_id=feedback_context.owner_ids[0],
            now=_NOW + timedelta(minutes=3),
        )
        duplicate = await persist_semantic_calibration_artifact(
            session,
            report=report_one,
            created_by_user_id=feedback_context.owner_ids[0],
            now=_NOW + timedelta(minutes=4),
        )
        assert artifact_one.created and artifact_one.version == 1
        assert artifact_one.sample_count == 2
        assert not duplicate.created and duplicate.artifact_id == artifact_one.artifact_id

        recalibrated_same_dataset = await build_semantic_calibration_report(
            session,
            profile_sha256=_PROFILE.profile_sha256,
            minimum_samples=3,
            minimum_per_class=1,
            threshold_step_micros=50_000,
        )
        assert recalibrated_same_dataset.dataset_sha256 == report_one.dataset_sha256
        assert not recalibrated_same_dataset.ready_for_enforcement
        artifact_two = await persist_semantic_calibration_artifact(
            session,
            report=recalibrated_same_dataset,
            created_by_user_id=feedback_context.owner_ids[0],
            now=_NOW + timedelta(minutes=4),
        )
        assert artifact_two.version == 2

        unsure = await record_semantic_anatomy_feedback(
            session,
            assessment_id=feedback_context.assessment_id,
            user_id=feedback_context.owner_ids[2],
            ground_truth=SemanticGroundTruth.UNJUDGEABLE,
            now=_NOW + timedelta(minutes=5),
        )
        assert unsure.agreement == SemanticFeedbackAgreement.UNSURE
        report_two = await build_semantic_calibration_report(
            session,
            profile_sha256=_PROFILE.profile_sha256,
            minimum_samples=2,
            minimum_per_class=1,
            threshold_step_micros=50_000,
        )
        assert report_two.feedback_count == 3
        assert report_two.labeled_sample_count == 2
        assert report_two.unjudgeable_count == 1
        assert report_two.dataset_sha256 != report_one.dataset_sha256
        artifact_three = await persist_semantic_calibration_artifact(
            session,
            report=report_two,
            created_by_user_id=feedback_context.owner_ids[0],
            now=_NOW + timedelta(minutes=6),
        )
        assert artifact_three.version == 3
        latest = await load_latest_semantic_calibration_artifact(
            session,
            profile_sha256=_PROFILE.profile_sha256,
        )
        assert latest is not None and latest.artifact_id == artifact_three.artifact_id
        assert latest.sample_count == 2
        await session.commit()

    async with feedback_context.database.sessions() as session:
        with pytest.raises(DatabaseError, match="append-only"):
            await session.execute(
                update(SemanticCalibrationArtifact)
                .where(SemanticCalibrationArtifact.id == artifact_one.artifact_id)
                .values(version=99)
            )
