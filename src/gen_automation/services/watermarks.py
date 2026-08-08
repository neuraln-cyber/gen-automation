"""Reusable, content-addressed watermark onboarding."""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from anyio import fail_after, to_process
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from gen_automation.db.models import (
    AdminUser,
    Asset,
    AuditEvent,
    IdempotencyRecord,
    Release,
)
from gen_automation.domain.canonical import canonical_sha256
from gen_automation.domain.enums import AdminRole, AssetKind, AssetState
from gen_automation.domain.ids import uuid7
from gen_automation.services.derivatives import (
    DerivativeInputError,
    VerifiedWatermark,
    verify_watermark_png,
)
from gen_automation.storage.base import (
    ObjectAlreadyExistsError,
    ObjectMetadata,
    ObjectNotFoundError,
    ObjectStore,
    ObjectStoreError,
    ObjectTooLargeError,
)

MAX_WATERMARK_BYTES = 4 * 1024 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}")
_PURPOSE = "watermark"
_SCHEMA = "watermark-asset/v1"


class WatermarkError(Exception):
    """Base error for reusable watermark registration."""


class WatermarkInputError(WatermarkError, ValueError):
    pass


class WatermarkNotFoundError(WatermarkError):
    pass


class WatermarkConflictError(WatermarkError):
    pass


class WatermarkStorageError(WatermarkError):
    pass


@dataclass(frozen=True, slots=True)
class RegisteredWatermark:
    asset_id: UUID
    display_name: str
    sha256: str
    storage_backend: str
    storage_bucket: str
    object_key: str
    object_version_id: str
    width: int
    height: int
    byte_size: int
    registered_at: datetime
    replayed: bool


@dataclass(frozen=True, slots=True)
class RegisteredWatermarkPayload:
    asset_id: UUID
    display_name: str
    sha256: str
    width: int
    height: int
    data: bytes


async def register_watermark(
    session: AsyncSession,
    store: ObjectStore,
    *,
    release_id: UUID,
    display_name: str,
    png_bytes: bytes,
    registered_by_user_id: UUID,
    idempotency_key: str,
    now: datetime | None = None,
) -> RegisteredWatermark:
    """Validate and register one immutable PNG for reuse by any release."""

    normalized_name = _bounded_text(display_name, "watermark display name", 100)
    normalized_key = _bounded_text(idempotency_key, "idempotency key", 200)
    if not isinstance(png_bytes, bytes) or not png_bytes:
        raise WatermarkInputError("watermark file is empty")
    if len(png_bytes) > MAX_WATERMARK_BYTES:
        raise WatermarkInputError("watermark file exceeds 4 MiB")
    registered_at = _as_utc(now or datetime.now(UTC))
    await _require_owner(session, registered_by_user_id)
    if await session.get(Release, release_id) is None:
        raise WatermarkNotFoundError("watermark provenance release was not found")

    try:
        with fail_after(30):
            verified = await to_process.run_sync(
                verify_watermark_png,
                png_bytes,
                cancellable=True,
            )
    except DerivativeInputError as error:
        raise WatermarkInputError("watermark must be a safe transparent PNG") from error
    except TimeoutError as error:
        raise WatermarkInputError("watermark validation timed out") from error
    if not isinstance(verified, VerifiedWatermark):
        raise WatermarkInputError("watermark validation failed")

    scope = "watermarks:register"
    request_sha256 = canonical_sha256(
        {
            "schema": "watermark-registration-request/v1",
            "release_id": str(release_id),
            "display_name": normalized_name,
            "sha256": verified.sha256,
            "registered_by_user_id": str(registered_by_user_id),
        }
    )
    replay = await _idempotency_replay(
        session,
        scope=scope,
        key=normalized_key,
        request_sha256=request_sha256,
    )
    if replay is not None:
        return replay

    object_key = _watermark_object_key(verified.sha256)
    existing = await session.scalar(
        select(Asset).where(
            Asset.storage_backend == store.backend,
            Asset.storage_bucket == store.bucket,
            Asset.object_key == object_key,
        )
    )
    if existing is not None:
        _validate_registry_asset(existing, expected_sha256=verified.sha256)
        result = _result(existing, replayed=True)
        session.add(
            _idempotency_record(
                scope=scope,
                key=normalized_key,
                request_sha256=request_sha256,
                result=result,
                created_at=registered_at,
            )
        )
        await session.commit()
        return result

    metadata = {
        "sha256": verified.sha256,
        "purpose": _PURPOSE,
        "schema": _SCHEMA,
    }
    try:
        try:
            stored = await store.write_bytes_if_absent(
                key=object_key,
                body=png_bytes,
                content_type=verified.content_type,
                metadata=metadata,
                max_bytes=MAX_WATERMARK_BYTES,
            )
        except ObjectAlreadyExistsError:
            existing_metadata = await store.head(object_key)
            if existing_metadata is None:
                raise WatermarkStorageError(
                    "watermark object disappeared during registration"
                ) from None
            stored = existing_metadata
    except ObjectStoreError as error:
        raise WatermarkStorageError("watermark storage is unavailable") from error
    _validate_stored_object(stored, verified=verified)

    asset = Asset(
        id=uuid7(),
        release_id=release_id,
        generation_job_id=None,
        output_index=None,
        kind=AssetKind.DERIVATIVE,
        state=AssetState.AVAILABLE,
        storage_backend=store.backend,
        storage_bucket=store.bucket,
        object_key=object_key,
        object_version_id=stored.version_id,
        sha256=verified.sha256,
        content_type=verified.content_type,
        image_format=verified.image_format,
        width=verified.width,
        height=verified.height,
        byte_size=verified.byte_size,
        asset_metadata={
            "purpose": _PURPOSE,
            "schema": _SCHEMA,
            "display_name": normalized_name,
            "registered_by_user_id": str(registered_by_user_id),
        },
        available_at=registered_at,
    )
    result = _result(asset, replayed=False)
    session.add(asset)
    session.add(
        AuditEvent(
            id=uuid7(),
            actor=f"admin:{registered_by_user_id}",
            action="watermark.registered",
            resource_type="asset",
            resource_id=asset.id,
            correlation_id=normalized_key,
            detail={
                "sha256": verified.sha256,
                "object_key": object_key,
                "object_version_id": stored.version_id,
                "width": verified.width,
                "height": verified.height,
                "byte_size": verified.byte_size,
                "provenance_release_id": str(release_id),
            },
            occurred_at=registered_at,
        )
    )
    session.add(
        _idempotency_record(
            scope=scope,
            key=normalized_key,
            request_sha256=request_sha256,
            result=result,
            created_at=registered_at,
        )
    )
    try:
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        replay = await _idempotency_replay(
            session,
            scope=scope,
            key=normalized_key,
            request_sha256=request_sha256,
        )
        if replay is not None:
            return replay
        winner = await session.scalar(
            select(Asset).where(
                Asset.storage_backend == store.backend,
                Asset.storage_bucket == store.bucket,
                Asset.object_key == object_key,
            )
        )
        if winner is not None:
            _validate_registry_asset(winner, expected_sha256=verified.sha256)
            return _result(winner, replayed=True)
        raise WatermarkConflictError("watermark was registered concurrently") from error
    return result


