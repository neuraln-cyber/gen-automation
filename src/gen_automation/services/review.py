import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import and_, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from gen_automation.db.models import (
    AdminUser,
    Asset,
    AssetRanking,
    AuditEvent,
    IdempotencyRecord,
    OutboxEvent,
    Release,
    ReleaseSelection,
    ReleaseVersion,
    ReviewDecision,
    ReviewTask,
    ReviewXSelection,
    ScoringRun,
)
from gen_automation.domain.canonical import canonical_sha256
from gen_automation.domain.enums import (
    AdminRole,
    AssetKind,
    AssetState,
    OutboxStatus,
    ReleasePhase,
    ReviewDecisionValue,
    ReviewTaskState,
    ScoringRunState,
)
from gen_automation.domain.ids import uuid7
from gen_automation.services.ranking_manifest import (
    RankingManifestIntegrityError,
    load_ranking_manifest_rows,
    validate_completed_ranking_manifest,
)

_REASON_CODE_PATTERN = re.compile(r"[a-z][a-z0-9_.-]{0,99}")
_MAX_NOTE_LENGTH = 4_000
_MAX_IDEMPOTENCY_KEY_LENGTH = 200


class ReviewServiceError(Exception):
    """Base error for the durable human-review workflow."""


class ReviewNotFoundError(ReviewServiceError):
    pass


class ReviewConflictError(ReviewServiceError):
    pass


