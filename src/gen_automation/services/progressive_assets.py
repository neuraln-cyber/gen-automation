import base64
import binascii
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from gen_automation.db.models import Asset, GenerationJob, Release, ReleaseVersion
from gen_automation.domain.enums import AssetKind, AssetState
from gen_automation.services.generation_positions import (
    generation_ordinal,
    generation_position,
    generation_queue_offsets,
)
from gen_automation.storage.base import ObjectStore

_CURSOR_VERSION = "1"
_MAX_CURSOR_LENGTH = 256


class ProgressiveAssetError(Exception):
    """Base error for progressive raw-master reads."""


class ProgressiveAssetNotFoundError(ProgressiveAssetError):
    pass


class ProgressiveAssetCursorError(ProgressiveAssetError):
    pass


class ProgressiveAssetIntegrityError(ProgressiveAssetError):
    pass


@dataclass(frozen=True, slots=True)
class AvailableRawMaster:
    asset_id: UUID
    source_sha256: str
    available_at: datetime
    width: int
    height: int
    image_format: str
    byte_size: int
    checksum_prefix: str
    ordinal: int
    output_index: int
    queue_position: int
    batch_index: int
    batch_name: str
    batch_image_number: int


@dataclass(frozen=True, slots=True)
class AvailableRawMasterPage:
    release_id: UUID
    assets: tuple[AvailableRawMaster, ...]
    next_cursor: str | None
    has_more: bool


@dataclass(frozen=True, slots=True)
class _AvailableCursor:
    available_at: datetime
    asset_id: UUID


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _encode_cursor(cursor: _AvailableCursor) -> str:
    timestamp = _as_utc(cursor.available_at).isoformat(timespec="microseconds")
    payload = f"{_CURSOR_VERSION}|{timestamp}|{cursor.asset_id}".encode("ascii")
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


def _decode_cursor(value: str | None) -> _AvailableCursor | None:
    if value is None:
        return None
    if (
        not value
        or value != value.strip()
        or len(value) > _MAX_CURSOR_LENGTH
        or any(ord(character) < 33 or ord(character) > 126 for character in value)
    ):
        raise ProgressiveAssetCursorError("available-master cursor is invalid")
    padded = value + ("=" * (-len(value) % 4))
    try:
        decoded = base64.b64decode(
            padded.encode("ascii"),
            altchars=b"-_",
            validate=True,
        ).decode("ascii")
        version, raw_timestamp, raw_asset_id = decoded.split("|", maxsplit=2)
        available_at = datetime.fromisoformat(raw_timestamp)
        asset_id = UUID(raw_asset_id)
    except (ValueError, UnicodeError, binascii.Error):
        raise ProgressiveAssetCursorError("available-master cursor is invalid") from None
    if version != _CURSOR_VERSION or available_at.tzinfo is None:
        raise ProgressiveAssetCursorError("available-master cursor is invalid")
    return _AvailableCursor(
        available_at=_as_utc(available_at),
        asset_id=asset_id,
    )


async def list_available_raw_masters(
    session: AsyncSession,
    *,
    store: ObjectStore,
    release_id: UUID,
    cursor: str | None = None,
    limit: int = 32,
) -> AvailableRawMasterPage:
    """Return newly available raw masters in commit order without missing late batches."""

    if limit <= 0 or limit > 64:
        raise ValueError("available-master limit must be between 1 and 64")
    decoded_cursor = _decode_cursor(cursor)
    release_version_id = await session.scalar(
        select(ReleaseVersion.id)
        .join(
            Release,
            (Release.id == ReleaseVersion.release_id)
            & (Release.current_version_no == ReleaseVersion.version_no),
        )
        .where(Release.id == release_id)
    )
    if release_version_id is None:
        raise ProgressiveAssetNotFoundError("release was not found")

    release_jobs = list(
        (
            await session.scalars(
                select(GenerationJob).where(GenerationJob.release_version_id == release_version_id)
            )
        ).all()
    )
    queue_offsets = generation_queue_offsets(release_jobs)

    query = (
        select(Asset, GenerationJob)
        .join(GenerationJob, GenerationJob.id == Asset.generation_job_id)
        .where(
            Asset.release_id == release_id,
            Asset.kind == AssetKind.RAW_MASTER,
            Asset.state == AssetState.AVAILABLE,
            Asset.available_at.is_not(None),
            GenerationJob.release_version_id == release_version_id,
        )
    )
    if decoded_cursor is not None:
        query = query.where(
            or_(
                Asset.available_at > decoded_cursor.available_at,
                and_(
                    Asset.available_at == decoded_cursor.available_at,
                    Asset.id > decoded_cursor.asset_id,
                ),
            )
        )
    rows = list(
        (await session.execute(query.order_by(Asset.available_at, Asset.id).limit(limit + 1))).all()
    )
    has_more = len(rows) > limit
    page_rows = rows[:limit]
    assets: list[AvailableRawMaster] = []
    last_cursor = decoded_cursor
    for asset, job in page_rows:
        available_at = asset.available_at
        if (
            available_at is None
            or asset.object_key is None
            or asset.sha256 is None
            or asset.width is None
            or asset.height is None
            or asset.image_format is None
            or asset.byte_size is None
            or asset.output_index is None
            or asset.storage_backend != store.backend
            or asset.storage_bucket != store.bucket
        ):
            raise ProgressiveAssetIntegrityError(
                "an available raw master has incomplete or incompatible storage metadata"
            )
        position = generation_position(job, asset)
        output_index = asset.output_index
        queue_offset = queue_offsets.get(job.id)
        if queue_offset is None:
            raise ProgressiveAssetIntegrityError(
                "an available raw master does not belong to the current generation queue"
            )
        normalized_available_at = _as_utc(available_at)
        assets.append(
            AvailableRawMaster(
                asset_id=asset.id,
                source_sha256=asset.sha256,
                available_at=normalized_available_at,
                width=asset.width,
                height=asset.height,
                image_format=asset.image_format,
                byte_size=asset.byte_size,
                checksum_prefix=asset.sha256[:12],
                ordinal=generation_ordinal(job),
                output_index=output_index,
                queue_position=queue_offset + output_index + 1,
                batch_index=position.batch_index,
                batch_name=position.batch_name,
                batch_image_number=position.batch_image_number,
            )
        )
        last_cursor = _AvailableCursor(
            available_at=normalized_available_at,
            asset_id=asset.id,
        )

    return AvailableRawMasterPage(
        release_id=release_id,
        assets=tuple(assets),
        next_cursor=_encode_cursor(last_cursor) if last_cursor is not None else None,
        has_more=has_more,
    )
