"""Restart-safe extracted-folder delivery of finished sets to MEGA.

The canonical input is the provider-independent ``FinishedSetArchive`` rather
than a Patreon package.  Each deterministic archive part is verified and
opened in a bounded temporary directory, while the original full-resolution
image bytes are uploaded unchanged into one flat, numerically ordered MEGA
folder.  The ordered manifest and completion marker are uploaded last.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import tempfile
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from io import BytesIO
from itertools import pairwise
from pathlib import Path, PurePosixPath
from typing import Any, Protocol
from uuid import UUID
from zipfile import ZIP_STORED, BadZipFile, ZipFile, ZipInfo

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gen_automation.db.models import (
    FinishedSetArchive,
    FinishedSetArchivePart,
    MegaSetDelivery,
    MegaSetDeliveryItem,
    Release,
    ReleaseVersion,
)
from gen_automation.domain.canonical import canonical_json_bytes
from gen_automation.domain.enums import FinishedSetArchiveState, MegaDeliveryState
from gen_automation.integrations.mega import (
    MegaError,
    MegaRemoteConflictError,
    MegaRemoteNode,
)
from gen_automation.integrations.mega.client import (
    validate_remote_filename,
    validate_remote_path,
)
from gen_automation.services.finished_set_archives import request_finished_set_archive
from gen_automation.services.outbound_image_privacy import (
    OutboundImagePrivacyError,
    require_metadata_free_image,
)
from gen_automation.storage.base import ObjectStore, ObjectStoreError

_SOURCE_MANIFEST_SCHEMA = "finished-set-manifest/v1"
_SOURCE_PART_SCHEMA = "finished-set-part-manifest/v1"
_REMOTE_MANIFEST_SCHEMA = "mega-extracted-set-manifest/v1"
_COMPLETION_SCHEMA = "mega-extracted-set-completion/v1"
_REMOTE_MANIFEST_FILENAME = "set-manifest.json"
_COMPLETION_FILENAME = "upload-complete.json"
_SAFE_FAILURE_DETAIL = "MEGA extracted-set delivery failed inside the isolated uploader."
_MAX_MANIFEST_BYTES = 2 * 1024 * 1024
_HASH_BUFFER_BYTES = 1024 * 1024
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CONTENT_PATH = re.compile(r"^content/([0-9]{3})\.(jpg|png|webp)$")
_CONTENT_TYPES = {
    "jpg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
}


class MegaSetDeliveryError(Exception):
    """Base error for extracted finished-set delivery."""


class MegaSetDeliveryContractError(MegaSetDeliveryError):
    """Frozen source data or a delivery identity is inconsistent."""


class MegaSetDeliveryLeaseLostError(MegaSetDeliveryError):
    """The controller no longer owns the active delivery lease."""


class MegaSetDeliveryClient(Protocol):
    async def ensure_folder(self, remote_folder: str) -> None: ...

    async def list_files(self, remote_folder: str) -> tuple[str, ...]: ...

    async def find_file(self, remote_path: str) -> tuple[MegaRemoteNode, ...]: ...

    async def upload_file(self, local_file: Path, remote_folder: str) -> None: ...

    async def upload_files(self, local_files: tuple[Path, ...], remote_folder: str) -> None: ...

    async def download_node(self, node: MegaRemoteNode, local_folder: Path) -> Path: ...


@dataclass(frozen=True, slots=True)
class ClaimedMegaSetDelivery:
    delivery_id: UUID
    archive_id: UUID
    manifest_sha256: str
    total_item_count: int
    remote_root: str
    remote_folder: str
    attempt: int
    lease_expires_at: datetime


@dataclass(frozen=True, slots=True)
class MegaSetDeliveryCycleResult:
    created_delivery: bool = False
    processed_delivery: bool = False
    completed_delivery: bool = False
    uploaded_items: int = 0
    adopted_items: int = 0

    @property
    def did_work(self) -> bool:
        return (
            self.created_delivery
            or self.processed_delivery
            or self.completed_delivery
            or self.uploaded_items > 0
            or self.adopted_items > 0
        )


@dataclass(frozen=True, slots=True)
class MegaSetDeliveryRequestResult:
    archive_id: UUID
    delivery_id: UUID | None
    replayed: bool


@dataclass(frozen=True, slots=True)
class _SourcePart:
    id: UUID
    part_number: int
    part_count: int
    first_ordinal: int
    last_ordinal: int
    storage_backend: str
    storage_bucket: str
    object_key: str
    object_version_id: str
    sha256: str
    byte_size: int


@dataclass(frozen=True, slots=True)
class _ManifestItem:
    ordinal: int
    source_derivative_output_id: UUID
    source_sha256: str
    source_byte_size: int
    source_content_type: str
    source_path: str
    remote_filename: str
    remote_path: str
    source_part_number: int


@dataclass(frozen=True, slots=True)
class _DeliverySource:
    archive_id: UUID
    manifest_sha256: str
    selection_count: int
    remote_folder: str
    source_manifest_bytes: bytes | None
    parts: tuple[_SourcePart, ...]


@dataclass(frozen=True, slots=True)
class _ManifestPlan:
    source_bytes: bytes
    source_payload: dict[str, Any]
    items: tuple[_ManifestItem, ...]
    total_byte_size: int


@dataclass(frozen=True, slots=True)
class _StagedItem:
    item_id: UUID
    path: Path


async def ensure_next_mega_set_delivery(
    session: AsyncSession,
    *,
    remote_root: str,
    now: datetime | None = None,
) -> bool:
    """Create one independent MEGA delivery for a ready finished-set archive."""

    # Keep validating the live controller setting, but never use it to retarget
    # an explicit request. The request row is the durable destination authority.
    validate_remote_path(remote_root, allow_root=True)
    created_at = _as_utc(now or datetime.now(UTC))
    delivery_exists = exists(
        select(MegaSetDelivery.id).where(
            MegaSetDelivery.finished_set_archive_id == FinishedSetArchive.id
        )
    )
    candidate = (
        await session.execute(
            select(
                FinishedSetArchive,
                Release.title,
            )
            .join(
                ReleaseVersion,
                ReleaseVersion.id == FinishedSetArchive.release_version_id,
            )
            .join(Release, Release.id == ReleaseVersion.release_id)
            .where(
                FinishedSetArchive.state == FinishedSetArchiveState.READY,
                FinishedSetArchive.mega_requested_at.is_not(None),
                FinishedSetArchive.mega_requested_by_user_id.is_not(None),
                FinishedSetArchive.mega_requested_remote_root.is_not(None),
                FinishedSetArchive.manifest_sha256.is_not(None),
                FinishedSetArchive.part_count.is_not(None),
                ~delivery_exists,
            )
            .order_by(FinishedSetArchive.completed_at, FinishedSetArchive.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
    ).one_or_none()
    if candidate is None:
        await session.rollback()
        return False
    archive, release_title = candidate
    requested_root = archive.mega_requested_remote_root
    if requested_root is None:
        raise MegaSetDeliveryContractError("MEGA request destination root is missing")
    try:
        normalized_root = validate_remote_path(requested_root, allow_root=True)
    except ValueError as error:
        raise MegaSetDeliveryContractError("MEGA request destination root is invalid") from error
    manifest_sha256 = archive.manifest_sha256
    if manifest_sha256 is None or _SHA256.fullmatch(manifest_sha256) is None:
        raise MegaSetDeliveryContractError("finished-set manifest identity is invalid")
    remote_folder = _remote_folder(
        normalized_root,
        set_name=release_title,
    )
    session.add(
        MegaSetDelivery(
            finished_set_archive_id=archive.id,
            state=MegaDeliveryState.PENDING,
            remote_root=normalized_root,
            remote_folder=remote_folder,
            manifest_sha256=manifest_sha256,
            total_item_count=archive.selection_count,
            uploaded_item_count=0,
            total_byte_size=None,
            source_manifest_json=None,
            uploaded_byte_size=0,
            attempts=0,
            available_at=created_at,
            lease_owner=None,
            lease_expires_at=None,
            completion_marker_node_handle=None,
            planned_at=None,
            started_at=None,
            verified_at=None,
            completed_at=None,
            last_error_code=None,
            last_error_detail=None,
            created_at=created_at,
            updated_at=created_at,
        )
    )
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        return False
    return True


async def request_mega_set_delivery(
    session: AsyncSession,
    *,
    review_task_id: UUID,
    requested_by_user_id: UUID,
    remote_root: str,
    now: datetime | None = None,
) -> MegaSetDeliveryRequestResult:
    """Persist one explicit MEGA request without selecting any other target."""

    normalized_root = validate_remote_path(remote_root, allow_root=True)
    requested_at = _as_utc(now or datetime.now(UTC))
    snapshot = await request_finished_set_archive(
        session,
        review_task_id=review_task_id,
        requested_by_user_id=None,
        now=requested_at,
    )
    archive = await session.scalar(
        select(FinishedSetArchive)
        .where(FinishedSetArchive.id == snapshot.archive_id)
        .with_for_update()
    )
    if archive is None:
        raise MegaSetDeliveryContractError("finished-set archive disappeared during request")

    delivery = await session.scalar(
        select(MegaSetDelivery)
        .where(MegaSetDelivery.finished_set_archive_id == archive.id)
        .with_for_update()
    )
    request_fields = (
        archive.mega_requested_at,
        archive.mega_requested_by_user_id,
        archive.mega_requested_remote_root,
    )
    request_is_absent = all(value is None for value in request_fields)
    if not request_is_absent and any(value is None for value in request_fields):
        raise MegaSetDeliveryContractError("stored MEGA request identity is incomplete")
    if not request_is_absent and archive.mega_requested_remote_root != normalized_root:
        raise MegaSetDeliveryContractError(
            "the existing MEGA request uses a different destination root"
        )
    if delivery is not None and delivery.remote_root != normalized_root:
        raise MegaSetDeliveryContractError(
            "the existing MEGA delivery uses a different destination root"
        )
    changed = request_is_absent
    if request_is_absent:
        archive.mega_requested_at = requested_at
        archive.mega_requested_by_user_id = requested_by_user_id
        archive.mega_requested_remote_root = normalized_root
        archive.updated_at = requested_at
    if delivery is not None and delivery.state in {
        MegaDeliveryState.FAILED,
        MegaDeliveryState.RETRY_WAIT,
    }:
        delivery.state = MegaDeliveryState.PENDING
        delivery.available_at = requested_at
        delivery.lease_owner = None
        delivery.lease_expires_at = None
        delivery.completion_marker_node_handle = None
        delivery.verified_at = None
        delivery.completed_at = None
        delivery.last_error_code = None
        delivery.last_error_detail = None
        delivery.updated_at = requested_at
        changed = True
    await session.commit()
    return MegaSetDeliveryRequestResult(
        archive_id=archive.id,
        delivery_id=delivery.id if delivery is not None else None,
        replayed=not changed,
    )


async def claim_mega_set_delivery(
    session: AsyncSession,
    *,
    worker_id: str,
    lease_seconds: int,
    now: datetime | None = None,
) -> ClaimedMegaSetDelivery | None:
    normalized_worker = _worker_id(worker_id)
    if isinstance(lease_seconds, bool) or not 120 <= lease_seconds <= 7200:
        raise ValueError("MEGA set lease must be between 120 and 7200 seconds")
    claimed_at = _as_utc(now or datetime.now(UTC))
    due = or_(
        and_(
            MegaSetDelivery.state.in_((MegaDeliveryState.PENDING, MegaDeliveryState.RETRY_WAIT)),
            MegaSetDelivery.available_at <= claimed_at,
        ),
        and_(
            MegaSetDelivery.state == MegaDeliveryState.CLAIMED,
            MegaSetDelivery.lease_expires_at.is_not(None),
            MegaSetDelivery.lease_expires_at <= claimed_at,
        ),
    )
    delivery = await session.scalar(
        select(MegaSetDelivery)
        .where(due)
        .order_by(
            MegaSetDelivery.available_at,
            MegaSetDelivery.created_at,
            MegaSetDelivery.id,
        )
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if delivery is None:
        await session.rollback()
        return None
    delivery.state = MegaDeliveryState.CLAIMED
    delivery.attempts += 1
    delivery.lease_owner = normalized_worker
    delivery.lease_expires_at = claimed_at + timedelta(seconds=lease_seconds)
    delivery.started_at = delivery.started_at or claimed_at
    delivery.last_error_code = None
    delivery.last_error_detail = None
    delivery.updated_at = claimed_at
    claim = ClaimedMegaSetDelivery(
        delivery_id=delivery.id,
        archive_id=delivery.finished_set_archive_id,
        manifest_sha256=delivery.manifest_sha256,
        total_item_count=delivery.total_item_count,
        remote_root=delivery.remote_root,
        remote_folder=delivery.remote_folder,
        attempt=delivery.attempts,
        lease_expires_at=delivery.lease_expires_at,
    )
    await session.commit()
    return claim


async def process_claimed_mega_set_delivery(
    sessions: async_sessionmaker[AsyncSession],
    *,
    claim: ClaimedMegaSetDelivery,
    worker_id: str,
    store: ObjectStore,
    client: MegaSetDeliveryClient,
    lease_seconds: int,
    max_part_bytes: int,
    batch_size: int,
    now: datetime | None = None,
) -> tuple[int, int, bool]:
    """Reconcile and upload at most one source archive part."""

    if not 1024 <= max_part_bytes <= 512 * 1024 * 1024:
        raise ValueError("MEGA source part byte limit is invalid")
    if isinstance(batch_size, bool) or not 1 <= batch_size <= 100:
        raise ValueError("MEGA upload batch size must be between 1 and 100")
    cycle_at = _as_utc(now or datetime.now(UTC))
    async with sessions() as session:
        source = await _load_delivery_source(session, claim=claim, store=store)
    manifest_plan = await _load_manifest_plan(
        store,
        source=source,
        max_part_bytes=max_part_bytes,
    )
    async with sessions() as session:
        await _ensure_items_planned(
            session,
            claim=claim,
            plan=manifest_plan,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
            now=cycle_at,
        )

    await client.ensure_folder(claim.remote_folder)
    remote_files = Counter(await client.list_files(claim.remote_folder))
    async with sessions() as session:
        current_items = await _load_items(session, claim=claim)
    expected_paths = {item.remote_path for item in current_items}
    expected_paths.update(
        {
            _remote_child(claim.remote_folder, _REMOTE_MANIFEST_FILENAME),
            _remote_child(claim.remote_folder, _COMPLETION_FILENAME),
        }
    )
    if any(path not in expected_paths for path in remote_files):
        raise MegaRemoteConflictError("MEGA set folder contains unexpected files")
    if any(remote_files[path] > 1 for path in expected_paths):
        raise MegaRemoteConflictError("MEGA set folder contains duplicate expected filenames")

    pending_remote = tuple(
        item
        for item in current_items
        if item.state != MegaDeliveryState.SUCCEEDED and remote_files[item.remote_path] == 1
    )[:batch_size]
    adopted = 0
    if pending_remote:
        adopted_rows = await _verify_remote_items(
            client,
            items=pending_remote,
        )
        async with sessions() as session:
            await _complete_items(
                session,
                claim=claim,
                worker_id=worker_id,
                completed=adopted_rows,
                lease_seconds=lease_seconds,
                now=cycle_at,
            )
        adopted = len(adopted_rows)

    async with sessions() as session:
        current_items = await _load_items(session, claim=claim)
    pending = tuple(item for item in current_items if item.state != MegaDeliveryState.SUCCEEDED)
    if not pending:
        completed = await _finalize_remote_folder(
            sessions,
            claim=claim,
            worker_id=worker_id,
            client=client,
            plan=manifest_plan,
            now=cycle_at,
        )
        return 0, adopted, completed

    first_pending = pending[0]
    part = _part_for_ordinal(source.parts, first_pending.ordinal)
    part_items = tuple(
        item for item in pending if part.first_ordinal <= item.ordinal <= part.last_ordinal
    )[:batch_size]
    source_body = await _read_source_part(
        store,
        part=part,
        max_part_bytes=max_part_bytes,
    )
    with tempfile.TemporaryDirectory(prefix="gen-automation-mega-set-") as temporary:
        root = Path(temporary)
        os.chmod(root, 0o700)
        staged = await asyncio.to_thread(
            _stage_items,
            source_body,
            part,
            manifest_plan,
            part_items,
            root,
        )
        del source_body
        async with sessions() as session:
            await _record_item_attempts(
                session,
                claim=claim,
                worker_id=worker_id,
                item_ids=tuple(item.id for item in part_items),
                lease_seconds=lease_seconds,
                now=cycle_at,
            )
        await client.upload_files(tuple(item.path for item in staged), claim.remote_folder)

    completed_rows = tuple((item, None) for item in part_items)
    async with sessions() as session:
        await _complete_items(
            session,
            claim=claim,
            worker_id=worker_id,
            completed=completed_rows,
            lease_seconds=lease_seconds,
            now=cycle_at,
        )
        remaining = await _remaining_item_count(session, claim=claim)
    if remaining:
        async with sessions() as session:
            await _release_claim(
                session,
                claim=claim,
                worker_id=worker_id,
                now=cycle_at,
            )
        return len(part_items), adopted, False

    completed = await _finalize_remote_folder(
        sessions,
        claim=claim,
        worker_id=worker_id,
        client=client,
        plan=manifest_plan,
        now=cycle_at,
    )
    return len(part_items), adopted, completed


async def fail_mega_set_delivery(
    session: AsyncSession,
    *,
    claim: ClaimedMegaSetDelivery,
    worker_id: str,
    error_code: str,
    retry_delay_seconds: int,
    terminal: bool,
    now: datetime | None = None,
) -> None:
    failed_at = _as_utc(now or datetime.now(UTC))
    if isinstance(retry_delay_seconds, bool) or not 1 <= retry_delay_seconds <= 7 * 86400:
        raise ValueError("MEGA retry delay must be between 1 second and 7 days")
    delivery = await _locked_owned_delivery(
        session,
        claim=claim,
        worker_id=worker_id,
        now=failed_at,
    )
    delivery.state = MegaDeliveryState.FAILED if terminal else MegaDeliveryState.RETRY_WAIT
    delivery.available_at = (
        failed_at if terminal else failed_at + timedelta(seconds=retry_delay_seconds)
    )
    delivery.lease_owner = None
    delivery.lease_expires_at = None
    delivery.completion_marker_node_handle = None
    delivery.verified_at = None
    delivery.completed_at = failed_at if terminal else None
    delivery.last_error_code = _error_code(error_code)
    delivery.last_error_detail = _SAFE_FAILURE_DETAIL
    delivery.updated_at = failed_at
    await session.commit()


async def run_mega_set_delivery_cycle(
    sessions: async_sessionmaker[AsyncSession],
    *,
    store: ObjectStore,
    client: MegaSetDeliveryClient,
    worker_id: str,
    remote_root: str,
    lease_seconds: int,
    retry_base_seconds: int,
    retry_max_seconds: int,
    max_part_bytes: int,
    batch_size: int = 100,
) -> MegaSetDeliveryCycleResult:
    """Perform one bounded discovery or extracted-folder transfer cycle."""

    if (
        isinstance(retry_base_seconds, bool)
        or isinstance(retry_max_seconds, bool)
        or not 1 <= retry_base_seconds <= retry_max_seconds <= 7 * 86400
    ):
        raise ValueError("MEGA retry bounds are invalid")
    async with sessions() as session:
        created = await ensure_next_mega_set_delivery(session, remote_root=remote_root)
    if created:
        return MegaSetDeliveryCycleResult(created_delivery=True)

    async with sessions() as session:
        claim = await claim_mega_set_delivery(
            session,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
        )
    if claim is None:
        return MegaSetDeliveryCycleResult()
    try:
        uploaded, adopted, completed = await process_claimed_mega_set_delivery(
            sessions,
            claim=claim,
            worker_id=worker_id,
            store=store,
            client=client,
            lease_seconds=lease_seconds,
            max_part_bytes=max_part_bytes,
            batch_size=batch_size,
        )
    except asyncio.CancelledError:
        await asyncio.shield(
            _mark_claim_retry_after_cancel(
                sessions,
                claim=claim,
                worker_id=worker_id,
                retry_delay_seconds=retry_base_seconds,
            )
        )
        raise
    except (MegaSetDeliveryContractError, MegaRemoteConflictError) as error:
        async with sessions() as session:
            await fail_mega_set_delivery(
                session,
                claim=claim,
                worker_id=worker_id,
                error_code=_failure_code(error),
                retry_delay_seconds=retry_base_seconds,
                terminal=True,
            )
        return MegaSetDeliveryCycleResult(processed_delivery=True)
    except (MegaError, ObjectStoreError, OSError, BadZipFile, json.JSONDecodeError) as error:
        retry_delay = min(
            retry_max_seconds,
            retry_base_seconds * (2 ** min(max(claim.attempt - 1, 0), 16)),
        )
        async with sessions() as session:
            await fail_mega_set_delivery(
                session,
                claim=claim,
                worker_id=worker_id,
                error_code=_failure_code(error),
                retry_delay_seconds=retry_delay,
                terminal=False,
            )
        return MegaSetDeliveryCycleResult(processed_delivery=True)
    return MegaSetDeliveryCycleResult(
        processed_delivery=True,
        completed_delivery=completed,
        uploaded_items=uploaded,
        adopted_items=adopted,
    )


async def _load_delivery_source(
    session: AsyncSession,
    *,
    claim: ClaimedMegaSetDelivery,
    store: ObjectStore,
) -> _DeliverySource:
    row = (
        await session.execute(
            select(MegaSetDelivery, FinishedSetArchive)
            .join(
                FinishedSetArchive,
                FinishedSetArchive.id == MegaSetDelivery.finished_set_archive_id,
            )
            .where(MegaSetDelivery.id == claim.delivery_id)
        )
    ).one_or_none()
    if row is None:
        raise MegaSetDeliveryContractError("claimed MEGA set delivery no longer exists")
    delivery, archive = row
    if (
        delivery.finished_set_archive_id != claim.archive_id
        or delivery.manifest_sha256 != claim.manifest_sha256
        or delivery.total_item_count != claim.total_item_count
        or delivery.remote_root != claim.remote_root
        or delivery.remote_folder != claim.remote_folder
        or archive.state != FinishedSetArchiveState.READY
        or archive.manifest_sha256 != claim.manifest_sha256
        or archive.selection_count != claim.total_item_count
        or archive.part_count is None
    ):
        raise MegaSetDeliveryContractError("claimed MEGA set source changed")
    parts = tuple(
        (
            await session.scalars(
                select(FinishedSetArchivePart)
                .where(FinishedSetArchivePart.archive_id == archive.id)
                .order_by(FinishedSetArchivePart.part_number)
            )
        ).all()
    )
    if (
        len(parts) != archive.part_count
        or tuple(part.part_number for part in parts) != tuple(range(1, archive.part_count + 1))
        or any(part.part_count != archive.part_count for part in parts)
        or any(part.manifest_sha256 != claim.manifest_sha256 for part in parts)
        or parts[0].first_ordinal != 1
        or parts[-1].last_ordinal != archive.selection_count
        or any(
            current.last_ordinal + 1 != following.first_ordinal
            for current, following in pairwise(parts)
        )
    ):
        raise MegaSetDeliveryContractError("finished-set archive parts are incomplete")
    if any(
        part.storage_backend != store.backend or part.storage_bucket != store.bucket
        for part in parts
    ):
        raise MegaSetDeliveryContractError("finished-set archive uses another object store")
    source_manifest_bytes: bytes | None = None
    if delivery.source_manifest_json is not None:
        source_manifest_bytes = delivery.source_manifest_json.encode("utf-8")
        if (
            len(source_manifest_bytes) > _MAX_MANIFEST_BYTES
            or hashlib.sha256(source_manifest_bytes).hexdigest() != claim.manifest_sha256
        ):
            raise MegaSetDeliveryContractError("cached finished-set manifest changed")
    return _DeliverySource(
        archive_id=archive.id,
        manifest_sha256=claim.manifest_sha256,
        selection_count=archive.selection_count,
        remote_folder=claim.remote_folder,
        source_manifest_bytes=source_manifest_bytes,
        parts=tuple(
            _SourcePart(
                id=part.id,
                part_number=part.part_number,
                part_count=part.part_count,
                first_ordinal=part.first_ordinal,
                last_ordinal=part.last_ordinal,
                storage_backend=part.storage_backend,
                storage_bucket=part.storage_bucket,
                object_key=part.object_key,
                object_version_id=part.object_version_id,
                sha256=part.sha256,
                byte_size=part.byte_size,
            )
            for part in parts
        ),
    )


async def _load_manifest_plan(
    store: ObjectStore,
    *,
    source: _DeliverySource,
    max_part_bytes: int,
) -> _ManifestPlan:
    manifest_bytes = source.source_manifest_bytes
    if manifest_bytes is None:
        first = source.parts[0]
        body = await _read_source_part(store, part=first, max_part_bytes=max_part_bytes)
        try:
            with ZipFile(BytesIO(body), mode="r") as archive:
                _validate_zip_contract(archive, max_part_bytes=max_part_bytes)
                manifest_bytes = _read_small_entry(archive, _REMOTE_MANIFEST_FILENAME)
        except (BadZipFile, KeyError, RuntimeError, ValueError) as error:
            raise MegaSetDeliveryContractError(
                "finished-set archive manifest is unreadable"
            ) from error
        finally:
            del body
    if hashlib.sha256(manifest_bytes).hexdigest() != source.manifest_sha256:
        raise MegaSetDeliveryContractError("finished-set archive manifest hash changed")
    try:
        payload: object = json.loads(manifest_bytes)
    except json.JSONDecodeError as error:
        raise MegaSetDeliveryContractError(
            "finished-set archive manifest is invalid JSON"
        ) from error
    if not isinstance(payload, dict):
        raise MegaSetDeliveryContractError("finished-set archive manifest is invalid")
    items = _manifest_items(
        payload,
        source=source,
    )
    return _ManifestPlan(
        source_bytes=manifest_bytes,
        source_payload=payload,
        items=items,
        total_byte_size=sum(item.source_byte_size for item in items),
    )


def _manifest_items(
    payload: dict[str, Any],
    *,
    source: _DeliverySource,
) -> tuple[_ManifestItem, ...]:
    if (
        payload.get("schema") != _SOURCE_MANIFEST_SCHEMA
        or payload.get("archive_id") != str(source.archive_id)
        or payload.get("selection_count") != source.selection_count
        or payload.get("ordering") != "frozen_generation_queue"
        or payload.get("ordering_key") != ["generation_queue_position"]
    ):
        raise MegaSetDeliveryContractError("finished-set archive manifest identity is invalid")
    raw_outputs = payload.get("outputs")
    if not isinstance(raw_outputs, list) or len(raw_outputs) != source.selection_count:
        raise MegaSetDeliveryContractError("finished-set archive manifest output count is invalid")
    items: list[_ManifestItem] = []
    for expected_ordinal, raw in enumerate(raw_outputs, start=1):
        if not isinstance(raw, dict) or raw.get("ordinal") != expected_ordinal:
            raise MegaSetDeliveryContractError("finished-set output ordering is invalid")
        source_path = raw.get("path")
        sha256 = raw.get("sha256")
        byte_size = raw.get("byte_size")
        content_type = raw.get("content_type")
        output_id = raw.get("derivative_output_id")
        if not isinstance(source_path, str):
            raise MegaSetDeliveryContractError("finished-set output path is invalid")
        path_match = _CONTENT_PATH.fullmatch(source_path)
        if path_match is None or int(path_match.group(1)) != expected_ordinal:
            raise MegaSetDeliveryContractError("finished-set output path is invalid")
        extension = path_match.group(2)
        if content_type != _CONTENT_TYPES[extension]:
            raise MegaSetDeliveryContractError("finished-set output content type is invalid")
        if not isinstance(sha256, str) or _SHA256.fullmatch(sha256) is None:
            raise MegaSetDeliveryContractError("finished-set output checksum is invalid")
        if isinstance(byte_size, bool) or not isinstance(byte_size, int) or byte_size <= 0:
            raise MegaSetDeliveryContractError("finished-set output byte size is invalid")
        try:
            derivative_output_id = UUID(str(output_id))
        except (TypeError, ValueError):
            raise MegaSetDeliveryContractError(
                "finished-set derivative output identity is invalid"
            ) from None
        remote_filename = validate_remote_filename(PurePosixPath(source_path).name)
        part = _part_for_ordinal(source.parts, expected_ordinal)
        items.append(
            _ManifestItem(
                ordinal=expected_ordinal,
                source_derivative_output_id=derivative_output_id,
                source_sha256=sha256,
                source_byte_size=byte_size,
                source_content_type=content_type,
                source_path=source_path,
                remote_filename=remote_filename,
                remote_path=_remote_child(source.remote_folder, remote_filename),
                source_part_number=part.part_number,
            )
        )
    return tuple(items)


async def _ensure_items_planned(
    session: AsyncSession,
    *,
    claim: ClaimedMegaSetDelivery,
    plan: _ManifestPlan,
    worker_id: str,
    lease_seconds: int,
    now: datetime,
) -> None:
    delivery = await _locked_owned_delivery(
        session,
        claim=claim,
        worker_id=worker_id,
        now=now,
    )
    existing = tuple(
        (
            await session.scalars(
                select(MegaSetDeliveryItem)
                .where(MegaSetDeliveryItem.delivery_id == claim.delivery_id)
                .order_by(MegaSetDeliveryItem.ordinal)
            )
        ).all()
    )
    if existing:
        if (
            delivery.total_byte_size != plan.total_byte_size
            or delivery.source_manifest_json != plan.source_bytes.decode("utf-8")
            or delivery.planned_at is None
            or len(existing) != len(plan.items)
            or any(
                not _item_matches(item, expected)
                for item, expected in zip(existing, plan.items, strict=True)
            )
        ):
            raise MegaSetDeliveryContractError("MEGA set transfer plan changed")
    else:
        if (
            delivery.total_byte_size is not None
            or delivery.source_manifest_json is not None
            or delivery.planned_at is not None
        ):
            raise MegaSetDeliveryContractError("MEGA set transfer plan is incomplete")
        for expected in plan.items:
            session.add(
                MegaSetDeliveryItem(
                    delivery_id=delivery.id,
                    ordinal=expected.ordinal,
                    source_derivative_output_id=expected.source_derivative_output_id,
                    source_sha256=expected.source_sha256,
                    source_byte_size=expected.source_byte_size,
                    source_content_type=expected.source_content_type,
                    remote_path=expected.remote_path,
                    state=MegaDeliveryState.PENDING,
                    attempts=0,
                    available_at=now,
                    remote_node_handle=None,
                    uploaded_at=None,
                    verified_at=None,
                    completed_at=None,
                    last_error_code=None,
                    last_error_detail=None,
                    created_at=now,
                    updated_at=now,
                )
            )
        delivery.total_byte_size = plan.total_byte_size
        delivery.source_manifest_json = plan.source_bytes.decode("utf-8")
        delivery.planned_at = now
    delivery.lease_expires_at = now + timedelta(seconds=lease_seconds)
    delivery.updated_at = now
    try:
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        raise MegaSetDeliveryContractError("MEGA set transfer plan conflicts") from error


async def _load_items(
    session: AsyncSession,
    *,
    claim: ClaimedMegaSetDelivery,
) -> tuple[MegaSetDeliveryItem, ...]:
    items = tuple(
        (
            await session.scalars(
                select(MegaSetDeliveryItem)
                .where(MegaSetDeliveryItem.delivery_id == claim.delivery_id)
                .order_by(MegaSetDeliveryItem.ordinal)
            )
        ).all()
    )
    if len(items) != claim.total_item_count:
        raise MegaSetDeliveryContractError("MEGA set item plan is incomplete")
    return items


async def _verify_remote_items(
    client: MegaSetDeliveryClient,
    *,
    items: tuple[MegaSetDeliveryItem, ...],
) -> tuple[tuple[MegaSetDeliveryItem, MegaRemoteNode], ...]:
    verified: list[tuple[MegaSetDeliveryItem, MegaRemoteNode]] = []
    with tempfile.TemporaryDirectory(prefix="gen-automation-mega-adopt-") as temporary:
        root = Path(temporary)
        os.chmod(root, 0o700)
        for index, item in enumerate(items):
            nodes = await client.find_file(item.remote_path)
            if len(nodes) != 1:
                raise MegaRemoteConflictError("MEGA set item path is ambiguous")
            destination = root / f"item-{index:03d}"
            destination.mkdir(mode=0o700)
            downloaded = await client.download_node(nodes[0], destination)
            byte_size, sha256 = await asyncio.to_thread(_file_identity, downloaded)
            if byte_size != item.source_byte_size or sha256 != item.source_sha256:
                raise MegaRemoteConflictError("MEGA set item bytes conflict with the manifest")
            verified.append((item, nodes[0]))
    return tuple(verified)


async def _record_item_attempts(
    session: AsyncSession,
    *,
    claim: ClaimedMegaSetDelivery,
    worker_id: str,
    item_ids: tuple[UUID, ...],
    lease_seconds: int,
    now: datetime,
) -> None:
    delivery = await _locked_owned_delivery(
        session,
        claim=claim,
        worker_id=worker_id,
        now=now,
    )
    items = tuple(
        (
            await session.scalars(
                select(MegaSetDeliveryItem)
                .where(
                    MegaSetDeliveryItem.delivery_id == delivery.id,
                    MegaSetDeliveryItem.id.in_(item_ids),
                )
                .with_for_update()
            )
        ).all()
    )
    if len(items) != len(item_ids) or any(
        item.state == MegaDeliveryState.SUCCEEDED for item in items
    ):
        raise MegaSetDeliveryContractError("MEGA set upload batch is no longer pending")
    for item in items:
        item.attempts += 1
        item.state = MegaDeliveryState.PENDING
        item.available_at = now
        item.last_error_code = None
        item.last_error_detail = None
        item.updated_at = now
    delivery.lease_expires_at = now + timedelta(seconds=lease_seconds)
    delivery.updated_at = now
    await session.commit()


async def _complete_items(
    session: AsyncSession,
    *,
    claim: ClaimedMegaSetDelivery,
    worker_id: str,
    completed: tuple[tuple[MegaSetDeliveryItem, MegaRemoteNode | None], ...],
    lease_seconds: int,
    now: datetime,
) -> None:
    delivery = await _locked_owned_delivery(
        session,
        claim=claim,
        worker_id=worker_id,
        now=now,
    )
    item_ids = tuple(item.id for item, _node in completed)
    items = tuple(
        (
            await session.scalars(
                select(MegaSetDeliveryItem)
                .where(
                    MegaSetDeliveryItem.delivery_id == delivery.id,
                    MegaSetDeliveryItem.id.in_(item_ids),
                )
                .with_for_update()
            )
        ).all()
    )
    by_id = {item.id: item for item in items}
    if len(by_id) != len(completed):
        raise MegaSetDeliveryContractError("MEGA set completion batch changed")
    for snapshot, node in completed:
        item = by_id[snapshot.id]
        if (
            item.ordinal != snapshot.ordinal
            or item.source_sha256 != snapshot.source_sha256
            or item.source_byte_size != snapshot.source_byte_size
            or item.remote_path != snapshot.remote_path
        ):
            raise MegaSetDeliveryContractError("MEGA set completion identity changed")
        item.state = MegaDeliveryState.SUCCEEDED
        item.remote_node_handle = node.handle if node is not None else None
        item.uploaded_at = now
        item.verified_at = now if node is not None else None
        item.completed_at = now
        item.last_error_code = None
        item.last_error_detail = None
        item.updated_at = now
    await session.flush()
    count, byte_size = (
        await session.execute(
            select(
                func.count(MegaSetDeliveryItem.id),
                func.coalesce(func.sum(MegaSetDeliveryItem.source_byte_size), 0),
            ).where(
                MegaSetDeliveryItem.delivery_id == delivery.id,
                MegaSetDeliveryItem.state == MegaDeliveryState.SUCCEEDED,
            )
        )
    ).one()
    delivery.uploaded_item_count = int(count)
    delivery.uploaded_byte_size = int(byte_size)
    delivery.lease_expires_at = now + timedelta(seconds=lease_seconds)
    delivery.updated_at = now
    await session.commit()


async def _remaining_item_count(
    session: AsyncSession,
    *,
    claim: ClaimedMegaSetDelivery,
) -> int:
    return int(
        await session.scalar(
            select(func.count(MegaSetDeliveryItem.id)).where(
                MegaSetDeliveryItem.delivery_id == claim.delivery_id,
                MegaSetDeliveryItem.state != MegaDeliveryState.SUCCEEDED,
            )
        )
        or 0
    )


async def _release_claim(
    session: AsyncSession,
    *,
    claim: ClaimedMegaSetDelivery,
    worker_id: str,
    now: datetime,
) -> None:
    delivery = await _locked_owned_delivery(
        session,
        claim=claim,
        worker_id=worker_id,
        now=now,
    )
    delivery.state = MegaDeliveryState.PENDING
    delivery.available_at = now
    delivery.lease_owner = None
    delivery.lease_expires_at = None
    delivery.updated_at = now
    await session.commit()


async def _finalize_remote_folder(
    sessions: async_sessionmaker[AsyncSession],
    *,
    claim: ClaimedMegaSetDelivery,
    worker_id: str,
    client: MegaSetDeliveryClient,
    plan: _ManifestPlan,
    now: datetime,
) -> bool:
    remote_manifest = _remote_manifest_bytes(plan, claim=claim)
    manifest_node = await _ensure_remote_control_file(
        client,
        remote_folder=claim.remote_folder,
        filename=_REMOTE_MANIFEST_FILENAME,
        body=remote_manifest,
    )
    marker = canonical_json_bytes(
        {
            "schema": _COMPLETION_SCHEMA,
            "delivery_id": str(claim.delivery_id),
            "finished_set_archive_id": str(claim.archive_id),
            "source_manifest_sha256": claim.manifest_sha256,
            "remote_manifest_sha256": hashlib.sha256(remote_manifest).hexdigest(),
            "image_count": claim.total_item_count,
            "total_byte_size": plan.total_byte_size,
            "manifest_node_handle": manifest_node.handle,
        }
    )
    marker_node = await _ensure_remote_control_file(
        client,
        remote_folder=claim.remote_folder,
        filename=_COMPLETION_FILENAME,
        body=marker,
    )
    completed_at = now
    async with sessions() as session:
        delivery = await _locked_owned_delivery(
            session,
            claim=claim,
            worker_id=worker_id,
            now=completed_at,
        )
        if (
            delivery.total_byte_size != plan.total_byte_size
            or delivery.uploaded_item_count != delivery.total_item_count
            or delivery.uploaded_byte_size != delivery.total_byte_size
        ):
            raise MegaSetDeliveryContractError("MEGA set cannot complete before every image")
        delivery.state = MegaDeliveryState.SUCCEEDED
        delivery.lease_owner = None
        delivery.lease_expires_at = None
        delivery.completion_marker_node_handle = marker_node.handle
        delivery.verified_at = completed_at
        delivery.completed_at = completed_at
        delivery.last_error_code = None
        delivery.last_error_detail = None
        delivery.updated_at = completed_at
        await session.commit()
    return True


async def _ensure_remote_control_file(
    client: MegaSetDeliveryClient,
    *,
    remote_folder: str,
    filename: str,
    body: bytes,
) -> MegaRemoteNode:
    remote_path = _remote_child(remote_folder, filename)
    nodes = await client.find_file(remote_path)
    if not nodes:
        with tempfile.TemporaryDirectory(prefix="gen-automation-mega-control-") as temporary:
            root = Path(temporary)
            os.chmod(root, 0o700)
            local_file = root / filename
            _write_private_file(local_file, body)
            await client.upload_file(local_file, remote_folder)
        nodes = await client.find_file(remote_path)
    if len(nodes) != 1:
        raise MegaRemoteConflictError("MEGA control-file path is ambiguous")
    with tempfile.TemporaryDirectory(prefix="gen-automation-mega-control-verify-") as temporary:
        root = Path(temporary)
        os.chmod(root, 0o700)
        downloaded = await client.download_node(nodes[0], root)
        existing = await asyncio.to_thread(downloaded.read_bytes)
    if existing != body:
        raise MegaRemoteConflictError("MEGA control-file bytes conflict with the delivery")
    return nodes[0]


async def _locked_owned_delivery(
    session: AsyncSession,
    *,
    claim: ClaimedMegaSetDelivery,
    worker_id: str,
    now: datetime,
) -> MegaSetDelivery:
    delivery = await session.scalar(
        select(MegaSetDelivery).where(MegaSetDelivery.id == claim.delivery_id).with_for_update()
    )
    if (
        delivery is None
        or delivery.state != MegaDeliveryState.CLAIMED
        or delivery.lease_owner != _worker_id(worker_id)
        or delivery.lease_expires_at is None
        or _as_utc(delivery.lease_expires_at) <= now
        or delivery.attempts != claim.attempt
        or delivery.finished_set_archive_id != claim.archive_id
        or delivery.manifest_sha256 != claim.manifest_sha256
        or delivery.remote_folder != claim.remote_folder
    ):
        await session.rollback()
        raise MegaSetDeliveryLeaseLostError("MEGA set delivery lease is no longer current")
    return delivery


async def _read_source_part(
    store: ObjectStore,
    *,
    part: _SourcePart,
    max_part_bytes: int,
) -> bytes:
    if part.byte_size > max_part_bytes:
        raise MegaSetDeliveryContractError("finished-set archive part exceeds its byte limit")
    try:
        body = await store.read_bytes(
            part.object_key,
            version_id=part.object_version_id,
            max_bytes=max_part_bytes,
        )
    except ObjectStoreError:
        raise
    if len(body) != part.byte_size or hashlib.sha256(body).hexdigest() != part.sha256:
        raise MegaSetDeliveryContractError("finished-set archive part bytes changed")
    return body


def _stage_items(
    body: bytes,
    part: _SourcePart,
    plan: _ManifestPlan,
    items: tuple[MegaSetDeliveryItem, ...],
    root: Path,
) -> tuple[_StagedItem, ...]:
    try:
        with ZipFile(BytesIO(body), mode="r") as archive:
            _validate_zip_contract(archive, max_part_bytes=len(body))
            source_manifest = _read_small_entry(archive, _REMOTE_MANIFEST_FILENAME)
            part_manifest_bytes = _read_small_entry(archive, "part-manifest.json")
            if source_manifest != plan.source_bytes:
                raise MegaSetDeliveryContractError("archive parts do not share one manifest")
            part_manifest: object = json.loads(part_manifest_bytes)
            _validate_part_manifest(part_manifest, part=part, plan=plan)
            by_ordinal = {expected.ordinal: expected for expected in plan.items}
            staged: list[_StagedItem] = []
            for item in items:
                expected = by_ordinal.get(item.ordinal)
                if expected is None or not _item_matches(item, expected):
                    raise MegaSetDeliveryContractError("MEGA set item changed before staging")
                info = archive.getinfo(expected.source_path)
                _validate_image_info(info, expected=expected)
                destination = root / expected.remote_filename
                digest = hashlib.sha256()
                byte_size = 0
                descriptor = os.open(
                    destination,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                    0o600,
                )
                try:
                    with archive.open(info, mode="r") as source_handle:
                        while chunk := source_handle.read(_HASH_BUFFER_BYTES):
                            byte_size += len(chunk)
                            digest.update(chunk)
                            view = memoryview(chunk)
                            while view:
                                written = os.write(descriptor, view)
                                view = view[written:]
                finally:
                    os.close(descriptor)
                if (
                    byte_size != expected.source_byte_size
                    or digest.hexdigest() != expected.source_sha256
                ):
                    raise MegaSetDeliveryContractError("finished-set image bytes changed")
                try:
                    require_metadata_free_image(
                        destination.read_bytes(),
                        content_type=expected.source_content_type,
                    )
                except OutboundImagePrivacyError as error:
                    raise MegaSetDeliveryContractError(
                        "finished-set image contains embedded metadata"
                    ) from error
                staged.append(_StagedItem(item_id=item.id, path=destination))
            return tuple(staged)
    except (BadZipFile, KeyError, RuntimeError, ValueError, json.JSONDecodeError) as error:
        if isinstance(error, MegaSetDeliveryContractError):
            raise
        raise MegaSetDeliveryContractError("finished-set archive part is invalid") from error


def _validate_zip_contract(archive: ZipFile, *, max_part_bytes: int) -> None:
    infos = archive.infolist()
    names = [info.filename for info in infos]
    if (
        not infos
        or len(names) != len(set(names))
        or _REMOTE_MANIFEST_FILENAME not in names
        or "part-manifest.json" not in names
    ):
        raise MegaSetDeliveryContractError("finished-set archive entries are incomplete")
    for info in infos:
        if (
            info.is_dir()
            or info.flag_bits & 0x1
            or info.compress_type != ZIP_STORED
            or info.file_size < 0
            or info.file_size > max_part_bytes
            or info.compress_size != info.file_size
            or info.filename.startswith("/")
            or "\\" in info.filename
            or any(part in {"", ".", ".."} for part in PurePosixPath(info.filename).parts)
        ):
            raise MegaSetDeliveryContractError("finished-set archive entry is unsafe")
        file_mode = info.external_attr >> 16
        if file_mode and (file_mode & 0o170000) not in {0, 0o100000}:
            raise MegaSetDeliveryContractError("finished-set archive entry is not a file")


def _read_small_entry(archive: ZipFile, name: str) -> bytes:
    info = archive.getinfo(name)
    if info.file_size > _MAX_MANIFEST_BYTES:
        raise MegaSetDeliveryContractError("finished-set manifest exceeds its byte limit")
    body = archive.read(info)
    if len(body) != info.file_size:
        raise MegaSetDeliveryContractError("finished-set manifest bytes are incomplete")
    return body


def _validate_part_manifest(
    payload: object,
    *,
    part: _SourcePart,
    plan: _ManifestPlan,
) -> None:
    if not isinstance(payload, dict):
        raise MegaSetDeliveryContractError("finished-set part manifest is invalid")
    expected_outputs = plan.source_payload["outputs"][part.first_ordinal - 1 : part.last_ordinal]
    if (
        payload.get("schema") != _SOURCE_PART_SCHEMA
        or payload.get("archive_id") != str(plan.source_payload["archive_id"])
        or payload.get("set_manifest_sha256") != hashlib.sha256(plan.source_bytes).hexdigest()
        or payload.get("part_number") != part.part_number
        or payload.get("part_count") != part.part_count
        or payload.get("first_ordinal") != part.first_ordinal
        or payload.get("last_ordinal") != part.last_ordinal
        or payload.get("outputs") != expected_outputs
    ):
        raise MegaSetDeliveryContractError("finished-set part manifest identity is invalid")


def _validate_image_info(info: ZipInfo, *, expected: _ManifestItem) -> None:
    if (
        info.filename != expected.source_path
        or info.file_size != expected.source_byte_size
        or info.compress_size != expected.source_byte_size
        or info.compress_type != ZIP_STORED
        or info.flag_bits & 0x1
        or info.is_dir()
    ):
        raise MegaSetDeliveryContractError("finished-set image entry is invalid")


def _remote_manifest_bytes(
    plan: _ManifestPlan,
    *,
    claim: ClaimedMegaSetDelivery,
) -> bytes:
    payload = dict(plan.source_payload)
    raw_outputs = payload.get("outputs")
    if not isinstance(raw_outputs, list):
        raise MegaSetDeliveryContractError("finished-set manifest outputs are invalid")
    outputs: list[dict[str, Any]] = []
    for raw in raw_outputs:
        if not isinstance(raw, dict) or not isinstance(raw.get("path"), str):
            raise MegaSetDeliveryContractError("finished-set manifest output is invalid")
        output = dict(raw)
        output["source_archive_path"] = output["path"]
        output["path"] = PurePosixPath(output["path"]).name
        outputs.append(output)
    payload["schema"] = _REMOTE_MANIFEST_SCHEMA
    payload["source_manifest_schema"] = _SOURCE_MANIFEST_SCHEMA
    payload["source_manifest_sha256"] = claim.manifest_sha256
    payload["delivery_id"] = str(claim.delivery_id)
    payload["remote_layout"] = "flat_generation_queue"
    payload["outputs"] = outputs
    return canonical_json_bytes(payload)


def _item_matches(item: MegaSetDeliveryItem, expected: _ManifestItem) -> bool:
    return (
        item.ordinal == expected.ordinal
        and item.source_derivative_output_id == expected.source_derivative_output_id
        and item.source_sha256 == expected.source_sha256
        and item.source_byte_size == expected.source_byte_size
        and item.source_content_type == expected.source_content_type
        and item.remote_path == expected.remote_path
    )


def _part_for_ordinal(
    parts: tuple[_SourcePart, ...],
    ordinal: int,
) -> _SourcePart:
    matching = tuple(part for part in parts if part.first_ordinal <= ordinal <= part.last_ordinal)
    if len(matching) != 1:
        raise MegaSetDeliveryContractError("finished-set ordinal has no unique source part")
    return matching[0]


def _remote_folder(
    remote_root: str,
    *,
    set_name: str,
) -> str:
    normalized_name = set_name.strip()
    if (
        not normalized_name
        or len(normalized_name) > 300
        or normalized_name in {".", ".."}
        or any(
            character in normalized_name for character in ("/", "\\", "*", "?", "\0", "\r", "\n")
        )
    ):
        raise MegaSetDeliveryContractError("MEGA set name is not a safe folder component")
    parts = [
        part
        for part in (
            remote_root.strip("/"),
            normalized_name,
        )
        if part
    ]
    return validate_remote_path("/" + "/".join(parts), allow_root=False)


def _remote_child(remote_folder: str, filename: str) -> str:
    validate_remote_filename(filename)
    return validate_remote_path(f"{remote_folder}/{filename}", allow_root=False)


def _write_private_file(path: Path, body: bytes) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
        0o600,
    )
    try:
        view = memoryview(body)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
    finally:
        os.close(descriptor)


def _file_identity(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    byte_size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(_HASH_BUFFER_BYTES):
            byte_size += len(chunk)
            digest.update(chunk)
    return byte_size, digest.hexdigest()


async def _mark_claim_retry_after_cancel(
    sessions: async_sessionmaker[AsyncSession],
    *,
    claim: ClaimedMegaSetDelivery,
    worker_id: str,
    retry_delay_seconds: int,
) -> None:
    try:
        async with sessions() as session:
            await fail_mega_set_delivery(
                session,
                claim=claim,
                worker_id=worker_id,
                error_code="mega_set_cancelled_retry",
                retry_delay_seconds=retry_delay_seconds,
                terminal=False,
            )
    except MegaSetDeliveryLeaseLostError:
        return


def _failure_code(error: BaseException) -> str:
    if isinstance(error, MegaRemoteConflictError):
        return "mega_set_remote_conflict"
    if isinstance(error, MegaSetDeliveryContractError):
        return "mega_set_contract"
    if isinstance(error, ObjectStoreError):
        return "mega_set_source_storage"
    if isinstance(error, BadZipFile):
        return "mega_set_archive_invalid"
    if isinstance(error, json.JSONDecodeError):
        return "mega_set_manifest_invalid"
    if isinstance(error, MegaError):
        return "mega_set_transport_retryable"
    return "mega_set_runtime_retryable"


def _worker_id(value: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > 200 or "\r" in normalized or "\n" in normalized:
        raise ValueError("MEGA worker ID is invalid")
    return normalized


def _error_code(value: str) -> str:
    normalized = value.strip().lower()
    if (
        not normalized
        or len(normalized) > 100
        or not all(character.isalnum() or character in "._-" for character in normalized)
    ):
        raise ValueError("MEGA error code is invalid")
    return normalized


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
