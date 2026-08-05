from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from gen_automation.db.models import (
    AdminUser,
    Asset,
    AssetRanking,
    AssetScore,
    AuditEvent,
    GenerationJob,
    IdempotencyRecord,
    Project,
    Release,
    ReleaseSelection,
    ReleaseVersion,
    ReviewAssetInspection,
    ReviewDecision,
    ReviewTask,
    ReviewXSelection,
    ScoringRun,
    SemanticAnatomyFeedback,
    SemanticAssessment,
)
from gen_automation.db.session import Database
from gen_automation.domain.enums import (
    AdminRole,
    AssetKind,
    AssetScoreState,
    AssetState,
    GenerationState,
    RankingDisposition,
    ReleasePhase,
    ReviewBulkAction,
    ReviewDecisionValue,
    ReviewTaskState,
    ScoringRunState,
    SemanticAssessmentState,
    SemanticEnforcementMode,
    SemanticGroundTruth,
    SemanticVerdict,
)
from gen_automation.semantic import prompt_sha256, schema_sha256
from gen_automation.services.ranking_manifest import ranking_manifest_sha256
from gen_automation.services.review import (
    SEMANTIC_SEVERE_OVERRIDE_AUDIT_ACTION,
    SEMANTIC_SEVERE_OVERRIDE_REASON_CODE,
    SORTING_DEFAULT_ACCEPT_REASON_CODE,
    ReviewConflictError,
    ReviewNotFoundError,
    ReviewTaskResult,
    append_review_decision,
    apply_bulk_review_action,
    create_review_task,
    get_review_summary,
    transition_review_task,
)
from gen_automation.services.review_inspections import record_review_inspections
from gen_automation.services.semantic_anatomy import SemanticAssessmentProfile
from gen_automation.services.semantic_review_reconciliation import (
    reconcile_one_completed_semantic_review,
)

NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class ReviewContext:
    database: Database
    release_id: UUID
    release_version_id: UUID
    scoring_run_id: UUID
    reviewer_id: UUID
    second_reviewer_id: UUID
    owner_id: UUID
    admin_id: UUID
    publisher_id: UUID
    ranked_asset_ids: tuple[UUID, ...]
    unranked_asset_id: UUID


def _admin(
    *,
    username: str,
    now: datetime,
    role: AdminRole = AdminRole.REVIEWER,
) -> AdminUser:
    return AdminUser(
        username_normalized=username,
        display_name=username.title(),
        password_hash="disabled-review-test-password-hash",  # noqa: S106
        role=role,
        is_active=True,
        failed_login_count=0,
        password_changed_at=now,
        credential_version=1,
        lock_version=1,
    )


def _raw_asset(
    *,
    asset_id: UUID,
    release_id: UUID,
    job_id: UUID,
    output_index: int,
) -> Asset:
    return Asset(
        id=asset_id,
        release_id=release_id,
        generation_job_id=job_id,
        output_index=output_index,
        kind=AssetKind.RAW_MASTER,
        state=AssetState.AVAILABLE,
        storage_backend="s3",
        storage_bucket="review-test",
        object_key=f"raw/{asset_id}.png",
        object_version_id=f"version-{asset_id}",
        sha256=f"{output_index + 1:064x}",
        content_type="image/png",
        image_format="PNG",
        width=1024,
        height=1024,
        byte_size=1_024 + output_index,
        asset_metadata={"immutable": True},
        available_at=NOW,
    )


@pytest.fixture
async def review_context(tmp_path: Path) -> AsyncIterator[ReviewContext]:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'review.db').as_posix()}")
    await database.create_schema()
    ranked_asset_ids = (
        UUID("10000000-0000-4000-8000-000000000001"),
        UUID("10000000-0000-4000-8000-000000000002"),
        UUID("10000000-0000-4000-8000-000000000003"),
    )
    unranked_asset_id = UUID("10000000-0000-4000-8000-000000000004")
    async with database.sessions() as session:
        reviewer = _admin(username="reviewer-one", now=NOW)
        second_reviewer = _admin(username="reviewer-two", now=NOW)
        owner = _admin(username="owner", now=NOW, role=AdminRole.OWNER)
        admin = _admin(username="admin", now=NOW, role=AdminRole.ADMIN)
        publisher = _admin(
            username="publisher",
            now=NOW,
            role=AdminRole.PUBLISHER,
        )
        project = Project(slug="review", name="Review")
        session.add_all([reviewer, second_reviewer, owner, admin, publisher, project])
        await session.flush()
        release = Release(
            project_id=project.id,
            slug="release",
            title="Review release",
            phase=ReleasePhase.REVIEWING,
            current_version_no=1,
            desired_accepted_count=2,
            lock_version=1,
        )
        session.add(release)
        await session.flush()
        version = ReleaseVersion(
            release_id=release.id,
            version_no=1,
            specification={"schema_version": 1},
            specification_sha256="a" * 64,
            created_by="test",
            created_at=NOW,
        )
        session.add(version)
        await session.flush()
        job = GenerationJob(
            release_version_id=version.id,
            logical_key="b" * 64,
            parameters={"batch": 4},
            parameters_sha256="c" * 64,
            provider="salad",
            state=GenerationState.SUCCEEDED,
            priority=100,
            expected_output_count=4,
            attempt_count=1,
            max_attempts=3,
            lock_version=1,
        )
        session.add(job)
        await session.flush()
        assets = [
            _raw_asset(
                asset_id=asset_id,
                release_id=release.id,
                job_id=job.id,
                output_index=index,
            )
            for index, asset_id in enumerate((*ranked_asset_ids, unranked_asset_id))
        ]
        session.add_all(assets)
        await session.flush()
        scoring_run = ScoringRun(
            release_version_id=version.id,
            configuration={"quality": "v1"},
            config_sha256="d" * 64,
            input_manifest_sha256="e" * 64,
            scorer_version="test-scorer-v1",
            pillow_version="12.0.0",
            state=ScoringRunState.RUNNING,
            asset_count=3,
            max_attempts=3,
            created_at=NOW,
            started_at=NOW,
            completed_at=None,
        )
        session.add(scoring_run)
        await session.flush()
        ranking_rows: list[tuple[AssetRanking, AssetScore]] = []
        for rank, asset in enumerate(assets[:3], start=1):
            aggregate_score_micros = 1_000_000 - rank
            score = AssetScore(
                scoring_run_id=scoring_run.id,
                asset_id=asset.id,
                asset_storage_backend=asset.storage_backend,
                asset_storage_bucket=asset.storage_bucket,
                asset_sha256=asset.sha256,
                asset_object_key=asset.object_key,
                asset_object_version_id=asset.object_version_id,
                asset_byte_size=asset.byte_size,
                asset_image_format=asset.image_format,
                asset_width=asset.width,
                asset_height=asset.height,
                state=AssetScoreState.FLAGGED_CORRUPT,
                attempts=1,
                max_attempts=3,
                available_at=NOW,
                aggregate_score_micros=aggregate_score_micros,
                signal_detail={"test": True},
                scorer_version=scoring_run.scorer_version,
                pillow_version=scoring_run.pillow_version,
                config_sha256=scoring_run.config_sha256,
                completed_at=NOW + timedelta(minutes=1),
                created_at=NOW,
            )
            session.add(score)
            await session.flush()
            ranking = AssetRanking(
                scoring_run_id=scoring_run.id,
                asset_score_id=score.id,
                asset_id=asset.id,
                rank=rank,
                aggregate_score_micros=aggregate_score_micros,
                disposition=RankingDisposition.FLAGGED_REVIEW,
                explanation={"rank": rank},
                is_duplicate_representative=False,
                scorer_version=scoring_run.scorer_version,
                pillow_version=scoring_run.pillow_version,
                config_sha256=scoring_run.config_sha256,
                frozen_at=NOW + timedelta(minutes=1),
            )
            session.add(ranking)
            ranking_rows.append((ranking, score))
        await session.flush()
        scoring_run.ranking_manifest_sha256 = ranking_manifest_sha256(
            scoring_run,
            ranking_rows,
        )
        scoring_run.state = ScoringRunState.COMPLETED
        scoring_run.completed_at = NOW + timedelta(minutes=1)
        await session.commit()
        context = ReviewContext(
            database=database,
            release_id=release.id,
            release_version_id=version.id,
            scoring_run_id=scoring_run.id,
            reviewer_id=reviewer.id,
            second_reviewer_id=second_reviewer.id,
            owner_id=owner.id,
            admin_id=admin.id,
            publisher_id=publisher.id,
            ranked_asset_ids=ranked_asset_ids,
            unranked_asset_id=unranked_asset_id,
        )
    try:
        yield context
    finally:
        await database.dispose()


