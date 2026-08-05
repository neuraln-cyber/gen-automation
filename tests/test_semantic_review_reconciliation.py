from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from gen_automation.db.models import SemanticAnatomyFeedback, SemanticAssessment
from gen_automation.domain.enums import (
    ReviewDecisionValue,
    ReviewTaskState,
    SemanticAssessmentState,
    SemanticEnforcementMode,
    SemanticGroundTruth,
)
from gen_automation.services import semantic_review_learning as learning_service
from gen_automation.services.review import append_review_decision, transition_review_task
from gen_automation.services.semantic_anatomy import (
    SemanticAssessmentProfile,
    run_semantic_assessment_cycle,
)
from gen_automation.services.semantic_review_reconciliation import (
    reconcile_one_completed_semantic_review,
)
from tests.test_semantic_anatomy import (
    SemanticRuntimeContext,
    _add_ranked_review,
    _passing_assessment,
    semantic_runtime_context,  # noqa: F401
)

_NOW = datetime(2026, 8, 5, 15, 0, tzinfo=UTC)
_MODEL = "Qwen/Qwen3-VL-8B-Instruct"
_REVISION = "0123456789abcdef0123456789abcdef01234567"


@pytest.mark.asyncio
async def test_late_results_are_learned_once_and_unsafe_choices_are_ignored(
    semantic_runtime_context: SemanticRuntimeContext,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_id, scoring_run_id, asset_ids = await _add_ranked_review(
        semantic_runtime_context,
        output_indexes=(40, 41, 42),
        review_created_at=_NOW,
    )
    profile = SemanticAssessmentProfile(model_name=_MODEL, model_revision=_REVISION)

    # Prefill the whole review in one transaction, but intentionally complete
    # only one assessment before the owner finishes the shadow-mode review.
    first = await run_semantic_assessment_cycle(
        semantic_runtime_context.database.sessions,
        semantic_runtime_context.store,
        worker_id="semantic:late-learning",
        profile=profile,
        analyzer=_passing_assessment,
        max_assessments_per_profile=3,
        asset_allowlist=asset_ids,
        max_attempts=2,
        lease_seconds=120,
        retry_base_seconds=5,
        retry_max_seconds=30,
        now=_NOW + timedelta(minutes=1),
    )
    assert first.created_assessment and first.processed_assessment
    async with semantic_runtime_context.database.sessions() as session:
        assessments = list(
            (
                await session.scalars(
                    select(SemanticAssessment).where(
                        SemanticAssessment.scoring_run_id == scoring_run_id,
                        SemanticAssessment.profile_sha256 == profile.profile_sha256,
                    )
                )
            ).all()
        )
    assert len(assessments) == 3
    pending_asset_ids = [
        assessment.asset_id
        for assessment in assessments
        if assessment.state == SemanticAssessmentState.PENDING
    ]
    assert len(pending_asset_ids) >= 2
    accepted_asset_id = pending_asset_ids[0]
    remaining_asset_ids = [asset_id for asset_id in asset_ids if asset_id != accepted_asset_id]

    decisions = (
        (accepted_asset_id, ReviewDecisionValue.ACCEPT, None),
        (remaining_asset_ids[0], ReviewDecisionValue.REJECT, "composition"),
        (remaining_asset_ids[1], ReviewDecisionValue.HOLD, None),
    )
    lock_version = 1
    for index, (asset_id, decision, reason_code) in enumerate(decisions, start=1):
        async with semantic_runtime_context.database.sessions() as session:
            result = await append_review_decision(
                session,
                review_task_id=task_id,
                asset_id=asset_id,
                decision=decision,
                decided_by_user_id=semantic_runtime_context.owner_id,
                expected_lock_version=lock_version,
                idempotency_key=f"late-learning-decision-{index}",
                reason_code=reason_code,
                semantic_profile_sha256=profile.profile_sha256,
                semantic_enforcement_mode=SemanticEnforcementMode.SHADOW,
                now=_NOW + timedelta(minutes=1, seconds=index),
            )
        lock_version = result.task_lock_version

    async with semantic_runtime_context.database.sessions() as session:
        await transition_review_task(
            session,
            review_task_id=task_id,
            target_state=ReviewTaskState.COMPLETED,
            changed_by_user_id=semantic_runtime_context.owner_id,
            expected_lock_version=lock_version,
            idempotency_key="late-learning-complete-review",
            semantic_profile_sha256=profile.profile_sha256,
            semantic_enforcement_mode=SemanticEnforcementMode.SHADOW,
            now=_NOW + timedelta(minutes=2),
        )

    async with semantic_runtime_context.database.sessions() as session:
        assert (
            await session.scalar(
                select(SemanticAnatomyFeedback.id)
                .join(
                    SemanticAssessment,
                    SemanticAssessment.id == SemanticAnatomyFeedback.semantic_assessment_id,
                )
                .where(SemanticAssessment.asset_id == accepted_asset_id)
            )
            is None
        )

    for minute in range(3, 8):
        await run_semantic_assessment_cycle(
            semantic_runtime_context.database.sessions,
            semantic_runtime_context.store,
            worker_id="semantic:late-learning",
            profile=profile,
            analyzer=_passing_assessment,
            max_assessments_per_profile=3,
            asset_allowlist=(),
            max_attempts=2,
            lease_seconds=120,
            retry_base_seconds=5,
            retry_max_seconds=30,
            now=_NOW + timedelta(minutes=minute),
        )

    refresh_calls: list[dict[str, object]] = []

    async def capture_refresh(_session: object, **kwargs: object) -> None:
        refresh_calls.append(kwargs)

    monkeypatch.setattr(
        learning_service,
        "refresh_semantic_calibration_artifact",
        capture_refresh,
    )
    async with semantic_runtime_context.database.sessions() as session:
        reconciled = await reconcile_one_completed_semantic_review(
            session,
            profile_sha256=profile.profile_sha256,
            now=_NOW + timedelta(minutes=8),
        )
        await session.commit()

    assert reconciled.review_task_id == task_id
    assert reconciled.did_work
    assert reconciled.learning is not None
    assert reconciled.learning.inferred_good_count == 1
    assert reconciled.learning.inferred_defect_count == 0
    assert len(refresh_calls) == 1

    async with semantic_runtime_context.database.sessions() as session:
        feedback = list(
            (
                await session.scalars(
                    select(SemanticAnatomyFeedback)
                    .join(
                        SemanticAssessment,
                        SemanticAssessment.id == SemanticAnatomyFeedback.semantic_assessment_id,
                    )
                    .where(SemanticAssessment.scoring_run_id == scoring_run_id)
                )
            ).all()
        )
    assert len(feedback) == 1
    assert feedback[0].asset_id == accepted_asset_id
    assert feedback[0].ground_truth == SemanticGroundTruth.ANATOMY_GOOD

    # The unique feedback identity plus candidate exclusion makes retries safe;
    # generic composition rejects and holds never become anatomy labels.
    async with semantic_runtime_context.database.sessions() as session:
        repeated = await reconcile_one_completed_semantic_review(
            session,
            profile_sha256=profile.profile_sha256,
            now=_NOW + timedelta(minutes=9),
        )
        await session.commit()
    assert not repeated.did_work
    assert len(refresh_calls) == 1
