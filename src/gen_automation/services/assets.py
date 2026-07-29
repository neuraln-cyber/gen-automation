from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from string import hexdigits
from uuid import UUID

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from gen_automation.db.models import (
    Asset,
    AuditEvent,
    GenerationJob,
    OutboxEvent,
    ReleaseVersion,
)
from gen_automation.domain.enums import AssetKind, AssetState, OutboxStatus
from gen_automation.domain.ids import uuid7
from gen_automation.storage.base import (
    ObjectAlreadyExistsError,
    ObjectMetadata,
    ObjectNotFoundError,
    ObjectStore,
    ObjectStoreError,
    ObjectTooLargeError,
)
from gen_automation.storage.images import (
    ImageVerificationError,
    VerifiedImage,
    verify_image_bytes_isolated,
)

ALLOWED_UPLOAD_CONTENT_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})


class AssetServiceError(Exception):
    """Base error for asset workflow failures."""


class AssetNotFoundError(AssetServiceError):
    pass


class AssetConflictError(AssetServiceError):
    pass


class AssetBusyError(AssetServiceError):
    pass


class UploadNotReadyError(AssetServiceError):
    pass


class AssetStorageUnavailableError(AssetServiceError):
    pass


class AssetQuarantinedError(AssetServiceError):
    pass


@dataclass(frozen=True)
class UploadIntent:
    asset_id: UUID
    upload_attempt_id: UUID
    output_index: int
    staging_key: str
    state: AssetState
    upload_url: str | None
    upload_method: str | None
    upload_fields: dict[str, str]
    upload_headers: dict[str, str]


@dataclass(frozen=True)
class FinalizedAsset:
    asset_id: UUID
    release_id: UUID
    generation_job_id: UUID
    output_index: int
    object_key: str
    object_version_id: str | None
    sha256: str
    content_type: str
    image_format: str
    width: int
    height: int
    byte_size: int
    replayed: bool

    @classmethod
    def from_model(cls, asset: Asset, *, replayed: bool) -> "FinalizedAsset":
        generation_job_id = asset.generation_job_id
        output_index = asset.output_index
        object_key = asset.object_key
        sha256 = asset.sha256
        content_type = asset.content_type
        image_format = asset.image_format
        width = asset.width
        height = asset.height
        byte_size = asset.byte_size
        if (
            generation_job_id is None
            or output_index is None
            or object_key is None
            or sha256 is None
            or content_type is None
            or image_format is None
            or width is None
            or height is None
            or byte_size is None
        ):
            raise AssetConflictError("available asset metadata is incomplete")
        return cls(
            asset_id=asset.id,
            release_id=asset.release_id,
            generation_job_id=generation_job_id,
            output_index=output_index,
            object_key=object_key,
            object_version_id=asset.object_version_id,
            sha256=sha256,
            content_type=content_type,
            image_format=image_format,
            width=width,
            height=height,
            byte_size=byte_size,
            replayed=replayed,
        )


def _staging_key(
    *,
    release_id: UUID,
    job_id: UUID,
    asset_id: UUID,
    upload_attempt_id: UUID,
) -> str:
    return f"staging/{release_id}/{job_id}/{asset_id}/{upload_attempt_id}/output"


def _staging_metadata(
    *,
    asset_id: UUID,
    job_id: UUID,
    output_index: int,
    content_type: str,
    upload_attempt_id: str,
) -> dict[str, str]:
    return {
        "asset-id": str(asset_id),
        "generation-job-id": str(job_id),
        "output-index": str(output_index),
        "declared-content-type": content_type,
        "upload-attempt-id": upload_attempt_id,
    }