async def _create_task(
    context: ReviewContext,
    *,
    key: str = "create-review-task",
) -> ReviewTaskResult:
    async with context.database.sessions() as session:
        return await create_review_task(
            session,
            scoring_run_id=context.scoring_run_id,
            created_by_user_id=context.reviewer_id,
            idempotency_key=key,
            now=NOW + timedelta(minutes=2),
        )


async def _seed_terminal_semantic_assessments(
    context: ReviewContext,
    *,
    profile: SemanticAssessmentProfile,
) -> None:
    async with context.database.sessions() as session:
        scores = {
            score.asset_id: score
            for score in (
                await session.scalars(
                    select(AssetScore).where(AssetScore.scoring_run_id == context.scoring_run_id)
                )
            ).all()
        }
        assets = {
            asset.id: asset
            for asset in (
                await session.scalars(select(Asset).where(Asset.id.in_(context.ranked_asset_ids)))
            ).all()
        }
        for index, asset_id in enumerate(context.ranked_asset_ids):
            score = scores[asset_id]
            asset = assets[asset_id]
            common = {
                "scoring_run_id": context.scoring_run_id,
                "asset_score_id": score.id,
                "asset_id": asset_id,
                "asset_storage_backend": score.asset_storage_backend,
                "asset_storage_bucket": score.asset_storage_bucket,
                "asset_object_key": score.asset_object_key,
                "asset_object_version_id": score.asset_object_version_id,
                "asset_sha256": score.asset_sha256,
                "asset_content_type": asset.content_type,
                "asset_byte_size": score.asset_byte_size,
                "profile_sha256": profile.profile_sha256,
                "model_name": profile.model_name,
                "model_revision": profile.model_revision,
                "prompt_sha256": prompt_sha256(),
                "schema_sha256": schema_sha256(),
                "attempts": 3 if index == 2 else 1,
                "max_attempts": 3,
                "available_at": NOW,
                "created_at": NOW,
                "started_at": NOW,
                "completed_at": NOW + timedelta(minutes=3),
            }
            if index == 0:
                session.add(
                    SemanticAssessment(
                        **common,
                        state=SemanticAssessmentState.COMPLETED,
                        verdict=SemanticVerdict.SEVERE,
                        confidence_micros=960_000,
                        issues=[
                            {
                                "code": "extra_limb",
                                "confidence_micros": 980_000,
                            }
                        ],
                        response_sha256="1" * 64,
                    )
                )
            elif index == 1:
                session.add(
                    SemanticAssessment(
                        **common,
                        state=SemanticAssessmentState.COMPLETED,
                        verdict=SemanticVerdict.PASS,
                        confidence_micros=990_000,
                        issues=[],
                        response_sha256="2" * 64,
                    )
                )
            else:
                session.add(
                    SemanticAssessment(
                        **common,
                        state=SemanticAssessmentState.UNAVAILABLE,
                        last_error_code="semantic_service_unavailable",
                        last_error_detail="Unavailable after bounded retries.",
                    )
                )
        await session.commit()


@pytest.mark.asyncio
async def test_review_inspections_union_batches_without_changing_task_lock(
    review_context: ReviewContext,
) -> None:
    task = await _create_task(review_context, key="create-inspection-review")
    first_two = review_context.ranked_asset_ids[:2]
    async with review_context.database.sessions() as session:
        first = await record_review_inspections(
            session,
            review_task_id=task.task_id,
            asset_ids=first_two,
            inspected_by_user_id=review_context.reviewer_id,
            now=NOW + timedelta(minutes=3),
        )
    assert first.inspected_asset_ids == first_two
    assert first.created_count == 2

    overlap = review_context.ranked_asset_ids[1:]
    async with review_context.database.sessions() as session:
        second = await record_review_inspections(
            session,
            review_task_id=task.task_id,
            asset_ids=overlap,
            inspected_by_user_id=review_context.reviewer_id,
            now=NOW + timedelta(minutes=4),
        )
        stored = set(
            await session.scalars(
                select(ReviewAssetInspection.asset_id).where(
                    ReviewAssetInspection.review_task_id == task.task_id,
                    ReviewAssetInspection.inspected_by_user_id
                    == review_context.reviewer_id,
                )
            )
        )
        persisted_task = await session.get(ReviewTask, task.task_id)

    assert second.inspected_asset_ids == overlap
    assert second.created_count == 1
    assert stored == set(review_context.ranked_asset_ids)
    assert persisted_task is not None and persisted_task.lock_version == 1

    async with review_context.database.sessions() as session:
        replay = await record_review_inspections(
            session,
            review_task_id=task.task_id,
            asset_ids=overlap,
            inspected_by_user_id=review_context.reviewer_id,
            now=NOW + timedelta(minutes=5),
        )
    assert replay.created_count == 0


@pytest.mark.asyncio
async def test_review_inspections_reject_nonmembers_and_terminal_tasks(
    review_context: ReviewContext,
) -> None:
    task = await _create_task(review_context, key="create-bounded-inspection-review")
    async with review_context.database.sessions() as session:
        with pytest.raises(ReviewConflictError, match="not part of the review ranking"):
            await record_review_inspections(
                session,
                review_task_id=task.task_id,
                asset_ids=(review_context.unranked_asset_id,),
                inspected_by_user_id=review_context.reviewer_id,
                now=NOW + timedelta(minutes=3),
            )
        await session.rollback()

    async with review_context.database.sessions() as session:
        await transition_review_task(
            session,
            review_task_id=task.task_id,
            target_state=ReviewTaskState.CANCELLED,
            changed_by_user_id=review_context.reviewer_id,
            expected_lock_version=1,
            idempotency_key="cancel-inspection-review",
            now=NOW + timedelta(minutes=4),
        )

    async with review_context.database.sessions() as session:
        with pytest.raises(ReviewConflictError, match="review task is not open"):
            await record_review_inspections(
                session,
                review_task_id=task.task_id,
                asset_ids=(review_context.ranked_asset_ids[0],),
                inspected_by_user_id=review_context.reviewer_id,
                now=NOW + timedelta(minutes=5),
            )


@pytest.mark.asyncio
async def test_create_task_snapshots_frozen_run_and_naturally_replays(
    review_context: ReviewContext,
) -> None:
    first = await _create_task(review_context)
    second = await _create_task(review_context, key="create-review-task-alias")

    assert first.task_id == second.task_id
    assert first.replayed is False
    assert second.replayed is True
    assert first.release_version_id == review_context.release_version_id
    assert first.desired_accepted_count == 2
    assert first.ranked_asset_count == 3
    assert first.state == ReviewTaskState.OPEN
    assert first.lock_version == 1

    async with review_context.database.sessions() as session:
        release = await session.get(Release, review_context.release_id)
        assert release is not None
        release.desired_accepted_count = 1
        await session.commit()

    third = await _create_task(review_context, key="create-after-release-edit")
    assert third.task_id == first.task_id
    assert third.desired_accepted_count == 2

    async with review_context.database.sessions() as session:
        task = await session.get(ReviewTask, first.task_id)
        assert task is not None
        run = await session.get(ScoringRun, review_context.scoring_run_id)
        assert run is not None
        assert task.release_version_no == 1
        assert task.release_specification_sha256 == "a" * 64
        assert task.scoring_config_sha256 == "d" * 64
        assert task.scoring_input_manifest_sha256 == "e" * 64
        assert task.ranking_manifest_sha256 == run.ranking_manifest_sha256
        assert task.desired_accepted_count == 2
        assert int(await session.scalar(select(func.count()).select_from(ReviewTask)) or 0) == 1
        assert (
            int(
                await session.scalar(
                    select(func.count())
                    .select_from(AuditEvent)
                    .where(AuditEvent.action == "review.task_created")
                )
                or 0
            )
            == 1
        )
        assert (
            int(
                await session.scalar(
                    select(func.count())
                    .select_from(IdempotencyRecord)
                    .where(
                        IdempotencyRecord.scope
                        == f"scoring-run:{review_context.scoring_run_id}:create-review-task"
                    )
                )
                or 0
            )
            == 3
        )


