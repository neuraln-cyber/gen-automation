"""Restart-safe delivery of immutable, clean Patreon packages to MEGA."""

from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Protocol
from uuid import UUID

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gen_automation.db.models import (
    MegaDelivery,
    Project,
    PublicationIntent,
    PublicationPackage,
    Release,
)
from gen_automation.domain.enums import MegaDeliveryState
from gen_automation.integrations.mega import (
    MegaConfigurationError,
    MegaError,
    MegaProtocolError,
    MegaRemoteConflictError,
    MegaRemoteNode,
    MegaRetryableError,
)
from gen_automation.integrations.mega.client import validate_remote_path
from gen_automation.storage.base import ObjectStore, ObjectStoreError

_SAFE_FAILURE_DETAIL = "MEGA completed-set delivery failed inside the isolated uploader."
_SHA256_BUFFER_BYTES = 1024 * 1024


class MegaDeliveryError(Exception):
    """Base error for durable completed-set delivery."""


class MegaDeliveryLeaseLostError(MegaDeliveryError):
    """The controller no longer owns the active MEGA delivery lease."""


class MegaDeliveryContractError(MegaDeliveryError):
    """Frozen package or delivery data violates the runtime contract."""


class MegaDeliveryClient(Protocol):
    async def ensure_folder(self, remote_folder: str) -> None: ...

    async def find_file(self, remote_path: str) -> tuple[MegaRemoteNode, ...]: ...

    async def upload_file(self, local_file: Path, remote_folder: str) -> None: ...

    async def download_node(self, node: MegaRemoteNode, local_folder: Path) -> Path: ...


@dataclass(frozen=True, slots=True)
class ClaimedMegaDelivery:
    delivery_id: UUID
    package_id: UUID
    storage_backend: str
    storage_bucket: str
    object_key: str
    object_version_id: str
    sha256: str
    byte_size: int
    remote_root: str
    remote_path: str
    attempt: int
    lease_expires_at: datetime


@dataclass(frozen=True, slots=True)
class MegaDeliveryReference:
    delivery_id: UUID
    package_id: UUID
    remote_path: str
    node_handle: str
    sha256: str
    byte_size: int
    verified_at: datetime


@dataclass(frozen=True, slots=True)
class MegaDeliveryCycleResult:
    created_delivery: bool = False
    processed_delivery: bool = False
    completed_delivery: bool = False

    @property
    def did_work(self) -> bool:
        return self.created_delivery or self.processed_delivery or self.completed_delivery