async def create_raw_master_upload_intents(
    session: AsyncSession,
    store: ObjectStore,
    *,
    generation_job_id: UUID,
    content_type: str = "image/png",
    expires_in: int = 600,
    max_bytes: int = 100 * 1024 * 1024,
    rotate_incomplete_uploads: bool = False,
    actor: str = "controller",
) -> list[UploadIntent]:
    if content_type not in ALLOWED_UPLOAD_CONTENT_TYPES:
        raise AssetConflictError("unsupported upload content type")

    row = (
        await session.execute(
            select(GenerationJob, ReleaseVersion)
            .join(
                ReleaseVersion,
                ReleaseVersion.id == GenerationJob.release_version_id,
            )
            .where(GenerationJob.id == generation_job_id)
            .with_for_update()
        )
    ).one_or_none()
    if row is None:
        raise AssetNotFoundError("generation job not found")
    job, release_version = row

    existing_assets = (
        (
            await session.scalars(
                select(Asset).where(
                    Asset.generation_job_id == job.id,
                    Asset.kind == AssetKind.RAW_MASTER,
                )
            )
        )
        .unique()
        .all()
    )
    assets_by_index = {
        asset.output_index: asset for asset in existing_assets if asset.output_index is not None
    }

    intents: list[UploadIntent] = []
    issued_asset_ids: list[str] = []
    for output_index in range(job.expected_output_count):
        asset = assets_by_index.get(output_index)
        issue_upload = False
        if asset is None:
            asset_id = uuid7()
            upload_attempt_id = uuid7()
            staging_key = _staging_key(
                release_id=release_version.release_id,
                job_id=job.id,
                asset_id=asset_id,
                upload_attempt_id=upload_attempt_id,
            )
            asset = Asset(
                id=asset_id,
                release_id=release_version.release_id,
                generation_job_id=job.id,
                output_index=output_index,
                kind=AssetKind.RAW_MASTER,
                state=AssetState.UPLOADING,
                storage_backend=store.backend,
                storage_bucket=store.bucket,
                staging_object_key=staging_key,
                asset_metadata={
                    "declared_content_type": content_type,
                    "staging_cleanup": "not_started",
                    "upload_attempt_id": str(upload_attempt_id),
                },
            )
            session.add(asset)
            issued_asset_ids.append(str(asset.id))
            issue_upload = True
        elif asset.storage_backend != store.backend or asset.storage_bucket != store.bucket:
            raise AssetConflictError("asset belongs to another object store")

        existing_content_type = asset.asset_metadata.get("declared_content_type")
        if existing_content_type != content_type:
            raise AssetConflictError("an upload intent already exists with another content type")

        if asset.state == AssetState.EXPECTED:
            upload_attempt_id = uuid7()
            asset.staging_object_key = _staging_key(
                release_id=release_version.release_id,
                job_id=job.id,
                asset_id=asset.id,
                upload_attempt_id=upload_attempt_id,
            )
            metadata = dict(asset.asset_metadata)
            metadata.update(
                {
                    "staging_cleanup": "not_started",
                    "upload_attempt_id": str(upload_attempt_id),
                }
            )
            asset.asset_metadata = metadata
            asset.state = AssetState.UPLOADING
            issue_upload = True
        elif asset.state == AssetState.UPLOADING and rotate_incomplete_uploads:
            upload_attempt_id = uuid7()
            asset.staging_object_key = _staging_key(
                release_id=release_version.release_id,
                job_id=job.id,
                asset_id=asset.id,
                upload_attempt_id=upload_attempt_id,
            )
            metadata = dict(asset.asset_metadata)
            metadata.update(
                {
                    "staging_cleanup": "not_started",
                    "upload_attempt_id": str(upload_attempt_id),
                }
            )
            asset.asset_metadata = metadata
            issued_asset_ids.append(str(asset.id))
            issue_upload = True

        if asset.staging_object_key is None or asset.output_index is None:
            raise AssetConflictError("asset upload metadata is incomplete")

        if issue_upload:
            metadata = _staging_metadata(
                asset_id=asset.id,
                job_id=job.id,
                output_index=asset.output_index,
                content_type=content_type,
                upload_attempt_id=str(asset.asset_metadata["upload_attempt_id"]),
            )
            upload_url = await store.presign_upload(
                key=asset.staging_object_key,
                content_type=content_type,
                metadata=metadata,
                expires_in=expires_in,
                max_bytes=max_bytes,
            )
            upload_method = upload_url.method
            upload_fields = upload_url.fields
            upload_headers = upload_url.headers
            signed_url = upload_url.url
        else:
            signed_url = None
            upload_method = None
            upload_fields = {}
            upload_headers = {}

        intents.append(
            UploadIntent(
                asset_id=asset.id,
                upload_attempt_id=UUID(str(asset.asset_metadata["upload_attempt_id"])),
                output_index=asset.output_index,
                staging_key=asset.staging_object_key,
                state=asset.state,
                upload_url=signed_url,
                upload_method=upload_method,
                upload_fields=upload_fields,
                upload_headers=upload_headers,
            )
        )

    if issued_asset_ids:
        now = datetime.now(UTC)
        session.add(
            AuditEvent(
                actor=actor,
                action="asset.upload_intents_created",
                resource_type="generation_job",
                resource_id=job.id,
                correlation_id=str(job.id),
                detail={
                    "asset_ids": issued_asset_ids,
                    "expected_output_count": job.expected_output_count,
                },
                occurred_at=now,
            )
        )
    await session.commit()
    return intents