@pytest.mark.asyncio
async def test_default_accept_creation_seeds_once_and_reject_supersedes_latest_choice(
    review_context: ReviewContext,
) -> None:
    async with review_context.database.sessions() as session:
        first = await create_review_task(
            session,
            scoring_run_id=review_context.scoring_run_id,
            created_by_user_id=review_context.reviewer_id,
            idempotency_key="create-default-kept-review",
            default_accept_ranked_assets=True,
            now=NOW + timedelta(minutes=2),
        )
    async with review_context.database.sessions() as session:
        replay = await create_review_task(
            session,
            scoring_run_id=review_context.scoring_run_id,
            created_by_user_id=review_context.reviewer_id,
            idempotency_key="create-default-kept-review",
            default_accept_ranked_assets=True,
            now=NOW + timedelta(minutes=3),
        )
    async with review_context.database.sessions() as session:
        alias_replay = await create_review_task(
            session,
            scoring_run_id=review_context.scoring_run_id,
            created_by_user_id=review_context.reviewer_id,
            idempotency_key="create-default-kept-review-alias",
            default_accept_ranked_assets=True,
            now=NOW + timedelta(minutes=4),
        )

    assert replay.task_id == first.task_id
    assert replay.replayed
    assert alias_replay.task_id == first.task_id
    assert alias_replay.replayed

    async with review_context.database.sessions() as session:
        summary = await get_review_summary(session, review_task_id=first.task_id)
        seeded = list(
            (
                await session.scalars(
                    select(ReviewDecision)
                    .where(ReviewDecision.review_task_id == first.task_id)
                    .order_by(ReviewDecision.asset_id)
                )
            ).all()
        )
        seed_audit_count = int(
            await session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.action == "review.default_acceptance_seeded")
            )
            or 0
        )

    assert summary.accepted_count == len(review_context.ranked_asset_ids)
    assert summary.rejected_count == 0
    assert summary.held_count == 0
    assert summary.undecided_count == 0
    assert summary.lock_version == 1
    assert len(seeded) == len(review_context.ranked_asset_ids)
    assert all(row.revision == 1 for row in seeded)
    assert all(row.decision == ReviewDecisionValue.ACCEPT for row in seeded)
    assert all(row.reason_code == SORTING_DEFAULT_ACCEPT_REASON_CODE for row in seeded)
    assert all(row.decided_by_user_id == review_context.reviewer_id for row in seeded)
    assert seed_audit_count == 1

    rejected_asset_id = review_context.ranked_asset_ids[0]
    original = next(row for row in seeded if row.asset_id == rejected_asset_id)
    async with review_context.database.sessions() as session:
        rejected = await append_review_decision(
            session,
            review_task_id=first.task_id,
            asset_id=rejected_asset_id,
            decision=ReviewDecisionValue.REJECT,
            decided_by_user_id=review_context.reviewer_id,
            expected_lock_version=1,
            idempotency_key="reject-default-kept-image",
            reason_code="composition",
            now=NOW + timedelta(minutes=5),
        )

    assert rejected.revision == 2
    assert rejected.supersedes_decision_id == original.id
    assert rejected.task_lock_version == 2

    async with review_context.database.sessions() as session:
        summary = await get_review_summary(session, review_task_id=first.task_id)
        revisions = list(
            (
                await session.scalars(
                    select(ReviewDecision)
                    .where(
                        ReviewDecision.review_task_id == first.task_id,
                        ReviewDecision.asset_id == rejected_asset_id,
                    )
                    .order_by(ReviewDecision.revision)
                )
            ).all()
        )

    assert summary.accepted_count == len(review_context.ranked_asset_ids) - 1
    assert summary.rejected_count == 1
    assert summary.undecided_count == 0
    assert [row.decision for row in revisions] == [
        ReviewDecisionValue.ACCEPT,
        ReviewDecisionValue.REJECT,
    ]


@pytest.mark.asyncio
async def test_internal_reason_codes_are_cleared_when_the_decision_is_incompatible(
    review_context: ReviewContext,
) -> None:
    async with review_context.database.sessions() as session:
        task = await create_review_task(
            session,
            scoring_run_id=review_context.scoring_run_id,
            created_by_user_id=review_context.reviewer_id,
            idempotency_key="create-reason-compatibility-review",
            default_accept_ranked_assets=True,
            now=NOW + timedelta(minutes=2),
        )

    asset_id = review_context.ranked_asset_ids[0]
    submissions = (
        (ReviewDecisionValue.REJECT, SORTING_DEFAULT_ACCEPT_REASON_CODE),
        (ReviewDecisionValue.ACCEPT, "anatomy"),
        (ReviewDecisionValue.REJECT, SEMANTIC_SEVERE_OVERRIDE_REASON_CODE),
    )
    for index, (decision, reason_code) in enumerate(submissions, start=1):
        async with review_context.database.sessions() as session:
            await append_review_decision(
                session,
                review_task_id=task.task_id,
                asset_id=asset_id,
                decision=decision,
                decided_by_user_id=review_context.reviewer_id,
                expected_lock_version=index,
                idempotency_key=f"reason-compatibility-{index}",
                reason_code=reason_code,
                now=NOW + timedelta(minutes=2 + index),
            )

    async with review_context.database.sessions() as session:
        revisions = list(
            (
                await session.scalars(
                    select(ReviewDecision)
                    .where(
                        ReviewDecision.review_task_id == task.task_id,
                        ReviewDecision.asset_id == asset_id,
                    )
                    .order_by(ReviewDecision.revision)
                )
            ).all()
        )

    assert [row.reason_code for row in revisions] == [
        SORTING_DEFAULT_ACCEPT_REASON_CODE,
        None,
        None,
        None,
    ]


@pytest.mark.asyncio
async def test_untouched_default_accepts_never_become_anatomy_good_training_labels(
    review_context: ReviewContext,
) -> None:
    profile = SemanticAssessmentProfile(
        model_name="Qwen/Qwen3-VL-8B-Instruct",
        model_revision="default-kept-learning-guard",
    )
    async with review_context.database.sessions() as session:
        release = await session.get(Release, review_context.release_id)
        assert release is not None
        release.desired_accepted_count = len(review_context.ranked_asset_ids)
        await session.commit()
    async with review_context.database.sessions() as session:
        task = await create_review_task(
            session,
            scoring_run_id=review_context.scoring_run_id,
            created_by_user_id=review_context.owner_id,
            idempotency_key="create-default-kept-learning-guard",
            default_accept_ranked_assets=True,
            now=NOW + timedelta(minutes=2),
        )
    await _seed_terminal_semantic_assessments(review_context, profile=profile)

    async with review_context.database.sessions() as session:
        completed = await transition_review_task(
            session,
            review_task_id=task.task_id,
            target_state=ReviewTaskState.COMPLETED,
            changed_by_user_id=review_context.owner_id,
            expected_lock_version=1,
            idempotency_key="complete-default-kept-learning-guard",
            semantic_profile_sha256=profile.profile_sha256,
            semantic_enforcement_mode=SemanticEnforcementMode.SHADOW,
            now=NOW + timedelta(minutes=4),
        )

    assert completed.state == ReviewTaskState.COMPLETED
    assert completed.accepted_count == len(review_context.ranked_asset_ids)

    async with review_context.database.sessions() as session:
        feedback_count = int(
            await session.scalar(
                select(func.count()).select_from(SemanticAnatomyFeedback)
            )
            or 0
        )
        reconciled = await reconcile_one_completed_semantic_review(
            session,
            profile_sha256=profile.profile_sha256,
            now=NOW + timedelta(minutes=5),
        )
        await session.commit()

    assert feedback_count == 0
    assert not reconciled.did_work
    assert reconciled.review_task_id is None