class ReviewInputError(ReviewServiceError, ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ReviewTaskResult:
    task_id: UUID
    release_version_id: UUID
    scoring_run_id: UUID
    desired_accepted_count: int
    ranked_asset_count: int
    state: ReviewTaskState
    lock_version: int
    replayed: bool


@dataclass(frozen=True, slots=True)
class ReviewDecisionResult:
    decision_id: UUID
    task_id: UUID
    asset_id: UUID
    revision: int
    decision: ReviewDecisionValue
    reason_code: str | None
    note: str | None
    decided_by_user_id: UUID
    supersedes_decision_id: UUID | None
    task_lock_version: int
    replayed: bool


@dataclass(frozen=True, slots=True)
class ReviewTransitionResult:
    task_id: UUID
    state: ReviewTaskState
    lock_version: int
    accepted_count: int
    replayed: bool


@dataclass(frozen=True, slots=True)
class ReviewXSelectionResult:
    task_id: UUID
    asset_id: UUID
    selected: bool
    selected_count: int
    replayed: bool


@dataclass(frozen=True, slots=True)
class CurrentAssetDecision:
    asset_id: UUID
    rank: int
    decision_id: UUID | None
    revision: int | None
    decision: ReviewDecisionValue | None
    reason_code: str | None
    note: str | None
    decided_by_user_id: UUID | None
    decided_at: datetime | None
    selected_for_x: bool


@dataclass(frozen=True, slots=True)
class ReviewSummary:
    task_id: UUID
    state: ReviewTaskState
    lock_version: int
    desired_accepted_count: int
    ranked_asset_count: int
    accepted_count: int
    rejected_count: int
    held_count: int
    undecided_count: int
    x_selected_count: int
    assets: tuple[CurrentAssetDecision, ...]


async def create_review_task(
    session: AsyncSession,
    *,
    scoring_run_id: UUID,
    created_by_user_id: UUID,
    idempotency_key: str,
    now: datetime | None = None,
) -> ReviewTaskResult:
    """Create one review task from an exact, completed ranking snapshot."""

    normalized_key = _validate_idempotency_key(idempotency_key)
    scope = f"scoring-run:{scoring_run_id}:create-review-task"
    request_sha256 = canonical_sha256(
        {
            "scoring_run_id": str(scoring_run_id),
            "created_by_user_id": str(created_by_user_id),
        }
    )
    await _require_active_reviewer(session, created_by_user_id)
    replay = await _task_idempotency_replay(
        session,
        scope=scope,
        idempotency_key=normalized_key,
        request_sha256=request_sha256,
    )
    if replay is not None:
        return replay

    existing = await session.scalar(
        select(ReviewTask).where(ReviewTask.scoring_run_id == scoring_run_id)
    )
    if existing is not None:
        await _validate_task_ranking_snapshot(session, existing, recompute=False)
        result = _task_result(existing, replayed=True)
        session.add(
            _idempotency_record(
                scope=scope,
                key=normalized_key,
                request_sha256=request_sha256,
                status=200,
                body=_task_response_body(result),
                created_at=_as_utc(now or datetime.now(UTC)),
            )
        )
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            replay = await _task_idempotency_replay(
                session,
                scope=scope,
                idempotency_key=normalized_key,
                request_sha256=request_sha256,
            )
            if replay is None:
                raise ReviewConflictError("review-task idempotency could not be reserved") from None
            return replay
        return result

    row = (
        await session.execute(
            select(ScoringRun, ReleaseVersion, Release)
            .join(
                ReleaseVersion,
                ReleaseVersion.id == ScoringRun.release_version_id,
            )
            .join(Release, Release.id == ReleaseVersion.release_id)
            .where(ScoringRun.id == scoring_run_id)
            .with_for_update()
        )
    ).one_or_none()
    if row is None:
        raise ReviewNotFoundError("scoring run was not found")
    scoring_run, release_version, release = row
    if scoring_run.state != ScoringRunState.COMPLETED or scoring_run.completed_at is None:
        raise ReviewConflictError("scoring run is not frozen")
    ranking_rows = await load_ranking_manifest_rows(session, scoring_run.id)
    try:
        ranking_manifest_sha256 = validate_completed_ranking_manifest(
            scoring_run,
            ranking_rows,
        )
    except RankingManifestIntegrityError as error:
        raise ReviewConflictError("frozen ranking snapshot is incomplete") from error
    ranked_asset_count = len(ranking_rows)
    if ranked_asset_count != scoring_run.asset_count:
        raise ReviewConflictError("frozen ranking snapshot is incomplete")
    if release.desired_accepted_count > ranked_asset_count:
        raise ReviewConflictError("desired accepted count exceeds ranked assets")

    created_at = _as_utc(now or datetime.now(UTC))
    task = ReviewTask(
        id=uuid7(),
        release_version_id=release_version.id,
        release_version_no=release_version.version_no,
        release_specification_sha256=release_version.specification_sha256,
        scoring_run_id=scoring_run.id,
        scoring_config_sha256=scoring_run.config_sha256,
        scoring_input_manifest_sha256=scoring_run.input_manifest_sha256,
        ranking_manifest_sha256=ranking_manifest_sha256,
        desired_accepted_count=release.desired_accepted_count,
        ranked_asset_count=ranked_asset_count,
        state=ReviewTaskState.OPEN,
        lock_version=1,
        created_by_user_id=created_by_user_id,
        created_at=created_at,
    )
    result = _task_result(task, replayed=False)
    session.add(task)
    session.add(
        AuditEvent(
            actor=_audit_actor(created_by_user_id),
            action="review.task_created",
            resource_type="review_task",
            resource_id=task.id,
            correlation_id=normalized_key,
            detail={
                "release_version_id": str(release_version.id),
                "release_version_no": release_version.version_no,
                "release_specification_sha256": release_version.specification_sha256,
                "scoring_run_id": str(scoring_run.id),
                "scoring_config_sha256": scoring_run.config_sha256,
                "scoring_input_manifest_sha256": scoring_run.input_manifest_sha256,
                "ranking_manifest_sha256": task.ranking_manifest_sha256,
                "desired_accepted_count": task.desired_accepted_count,
                "ranked_asset_count": task.ranked_asset_count,
            },
            occurred_at=created_at,
        )
    )
    session.add(
        _idempotency_record(
            scope=scope,
            key=normalized_key,
            request_sha256=request_sha256,
            status=201,
            body=_task_response_body(result),
            created_at=created_at,
        )
    )
    try:
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        replay = await _task_idempotency_replay(
            session,
            scope=scope,
            idempotency_key=normalized_key,
            request_sha256=request_sha256,
        )
        if replay is not None:
            return replay
        winner = await session.scalar(
            select(ReviewTask).where(ReviewTask.scoring_run_id == scoring_run_id)
        )
        if winner is not None:
            return _task_result(winner, replayed=True)
        raise ReviewConflictError("review task was created concurrently") from error
    return result


async def append_review_decision(
    session: AsyncSession,
    *,
    review_task_id: UUID,
    asset_id: UUID,
    decision: ReviewDecisionValue,
    decided_by_user_id: UUID,
    expected_lock_version: int,
    idempotency_key: str,
    reason_code: str | None = None,
    note: str | None = None,
    now: datetime | None = None,
) -> ReviewDecisionResult:
    """Append an attributed decision revision without mutating its raw asset."""

    normalized_key = _validate_idempotency_key(idempotency_key)
    normalized_decision = _validate_decision(decision)
    normalized_reason = _normalize_reason_code(reason_code)
    normalized_note = _normalize_note(note)
    expected_version = _validate_lock_version(expected_lock_version)
    scope = f"review-task:{review_task_id}:append-decision"
    request_sha256 = canonical_sha256(
        {
            "review_task_id": str(review_task_id),
            "asset_id": str(asset_id),
            "decision": normalized_decision.value,
            "decided_by_user_id": str(decided_by_user_id),
            "expected_lock_version": expected_version,
            "reason_code": normalized_reason,
            "note": normalized_note,
        }
    )
    await _require_active_reviewer(session, decided_by_user_id)
    replay = await _decision_idempotency_replay(
        session,
        scope=scope,
        idempotency_key=normalized_key,
        request_sha256=request_sha256,
    )
    if replay is not None:
        return replay

    task = await _load_task_locked(session, review_task_id)
    if task.state != ReviewTaskState.OPEN:
        raise ReviewConflictError("review task is not open")
    if task.lock_version != expected_version:
        raise ReviewConflictError("review task lock version is stale")
    await _validate_task_ranking_snapshot(session, task, recompute=False)

    ranking_id = await session.scalar(
        select(AssetRanking.id).where(
            AssetRanking.scoring_run_id == task.scoring_run_id,
            AssetRanking.asset_id == asset_id,
        )
    )
    if ranking_id is None:
        raise ReviewConflictError("asset is not ranked in this review task")

    claimed_task_id = await session.scalar(
        update(ReviewTask)
        .where(
            ReviewTask.id == task.id,
            ReviewTask.state == ReviewTaskState.OPEN,
            ReviewTask.lock_version == expected_version,
        )
        .values(lock_version=expected_version + 1)
        .returning(ReviewTask.id)
    )
    if claimed_task_id is None:
        raise ReviewConflictError("review task was changed concurrently")

    prior = await session.scalar(
        select(ReviewDecision)
        .where(
            ReviewDecision.review_task_id == task.id,
            ReviewDecision.asset_id == asset_id,
        )
        .order_by(ReviewDecision.revision.desc())
        .limit(1)
        .with_for_update()
    )
    revision = 1 if prior is None else prior.revision + 1
    decided_at = _as_utc(now or datetime.now(UTC))
    stored = ReviewDecision(
        id=uuid7(),
        review_task_id=task.id,
        scoring_run_id=task.scoring_run_id,
        asset_id=asset_id,
        revision=revision,
        decision=normalized_decision,
        reason_code=normalized_reason,
        note=normalized_note,
        decided_by_user_id=decided_by_user_id,
        decided_at=decided_at,
        supersedes_revision=prior.revision if prior is not None else None,
        supersedes_decision_id=prior.id if prior is not None else None,
    )
    result = _decision_result(
        stored,
        task_lock_version=expected_version + 1,
        replayed=False,
    )
    session.add(stored)
    session.add(
        AuditEvent(
            actor=_audit_actor(decided_by_user_id),
            action="review.decision_appended",
            resource_type="review_decision",
            resource_id=stored.id,
            correlation_id=normalized_key,
            detail={
                "review_task_id": str(task.id),
                "asset_id": str(asset_id),
                "revision": revision,
                "decision": normalized_decision.value,
                "reason_code": normalized_reason,
                "note_present": normalized_note is not None,
                "supersedes_decision_id": (str(prior.id) if prior is not None else None),
                "task_lock_version": expected_version + 1,
            },
            occurred_at=decided_at,
        )
    )
    session.add(
        _idempotency_record(
            scope=scope,
            key=normalized_key,
            request_sha256=request_sha256,
            status=201,
            body=_decision_response_body(result),
            created_at=decided_at,
        )
    )
    try:
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        replay = await _decision_idempotency_replay(
            session,
            scope=scope,
            idempotency_key=normalized_key,
            request_sha256=request_sha256,
        )
        if replay is not None:
            return replay
        raise ReviewConflictError("decision was appended concurrently") from error
    return result


async def transition_review_task(
    session: AsyncSession,
    *,
    review_task_id: UUID,
    target_state: ReviewTaskState,
    changed_by_user_id: UUID,
    expected_lock_version: int,
    idempotency_key: str,
    now: datetime | None = None,
) -> ReviewTransitionResult:
    """Close an open task once its exact acceptance target is satisfied, or cancel it."""

    normalized_key = _validate_idempotency_key(idempotency_key)
    normalized_target = _validate_terminal_state(target_state)
    expected_version = _validate_lock_version(expected_lock_version)
    scope = f"review-task:{review_task_id}:transition"
    request_sha256 = canonical_sha256(
        {
            "review_task_id": str(review_task_id),
            "target_state": normalized_target.value,
            "changed_by_user_id": str(changed_by_user_id),
            "expected_lock_version": expected_version,
        }
    )
    await _require_active_reviewer(session, changed_by_user_id)
    replay = await _transition_idempotency_replay(
        session,
        scope=scope,
        idempotency_key=normalized_key,
        request_sha256=request_sha256,
    )
    if replay is not None:
        return replay

    task = await _load_task_locked(session, review_task_id)
    if task.state != ReviewTaskState.OPEN:
        raise ReviewConflictError("review task is not open")
    if task.lock_version != expected_version:
        raise ReviewConflictError("review task lock version is stale")
    summary = await _review_summary(session, task)
    if (
        normalized_target == ReviewTaskState.COMPLETED
        and summary.accepted_count != task.desired_accepted_count
    ):
        raise ReviewConflictError("accepted asset count must exactly match the task target")

    changed_at = _as_utc(now or datetime.now(UTC))
    approved_release_id: UUID | None = None
    if normalized_target == ReviewTaskState.COMPLETED:
        approved_release_id = await _freeze_release_selections(
            session,
            task=task,
            frozen_at=changed_at,
            actor_user_id=changed_by_user_id,
            correlation_id=normalized_key,
        )
    values: dict[str, Any] = {
        "state": normalized_target,
        "lock_version": expected_version + 1,
    }
    if normalized_target == ReviewTaskState.COMPLETED:
        values.update(
            completed_by_user_id=changed_by_user_id,
            completed_at=changed_at,
        )
    else:
        values.update(
            cancelled_by_user_id=changed_by_user_id,
            cancelled_at=changed_at,
        )
    try:
        claimed_task_id = await session.scalar(
            update(ReviewTask)
            .where(
                ReviewTask.id == task.id,
                ReviewTask.state == ReviewTaskState.OPEN,
                ReviewTask.lock_version == expected_version,
            )
            .values(**values)
            .returning(ReviewTask.id)
        )
    except IntegrityError as error:
        await session.rollback()
        raise ReviewConflictError("review selection snapshot could not be frozen") from error
    if claimed_task_id is None:
        raise ReviewConflictError("review task was changed concurrently")
    if approved_release_id is not None:
        session.add(
            AuditEvent(
                actor=_audit_actor(changed_by_user_id),
                action="release.review_approved",
                resource_type="release",
                resource_id=approved_release_id,
                correlation_id=normalized_key,
                detail={
                    "review_task_id": str(task.id),
                    "release_version_id": str(task.release_version_id),
                    "phase": ReleasePhase.APPROVED.value,
                },
                occurred_at=changed_at,
            )
        )

    result = ReviewTransitionResult(
        task_id=task.id,
        state=normalized_target,
        lock_version=expected_version + 1,
        accepted_count=summary.accepted_count,
        replayed=False,
    )
    session.add(
        AuditEvent(
            actor=_audit_actor(changed_by_user_id),
            action=f"review.task_{normalized_target.value}",
            resource_type="review_task",
            resource_id=task.id,
            correlation_id=normalized_key,
            detail={
                "previous_state": ReviewTaskState.OPEN.value,
                "state": normalized_target.value,
                "accepted_count": summary.accepted_count,
                "rejected_count": summary.rejected_count,
                "held_count": summary.held_count,
                "undecided_count": summary.undecided_count,
                "desired_accepted_count": task.desired_accepted_count,
                "task_lock_version": expected_version + 1,
            },
            occurred_at=changed_at,
        )
    )
    session.add(
        _idempotency_record(
            scope=scope,
            key=normalized_key,
            request_sha256=request_sha256,
            status=200,
            body=_transition_response_body(result),
            created_at=changed_at,
        )
    )
    try:
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        replay = await _transition_idempotency_replay(
            session,
            scope=scope,
            idempotency_key=normalized_key,
            request_sha256=request_sha256,
        )
        if replay is not None:
            return replay
        raise ReviewConflictError("review task was changed concurrently") from error
    return result


async def set_review_x_selection(
    session: AsyncSession,
    *,
    review_task_id: UUID,
    asset_id: UUID,
    selected: bool,
    selected_by_user_id: UUID,
    expected_lock_version: int,
    now: datetime | None = None,
) -> ReviewXSelectionResult:
    """Select or unselect one ranked image for X while its review is open."""

    if not isinstance(selected, bool):
        raise ReviewInputError("selected must be a boolean")
    expected_version = _validate_lock_version(expected_lock_version)
    await _require_active_owner(session, selected_by_user_id)
    task = await _load_task_locked(session, review_task_id)
    if task.state != ReviewTaskState.OPEN:
        raise ReviewConflictError("X image selection is frozen after review completion")
    if task.lock_version != expected_version:
        raise ReviewConflictError("review task lock version is stale")
    ranked_asset_id = await session.scalar(
        select(AssetRanking.asset_id).where(
            AssetRanking.scoring_run_id == task.scoring_run_id,
            AssetRanking.asset_id == asset_id,
        )
    )
    if ranked_asset_id is None:
        raise ReviewNotFoundError("ranked review asset was not found")

    existing = await session.scalar(
        select(ReviewXSelection)
        .where(
            ReviewXSelection.review_task_id == task.id,
            ReviewXSelection.asset_id == asset_id,
        )
        .with_for_update()
    )
    if selected == (existing is not None):
        task_id = task.id
        selected_count = int(
            await session.scalar(
                select(func.count())
                .select_from(ReviewXSelection)
                .where(ReviewXSelection.review_task_id == task.id)
            )
            or 0
        )
        await session.rollback()
        return ReviewXSelectionResult(
            task_id=task_id,
            asset_id=asset_id,
            selected=selected,
            selected_count=selected_count,
            replayed=True,
        )

    changed_at = _as_utc(now or datetime.now(UTC))
    if selected:
        selected_count = int(
            await session.scalar(
                select(func.count())
                .select_from(ReviewXSelection)
                .where(ReviewXSelection.review_task_id == task.id)
            )
            or 0
        )
        if selected_count >= 4:
            raise ReviewConflictError("at most four images can be selected for one X post")
        session.add(
            ReviewXSelection(
                id=uuid7(),
                review_task_id=task.id,
                asset_id=asset_id,
                selected_by_user_id=selected_by_user_id,
                selected_at=changed_at,
            )
        )
        selected_count += 1
    else:
        assert existing is not None
        selected_count = (
            int(
                await session.scalar(
                    select(func.count())
                    .select_from(ReviewXSelection)
                    .where(ReviewXSelection.review_task_id == task.id)
                )
                or 0
            )
            - 1
        )
        await session.delete(existing)

    session.add(
        AuditEvent(
            actor=_audit_actor(selected_by_user_id),
            action=("review.x_selected" if selected else "review.x_unselected"),
            resource_type="review_task",
            resource_id=task.id,
            correlation_id=f"review-x:{task.id}:{asset_id}:{int(selected)}",
            detail={
                "asset_id": str(asset_id),
                "selected": selected,
                "selected_count": selected_count,
            },
            occurred_at=changed_at,
        )
    )
    try:
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        raise ReviewConflictError("X image selection changed concurrently") from error
    return ReviewXSelectionResult(
        task_id=task.id,
        asset_id=asset_id,
        selected=selected,
        selected_count=selected_count,
        replayed=False,
    )


async def _freeze_release_selections(
    session: AsyncSession,
    *,
    task: ReviewTask,
    frozen_at: datetime,
    actor_user_id: UUID,
    correlation_id: str,
) -> UUID:
    existing_count = int(
        await session.scalar(
            select(func.count())
            .select_from(ReleaseSelection)
            .where(ReleaseSelection.review_task_id == task.id)
        )
        or 0
    )
    if existing_count:
        raise ReviewConflictError("review task already has a frozen selection set")

    release_version = await session.get(ReleaseVersion, task.release_version_id)
    if release_version is None:
        raise ReviewConflictError("review task release version is unavailable")
    release = await session.scalar(
        select(Release).where(Release.id == release_version.release_id).with_for_update()
    )
    if (
        release is None
        or release.current_version_no != release_version.version_no
        or release.phase != ReleasePhase.REVIEWING
    ):
        raise ReviewConflictError(
            "review task is stale or its release phase does not allow completion"
        )
    latest_revisions = (
        select(
            ReviewDecision.asset_id.label("asset_id"),
            func.max(ReviewDecision.revision).label("revision"),
        )
        .where(ReviewDecision.review_task_id == task.id)
        .group_by(ReviewDecision.asset_id)
        .subquery()
    )
    rows = (
        await session.execute(
            select(ReviewDecision, AssetRanking, Asset)
            .join(
                latest_revisions,
                (latest_revisions.c.asset_id == ReviewDecision.asset_id)
                & (latest_revisions.c.revision == ReviewDecision.revision),
            )
            .join(
                AssetRanking,
                (AssetRanking.scoring_run_id == task.scoring_run_id)
                & (AssetRanking.asset_id == ReviewDecision.asset_id),
            )
            .join(Asset, Asset.id == ReviewDecision.asset_id)
            .where(
                ReviewDecision.review_task_id == task.id,
                ReviewDecision.decision == ReviewDecisionValue.ACCEPT,
            )
            .order_by(AssetRanking.rank, Asset.id)
            .with_for_update()
        )
    ).all()
    if len(rows) != task.desired_accepted_count:
        raise ReviewConflictError("accepted source count changed before selection freeze")
    accepted_asset_ids = {decision.asset_id for decision, _, _ in rows}
    x_selected_asset_ids = set(
        (
            await session.scalars(
                select(ReviewXSelection.asset_id).where(ReviewXSelection.review_task_id == task.id)
            )
        ).all()
    )
    if len(x_selected_asset_ids) > 4 or not x_selected_asset_ids.issubset(accepted_asset_ids):
        raise ReviewConflictError(
            "X selections must contain at most four currently accepted images"
        )

    selection_ids: list[str] = []
    for display_order, (decision, ranking, asset) in enumerate(rows, start=1):
        if (
            decision.scoring_run_id != task.scoring_run_id
            or ranking.scoring_run_id != task.scoring_run_id
            or asset.release_id != release_version.release_id
            or asset.kind != AssetKind.RAW_MASTER
            or asset.state != AssetState.AVAILABLE
            or not asset.storage_backend.strip()
            or not asset.storage_bucket.strip()
            or asset.object_key is None
            or not asset.object_key.strip()
            or asset.object_version_id is None
            or not asset.object_version_id.strip()
            or asset.sha256 is None
            or len(asset.sha256) != 64
            or asset.content_type is None
            or not asset.content_type.strip()
            or asset.image_format is None
            or not asset.image_format.strip()
            or asset.width is None
            or asset.width <= 0
            or asset.height is None
            or asset.height <= 0
            or asset.byte_size is None
            or asset.byte_size <= 0
            or asset.available_at is None
            or _stored_as_utc(asset.available_at) > frozen_at
        ):
            raise ReviewConflictError("an accepted raw-master source is unavailable or incomplete")
        selection_id = uuid7()
        selection_ids.append(str(selection_id))
        session.add(
            ReleaseSelection(
                id=selection_id,
                review_task_id=task.id,
                scoring_run_id=task.scoring_run_id,
                review_decision_id=decision.id,
                decision_revision=decision.revision,
                release_version_id=task.release_version_id,
                asset_id=asset.id,
                ranking_rank=ranking.rank,
                display_order=display_order,
                ranking_manifest_sha256=task.ranking_manifest_sha256,
                source_storage_backend=asset.storage_backend,
                source_storage_bucket=asset.storage_bucket,
                source_object_key=asset.object_key,
                source_object_version_id=asset.object_version_id,
                source_sha256=asset.sha256,
                source_content_type=asset.content_type,
                source_image_format=asset.image_format,
                source_width=asset.width,
                source_height=asset.height,
                source_byte_size=asset.byte_size,
                source_available_at=_stored_as_utc(asset.available_at),
                frozen_at=frozen_at,
            )
        )
    try:
        await session.flush()
    except IntegrityError as error:
        await session.rollback()
        raise ReviewConflictError("review selection snapshot could not be frozen") from error
    session.add(
        AuditEvent(
            actor=_audit_actor(actor_user_id),
            action="review.selections_frozen",
            resource_type="review_task",
            resource_id=task.id,
            correlation_id=correlation_id,
            detail={
                "release_version_id": str(task.release_version_id),
                "scoring_run_id": str(task.scoring_run_id),
                "ranking_manifest_sha256": task.ranking_manifest_sha256,
                "selection_ids": selection_ids,
                "selection_count": len(selection_ids),
            },
            occurred_at=frozen_at,
        )
    )
    session.add(
        OutboxEvent(
            topic="review.selections_frozen",
            dedupe_key=f"review.selections_frozen:{task.id}",
            correlation_id=correlation_id,
            aggregate_type="review_task",
            aggregate_id=task.id,
            payload={
                "review_task_id": str(task.id),
                "release_version_id": str(task.release_version_id),
                "selection_count": len(selection_ids),
                "ranking_manifest_sha256": task.ranking_manifest_sha256,
            },
            status=OutboxStatus.PENDING,
            attempts=0,
            max_attempts=10,
            available_at=frozen_at,
            created_at=frozen_at,
        )
    )
    return UUID(str(release.id))


async def get_review_summary(
    session: AsyncSession,
    *,
    review_task_id: UUID,
) -> ReviewSummary:
    task = await session.get(ReviewTask, review_task_id)
    if task is None:
        raise ReviewNotFoundError("review task was not found")
    return await _review_summary(session, task)


async def _review_summary(
    session: AsyncSession,
    task: ReviewTask,
) -> ReviewSummary:
    await _validate_task_ranking_snapshot(session, task, recompute=True)
    latest_revisions = (
        select(
            ReviewDecision.asset_id.label("asset_id"),
            func.max(ReviewDecision.revision).label("revision"),
        )
        .where(ReviewDecision.review_task_id == task.id)
        .group_by(ReviewDecision.asset_id)
        .subquery()
    )
    rows = (
        await session.execute(
            select(AssetRanking, ReviewDecision)
            .outerjoin(
                latest_revisions,
                latest_revisions.c.asset_id == AssetRanking.asset_id,
            )
            .outerjoin(
                ReviewDecision,
                and_(
                    ReviewDecision.review_task_id == task.id,
                    ReviewDecision.asset_id == latest_revisions.c.asset_id,
                    ReviewDecision.revision == latest_revisions.c.revision,
                ),
            )
            .where(AssetRanking.scoring_run_id == task.scoring_run_id)
            .order_by(AssetRanking.rank)
        )
    ).all()
    if len(rows) != task.ranked_asset_count:
        raise ReviewConflictError("review task ranking snapshot is incomplete")

    x_selected_asset_ids = set(
        (
            await session.scalars(
                select(ReviewXSelection.asset_id).where(ReviewXSelection.review_task_id == task.id)
            )
        ).all()
    )
    assets: list[CurrentAssetDecision] = []
    accepted = rejected = held = undecided = 0
    for ranking, decision in rows:
        if decision is None:
            undecided += 1
        elif decision.decision == ReviewDecisionValue.ACCEPT:
            accepted += 1
        elif decision.decision == ReviewDecisionValue.REJECT:
            rejected += 1
        else:
            held += 1
        assets.append(
            CurrentAssetDecision(
                asset_id=ranking.asset_id,
                rank=ranking.rank,
                decision_id=decision.id if decision is not None else None,
                revision=decision.revision if decision is not None else None,
                decision=decision.decision if decision is not None else None,
                reason_code=decision.reason_code if decision is not None else None,
                note=decision.note if decision is not None else None,
                decided_by_user_id=(decision.decided_by_user_id if decision is not None else None),
                decided_at=decision.decided_at if decision is not None else None,
                selected_for_x=ranking.asset_id in x_selected_asset_ids,
            )
        )
    return ReviewSummary(
        task_id=task.id,
        state=task.state,
        lock_version=task.lock_version,
        desired_accepted_count=task.desired_accepted_count,
        ranked_asset_count=task.ranked_asset_count,
        accepted_count=accepted,
        rejected_count=rejected,
        held_count=held,
        undecided_count=undecided,
        x_selected_count=len(x_selected_asset_ids),
        assets=tuple(assets),
    )


async def _validate_task_ranking_snapshot(
    session: AsyncSession,
    task: ReviewTask,
    *,
    recompute: bool,
) -> None:
    run = await session.get(ScoringRun, task.scoring_run_id)
    if (
        run is None
        or run.state != ScoringRunState.COMPLETED
        or run.release_version_id != task.release_version_id
        or run.config_sha256 != task.scoring_config_sha256
        or run.input_manifest_sha256 != task.scoring_input_manifest_sha256
        or run.ranking_manifest_sha256 != task.ranking_manifest_sha256
        or run.asset_count != task.ranked_asset_count
    ):
        raise ReviewConflictError("review task ranking snapshot changed")
    if not recompute:
        return
    rows = await load_ranking_manifest_rows(session, run.id)
    try:
        actual = validate_completed_ranking_manifest(run, rows)
    except RankingManifestIntegrityError as error:
        raise ReviewConflictError("review task ranking snapshot changed") from error
    if actual != task.ranking_manifest_sha256:
        raise ReviewConflictError("review task ranking snapshot changed")


async def _load_task_locked(
    session: AsyncSession,
    review_task_id: UUID,
) -> ReviewTask:
    task = await session.scalar(
        select(ReviewTask).where(ReviewTask.id == review_task_id).with_for_update()
    )
    if task is None:
        raise ReviewNotFoundError("review task was not found")
    return task


async def _require_active_reviewer(
    session: AsyncSession,
    user_id: UUID,
) -> None:
    actor_id = await session.scalar(
        select(AdminUser.id).where(
            AdminUser.id == user_id,
            AdminUser.is_active.is_(True),
            AdminUser.role.in_((AdminRole.OWNER, AdminRole.REVIEWER)),
        )
    )
    if actor_id is None:
        raise ReviewNotFoundError("authorized review actor was not found")


async def _require_active_owner(
    session: AsyncSession,
    user_id: UUID,
) -> None:
    actor_id = await session.scalar(
        select(AdminUser.id).where(
            AdminUser.id == user_id,
            AdminUser.is_active.is_(True),
            AdminUser.role == AdminRole.OWNER,
        )
    )
    if actor_id is None:
        raise ReviewNotFoundError("authorized owner was not found")


async def _task_idempotency_replay(
    session: AsyncSession,
    *,
    scope: str,
    idempotency_key: str,
    request_sha256: str,
) -> ReviewTaskResult | None:
    record = await _load_idempotency_record(
        session,
        scope=scope,
        key=idempotency_key,
        request_sha256=request_sha256,
    )
    if record is None:
        return None
    task_id = _response_uuid(record.response_body, "task_id", "review task")
    task = await session.get(ReviewTask, task_id)
    if task is None:
        raise ReviewConflictError("idempotency record references a missing review task")
    return _task_result(task, replayed=True)


async def _decision_idempotency_replay(
    session: AsyncSession,
    *,
    scope: str,
    idempotency_key: str,
    request_sha256: str,
) -> ReviewDecisionResult | None:
    record = await _load_idempotency_record(
        session,
        scope=scope,
        key=idempotency_key,
        request_sha256=request_sha256,
    )
    if record is None:
        return None
    decision_id = _response_uuid(record.response_body, "decision_id", "review decision")
    task_lock_version = _response_positive_int(
        record.response_body,
        "task_lock_version",
        "review decision",
    )
    decision = await session.get(ReviewDecision, decision_id)
    if decision is None:
        raise ReviewConflictError("idempotency record references a missing review decision")
    return _decision_result(
        decision,
        task_lock_version=task_lock_version,
        replayed=True,
    )


async def _transition_idempotency_replay(
    session: AsyncSession,
    *,
    scope: str,
    idempotency_key: str,
    request_sha256: str,
) -> ReviewTransitionResult | None:
    record = await _load_idempotency_record(
        session,
        scope=scope,
        key=idempotency_key,
        request_sha256=request_sha256,
    )
    if record is None:
        return None
    body = record.response_body
    task_id = _response_uuid(body, "task_id", "review transition")
    task = await session.get(ReviewTask, task_id)
    if task is None:
        raise ReviewConflictError("idempotency record references a missing review task")
    try:
        state = ReviewTaskState(str(body["state"]))
    except (KeyError, ValueError):
        raise ReviewConflictError("review transition idempotency record is invalid") from None
    accepted_count = _response_nonnegative_int(
        body,
        "accepted_count",
        "review transition",
    )
    lock_version = _response_positive_int(
        body,
        "lock_version",
        "review transition",
    )
    return ReviewTransitionResult(
        task_id=task.id,
        state=state,
        lock_version=lock_version,
        accepted_count=accepted_count,
        replayed=True,
    )


async def _load_idempotency_record(
    session: AsyncSession,
    *,
    scope: str,
    key: str,
    request_sha256: str,
) -> IdempotencyRecord | None:
    record = await session.scalar(
        select(IdempotencyRecord).where(
            IdempotencyRecord.scope == scope,
            IdempotencyRecord.idempotency_key == key,
        )
    )
    if record is not None and record.request_sha256 != request_sha256:
        raise ReviewConflictError("idempotency key was already used for another request")
    return record


def _task_result(task: ReviewTask, *, replayed: bool) -> ReviewTaskResult:
    return ReviewTaskResult(
        task_id=task.id,
        release_version_id=task.release_version_id,
        scoring_run_id=task.scoring_run_id,
        desired_accepted_count=task.desired_accepted_count,
        ranked_asset_count=task.ranked_asset_count,
        state=task.state,
        lock_version=task.lock_version,
        replayed=replayed,
    )


def _decision_result(
    decision: ReviewDecision,
    *,
    task_lock_version: int,
    replayed: bool,
) -> ReviewDecisionResult:
    return ReviewDecisionResult(
        decision_id=decision.id,
        task_id=decision.review_task_id,
        asset_id=decision.asset_id,
        revision=decision.revision,
        decision=decision.decision,
        reason_code=decision.reason_code,
        note=decision.note,
        decided_by_user_id=decision.decided_by_user_id,
        supersedes_decision_id=decision.supersedes_decision_id,
        task_lock_version=task_lock_version,
        replayed=replayed,
    )


def _idempotency_record(
    *,
    scope: str,
    key: str,
    request_sha256: str,
    status: int,
    body: dict[str, Any],
    created_at: datetime,
) -> IdempotencyRecord:
    return IdempotencyRecord(
        scope=scope,
        idempotency_key=key,
        request_sha256=request_sha256,
        response_status=status,
        response_body=body,
        created_at=created_at,
        expires_at=None,
    )


def _task_response_body(result: ReviewTaskResult) -> dict[str, Any]:
    return {
        "schema": "review-task-result/v1",
        "task_id": str(result.task_id),
        "release_version_id": str(result.release_version_id),
        "scoring_run_id": str(result.scoring_run_id),
        "desired_accepted_count": result.desired_accepted_count,
        "ranked_asset_count": result.ranked_asset_count,
        "state": result.state.value,
        "lock_version": result.lock_version,
    }


def _decision_response_body(result: ReviewDecisionResult) -> dict[str, Any]:
    return {
        "schema": "review-decision-result/v1",
        "decision_id": str(result.decision_id),
        "task_id": str(result.task_id),
        "asset_id": str(result.asset_id),
        "revision": result.revision,
        "decision": result.decision.value,
        "task_lock_version": result.task_lock_version,
    }


def _transition_response_body(result: ReviewTransitionResult) -> dict[str, Any]:
    return {
        "schema": "review-transition-result/v1",
        "task_id": str(result.task_id),
        "state": result.state.value,
        "lock_version": result.lock_version,
        "accepted_count": result.accepted_count,
    }


def _response_uuid(
    body: dict[str, Any],
    key: str,
    resource: str,
) -> UUID:
    try:
        return UUID(str(body[key]))
    except (KeyError, ValueError):
        raise ReviewConflictError(f"{resource} idempotency record is invalid") from None


def _response_positive_int(
    body: dict[str, Any],
    key: str,
    resource: str,
) -> int:
    value = body.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ReviewConflictError(f"{resource} idempotency record is invalid")
    return value


def _response_nonnegative_int(
    body: dict[str, Any],
    key: str,
    resource: str,
) -> int:
    value = body.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ReviewConflictError(f"{resource} idempotency record is invalid")
    return value


def _validate_idempotency_key(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > _MAX_IDEMPOTENCY_KEY_LENGTH
    ):
        raise ReviewInputError("idempotency_key must be 1 to 200 non-whitespace characters")
    return value


def _validate_lock_version(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ReviewInputError("expected_lock_version must be a positive integer")
    return value


def _validate_decision(value: ReviewDecisionValue) -> ReviewDecisionValue:
    try:
        return ReviewDecisionValue(value)
    except ValueError:
        raise ReviewInputError("decision must be accept, reject, or hold") from None


def _validate_terminal_state(value: ReviewTaskState) -> ReviewTaskState:
    try:
        state = ReviewTaskState(value)
    except ValueError:
        raise ReviewInputError("target_state is invalid") from None
    if state not in {ReviewTaskState.COMPLETED, ReviewTaskState.CANCELLED}:
        raise ReviewInputError("target_state must be completed or cancelled")
    return state


def _normalize_reason_code(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not _REASON_CODE_PATTERN.fullmatch(normalized):
        raise ReviewInputError("reason_code must be a lowercase machine identifier")
    return normalized


def _normalize_note(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized or len(normalized) > _MAX_NOTE_LENGTH:
        raise ReviewInputError("note must be between 1 and 4000 characters")
    return normalized


def _audit_actor(user_id: UUID) -> str:
    return f"admin-user:{user_id}"


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ReviewInputError("timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _stored_as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