async def _claim_verification(
    session: AsyncSession,
    *,
    asset_id: UUID,
    lease_seconds: int,
) -> tuple[Asset, str]:
    now = datetime.now(UTC)
    lease_owner = str(uuid7())
    claimable = or_(
        Asset.state.in_([AssetState.EXPECTED, AssetState.UPLOADING]),
        and_(
            Asset.state == AssetState.VERIFYING,
            or_(
                Asset.verification_lease_expires_at.is_(None),
                Asset.verification_lease_expires_at <= now,
            ),
        ),
    )
    claimed_id = await session.scalar(
        update(Asset)
        .where(
            Asset.id == asset_id,
            Asset.kind == AssetKind.RAW_MASTER,
            claimable,
        )
        .values(
            state=AssetState.VERIFYING,
            verification_lease_owner=lease_owner,
            verification_lease_expires_at=now + timedelta(seconds=lease_seconds),
            verification_error_code=None,
            verification_error_detail=None,
        )
        .returning(Asset.id)
    )
    await session.commit()

    if claimed_id is not None:
        claimed = await session.get(Asset, claimed_id)
        if claimed is None:
            raise AssetNotFoundError("asset disappeared after verification claim")
        return claimed, lease_owner

    current = await session.get(Asset, asset_id)
    if current is None or current.kind != AssetKind.RAW_MASTER:
        raise AssetNotFoundError("raw master asset not found")
    if current.state == AssetState.AVAILABLE:
        raise _AlreadyAvailableError(current)
    if current.state == AssetState.VERIFYING:
        raise AssetBusyError("asset verification is already in progress")
    if current.state == AssetState.QUARANTINED:
        raise AssetQuarantinedError("asset is quarantined")
    raise AssetConflictError(f"asset cannot be verified from state {current.state}")


class _AlreadyAvailableError(Exception):
    def __init__(self, asset: Asset) -> None:
        self.asset = asset
        super().__init__("asset is already available")


async def _release_verification_claim(
    session: AsyncSession,
    *,
    asset: Asset,
    lease_owner: str,
    error_code: str,
    actor: str,
) -> None:
    now = datetime.now(UTC)
    released_id = await session.scalar(
        update(Asset)
        .where(
            Asset.id == asset.id,
            Asset.state == AssetState.VERIFYING,
            Asset.verification_lease_owner == lease_owner,
        )
        .values(
            state=AssetState.UPLOADING,
            verification_lease_owner=None,
            verification_lease_expires_at=None,
            verification_error_code=error_code,
            verification_error_detail="verification will be retried",
        )
        .returning(Asset.id)
    )
    if released_id is not None:
        session.add(
            AuditEvent(
                actor=actor,
                action="asset.verification_deferred",
                resource_type="asset",
                resource_id=asset.id,
                correlation_id=lease_owner,
                detail={"error_code": error_code},
                occurred_at=now,
            )
        )
    await session.commit()


async def _quarantine(
    session: AsyncSession,
    *,
    asset: Asset,
    lease_owner: str,
    error_code: str,
    actor: str,
) -> None:
    now = datetime.now(UTC)
    quarantined_id = await session.scalar(
        update(Asset)
        .where(
            Asset.id == asset.id,
            Asset.state == AssetState.VERIFYING,
            Asset.verification_lease_owner == lease_owner,
        )
        .values(
            state=AssetState.QUARANTINED,
            verification_lease_owner=None,
            verification_lease_expires_at=None,
            verification_error_code=error_code,
            verification_error_detail="staging object retained for investigation",
        )
        .returning(Asset.id)
    )
    if quarantined_id is None:
        await session.rollback()
        raise AssetBusyError("asset verification lease was lost")
    session.add(
        AuditEvent(
            actor=actor,
            action="asset.quarantined",
            resource_type="asset",
            resource_id=asset.id,
            correlation_id=lease_owner,
            detail={"error_code": error_code},
            occurred_at=now,
        )
    )
    await session.commit()