@pytest.mark.asyncio
async def test_inspected_default_accepts_become_positive_labels_on_completion(
    review_context: ReviewContext,
) -> None:
    profile = SemanticAssessmentProfile(
        model_name="Qwen/Qwen3-VL-8B-Instruct",
        model_revision="inspected-default-kept-learning",
    )
    async with review_context.database.sessions() as session:
        release = await session.get(Release, review_context.release_id)
        assert release is not None
        release.desired_accepted_count = len(review_context.ranked_asset_ids)
        await session.commit()
    async with review_context.database.sessions() as session:
        task = await create_review_task(
            session,
            scoring_run_id=review_context.scoring_run_id,
            created_by_user_id=review_context.owner_id,
            idempotency_key="create-inspected-default-kept-learning",
            default_accept_ranked_assets=True,
            now=NOW + timedelta(minutes=2),
        )
    await _seed_terminal_semantic_assessments(review_context, profile=profile)
    async with review_context.database.sessions() as session:
        await record_review_inspections(
            session,
            review_task_id=task.task_id,
            asset_ids=review_context.ranked_asset_ids,
            inspected_by_user_id=review_context.owner_id,
            now=NOW + timedelta(minutes=3),
        )
    async with review_context.database.sessions() as session:
        completed = await transition_review_task(
            session,
            review_task_id=task.task_id,
            target_state=ReviewTaskState.COMPLETED,
            changed_by_user_id=review_context.owner_id,
            expected_lock_version=1,
            idempotency_key="complete-inspected-default-kept-learning",
            semantic_profile_sha256=profile.profile_sha256,
            semantic_enforcement_mode=SemanticEnforcementMode.SHADOW,
            now=NOW + timedelta(minutes=4),
        )

    assert completed.state == ReviewTaskState.COMPLETED
    async with review_context.database.sessions() as session:
        feedback = list(
            (
                await session.scalars(
                    select(SemanticAnatomyFeedback).where(
                        SemanticAnatomyFeedback.feedback_by_user_id
                        == review_context.owner_id
                    )
                )
            ).all()
        )
    assert len(feedback) == 2
    assert all(item.ground_truth == SemanticGroundTruth.ANATOMY_GOOD for item in feedback)


@pytest.mark.asyncio
async def test_create_task_requires_completed_run_and_database_freezes_ranking(
    review_context: ReviewContext,
) -> None:
    async with review_context.database.sessions() as session:
        running = ScoringRun(
            release_version_id=review_context.release_version_id,
            configuration={"quality": "still-running"},
            config_sha256="f" * 64,
            input_manifest_sha256="9" * 64,
            scorer_version="test-scorer-v1",
            pillow_version="12.0.0",
            state=ScoringRunState.RUNNING,
            asset_count=3,
            max_attempts=3,
            created_at=NOW,
            started_at=NOW,
            completed_at=None,
        )
        session.add(running)
        await session.commit()

    async with review_context.database.sessions() as session:
        with pytest.raises(ReviewConflictError, match="not frozen"):
            await create_review_task(
                session,
                scoring_run_id=running.id,
                created_by_user_id=review_context.reviewer_id,
                idempotency_key="running-run",
                now=NOW + timedelta(minutes=2),
            )

    async with review_context.database.sessions() as session:
        run = await session.get(ScoringRun, review_context.scoring_run_id)
        assert run is not None
        run.config_sha256 = "0" * 64
        with pytest.raises(IntegrityError, match="immutable"):
            await session.commit()
        await session.rollback()

        ranking = await session.scalar(
            select(AssetRanking).where(
                AssetRanking.scoring_run_id == review_context.scoring_run_id,
                AssetRanking.rank == 3,
            )
        )
        assert ranking is not None
        await session.delete(ranking)
        with pytest.raises(IntegrityError, match="append-only"):
            await session.commit()
        await session.rollback()


@pytest.mark.asyncio
async def test_decisions_are_revisioned_idempotent_and_preserve_raw_master(
    review_context: ReviewContext,
) -> None:
    task = await _create_task(review_context)
    asset_id = review_context.ranked_asset_ids[0]
    async with review_context.database.sessions() as session:
        raw_before = await session.get(Asset, asset_id)
        assert raw_before is not None
        raw_snapshot = (
            raw_before.kind,
            raw_before.state,
            raw_before.object_key,
            raw_before.object_version_id,
            raw_before.sha256,
            raw_before.byte_size,
            raw_before.asset_metadata,
        )

    async with review_context.database.sessions() as session:
        first = await append_review_decision(
            session,
            review_task_id=task.task_id,
            asset_id=asset_id,
            decision=ReviewDecisionValue.HOLD,
            decided_by_user_id=review_context.reviewer_id,
            expected_lock_version=1,
            idempotency_key="asset-one-hold",
            reason_code="needs_detail_check",
            note="Inspect hands at full resolution.",
            now=NOW + timedelta(minutes=3),
        )
    async with review_context.database.sessions() as session:
        replay = await append_review_decision(
            session,
            review_task_id=task.task_id,
            asset_id=asset_id,
            decision=ReviewDecisionValue.HOLD,
            decided_by_user_id=review_context.reviewer_id,
            expected_lock_version=1,
            idempotency_key="asset-one-hold",
            reason_code="needs_detail_check",
            note="Inspect hands at full resolution.",
            now=NOW + timedelta(minutes=4),
        )
    assert replay.decision_id == first.decision_id
    assert replay.replayed is True

    async with review_context.database.sessions() as session:
        with pytest.raises(ReviewConflictError, match="idempotency key"):
            await append_review_decision(
                session,
                review_task_id=task.task_id,
                asset_id=asset_id,
                decision=ReviewDecisionValue.REJECT,
                decided_by_user_id=review_context.reviewer_id,
                expected_lock_version=1,
                idempotency_key="asset-one-hold",
                reason_code="anatomy",
            )

    async with review_context.database.sessions() as session:
        second = await append_review_decision(
            session,
            review_task_id=task.task_id,
            asset_id=asset_id,
            decision=ReviewDecisionValue.ACCEPT,
            decided_by_user_id=review_context.second_reviewer_id,
            expected_lock_version=2,
            idempotency_key="asset-one-accept",
            reason_code="manual_qc_pass",
            now=NOW + timedelta(minutes=5),
        )
    async with review_context.database.sessions() as session:
        third = await append_review_decision(
            session,
            review_task_id=task.task_id,
            asset_id=review_context.ranked_asset_ids[1],
            decision=ReviewDecisionValue.REJECT,
            decided_by_user_id=review_context.reviewer_id,
            expected_lock_version=3,
            idempotency_key="asset-two-reject",
            reason_code="anatomy",
            now=NOW + timedelta(minutes=6),
        )

    assert first.revision == 1
    assert first.supersedes_decision_id is None
    assert second.revision == 2
    assert second.supersedes_decision_id == first.decision_id
    assert third.revision == 1
    assert third.task_lock_version == 4

    async with review_context.database.sessions() as session:
        summary = await get_review_summary(session, review_task_id=task.task_id)
        assert summary.accepted_count == 1
        assert summary.rejected_count == 1
        assert summary.held_count == 0
        assert summary.undecided_count == 1
        assert summary.lock_version == 4
        assert summary.assets[0].decision == ReviewDecisionValue.ACCEPT
        assert summary.assets[0].revision == 2
        assert summary.assets[1].decision == ReviewDecisionValue.REJECT
        assert summary.assets[2].decision is None

        revisions = list(
            (
                await session.scalars(
                    select(ReviewDecision)
                    .where(
                        ReviewDecision.review_task_id == task.task_id,
                        ReviewDecision.asset_id == asset_id,
                    )
                    .order_by(ReviewDecision.revision)
                )
            ).all()
        )
        assert [row.decision for row in revisions] == [
            ReviewDecisionValue.HOLD,
            ReviewDecisionValue.ACCEPT,
        ]
        assert revisions[0].note == "Inspect hands at full resolution."
        raw_after = await session.get(Asset, asset_id)
        assert raw_after is not None
        assert (
            raw_after.kind,
            raw_after.state,
            raw_after.object_key,
            raw_after.object_version_id,
            raw_after.sha256,
            raw_after.byte_size,
            raw_after.asset_metadata,
        ) == raw_snapshot


