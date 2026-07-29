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
    ReleaseVersion,
    ReviewDecision,
    ReviewTask,
    ScoringRun,
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
    ReviewDecisionValue,
    ReviewTaskState,
    ScoringRunState,
    SemanticAssessmentState,
    SemanticVerdict,
)
from gen_automation.semantic import prompt_sha256, schema_sha256
from gen_automation.services.ranking_manifest import ranking_manifest_sha256
from gen_automation.services.review import (
    SEMANTIC_SEVERE_OVERRIDE_AUDIT_ACTION,
    SEMANTIC_SEVERE_OVERRIDE_REASON_CODE,
    ReviewConflictError,
    ReviewNotFoundError,
    ReviewTaskResult,
    append_review_decision,
    create_review_task,
    get_review_summary,
    transition_review_task,
)
from gen_automation.services.semantic_anatomy import SemanticAssessmentProfile

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
async def test_completion_requires_exact_acceptance_target_and_is_terminal(
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
        with pytest.raises(ReviewConflictError, match="exactly match"):
            await transition_review_task(
                session,
                review_task_id=task.task_id,
                target_state=ReviewTaskState.COMPLETED,
                changed_by_user_id=review_context.reviewer_id,
                expected_lock_version=2,
                idempotency_key="complete-too-early",
            )

    async with review_context.database.sessions() as session:
        await append_review_decision(
            session,
            review_task_id=task.task_id,
            asset_id=review_context.ranked_asset_ids[1],
            decision=ReviewDecisionValue.ACCEPT,
            decided_by_user_id=review_context.second_reviewer_id,
            expected_lock_version=2,
            idempotency_key="accept-two",
        )
    async with review_context.database.sessions() as session:
        completed = await transition_review_task(
            session,
            review_task_id=task.task_id,
            target_state=ReviewTaskState.COMPLETED,
            changed_by_user_id=review_context.reviewer_id,
            expected_lock_version=3,
            idempotency_key="complete-review",
            now=NOW + timedelta(minutes=8),
        )
    async with review_context.database.sessions() as session:
        replay = await transition_review_task(
            session,
            review_task_id=task.task_id,
            target_state=ReviewTaskState.COMPLETED,
            changed_by_user_id=review_context.reviewer_id,
            expected_lock_version=3,
            idempotency_key="complete-review",
            now=NOW + timedelta(minutes=9),
        )

    assert completed.state == ReviewTaskState.COMPLETED
    assert completed.accepted_count == 2
    assert completed.lock_version == 4
    assert replay.replayed is True
    assert replay.lock_version == 4

    async with review_context.database.sessions() as session:
        with pytest.raises(ReviewConflictError, match="not open"):
            await append_review_decision(
                session,
                review_task_id=task.task_id,
                asset_id=review_context.ranked_asset_ids[2],
                decision=ReviewDecisionValue.REJECT,
                decided_by_user_id=review_context.reviewer_id,
                expected_lock_version=4,
                idempotency_key="after-completion",
            )
        stored = await session.get(ReviewTask, task.task_id)
        assert stored is not None
        assert stored.state == ReviewTaskState.COMPLETED
        assert stored.completed_by_user_id == review_context.reviewer_id
        assert stored.completed_at == (NOW + timedelta(minutes=8)).replace(tzinfo=None)
        assert stored.cancelled_at is None


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