def _validate_staging_metadata(asset: Asset, staging: ObjectMetadata) -> None:
    expected = {
        "asset-id": str(asset.id),
        "generation-job-id": str(asset.generation_job_id),
        "output-index": str(asset.output_index),
        "upload-attempt-id": str(asset.asset_metadata.get("upload_attempt_id")),
    }
    if any(staging.metadata.get(key) != value for key, value in expected.items()):
        raise ImageVerificationError("staging object metadata does not match the asset")
    declared_type = staging.metadata.get("declared-content-type")
    stored_declared_type = asset.asset_metadata.get("declared_content_type")
    if declared_type != stored_declared_type or staging.content_type != declared_type:
        raise ImageVerificationError("staging object content type does not match the intent")


def _validate_reported_sha256(reported_sha256: str | None) -> str | None:
    if reported_sha256 is None:
        return None
    normalized = reported_sha256.lower()
    if len(normalized) != 64 or any(character not in hexdigits for character in normalized):
        raise AssetConflictError("reported SHA-256 is malformed")
    return normalized


def _master_key(asset: Asset, verified: VerifiedImage) -> str:
    return f"masters/{asset.release_id}/{asset.id}/{verified.sha256}.{verified.extension}"


def _master_metadata(asset: Asset, verified: VerifiedImage) -> dict[str, str]:
    return {
        "asset-id": str(asset.id),
        "generation-job-id": str(asset.generation_job_id),
        "output-index": str(asset.output_index),
        "sha256": verified.sha256,
    }


def _existing_master_matches(
    existing: ObjectMetadata,
    *,
    asset: Asset,
    verified: VerifiedImage,
) -> bool:
    expected_metadata = _master_metadata(asset, verified)
    return (
        existing.byte_size == verified.byte_size
        and existing.content_type == verified.content_type
        and all(existing.metadata.get(key) == value for key, value in expected_metadata.items())
    )


async def _promote_master(
    store: ObjectStore,
    *,
    asset: Asset,
    staging: ObjectMetadata,
    verified: VerifiedImage,
) -> tuple[str, ObjectMetadata]:
    if asset.staging_object_key is None:
        raise AssetConflictError("asset has no staging object key")
    destination_key = _master_key(asset, verified)
    metadata = _master_metadata(asset, verified)
    try:
        promoted = await store.copy_if_absent(
            source_key=asset.staging_object_key,
            destination_key=destination_key,
            content_type=verified.content_type,
            metadata=metadata,
            source_version_id=staging.version_id,
            source_etag=staging.etag,
        )
    except ObjectAlreadyExistsError:
        existing = await store.head(destination_key)
        if existing is None or not _existing_master_matches(
            existing,
            asset=asset,
            verified=verified,
        ):
            raise ImageVerificationError(
                "immutable master destination contains conflicting bytes"
            ) from None
        existing_bytes = await store.read_bytes(
            destination_key,
            max_bytes=verified.byte_size,
            version_id=existing.version_id,
            etag=existing.etag,
        )
        existing_verified = await verify_image_bytes_isolated(existing_bytes)
        if existing_verified != verified:
            raise ImageVerificationError(
                "immutable master destination contains conflicting bytes"
            ) from None
        promoted = existing

    if not _existing_master_matches(
        promoted,
        asset=asset,
        verified=verified,
    ):
        raise ImageVerificationError("promoted master metadata failed verification")
    return destination_key, promoted