async def list_registered_watermarks(
    session: AsyncSession,
    *,
    limit: int = 100,
) -> tuple[RegisteredWatermark, ...]:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise WatermarkInputError("watermark list limit is invalid")
    assets = tuple(
        (
            await session.scalars(
                select(Asset)
                .where(
                    Asset.kind == AssetKind.DERIVATIVE,
                    Asset.state == AssetState.AVAILABLE,
                    Asset.object_key.like("watermarks/%"),
                )
                .order_by(Asset.available_at.desc(), Asset.id.desc())
                .limit(limit)
            )
        ).all()
    )
    results: list[RegisteredWatermark] = []
    for asset in assets:
        try:
            _validate_registry_asset(asset)
        except WatermarkConflictError:
            continue
        results.append(_result(asset, replayed=False))
    return tuple(results)


async def read_registered_watermark(
    session: AsyncSession,
    store: ObjectStore,
    *,
    asset_id: UUID,
) -> RegisteredWatermarkPayload:
    """Read one exact reusable watermark without exposing arbitrary derivatives."""

    asset = await session.get(Asset, asset_id)
    if asset is None or not is_registered_watermark(asset):
        raise WatermarkNotFoundError("registered watermark was not found")
    _validate_registry_asset(asset)
    if asset.storage_backend != store.backend or asset.storage_bucket != store.bucket:
        raise WatermarkConflictError("watermark belongs to another object store")
    assert asset.object_key is not None
    assert asset.object_version_id is not None
    assert asset.byte_size is not None
    assert asset.sha256 is not None
    try:
        data = await store.read_bytes(
            asset.object_key,
            max_bytes=asset.byte_size,
            version_id=asset.object_version_id,
        )
    except (ObjectNotFoundError, ObjectTooLargeError) as error:
        raise WatermarkConflictError("watermark object identity changed") from error
    except ObjectStoreError as error:
        raise WatermarkStorageError("watermark storage is unavailable") from error
    if len(data) != asset.byte_size or not hmac.compare_digest(
        hashlib.sha256(data).hexdigest(),
        asset.sha256,
    ):
        raise WatermarkConflictError("watermark object identity changed")
    display_name = asset.asset_metadata.get("display_name")
    return RegisteredWatermarkPayload(
        asset_id=asset.id,
        display_name=(
            display_name if isinstance(display_name, str) and display_name else "Watermark"
        ),
        sha256=asset.sha256,
        width=asset.width or 0,
        height=asset.height or 0,
        data=data,
    )


