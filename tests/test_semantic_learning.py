from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import func, select

from gen_automation.db.models import (
    AdminUser,
    SemanticLearningPolicy,
    SemanticModelPromotion,
    SemanticTrainingRun,
)
from gen_automation.db.session import Database
from gen_automation.domain.canonical import canonical_sha256
from gen_automation.domain.enums import (
    AdminRole,
    SemanticGroundTruth,
    SemanticPromotionDecision,
    SemanticTrainingKind,
    SemanticTrainingState,
    SemanticVerdict,
)
from gen_automation.services import semantic_learning as learning_service
from gen_automation.services.semantic_learning import (
    claim_meta_training_run,
    enqueue_ready_meta_training_runs,
    ensure_semantic_learning_policy,
    load_effective_semantic_meta_model,
    process_claimed_meta_training_run,
    rollback_semantic_meta_model,
    update_semantic_learning_policy,
)
from gen_automation.services.semantic_learning_readiness import (
    SOURCE_EXPLICIT,
    SemanticLearningSample,
)
from gen_automation.services.semantic_meta_classifier import (
    SemanticMetaDatasetSplit,
    SemanticMetaTrainingParameters,
    compare_semantic_meta_challenger,
    evaluate_semantic_meta_predictions,
    fit_semantic_meta_classifier,
)

NOW = datetime(2026, 8, 6, 10, 0, tzinfo=UTC)
OWNER_ID = UUID("10000000-0000-4000-8000-000000000001")
PROFILE_SHA256 = "a" * 64


@dataclass(frozen=True, slots=True)
class LearningContext:
    database: Database
    owner_id: UUID


@pytest.fixture
async def learning_context(tmp_path: Path) -> AsyncIterator[LearningContext]:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'learning.db').as_posix()}")
    await database.create_schema()
    async with database.sessions() as session:
        session.add(
            AdminUser(
                id=OWNER_ID,
                username_normalized="owner",
                display_name="Owner",
                password_hash="disabled-test-password-hash",  # noqa: S106
                role=AdminRole.OWNER,
                is_active=True,
                failed_login_count=0,
                password_changed_at=NOW,
                credential_version=1,
                lock_version=1,
            )
        )
        await session.commit()
    try:
        yield LearningContext(database=database, owner_id=OWNER_ID)
    finally:
        await database.dispose()


def _learning_samples() -> tuple[SemanticLearningSample, ...]:
    samples: list[SemanticLearningSample] = []
    index = 0
    for group, (good_count, defect_count) in enumerate(
        ((150, 75), (150, 75), (100, 50), (100, 50))
    ):
        for defect in (False, True):
            count = defect_count if defect else good_count
            for _offset in range(count):
                index += 1
                samples.append(
                    SemanticLearningSample(
                        feedback_id=UUID(int=10_000 + index),
                        assessment_id=UUID(int=20_000 + index),
                        asset_id=UUID(int=30_000 + index),
                        feedback_by_user_id=OWNER_ID,
                        profile_sha256=PROFILE_SHA256,
                        asset_sha256=f"{index:064x}",
                        ground_truth=(
                            SemanticGroundTruth.ANATOMY_DEFECT
                            if defect
                            else SemanticGroundTruth.ANATOMY_GOOD
                        ),
                        owner_issue_code=None,
                        source=SOURCE_EXPLICIT,
                        verdict=(SemanticVerdict.SEVERE if defect else SemanticVerdict.PASS),
                        confidence_micros=900_000 if defect else 100_000,
                        predicted_issues=(),
                        release_id=UUID(int=40_000 + group),
                        generation_job_id=UUID(int=50_000 + (index % 5)),
                        generated_at=NOW + timedelta(days=group),
                        labeled_at=NOW + timedelta(days=group, minutes=index),
                        completed_review=True,
                    )
                )
    return tuple(samples)


