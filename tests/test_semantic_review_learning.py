from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import select

from gen_automation.db.models import SemanticAnatomyFeedback, SemanticAssessment
from gen_automation.domain.enums import (
    ReviewDecisionValue,
    SemanticGroundTruth,
    SemanticIssueCode,
)
from gen_automation.services import semantic_review_learning as learning_service
from gen_automation.services.semantic_feedback import (
    SEMANTIC_INFERRED_ANATOMY_REJECT_NOTE,
    SEMANTIC_INFERRED_REVIEW_ACCEPT_NOTE,
    record_semantic_anatomy_feedback,
)
from gen_automation.services.semantic_review_learning import (
    SemanticReviewChoice,
    learn_semantic_anatomy_from_final_review,
)
from tests.test_semantic_anatomy import semantic_runtime_context  # noqa: F401
from tests.test_semantic_feedback import (  # noqa: F401
    FeedbackContext,
    feedback_context,
)

_NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class _AssessmentIdentity:
    assessment_id: UUID
    scoring_run_id: UUID
    asset_id: UUID
    profile_sha256: str


async def _assessment_identity(context: FeedbackContext) -> _AssessmentIdentity:
    async with context.database.sessions() as session:
        assessment = await session.get(SemanticAssessment, context.assessment_id)
        assert assessment is not None
        return _AssessmentIdentity(
            assessment_id=assessment.id,
            scoring_run_id=assessment.scoring_run_id,
            asset_id=assessment.asset_id,
            profile_sha256=assessment.profile_sha256,
        )


def _choice(
    identity: _AssessmentIdentity,
    *,
    owner_user_id: UUID,
    decision: ReviewDecisionValue,
    reason_code: str | None = None,
    severe_override: bool = False,
) -> SemanticReviewChoice:
    return SemanticReviewChoice(
        asset_id=identity.asset_id,
        decision=decision,
        reason_code=reason_code,
        decided_by_user_id=owner_user_id,
        semantic_severe_override_attested=severe_override,
    )