async def _mark_available(
    session: AsyncSession,
    *,
    asset: Asset,
    lease_owner: str,
    destination_key: str,
    promoted: ObjectMetadata,
    verified: VerifiedImage,
    staging: ObjectMetadata,
    actor: str,
) -> Asset:
    now = datetime.now(UTC)
    metadata = dict(asset.asset_metadata)
    metadata.update(
        {
            "staging_cleanup": "pending",
            "staging_version_id": staging.version_id,
            "staging_etag": staging.etag,
        }
    )
    available_id = await session.scalar(
        update(Asset)
        .where(
            Asset.id == asset.id,
            Asset.state == AssetState.VERIFYING,
            Asset.verification_lease_owner == lease_owner,
        )
        .values(
            state=AssetState.AVAILABLE,
            object_key=destination_key,
            object_version_id=promoted.version_id,
            sha256=verified.sha256,
            content_type=verified.content_type,
            image_format=verified.image_format,
            width=verified.width,
            height=verified.height,
            byte_size=verified.byte_size,
            asset_metadata=metadata,
            verification_lease_owner=None,
            verification_lease_expires_at=None,
            verification_error_code=None,
            verification_error_detail=None,
            available_at=now,
        )
        .returning(Asset.id)
    )
    if available_id is None:
        await session.rollback()
        raise AssetBusyError("asset verification lease was lost before commit")

    session.add(
        AuditEvent(
            actor=actor,
            action="asset.raw_master_available",
            resource_type="asset",
            resource_id=asset.id,
            correlation_id=lease_owner,
            detail={
                "sha256": verified.sha256,
                "byte_size": verified.byte_size,
                "width": verified.width,
                "height": verified.height,
            },
            occurred_at=now,
        )
    )
    session.add(
        OutboxEvent(
            topic="asset.available",
            dedupe_key=f"asset.available:{asset.id}",
            correlation_id=lease_owner,
            aggregate_type="asset",
            aggregate_id=asset.id,
            payload={
                "asset_id": str(asset.id),
                "generation_job_id": str(asset.generation_job_id),
                "sha256": verified.sha256,
            },
            status=OutboxStatus.PENDING,
            attempts=0,
            available_at=now,
            created_at=now,
        )
    )
    await session.commit()
    available = await session.get(Asset, available_id)
    if available is None:
        raise AssetNotFoundError("asset disappeared after promotion")
    return available


async def _mark_staging_cleaned_up(
    session: AsyncSession,
    *,
    asset_id: UUID,
    actor: str,
) -> None:
    asset = await session.get(Asset, asset_id)
    if asset is None:
        return
    metadata = dict(asset.asset_metadata)
    metadata["staging_cleanup"] = "completed"
    metadata["staging_cleaned_at"] = datetime.now(UTC).isoformat()
    asset.asset_metadata = metadata
    session.add(
        AuditEvent(
            actor=actor,
            action="asset.staging_deleted",
            resource_type="asset",
            resource_id=asset.id,
            correlation_id=str(asset.id),
            detail={},
            occurred_at=datetime.now(UTC),
        )
    )
    await session.commit()