async def ensure_next_mega_delivery(
    session: AsyncSession,
    *,
    remote_root: str,
    now: datetime | None = None,
) -> bool:
    """Create one delivery for an immutable Patreon package not yet mirrored."""

    normalized_root = validate_remote_path(remote_root, allow_root=True)
    created_at = _as_utc(now or datetime.now(UTC))
    delivery_exists = exists(
        select(MegaDelivery.id).where(MegaDelivery.publication_package_id == PublicationPackage.id)
    )
    candidate = (
        await session.execute(
            select(PublicationPackage, Project.slug, Release.slug)
            .join(
                PublicationIntent,
                PublicationIntent.id == PublicationPackage.intent_id,
            )
            .join(Release, Release.id == PublicationIntent.release_id)
            .join(Project, Project.id == Release.project_id)
            .where(~delivery_exists)
            .order_by(PublicationPackage.created_at, PublicationPackage.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
    ).one_or_none()
    if candidate is None:
        await session.rollback()
        return False
    package, project_slug, release_slug = candidate
    remote_path = _package_remote_path(
        normalized_root,
        project_slug=project_slug,
        release_slug=release_slug,
        package_id=package.id,
        sha256=package.sha256,
    )
    delivery = MegaDelivery(
        publication_package_id=package.id,
        state=MegaDeliveryState.PENDING,
        remote_root=normalized_root,
        remote_path=remote_path,
        sha256=package.sha256,
        byte_size=package.byte_size,
        attempts=0,
        available_at=created_at,
        lease_owner=None,
        lease_expires_at=None,
        remote_node_handle=None,
        verified_at=None,
        completed_at=None,
        last_error_code=None,
        last_error_detail=None,
        created_at=created_at,
        updated_at=created_at,
    )
    session.add(delivery)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        return False
    return True


async def claim_mega_delivery(
    session: AsyncSession,
    *,
    worker_id: str,
    lease_seconds: int,
    now: datetime | None = None,
) -> ClaimedMegaDelivery | None:
    normalized_worker = _worker_id(worker_id)
    if isinstance(lease_seconds, bool) or not 30 <= lease_seconds <= 7200:
        raise ValueError("MEGA lease_seconds must be between 30 and 7200")
    claimed_at = _as_utc(now or datetime.now(UTC))

    due_state = or_(
        and_(
            MegaDelivery.state == MegaDeliveryState.PENDING,
            MegaDelivery.available_at <= claimed_at,
        ),
        and_(
            MegaDelivery.state == MegaDeliveryState.RETRY_WAIT,
            MegaDelivery.available_at <= claimed_at,
        ),
        and_(
            MegaDelivery.state == MegaDeliveryState.CLAIMED,
            MegaDelivery.lease_expires_at.is_not(None),
            MegaDelivery.lease_expires_at <= claimed_at,
        ),
    )
    row = (
        await session.execute(
            select(MegaDelivery, PublicationPackage)
            .join(
                PublicationPackage,
                PublicationPackage.id == MegaDelivery.publication_package_id,
            )
            .where(due_state)
            .order_by(
                MegaDelivery.available_at,
                MegaDelivery.created_at,
                MegaDelivery.id,
            )
            .limit(1)
            .with_for_update(skip_locked=True)
        )
    ).one_or_none()
    if row is None:
        await session.rollback()
        return None
    delivery, package = row
    _validate_package_snapshot(delivery, package)
    delivery.state = MegaDeliveryState.CLAIMED
    delivery.attempts += 1
    delivery.lease_owner = normalized_worker
    delivery.lease_expires_at = claimed_at + timedelta(seconds=lease_seconds)
    delivery.completed_at = None
    delivery.last_error_code = None
    delivery.last_error_detail = None
    delivery.updated_at = claimed_at
    claim = ClaimedMegaDelivery(
        delivery_id=delivery.id,
        package_id=package.id,
        storage_backend=package.storage_backend,
        storage_bucket=package.storage_bucket,
        object_key=package.object_key,
        object_version_id=package.object_version_id,
        sha256=delivery.sha256,
        byte_size=delivery.byte_size,
        remote_root=delivery.remote_root,
        remote_path=delivery.remote_path,
        attempt=delivery.attempts,
        lease_expires_at=delivery.lease_expires_at,
    )
    await session.commit()
    return claim


async def deliver_claimed_package(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    claim: ClaimedMegaDelivery,
    worker_id: str,
    store: ObjectStore,
    client: MegaDeliveryClient,
    max_package_bytes: int,
    now: datetime | None = None,
) -> MegaDeliveryReference:
    """Upload or adopt, then verify remote bytes before recording success."""

    if not 1 <= max_package_bytes <= 512 * 1024 * 1024:
        raise ValueError("MEGA package byte limit is invalid")
    if claim.storage_backend != store.backend or claim.storage_bucket != store.bucket:
        raise MegaDeliveryContractError("MEGA delivery package belongs to a different object store")
    if claim.byte_size > max_package_bytes:
        raise MegaDeliveryContractError("MEGA delivery package exceeds its byte limit")
    try:
        package_bytes = await store.read_bytes(
            claim.object_key,
            max_bytes=max_package_bytes,
            version_id=claim.object_version_id,
        )
    except ObjectStoreError as error:
        raise MegaRetryableError("MEGA source package could not be read") from error
    if (
        len(package_bytes) != claim.byte_size
        or hashlib.sha256(package_bytes).hexdigest() != claim.sha256
    ):
        raise MegaDeliveryContractError("MEGA source package does not match its frozen identity")

    with tempfile.TemporaryDirectory(prefix="gen-automation-mega-") as temporary:
        root = Path(temporary)
        os.chmod(root, 0o700)
        package_path = root / PurePosixPath(claim.remote_path).name
        _write_private_file(package_path, package_bytes)
        del package_bytes

        remote_folder = str(PurePosixPath(claim.remote_path).parent)
        await client.ensure_folder(remote_folder)
        node = await _find_verified_remote(
            client,
            remote_path=claim.remote_path,
            expected_sha256=claim.sha256,
            expected_byte_size=claim.byte_size,
            temporary_root=root,
        )
        if node is None:
            await client.upload_file(package_path, remote_folder)
            node = await _find_verified_remote(
                client,
                remote_path=claim.remote_path,
                expected_sha256=claim.sha256,
                expected_byte_size=claim.byte_size,
                temporary_root=root,
            )
            if node is None:
                raise MegaRetryableError(
                    "MEGAcmd upload completed without a discoverable remote node"
                )

    verified_at = _as_utc(now or datetime.now(UTC))
    async with session_factory() as session:
        delivery = await _locked_owned_delivery(
            session,
            claim=claim,
            worker_id=worker_id,
            now=verified_at,
        )
        delivery.state = MegaDeliveryState.SUCCEEDED
        delivery.lease_owner = None
        delivery.lease_expires_at = None
        delivery.remote_node_handle = node.handle
        delivery.verified_at = verified_at
        delivery.completed_at = verified_at
        delivery.last_error_code = None
        delivery.last_error_detail = None
        delivery.updated_at = verified_at
        await session.commit()
    return MegaDeliveryReference(
        delivery_id=claim.delivery_id,
        package_id=claim.package_id,
        remote_path=claim.remote_path,
        node_handle=node.handle,
        sha256=claim.sha256,
        byte_size=claim.byte_size,
        verified_at=verified_at,
    )


async def fail_mega_delivery(
    session: AsyncSession,
    *,
    claim: ClaimedMegaDelivery,
    worker_id: str,
    error_code: str,
    retry_delay_seconds: int,
    terminal: bool,
    now: datetime | None = None,
) -> None:
    failed_at = _as_utc(now or datetime.now(UTC))
    if isinstance(retry_delay_seconds, bool) or not 1 <= retry_delay_seconds <= 7 * 86400:
        raise ValueError("MEGA retry delay must be between 1 second and 7 days")
    normalized_code = _error_code(error_code)
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
    delivery.remote_node_handle = None
    delivery.verified_at = None
    delivery.completed_at = failed_at if terminal else None
    delivery.last_error_code = normalized_code
    delivery.last_error_detail = _SAFE_FAILURE_DETAIL
    delivery.updated_at = failed_at
    await session.commit()


async def run_mega_delivery_cycle(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    store: ObjectStore,
    client: MegaDeliveryClient,
    worker_id: str,
    remote_root: str,
    lease_seconds: int,
    retry_base_seconds: int,
    retry_max_seconds: int,
    max_package_bytes: int,
) -> MegaDeliveryCycleResult:
    """Perform at most one durable discovery or delivery action."""

    if (
        isinstance(retry_base_seconds, bool)
        or isinstance(retry_max_seconds, bool)
        or not 1 <= retry_base_seconds <= retry_max_seconds <= 7 * 86400
    ):
        raise ValueError("MEGA retry bounds are invalid")
    async with session_factory() as session:
        created = await ensure_next_mega_delivery(
            session,
            remote_root=remote_root,
        )
    if created:
        return MegaDeliveryCycleResult(created_delivery=True)

    async with session_factory() as session:
        claim = await claim_mega_delivery(
            session,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
        )
    if claim is None:
        return MegaDeliveryCycleResult()

    try:
        await deliver_claimed_package(
            session_factory,
            claim=claim,
            worker_id=worker_id,
            store=store,
            client=client,
            max_package_bytes=max_package_bytes,
        )
    except asyncio.CancelledError:
        raise
    except (MegaRemoteConflictError, MegaDeliveryContractError) as error:
        async with session_factory() as session:
            await fail_mega_delivery(
                session,
                claim=claim,
                worker_id=worker_id,
                error_code=_failure_code(error),
                retry_delay_seconds=retry_base_seconds,
                terminal=True,
            )
        return MegaDeliveryCycleResult(processed_delivery=True)
    except (MegaError, ObjectStoreError, OSError) as error:
        retry_delay = min(
            retry_max_seconds,
            retry_base_seconds * (2 ** min(max(claim.attempt - 1, 0), 16)),
        )
        async with session_factory() as session:
            await fail_mega_delivery(
                session,
                claim=claim,
                worker_id=worker_id,
                error_code=_failure_code(error),
                retry_delay_seconds=retry_delay,
                terminal=False,
            )
        return MegaDeliveryCycleResult(processed_delivery=True)
    return MegaDeliveryCycleResult(
        processed_delivery=True,
        completed_delivery=True,
    )


async def _find_verified_remote(
    client: MegaDeliveryClient,
    *,
    remote_path: str,
    expected_sha256: str,
    expected_byte_size: int,
    temporary_root: Path,
) -> MegaRemoteNode | None:
    candidates = await client.find_file(remote_path)
    if not candidates:
        return None
    verified: list[MegaRemoteNode] = []
    for index, candidate in enumerate(candidates):
        verification_folder = temporary_root / f"verify-{index}"
        verification_folder.mkdir(mode=0o700)
        downloaded = await client.download_node(candidate, verification_folder)
        byte_size, sha256 = await asyncio.to_thread(_file_identity, downloaded)
        if byte_size == expected_byte_size and sha256 == expected_sha256:
            verified.append(candidate)
    if len(candidates) != 1 or len(verified) != 1:
        raise MegaRemoteConflictError(
            "MEGA content-addressed path contains conflicting remote bytes"
        )
    return verified[0]


async def _locked_owned_delivery(
    session: AsyncSession,
    *,
    claim: ClaimedMegaDelivery,
    worker_id: str,
    now: datetime,
) -> MegaDelivery:
    delivery = await session.scalar(
        select(MegaDelivery).where(MegaDelivery.id == claim.delivery_id).with_for_update()
    )
    if (
        delivery is None
        or delivery.state != MegaDeliveryState.CLAIMED
        or delivery.lease_owner != _worker_id(worker_id)
        or delivery.lease_expires_at is None
        or _as_utc(delivery.lease_expires_at) <= now
        or delivery.attempts != claim.attempt
        or delivery.publication_package_id != claim.package_id
        or delivery.remote_path != claim.remote_path
        or delivery.sha256 != claim.sha256
        or delivery.byte_size != claim.byte_size
    ):
        await session.rollback()
        raise MegaDeliveryLeaseLostError("MEGA delivery lease is no longer current")
    return delivery


def _validate_package_snapshot(
    delivery: MegaDelivery,
    package: PublicationPackage,
) -> None:
    if (
        delivery.publication_package_id != package.id
        or delivery.sha256 != package.sha256
        or delivery.byte_size != package.byte_size
        or package.content_type != "application/zip"
        or len(package.object_version_id.strip()) == 0
    ):
        raise MegaDeliveryContractError(
            "MEGA delivery does not match its immutable Patreon package"
        )
    validate_remote_path(delivery.remote_root, allow_root=True)
    validate_remote_path(delivery.remote_path, allow_root=False)


def _package_remote_path(
    remote_root: str,
    *,
    project_slug: str,
    release_slug: str,
    package_id: UUID,
    sha256: str,
) -> str:
    if len(sha256) != 64:
        raise MegaDeliveryContractError("Patreon package checksum is invalid")
    try:
        int(sha256, 16)
    except ValueError:
        raise MegaDeliveryContractError("Patreon package checksum is invalid") from None
    parts = [
        part
        for part in (
            remote_root.strip("/"),
            project_slug,
            release_slug,
            str(package_id),
            f"{sha256}.zip",
        )
        if part
    ]
    return validate_remote_path("/" + "/".join(parts), allow_root=False)


def _write_private_file(path: Path, payload: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
    finally:
        os.close(descriptor)


def _file_identity(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    byte_size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(_SHA256_BUFFER_BYTES):
            byte_size += len(chunk)
            digest.update(chunk)
    return byte_size, digest.hexdigest()


def _failure_code(error: BaseException) -> str:
    if isinstance(error, MegaRemoteConflictError):
        return "mega_remote_conflict"
    if isinstance(error, MegaDeliveryContractError):
        return "mega_package_contract"
    if isinstance(error, MegaConfigurationError):
        return "mega_profile_unavailable"
    if isinstance(error, MegaProtocolError):
        return "mega_protocol_error"
    if isinstance(error, MegaRetryableError):
        return "mega_retryable_error"
    if isinstance(error, ObjectStoreError):
        return "mega_source_storage_error"
    return "mega_runtime_error"


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
