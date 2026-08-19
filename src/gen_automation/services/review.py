import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import and_, func, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from gen_automation.db.models import (
    AdminUser,
    Asset,
    AssetRanking,
    AuditEvent,
    GenerationJob,
    IdempotencyRecord,
    OutboxEvent,
    PublicationIntent,
    Release,
    ReleaseSelection,
    ReleaseVersion,
    ReviewAssetInspection,
    ReviewDecision,
    ReviewTask,
    ReviewXSelection,
    ScoringRun,
    SemanticAssessment,
    XTeaserRevisionHead,
)
from gen_automation.domain.canonical import canonical_sha256
from gen_automation.domain.enums import (
    AdminRole,
    AssetKind,
    AssetState,
    OutboxStatus,
    PublicationIntentState,
    PublicationTarget,
    ReleasePhase,
    ReviewBulkAction,
    ReviewDecisionValue,
    ReviewTaskState,
    ScoringRunState,
    SemanticAssessmentState,
    SemanticEnforcementMode,
    SemanticVerdict,
)
from gen_automation.domain.ids import uuid7
from gen_automation.services.generation_positions import (
    generation_ordinal,
    generation_queue_offsets,
)
from gen_automation.services.ranking_manifest import (
    RankingManifestIntegrityError,
    load_ranking_manifest_rows,
    validate_completed_ranking_manifest,
)
from gen_automation.services.semantic_review_learning import ANATOMY_REASON_CODES

_REASON_CODE_PATTERN = re.compile(r"[a-z][a-z0-9_.-]{0,99}")
_MAX_NOTE_LENGTH = 4_000
_MAX_IDEMPOTENCY_KEY_LENGTH = 200
_MAX_BULK_ASSET_COUNT = 500
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_RETRYABLE_TRANSITION_SQLSTATES = frozenset({"55P03", "57014"})
_REVIEW_TRANSITION_LOCK_TIMEOUT = "5s"
_REVIEW_TRANSITION_STATEMENT_TIMEOUT = "20s"
_BLOCKING_X_PUBLICATION_STATES = tuple(
    state
    for state in PublicationIntentState
    if state not in {PublicationIntentState.FAILED, PublicationIntentState.CANCELLED}
)

SEMANTIC_SEVERE_OVERRIDE_REASON_CODE = "semantic_severe_override"
SEMANTIC_SEVERE_OVERRIDE_AUDIT_ACTION = "review.semantic_severe_overridden"
SORTING_DEFAULT_ACCEPT_REASON_CODE = "sorting_default_accept"


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
class ReviewXSelectionEditability:
    allowed: bool
    blocked_reason: str | None


@dataclass(frozen=True, slots=True)
class ReviewBulkActionResult:
    task_id: UUID
    action: ReviewBulkAction
    asset_ids: tuple[UUID, ...]
    changed_count: int
    x_selected_count: int
    task_lock_version: int
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
    semantic_severe_override_attested: bool


@dataclass(frozen=True, slots=True)
class ReviewSemanticGate:
    enabled: bool
    mode: SemanticEnforcementMode
    ranked_asset_count: int
    terminal_count: int
    pending_count: int
    unavailable_count: int
    severe_count: int
    severe_override_count: int
    severe_blocked_count: int

    @property
    def completion_ready(self) -> bool:
        return (
            not self.enabled
            or self.mode != SemanticEnforcementMode.ENFORCE
            or (self.pending_count == 0 and self.severe_blocked_count == 0)
        )


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
    semantic_gate: ReviewSemanticGate


