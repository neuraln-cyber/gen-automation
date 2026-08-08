"""Append-only X teaser revisions with an atomic active/pending head."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from gen_automation.db.models import (
    AuditEvent,
    DerivativeJob,
    DerivativeOutput,
    IdempotencyRecord,
    OutboxEvent,
    PublicationIntent,
    Release,
    ReleaseSelection,
    ReleaseVersion,
    ReviewTask,
    ReviewXSelection,
    XTeaserRevision,
    XTeaserRevisionHead,
    XTeaserRevisionMember,
)
from gen_automation.domain.canonical import canonical_sha256
from gen_automation.domain.enums import (
    DerivativeJobState,
    OutboxStatus,
    PublicationIntentState,
    PublicationTarget,
    ReleasePhase,
    ReviewTaskState,
)
from gen_automation.domain.ids import uuid7
from gen_automation.services.derivative_pipeline import (
    DerivativePipelineConflictError,
    DerivativePipelineNotFoundError,
    current_release_gating_job_predicate,
)

_X_TARGET = "x_teaser"
_BLOCKING_PUBLICATION_STATES = tuple(
    state
    for state in PublicationIntentState
    if state not in {PublicationIntentState.FAILED, PublicationIntentState.CANCELLED}
)
_REPLACEMENT_RELEASE_PHASES = frozenset(
    {
        ReleasePhase.RENDERING,
        ReleasePhase.READY_TO_PUBLISH,
        ReleasePhase.PUBLISHING,
        ReleasePhase.PUBLISHED,
    }
)


@dataclass(frozen=True, slots=True)
class XTeaserRevisionStatus:
    active_revision_id: UUID | None
    active_revision_no: int | None
    pending_revision_id: UUID | None
    pending_state: str | None
    pending_total: int
    pending_succeeded: int
    pending_failed: int
    can_replace: bool
    blocked_reason: str | None
    current_watermark_asset_id: UUID | None
    current_positions_by_asset_id: dict[UUID, str]


async def lock_revision_head(
    session: AsyncSession,
    *,
    review_task_id: UUID,
) -> XTeaserRevisionHead | None:
    head: XTeaserRevisionHead | None = await session.scalar(
        select(XTeaserRevisionHead)
        .where(XTeaserRevisionHead.review_task_id == review_task_id)
        .with_for_update()
    )
    return head


async def _lock_release_version(
    session: AsyncSession,
    *,
    release_version_id: UUID,
) -> ReleaseVersion:
    version: ReleaseVersion | None = await session.scalar(
        select(ReleaseVersion).where(ReleaseVersion.id == release_version_id).with_for_update()
    )
    if version is None:
        raise DerivativePipelineConflictError("release version is unavailable")
    return version


async def _lock_current_release(
    session: AsyncSession,
    *,
    version: ReleaseVersion,
) -> Release:
    release: Release | None = await session.scalar(
        select(Release).where(Release.id == version.release_id).with_for_update()
    )
    if release is None or release.current_version_no != version.version_no:
        raise DerivativePipelineConflictError(
            "X teaser revision requires the current release version"
        )
    return release


async def require_no_x_publication_race(
    session: AsyncSession,
    *,
    release_version_id: UUID,
) -> None:
    intent = await session.scalar(
        select(PublicationIntent.id)
        .where(
            PublicationIntent.release_version_id == release_version_id,
            PublicationIntent.target == PublicationTarget.X,
            PublicationIntent.state.in_(_BLOCKING_PUBLICATION_STATES),
        )
        .limit(1)
        .with_for_update()
    )
    if intent is not None:
        raise DerivativePipelineConflictError(
            "watermarked teasers cannot be replaced after X delivery has been prepared"
        )


async def active_x_teaser_outputs(
    session: AsyncSession,
    *,
    review_task_id: UUID,
    lock_head: bool = False,
) -> tuple[DerivativeOutput, ...]:
    query = select(XTeaserRevisionHead).where(XTeaserRevisionHead.review_task_id == review_task_id)
    if lock_head:
        query = query.with_for_update()
    head = await session.scalar(query)
    if head is None or head.active_revision_id is None:
        return ()
    members = tuple(
        (
            await session.scalars(
                select(XTeaserRevisionMember)
                .where(XTeaserRevisionMember.revision_id == head.active_revision_id)
                .order_by(XTeaserRevisionMember.display_order)
            )
        ).all()
    )
    return await _resolved_member_outputs(session, members=members, require_complete=True)


async def x_teaser_revision_status(
    session: AsyncSession,
    *,
    review_task_id: UUID,
) -> XTeaserRevisionStatus:
    head = await session.scalar(
        select(XTeaserRevisionHead).where(XTeaserRevisionHead.review_task_id == review_task_id)
    )
    active_revision: XTeaserRevision | None = None
    current_positions: dict[UUID, str] = {}
    if head is not None and head.active_revision_id is not None:
        active_revision = await session.get(XTeaserRevision, head.active_revision_id)
        rows = (
            await session.execute(
                select(
                    XTeaserRevisionMember.source_asset_id,
                    XTeaserRevisionMember.watermark_position,
                ).where(XTeaserRevisionMember.revision_id == head.active_revision_id)
            )
        ).all()
        current_positions = {asset_id: position for asset_id, position in rows}

    pending_total = pending_succeeded = pending_failed = 0
    pending_state: str | None = None
    if head is not None and head.pending_revision_id is not None:
        members = tuple(
            (
                await session.scalars(
                    select(XTeaserRevisionMember).where(
                        XTeaserRevisionMember.revision_id == head.pending_revision_id
                    )
                )
            ).all()
        )
        pending_total = len(members)
        for member in members:
            if member.derivative_output_id is not None:
                pending_succeeded += 1
                continue
            job = await session.get(DerivativeJob, member.derivative_job_id)
            if job is not None and job.state == DerivativeJobState.SUCCEEDED:
                pending_succeeded += 1
            elif job is not None and job.state in {
                DerivativeJobState.FAILED,
                DerivativeJobState.CANCELLED,
            }:
                pending_failed += 1
        if pending_failed:
            pending_state = "failed"
        elif pending_total and pending_succeeded == pending_total:
            pending_state = "activating"
        elif pending_succeeded:
            pending_state = "running"
        else:
            pending_state = "queued"

    blocking_intent = await session.scalar(
        select(PublicationIntent.id)
        .join(ReviewTask, ReviewTask.release_version_id == PublicationIntent.release_version_id)
        .where(
            ReviewTask.id == review_task_id,
            PublicationIntent.target == PublicationTarget.X,
            PublicationIntent.state.in_(_BLOCKING_PUBLICATION_STATES),
        )
        .limit(1)
    )
    blocked_reason: str | None = None
    if head is not None and head.pending_revision_id is not None:
        blocked_reason = "A replacement teaser render is already in progress."
    elif blocking_intent is not None:
        blocked_reason = "X delivery is already prepared or published for this set."
    return XTeaserRevisionStatus(
        active_revision_id=head.active_revision_id if head is not None else None,
        active_revision_no=active_revision.revision_no if active_revision is not None else None,
        pending_revision_id=head.pending_revision_id if head is not None else None,
        pending_state=pending_state,
        pending_total=pending_total,
        pending_succeeded=pending_succeeded,
        pending_failed=pending_failed,
        can_replace=(
            active_revision is not None
            and (head is None or head.pending_revision_id is None)
            and blocking_intent is None
        ),
        blocked_reason=blocked_reason,
        current_watermark_asset_id=(
            active_revision.watermark_asset_id if active_revision is not None else None
        ),
        current_positions_by_asset_id=current_positions,
    )


async def create_pending_x_teaser_revision(
    session: AsyncSession,
    *,
    review_task_id: UUID,
    watermark_asset_id: UUID,
    positions_by_asset_id: dict[UUID, str],
    actor_user_id: UUID,
    idempotency_key: str,
    created_at: datetime,
    render_profile_sha256: str,
    require_active_revision: bool | None = None,
) -> tuple[
    XTeaserRevision,
    XTeaserRevisionHead,
    tuple[ReleaseSelection, ...],
    bool,
    bool,
]:
    """Freeze a complete desired revision and reserve the target against publication."""

    task = await session.scalar(
        select(ReviewTask).where(ReviewTask.id == review_task_id).with_for_update()
    )
    if task is None:
        raise DerivativePipelineNotFoundError("review task was not found")
    if task.state != ReviewTaskState.COMPLETED:
        raise DerivativePipelineConflictError(
            "X teaser preparation requires a completed review task"
        )
    # Full derivative planning locks ReviewTask before Version/Release. Match
    # that order here; publication never subsequently locks ReviewTask.
    version = await _lock_release_version(
        session,
        release_version_id=task.release_version_id,
    )
    release = await _lock_current_release(session, version=version)
    selections = tuple(
        (
            await session.scalars(
                select(ReleaseSelection)
                .join(
                    ReviewXSelection,
                    (ReviewXSelection.review_task_id == ReleaseSelection.review_task_id)
                    & (ReviewXSelection.asset_id == ReleaseSelection.asset_id),
                )
                .where(ReleaseSelection.review_task_id == review_task_id)
                .order_by(ReleaseSelection.display_order)
            )
        ).all()
    )
    if not selections:
        raise DerivativePipelineConflictError("X teaser preparation requires selected X images")
    if set(positions_by_asset_id) != {selection.asset_id for selection in selections}:
        raise DerivativePipelineConflictError(
            "watermark placements must cover the frozen X selections exactly"
        )
    if len(render_profile_sha256) != 64 or any(
        character not in "0123456789abcdef" for character in render_profile_sha256
    ):
        raise DerivativePipelineConflictError("X teaser render profile is invalid")
    request_sha256 = canonical_sha256(
        {
            "schema": "x-teaser-revision-request/v1",
            "review_task_id": str(review_task_id),
            "release_version_id": str(task.release_version_id),
            "watermark_asset_id": str(watermark_asset_id),
            "render_profile_sha256": render_profile_sha256,
            "placements": {
                str(asset_id): positions_by_asset_id[asset_id]
                for asset_id in sorted(positions_by_asset_id, key=str)
            },
        }
    )
    normalized_idempotency_key = idempotency_key.strip()
    if not normalized_idempotency_key or len(normalized_idempotency_key) > 200:
        raise DerivativePipelineConflictError("X teaser idempotency key is invalid")
    idempotency_scope = f"review-task:{review_task_id}:x-teaser-revision"
    idempotency_record: IdempotencyRecord | None = await session.scalar(
        select(IdempotencyRecord)
        .where(
            IdempotencyRecord.scope == idempotency_scope,
            IdempotencyRecord.idempotency_key == normalized_idempotency_key,
        )
        .with_for_update()
    )
    if idempotency_record is not None:
        if idempotency_record.request_sha256 != request_sha256:
            raise DerivativePipelineConflictError(
                "X teaser idempotency key was already used for a different replacement"
            )
        raw_revision_id = idempotency_record.response_body.get("revision_id")
        try:
            replay_revision_id = UUID(str(raw_revision_id))
        except (TypeError, ValueError):
            raise DerivativePipelineConflictError(
                "X teaser idempotency snapshot is unavailable"
            ) from None
        replay_revision = await session.get(XTeaserRevision, replay_revision_id)
        replay_head = await lock_revision_head(session, review_task_id=review_task_id)
        if replay_revision is None or replay_head is None:
            raise DerivativePipelineConflictError("X teaser idempotency snapshot is unavailable")
        return replay_revision, replay_head, selections, True, False
    head = await lock_revision_head(session, review_task_id=review_task_id)
    has_active_revision = head is not None and head.active_revision_id is not None
    if require_active_revision is True and not has_active_revision:
        raise DerivativePipelineConflictError(
            "X teaser replacement requires an active teaser revision"
        )
    if require_active_revision is False and has_active_revision:
        raise DerivativePipelineConflictError(
            "X teasers are already active; use the replacement action"
        )
    gates_release = not has_active_revision and release.phase in {
        ReleasePhase.APPROVED,
        ReleasePhase.RENDERING,
    }
    current_revision_ids = tuple(
        revision_id
        for revision_id in (
            head.active_revision_id if head is not None else None,
            head.pending_revision_id if head is not None else None,
        )
        if revision_id is not None
    )
    existing = (
        await session.scalar(
            select(XTeaserRevision)
            .where(
                XTeaserRevision.id.in_(current_revision_ids),
                XTeaserRevision.request_sha256 == request_sha256,
            )
            .order_by(XTeaserRevision.revision_no.desc())
            .limit(1)
        )
        if current_revision_ids
        else None
    )
    if existing is not None:
        session.add(
            IdempotencyRecord(
                scope=idempotency_scope,
                idempotency_key=normalized_idempotency_key,
                request_sha256=request_sha256,
                response_status=200,
                response_body={
                    "schema": "x-teaser-revision-idempotency/v1",
                    "revision_id": str(existing.id),
                },
                created_at=created_at,
                expires_at=created_at + timedelta(days=30),
            )
        )
        await session.flush()
        return existing, _require_head(head), selections, True, gates_release
    if head is not None and head.pending_revision_id is not None:
        raise DerivativePipelineConflictError("an X teaser replacement is already in progress")
    if head is not None and head.active_revision_id is not None:
        await require_no_x_publication_race(
            session,
            release_version_id=task.release_version_id,
        )
    revision_no = (
        int(
            await session.scalar(
                select(func.max(XTeaserRevision.revision_no)).where(
                    XTeaserRevision.review_task_id == review_task_id
                )
            )
            or 0
        )
        + 1
    )
    revision = XTeaserRevision(
        id=uuid7(),
        review_task_id=review_task_id,
        release_version_id=task.release_version_id,
        revision_no=revision_no,
        watermark_asset_id=watermark_asset_id,
        request_sha256=request_sha256,
        created_by_user_id=actor_user_id,
        created_at=created_at,
    )
    session.add(revision)
    await session.flush()
    if head is None:
        head = XTeaserRevisionHead(
            id=uuid7(),
            review_task_id=review_task_id,
            release_version_id=task.release_version_id,
            active_revision_id=None,
            pending_revision_id=revision.id,
            lock_version=1,
            updated_at=created_at,
        )
        session.add(head)
    else:
        head.pending_revision_id = revision.id
        head.lock_version += 1
        head.updated_at = created_at
    session.add(
        IdempotencyRecord(
            scope=idempotency_scope,
            idempotency_key=normalized_idempotency_key,
            request_sha256=request_sha256,
            response_status=202,
            response_body={
                "schema": "x-teaser-revision-idempotency/v1",
                "revision_id": str(revision.id),
            },
            created_at=created_at,
            expires_at=created_at + timedelta(days=30),
        )
    )
    await session.flush()
    return revision, head, selections, False, gates_release


async def activate_ready_x_teaser_revision(
    session: AsyncSession,
    *,
    revision_id: UUID,
    activated_at: datetime | None = None,
) -> bool:
    """Atomically switch the head only after every member has an immutable output."""

    revision = await session.get(XTeaserRevision, revision_id)
    if revision is None:
        raise DerivativePipelineNotFoundError("X teaser revision was not found")
    # Publication and revision creation take this same durable mutex first.
    # The later zero-row intent check is therefore race-free.
    version = await _lock_release_version(
        session,
        release_version_id=revision.release_version_id,
    )
    release = await _lock_current_release(session, version=version)
    head = await lock_revision_head(session, review_task_id=revision.review_task_id)
    if head is None:
        raise DerivativePipelineConflictError("X teaser revision head is unavailable")
    if head.active_revision_id == revision.id and head.pending_revision_id is None:
        return True
    if head.pending_revision_id != revision.id:
        raise DerivativePipelineConflictError("X teaser revision is no longer pending")
    if release.phase not in _REPLACEMENT_RELEASE_PHASES:
        raise DerivativePipelineConflictError(
            "X teaser revision release phase is no longer eligible"
        )
    members = tuple(
        (
            await session.scalars(
                select(XTeaserRevisionMember)
                .where(XTeaserRevisionMember.revision_id == revision.id)
                .order_by(XTeaserRevisionMember.display_order)
            )
        ).all()
    )
    selected_count = int(
        await session.scalar(
            select(func.count())
            .select_from(ReviewXSelection)
            .where(ReviewXSelection.review_task_id == revision.review_task_id)
        )
        or 0
    )
    if not members or len(members) != selected_count:
        return False
    outputs = await _resolved_member_outputs(session, members=members, require_complete=False)
    if len(outputs) != len(members):
        await _discard_if_terminal_failure(
            session,
            head=head,
            release=release,
            revision_id=revision.id,
            members=members,
            discarded_at=activated_at,
            actor="x-teaser-revision-activation",
        )
        return False
    await require_no_x_publication_race(
        session,
        release_version_id=revision.release_version_id,
    )
    head.active_revision_id = revision.id
    head.pending_revision_id = None
    head.lock_version += 1
    head.updated_at = activated_at or datetime.now(UTC)
    await session.flush()
    return True


async def discard_failed_pending_x_teaser_revision(
    session: AsyncSession,
    *,
    revision_id: UUID,
    discarded_at: datetime | None = None,
    actor: str = "x-teaser-revision-recovery",
) -> bool:
    """Release a failed replacement while retaining the prior active outputs."""

    revision = await session.get(XTeaserRevision, revision_id)
    if revision is None:
        return False
    version = await _lock_release_version(
        session,
        release_version_id=revision.release_version_id,
    )
    release = await _lock_current_release(session, version=version)
    head = await lock_revision_head(session, review_task_id=revision.review_task_id)
    if head is None or head.pending_revision_id != revision_id:
        return False
    members = tuple(
        (
            await session.scalars(
                select(XTeaserRevisionMember).where(
                    XTeaserRevisionMember.revision_id == revision_id
                )
            )
        ).all()
    )
    return await _discard_if_terminal_failure(
        session,
        head=head,
        release=release,
        revision_id=revision_id,
        members=members,
        discarded_at=discarded_at,
        actor=actor,
    )


async def _discard_if_terminal_failure(
    session: AsyncSession,
    *,
    head: XTeaserRevisionHead,
    release: Release,
    revision_id: UUID,
    members: tuple[XTeaserRevisionMember, ...],
    discarded_at: datetime | None,
    actor: str,
) -> bool:
    if head.pending_revision_id != revision_id or not members:
        return False
    job_ids = tuple(
        member.derivative_job_id for member in members if member.derivative_job_id is not None
    )
    if not job_ids:
        return False
    states = tuple(
        (
            await session.scalars(select(DerivativeJob.state).where(DerivativeJob.id.in_(job_ids)))
        ).all()
    )
    if len(states) != len(job_ids) or any(
        state
        in {
            DerivativeJobState.REQUESTED,
            DerivativeJobState.CLAIMED,
            DerivativeJobState.PROCESSING,
            DerivativeJobState.RETRY_WAIT,
        }
        for state in states
    ):
        return False
    if not any(
        state in {DerivativeJobState.FAILED, DerivativeJobState.CANCELLED} for state in states
    ):
        return False
    was_initial_revision = head.active_revision_id is None
    head.pending_revision_id = None
    head.lock_version += 1
    reconciled_at = discarded_at or datetime.now(UTC)
    head.updated_at = reconciled_at
    await session.flush()
    if (
        was_initial_revision
        and release.phase == ReleasePhase.RENDERING
        and await _remaining_current_gating_jobs(
            session,
            release_version_id=head.release_version_id,
        )
        == 0
    ):
        release.phase = ReleasePhase.READY_TO_PUBLISH
        release.lock_version += 1
        readiness_dedupe_key = f"release.ready_to_publish:{head.release_version_id}"
        existing_readiness_event = await session.scalar(
            select(OutboxEvent.id).where(
                OutboxEvent.topic == "release.ready_to_publish",
                OutboxEvent.dedupe_key == readiness_dedupe_key,
            )
        )
        if existing_readiness_event is None:
            session.add(
                AuditEvent(
                    actor=actor[:200],
                    action="release.ready_to_publish",
                    resource_type="release",
                    resource_id=release.id,
                    correlation_id=str(revision_id),
                    detail={
                        "release_version_id": str(head.release_version_id),
                        "phase": ReleasePhase.READY_TO_PUBLISH.value,
                        "reason": "initial_x_teaser_revision_terminal",
                    },
                    occurred_at=reconciled_at,
                )
            )
            session.add(
                OutboxEvent(
                    topic="release.ready_to_publish",
                    dedupe_key=readiness_dedupe_key,
                    correlation_id=str(revision_id),
                    aggregate_type="release",
                    aggregate_id=release.id,
                    payload={
                        "release_id": str(release.id),
                        "release_version_id": str(head.release_version_id),
                    },
                    status=OutboxStatus.PENDING,
                    attempts=0,
                    max_attempts=10,
                    available_at=reconciled_at,
                    created_at=reconciled_at,
                )
            )
        await session.flush()
    return True


async def _remaining_current_gating_jobs(
    session: AsyncSession,
    *,
    release_version_id: UUID,
) -> int:
    return int(
        await session.scalar(
            select(func.count())
            .select_from(DerivativeJob)
            .where(
                DerivativeJob.release_version_id == release_version_id,
                DerivativeJob.gates_release.is_(True),
                DerivativeJob.state != DerivativeJobState.SUCCEEDED,
                current_release_gating_job_predicate(),
            )
        )
        or 0
    )


async def _resolved_member_outputs(
    session: AsyncSession,
    *,
    members: tuple[XTeaserRevisionMember, ...],
    require_complete: bool,
) -> tuple[DerivativeOutput, ...]:
    resolved: list[DerivativeOutput] = []
    for member in members:
        output: DerivativeOutput | None
        if member.derivative_output_id is not None:
            output = await session.get(DerivativeOutput, member.derivative_output_id)
        else:
            output = await session.scalar(
                select(DerivativeOutput)
                .join(DerivativeJob, DerivativeJob.id == DerivativeOutput.derivative_job_id)
                .where(
                    DerivativeOutput.derivative_job_id == member.derivative_job_id,
                    DerivativeOutput.target == _X_TARGET,
                    DerivativeJob.state == DerivativeJobState.SUCCEEDED,
                )
            )
        if output is None:
            if require_complete:
                raise DerivativePipelineConflictError("the active X teaser revision is incomplete")
            continue
        if (
            output.release_selection_id != member.release_selection_id
            or output.derivative_recipe_id != member.derivative_recipe_id
            or output.source_asset_id != member.source_asset_id
            or output.target != _X_TARGET
        ):
            raise DerivativePipelineConflictError("X teaser revision member is invalid")
        resolved.append(output)
    return tuple(resolved)


def _require_head(head: XTeaserRevisionHead | None) -> XTeaserRevisionHead:
    if head is None:
        raise DerivativePipelineConflictError("X teaser revision head is unavailable")
    return head
