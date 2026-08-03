from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.exc import DatabaseError

from gen_automation.db.models import (
    AdminUser,
    SemanticAnatomyFeedback,
    SemanticAssessment,
    SemanticCalibrationArtifact,
)
from gen_automation.db.session import Database
from gen_automation.domain.enums import (
    AdminRole,
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
            item
            for item in report_one.threshold_sweep
            if item.threshold_micros == 950_000
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