async def finalize_raw_master(
    session: AsyncSession,
    store: ObjectStore,
    *,
    asset_id: UUID,
    max_bytes: int,
    reported_sha256: str | None = None,
    verification_lease_seconds: int = 900,
    actor: str = "controller",
) -> FinalizedAsset:
    normalized_reported_sha256 = _validate_reported_sha256(reported_sha256)
    try:
        asset, lease_owner = await _claim_verification(
            session,
            asset_id=asset_id,
            lease_seconds=verification_lease_seconds,
        )
    except _AlreadyAvailableError as existing:
        if (
            existing.asset.storage_backend != store.backend
            or existing.asset.storage_bucket != store.bucket
        ):
            raise AssetConflictError("asset belongs to another object store") from None
        if (
            normalized_reported_sha256 is not None
            and existing.asset.sha256 != normalized_reported_sha256
        ):
            now = datetime.now(UTC)
            session.add(
                AuditEvent(
                    actor=actor,
                    action="asset.duplicate_completion_conflict",
                    resource_type="asset",
                    resource_id=existing.asset.id,
                    correlation_id=str(existing.asset.id),
                    detail={"reported_sha256": normalized_reported_sha256},
                    occurred_at=now,
                )
            )
            await session.commit()
            raise AssetConflictError("duplicate completion reported a different checksum") from None
        return FinalizedAsset.from_model(existing.asset, replayed=True)

    if asset.storage_backend != store.backend or asset.storage_bucket != store.bucket:
        await _release_verification_claim(
            session,
            asset=asset,
            lease_owner=lease_owner,
            error_code="wrong_storage_bucket",
            actor=actor,
        )
        raise AssetConflictError("asset belongs to another object store")
    if asset.staging_object_key is None:
        await _quarantine(
            session,
            asset=asset,
            lease_owner=lease_owner,
            error_code="missing_staging_key",
            actor=actor,
        )
        raise AssetQuarantinedError("asset has no staging object key")

    try:
        staging = await store.head(asset.staging_object_key)
        if staging is None:
            await _release_verification_claim(
                session,
                asset=asset,
                lease_owner=lease_owner,
                error_code="staging_not_found",
                actor=actor,
            )
            raise UploadNotReadyError("staging upload is not visible yet")
        if staging.byte_size > max_bytes:
            raise ObjectTooLargeError("staging object exceeds the configured limit")
        if staging.version_id is None or staging.etag is None:
            await _release_verification_claim(
                session,
                asset=asset,
                lease_owner=lease_owner,
                error_code="storage_versioning_required",
                actor=actor,
            )
            raise AssetStorageUnavailableError(
                "object storage did not return a version ID and ETag"
            )

        _validate_staging_metadata(asset, staging)
        data = await store.read_bytes(
            asset.staging_object_key,
            max_bytes=max_bytes,
            version_id=staging.version_id,
            etag=staging.etag,
        )
        if len(data) != staging.byte_size:
            raise ImageVerificationError("staging object size changed during verification")
        verified = await verify_image_bytes_isolated(data)
        if verified.content_type != staging.content_type:
            raise ImageVerificationError("image signature does not match its content type")
        if normalized_reported_sha256 is not None and verified.sha256 != normalized_reported_sha256:
            raise ImageVerificationError("reported SHA-256 does not match the uploaded bytes")

        destination_key, promoted = await _promote_master(
            store,
            asset=asset,
            staging=staging,
            verified=verified,
        )
        if promoted.version_id is None:
            raise ObjectStoreError("promoted master has no version ID")
    except ObjectNotFoundError:
        await _release_verification_claim(
            session,
            asset=asset,
            lease_owner=lease_owner,
            error_code="staging_changed",
            actor=actor,
        )
        raise UploadNotReadyError("staging upload changed during verification") from None
    except ObjectTooLargeError:
        await _quarantine(
            session,
            asset=asset,
            lease_owner=lease_owner,
            error_code="image_too_large",
            actor=actor,
        )
        raise AssetQuarantinedError("image exceeds the configured size limit") from None
    except ImageVerificationError:
        await _quarantine(
            session,
            asset=asset,
            lease_owner=lease_owner,
            error_code="image_verification_failed",
            actor=actor,
        )
        raise AssetQuarantinedError("image failed verification") from None
    except ObjectStoreError:
        await _release_verification_claim(
            session,
            asset=asset,
            lease_owner=lease_owner,
            error_code="storage_unavailable",
            actor=actor,
        )
        raise AssetStorageUnavailableError("object storage is unavailable") from None

    available = await _mark_available(
        session,
        asset=asset,
        lease_owner=lease_owner,
        destination_key=destination_key,
        promoted=promoted,
        verified=verified,
        staging=staging,
        actor=actor,
    )

    try:
        await store.delete(
            asset.staging_object_key,
            version_id=staging.version_id,
        )
    except ObjectStoreError:
        pass
    else:
        try:
            await _mark_staging_cleaned_up(
                session,
                asset_id=asset.id,
                actor=actor,
            )
        except SQLAlchemyError:
            await session.rollback()
        else:
            refreshed = await session.get(Asset, asset.id)
            if refreshed is not None:
                available = refreshed

    return FinalizedAsset.from_model(available, replayed=False)


async def presign_asset_download(
    session: AsyncSession,
    store: ObjectStore,
    *,
    asset_id: UUID,
    expires_in: int,
    download_name: str | None = None,
) -> str:
    asset = await session.get(Asset, asset_id)
    if asset is None:
        raise AssetNotFoundError("asset not found")
    if asset.state != AssetState.AVAILABLE or asset.object_key is None:
        raise AssetConflictError("asset is not available for download")
    if asset.storage_backend != store.backend or asset.storage_bucket != store.bucket:
        raise AssetConflictError("asset belongs to another object store")
    return await store.presign_download(
        key=asset.object_key,
        expires_in=expires_in,
        download_name=download_name,
        version_id=asset.object_version_id,
    )