@pytest.mark.asyncio
async def test_bulk_decisions_are_atomic_idempotent_and_preserve_raw_masters(
    review_context: ReviewContext,
) -> None:
    task = await _create_task(review_context)
    first, second, third = review_context.ranked_asset_ids

    async with review_context.database.sessions() as session:
        accepted = await apply_bulk_review_action(
            session,
            review_task_id=task.task_id,
            asset_ids=(second, first),
            action=ReviewBulkAction.ACCEPT,
            changed_by_user_id=review_context.reviewer_id,
            expected_lock_version=1,
            idempotency_key="bulk-accept-two",
            reason_code="manual_qc_pass",
            now=NOW + timedelta(minutes=3),
        )
    assert accepted.asset_ids == (first, second)
    assert accepted.changed_count == 2
    assert accepted.task_lock_version == 2

    async with review_context.database.sessions() as session:
        replay = await apply_bulk_review_action(
            session,
            review_task_id=task.task_id,
            asset_ids=(first, second),
            action=ReviewBulkAction.ACCEPT,
            changed_by_user_id=review_context.reviewer_id,
            expected_lock_version=1,
            idempotency_key="bulk-accept-two",
            reason_code="manual_qc_pass",
            now=NOW + timedelta(minutes=4),
        )
    assert replay.replayed is True
    assert replay.task_lock_version == 2

    async with review_context.database.sessions() as session:
        held = await apply_bulk_review_action(
            session,
            review_task_id=task.task_id,
            asset_ids=(third, first),
            action=ReviewBulkAction.HOLD,
            changed_by_user_id=review_context.second_reviewer_id,
            expected_lock_version=2,
            idempotency_key="bulk-hold-two",
            reason_code="needs_detail_check",
            note="Inspect selected images again.",
            now=NOW + timedelta(minutes=5),
        )
    assert held.changed_count == 2
    assert held.task_lock_version == 3

    async with review_context.database.sessions() as session:
        rejected = await apply_bulk_review_action(
            session,
            review_task_id=task.task_id,
            asset_ids=(second, third),
            action=ReviewBulkAction.REJECT,
            changed_by_user_id=review_context.reviewer_id,
            expected_lock_version=3,
            idempotency_key="bulk-reject-two",
            reason_code="manual_reject",
            now=NOW + timedelta(minutes=6),
        )
    assert rejected.changed_count == 2
    assert rejected.task_lock_version == 4

    async with review_context.database.sessions() as session:
        with pytest.raises(ReviewConflictError, match="stale"):
            await apply_bulk_review_action(
                session,
                review_task_id=task.task_id,
                asset_ids=(first,),
                action=ReviewBulkAction.ACCEPT,
                changed_by_user_id=review_context.reviewer_id,
                expected_lock_version=3,
                idempotency_key="bulk-stale",
            )

    async with review_context.database.sessions() as session:
        summary = await get_review_summary(session, review_task_id=task.task_id)
        assert summary.lock_version == 4
        assert [asset.decision for asset in summary.assets] == [
            ReviewDecisionValue.HOLD,
            ReviewDecisionValue.REJECT,
            ReviewDecisionValue.REJECT,
        ]
        assert [asset.revision for asset in summary.assets] == [2, 2, 2]
        raw_assets = (
            await session.scalars(
                select(Asset).where(Asset.id.in_(review_context.ranked_asset_ids))
            )
        ).all()
        assert len(raw_assets) == 3
        assert all(asset.state == AssetState.AVAILABLE for asset in raw_assets)
        assert all(asset.kind == AssetKind.RAW_MASTER for asset in raw_assets)
        assert (
            await session.scalar(
                select(func.count())
                .select_from(IdempotencyRecord)
                .where(IdempotencyRecord.scope == f"review-task:{task.task_id}:bulk-action")
            )
            == 3
        )


@pytest.mark.asyncio
async def test_bulk_x_selection_is_owner_only_bounded_and_revision_locked(
    review_context: ReviewContext,
) -> None:
    task = await _create_task(review_context)
    first, second, third = review_context.ranked_asset_ids

    async with review_context.database.sessions() as session:
        selected = await apply_bulk_review_action(
            session,
            review_task_id=task.task_id,
            asset_ids=(third, first, second),
            action=ReviewBulkAction.X_ADD,
            changed_by_user_id=review_context.owner_id,
            expected_lock_version=1,
            idempotency_key="bulk-x-add",
            now=NOW + timedelta(minutes=3),
        )
    assert selected.changed_count == 3
    assert selected.x_selected_count == 3
    assert selected.task_lock_version == 2

    async with review_context.database.sessions() as session:
        replay = await apply_bulk_review_action(
            session,
            review_task_id=task.task_id,
            asset_ids=(first, second, third),
            action=ReviewBulkAction.X_ADD,
            changed_by_user_id=review_context.owner_id,
            expected_lock_version=1,
            idempotency_key="bulk-x-add",
        )
    assert replay.replayed is True
    assert replay.x_selected_count == 3

    async with review_context.database.sessions() as session:
        with pytest.raises(ReviewNotFoundError, match="authorized owner"):
            await apply_bulk_review_action(
                session,
                review_task_id=task.task_id,
                asset_ids=(first,),
                action=ReviewBulkAction.X_REMOVE,
                changed_by_user_id=review_context.reviewer_id,
                expected_lock_version=2,
                idempotency_key="reviewer-bulk-x-remove",
            )

    async with review_context.database.sessions() as session:
        removed = await apply_bulk_review_action(
            session,
            review_task_id=task.task_id,
            asset_ids=(first, third),
            action=ReviewBulkAction.X_REMOVE,
            changed_by_user_id=review_context.owner_id,
            expected_lock_version=2,
            idempotency_key="bulk-x-remove",
            now=NOW + timedelta(minutes=4),
        )
    assert removed.changed_count == 2
    assert removed.x_selected_count == 1
    assert removed.task_lock_version == 3

    async with review_context.database.sessions() as session:
        no_change = await apply_bulk_review_action(
            session,
            review_task_id=task.task_id,
            asset_ids=(first, third),
            action=ReviewBulkAction.X_REMOVE,
            changed_by_user_id=review_context.owner_id,
            expected_lock_version=3,
            idempotency_key="bulk-x-remove-noop",
        )
        selected_rows = (
            await session.scalars(
                select(ReviewXSelection).where(ReviewXSelection.review_task_id == task.task_id)
            )
        ).all()
    assert no_change.changed_count == 0
    assert no_change.x_selected_count == 1
    assert no_change.task_lock_version == 4
    assert [row.asset_id for row in selected_rows] == [second]


@pytest.mark.asyncio
async def test_bulk_x_selection_rejects_a_fifth_image_atomically(
    review_context: ReviewContext,
) -> None:
    task = await _create_task(review_context, key="create-x-capacity-review-task")
    first, second, third = review_context.ranked_asset_ids
    extra_asset_id = UUID("10000000-0000-4000-8000-000000000005")

    async with review_context.database.sessions() as session:
        job_id = await session.scalar(
            select(GenerationJob.id).where(
                GenerationJob.release_version_id == review_context.release_version_id
            )
        )
        assert job_id is not None
        session.add(
            _raw_asset(
                asset_id=extra_asset_id,
                release_id=review_context.release_id,
                job_id=job_id,
                output_index=4,
            )
        )
        await session.flush()
        session.add_all(
            ReviewXSelection(
                id=uuid4(),
                review_task_id=task.task_id,
                asset_id=asset_id,
                selected_by_user_id=review_context.owner_id,
                selected_at=NOW,
            )
            for asset_id in (
                second,
                third,
                review_context.unranked_asset_id,
                extra_asset_id,
            )
        )
        await session.commit()

    async with review_context.database.sessions() as session:
        with pytest.raises(ReviewConflictError, match="at most four images"):
            await apply_bulk_review_action(
                session,
                review_task_id=task.task_id,
                asset_ids=(first,),
                action=ReviewBulkAction.X_ADD,
                changed_by_user_id=review_context.owner_id,
                expected_lock_version=1,
                idempotency_key="bulk-x-fifth",
            )

    async with review_context.database.sessions() as session:
        summary = await get_review_summary(session, review_task_id=task.task_id)
        assert summary.lock_version == 1
        assert summary.x_selected_count == 4