def _training_output(
    split: SemanticMetaDatasetSplit,
    *,
    promote: bool,
) -> learning_service.SemanticMetaTrainingOutput:
    fitted = fit_semantic_meta_classifier(
        split,
        parameters=SemanticMetaTrainingParameters(iterations=1),
    )
    model = replace(
        fitted,
        keep_threshold_micros=100_000 if promote else 0,
        reject_threshold_micros=900_000 if promote else 1_000_000,
    )
    challenger_probabilities = {
        example.sample_id: (
            (1_000_000 if example.is_defect else 0) if promote else 500_000
        )
        for example in split.holdout
    }
    challenger = evaluate_semantic_meta_predictions(
        split.holdout,
        challenger_probabilities,
        keep_threshold_micros=model.keep_threshold_micros,
        reject_threshold_micros=model.reject_threshold_micros,
        model_sha256=model.artifact_sha256,
    )
    champion = evaluate_semantic_meta_predictions(
        split.holdout,
        {example.sample_id: 500_000 for example in split.holdout},
        keep_threshold_micros=0,
        reject_threshold_micros=1_000_000,
        model_sha256="f" * 64,
    )
    promotion = compare_semantic_meta_challenger(champion, challenger)
    assert promotion.promote is promote
    evaluation_report = {
        "schema_version": "semantic-meta-evaluation/v1",
        "holdout_sha256": challenger.evaluation_sha256,
        "champion": {"identity": "test-champion", **asdict(champion)},
        "challenger": asdict(challenger),
        "promotion": asdict(promotion),
    }
    return learning_service.SemanticMetaTrainingOutput(
        model=model,
        challenger=challenger,
        champion=champion,
        promotion=promotion,
        model_payload=json.loads(model.serialize()),
        evaluation_report=evaluation_report,
        evaluation_sha256=canonical_sha256(evaluation_report),
    )


async def _queue_and_claim(
    context: LearningContext,
    monkeypatch: pytest.MonkeyPatch,
    *,
    samples: tuple[SemanticLearningSample, ...],
    auto_promote: bool = True,
) -> learning_service.ClaimedSemanticTrainingRun:
    async def load_samples(
        *_args: object,
        **_kwargs: object,
    ) -> tuple[SemanticLearningSample, ...]:
        return samples

    monkeypatch.setattr(learning_service, "load_semantic_learning_samples", load_samples)
    now = datetime.now(UTC)
    async with context.database.sessions() as session:
        policy, _created = await ensure_semantic_learning_policy(
            session,
            owner_user_id=context.owner_id,
            now=now,
        )
        policy.auto_promote_validated = auto_promote
        queued = await enqueue_ready_meta_training_runs(session, now=now)
        claimed = await claim_meta_training_run(
            session,
            worker_id="lifecycle-test-worker",
            lease_seconds=600,
            now=now,
        )
        await session.commit()
    assert queued == 1
    assert claimed is not None
    return claimed


@pytest.mark.asyncio
async def test_standing_policy_is_enabled_once_and_updates_with_cas(
    learning_context: LearningContext,
) -> None:
    async with learning_context.database.sessions() as session:
        policy, created = await ensure_semantic_learning_policy(
            session,
            owner_user_id=learning_context.owner_id,
            now=NOW,
        )
        repeated, repeated_created = await ensure_semantic_learning_policy(
            session,
            owner_user_id=learning_context.owner_id,
            now=NOW,
        )
        assert created
        assert not repeated_created
        assert repeated.owner_user_id == policy.owner_user_id
        assert policy.learning_enabled
        assert policy.auto_train_meta
        assert policy.auto_train_visual
        assert policy.auto_promote_validated

        updated = await update_semantic_learning_policy(
            session,
            owner_user_id=learning_context.owner_id,
            expected_lock_version=1,
            learning_enabled=True,
            auto_train_meta=True,
            auto_train_visual=False,
            auto_promote_validated=True,
            max_visual_run_microusd=5_000_000,
            minimum_new_labels_for_retrain=75,
            now=NOW + timedelta(minutes=1),
        )
        await session.commit()

    assert updated.lock_version == 2
    assert not updated.auto_train_visual
    assert updated.max_visual_run_microusd == 5_000_000
    assert updated.minimum_new_labels_for_retrain == 75


