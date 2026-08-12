"""Private media ingress and egress for the focused image-to-video workflow.

This module deliberately owns only media mechanics.  It validates image bytes,
binds direct uploads to the authenticated operator, promotes immutable inputs,
and creates short-lived output links.  It does not make content, character, or
licensing decisions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import UUID

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from gen_automation.db.models import Asset, GenerationJob, Release, ReleaseVersion
from gen_automation.domain.enums import AssetKind, AssetState
from gen_automation.domain.i2v import (
    I2VAttemptSnapshot,
    I2VInputRegistration,
    I2VInputSnapshot,
    I2VInputSource,
    I2VJobSnapshot,
    I2VOutputRegistration,
    I2VOutputSnapshot,
)
from gen_automation.domain.ids import uuid7
from gen_automation.integrations.salad.models import JSONValue
from gen_automation.services.i2v import register_i2v_input
from gen_automation.storage.base import (
    ObjectAlreadyExistsError,
    ObjectMetadata,
    ObjectNotFoundError,
    ObjectStore,
    ObjectStoreError,
    ObjectTooLargeError,
    PresignedUpload,
)
from gen_automation.storage.images import (
    ImageVerificationError,
    VerifiedImage,
    verify_image_bytes_isolated,
)


class I2VMediaError(Exception):
    """Base error for I2V media operations."""


class I2VMediaNotFoundError(I2VMediaError):
    pass


class I2VMediaConflictError(I2VMediaError):
    pass


class I2VMediaStorageError(I2VMediaError):
    pass


@dataclass(frozen=True, slots=True)
class I2VLibraryImage:
    asset_id: UUID
    display_name: str
    preview_url: str
    width: int
    height: int
    byte_size: int
    created_at: datetime


@dataclass(frozen=True, slots=True)
class I2VUploadIntent:
    upload_id: UUID
    display_name: str
    content_type: str
    max_bytes: int
    grant: PresignedUpload


@dataclass(frozen=True, slots=True)
class I2VSignedGrantBuilder:
    """Issue attempt-scoped S3 grants without persisting their signatures."""

    store: ObjectStore
    expires_in: int
    output_prefix: str = "i2v/outputs"

    async def build(
        self,
        *,
        job: I2VJobSnapshot,
        attempt: I2VAttemptSnapshot,
    ) -> dict[str, JSONValue]:
        snapshot = job.input_snapshot
        if (
            snapshot.get("storage_backend") != self.store.backend
            or snapshot.get("storage_bucket") != self.store.bucket
            or not isinstance(snapshot.get("object_key"), str)
            or not isinstance(snapshot.get("object_version_id"), str)
        ):
            raise I2VMediaConflictError("I2V input is not an immutable object in this store")
        expires_at = datetime.now(UTC) + timedelta(seconds=self.expires_in)
        try:
            input_url = await self.store.presign_download(
                key=snapshot["object_key"],
                version_id=snapshot["object_version_id"],
                expires_in=self.expires_in,
            )
            output_key = f"{self.output_prefix}/{job.job_id}/{attempt.attempt_id}.mp4"
            output = await self.store.presign_put(
                key=output_key,
                content_type="video/mp4",
                metadata={
                    "i2v-job-id": str(job.job_id),
                    "i2v-attempt-id": str(attempt.attempt_id),
                    "request-sha256": job.request_sha256,
                },
                expires_in=self.expires_in,
            )
        except ObjectStoreError as error:
            raise I2VMediaStorageError("private I2V grants could not be issued") from error
        if output.method != "PUT" or output.fields:
            raise I2VMediaStorageError("private store did not issue a direct PUT grant")
        return {
            "input_grant": {
                "method": "GET",
                "url": input_url,
                "expires_at": expires_at.isoformat(),
            },
            "output_grant": {
                "method": "PUT",
                "url": output.url,
                "headers": cast(JSONValue, output.headers),
                "storage_backend": self.store.backend,
                "storage_bucket": self.store.bucket,
                "object_key": output_key,
                "expires_at": expires_at.isoformat(),
            },
        }

    async def verify_output(
        self,
        *,
        job: I2VJobSnapshot,
        attempt: I2VAttemptSnapshot,
        output: I2VOutputRegistration,
    ) -> None:
        expected_key = f"{self.output_prefix}/{job.job_id}/{attempt.attempt_id}.mp4"
        if (
            output.storage_backend != self.store.backend
            or output.storage_bucket != self.store.bucket
            or output.object_key != expected_key
            or output.object_version_id is None
        ):
            raise I2VMediaConflictError("I2V output identity is invalid")
        try:
            metadata = await self.store.head(
                output.object_key,
                version_id=output.object_version_id,
            )
            if metadata is None:
                raise I2VMediaConflictError("I2V output object was not found")
            expected_metadata = {
                "i2v-job-id": str(job.job_id),
                "i2v-attempt-id": str(attempt.attempt_id),
                "request-sha256": job.request_sha256,
            }
            if (
                metadata.byte_size != output.byte_size
                or metadata.content_type != "video/mp4"
                or any(
                    metadata.metadata.get(key) != value for key, value in expected_metadata.items()
                )
            ):
                raise I2VMediaConflictError("I2V output metadata is invalid")
            digest = await self.store.sha256(
                output.object_key,
                max_bytes=output.byte_size,
                version_id=output.object_version_id,
                etag=metadata.etag,
            )
            if digest.byte_size != output.byte_size or digest.sha256 != output.sha256:
                raise I2VMediaConflictError("I2V output checksum is invalid")
        except I2VMediaError:
            raise
        except ObjectStoreError as error:
            raise I2VMediaStorageError("private I2V output could not be verified") from error


_UPLOAD_CONTENT_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})


async def list_i2v_library_images(
    session: AsyncSession,
    *,
    limit: int = 100,
) -> tuple[I2VLibraryImage, ...]:
    """List recent, verified generation masters for the source picker."""

    if isinstance(limit, bool) or limit < 1 or limit > 200:
        raise ValueError("limit must be between 1 and 200")
    rows = (
        await session.execute(
            select(Asset, Release.title)
            .join(GenerationJob, GenerationJob.id == Asset.generation_job_id)
            .join(ReleaseVersion, ReleaseVersion.id == GenerationJob.release_version_id)
            .join(Release, Release.id == ReleaseVersion.release_id)
            .where(
                Asset.kind == AssetKind.RAW_MASTER,
                Asset.state == AssetState.AVAILABLE,
                Asset.sha256.is_not(None),
                Asset.width.is_not(None),
                Asset.height.is_not(None),
                Asset.byte_size.is_not(None),
            )
            .order_by(desc(Asset.available_at), desc(Asset.created_at), desc(Asset.id))
            .limit(limit)
        )
    ).all()
    return tuple(
        I2VLibraryImage(
            asset_id=asset.id,
            display_name=f"{title} · image {(asset.output_index or 0) + 1}",
            preview_url=_library_preview_url(
                asset_id=asset.id, source_sha256=_required_text(asset.sha256)
            ),
            width=_required_int(asset.width),
            height=_required_int(asset.height),
            byte_size=_required_int(asset.byte_size),
            created_at=asset.available_at or asset.created_at,
        )
        for asset, title in rows
    )


async def create_i2v_upload_intent(
    store: ObjectStore,
    *,
    actor_user_id: UUID,
    display_name: str,
    content_type: str,
    max_bytes: int,
    expires_in: int,
) -> I2VUploadIntent:
    normalized_name = _display_name(display_name)
    normalized_type = content_type.strip().lower()
    if normalized_type not in _UPLOAD_CONTENT_TYPES:
        raise I2VMediaConflictError("upload must be a JPEG, PNG, or WebP image")
    if isinstance(max_bytes, bool) or max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    if isinstance(expires_in, bool) or expires_in <= 0:
        raise ValueError("expires_in must be positive")
    upload_id = uuid7()
    key = _staging_key(actor_user_id=actor_user_id, upload_id=upload_id)
    metadata = {
        "i2v-upload-id": str(upload_id),
        "actor-user-id": str(actor_user_id),
        "declared-content-type": normalized_type,
    }
    try:
        grant = await store.presign_upload(
            key=key,
            content_type=normalized_type,
            metadata=metadata,
            expires_in=expires_in,
            max_bytes=max_bytes,
        )
    except ObjectStoreError as error:
        raise I2VMediaStorageError("private storage could not issue an upload") from error
    return I2VUploadIntent(
        upload_id=upload_id,
        display_name=normalized_name,
        content_type=normalized_type,
        max_bytes=max_bytes,
        grant=grant,
    )


async def complete_i2v_upload(
    session: AsyncSession,
    store: ObjectStore,
    *,
    actor_user_id: UUID,
    upload_id: UUID,
    display_name: str,
    max_bytes: int,
) -> I2VInputSnapshot:
    """Verify a direct upload and register an immutable I2V input."""

    staging_key = _staging_key(actor_user_id=actor_user_id, upload_id=upload_id)
    try:
        staging = await store.head(staging_key)
        if staging is None:
            raise I2VMediaNotFoundError("uploaded image was not found")
        _validate_staging(
            staging, actor_user_id=actor_user_id, upload_id=upload_id, max_bytes=max_bytes
        )
        body = await store.read_bytes(
            staging_key,
            max_bytes=max_bytes,
            version_id=staging.version_id,
            etag=staging.etag,
        )
        verified = await verify_image_bytes_isolated(body)
        declared_type = staging.metadata.get("declared-content-type")
        if staging.content_type != declared_type or verified.content_type != declared_type:
            raise I2VMediaConflictError("uploaded image type does not match the upload intent")
        destination_key, promoted = await _promote_input(
            store,
            source_key=staging_key,
            source=staging,
            verified=verified,
            actor_user_id=actor_user_id,
            upload_id=upload_id,
        )
        snapshot = await register_i2v_input(
            session,
            actor_user_id=actor_user_id,
            registration=I2VInputRegistration(
                source=I2VInputSource.UPLOAD,
                display_name=_display_name(display_name),
                storage_backend=store.backend,
                storage_bucket=store.bucket,
                object_key=destination_key,
                object_version_id=promoted.version_id,
                sha256=verified.sha256,
                content_type=verified.content_type,
                width=verified.width,
                height=verified.height,
                byte_size=verified.byte_size,
                metadata={"upload_id": str(upload_id)},
            ),
        )
    except I2VMediaError:
        raise
    except (ImageVerificationError, ObjectTooLargeError) as error:
        raise I2VMediaConflictError("uploaded image is invalid or unsafe") from error
    except (ObjectNotFoundError, ObjectStoreError) as error:
        raise I2VMediaStorageError("private storage could not verify the upload") from error

    # The immutable copy is registered before staging cleanup.  Cleanup failure
    # is harmless and must not turn a usable input into a failed request.
    try:
        await store.delete(staging_key, version_id=staging.version_id)
    except ObjectStoreError:
        pass
    return snapshot


async def register_i2v_generation_asset(
    session: AsyncSession,
    store: ObjectStore,
    *,
    actor_user_id: UUID,
    asset_id: UUID,
) -> I2VInputSnapshot:
    asset = await session.get(Asset, asset_id)
    if asset is None or asset.kind != AssetKind.RAW_MASTER:
        raise I2VMediaNotFoundError("generated image was not found")
    if (
        asset.state != AssetState.AVAILABLE
        or asset.object_key is None
        or asset.sha256 is None
        or asset.content_type is None
        or asset.width is None
        or asset.height is None
        or asset.byte_size is None
    ):
        raise I2VMediaConflictError("generated image is not available")
    if asset.storage_backend != store.backend or asset.storage_bucket != store.bucket:
        raise I2VMediaConflictError("generated image belongs to another private store")
    extension = _extension_for_content_type(asset.content_type)
    destination_key = _input_key(asset.sha256, extension)
    metadata = {
        "sha256": asset.sha256,
        "source-asset-id": str(asset.id),
        "actor-user-id": str(actor_user_id),
    }
    try:
        promoted = await _copy_or_match(
            store,
            source_key=asset.object_key,
            destination_key=destination_key,
            content_type=asset.content_type,
            metadata=metadata,
            source_version_id=asset.object_version_id,
            source_etag=None,
            expected_sha256=asset.sha256,
            expected_size=asset.byte_size,
        )
    except (ObjectNotFoundError, ObjectStoreError) as error:
        raise I2VMediaStorageError("private storage could not prepare the image") from error
    return await register_i2v_input(
        session,
        actor_user_id=actor_user_id,
        registration=I2VInputRegistration(
            source=I2VInputSource.GENERATION,
            asset_id=asset.id,
            display_name=f"Generated image {str(asset.id)[:8]}",
            storage_backend=store.backend,
            storage_bucket=store.bucket,
            object_key=destination_key,
            object_version_id=promoted.version_id,
            sha256=asset.sha256,
            content_type=asset.content_type,
            width=asset.width,
            height=asset.height,
            byte_size=asset.byte_size,
            metadata={"source_asset_id": str(asset.id)},
        ),
    )


async def presign_i2v_output_download(
    store: ObjectStore,
    *,
    output: I2VOutputSnapshot,
    expires_in: int,
    attachment: bool = False,
) -> str:
    if output.storage_backend != store.backend or output.storage_bucket != store.bucket:
        raise I2VMediaConflictError("video belongs to another private store")
    try:
        return await store.presign_download(
            key=output.object_key,
            expires_in=expires_in,
            download_name=f"i2v-{output.job_id}.mp4" if attachment else None,
            version_id=output.object_version_id,
        )
    except (ObjectNotFoundError, ObjectStoreError) as error:
        raise I2VMediaStorageError("video download is temporarily unavailable") from error


def _staging_key(*, actor_user_id: UUID, upload_id: UUID) -> str:
    return f"i2v/staging/{actor_user_id}/{upload_id}"


def _library_preview_url(*, asset_id: UUID, source_sha256: str) -> str:
    return f"/api/v1/i2v/source-images/{asset_id}/preview/{source_sha256[:16]}.jpg"


def _input_key(sha256: str, extension: str) -> str:
    return f"i2v/inputs/sha256/{sha256[:2]}/{sha256}.{extension}"


def _display_name(value: str) -> str:
    normalized = Path(value.strip()).name
    if not normalized or len(normalized) > 255:
        raise I2VMediaConflictError("display name must contain 1 to 255 characters")
    return normalized


def _extension_for_content_type(content_type: str) -> str:
    try:
        return {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}[content_type]
    except KeyError:
        raise I2VMediaConflictError("image type is not supported") from None


def _validate_staging(
    staging: ObjectMetadata,
    *,
    actor_user_id: UUID,
    upload_id: UUID,
    max_bytes: int,
) -> None:
    expected = {
        "i2v-upload-id": str(upload_id),
        "actor-user-id": str(actor_user_id),
    }
    if any(staging.metadata.get(name) != value for name, value in expected.items()):
        raise I2VMediaConflictError("upload identity does not match this request")
    if staging.byte_size < 1 or staging.byte_size > max_bytes:
        raise I2VMediaConflictError("uploaded image exceeds the configured image size")
    if staging.content_type not in _UPLOAD_CONTENT_TYPES:
        raise I2VMediaConflictError("uploaded image type is not supported")


async def _promote_input(
    store: ObjectStore,
    *,
    source_key: str,
    source: ObjectMetadata,
    verified: VerifiedImage,
    actor_user_id: UUID,
    upload_id: UUID,
) -> tuple[str, ObjectMetadata]:
    destination_key = _input_key(verified.sha256, verified.extension)
    promoted = await _copy_or_match(
        store,
        source_key=source_key,
        destination_key=destination_key,
        content_type=verified.content_type,
        metadata={
            "sha256": verified.sha256,
            "actor-user-id": str(actor_user_id),
            "source-upload-id": str(upload_id),
        },
        source_version_id=source.version_id,
        source_etag=source.etag,
        expected_sha256=verified.sha256,
        expected_size=verified.byte_size,
    )
    return destination_key, promoted


async def _copy_or_match(
    store: ObjectStore,
    *,
    source_key: str,
    destination_key: str,
    content_type: str,
    metadata: dict[str, str],
    source_version_id: str | None,
    source_etag: str | None,
    expected_sha256: str,
    expected_size: int,
) -> ObjectMetadata:
    try:
        result = await store.copy_if_absent(
            source_key=source_key,
            destination_key=destination_key,
            content_type=content_type,
            metadata=metadata,
            source_version_id=source_version_id,
            source_etag=source_etag,
        )
    except ObjectAlreadyExistsError:
        existing = await store.head(destination_key)
        if existing is None:
            raise I2VMediaStorageError("immutable input disappeared during promotion") from None
        if (
            existing.byte_size != expected_size
            or existing.content_type != content_type
            or existing.metadata.get("sha256") != expected_sha256
        ):
            raise I2VMediaConflictError(
                "immutable input destination contains different bytes"
            ) from None
        result = existing
    if result.byte_size != expected_size or result.content_type != content_type:
        raise I2VMediaConflictError("promoted image metadata does not match its source")
    return result


def _required_text(value: str | None) -> str:
    if value is None:
        raise RuntimeError("complete asset is missing text metadata")
    return value


def _required_int(value: int | None) -> int:
    if value is None:
        raise RuntimeError("complete asset is missing numeric metadata")
    return value