@pytest.mark.asyncio
async def test_bulk_accept_preserves_semantic_owner_override_gate(
    review_context: ReviewContext,
) -> None:
    task = await _create_task(review_context)
    profile = SemanticAssessmentProfile(
        model_name="Qwen/Qwen3-VL-8B-Instruct",
        model_revision="pinned-bulk-test-revision",
    )
    await _seed_terminal_semantic_assessments(review_context, profile=profile)
    selected = review_context.ranked_asset_ids[:2]

    async with review_context.database.sessions() as session:
        with pytest.raises(ReviewConflictError, match="owner override"):
            await apply_bulk_review_action(
                session,
                review_task_id=task.task_id,
                asset_ids=selected,
                action=ReviewBulkAction.ACCEPT,
                changed_by_user_id=review_context.reviewer_id,
                expected_lock_version=1,
                idempotency_key="bulk-severe-without-override",
                semantic_profile_sha256=profile.profile_sha256,
            )

    async with review_context.database.sessions() as session:
        accepted = await apply_bulk_review_action(
            session,
            review_task_id=task.task_id,
            asset_ids=selected,
            action=ReviewBulkAction.ACCEPT,
            changed_by_user_id=review_context.owner_id,
            expected_lock_version=1,
            idempotency_key="bulk-severe-owner-override",
            reason_code=SEMANTIC_SEVERE_OVERRIDE_REASON_CODE,
            note="Owner inspected every selected image at full resolution.",
            semantic_profile_sha256=profile.profile_sha256,
        )
    assert accepted.changed_count == 2
    assert accepted.task_lock_version == 2

    async with review_context.database.sessions() as session:
        summary = await get_review_summary(
            session,
            review_task_id=task.task_id,
            semantic_profile_sha256=profile.profile_sha256,
        )
        assert summary.semantic_gate.severe_override_count == 1
        assert summary.semantic_gate.severe_blocked_count == 0
        assert summary.assets[0].semantic_severe_override_attested is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode",
    (SemanticEnforcementMode.SHADOW, SemanticEnforcementMode.ASSIST),
)
async def test_non_enforcing_semantic_modes_never_block_owner_decisions_or_completion(
    review_context: ReviewContext,
    mode: SemanticEnforcementMode,
) -> None:
    task = await _create_task(review_context)
    profile = SemanticAssessmentProfile(
        model_name="Qwen/Qwen3-VL-8B-Instruct",
        model_revision=f"pinned-{mode.value}-mode-revision",
    )

    for offset, asset_id in enumerate(review_context.ranked_asset_ids[:2]):
        async with review_context.database.sessions() as session:
            await append_review_decision(
                session,
                review_task_id=task.task_id,
                asset_id=asset_id,
                decision=ReviewDecisionValue.ACCEPT,
                decided_by_user_id=review_context.reviewer_id,
                expected_lock_version=offset + 1,
                idempotency_key=f"{mode.value}-accept-{offset}",
                semantic_profile_sha256=profile.profile_sha256,
                semantic_enforcement_mode=mode,
            )

    async with review_context.database.sessions() as session:
        summary = await get_review_summary(
            session,
            review_task_id=task.task_id,
            semantic_profile_sha256=profile.profile_sha256,
            semantic_enforcement_mode=mode,
        )
        assert summary.semantic_gate.enabled is True
        assert summary.semantic_gate.mode == mode
        assert summary.semantic_gate.pending_count == len(review_context.ranked_asset_ids)
        assert summary.semantic_gate.completion_ready is True

    async with review_context.database.sessions() as session:
        completed = await transition_review_task(
            session,
            review_task_id=task.task_id,
            target_state=ReviewTaskState.COMPLETED,
            changed_by_user_id=review_context.reviewer_id,
            expected_lock_version=3,
            idempotency_key=f"{mode.value}-complete-with-pending-assessments",
            semantic_profile_sha256=profile.profile_sha256,
            semantic_enforcement_mode=mode,
        )
    assert completed.state == ReviewTaskState.COMPLETED


@pytest.mark.asyncio
async def test_decision_requires_exact_ranking_and_current_lock_version(
    review_context: ReviewContext,
) -> None:
    task = await _create_task(review_context)
    async with review_context.database.sessions() as session:
        with pytest.raises(ReviewConflictError, match="not ranked"):
            await append_review_decision(
                session,
                review_task_id=task.task_id,
                asset_id=review_context.unranked_asset_id,
                decision=ReviewDecisionValue.ACCEPT,
                decided_by_user_id=review_context.reviewer_id,
                expected_lock_version=1,
                idempotency_key="unranked",
            )

    async with review_context.database.sessions() as session:
        accepted = await append_review_decision(
            session,
            review_task_id=task.task_id,
            asset_id=review_context.ranked_asset_ids[0],
            decision=ReviewDecisionValue.ACCEPT,
            decided_by_user_id=review_context.reviewer_id,
            expected_lock_version=1,
            idempotency_key="first-reviewer",
        )
    assert accepted.task_lock_version == 2

    async with review_context.database.sessions() as session:
        with pytest.raises(ReviewConflictError, match="stale"):
            await append_review_decision(
                session,
                review_task_id=task.task_id,
                asset_id=review_context.ranked_asset_ids[1],
                decision=ReviewDecisionValue.ACCEPT,
                decided_by_user_id=review_context.second_reviewer_id,
                expected_lock_version=1,
                idempotency_key="stale-second-reviewer",
            )
        stored_task = await session.get(ReviewTask, task.task_id)
        assert stored_task is not None
        assert stored_task.lock_version == 2


@pytest.mark.asyncio
async def test_completion_shrinks_target_to_accepted_count_and_is_terminal(
    review_context: ReviewContext,
) -> None:
    task = await _create_task(review_context)
    async with review_context.database.sessions() as session:
        await append_review_decision(
            session,
            review_task_id=task.task_id,
            asset_id=review_context.ranked_asset_ids[0],
            decision=ReviewDecisionValue.ACCEPT,
            decided_by_user_id=review_context.reviewer_id,
            expected_lock_version=1,
            idempotency_key="accept-one",
        )
    async with review_context.database.sessions() as session:
        completed = await transition_review_task(
            session,
            review_task_id=task.task_id,
            target_state=ReviewTaskState.COMPLETED,
            changed_by_user_id=review_context.reviewer_id,
            expected_lock_version=2,
            idempotency_key="complete-review",
            now=NOW + timedelta(minutes=8),
        )
    async with review_context.database.sessions() as session:
        replay = await transition_review_task(
            session,
            review_task_id=task.task_id,
            target_state=ReviewTaskState.COMPLETED,
            changed_by_user_id=review_context.reviewer_id,
            expected_lock_version=2,
            idempotency_key="complete-review",
            now=NOW + timedelta(minutes=9),
        )

    assert completed.state == ReviewTaskState.COMPLETED
    assert completed.accepted_count == 1
    assert completed.lock_version == 3
    assert replay.replayed is True
    assert replay.lock_version == 3

    async with review_context.database.sessions() as session:
        with pytest.raises(ReviewConflictError, match="not open"):
            await append_review_decision(
                session,
                review_task_id=task.task_id,
                asset_id=review_context.ranked_asset_ids[2],
                decision=ReviewDecisionValue.REJECT,
                decided_by_user_id=review_context.reviewer_id,
                expected_lock_version=3,
                idempotency_key="after-completion",
            )
        stored = await session.get(ReviewTask, task.task_id)
        assert stored is not None
        assert stored.state == ReviewTaskState.COMPLETED
        assert stored.desired_accepted_count == 1
        assert stored.completed_by_user_id == review_context.reviewer_id
        assert stored.completed_at == (NOW + timedelta(minutes=8)).replace(tzinfo=None)
        assert stored.cancelled_at is None
        assert (
            await session.scalar(
                select(func.count())
                .select_from(ReleaseSelection)
                .where(ReleaseSelection.review_task_id == task.task_id)
            )
            == 1
        )
        completed_audit = await session.scalar(
            select(AuditEvent).where(
                AuditEvent.action == "review.task_completed",
                AuditEvent.resource_id == task.task_id,
            )
        )
        assert completed_audit is not None
        assert completed_audit.detail["configured_accepted_count"] == 2
        assert completed_audit.detail["final_accepted_count"] == 1
        assert completed_audit.detail["desired_accepted_count"] == 1