@pytest.mark.asyncio
async def test_ready_snapshot_queues_one_free_idempotent_cpu_run(
    learning_context: LearningContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    samples = _learning_samples()

    async def load_samples(
        *_args: object,
        **_kwargs: object,
    ) -> tuple[SemanticLearningSample, ...]:
        return samples

    monkeypatch.setattr(learning_service, "load_semantic_learning_samples", load_samples)
    async with learning_context.database.sessions() as session:
        await ensure_semantic_learning_policy(
            session,
            owner_user_id=learning_context.owner_id,
            now=NOW,
        )
        first = await enqueue_ready_meta_training_runs(session, now=NOW)
        second = await enqueue_ready_meta_training_runs(session, now=NOW)
        await session.commit()
        run = await session.scalar(select(SemanticTrainingRun))
        count = await session.scalar(select(func.count(SemanticTrainingRun.id)))

    assert first == 1
    assert second == 0
    assert count == 1
    assert run is not None
    assert run.kind == SemanticTrainingKind.META_CLASSIFIER
    assert run.state == SemanticTrainingState.QUEUED
    assert run.estimated_cost_microusd == 0
    assert run.actual_cost_microusd == 0
    assert run.split_manifest_sha256
    assert run.training_config["binary_labeled_count"] == 750


@pytest.mark.asyncio
async def test_claim_is_bounded_and_lease_owned(
    learning_context: LearningContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    samples = _learning_samples()

    async def load_samples(
        *_args: object,
        **_kwargs: object,
    ) -> tuple[SemanticLearningSample, ...]:
        return samples

    monkeypatch.setattr(learning_service, "load_semantic_learning_samples", load_samples)
    async with learning_context.database.sessions() as session:
        await ensure_semantic_learning_policy(
            session,
            owner_user_id=learning_context.owner_id,
            now=NOW,
        )
        await enqueue_ready_meta_training_runs(session, now=NOW)
        claimed = await claim_meta_training_run(
            session,
            worker_id="test-worker",
            lease_seconds=600,
            now=NOW,
        )
        await session.commit()
        policy = await session.get(SemanticLearningPolicy, learning_context.owner_id)
        run = await session.get(SemanticTrainingRun, claimed.run_id if claimed else UUID(int=0))

    assert policy is not None
    assert claimed is not None
    assert claimed.attempt == 1
    assert claimed.lease_expires_at == NOW + timedelta(seconds=600)
    assert run is not None
    assert run.state == SemanticTrainingState.PREPARING
    assert run.lease_owner == "test-worker"


@pytest.mark.asyncio
async def test_claimed_training_persists_and_promotes_a_loadable_model(
    learning_context: LearningContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claimed = await _queue_and_claim(
        learning_context,
        monkeypatch,
        samples=_learning_samples(),
    )

    def fit_for_test(
        split: SemanticMetaDatasetSplit,
        *_args: object,
    ) -> learning_service.SemanticMetaTrainingOutput:
        return _training_output(split, promote=True)

    monkeypatch.setattr(learning_service, "_fit_and_evaluate_meta_run", fit_for_test)
    await process_claimed_meta_training_run(
        learning_context.database.sessions,
        claimed=claimed,
    )

    async with learning_context.database.sessions() as session:
        run = await session.get(SemanticTrainingRun, claimed.run_id)
        promotion = await session.scalar(select(SemanticModelPromotion))
        effective = await load_effective_semantic_meta_model(
            session,
            owner_user_id=learning_context.owner_id,
            profile_sha256=PROFILE_SHA256,
        )

    assert run is not None
    assert run.state == SemanticTrainingState.SUCCEEDED
    assert run.actual_cost_microusd == 0
    assert run.lease_owner is None
    assert run.model_payload is not None
    assert run.evaluation_report is not None
    assert run.evaluation_sha256 == canonical_sha256(run.evaluation_report)
    assert promotion is not None
    assert promotion.decision == SemanticPromotionDecision.PROMOTED
    assert promotion.training_run_id == run.id
    assert promotion.previous_training_run_id is None
    assert effective is not None
    assert effective.artifact_sha256 == run.artifact_sha256
    assert effective.owner_user_id == str(learning_context.owner_id)
    assert effective.profile_sha256 == PROFILE_SHA256


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("challenger_is_valid", "auto_promote", "reason_fragment"),
    (
        (False, True, "below the safety target"),
        (True, False, "disabled by the owner policy"),
    ),
)
async def test_rejected_or_owner_disabled_challenger_never_becomes_effective(
    learning_context: LearningContext,
    monkeypatch: pytest.MonkeyPatch,
    challenger_is_valid: bool,
    auto_promote: bool,
    reason_fragment: str,
) -> None:
    claimed = await _queue_and_claim(
        learning_context,
        monkeypatch,
        samples=_learning_samples(),
        auto_promote=auto_promote,
    )

    def fit_for_test(
        split: SemanticMetaDatasetSplit,
        *_args: object,
    ) -> learning_service.SemanticMetaTrainingOutput:
        return _training_output(split, promote=challenger_is_valid)

    monkeypatch.setattr(learning_service, "_fit_and_evaluate_meta_run", fit_for_test)
    await process_claimed_meta_training_run(
        learning_context.database.sessions,
        claimed=claimed,
    )

    async with learning_context.database.sessions() as session:
        run = await session.get(SemanticTrainingRun, claimed.run_id)
        promotion = await session.scalar(select(SemanticModelPromotion))
        effective = await load_effective_semantic_meta_model(
            session,
            owner_user_id=learning_context.owner_id,
            profile_sha256=PROFILE_SHA256,
        )

    assert run is not None
    assert run.state == SemanticTrainingState.SUCCEEDED
    assert run.artifact_sha256 is not None
    assert promotion is not None
    assert promotion.decision == SemanticPromotionDecision.REJECTED
    assert reason_fragment in promotion.reason
    assert effective is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "identity_field",
    ("owner_user_id", "profile_sha256", "training_dataset_sha256", "split_manifest_sha256"),
)
async def test_training_output_with_wrong_identity_fails_closed_before_promotion(
    learning_context: LearningContext,
    monkeypatch: pytest.MonkeyPatch,
    identity_field: str,
) -> None:
    claimed = await _queue_and_claim(
        learning_context,
        monkeypatch,
        samples=_learning_samples(),
    )

    def fit_for_test(
        split: SemanticMetaDatasetSplit,
        *_args: object,
    ) -> learning_service.SemanticMetaTrainingOutput:
        output = _training_output(split, promote=True)
        if identity_field == "owner_user_id":
            mismatched = replace(
                output.model,
                owner_user_id=str(UUID("20000000-0000-4000-8000-000000000002")),
            )
        elif identity_field == "profile_sha256":
            mismatched = replace(output.model, profile_sha256="c" * 64)
        elif identity_field == "training_dataset_sha256":
            mismatched = replace(output.model, training_dataset_sha256="d" * 64)
        else:
            mismatched = replace(output.model, split_manifest_sha256="e" * 64)
        return replace(
            output,
            model=mismatched,
            model_payload=json.loads(mismatched.serialize()),
        )

    monkeypatch.setattr(learning_service, "_fit_and_evaluate_meta_run", fit_for_test)
    await process_claimed_meta_training_run(
        learning_context.database.sessions,
        claimed=claimed,
    )

    async with learning_context.database.sessions() as session:
        run = await session.get(SemanticTrainingRun, claimed.run_id)
        promotion_count = await session.scalar(
            select(func.count(SemanticModelPromotion.id))
        )

    assert run is not None
    assert run.state == SemanticTrainingState.FAILED
    assert run.artifact_sha256 is None
    assert run.last_error_code == "semantic_training_contract_error"
    assert promotion_count == 0


@pytest.mark.asyncio
async def test_owner_can_roll_back_to_a_previously_active_model(
    learning_context: LearningContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claimed = await _queue_and_claim(
        learning_context,
        monkeypatch,
        samples=_learning_samples(),
    )

    def fit_for_test(
        split: SemanticMetaDatasetSplit,
        *_args: object,
    ) -> learning_service.SemanticMetaTrainingOutput:
        return _training_output(split, promote=True)

    monkeypatch.setattr(learning_service, "_fit_and_evaluate_meta_run", fit_for_test)
    await process_claimed_meta_training_run(
        learning_context.database.sessions,
        claimed=claimed,
    )

    async with learning_context.database.sessions() as session:
        first_run = await session.get(SemanticTrainingRun, claimed.run_id)
        first_promotion = await session.scalar(select(SemanticModelPromotion))
        first_model = await load_effective_semantic_meta_model(
            session,
            owner_user_id=learning_context.owner_id,
            profile_sha256=PROFILE_SHA256,
        )
        assert first_run is not None
        assert first_promotion is not None
        assert first_model is not None
        second_model = replace(first_model, intercept=first_model.intercept + 0.125)
        second_report = {"schema_version": "test-evaluation/v1", "sequence": 2}
        second_completed_at = datetime.now(UTC) + timedelta(minutes=1)
        second_run = SemanticTrainingRun(
            owner_user_id=learning_context.owner_id,
            kind=SemanticTrainingKind.META_CLASSIFIER,
            state=SemanticTrainingState.SUCCEEDED,
            profile_sha256=PROFILE_SHA256,
            dataset_sha256="b" * 64,
            dataset_schema_version=first_run.dataset_schema_version,
            split_manifest=first_run.split_manifest,
            split_manifest_sha256=first_run.split_manifest_sha256,
            training_config={"schema_version": "test-training/v1", "sequence": 2},
            training_config_sha256=canonical_sha256(
                {"schema_version": "test-training/v1", "sequence": 2}
            ),
            attempts=1,
            max_attempts=3,
            available_at=second_completed_at,
            artifact_sha256=second_model.artifact_sha256,
            model_payload=json.loads(second_model.serialize()),
            evaluation_report=second_report,
            evaluation_sha256=canonical_sha256(second_report),
            estimated_cost_microusd=0,
            actual_cost_microusd=0,
            created_at=second_completed_at,
            started_at=second_completed_at,
            completed_at=second_completed_at,
        )
        session.add(second_run)
        await session.flush()
        session.add(
            SemanticModelPromotion(
                owner_user_id=learning_context.owner_id,
                kind=SemanticTrainingKind.META_CLASSIFIER,
                training_run_id=second_run.id,
                previous_training_run_id=first_run.id,
                profile_sha256=PROFILE_SHA256,
                artifact_sha256=second_model.artifact_sha256,
                dataset_sha256=second_run.dataset_sha256,
                evaluation_sha256=second_run.evaluation_sha256,
                decision=SemanticPromotionDecision.PROMOTED,
                keep_threshold_micros=second_model.keep_threshold_micros,
                reject_threshold_micros=second_model.reject_threshold_micros,
                reason="Validated second challenger.",
                created_by_user_id=learning_context.owner_id,
                created_at=second_completed_at,
            )
        )
        await session.commit()

    async with learning_context.database.sessions() as session:
        rollback = await rollback_semantic_meta_model(
            session,
            owner_user_id=learning_context.owner_id,
            actor_user_id=learning_context.owner_id,
            profile_sha256=PROFILE_SHA256,
            reason="Return to the last known-good model.",
            now=datetime.now(UTC) + timedelta(minutes=2),
        )
        await session.commit()
        effective = await load_effective_semantic_meta_model(
            session,
            owner_user_id=learning_context.owner_id,
            profile_sha256=PROFILE_SHA256,
        )

    assert rollback.decision == SemanticPromotionDecision.ROLLED_BACK
    assert rollback.training_run_id == first_run.id
    assert rollback.previous_training_run_id == second_run.id
    assert effective is not None
    assert effective.artifact_sha256 == first_model.artifact_sha256