async def create_review_task(
    session: AsyncSession,
    *,
    scoring_run_id: UUID,
    created_by_user_id: UUID,
    idempotency_key: str,
    default_accept_ranked_assets: bool = False,
    now: datetime | None = None,
) -> ReviewTaskResult:
    """Create one review task from an exact, completed ranking snapshot."""

    normalized_key = _validate_idempotency_key(idempotency_key)
    if not isinstance(default_accept_ranked_assets, bool):
        raise ReviewInputError("default acceptance mode must be boolean")
    scope = f"scoring-run:{scoring_run_id}:create-review-task"
    request_sha256 = canonical_sha256(
        {
            "scoring_run_id": str(scoring_run_id),
            "created_by_user_id": str(created_by_user_id),
            "default_accept_ranked_assets": default_accept_ranked_assets,
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
                "default_accept_ranked_assets": default_accept_ranked_assets,
            },
            occurred_at=created_at,
        )
    )
    if default_accept_ranked_assets:
        default_decisions = tuple(
            ReviewDecision(
                id=uuid7(),
                review_task_id=task.id,
                scoring_run_id=task.scoring_run_id,
                asset_id=ranking.asset_id,
                revision=1,
                decision=ReviewDecisionValue.ACCEPT,
                reason_code=SORTING_DEFAULT_ACCEPT_REASON_CODE,
                note=None,
                decided_by_user_id=created_by_user_id,
                decided_at=created_at,
                supersedes_revision=None,
                supersedes_decision_id=None,
            )
            for ranking, _score in ranking_rows
        )
        session.add_all(default_decisions)
        session.add(
            AuditEvent(
                actor=_audit_actor(created_by_user_id),
                action="review.default_acceptance_seeded",
                resource_type="review_task",
                resource_id=task.id,
                correlation_id=normalized_key,
                detail={
                    "scoring_run_id": str(task.scoring_run_id),
                    "accepted_count": len(default_decisions),
                    "reason_code": SORTING_DEFAULT_ACCEPT_REASON_CODE,
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
        if default_accept_ranked_assets:
            # ReviewDecision has a database-level task-state trigger but no ORM
            # relationship that lets SQLAlchemy infer insert order. Flush only the
            # open parent before the seeded decisions are flushed at commit.
            await session.flush((task,))
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
    semantic_profile_sha256: str | None = None,
    semantic_severe_confidence_micros: int = 900_000,
    semantic_enforcement_mode: SemanticEnforcementMode = SemanticEnforcementMode.ENFORCE,
    now: datetime | None = None,
) -> ReviewDecisionResult:
    """Append an attributed decision revision without mutating its raw asset."""

    normalized_key = _validate_idempotency_key(idempotency_key)
    normalized_decision = _validate_decision(decision)
    normalized_reason = _normalize_reason_code(reason_code)
    normalized_reason = _reason_compatible_with_decision(
        decision=normalized_decision,
        reason_code=normalized_reason,
    )
    normalized_note = _normalize_note(note)
    normalized_semantic_profile = _normalize_semantic_profile_sha256(semantic_profile_sha256)
    semantic_threshold = _validate_semantic_confidence_threshold(semantic_severe_confidence_micros)
    semantic_mode = _validate_semantic_enforcement_mode(semantic_enforcement_mode)
    expected_version = _validate_lock_version(expected_lock_version)
    scope = f"review-task:{review_task_id}:append-decision"
    request_sha256 = _decision_request_sha256(
        review_task_id=review_task_id,
        asset_id=asset_id,
        decision=normalized_decision,
        decided_by_user_id=decided_by_user_id,
        expected_lock_version=expected_version,
        reason_code=normalized_reason,
        note=normalized_note,
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

    semantic_override = False
    if (
        semantic_mode == SemanticEnforcementMode.ENFORCE
        and normalized_semantic_profile is not None
        and normalized_decision == ReviewDecisionValue.ACCEPT
    ):
        assessment = await session.scalar(
            select(SemanticAssessment).where(
                SemanticAssessment.scoring_run_id == task.scoring_run_id,
                SemanticAssessment.asset_id == asset_id,
                SemanticAssessment.profile_sha256 == normalized_semantic_profile,
            )
        )
        if _is_high_confidence_severe_assessment(
            assessment,
            threshold_micros=semantic_threshold,
        ):
            if normalized_reason != SEMANTIC_SEVERE_OVERRIDE_REASON_CODE or normalized_note is None:
                raise ReviewConflictError(
                    "high-confidence severe anatomy requires an explicit owner override"
                )
            await _require_active_owner(session, decided_by_user_id)
            semantic_override = True

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
    if semantic_override:
        session.add(
            AuditEvent(
                actor=_audit_actor(decided_by_user_id),
                action=SEMANTIC_SEVERE_OVERRIDE_AUDIT_ACTION,
                resource_type="review_decision",
                resource_id=stored.id,
                correlation_id=normalized_key,
                detail={
                    "review_task_id": str(task.id),
                    "scoring_run_id": str(task.scoring_run_id),
                    "asset_id": str(asset_id),
                    "decision_revision": revision,
                    "semantic_profile_sha256": normalized_semantic_profile,
                    "semantic_severe_confidence_micros": semantic_threshold,
                    "reason_code": SEMANTIC_SEVERE_OVERRIDE_REASON_CODE,
                    "attestation": "owner reviewed and explicitly accepted severe anatomy",
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


async def apply_bulk_review_action(
    session: AsyncSession,
    *,
    review_task_id: UUID,
    asset_ids: Sequence[UUID],
    action: ReviewBulkAction,
    changed_by_user_id: UUID,
    expected_lock_version: int,
    idempotency_key: str,
    reason_code: str | None = None,
    note: str | None = None,
    semantic_profile_sha256: str | None = None,
    semantic_severe_confidence_micros: int = 900_000,
    semantic_enforcement_mode: SemanticEnforcementMode = SemanticEnforcementMode.ENFORCE,
    now: datetime | None = None,
) -> ReviewBulkActionResult:
    """Apply one atomic review action to a bounded set of ranked assets."""

    normalized_key = _validate_idempotency_key(idempotency_key)
    normalized_action = _validate_bulk_action(action)
    normalized_asset_ids = _normalize_bulk_asset_ids(asset_ids)
    normalized_reason = _normalize_reason_code(reason_code)
    normalized_note = _normalize_note(note)
    normalized_semantic_profile = _normalize_semantic_profile_sha256(semantic_profile_sha256)
    semantic_threshold = _validate_semantic_confidence_threshold(semantic_severe_confidence_micros)
    semantic_mode = _validate_semantic_enforcement_mode(semantic_enforcement_mode)
    expected_version = _validate_lock_version(expected_lock_version)
    decision_actions = {
        ReviewBulkAction.ACCEPT,
        ReviewBulkAction.REJECT,
        ReviewBulkAction.HOLD,
    }
    if normalized_action in decision_actions:
        normalized_reason = _reason_compatible_with_decision(
            decision=ReviewDecisionValue(normalized_action.value),
            reason_code=normalized_reason,
        )
    if normalized_action not in decision_actions and (
        normalized_reason is not None or normalized_note is not None
    ):
        raise ReviewInputError("reason_code and note are only valid for review decisions")

    scope = f"review-task:{review_task_id}:bulk-action"
    request_sha256 = canonical_sha256(
        {
            "review_task_id": str(review_task_id),
            "asset_ids": [str(asset_id) for asset_id in normalized_asset_ids],
            "action": normalized_action.value,
            "changed_by_user_id": str(changed_by_user_id),
            "expected_lock_version": expected_version,
            "reason_code": normalized_reason,
            "note": normalized_note,
            "semantic_profile_sha256": normalized_semantic_profile,
            "semantic_severe_confidence_micros": semantic_threshold,
            "semantic_enforcement_mode": semantic_mode.value,
        }
    )
    if normalized_action in decision_actions:
        await _require_active_reviewer(session, changed_by_user_id)
    else:
        await _require_active_owner(session, changed_by_user_id)
    replay = await _bulk_action_idempotency_replay(
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

    ranked_asset_ids = set(
        (
            await session.scalars(
                select(AssetRanking.asset_id).where(
                    AssetRanking.scoring_run_id == task.scoring_run_id,
                    AssetRanking.asset_id.in_(normalized_asset_ids),
                )
            )
        ).all()
    )
    if ranked_asset_ids != set(normalized_asset_ids):
        raise ReviewNotFoundError("one or more ranked review assets were not found")

    changed_at = _as_utc(now or datetime.now(UTC))
    changed_count = 0
    x_selected_count: int
    severe_override_asset_ids: set[UUID] = set()

    if normalized_action in decision_actions:
        decision = ReviewDecisionValue(normalized_action.value)
        if (
            semantic_mode == SemanticEnforcementMode.ENFORCE
            and normalized_semantic_profile is not None
            and decision == ReviewDecisionValue.ACCEPT
        ):
            assessments = (
                await session.scalars(
                    select(SemanticAssessment).where(
                        SemanticAssessment.scoring_run_id == task.scoring_run_id,
                        SemanticAssessment.asset_id.in_(normalized_asset_ids),
                        SemanticAssessment.profile_sha256 == normalized_semantic_profile,
                    )
                )
            ).all()
            severe_override_asset_ids = {
                assessment.asset_id
                for assessment in assessments
                if _is_high_confidence_severe_assessment(
                    assessment,
                    threshold_micros=semantic_threshold,
                )
            }
            if severe_override_asset_ids:
                if (
                    normalized_reason != SEMANTIC_SEVERE_OVERRIDE_REASON_CODE
                    or normalized_note is None
                ):
                    raise ReviewConflictError(
                        "high-confidence severe anatomy requires an explicit owner override"
                    )
                await _require_active_owner(session, changed_by_user_id)

        prior_rows = (
            await session.scalars(
                _latest_review_decisions_for_update_statement(
                    review_task_id=task.id,
                    asset_ids=normalized_asset_ids,
                )
            )
        ).all()
        priors = {prior.asset_id: prior for prior in prior_rows}
        await _claim_review_task_version(
            session,
            task=task,
            expected_lock_version=expected_version,
        )
        for asset_id in normalized_asset_ids:
            prior = priors.get(asset_id)
            revision = 1 if prior is None else prior.revision + 1
            stored = ReviewDecision(
                id=uuid7(),
                review_task_id=task.id,
                scoring_run_id=task.scoring_run_id,
                asset_id=asset_id,
                revision=revision,
                decision=decision,
                reason_code=normalized_reason,
                note=normalized_note,
                decided_by_user_id=changed_by_user_id,
                decided_at=changed_at,
                supersedes_revision=prior.revision if prior is not None else None,
                supersedes_decision_id=prior.id if prior is not None else None,
            )
            session.add(stored)
            session.add(
                AuditEvent(
                    actor=_audit_actor(changed_by_user_id),
                    action="review.decision_appended",
                    resource_type="review_decision",
                    resource_id=stored.id,
                    correlation_id=normalized_key,
                    detail={
                        "review_task_id": str(task.id),
                        "asset_id": str(asset_id),
                        "revision": revision,
                        "decision": decision.value,
                        "reason_code": normalized_reason,
                        "note_present": normalized_note is not None,
                        "supersedes_decision_id": str(prior.id) if prior is not None else None,
                        "task_lock_version": expected_version + 1,
                        "bulk_action": True,
                    },
                    occurred_at=changed_at,
                )
            )
            if asset_id in severe_override_asset_ids:
                session.add(
                    AuditEvent(
                        actor=_audit_actor(changed_by_user_id),
                        action=SEMANTIC_SEVERE_OVERRIDE_AUDIT_ACTION,
                        resource_type="review_decision",
                        resource_id=stored.id,
                        correlation_id=normalized_key,
                        detail={
                            "review_task_id": str(task.id),
                            "scoring_run_id": str(task.scoring_run_id),
                            "asset_id": str(asset_id),
                            "decision_revision": revision,
                            "semantic_profile_sha256": normalized_semantic_profile,
                            "semantic_severe_confidence_micros": semantic_threshold,
                            "reason_code": SEMANTIC_SEVERE_OVERRIDE_REASON_CODE,
                            "attestation": (
                                "owner reviewed and explicitly accepted severe anatomy"
                            ),
                            "bulk_action": True,
                        },
                        occurred_at=changed_at,
                    )
                )
        changed_count = len(normalized_asset_ids)
        task_lock_version = expected_version + 1
        x_selected_count = await _review_x_selected_count(session, task.id)
    else:
        selected_rows = (
            await session.scalars(
                select(ReviewXSelection)
                .where(ReviewXSelection.review_task_id == task.id)
                .with_for_update()
            )
        ).all()
        selected_by_asset = {row.asset_id: row for row in selected_rows}
        if normalized_action == ReviewBulkAction.X_ADD:
            changed_asset_ids = tuple(
                asset_id for asset_id in normalized_asset_ids if asset_id not in selected_by_asset
            )
            if len(selected_rows) + len(changed_asset_ids) > 4:
                raise ReviewConflictError("at most four images can be selected for one X post")
        else:
            changed_asset_ids = tuple(
                asset_id for asset_id in normalized_asset_ids if asset_id in selected_by_asset
            )

        await _claim_review_task_version(
            session,
            task=task,
            expected_lock_version=expected_version,
        )
        if changed_asset_ids:
            for asset_id in changed_asset_ids:
                if normalized_action == ReviewBulkAction.X_ADD:
                    session.add(
                        ReviewXSelection(
                            id=uuid7(),
                            review_task_id=task.id,
                            asset_id=asset_id,
                            selected_by_user_id=changed_by_user_id,
                            selected_at=changed_at,
                        )
                    )
                else:
                    await session.delete(selected_by_asset[asset_id])
                session.add(
                    AuditEvent(
                        actor=_audit_actor(changed_by_user_id),
                        action=(
                            "review.x_selected"
                            if normalized_action == ReviewBulkAction.X_ADD
                            else "review.x_unselected"
                        ),
                        resource_type="review_task",
                        resource_id=task.id,
                        correlation_id=normalized_key,
                        detail={
                            "asset_id": str(asset_id),
                            "selected": normalized_action == ReviewBulkAction.X_ADD,
                            "bulk_action": True,
                        },
                        occurred_at=changed_at,
                    )
                )
        changed_count = len(changed_asset_ids)
        task_lock_version = expected_version + 1
        x_selected_count = len(selected_rows) + (
            changed_count if normalized_action == ReviewBulkAction.X_ADD else -changed_count
        )

    result = ReviewBulkActionResult(
        task_id=task.id,
        action=normalized_action,
        asset_ids=normalized_asset_ids,
        changed_count=changed_count,
        x_selected_count=x_selected_count,
        task_lock_version=task_lock_version,
        replayed=False,
    )
    session.add(
        AuditEvent(
            actor=_audit_actor(changed_by_user_id),
            action="review.bulk_action_applied",
            resource_type="review_task",
            resource_id=task.id,
            correlation_id=normalized_key,
            detail={
                "action": normalized_action.value,
                "requested_asset_count": len(normalized_asset_ids),
                "changed_count": changed_count,
                "x_selected_count": x_selected_count,
                "task_lock_version": task_lock_version,
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
            body=_bulk_action_response_body(result),
            created_at=changed_at,
        )
    )
    try:
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        replay = await _bulk_action_idempotency_replay(
            session,
            scope=scope,
            idempotency_key=normalized_key,
            request_sha256=request_sha256,
        )
        if replay is not None:
            return replay
        raise ReviewConflictError("bulk review action changed concurrently") from error
    return result


async def _configure_review_transition_timeouts(session: AsyncSession) -> None:
    """Bound lock and statement waits for the current PostgreSQL transaction."""

    if session.get_bind().dialect.name != "postgresql":
        return
    await session.execute(text(f"SET LOCAL lock_timeout = '{_REVIEW_TRANSITION_LOCK_TIMEOUT}'"))
    await session.execute(
        text(f"SET LOCAL statement_timeout = '{_REVIEW_TRANSITION_STATEMENT_TIMEOUT}'")
    )


def _is_retryable_transition_database_timeout(error: DBAPIError) -> bool:
    candidate: BaseException | None = error.orig
    visited: set[int] = set()
    while candidate is not None and id(candidate) not in visited:
        visited.add(id(candidate))
        sqlstate = getattr(candidate, "sqlstate", None) or getattr(candidate, "pgcode", None)
        if isinstance(sqlstate, str):
            return sqlstate.strip().upper() in _RETRYABLE_TRANSITION_SQLSTATES
        candidate = candidate.__cause__ or candidate.__context__
    return False


async def transition_review_task(
    session: AsyncSession,
    *,
    review_task_id: UUID,
    target_state: ReviewTaskState,
    changed_by_user_id: UUID,
    expected_lock_version: int,
    idempotency_key: str,
    inspected_asset_ids: Sequence[UUID] = (),
    semantic_profile_sha256: str | None = None,
    semantic_severe_confidence_micros: int = 900_000,
    semantic_enforcement_mode: SemanticEnforcementMode = SemanticEnforcementMode.ENFORCE,
    now: datetime | None = None,
) -> ReviewTransitionResult:
    """Run a terminal transition with bounded PostgreSQL lock and query waits."""

    try:
        return await _transition_review_task(
            session,
            review_task_id=review_task_id,
            target_state=target_state,
            changed_by_user_id=changed_by_user_id,
            expected_lock_version=expected_lock_version,
            idempotency_key=idempotency_key,
            inspected_asset_ids=inspected_asset_ids,
            semantic_profile_sha256=semantic_profile_sha256,
            semantic_severe_confidence_micros=semantic_severe_confidence_micros,
            semantic_enforcement_mode=semantic_enforcement_mode,
            now=now,
        )
    except DBAPIError as error:
        if not _is_retryable_transition_database_timeout(error):
            raise
        await session.rollback()
        raise ReviewConflictError(
            "review transition timed out waiting for concurrent work; please retry"
        ) from error


async def _transition_review_task(
    session: AsyncSession,
    *,
    review_task_id: UUID,
    target_state: ReviewTaskState,
    changed_by_user_id: UUID,
    expected_lock_version: int,
    idempotency_key: str,
    inspected_asset_ids: Sequence[UUID] = (),
    semantic_profile_sha256: str | None = None,
    semantic_severe_confidence_micros: int = 900_000,
    semantic_enforcement_mode: SemanticEnforcementMode = SemanticEnforcementMode.ENFORCE,
    now: datetime | None = None,
) -> ReviewTransitionResult:
    """Close or cancel an open task.

    Completion treats the configured acceptance target as a ceiling. At least
    one asset must be accepted, and the accepted count is frozen as the task's
    final exact target in the same terminal transition.
    """

    normalized_key = _validate_idempotency_key(idempotency_key)
    normalized_target = _validate_terminal_state(target_state)
    normalized_semantic_profile = _normalize_semantic_profile_sha256(semantic_profile_sha256)
    semantic_threshold = _validate_semantic_confidence_threshold(semantic_severe_confidence_micros)
    semantic_mode = _validate_semantic_enforcement_mode(semantic_enforcement_mode)
    expected_version = _validate_lock_version(expected_lock_version)
    normalized_inspected_asset_ids = _normalize_optional_asset_ids(
        inspected_asset_ids,
        label="inspected_asset_ids",
    )
    if normalized_target != ReviewTaskState.COMPLETED and normalized_inspected_asset_ids:
        raise ReviewInputError("inspected_asset_ids may only accompany review completion")
    scope = f"review-task:{review_task_id}:transition"
    request_sha256 = canonical_sha256(
        {
            "review_task_id": str(review_task_id),
            "target_state": normalized_target.value,
            "changed_by_user_id": str(changed_by_user_id),
            "expected_lock_version": expected_version,
            "semantic_profile_sha256": normalized_semantic_profile,
            "semantic_severe_confidence_micros": semantic_threshold,
            "semantic_enforcement_mode": semantic_mode.value,
        }
    )
    await _configure_review_transition_timeouts(session)
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
    configured_accepted_count = task.desired_accepted_count
    changed_at = _as_utc(now or datetime.now(UTC))
    await _record_completion_inspections(
        session,
        task=task,
        asset_ids=normalized_inspected_asset_ids,
        inspected_by_user_id=changed_by_user_id,
        inspected_at=changed_at,
    )
    summary = await _review_summary(
        session,
        task,
        semantic_profile_sha256=(
            normalized_semantic_profile if normalized_target == ReviewTaskState.COMPLETED else None
        ),
        semantic_severe_confidence_micros=semantic_threshold,
        semantic_enforcement_mode=semantic_mode,
    )
    if normalized_target == ReviewTaskState.COMPLETED and summary.accepted_count < 1:
        raise ReviewConflictError("at least one asset must be accepted before completion")
    if (
        normalized_target == ReviewTaskState.COMPLETED
        and summary.accepted_count > configured_accepted_count
    ):
        raise ReviewConflictError("accepted asset count exceeds the configured task target")
    if (
        normalized_target == ReviewTaskState.COMPLETED
        and semantic_mode == SemanticEnforcementMode.ENFORCE
        and summary.semantic_gate.pending_count
    ):
        raise ReviewConflictError("configured semantic anatomy assessments are not terminal")
    if (
        normalized_target == ReviewTaskState.COMPLETED
        and semantic_mode == SemanticEnforcementMode.ENFORCE
        and summary.semantic_gate.severe_blocked_count
    ):
        raise ReviewConflictError("accepted severe anatomy requires an explicit owner override")

    approved_release_id: UUID | None = None
    if normalized_target == ReviewTaskState.COMPLETED:
        approved_release_id = await _freeze_release_selections(
            session,
            task=task,
            final_accepted_count=summary.accepted_count,
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
            desired_accepted_count=summary.accepted_count,
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
                    "configured_accepted_count": configured_accepted_count,
                    "final_accepted_count": summary.accepted_count,
                },
                occurred_at=changed_at,
            )
        )

    terminal_detail: dict[str, Any] = {
        "previous_state": ReviewTaskState.OPEN.value,
        "state": normalized_target.value,
        "accepted_count": summary.accepted_count,
        "rejected_count": summary.rejected_count,
        "held_count": summary.held_count,
        "undecided_count": summary.undecided_count,
        "desired_accepted_count": (
            summary.accepted_count
            if normalized_target == ReviewTaskState.COMPLETED
            else configured_accepted_count
        ),
        "semantic_gate_enabled": summary.semantic_gate.enabled,
        "semantic_terminal_count": summary.semantic_gate.terminal_count,
        "semantic_unavailable_count": summary.semantic_gate.unavailable_count,
        "semantic_severe_override_count": summary.semantic_gate.severe_override_count,
        "semantic_learning_status": (
            "deferred_to_reconciler"
            if normalized_target == ReviewTaskState.COMPLETED
            and normalized_semantic_profile is not None
            else "not_requested"
        ),
        "semantic_learning_inferred_good_count": 0,
        "semantic_learning_inferred_defect_count": 0,
        "semantic_learning_skipped_existing_count": 0,
        "semantic_learning_error": None,
        "task_lock_version": expected_version + 1,
    }
    if normalized_target == ReviewTaskState.COMPLETED:
        terminal_detail.update(
            configured_accepted_count=configured_accepted_count,
            final_accepted_count=summary.accepted_count,
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
            detail=terminal_detail,
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


async def get_review_x_selection_editability(
    session: AsyncSession,
    *,
    review_task_id: UUID,
) -> ReviewXSelectionEditability:
    """Return whether the owner may still change this review's X image choices."""

    task = await session.get(ReviewTask, review_task_id)
    if task is None:
        raise ReviewNotFoundError("review task was not found")
    return await _review_x_selection_editability(session, task=task, lock=False)


async def _review_x_selection_editability(
    session: AsyncSession,
    *,
    task: ReviewTask,
    lock: bool,
) -> ReviewXSelectionEditability:
    if task.state == ReviewTaskState.OPEN:
        return ReviewXSelectionEditability(allowed=True, blocked_reason=None)
    if task.state != ReviewTaskState.COMPLETED:
        return ReviewXSelectionEditability(
            allowed=False,
            blocked_reason="X image choices are unavailable for a cancelled review.",
        )

    revision_query = select(XTeaserRevisionHead).where(
        XTeaserRevisionHead.review_task_id == task.id
    )
    if lock:
        revision_query = revision_query.with_for_update()
    revision_head = await session.scalar(revision_query)
    if revision_head is not None and (
        revision_head.active_revision_id is not None
        or revision_head.pending_revision_id is not None
    ):
        return ReviewXSelectionEditability(
            allowed=False,
            blocked_reason=(
                "X image choices are locked because watermarked X teasers have already "
                "been prepared."
            ),
        )

    publication_query = select(PublicationIntent).where(
        PublicationIntent.release_version_id == task.release_version_id,
        PublicationIntent.target == PublicationTarget.X,
        PublicationIntent.state.in_(_BLOCKING_X_PUBLICATION_STATES),
    )
    if lock:
        publication_query = publication_query.with_for_update()
    if await session.scalar(publication_query) is not None:
        return ReviewXSelectionEditability(
            allowed=False,
            blocked_reason="X image choices are locked because X delivery has been prepared.",
        )
    return ReviewXSelectionEditability(allowed=True, blocked_reason=None)


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
    """Select an X image during review or after finalization but before X preparation."""

    if not isinstance(selected, bool):
        raise ReviewInputError("selected must be a boolean")
    expected_version = _validate_lock_version(expected_lock_version)
    await _require_active_owner(session, selected_by_user_id)
    task = await _load_task_locked(session, review_task_id)
    if task.lock_version != expected_version:
        raise ReviewConflictError("review task lock version is stale")
    editability = await _review_x_selection_editability(session, task=task, lock=True)
    if not editability.allowed:
        raise ReviewConflictError(editability.blocked_reason or "X image selection is unavailable")
    if task.state == ReviewTaskState.COMPLETED:
        accepted_asset_id = await session.scalar(
            select(ReleaseSelection.asset_id).where(
                ReleaseSelection.review_task_id == task.id,
                ReleaseSelection.asset_id == asset_id,
            )
        )
        if accepted_asset_id is None:
            raise ReviewConflictError("only images in the finalized set can be selected for X")
    else:
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
                "post_completion": task.state == ReviewTaskState.COMPLETED,
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


async def _record_completion_inspections(
    session: AsyncSession,
    *,
    task: ReviewTask,
    asset_ids: tuple[UUID, ...],
    inspected_by_user_id: UUID,
    inspected_at: datetime,
) -> None:
    """Persist the browser's final viewed-image snapshot under the task lock.

    The standalone inspection endpoint and completion both lock the same review
    task. Whichever request wins is committed first; this transaction then only
    inserts the missing immutable rows. That keeps completion independent from a
    slow browser telemetry request without losing positive-learning eligibility.
    """

    if not asset_ids:
        return
    ranked_asset_ids = set(
        await session.scalars(
            select(AssetRanking.asset_id).where(
                AssetRanking.scoring_run_id == task.scoring_run_id,
                AssetRanking.asset_id.in_(asset_ids),
            )
        )
    )
    if ranked_asset_ids != set(asset_ids):
        raise ReviewConflictError("inspection asset is not part of the review ranking")
    existing_asset_ids = set(
        await session.scalars(
            select(ReviewAssetInspection.asset_id).where(
                ReviewAssetInspection.review_task_id == task.id,
                ReviewAssetInspection.inspected_by_user_id == inspected_by_user_id,
                ReviewAssetInspection.asset_id.in_(asset_ids),
            )
        )
    )
    session.add_all(
        [
            ReviewAssetInspection(
                review_task_id=task.id,
                scoring_run_id=task.scoring_run_id,
                asset_id=asset_id,
                inspected_by_user_id=inspected_by_user_id,
                inspected_at=inspected_at,
            )
            for asset_id in asset_ids
            if asset_id not in existing_asset_ids
        ]
    )
    try:
        await session.flush()
    except IntegrityError as error:
        await session.rollback()
        raise ReviewConflictError("reviewed-image progress could not be frozen") from error


async def _freeze_release_selections(
    session: AsyncSession,
    *,
    task: ReviewTask,
    final_accepted_count: int,
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
    rows = (
        await session.execute(
            _accepted_release_selection_sources_statement(
                review_task_id=task.id,
                scoring_run_id=task.scoring_run_id,
            )
        )
    ).all()
    if len(rows) != final_accepted_count:
        raise ReviewConflictError("accepted source count changed before selection freeze")
    accepted_asset_ids = {decision.asset_id for decision, _, _, _ in rows}
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

    release_jobs = tuple(
        (
            await session.scalars(
                select(GenerationJob).where(
                    GenerationJob.release_version_id == task.release_version_id
                )
            )
        ).all()
    )
    queue_offsets = generation_queue_offsets(release_jobs)
    frozen_queue_positions: set[int] = set()
    selection_ids: list[str] = []
    for display_order, (decision, ranking, asset, generation_job) in enumerate(
        rows,
        start=1,
    ):
        source_output_index = asset.output_index
        queue_offset = queue_offsets.get(generation_job.id)
        if (
            decision.scoring_run_id != task.scoring_run_id
            or ranking.scoring_run_id != task.scoring_run_id
            or asset.release_id != release_version.release_id
            or asset.kind != AssetKind.RAW_MASTER
            or asset.state != AssetState.AVAILABLE
            or asset.generation_job_id != generation_job.id
            or generation_job.release_version_id != task.release_version_id
            or source_output_index is None
            or source_output_index < 0
            or queue_offset is None
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
        source_queue_position = queue_offset + source_output_index + 1
        if source_queue_position in frozen_queue_positions:
            raise ReviewConflictError("accepted sources have duplicate generation queue positions")
        frozen_queue_positions.add(source_queue_position)
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
                source_generation_job_id=generation_job.id,
                source_output_index=source_output_index,
                source_generation_ordinal=generation_ordinal(generation_job),
                source_generation_queue_position=source_queue_position,
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


def _accepted_release_selection_sources_statement(
    *,
    review_task_id: UUID,
    scoring_run_id: UUID,
) -> Select[tuple[ReviewDecision, AssetRanking, Asset, GenerationJob]]:
    latest_revisions = (
        select(
            ReviewDecision.asset_id.label("asset_id"),
            func.max(ReviewDecision.revision).label("revision"),
        )
        .where(ReviewDecision.review_task_id == review_task_id)
        .group_by(ReviewDecision.asset_id)
        .subquery()
    )
    return (
        select(ReviewDecision, AssetRanking, Asset, GenerationJob)
        .join(
            latest_revisions,
            (latest_revisions.c.asset_id == ReviewDecision.asset_id)
            & (latest_revisions.c.revision == ReviewDecision.revision),
        )
        .join(
            AssetRanking,
            (AssetRanking.scoring_run_id == scoring_run_id)
            & (AssetRanking.asset_id == ReviewDecision.asset_id),
        )
        .join(Asset, Asset.id == ReviewDecision.asset_id)
        .join(GenerationJob, GenerationJob.id == Asset.generation_job_id)
        .where(
            ReviewDecision.review_task_id == review_task_id,
            ReviewDecision.decision == ReviewDecisionValue.ACCEPT,
        )
        .order_by(AssetRanking.rank, Asset.id)
        .with_for_update(of=(ReviewDecision, AssetRanking, Asset, GenerationJob))
    )


async def get_review_summary(
    session: AsyncSession,
    *,
    review_task_id: UUID,
    semantic_profile_sha256: str | None = None,
    semantic_severe_confidence_micros: int = 900_000,
    semantic_enforcement_mode: SemanticEnforcementMode = SemanticEnforcementMode.ENFORCE,
) -> ReviewSummary:
    task = await session.get(ReviewTask, review_task_id)
    if task is None:
        raise ReviewNotFoundError("review task was not found")
    return await _review_summary(
        session,
        task,
        semantic_profile_sha256=_normalize_semantic_profile_sha256(semantic_profile_sha256),
        semantic_severe_confidence_micros=_validate_semantic_confidence_threshold(
            semantic_severe_confidence_micros
        ),
        semantic_enforcement_mode=_validate_semantic_enforcement_mode(semantic_enforcement_mode),
    )


async def _review_summary(
    session: AsyncSession,
    task: ReviewTask,
    *,
    semantic_profile_sha256: str | None = None,
    semantic_severe_confidence_micros: int = 900_000,
    semantic_enforcement_mode: SemanticEnforcementMode = SemanticEnforcementMode.ENFORCE,
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
    decision_ids = {decision.id for _, decision in rows if decision is not None}
    semantic_override_profiles: dict[UUID, str] = {}
    if decision_ids:
        override_events = (
            await session.scalars(
                select(AuditEvent).where(
                    AuditEvent.action == SEMANTIC_SEVERE_OVERRIDE_AUDIT_ACTION,
                    AuditEvent.resource_type == "review_decision",
                    AuditEvent.resource_id.in_(decision_ids),
                )
            )
        ).all()
        for event in override_events:
            profile = event.detail.get("semantic_profile_sha256")
            if isinstance(profile, str):
                semantic_override_profiles[event.resource_id] = profile

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
                semantic_severe_override_attested=(
                    decision is not None
                    and semantic_profile_sha256 is not None
                    and semantic_override_profiles.get(decision.id) == semantic_profile_sha256
                ),
            )
        )
    semantic_gate = await _semantic_review_gate(
        session,
        task=task,
        assets=tuple(assets),
        semantic_profile_sha256=semantic_profile_sha256,
        semantic_severe_confidence_micros=semantic_severe_confidence_micros,
        semantic_enforcement_mode=semantic_enforcement_mode,
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
        semantic_gate=semantic_gate,
    )


async def _semantic_review_gate(
    session: AsyncSession,
    *,
    task: ReviewTask,
    assets: tuple[CurrentAssetDecision, ...],
    semantic_profile_sha256: str | None,
    semantic_severe_confidence_micros: int,
    semantic_enforcement_mode: SemanticEnforcementMode,
) -> ReviewSemanticGate:
    if semantic_profile_sha256 is None:
        return ReviewSemanticGate(
            enabled=False,
            mode=semantic_enforcement_mode,
            ranked_asset_count=task.ranked_asset_count,
            terminal_count=0,
            pending_count=0,
            unavailable_count=0,
            severe_count=0,
            severe_override_count=0,
            severe_blocked_count=0,
        )

    assessments = {
        assessment.asset_id: assessment
        for assessment in (
            await session.scalars(
                select(SemanticAssessment).where(
                    SemanticAssessment.scoring_run_id == task.scoring_run_id,
                    SemanticAssessment.profile_sha256 == semantic_profile_sha256,
                )
            )
        ).all()
    }
    terminal_count = unavailable_count = severe_count = 0
    severe_override_count = severe_blocked_count = 0
    for asset in assets:
        assessment = assessments.get(asset.asset_id)
        if assessment is None or assessment.state not in (
            SemanticAssessmentState.COMPLETED,
            SemanticAssessmentState.UNAVAILABLE,
        ):
            continue
        terminal_count += 1
        if assessment.state == SemanticAssessmentState.UNAVAILABLE:
            unavailable_count += 1
            continue
        if not _is_high_confidence_severe_assessment(
            assessment,
            threshold_micros=semantic_severe_confidence_micros,
        ):
            continue
        severe_count += 1
        if asset.decision != ReviewDecisionValue.ACCEPT:
            continue
        if asset.semantic_severe_override_attested:
            severe_override_count += 1
        else:
            severe_blocked_count += 1

    return ReviewSemanticGate(
        enabled=True,
        mode=semantic_enforcement_mode,
        ranked_asset_count=task.ranked_asset_count,
        terminal_count=terminal_count,
        pending_count=task.ranked_asset_count - terminal_count,
        unavailable_count=unavailable_count,
        severe_count=severe_count,
        severe_override_count=severe_override_count,
        severe_blocked_count=severe_blocked_count,
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


def _latest_review_decisions_for_update_statement(
    *,
    review_task_id: UUID,
    asset_ids: Sequence[UUID],
) -> Select[tuple[ReviewDecision]]:
    latest_revisions = (
        select(
            ReviewDecision.asset_id.label("asset_id"),
            func.max(ReviewDecision.revision).label("revision"),
        )
        .where(
            ReviewDecision.review_task_id == review_task_id,
            ReviewDecision.asset_id.in_(asset_ids),
        )
        .group_by(ReviewDecision.asset_id)
        .subquery()
    )
    return (
        select(ReviewDecision)
        .join(
            latest_revisions,
            and_(
                latest_revisions.c.asset_id == ReviewDecision.asset_id,
                latest_revisions.c.revision == ReviewDecision.revision,
            ),
        )
        .where(ReviewDecision.review_task_id == review_task_id)
        .with_for_update(of=ReviewDecision)
    )


async def _claim_review_task_version(
    session: AsyncSession,
    *,
    task: ReviewTask,
    expected_lock_version: int,
) -> None:
    claimed_task_id = await session.scalar(
        update(ReviewTask)
        .where(
            ReviewTask.id == task.id,
            ReviewTask.state == ReviewTaskState.OPEN,
            ReviewTask.lock_version == expected_lock_version,
        )
        .values(lock_version=expected_lock_version + 1)
        .returning(ReviewTask.id)
    )
    if claimed_task_id is None:
        raise ReviewConflictError("review task was changed concurrently")


async def _review_x_selected_count(session: AsyncSession, review_task_id: UUID) -> int:
    return int(
        await session.scalar(
            select(func.count())
            .select_from(ReviewXSelection)
            .where(ReviewXSelection.review_task_id == review_task_id)
        )
        or 0
    )


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
    record = await _find_idempotency_record(
        session,
        scope=scope,
        key=idempotency_key,
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
    if record.request_sha256 != request_sha256:
        if task_lock_version <= 1:
            raise ReviewConflictError("review decision idempotency record is invalid")
        persisted_request_sha256 = _decision_request_sha256(
            review_task_id=decision.review_task_id,
            asset_id=decision.asset_id,
            decision=decision.decision,
            decided_by_user_id=decision.decided_by_user_id,
            expected_lock_version=task_lock_version - 1,
            reason_code=decision.reason_code,
            note=decision.note,
        )
        if persisted_request_sha256 != request_sha256:
            raise ReviewConflictError("idempotency key was already used for another request")
    return _decision_result(
        decision,
        task_lock_version=task_lock_version,
        replayed=True,
    )


async def _bulk_action_idempotency_replay(
    session: AsyncSession,
    *,
    scope: str,
    idempotency_key: str,
    request_sha256: str,
) -> ReviewBulkActionResult | None:
    record = await _load_idempotency_record(
        session,
        scope=scope,
        key=idempotency_key,
        request_sha256=request_sha256,
    )
    if record is None:
        return None
    body = record.response_body
    task_id = _response_uuid(body, "task_id", "bulk review action")
    if await session.get(ReviewTask, task_id) is None:
        raise ReviewConflictError("idempotency record references a missing review task")
    try:
        action = ReviewBulkAction(str(body["action"]))
        raw_asset_ids = body["asset_ids"]
        if not isinstance(raw_asset_ids, list):
            raise ValueError
        asset_ids = _normalize_bulk_asset_ids(tuple(UUID(str(value)) for value in raw_asset_ids))
    except (KeyError, TypeError, ValueError):
        raise ReviewConflictError("bulk review action idempotency record is invalid") from None
    return ReviewBulkActionResult(
        task_id=task_id,
        action=action,
        asset_ids=asset_ids,
        changed_count=_response_nonnegative_int(
            body,
            "changed_count",
            "bulk review action",
        ),
        x_selected_count=_response_nonnegative_int(
            body,
            "x_selected_count",
            "bulk review action",
        ),
        task_lock_version=_response_positive_int(
            body,
            "task_lock_version",
            "bulk review action",
        ),
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
    record = await _find_idempotency_record(session, scope=scope, key=key)
    if record is not None and record.request_sha256 != request_sha256:
        raise ReviewConflictError("idempotency key was already used for another request")
    return record


async def _find_idempotency_record(
    session: AsyncSession,
    *,
    scope: str,
    key: str,
) -> IdempotencyRecord | None:
    record = await session.scalar(
        select(IdempotencyRecord).where(
            IdempotencyRecord.scope == scope,
            IdempotencyRecord.idempotency_key == key,
        )
    )
    return record


def _decision_request_sha256(
    *,
    review_task_id: UUID,
    asset_id: UUID,
    decision: ReviewDecisionValue,
    decided_by_user_id: UUID,
    expected_lock_version: int,
    reason_code: str | None,
    note: str | None,
) -> str:
    """Hash the stable caller command, excluding mutable server policy inputs."""

    return canonical_sha256(
        {
            "review_task_id": str(review_task_id),
            "asset_id": str(asset_id),
            "decision": decision.value,
            "decided_by_user_id": str(decided_by_user_id),
            "expected_lock_version": expected_lock_version,
            "reason_code": reason_code,
            "note": note,
        }
    )


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


def _bulk_action_response_body(result: ReviewBulkActionResult) -> dict[str, Any]:
    return {
        "schema": "review-bulk-action-result/v1",
        "task_id": str(result.task_id),
        "action": result.action.value,
        "asset_ids": [str(asset_id) for asset_id in result.asset_ids],
        "changed_count": result.changed_count,
        "x_selected_count": result.x_selected_count,
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


def _validate_bulk_action(value: ReviewBulkAction) -> ReviewBulkAction:
    try:
        return ReviewBulkAction(value)
    except ValueError:
        raise ReviewInputError("bulk action is invalid") from None


def _validate_semantic_enforcement_mode(
    value: SemanticEnforcementMode,
) -> SemanticEnforcementMode:
    try:
        return SemanticEnforcementMode(value)
    except ValueError:
        raise ReviewInputError("semantic enforcement mode is invalid") from None


def _normalize_bulk_asset_ids(values: Sequence[UUID]) -> tuple[UUID, ...]:
    if isinstance(values, (str, bytes)) or not 1 <= len(values) <= _MAX_BULK_ASSET_COUNT:
        raise ReviewInputError("asset_ids must contain between 1 and 500 assets")
    normalized: list[UUID] = []
    for value in values:
        if not isinstance(value, UUID):
            raise ReviewInputError("asset_ids must contain UUID values")
        normalized.append(value)
    if len(set(normalized)) != len(normalized):
        raise ReviewInputError("asset_ids must not contain duplicates")
    return tuple(sorted(normalized, key=str))


def _normalize_optional_asset_ids(
    values: Sequence[UUID],
    *,
    label: str,
) -> tuple[UUID, ...]:
    if isinstance(values, (str, bytes)) or len(values) > _MAX_BULK_ASSET_COUNT:
        raise ReviewInputError(f"{label} must contain at most 500 assets")
    normalized: list[UUID] = []
    for value in values:
        if not isinstance(value, UUID):
            raise ReviewInputError(f"{label} must contain UUID values")
        normalized.append(value)
    if len(set(normalized)) != len(normalized):
        raise ReviewInputError(f"{label} must not contain duplicates")
    return tuple(sorted(normalized, key=str))


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


def _reason_compatible_with_decision(
    *,
    decision: ReviewDecisionValue,
    reason_code: str | None,
) -> str | None:
    """Discard stale internal reasons that cannot describe the submitted decision."""

    if reason_code is None:
        return None
    if reason_code == SEMANTIC_SEVERE_OVERRIDE_REASON_CODE:
        return reason_code if decision == ReviewDecisionValue.ACCEPT else None
    if reason_code == SORTING_DEFAULT_ACCEPT_REASON_CODE:
        return reason_code if decision == ReviewDecisionValue.ACCEPT else None
    if reason_code in ANATOMY_REASON_CODES:
        return reason_code if decision == ReviewDecisionValue.REJECT else None
    return reason_code


def _normalize_note(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized or len(normalized) > _MAX_NOTE_LENGTH:
        raise ReviewInputError("note must be between 1 and 4000 characters")
    return normalized


def _normalize_semantic_profile_sha256(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _SHA256_PATTERN.fullmatch(value):
        raise ReviewInputError("semantic_profile_sha256 must be a lowercase SHA-256 digest")
    return value


def _validate_semantic_confidence_threshold(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 1_000_000:
        raise ReviewInputError("semantic severe confidence threshold must be between 0 and 1000000")
    return value


def _is_high_confidence_severe_assessment(
    assessment: SemanticAssessment | None,
    *,
    threshold_micros: int,
) -> bool:
    return (
        assessment is not None
        and assessment.state == SemanticAssessmentState.COMPLETED
        and assessment.verdict == SemanticVerdict.SEVERE
        and assessment.confidence_micros is not None
        and assessment.confidence_micros >= threshold_micros
    )


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