def is_registered_watermark(asset: Asset) -> bool:
    schema = asset.asset_metadata.get("schema")
    return (
        asset.kind == AssetKind.DERIVATIVE
        and asset.asset_metadata.get("purpose") == _PURPOSE
        and (schema is None or schema == _SCHEMA)
        and asset.content_type == "image/png"
        and asset.image_format == "PNG"
    )


async def _require_owner(session: AsyncSession, user_id: UUID) -> None:
    owner_id = await session.scalar(
        select(AdminUser.id).where(
            AdminUser.id == user_id,
            AdminUser.is_active.is_(True),
            AdminUser.role == AdminRole.OWNER,
        )
    )
    if owner_id is None:
        raise WatermarkNotFoundError("active owner was not found")


async def _idempotency_replay(
    session: AsyncSession,
    *,
    scope: str,
    key: str,
    request_sha256: str,
) -> RegisteredWatermark | None:
    record = await session.scalar(
        select(IdempotencyRecord).where(
            IdempotencyRecord.scope == scope,
            IdempotencyRecord.idempotency_key == key,
        )
    )
    if record is None:
        return None
    if record.request_sha256 != request_sha256:
        raise WatermarkConflictError("idempotency key was reused for another watermark")
    try:
        asset_id = UUID(str(record.response_body["asset_id"]))
    except (KeyError, TypeError, ValueError):
        raise WatermarkConflictError("stored watermark replay is invalid") from None
    asset = await session.get(Asset, asset_id)
    if asset is None:
        raise WatermarkConflictError("stored watermark replay asset is unavailable")
    _validate_registry_asset(asset)
    return _result(asset, replayed=True)


def _idempotency_record(
    *,
    scope: str,
    key: str,
    request_sha256: str,
    result: RegisteredWatermark,
    created_at: datetime,
) -> IdempotencyRecord:
    return IdempotencyRecord(
        id=uuid7(),
        scope=scope,
        idempotency_key=key,
        request_sha256=request_sha256,
        response_status=201,
        response_body={"asset_id": str(result.asset_id)},
        created_at=created_at,
        expires_at=created_at + timedelta(days=30),
    )


def _validate_registry_asset(
    asset: Asset,
    *,
    expected_sha256: str | None = None,
) -> None:
    if (
        not is_registered_watermark(asset)
        or asset.state != AssetState.AVAILABLE
        or not asset.storage_backend.strip()
        or not asset.storage_bucket.strip()
        or asset.object_key is None
        or not asset.object_key.startswith("watermarks/")
        or asset.object_version_id is None
        or not asset.object_version_id.strip()
        or asset.sha256 is None
        or _SHA256.fullmatch(asset.sha256) is None
        or (expected_sha256 is not None and asset.sha256 != expected_sha256)
        or asset.width is None
        or asset.width <= 0
        or asset.height is None
        or asset.height <= 0
        or asset.byte_size is None
        or asset.byte_size <= 0
        or asset.available_at is None
    ):
        raise WatermarkConflictError("registered watermark identity is incomplete")


def _validate_stored_object(
    stored: ObjectMetadata,
    *,
    verified: VerifiedWatermark,
) -> None:
    if (
        stored.version_id is None
        or not stored.version_id.strip()
        or stored.byte_size != verified.byte_size
        or stored.content_type != verified.content_type
        or stored.metadata.get("sha256") != verified.sha256
        or stored.metadata.get("purpose") != _PURPOSE
        or stored.metadata.get("schema") != _SCHEMA
    ):
        raise WatermarkConflictError("stored watermark identity does not match its bytes")


def _result(asset: Asset, *, replayed: bool) -> RegisteredWatermark:
    _validate_registry_asset(asset)
    assert asset.object_key is not None
    assert asset.object_version_id is not None
    assert asset.sha256 is not None
    assert asset.width is not None
    assert asset.height is not None
    assert asset.byte_size is not None
    assert asset.available_at is not None
    display_name = asset.asset_metadata.get("display_name")
    return RegisteredWatermark(
        asset_id=asset.id,
        display_name=(
            display_name if isinstance(display_name, str) and display_name else "Watermark"
        ),
        sha256=asset.sha256,
        storage_backend=asset.storage_backend,
        storage_bucket=asset.storage_bucket,
        object_key=asset.object_key,
        object_version_id=asset.object_version_id,
        width=asset.width,
        height=asset.height,
        byte_size=asset.byte_size,
        registered_at=_stored_as_utc(asset.available_at),
        replayed=replayed,
    )


def _watermark_object_key(sha256: str) -> str:
    if _SHA256.fullmatch(sha256) is None:
        raise WatermarkInputError("watermark checksum is invalid")
    return f"watermarks/{sha256[:2]}/{sha256}.png"


def _bounded_text(value: str, label: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise WatermarkInputError(f"{label} is invalid")
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise WatermarkInputError(f"{label} is invalid")
    return normalized


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise WatermarkInputError("watermark timestamp must include a timezone")
    return value.astimezone(UTC)


def _stored_as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
