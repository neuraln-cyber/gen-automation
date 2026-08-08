"""Provider-independent, restart-safe archives for completed ranked sets.

The finished-set archive is intentionally separate from Patreon, MEGA, and X
preparation. It freezes the clean ``full`` derivative outputs in generation-queue
order and makes those bytes downloadable as soon as the derivative handoff is
complete.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from io import BytesIO
from itertools import pairwise
from typing import Any
from uuid import UUID
from zipfile import ZIP_STORED, ZipFile, ZipInfo

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gen_automation.db.models import (
    AdminUser,
    Asset,
    AuditEvent,
    DerivativeJob,
    DerivativeOutput,
    FinishedSetArchive,
    FinishedSetArchivePart,
    GenerationJob,
    Release,
    ReleaseSelection,
    ReleaseVersion,
    ReviewTask,
)
from gen_automation.domain.canonical import canonical_json_bytes
from gen_automation.domain.deliverability import (
    MAX_ACCEPTED_IMAGES_PER_RELEASE,
)
from gen_automation.domain.enums import (
    AdminRole,
    DerivativeJobState,
    FinishedSetArchiveState,
    ReleasePhase,
    ReviewTaskState,
)
from gen_automation.domain.ids import uuid7
from gen_automation.services.generation_positions import (
    generation_ordinal,
    generation_queue_offsets,
)
from gen_automation.services.outbound_image_privacy import (
    OutboundImagePrivacyError,
    require_metadata_free_image,
)
from gen_automation.storage.base import (
    ObjectAlreadyExistsError,
    ObjectMetadata,
    ObjectStore,
    ObjectStoreError,
)

_ARCHIVE_CONTENT_TYPE = "application/zip"
_ARCHIVE_SCHEMA = "finished-set-manifest/v1"
_PART_SCHEMA = "finished-set-part-manifest/v1"
_MAX_ARCHIVE_PARTS = MAX_ACCEPTED_IMAGES_PER_RELEASE
_MAX_IMAGES_PER_PART = 100
_MAX_SAFE_ERROR_BYTES = 500
_MIN_ARCHIVE_BYTES = 1024
_MAX_ARCHIVE_BYTES = 160 * 1024 * 1024
_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
_IMAGE_EXTENSIONS = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
}
_DOWNLOADABLE_RELEASE_PHASES = (
    ReleasePhase.RENDERING,
    ReleasePhase.READY_TO_PUBLISH,
    ReleasePhase.PUBLISHING,
    ReleasePhase.PUBLISHED,
)


class FinishedSetArchiveError(Exception):
    """Base error for provider-independent finished-set archives."""


class FinishedSetArchiveInputError(FinishedSetArchiveError):
    """Caller input is invalid."""


class FinishedSetArchiveNotFoundError(FinishedSetArchiveError):
    """The requested review or archive does not exist."""


class FinishedSetArchiveConflictError(FinishedSetArchiveError):
    """Frozen review, derivative, or archive state is inconsistent."""


class _FinishedSetArchiveContractError(FinishedSetArchiveError):
    """Durable source data cannot safely produce the requested archive."""


@dataclass(frozen=True, slots=True)
class FinishedSetArchivePartSnapshot:
    part_id: UUID
    part_number: int
    part_count: int
    first_ordinal: int
    last_ordinal: int
    sha256: str
    manifest_sha256: str
    byte_size: int


@dataclass(frozen=True, slots=True)
class FinishedSetArchiveSnapshot:
    archive_id: UUID
    review_task_id: UUID
    release_version_id: UUID
    state: FinishedSetArchiveState
    selection_count: int
    manifest_sha256: str | None
    part_count: int | None
    attempts: int
    max_attempts: int
    available_at: datetime
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    last_error_code: str | None
    last_error_detail: str | None
    parts: tuple[FinishedSetArchivePartSnapshot, ...]


@dataclass(frozen=True, slots=True)
class FinishedSetArchiveDownloadResult:
    review_task_id: UUID
    archive_id: UUID
    part_id: UUID
    url: str
    filename: str
    sha256: str
    manifest_sha256: str
    byte_size: int
    expires_at: datetime
    part_number: int
    part_count: int
    first_ordinal: int
    last_ordinal: int


@dataclass(frozen=True, slots=True)
class FinishedSetArchiveCycleResult:
    created_archive: bool = False
    processed_archive: bool = False
    completed_archive: bool = False
    archive_id: UUID | None = None
    state: FinishedSetArchiveState | None = None
    error_code: str | None = None

    @property
    def did_work(self) -> bool:
        return self.created_archive or self.processed_archive or self.completed_archive


@dataclass(frozen=True, slots=True)
class _OutputRecord:
    ordinal: int
    generation_ordinal: int
    generation_job_id: UUID
    generation_queue_position: int
    source_output_index: int
    review_display_order: int
    ranking_rank: int
    selection_id: UUID
    output_id: UUID
    object_key: str
    object_version_id: str
    sha256: str
    content_type: str
    image_format: str
    width: int
    height: int
    byte_size: int
    path: str


@dataclass(frozen=True, slots=True)
class _ArchivePlan:
    archive_id: UUID
    review_task_id: UUID
    release_version_id: UUID
    selection_count: int
    outputs: tuple[_OutputRecord, ...]
    manifest: bytes
    manifest_sha256: str


@dataclass(frozen=True, slots=True)
class _ClaimedArchive:
    archive_id: UUID
    worker_id: str
    attempt: int


@dataclass(frozen=True, slots=True)
class _BuiltPart:
    part_number: int
    part_count: int
    first_ordinal: int
    last_ordinal: int
    body: bytes
    sha256: str


@dataclass(frozen=True, slots=True)
class _StoredPart:
    part_number: int
    part_count: int
    first_ordinal: int
    last_ordinal: int
    sha256: str
    byte_size: int
    storage_backend: str
    storage_bucket: str
    object_key: str
    metadata: ObjectMetadata


async def request_finished_set_archive(
    session: AsyncSession,
    *,
    review_task_id: UUID,
    requested_by_user_id: UUID | None = None,
    now: datetime | None = None,
) -> FinishedSetArchiveSnapshot:
    """Idempotently request an archive for one completed, current review."""

    requested_at = _as_utc(now or datetime.now(UTC))
    review, _release, version = await _load_completed_ready_review(
        session,
        review_task_id=review_task_id,
        lock=True,
    )
    if requested_by_user_id is not None:
        user = await session.get(AdminUser, requested_by_user_id)
        if user is None or not user.is_active:
            raise FinishedSetArchiveConflictError("an active requesting user is required")
    selection_count = await _selection_count(session, review_task_id=review.id)
    _require_selection_count(selection_count)
    archive = await session.scalar(
        select(FinishedSetArchive)
        .where(FinishedSetArchive.review_task_id == review.id)
        .with_for_update()
    )
    if archive is None:
        archive = FinishedSetArchive(
            id=uuid7(),
            review_task_id=review.id,
            release_version_id=version.id,
            requested_by_user_id=requested_by_user_id,
            state=FinishedSetArchiveState.PENDING,
            selection_count=selection_count,
            manifest_sha256=None,
            part_count=None,
            attempts=0,
            max_attempts=5,
            available_at=requested_at,
            lease_owner=None,
            lease_expires_at=None,
            last_error_code=None,
            last_error_detail=None,
            created_at=requested_at,
            updated_at=requested_at,
            started_at=None,
            completed_at=None,
        )
        session.add(archive)
    else:
        _validate_archive_identity(
            archive,
            release_version_id=version.id,
            selection_count=selection_count,
        )
        if archive.state == FinishedSetArchiveState.FAILED:
            archive.state = FinishedSetArchiveState.PENDING
            archive.attempts = 0
            archive.available_at = requested_at
            archive.lease_owner = None
            archive.lease_expires_at = None
            archive.last_error_code = None
            archive.last_error_detail = None
            archive.started_at = None
            archive.completed_at = None
            archive.updated_at = requested_at
            if requested_by_user_id is not None:
                archive.requested_by_user_id = requested_by_user_id
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        archive = await session.scalar(
            select(FinishedSetArchive).where(FinishedSetArchive.review_task_id == review_task_id)
        )
        if archive is None:
            raise
    snapshot = await _snapshot(session, archive)
    return snapshot


async def load_finished_set_archive(
    session: AsyncSession,
    *,
    review_task_id: UUID,
) -> FinishedSetArchiveSnapshot | None:
    """Return the archive snapshot, or ``None`` before any archive is requested."""

    archive = await session.scalar(
        select(FinishedSetArchive).where(FinishedSetArchive.review_task_id == review_task_id)
    )
    if archive is None:
        return None
    return await _snapshot(session, archive)


async def presign_finished_set_archive_part(
    session: AsyncSession,
    store: ObjectStore,
    *,
    review_task_id: UUID,
    archive_id: UUID,
    actor_user_id: UUID,
    actor_role: AdminRole | str,
    part_number: int = 1,
    expires_in_seconds: int = 300,
    now: datetime | None = None,
) -> FinishedSetArchiveDownloadResult:
    """Authorize and presign one immutable archive part for the active owner."""

    requested_at = _as_utc(now or datetime.now(UTC))
    normalized_part = _bounded_int(part_number, "archive part number", 1, _MAX_ARCHIVE_PARTS)
    normalized_expiry = _bounded_int(
        expires_in_seconds,
        "download expiry seconds",
        30,
        900,
    )
    actor = await _require_owner(
        session,
        actor_user_id=actor_user_id,
        actor_role=actor_role,
    )
    review, _release, version = await _load_completed_ready_review(
        session,
        review_task_id=review_task_id,
        lock=True,
    )
    archive = await session.scalar(
        select(FinishedSetArchive).where(FinishedSetArchive.id == archive_id).with_for_update()
    )
    if archive is None:
        raise FinishedSetArchiveNotFoundError("finished-set archive was not found")
    if archive.review_task_id != review.id:
        raise FinishedSetArchiveConflictError(
            "finished-set archive does not belong to the completed review"
        )
    selection_count = await _selection_count(session, review_task_id=review.id)
    _validate_archive_identity(
        archive,
        release_version_id=version.id,
        selection_count=selection_count,
    )
    parts = await _load_valid_parts(session, archive)
    if archive.state != FinishedSetArchiveState.READY:
        raise FinishedSetArchiveConflictError("finished-set archive is not ready")
    if normalized_part > len(parts):
        raise FinishedSetArchiveInputError("archive part number is out of range")
    part = parts[normalized_part - 1]
    if store.backend != part.storage_backend or store.bucket != part.storage_bucket:
        raise FinishedSetArchiveConflictError("finished-set archive storage is unavailable")
    filename = _download_filename(review.id, part.part_number, part.part_count)
    expires_at = requested_at + timedelta(seconds=normalized_expiry)
    session.add(
        AuditEvent(
            id=uuid7(),
            actor=f"admin:{actor.id}",
            action="review.finished_set_archive_download_authorized",
            resource_type="finished_set_archive",
            resource_id=archive.id,
            correlation_id=f"finished-set-archive:{review.id}:{part.id}",
            detail={
                "review_task_id": str(review.id),
                "archive_id": str(archive.id),
                "part_id": str(part.id),
                "part_number": part.part_number,
                "part_count": part.part_count,
                "sha256": part.sha256,
                "manifest_sha256": part.manifest_sha256,
                "expires_at": _canonical_datetime(expires_at),
                "authorization_basis": "completed_review_owner",
            },
            occurred_at=requested_at,
        )
    )
    await session.commit()

    metadata = await store.head(part.object_key)
    expected_metadata = _part_object_metadata(
        archive_id=archive.id,
        review_task_id=review.id,
        release_version_id=version.id,
        part_number=part.part_number,
        part_count=part.part_count,
        first_ordinal=part.first_ordinal,
        last_ordinal=part.last_ordinal,
        sha256=part.sha256,
        manifest_sha256=part.manifest_sha256,
    )
    if not _stored_metadata_matches(
        metadata,
        key=part.object_key,
        version_id=part.object_version_id,
        byte_size=part.byte_size,
        expected_metadata=expected_metadata,
    ):
        raise FinishedSetArchiveConflictError("finished-set archive storage snapshot is invalid")
    url = await store.presign_download(
        key=part.object_key,
        version_id=part.object_version_id,
        expires_in=normalized_expiry,
        download_name=filename,
    )
    return FinishedSetArchiveDownloadResult(
        review_task_id=review.id,
        archive_id=archive.id,
        part_id=part.id,
        url=url,
        filename=filename,
        sha256=part.sha256,
        manifest_sha256=part.manifest_sha256,
        byte_size=part.byte_size,
        expires_at=expires_at,
        part_number=part.part_number,
        part_count=part.part_count,
        first_ordinal=part.first_ordinal,
        last_ordinal=part.last_ordinal,
    )


async def run_finished_set_archive_cycle(
    sessions: async_sessionmaker[AsyncSession],
    store: ObjectStore,
    *,
    worker_id: str,
    lease_seconds: int,
    retry_base_seconds: int,
    retry_max_seconds: int,
    max_archive_bytes: int,
    now: datetime | None = None,
) -> FinishedSetArchiveCycleResult:
    """Discover, claim, build, and atomically register at most one archive."""

    normalized_worker = _worker_id(worker_id)
    normalized_lease = _bounded_int(lease_seconds, "archive lease seconds", 30, 7200)
    normalized_retry_base = _bounded_int(
        retry_base_seconds,
        "archive retry base seconds",
        1,
        7 * 86400,
    )
    normalized_retry_max = _bounded_int(
        retry_max_seconds,
        "archive retry maximum seconds",
        normalized_retry_base,
        7 * 86400,
    )
    normalized_max_bytes = _bounded_int(
        max_archive_bytes,
        "archive maximum bytes",
        _MIN_ARCHIVE_BYTES,
        _MAX_ARCHIVE_BYTES,
    )
    cycle_at = _as_utc(now or datetime.now(UTC))
    async with sessions() as session:
        created = await _ensure_next_archive(session, now=cycle_at)
    async with sessions() as session:
        exhausted_id = await _fail_one_exhausted_lease(session, now=cycle_at)
    if exhausted_id is not None:
        return FinishedSetArchiveCycleResult(
            created_archive=created,
            processed_archive=True,
            archive_id=exhausted_id,
            state=FinishedSetArchiveState.FAILED,
            error_code="archive_attempts_exhausted",
        )
    async with sessions() as session:
        claim = await _claim_archive(
            session,
            worker_id=normalized_worker,
            lease_seconds=normalized_lease,
            now=cycle_at,
        )
    if claim is None:
        return FinishedSetArchiveCycleResult(created_archive=created)

    try:
        async with sessions() as session:
            plan = await _load_plan(session, archive_id=claim.archive_id, store=store)
        chunks = _partition_outputs(plan, max_archive_bytes=normalized_max_bytes)
        async with sessions() as session:
            checkpointed = await _prepare_checkpoint_resume(
                session,
                claim=claim,
                plan=plan,
                chunks=chunks,
                lease_seconds=normalized_lease,
                now=_runtime_now(now),
            )
        await _verify_checkpointed_parts(store, plan=plan, parts=checkpointed)
        for part_number, chunk in enumerate(
            chunks[len(checkpointed) :],
            start=len(checkpointed) + 1,
        ):
            built = await _build_part(
                store,
                plan=plan,
                outputs=chunk,
                part_number=part_number,
                part_count=len(chunks),
                max_archive_bytes=normalized_max_bytes,
            )
            stored = await _write_or_adopt_part(
                store,
                plan=plan,
                part=built,
                max_archive_bytes=normalized_max_bytes,
            )
            async with sessions() as session:
                await _checkpoint_part(
                    session,
                    claim=claim,
                    plan=plan,
                    chunks=chunks,
                    stored=stored,
                    lease_seconds=normalized_lease,
                    now=_runtime_now(now),
                )
        async with sessions() as session:
            await _finalize_archive(
                session,
                claim=claim,
                plan=plan,
                chunks=chunks,
                now=_runtime_now(now),
            )
    except asyncio.CancelledError:
        await _shield_cancelled_claim_retry(
            sessions,
            claim=claim,
            retry_base_seconds=normalized_retry_base,
            retry_max_seconds=normalized_retry_max,
            now=_runtime_now(now),
        )
        raise
    except (
        _FinishedSetArchiveContractError,
        FinishedSetArchiveConflictError,
        FinishedSetArchiveInputError,
        FinishedSetArchiveNotFoundError,
    ) as error:
        code, detail = _safe_error("archive_contract_invalid", str(error))
        state = await _fail_claim(
            sessions,
            claim=claim,
            error_code=code,
            error_detail=detail,
            retry=False,
            retry_base_seconds=normalized_retry_base,
            retry_max_seconds=normalized_retry_max,
            now=_runtime_now(now),
        )
        return FinishedSetArchiveCycleResult(
            created_archive=created,
            processed_archive=True,
            archive_id=claim.archive_id,
            state=state,
            error_code=code,
        )
    except ObjectStoreError:
        code, detail = _safe_error(
            "archive_storage_retryable",
            "Finished-set archive storage was temporarily unavailable.",
        )
        state = await _fail_claim(
            sessions,
            claim=claim,
            error_code=code,
            error_detail=detail,
            retry=True,
            retry_base_seconds=normalized_retry_base,
            retry_max_seconds=normalized_retry_max,
            now=_runtime_now(now),
        )
        return FinishedSetArchiveCycleResult(
            created_archive=created,
            processed_archive=True,
            archive_id=claim.archive_id,
            state=state,
            error_code=code,
        )
    except Exception:
        code, detail = _safe_error(
            "archive_runtime_retryable",
            "Finished-set archive creation encountered a bounded internal error.",
        )
        state = await _fail_claim(
            sessions,
            claim=claim,
            error_code=code,
            error_detail=detail,
            retry=True,
            retry_base_seconds=normalized_retry_base,
            retry_max_seconds=normalized_retry_max,
            now=_runtime_now(now),
        )
        return FinishedSetArchiveCycleResult(
            created_archive=created,
            processed_archive=True,
            archive_id=claim.archive_id,
            state=state,
            error_code=code,
        )
    return FinishedSetArchiveCycleResult(
        created_archive=created,
        processed_archive=True,
        completed_archive=True,
        archive_id=claim.archive_id,
        state=FinishedSetArchiveState.READY,
    )


async def _ensure_next_archive(session: AsyncSession, *, now: datetime) -> bool:
    archive_exists = exists(
        select(FinishedSetArchive.id).where(FinishedSetArchive.review_task_id == ReviewTask.id)
    )
    selection_count = (
        select(func.count(ReleaseSelection.id))
        .where(ReleaseSelection.review_task_id == ReviewTask.id)
        .correlate(ReviewTask)
        .scalar_subquery()
    )
    full_output_count = _succeeded_full_output_count(
        review_task_id=ReviewTask.id,
        release_version_id=ReleaseVersion.id,
        distinct_selections=False,
    )
    full_selection_count = _succeeded_full_output_count(
        review_task_id=ReviewTask.id,
        release_version_id=ReleaseVersion.id,
        distinct_selections=True,
    )
    candidate = (
        await session.execute(
            select(ReviewTask, ReleaseVersion, selection_count.label("selection_count"))
            .join(ReleaseVersion, ReleaseVersion.id == ReviewTask.release_version_id)
            .join(Release, Release.id == ReleaseVersion.release_id)
            .where(
                ReviewTask.state == ReviewTaskState.COMPLETED,
                Release.phase.in_(_DOWNLOADABLE_RELEASE_PHASES),
                Release.current_version_no == ReleaseVersion.version_no,
                ~archive_exists,
                selection_count >= 1,
                selection_count <= MAX_ACCEPTED_IMAGES_PER_RELEASE,
                full_output_count == selection_count,
                full_selection_count == selection_count,
            )
            .order_by(ReviewTask.completed_at, ReviewTask.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
    ).one_or_none()
    if candidate is None:
        await session.rollback()
        return False
    review, version, count = candidate
    session.add(
        FinishedSetArchive(
            id=uuid7(),
            review_task_id=review.id,
            release_version_id=version.id,
            requested_by_user_id=None,
            state=FinishedSetArchiveState.PENDING,
            selection_count=int(count),
            manifest_sha256=None,
            part_count=None,
            attempts=0,
            max_attempts=5,
            available_at=now,
            lease_owner=None,
            lease_expires_at=None,
            last_error_code=None,
            last_error_detail=None,
            created_at=now,
            updated_at=now,
            started_at=None,
            completed_at=None,
        )
    )
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        return False
    return True


async def _fail_one_exhausted_lease(
    session: AsyncSession,
    *,
    now: datetime,
) -> UUID | None:
    archive = await session.scalar(
        select(FinishedSetArchive)
        .where(
            FinishedSetArchive.state == FinishedSetArchiveState.PROCESSING,
            FinishedSetArchive.lease_expires_at.is_not(None),
            FinishedSetArchive.lease_expires_at <= now,
            FinishedSetArchive.attempts >= FinishedSetArchive.max_attempts,
        )
        .order_by(FinishedSetArchive.lease_expires_at, FinishedSetArchive.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if archive is None:
        await session.rollback()
        return None
    archive.state = FinishedSetArchiveState.FAILED
    archive.lease_owner = None
    archive.lease_expires_at = None
    archive.last_error_code = "archive_attempts_exhausted"
    archive.last_error_detail = "Finished-set archive creation exhausted its retry limit."
    archive.completed_at = now
    archive.updated_at = now
    archive_id = UUID(str(archive.id))
    await session.commit()
    return archive_id


async def _claim_archive(
    session: AsyncSession,
    *,
    worker_id: str,
    lease_seconds: int,
    now: datetime,
) -> _ClaimedArchive | None:
    due = or_(
        and_(
            FinishedSetArchive.state.in_(
                (FinishedSetArchiveState.PENDING, FinishedSetArchiveState.RETRY_WAIT)
            ),
            FinishedSetArchive.available_at <= now,
        ),
        and_(
            FinishedSetArchive.state == FinishedSetArchiveState.PROCESSING,
            FinishedSetArchive.lease_expires_at.is_not(None),
            FinishedSetArchive.lease_expires_at <= now,
        ),
    )
    full_output_count = _succeeded_full_output_count(
        review_task_id=ReviewTask.id,
        release_version_id=ReleaseVersion.id,
        distinct_selections=False,
    )
    full_selection_count = _succeeded_full_output_count(
        review_task_id=ReviewTask.id,
        release_version_id=ReleaseVersion.id,
        distinct_selections=True,
    )
    archive = await session.scalar(
        select(FinishedSetArchive)
        .join(ReviewTask, ReviewTask.id == FinishedSetArchive.review_task_id)
        .join(ReleaseVersion, ReleaseVersion.id == ReviewTask.release_version_id)
        .join(Release, Release.id == ReleaseVersion.release_id)
        .where(
            due,
            FinishedSetArchive.attempts < FinishedSetArchive.max_attempts,
            ReviewTask.state == ReviewTaskState.COMPLETED,
            Release.current_version_no == ReleaseVersion.version_no,
            Release.phase.in_(_DOWNLOADABLE_RELEASE_PHASES),
            full_output_count == FinishedSetArchive.selection_count,
            full_selection_count == FinishedSetArchive.selection_count,
        )
        .order_by(FinishedSetArchive.available_at, FinishedSetArchive.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if archive is None:
        await session.rollback()
        return None
    archive.state = FinishedSetArchiveState.PROCESSING
    archive.attempts += 1
    archive.lease_owner = worker_id
    archive.lease_expires_at = now + timedelta(seconds=lease_seconds)
    archive.last_error_code = None
    archive.last_error_detail = None
    archive.completed_at = None
    archive.started_at = archive.started_at or now
    archive.updated_at = now
    claim = _ClaimedArchive(
        archive_id=archive.id,
        worker_id=worker_id,
        attempt=archive.attempts,
    )
    await session.commit()
    return claim


def _succeeded_full_output_count(
    *,
    review_task_id: Any,
    release_version_id: Any,
    distinct_selections: bool,
) -> Any:
    counted = (
        func.count(func.distinct(ReleaseSelection.id))
        if distinct_selections
        else func.count(DerivativeOutput.id)
    )
    return (
        select(counted)
        .select_from(ReleaseSelection)
        .join(
            DerivativeOutput,
            DerivativeOutput.release_selection_id == ReleaseSelection.id,
        )
        .join(DerivativeJob, DerivativeJob.id == DerivativeOutput.derivative_job_id)
        .where(
            ReleaseSelection.review_task_id == review_task_id,
            DerivativeOutput.target == "full",
            DerivativeJob.state == DerivativeJobState.SUCCEEDED,
            DerivativeJob.release_version_id == release_version_id,
        )
        .correlate(ReviewTask, ReleaseVersion, FinishedSetArchive)
        .scalar_subquery()
    )


async def _load_plan(
    session: AsyncSession,
    *,
    archive_id: UUID,
    store: ObjectStore,
) -> _ArchivePlan:
    archive = await session.get(FinishedSetArchive, archive_id)
    if archive is None:
        raise _FinishedSetArchiveContractError("claimed archive no longer exists")
    if archive.state != FinishedSetArchiveState.PROCESSING:
        raise _FinishedSetArchiveContractError("claimed archive is no longer processing")
    review, _release, version = await _load_completed_ready_review(
        session,
        review_task_id=archive.review_task_id,
        lock=False,
    )
    rows = (
        await session.execute(
            select(
                ReleaseSelection,
                DerivativeOutput,
                DerivativeJob,
                Asset,
            )
            .join(
                DerivativeOutput,
                DerivativeOutput.release_selection_id == ReleaseSelection.id,
            )
            .join(DerivativeJob, DerivativeJob.id == DerivativeOutput.derivative_job_id)
            .join(Asset, Asset.id == ReleaseSelection.asset_id)
            .where(
                ReleaseSelection.review_task_id == review.id,
                DerivativeOutput.target == "full",
                DerivativeJob.state == DerivativeJobState.SUCCEEDED,
                DerivativeJob.release_version_id == version.id,
            )
        )
    ).all()
    if len(rows) != archive.selection_count:
        raise _FinishedSetArchiveContractError(
            "completed review does not have exactly one succeeded full output per selection"
        )
    display_orders = tuple(
        sorted(selection.display_order for selection, _output, _job, _asset in rows)
    )
    if display_orders != tuple(range(1, archive.selection_count + 1)):
        raise _FinishedSetArchiveContractError(
            "completed review selections are not contiguous in display order"
        )
    if len({selection.id for selection, _output, _job, _asset in rows}) != len(rows):
        raise _FinishedSetArchiveContractError(
            "completed review has duplicate full derivative outputs"
        )
    release_jobs = tuple(
        (
            await session.scalars(
                select(GenerationJob).where(GenerationJob.release_version_id == version.id)
            )
        ).all()
    )
    queue_offsets = generation_queue_offsets(release_jobs)
    jobs_by_id = {job.id: job for job in release_jobs}
    ordered_rows: list[
        tuple[
            int,
            int,
            UUID,
            int,
            ReleaseSelection,
            DerivativeOutput,
            DerivativeJob,
            Asset,
        ]
    ] = []
    for selection, output, job, source_asset in rows:
        frozen_position = (
            selection.source_generation_job_id,
            selection.source_output_index,
            selection.source_generation_ordinal,
            selection.source_generation_queue_position,
        )
        if all(value is not None for value in frozen_position):
            generation_job_id = selection.source_generation_job_id
            source_output_index = selection.source_output_index
            source_generation_ordinal = selection.source_generation_ordinal
            queue_position = selection.source_generation_queue_position
            assert generation_job_id is not None
            assert source_output_index is not None
            assert source_generation_ordinal is not None
            assert queue_position is not None
            if source_output_index < 0 or source_generation_ordinal < 0 or queue_position <= 0:
                raise _FinishedSetArchiveContractError(
                    "a finished-set source has an invalid frozen generation position"
                )
        elif all(value is None for value in frozen_position):
            generation_job_id = source_asset.generation_job_id
            source_output_index = source_asset.output_index
            generation_job = jobs_by_id.get(generation_job_id)
            queue_offset = queue_offsets.get(generation_job_id)
            if (
                generation_job is None
                or queue_offset is None
                or source_output_index is None
                or source_output_index < 0
                or source_output_index >= generation_job.expected_output_count
            ):
                raise _FinishedSetArchiveContractError(
                    "a legacy finished-set source is outside the generation queue"
                )
            source_generation_ordinal = generation_ordinal(generation_job)
            queue_position = queue_offset + source_output_index + 1
        else:
            raise _FinishedSetArchiveContractError(
                "a finished-set source has an incomplete frozen generation position"
            )
        ordered_rows.append(
            (
                queue_position,
                source_generation_ordinal,
                generation_job_id,
                source_output_index,
                selection,
                output,
                job,
                source_asset,
            )
        )
    ordered_rows.sort(key=lambda row: (row[0], row[4].id))
    if len({row[0] for row in ordered_rows}) != len(ordered_rows):
        raise _FinishedSetArchiveContractError(
            "finished-set sources have duplicate generation queue positions"
        )

    outputs: list[_OutputRecord] = []
    for archive_ordinal, row in enumerate(ordered_rows, start=1):
        (
            queue_position,
            source_generation_ordinal,
            generation_job_id,
            source_output_index,
            selection,
            output,
            job,
            source_asset,
        ) = row
        if (
            selection.release_version_id != version.id
            or job.release_selection_id != selection.id
            or output.release_selection_id != selection.id
            or source_asset.id != selection.asset_id
            or output.asset_storage_backend != store.backend
            or output.asset_storage_bucket != store.bucket
        ):
            raise _FinishedSetArchiveContractError(
                "finished-set derivative storage snapshot is inconsistent"
            )
        extension = _IMAGE_EXTENSIONS.get(output.asset_content_type.lower())
        if extension is None:
            raise _FinishedSetArchiveContractError(
                "finished-set outputs must be JPEG, PNG, or WebP images"
            )
        outputs.append(
            _OutputRecord(
                ordinal=archive_ordinal,
                generation_ordinal=source_generation_ordinal,
                generation_job_id=generation_job_id,
                generation_queue_position=queue_position,
                source_output_index=source_output_index,
                review_display_order=selection.display_order,
                ranking_rank=selection.ranking_rank,
                selection_id=selection.id,
                output_id=output.id,
                object_key=output.asset_object_key,
                object_version_id=output.asset_object_version_id,
                sha256=output.asset_sha256,
                content_type=output.asset_content_type,
                image_format=output.asset_image_format,
                width=output.asset_width,
                height=output.asset_height,
                byte_size=output.asset_byte_size,
                path=f"content/{archive_ordinal:03d}.{extension}",
            )
        )
    _validate_archive_identity(
        archive,
        release_version_id=version.id,
        selection_count=len(outputs),
    )
    manifest = canonical_json_bytes(
        {
            "schema": _ARCHIVE_SCHEMA,
            "archive_id": str(archive.id),
            "review_task_id": str(review.id),
            "release_version_id": str(version.id),
            "selection_count": len(outputs),
            "max_images_per_part": _MAX_IMAGES_PER_PART,
            "ordering": "frozen_generation_queue",
            "ordering_key": [
                "generation_queue_position",
            ],
            "outputs": [_manifest_record(output) for output in outputs],
        }
    )
    return _ArchivePlan(
        archive_id=archive.id,
        review_task_id=review.id,
        release_version_id=version.id,
        selection_count=len(outputs),
        outputs=tuple(outputs),
        manifest=manifest,
        manifest_sha256=hashlib.sha256(manifest).hexdigest(),
    )


def _partition_outputs(
    plan: _ArchivePlan,
    *,
    max_archive_bytes: int,
) -> tuple[tuple[_OutputRecord, ...], ...]:
    chunks: list[tuple[_OutputRecord, ...]] = []
    cursor = 0
    while cursor < len(plan.outputs):
        accepted: tuple[_OutputRecord, ...] | None = None
        max_end = min(cursor + _MAX_IMAGES_PER_PART, len(plan.outputs))
        for end in range(cursor + 1, max_end + 1):
            candidate = plan.outputs[cursor:end]
            part_manifest = _part_manifest(
                plan,
                candidate,
                part_number=999,
                part_count=999,
            )
            entries = (
                ("set-manifest.json", len(plan.manifest)),
                ("part-manifest.json", len(part_manifest)),
                *((output.path, output.byte_size) for output in candidate),
            )
            if _zip_stored_size(entries) > max_archive_bytes:
                break
            accepted = candidate
        if accepted is None:
            raise _FinishedSetArchiveContractError(
                "one finished-set image cannot fit in the configured archive part capacity"
            )
        chunks.append(accepted)
        cursor += len(accepted)
    if not chunks or len(chunks) > _MAX_ARCHIVE_PARTS:
        raise _FinishedSetArchiveContractError("finished-set archive part count is invalid")
    return tuple(chunks)


async def _build_part(
    store: ObjectStore,
    *,
    plan: _ArchivePlan,
    outputs: tuple[_OutputRecord, ...],
    part_number: int,
    part_count: int,
    max_archive_bytes: int,
) -> _BuiltPart:
    image_entries: list[tuple[str, bytes]] = []
    for output in outputs:
        body = await store.read_bytes(
            output.object_key,
            version_id=output.object_version_id,
            max_bytes=output.byte_size,
        )
        if len(body) != output.byte_size or hashlib.sha256(body).hexdigest() != output.sha256:
            raise _FinishedSetArchiveContractError(
                "a finished-set derivative no longer matches its frozen bytes"
            )
        try:
            require_metadata_free_image(body, content_type=output.content_type)
        except OutboundImagePrivacyError as error:
            raise _FinishedSetArchiveContractError(
                "a finished-set derivative contains embedded metadata"
            ) from error
        image_entries.append((output.path, body))
    part_manifest = _part_manifest(
        plan,
        outputs,
        part_number=part_number,
        part_count=part_count,
    )
    body = await asyncio.to_thread(
        _zip_bytes,
        (
            ("set-manifest.json", plan.manifest),
            ("part-manifest.json", part_manifest),
            *image_entries,
        ),
    )
    if len(body) > max_archive_bytes:
        raise _FinishedSetArchiveContractError(
            "finished-set archive exceeded its deterministic part capacity"
        )
    return _BuiltPart(
        part_number=part_number,
        part_count=part_count,
        first_ordinal=outputs[0].ordinal,
        last_ordinal=outputs[-1].ordinal,
        body=body,
        sha256=hashlib.sha256(body).hexdigest(),
    )


async def _write_or_adopt_part(
    store: ObjectStore,
    *,
    plan: _ArchivePlan,
    part: _BuiltPart,
    max_archive_bytes: int,
) -> _StoredPart:
    key = (
        f"finished-set-archives/{plan.archive_id}/"
        f"part-{part.part_number:03d}-of-{part.part_count:03d}/{part.sha256}.zip"
    )
    expected_metadata = _part_object_metadata(
        archive_id=plan.archive_id,
        review_task_id=plan.review_task_id,
        release_version_id=plan.release_version_id,
        part_number=part.part_number,
        part_count=part.part_count,
        first_ordinal=part.first_ordinal,
        last_ordinal=part.last_ordinal,
        sha256=part.sha256,
        manifest_sha256=plan.manifest_sha256,
    )
    metadata: ObjectMetadata | None
    try:
        metadata = await store.write_bytes_if_absent(
            key=key,
            body=part.body,
            content_type=_ARCHIVE_CONTENT_TYPE,
            metadata=expected_metadata,
            max_bytes=max_archive_bytes,
        )
    except ObjectAlreadyExistsError as error:
        metadata = await store.head(key)
        if metadata is None or metadata.version_id is None:
            raise _FinishedSetArchiveContractError(
                "existing finished-set archive object has no immutable version"
            ) from error
        existing = await store.read_bytes(
            key,
            version_id=metadata.version_id,
            max_bytes=max_archive_bytes,
        )
        if existing != part.body:
            raise _FinishedSetArchiveContractError(
                "existing finished-set archive object does not match deterministic bytes"
            ) from error
    if not _stored_metadata_matches(
        metadata,
        key=key,
        version_id=metadata.version_id if metadata is not None else None,
        byte_size=len(part.body),
        expected_metadata=expected_metadata,
    ):
        raise _FinishedSetArchiveContractError(
            "stored finished-set archive metadata does not match deterministic bytes"
        )
    assert metadata is not None
    return _StoredPart(
        part_number=part.part_number,
        part_count=part.part_count,
        first_ordinal=part.first_ordinal,
        last_ordinal=part.last_ordinal,
        sha256=part.sha256,
        byte_size=len(part.body),
        storage_backend=store.backend,
        storage_bucket=store.bucket,
        object_key=key,
        metadata=metadata,
    )


async def _prepare_checkpoint_resume(
    session: AsyncSession,
    *,
    claim: _ClaimedArchive,
    plan: _ArchivePlan,
    chunks: tuple[tuple[_OutputRecord, ...], ...],
    lease_seconds: int,
    now: datetime,
) -> tuple[FinishedSetArchivePart, ...]:
    archive = await _locked_claim_archive(session, claim=claim, now=now)
    if (
        archive.id != plan.archive_id
        or archive.review_task_id != plan.review_task_id
        or archive.release_version_id != plan.release_version_id
        or archive.selection_count != plan.selection_count
    ):
        raise _FinishedSetArchiveContractError("finished-set archive plan changed after claim")
    if archive.manifest_sha256 is None:
        archive.manifest_sha256 = plan.manifest_sha256
    elif archive.manifest_sha256 != plan.manifest_sha256:
        raise _FinishedSetArchiveContractError(
            "finished-set archive manifest changed after a checkpoint"
        )
    if archive.part_count is None:
        archive.part_count = len(chunks)
    elif archive.part_count != len(chunks):
        raise _FinishedSetArchiveContractError(
            "finished-set archive partition changed after a checkpoint"
        )
    _require_archive_plan(archive, plan=plan, part_count=len(chunks))
    parts = await _registered_parts(session, archive_id=archive.id)
    _validate_registered_parts(parts, plan=plan, chunks=chunks, complete=False)
    archive.lease_expires_at = now + timedelta(seconds=lease_seconds)
    archive.updated_at = now
    await session.commit()
    return parts


async def _verify_checkpointed_parts(
    store: ObjectStore,
    *,
    plan: _ArchivePlan,
    parts: tuple[FinishedSetArchivePart, ...],
) -> None:
    for part in parts:
        if store.backend != part.storage_backend or store.bucket != part.storage_bucket:
            raise _FinishedSetArchiveContractError(
                "checkpointed finished-set archive storage is unavailable"
            )
        metadata = await store.head(part.object_key)
        expected_metadata = _part_object_metadata(
            archive_id=plan.archive_id,
            review_task_id=plan.review_task_id,
            release_version_id=plan.release_version_id,
            part_number=part.part_number,
            part_count=part.part_count,
            first_ordinal=part.first_ordinal,
            last_ordinal=part.last_ordinal,
            sha256=part.sha256,
            manifest_sha256=part.manifest_sha256,
        )
        if not _stored_metadata_matches(
            metadata,
            key=part.object_key,
            version_id=part.object_version_id,
            byte_size=part.byte_size,
            expected_metadata=expected_metadata,
        ):
            raise _FinishedSetArchiveContractError(
                "checkpointed finished-set archive storage snapshot is invalid"
            )


async def _checkpoint_part(
    session: AsyncSession,
    *,
    claim: _ClaimedArchive,
    plan: _ArchivePlan,
    chunks: tuple[tuple[_OutputRecord, ...], ...],
    stored: _StoredPart,
    lease_seconds: int,
    now: datetime,
) -> None:
    archive = await _locked_claim_archive(session, claim=claim, now=now)
    _require_archive_plan(archive, plan=plan, part_count=len(chunks))
    parts = await _registered_parts(session, archive_id=archive.id)
    _validate_registered_parts(parts, plan=plan, chunks=chunks, complete=False)
    expected_number = len(parts) + 1
    if stored.part_number != expected_number or expected_number > len(chunks):
        raise _FinishedSetArchiveContractError(
            "finished-set archive checkpoint is not the next ordered part"
        )
    expected_chunk = chunks[expected_number - 1]
    if (
        stored.part_count != len(chunks)
        or stored.first_ordinal != expected_chunk[0].ordinal
        or stored.last_ordinal != expected_chunk[-1].ordinal
    ):
        raise _FinishedSetArchiveContractError(
            "finished-set archive checkpoint does not match its frozen partition"
        )
    version_id = stored.metadata.version_id
    if version_id is None:
        raise _FinishedSetArchiveContractError(
            "stored finished-set archive part has no immutable version"
        )
    session.add(
        FinishedSetArchivePart(
            id=uuid7(),
            archive_id=archive.id,
            part_number=stored.part_number,
            part_count=stored.part_count,
            first_ordinal=stored.first_ordinal,
            last_ordinal=stored.last_ordinal,
            storage_backend=stored.storage_backend,
            storage_bucket=stored.storage_bucket,
            object_key=stored.object_key,
            object_version_id=version_id,
            sha256=stored.sha256,
            manifest_sha256=plan.manifest_sha256,
            byte_size=stored.byte_size,
            content_type=_ARCHIVE_CONTENT_TYPE,
            created_at=now,
        )
    )
    archive.lease_expires_at = now + timedelta(seconds=lease_seconds)
    archive.updated_at = now
    await session.commit()


async def _finalize_archive(
    session: AsyncSession,
    *,
    claim: _ClaimedArchive,
    plan: _ArchivePlan,
    chunks: tuple[tuple[_OutputRecord, ...], ...],
    now: datetime,
) -> None:
    archive = await _locked_claim_archive(session, claim=claim, now=now)
    _require_archive_plan(archive, plan=plan, part_count=len(chunks))
    parts = await _registered_parts(session, archive_id=archive.id)
    _validate_registered_parts(parts, plan=plan, chunks=chunks, complete=True)
    archive.state = FinishedSetArchiveState.READY
    archive.lease_owner = None
    archive.lease_expires_at = None
    archive.last_error_code = None
    archive.last_error_detail = None
    archive.completed_at = now
    archive.updated_at = now
    await session.commit()


async def _locked_claim_archive(
    session: AsyncSession,
    *,
    claim: _ClaimedArchive,
    now: datetime,
) -> FinishedSetArchive:
    archive = await session.scalar(
        select(FinishedSetArchive)
        .where(FinishedSetArchive.id == claim.archive_id)
        .with_for_update()
    )
    if (
        archive is None
        or archive.state != FinishedSetArchiveState.PROCESSING
        or archive.lease_owner != claim.worker_id
        or archive.attempts != claim.attempt
        or archive.lease_expires_at is None
        or _as_utc(archive.lease_expires_at) <= now
    ):
        raise _FinishedSetArchiveContractError("finished-set archive lease was lost")
    return archive


def _require_archive_plan(
    archive: FinishedSetArchive,
    *,
    plan: _ArchivePlan,
    part_count: int,
) -> None:
    if (
        archive.id != plan.archive_id
        or archive.review_task_id != plan.review_task_id
        or archive.release_version_id != plan.release_version_id
        or archive.selection_count != plan.selection_count
        or archive.manifest_sha256 != plan.manifest_sha256
        or archive.part_count != part_count
    ):
        raise _FinishedSetArchiveContractError("finished-set archive plan changed after claim")


async def _registered_parts(
    session: AsyncSession,
    *,
    archive_id: UUID,
) -> tuple[FinishedSetArchivePart, ...]:
    return tuple(
        (
            await session.scalars(
                select(FinishedSetArchivePart)
                .where(FinishedSetArchivePart.archive_id == archive_id)
                .order_by(FinishedSetArchivePart.part_number)
            )
        ).all()
    )


def _validate_registered_parts(
    parts: tuple[FinishedSetArchivePart, ...],
    *,
    plan: _ArchivePlan,
    chunks: tuple[tuple[_OutputRecord, ...], ...],
    complete: bool,
) -> None:
    if (
        len(parts) > len(chunks)
        or tuple(part.part_number for part in parts) != tuple(range(1, len(parts) + 1))
        or any(part.part_count != len(chunks) for part in parts)
        or any(part.manifest_sha256 != plan.manifest_sha256 for part in parts)
    ):
        raise _FinishedSetArchiveContractError(
            "finished-set archive checkpoints are not a valid ordered prefix"
        )
    for part, chunk in zip(parts, chunks, strict=False):
        if part.first_ordinal != chunk[0].ordinal or part.last_ordinal != chunk[-1].ordinal:
            raise _FinishedSetArchiveContractError("finished-set archive checkpoint range changed")
    if complete and len(parts) != len(chunks):
        raise _FinishedSetArchiveContractError("finished-set archive checkpoints are incomplete")


async def _fail_claim(
    sessions: async_sessionmaker[AsyncSession],
    *,
    claim: _ClaimedArchive,
    error_code: str,
    error_detail: str,
    retry: bool,
    retry_base_seconds: int,
    retry_max_seconds: int,
    now: datetime,
) -> FinishedSetArchiveState:
    async with sessions() as session:
        archive = await session.scalar(
            select(FinishedSetArchive)
            .where(FinishedSetArchive.id == claim.archive_id)
            .with_for_update()
        )
        if (
            archive is None
            or archive.state != FinishedSetArchiveState.PROCESSING
            or archive.lease_owner != claim.worker_id
            or archive.attempts != claim.attempt
        ):
            return FinishedSetArchiveState.PROCESSING
        should_retry = retry and archive.attempts < archive.max_attempts
        archive.state = (
            FinishedSetArchiveState.RETRY_WAIT if should_retry else FinishedSetArchiveState.FAILED
        )
        archive.lease_owner = None
        archive.lease_expires_at = None
        archive.last_error_code = error_code
        archive.last_error_detail = error_detail
        archive.updated_at = now
        if should_retry:
            archive.available_at = now + timedelta(
                seconds=min(
                    retry_max_seconds,
                    retry_base_seconds * (2 ** max(0, archive.attempts - 1)),
                )
            )
            archive.completed_at = None
        else:
            archive.completed_at = now
        await session.commit()
        return archive.state


async def _shield_cancelled_claim_retry(
    sessions: async_sessionmaker[AsyncSession],
    *,
    claim: _ClaimedArchive,
    retry_base_seconds: int,
    retry_max_seconds: int,
    now: datetime,
) -> None:
    settlement = asyncio.create_task(
        _fail_claim(
            sessions,
            claim=claim,
            error_code="archive_cycle_cancelled",
            error_detail=(
                "Finished-set archive creation was interrupted and will resume from "
                "its durable checkpoints."
            ),
            retry=True,
            retry_base_seconds=retry_base_seconds,
            retry_max_seconds=retry_max_seconds,
            now=now,
        )
    )
    while not settlement.done():
        try:
            await asyncio.shield(settlement)
        except asyncio.CancelledError:
            continue
        except Exception:
            return
    try:
        settlement.result()
    except Exception:
        # Preserve cancellation semantics even if the best-effort durable
        # settlement itself encounters a database outage.
        return


async def _load_completed_ready_review(
    session: AsyncSession,
    *,
    review_task_id: UUID,
    lock: bool,
) -> tuple[ReviewTask, Release, ReleaseVersion]:
    query = (
        select(ReviewTask, Release, ReleaseVersion)
        .join(ReleaseVersion, ReleaseVersion.id == ReviewTask.release_version_id)
        .join(Release, Release.id == ReleaseVersion.release_id)
        .where(ReviewTask.id == review_task_id)
    )
    if lock:
        query = query.with_for_update()
    row = (await session.execute(query)).one_or_none()
    if row is None:
        review_exists = await session.get(ReviewTask, review_task_id)
        if review_exists is None:
            raise FinishedSetArchiveNotFoundError("completed review task was not found")
        raise FinishedSetArchiveConflictError("review release snapshot is unavailable")
    review, release, version = row
    if review.state != ReviewTaskState.COMPLETED:
        raise FinishedSetArchiveConflictError("finished-set archive requires a completed review")
    if (
        review.release_version_id != version.id
        or version.release_id != release.id
        or release.current_version_no != version.version_no
        or release.phase not in _DOWNLOADABLE_RELEASE_PHASES
    ):
        raise FinishedSetArchiveConflictError(
            "finished-set archive requires the current downloadable release"
        )
    return review, release, version


async def _selection_count(session: AsyncSession, *, review_task_id: UUID) -> int:
    return int(
        await session.scalar(
            select(func.count(ReleaseSelection.id)).where(
                ReleaseSelection.review_task_id == review_task_id
            )
        )
        or 0
    )


async def _snapshot(
    session: AsyncSession,
    archive: FinishedSetArchive,
) -> FinishedSetArchiveSnapshot:
    parts = await _load_valid_parts(session, archive)
    return FinishedSetArchiveSnapshot(
        archive_id=archive.id,
        review_task_id=archive.review_task_id,
        release_version_id=archive.release_version_id,
        state=archive.state,
        selection_count=archive.selection_count,
        manifest_sha256=archive.manifest_sha256,
        part_count=archive.part_count,
        attempts=archive.attempts,
        max_attempts=archive.max_attempts,
        available_at=_as_utc(archive.available_at),
        created_at=_as_utc(archive.created_at),
        started_at=_optional_utc(archive.started_at),
        completed_at=_optional_utc(archive.completed_at),
        last_error_code=archive.last_error_code,
        last_error_detail=archive.last_error_detail,
        parts=tuple(
            FinishedSetArchivePartSnapshot(
                part_id=part.id,
                part_number=part.part_number,
                part_count=part.part_count,
                first_ordinal=part.first_ordinal,
                last_ordinal=part.last_ordinal,
                sha256=part.sha256,
                manifest_sha256=part.manifest_sha256,
                byte_size=part.byte_size,
            )
            for part in parts
        ),
    )


async def _load_valid_parts(
    session: AsyncSession,
    archive: FinishedSetArchive,
) -> tuple[FinishedSetArchivePart, ...]:
    parts = tuple(
        (
            await session.scalars(
                select(FinishedSetArchivePart)
                .where(FinishedSetArchivePart.archive_id == archive.id)
                .order_by(FinishedSetArchivePart.part_number)
            )
        ).all()
    )
    if not parts:
        if archive.state == FinishedSetArchiveState.READY:
            raise FinishedSetArchiveConflictError("finished-set archive parts are incomplete")
        return ()
    if (
        archive.part_count is None
        or archive.manifest_sha256 is None
        or len(parts) > archive.part_count
        or tuple(part.part_number for part in parts) != tuple(range(1, len(parts) + 1))
        or any(part.part_count != archive.part_count for part in parts)
        or any(part.manifest_sha256 != archive.manifest_sha256 for part in parts)
        or parts[0].first_ordinal != 1
        or parts[-1].last_ordinal > archive.selection_count
        or any(
            current.last_ordinal + 1 != following.first_ordinal
            for current, following in pairwise(parts)
        )
    ):
        raise FinishedSetArchiveConflictError("finished-set archive parts are incomplete")
    if archive.state == FinishedSetArchiveState.READY and (
        len(parts) != archive.part_count or parts[-1].last_ordinal != archive.selection_count
    ):
        raise FinishedSetArchiveConflictError("finished-set archive parts are incomplete")
    return parts


async def _require_owner(
    session: AsyncSession,
    *,
    actor_user_id: UUID,
    actor_role: AdminRole | str,
) -> AdminUser:
    try:
        asserted_role = actor_role if isinstance(actor_role, AdminRole) else AdminRole(actor_role)
    except ValueError as error:
        raise FinishedSetArchiveInputError("actor role is invalid") from error
    actor = await session.get(AdminUser, actor_user_id)
    if (
        actor is None
        or not actor.is_active
        or actor.role != AdminRole.OWNER
        or asserted_role != AdminRole.OWNER
    ):
        raise FinishedSetArchiveConflictError("an active owner is required")
    return actor


def _validate_archive_identity(
    archive: FinishedSetArchive,
    *,
    release_version_id: UUID,
    selection_count: int,
) -> None:
    _require_selection_count(selection_count)
    if (
        archive.release_version_id != release_version_id
        or archive.selection_count != selection_count
    ):
        raise FinishedSetArchiveConflictError(
            "finished-set archive no longer matches the completed review"
        )


def _require_selection_count(selection_count: int) -> None:
    if not 1 <= selection_count <= MAX_ACCEPTED_IMAGES_PER_RELEASE:
        raise FinishedSetArchiveInputError(
            "finished-set archive requires between 1 and "
            f"{MAX_ACCEPTED_IMAGES_PER_RELEASE} selected images"
        )


def _manifest_record(output: _OutputRecord) -> dict[str, Any]:
    return {
        "ordinal": output.ordinal,
        "generation_ordinal": output.generation_ordinal,
        "generation_job_id": str(output.generation_job_id),
        "generation_queue_position": output.generation_queue_position,
        "source_output_index": output.source_output_index,
        "review_display_order": output.review_display_order,
        "ranking_rank": output.ranking_rank,
        "selection_id": str(output.selection_id),
        "derivative_output_id": str(output.output_id),
        "path": output.path,
        "sha256": output.sha256,
        "content_type": output.content_type,
        "image_format": output.image_format,
        "width": output.width,
        "height": output.height,
        "byte_size": output.byte_size,
    }


def _part_manifest(
    plan: _ArchivePlan,
    outputs: tuple[_OutputRecord, ...],
    *,
    part_number: int,
    part_count: int,
) -> bytes:
    return canonical_json_bytes(
        {
            "schema": _PART_SCHEMA,
            "archive_id": str(plan.archive_id),
            "review_task_id": str(plan.review_task_id),
            "release_version_id": str(plan.release_version_id),
            "set_manifest_sha256": plan.manifest_sha256,
            "part_number": part_number,
            "part_count": part_count,
            "first_ordinal": outputs[0].ordinal,
            "last_ordinal": outputs[-1].ordinal,
            "outputs": [_manifest_record(output) for output in outputs],
        }
    )


def _zip_bytes(entries: tuple[tuple[str, bytes], ...]) -> bytes:
    buffer = BytesIO()
    with ZipFile(buffer, mode="w", compression=ZIP_STORED, allowZip64=False) as archive:
        for path, body in entries:
            info = ZipInfo(path, date_time=_ZIP_TIMESTAMP)
            info.compress_type = ZIP_STORED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.extra = b""
            info.comment = b""
            archive.writestr(info, body)
    return buffer.getvalue()


def _zip_stored_size(entries: tuple[tuple[str, int], ...]) -> int:
    # Deterministic ASCII paths, empty extras/comments, ZIP_STORED, and no ZIP64.
    return 22 + sum(size + 76 + (2 * len(path.encode("ascii"))) for path, size in entries)


def _part_object_metadata(
    *,
    archive_id: UUID,
    review_task_id: UUID,
    release_version_id: UUID,
    part_number: int,
    part_count: int,
    first_ordinal: int,
    last_ordinal: int,
    sha256: str,
    manifest_sha256: str,
) -> dict[str, str]:
    return {
        "sha256": sha256,
        "manifest-sha256": manifest_sha256,
        "archive-id": str(archive_id),
        "review-task-id": str(review_task_id),
        "release-version-id": str(release_version_id),
        "part-number": str(part_number),
        "part-count": str(part_count),
        "first-ordinal": str(first_ordinal),
        "last-ordinal": str(last_ordinal),
    }


def _stored_metadata_matches(
    metadata: ObjectMetadata | None,
    *,
    key: str,
    version_id: str | None,
    byte_size: int,
    expected_metadata: dict[str, str],
) -> bool:
    return bool(
        metadata is not None
        and version_id is not None
        and metadata.key == key
        and metadata.version_id == version_id
        and metadata.byte_size == byte_size
        and metadata.content_type == _ARCHIVE_CONTENT_TYPE
        and all(metadata.metadata.get(name) == value for name, value in expected_metadata.items())
    )


def _download_filename(review_task_id: UUID, part_number: int, part_count: int) -> str:
    if part_count == 1:
        return f"finished-ranked-set-{review_task_id}.zip"
    return f"finished-ranked-set-{review_task_id}-part-{part_number:03d}-of-{part_count:03d}.zip"


def _worker_id(value: str) -> str:
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > 200:
        raise ValueError("archive worker_id must contain 1 to 200 characters")
    return normalized


def _bounded_int(value: int, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise FinishedSetArchiveInputError(f"{name} must be between {minimum} and {maximum}")
    return value


def _safe_error(code: str, detail: str) -> tuple[str, str]:
    normalized_code = re.sub(r"[^a-z0-9_]", "_", code.lower())[:100] or "error"
    normalized_detail = " ".join(detail.split())[:_MAX_SAFE_ERROR_BYTES]
    return normalized_code, normalized_detail


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _optional_utc(value: datetime | None) -> datetime | None:
    return _as_utc(value) if value is not None else None


def _runtime_now(value: datetime | None) -> datetime:
    return _as_utc(value or datetime.now(UTC))


def _canonical_datetime(value: datetime) -> str:
    return _as_utc(value).isoformat().replace("+00:00", "Z")