@pytest.mark.asyncio
async def test_completion_rejects_zero_and_over_configured_target(
    review_context: ReviewContext,
) -> None:
    task = await _create_task(review_context)
    async with review_context.database.sessions() as session:
        with pytest.raises(ReviewConflictError, match="at least one"):
            await transition_review_task(
                session,
                review_task_id=task.task_id,
                target_state=ReviewTaskState.COMPLETED,
                changed_by_user_id=review_context.reviewer_id,
                expected_lock_version=1,
                idempotency_key="complete-with-zero",
            )

    for offset, asset_id in enumerate(review_context.ranked_asset_ids):
        async with review_context.database.sessions() as session:
            await append_review_decision(
                session,
                review_task_id=task.task_id,
                asset_id=asset_id,
                decision=ReviewDecisionValue.ACCEPT,
                decided_by_user_id=review_context.reviewer_id,
                expected_lock_version=offset + 1,
                idempotency_key=f"accept-over-target-{offset}",
            )

    async with review_context.database.sessions() as session:
        with pytest.raises(ReviewConflictError, match="exceeds the configured"):
            await transition_review_task(
                session,
                review_task_id=task.task_id,
                target_state=ReviewTaskState.COMPLETED,
                changed_by_user_id=review_context.reviewer_id,
                expected_lock_version=4,
                idempotency_key="complete-over-target",
            )


@pytest.mark.asyncio
async def test_semantic_completion_gate_requires_terminal_checks_and_owner_override(
    review_context: ReviewContext,
) -> None:
    task = await _create_task(review_context)
    profile = SemanticAssessmentProfile(
        model_name="Qwen/Qwen3-VL-8B-Instruct",
        model_revision="pinned-test-revision",
    )
    async with review_context.database.sessions() as session:
        await append_review_decision(
            session,
            review_task_id=task.task_id,
            asset_id=review_context.ranked_asset_ids[0],
            decision=ReviewDecisionValue.ACCEPT,
            decided_by_user_id=review_context.reviewer_id,
            expected_lock_version=1,
            idempotency_key="semantic-accept-before-result",
        )
    async with review_context.database.sessions() as session:
        await append_review_decision(
            session,
            review_task_id=task.task_id,
            asset_id=review_context.ranked_asset_ids[1],
            decision=ReviewDecisionValue.ACCEPT,
            decided_by_user_id=review_context.reviewer_id,
            expected_lock_version=2,
            idempotency_key="semantic-accept-pass",
        )
    async with review_context.database.sessions() as session:
        with pytest.raises(ReviewConflictError, match="not terminal"):
            await transition_review_task(
                session,
                review_task_id=task.task_id,
                target_state=ReviewTaskState.COMPLETED,
                changed_by_user_id=review_context.reviewer_id,
                expected_lock_version=3,
                idempotency_key="semantic-complete-pending",
                semantic_profile_sha256=profile.profile_sha256,
            )

    await _seed_terminal_semantic_assessments(review_context, profile=profile)
    async with review_context.database.sessions() as session:
        with pytest.raises(ReviewConflictError, match="owner override"):
            await transition_review_task(
                session,
                review_task_id=task.task_id,
                target_state=ReviewTaskState.COMPLETED,
                changed_by_user_id=review_context.reviewer_id,
                expected_lock_version=3,
                idempotency_key="semantic-complete-severe-block",
                semantic_profile_sha256=profile.profile_sha256,
            )
    async with review_context.database.sessions() as session:
        with pytest.raises(ReviewNotFoundError, match="authorized owner"):
            await append_review_decision(
                session,
                review_task_id=task.task_id,
                asset_id=review_context.ranked_asset_ids[0],
                decision=ReviewDecisionValue.ACCEPT,
                decided_by_user_id=review_context.reviewer_id,
                expected_lock_version=3,
                idempotency_key="reviewer-cannot-override-severe",
                reason_code=SEMANTIC_SEVERE_OVERRIDE_REASON_CODE,
                note="Reviewer attempted an owner-only override.",
                semantic_profile_sha256=profile.profile_sha256,
            )
    async with review_context.database.sessions() as session:
        override = await append_review_decision(
            session,
            review_task_id=task.task_id,
            asset_id=review_context.ranked_asset_ids[0],
            decision=ReviewDecisionValue.ACCEPT,
            decided_by_user_id=review_context.owner_id,
            expected_lock_version=3,
            idempotency_key="owner-overrides-severe",
            reason_code=SEMANTIC_SEVERE_OVERRIDE_REASON_CODE,
            note="Hands and limb layout were inspected at full resolution.",
            semantic_profile_sha256=profile.profile_sha256,
        )
    async with review_context.database.sessions() as session:
        summary = await get_review_summary(
            session,
            review_task_id=task.task_id,
            semantic_profile_sha256=profile.profile_sha256,
        )
        assert summary.semantic_gate.terminal_count == 3
        assert summary.semantic_gate.pending_count == 0
        assert summary.semantic_gate.unavailable_count == 1
        assert summary.semantic_gate.severe_blocked_count == 0
        assert summary.semantic_gate.severe_override_count == 1
        assert summary.semantic_gate.completion_ready is True
        assert summary.assets[0].semantic_severe_override_attested is True
        assert (
            await session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(
                    AuditEvent.action == SEMANTIC_SEVERE_OVERRIDE_AUDIT_ACTION,
                    AuditEvent.resource_id == override.decision_id,
                )
            )
            == 1
        )
    async with review_context.database.sessions() as session:
        completed = await transition_review_task(
            session,
            review_task_id=task.task_id,
            target_state=ReviewTaskState.COMPLETED,
            changed_by_user_id=review_context.owner_id,
            expected_lock_version=4,
            idempotency_key="semantic-complete-after-override",
            semantic_profile_sha256=profile.profile_sha256,
        )
    assert completed.state == ReviewTaskState.COMPLETED


@pytest.mark.asyncio
async def test_cancelled_task_rejects_later_decisions(
    review_context: ReviewContext,
) -> None:
    task = await _create_task(review_context)
    async with review_context.database.sessions() as session:
        cancelled = await transition_review_task(
            session,
            review_task_id=task.task_id,
            target_state=ReviewTaskState.CANCELLED,
            changed_by_user_id=review_context.second_reviewer_id,
            expected_lock_version=1,
            idempotency_key="cancel-review",
            now=NOW + timedelta(minutes=3),
        )
    assert cancelled.state == ReviewTaskState.CANCELLED
    assert cancelled.lock_version == 2

    async with review_context.database.sessions() as session:
        with pytest.raises(ReviewConflictError, match="not open"):
            await append_review_decision(
                session,
                review_task_id=task.task_id,
                asset_id=review_context.ranked_asset_ids[0],
                decision=ReviewDecisionValue.ACCEPT,
                decided_by_user_id=review_context.reviewer_id,
                expected_lock_version=2,
                idempotency_key="after-cancel",
            )
        stored = await session.get(ReviewTask, task.task_id)
        assert stored is not None
        assert stored.cancelled_by_user_id == review_context.second_reviewer_id
        assert stored.cancelled_at == (NOW + timedelta(minutes=3)).replace(tzinfo=None)
        assert stored.completed_at is None