def _capture_refresh(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []

    async def refresh(_session: object, **kwargs: object) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(
        learning_service,
        "refresh_semantic_calibration_artifact",
        refresh,
    )
    return calls


@pytest.mark.asyncio
async def test_final_owner_accept_is_inferred_as_anatomy_good(
    feedback_context: FeedbackContext,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = await _assessment_identity(feedback_context)
    owner_id = feedback_context.owner_ids[0]
    refresh_calls = _capture_refresh(monkeypatch)

    async with feedback_context.database.sessions() as session:
        result = await learn_semantic_anatomy_from_final_review(
            session,
            scoring_run_id=identity.scoring_run_id,
            profile_sha256=identity.profile_sha256,
            owner_user_id=owner_id,
            choices=(
                _choice(
                    identity,
                    owner_user_id=owner_id,
                    decision=ReviewDecisionValue.ACCEPT,
                ),
            ),
            now=_NOW,
        )
        feedback = await session.scalar(
            select(SemanticAnatomyFeedback).where(
                SemanticAnatomyFeedback.semantic_assessment_id == identity.assessment_id,
                SemanticAnatomyFeedback.feedback_by_user_id == owner_id,
            )
        )

    assert result.inferred_good_count == 1
    assert result.inferred_defect_count == 0
    assert result.skipped_existing_count == 0
    assert result.skipped_unsafe_count == 0
    assert feedback is not None
    assert feedback.ground_truth == SemanticGroundTruth.ANATOMY_GOOD
    assert feedback.issue_code is None
    assert feedback.note == SEMANTIC_INFERRED_REVIEW_ACCEPT_NOTE
    assert len(refresh_calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reason_code", "expected_issue"),
    (
        ("anatomy", None),
        ("malformed_hand", SemanticIssueCode.MALFORMED_HAND),
    ),
)
async def test_anatomy_coded_reject_is_inferred_as_anatomy_defect(
    feedback_context: FeedbackContext,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    reason_code: str,
    expected_issue: SemanticIssueCode | None,
) -> None:
    identity = await _assessment_identity(feedback_context)
    owner_id = feedback_context.owner_ids[0]
    refresh_calls = _capture_refresh(monkeypatch)

    async with feedback_context.database.sessions() as session:
        result = await learn_semantic_anatomy_from_final_review(
            session,
            scoring_run_id=identity.scoring_run_id,
            profile_sha256=identity.profile_sha256,
            owner_user_id=owner_id,
            choices=(
                _choice(
                    identity,
                    owner_user_id=owner_id,
                    decision=ReviewDecisionValue.REJECT,
                    reason_code=reason_code,
                ),
            ),
            now=_NOW,
        )
        feedback = await session.scalar(
            select(SemanticAnatomyFeedback).where(
                SemanticAnatomyFeedback.semantic_assessment_id == identity.assessment_id,
                SemanticAnatomyFeedback.feedback_by_user_id == owner_id,
            )
        )

    assert result.inferred_good_count == 0
    assert result.inferred_defect_count == 1
    assert result.skipped_unsafe_count == 0
    assert feedback is not None
    assert feedback.ground_truth == SemanticGroundTruth.ANATOMY_DEFECT
    assert feedback.issue_code == expected_issue
    assert feedback.note == SEMANTIC_INFERRED_ANATOMY_REJECT_NOTE
    assert len(refresh_calls) == 1


@pytest.mark.asyncio
async def test_generic_reject_and_hold_are_ignored(
    feedback_context: FeedbackContext,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = await _assessment_identity(feedback_context)
    owner_id = feedback_context.owner_ids[0]
    refresh_calls = _capture_refresh(monkeypatch)

    async with feedback_context.database.sessions() as session:
        result = await learn_semantic_anatomy_from_final_review(
            session,
            scoring_run_id=identity.scoring_run_id,
            profile_sha256=identity.profile_sha256,
            owner_user_id=owner_id,
            choices=(
                _choice(
                    identity,
                    owner_user_id=owner_id,
                    decision=ReviewDecisionValue.REJECT,
                    reason_code="composition",
                ),
                _choice(
                    identity,
                    owner_user_id=owner_id,
                    decision=ReviewDecisionValue.HOLD,
                ),
            ),
            now=_NOW,
        )
        feedback = await session.scalar(
            select(SemanticAnatomyFeedback).where(
                SemanticAnatomyFeedback.semantic_assessment_id == identity.assessment_id,
                SemanticAnatomyFeedback.feedback_by_user_id == owner_id,
            )
        )

    assert result.inferred_count == 0
    assert result.skipped_existing_count == 0
    assert result.skipped_unsafe_count == 2
    assert feedback is None
    assert refresh_calls == []


@pytest.mark.asyncio
async def test_severe_override_accept_is_ignored(
    feedback_context: FeedbackContext,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = await _assessment_identity(feedback_context)
    owner_id = feedback_context.owner_ids[0]
    refresh_calls = _capture_refresh(monkeypatch)

    async with feedback_context.database.sessions() as session:
        result = await learn_semantic_anatomy_from_final_review(
            session,
            scoring_run_id=identity.scoring_run_id,
            profile_sha256=identity.profile_sha256,
            owner_user_id=owner_id,
            choices=(
                _choice(
                    identity,
                    owner_user_id=owner_id,
                    decision=ReviewDecisionValue.ACCEPT,
                    reason_code="semantic_severe_override",
                    severe_override=True,
                ),
            ),
            now=_NOW,
        )

    assert result.inferred_count == 0
    assert result.skipped_unsafe_count == 1
    assert refresh_calls == []


@pytest.mark.asyncio
async def test_non_owner_decision_is_ignored(
    feedback_context: FeedbackContext,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = await _assessment_identity(feedback_context)
    owner_id = feedback_context.owner_ids[0]
    other_actor_id = feedback_context.owner_ids[1]
    refresh_calls = _capture_refresh(monkeypatch)

    async with feedback_context.database.sessions() as session:
        result = await learn_semantic_anatomy_from_final_review(
            session,
            scoring_run_id=identity.scoring_run_id,
            profile_sha256=identity.profile_sha256,
            owner_user_id=owner_id,
            choices=(
                _choice(
                    identity,
                    owner_user_id=other_actor_id,
                    decision=ReviewDecisionValue.ACCEPT,
                ),
            ),
            now=_NOW,
        )

    assert result.inferred_count == 0
    assert result.skipped_existing_count == 0
    assert result.skipped_unsafe_count == 1
    assert refresh_calls == []


@pytest.mark.asyncio
async def test_existing_explicit_feedback_wins_over_inferred_choice(
    feedback_context: FeedbackContext,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = await _assessment_identity(feedback_context)
    owner_id = feedback_context.owner_ids[0]
    refresh_calls = _capture_refresh(monkeypatch)

    async with feedback_context.database.sessions() as session:
        explicit = await record_semantic_anatomy_feedback(
            session,
            assessment_id=identity.assessment_id,
            user_id=owner_id,
            ground_truth=SemanticGroundTruth.ANATOMY_DEFECT,
            issue_code=SemanticIssueCode.EXTRA_FINGER,
            note="Explicit owner label.",
            now=_NOW,
        )
        result = await learn_semantic_anatomy_from_final_review(
            session,
            scoring_run_id=identity.scoring_run_id,
            profile_sha256=identity.profile_sha256,
            owner_user_id=owner_id,
            choices=(
                _choice(
                    identity,
                    owner_user_id=owner_id,
                    decision=ReviewDecisionValue.ACCEPT,
                ),
            ),
            now=_NOW + timedelta(minutes=1),
        )
        feedback = await session.get(SemanticAnatomyFeedback, explicit.feedback_id)

    assert result.inferred_count == 0
    assert result.skipped_existing_count == 1
    assert result.skipped_unsafe_count == 0
    assert feedback is not None
    assert feedback.ground_truth == SemanticGroundTruth.ANATOMY_DEFECT
    assert feedback.issue_code == SemanticIssueCode.EXTRA_FINGER
    assert feedback.note == "Explicit owner label."
    assert refresh_calls == []


@pytest.mark.asyncio
async def test_repeated_learning_is_idempotent_and_refreshes_only_after_new_labels(
    feedback_context: FeedbackContext,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = await _assessment_identity(feedback_context)
    owner_id = feedback_context.owner_ids[0]
    refresh_calls = _capture_refresh(monkeypatch)
    choice = _choice(
        identity,
        owner_user_id=owner_id,
        decision=ReviewDecisionValue.ACCEPT,
    )

    async with feedback_context.database.sessions() as session:
        first = await learn_semantic_anatomy_from_final_review(
            session,
            scoring_run_id=identity.scoring_run_id,
            profile_sha256=identity.profile_sha256,
            owner_user_id=owner_id,
            choices=(choice,),
            now=_NOW,
        )
        repeated = await learn_semantic_anatomy_from_final_review(
            session,
            scoring_run_id=identity.scoring_run_id,
            profile_sha256=identity.profile_sha256,
            owner_user_id=owner_id,
            choices=(choice,),
            now=_NOW + timedelta(minutes=1),
        )
        feedback_count = len(
            (
                await session.scalars(
                    select(SemanticAnatomyFeedback).where(
                        SemanticAnatomyFeedback.semantic_assessment_id == identity.assessment_id,
                        SemanticAnatomyFeedback.feedback_by_user_id == owner_id,
                    )
                )
            ).all()
        )

    assert first.inferred_count == 1
    assert first.skipped_existing_count == 0
    assert repeated.inferred_count == 0
    assert repeated.skipped_existing_count == 1
    assert feedback_count == 1
    assert len(refresh_calls) == 1
    assert refresh_calls[0]["profile_sha256"] == identity.profile_sha256
    assert refresh_calls[0]["created_by_user_id"] == owner_id