@pytest.mark.asyncio
async def test_sqlite_enforces_actor_fk_and_same_asset_revision_chain(
    review_context: ReviewContext,
) -> None:
    task = await _create_task(review_context)
    asset_one, asset_two, _ = review_context.ranked_asset_ids
    async with review_context.database.sessions() as session:
        session.add(
            ReviewDecision(
                review_task_id=task.task_id,
                scoring_run_id=review_context.scoring_run_id,
                asset_id=asset_one,
                revision=1,
                decision=ReviewDecisionValue.ACCEPT,
                decided_by_user_id=uuid4(),
                decided_at=NOW + timedelta(minutes=3),
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()

    async with review_context.database.sessions() as session:
        first = await append_review_decision(
            session,
            review_task_id=task.task_id,
            asset_id=asset_one,
            decision=ReviewDecisionValue.HOLD,
            decided_by_user_id=review_context.reviewer_id,
            expected_lock_version=1,
            idempotency_key="valid-first-revision",
        )

    async with review_context.database.sessions() as session:
        session.add(
            ReviewDecision(
                review_task_id=task.task_id,
                scoring_run_id=review_context.scoring_run_id,
                asset_id=asset_two,
                revision=2,
                decision=ReviewDecisionValue.REJECT,
                decided_by_user_id=review_context.reviewer_id,
                decided_at=NOW + timedelta(minutes=4),
                supersedes_revision=1,
                supersedes_decision_id=first.decision_id,
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()


@pytest.mark.asyncio
async def test_database_rejects_decisions_outside_frozen_ranking_membership(
    review_context: ReviewContext,
) -> None:
    task = await _create_task(review_context)

    async with review_context.database.sessions() as session:
        session.add(
            ReviewDecision(
                review_task_id=task.task_id,
                scoring_run_id=review_context.scoring_run_id,
                asset_id=review_context.unranked_asset_id,
                revision=1,
                decision=ReviewDecisionValue.ACCEPT,
                decided_by_user_id=review_context.reviewer_id,
                decided_at=NOW + timedelta(minutes=3),
            )
        )
        with pytest.raises(IntegrityError, match="FOREIGN KEY"):
            await session.commit()
        await session.rollback()

        assert (
            await session.scalar(
                select(func.count())
                .select_from(ReviewDecision)
                .where(ReviewDecision.review_task_id == task.task_id)
            )
            == 0
        )


@pytest.mark.asyncio
async def test_database_rejects_review_task_snapshot_mutation(
    review_context: ReviewContext,
) -> None:
    task = await _create_task(review_context)

    async with review_context.database.sessions() as session:
        stored = await session.get(ReviewTask, task.task_id)
        assert stored is not None
        stored.ranking_manifest_sha256 = "0" * 64
        with pytest.raises(IntegrityError, match="identity is immutable"):
            await session.commit()
        await session.rollback()

        stored = await session.get(ReviewTask, task.task_id)
        assert stored is not None
        assert stored.ranking_manifest_sha256 != "0" * 64


@pytest.mark.asyncio
async def test_review_decisions_are_database_append_only(
    review_context: ReviewContext,
) -> None:
    task = await _create_task(review_context)
    async with review_context.database.sessions() as session:
        stored = await append_review_decision(
            session,
            review_task_id=task.task_id,
            asset_id=review_context.ranked_asset_ids[0],
            decision=ReviewDecisionValue.HOLD,
            decided_by_user_id=review_context.reviewer_id,
            expected_lock_version=1,
            idempotency_key="append-only-decision",
        )

    async with review_context.database.sessions() as session:
        decision = await session.get(ReviewDecision, stored.decision_id)
        assert decision is not None
        decision.note = "Attempted mutation"
        with pytest.raises(IntegrityError, match="append-only"):
            await session.commit()
        await session.rollback()

        decision = await session.get(ReviewDecision, stored.decision_id)
        assert decision is not None
        await session.delete(decision)
        with pytest.raises(IntegrityError, match="append-only"):
            await session.commit()
        await session.rollback()

        assert await session.get(ReviewDecision, stored.decision_id) is not None


@pytest.mark.asyncio
async def test_only_active_reviewers_and_owners_can_mutate_review_workflow(
    review_context: ReviewContext,
) -> None:
    for actor_id, key in (
        (review_context.admin_id, "admin-create"),
        (review_context.publisher_id, "publisher-create"),
    ):
        async with review_context.database.sessions() as session:
            with pytest.raises(ReviewNotFoundError, match="authorized review actor"):
                await create_review_task(
                    session,
                    scoring_run_id=review_context.scoring_run_id,
                    created_by_user_id=actor_id,
                    idempotency_key=key,
                )

    async with review_context.database.sessions() as session:
        task = await create_review_task(
            session,
            scoring_run_id=review_context.scoring_run_id,
            created_by_user_id=review_context.owner_id,
            idempotency_key="owner-create",
        )

    for actor_id, key in (
        (review_context.admin_id, "admin-decision"),
        (review_context.publisher_id, "publisher-decision"),
    ):
        async with review_context.database.sessions() as session:
            with pytest.raises(ReviewNotFoundError, match="authorized review actor"):
                await append_review_decision(
                    session,
                    review_task_id=task.task_id,
                    asset_id=review_context.ranked_asset_ids[0],
                    decision=ReviewDecisionValue.ACCEPT,
                    decided_by_user_id=actor_id,
                    expected_lock_version=1,
                    idempotency_key=key,
                )

    async with review_context.database.sessions() as session:
        accepted = await append_review_decision(
            session,
            review_task_id=task.task_id,
            asset_id=review_context.ranked_asset_ids[0],
            decision=ReviewDecisionValue.ACCEPT,
            decided_by_user_id=review_context.owner_id,
            expected_lock_version=1,
            idempotency_key="owner-decision",
        )
    assert accepted.task_lock_version == 2

    for actor_id, key in (
        (review_context.admin_id, "admin-transition"),
        (review_context.publisher_id, "publisher-transition"),
    ):
        async with review_context.database.sessions() as session:
            with pytest.raises(ReviewNotFoundError, match="authorized review actor"):
                await transition_review_task(
                    session,
                    review_task_id=task.task_id,
                    target_state=ReviewTaskState.CANCELLED,
                    changed_by_user_id=actor_id,
                    expected_lock_version=2,
                    idempotency_key=key,
                )

    async with review_context.database.sessions() as session:
        cancelled = await transition_review_task(
            session,
            review_task_id=task.task_id,
            target_state=ReviewTaskState.CANCELLED,
            changed_by_user_id=review_context.owner_id,
            expected_lock_version=2,
            idempotency_key="owner-transition",
        )
    assert cancelled.lock_version == 3


@pytest.mark.asyncio
async def test_revoked_reviewer_cannot_replay_prior_mutation(
    review_context: ReviewContext,
) -> None:
    task = await _create_task(review_context)
    request = {
        "review_task_id": task.task_id,
        "asset_id": review_context.ranked_asset_ids[0],
        "decision": ReviewDecisionValue.HOLD,
        "decided_by_user_id": review_context.reviewer_id,
        "expected_lock_version": 1,
        "idempotency_key": "replay-before-revocation",
    }

    async with review_context.database.sessions() as session:
        await append_review_decision(session, **request)

    async with review_context.database.sessions() as session:
        reviewer = await session.get(AdminUser, review_context.reviewer_id)
        assert reviewer is not None
        reviewer.is_active = False
        await session.commit()

    async with review_context.database.sessions() as session:
        with pytest.raises(ReviewNotFoundError, match="authorized review actor"):
            await append_review_decision(session, **request)


@pytest.mark.asyncio
async def test_unknown_user_and_task_fail_closed(
    review_context: ReviewContext,
) -> None:
    async with review_context.database.sessions() as session:
        with pytest.raises(ReviewNotFoundError, match="actor"):
            await create_review_task(
                session,
                scoring_run_id=review_context.scoring_run_id,
                created_by_user_id=uuid4(),
                idempotency_key="unknown-reviewer",
            )
        with pytest.raises(ReviewNotFoundError, match="task"):
            await get_review_summary(session, review_task_id=uuid4())
