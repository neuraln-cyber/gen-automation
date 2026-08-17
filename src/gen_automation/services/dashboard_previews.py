"""Small immutable dashboard previews for private raw masters.

The browser must never use a multi-megabyte raw master as a grid thumbnail.  A
preview is created once from the verified master, stored under a deterministic
content-addressed key, and re-encoded without source metadata.  The stable
authenticated dashboard URL can therefore be cached privately by the browser
while the exact master remains available through the separate view/download
routes.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import warnings
from dataclasses import dataclass
from io import BytesIO
from uuid import UUID

from anyio import CapacityLimiter, fail_after, to_process
from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy.ext.asyncio import AsyncSession

from gen_automation.db.models import Asset
from gen_automation.domain.enums import AssetKind, AssetState
from gen_automation.services.outbound_image_privacy import (
    OutboundImagePrivacyError,
    require_metadata_free_image,
)
from gen_automation.storage.base import (
    ObjectAlreadyExistsError,
    ObjectConflictError,
    ObjectNotFoundError,
    ObjectStore,
    ObjectStoreError,
    ObjectTooLargeError,
)
from gen_automation.storage.images import (
    FORMAT_CONTENT_TYPES,
    MAX_ASPECT_RATIO,
    MAX_IMAGE_HEIGHT,
    MAX_IMAGE_PIXELS,
    MAX_IMAGE_WIDTH,
)

DASHBOARD_PREVIEW_VERSION = "dashboard-preview-v1"
DASHBOARD_PREVIEW_EDGE = 768
DASHBOARD_PREVIEW_QUALITY = 78
DASHBOARD_PREVIEW_MAX_BYTES = 512 * 1024
DASHBOARD_PREVIEW_TOKEN_LENGTH = 16
DASHBOARD_PREVIEW_CACHE_CONTROL = "private, no-cache, must-revalidate"
DASHBOARD_PREVIEW_CONTENT_TYPE = "image/jpeg"

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TOKEN = re.compile(rf"^[0-9a-f]{{{DASHBOARD_PREVIEW_TOKEN_LENGTH}}}$")
_PREVIEW_CREATION_LIMITER = CapacityLimiter(2)


class DashboardPreviewError(Exception):
    """Base error for the private preview pipeline."""


class DashboardPreviewNotFoundError(DashboardPreviewError):
    pass


class DashboardPreviewConflictError(DashboardPreviewError):
    pass


class DashboardPreviewStorageError(DashboardPreviewError):
    pass


class DashboardPreviewRenderError(DashboardPreviewError):
    pass


@dataclass(frozen=True, slots=True)
class DashboardPreview:
    data: bytes
    sha256: str
    width: int
    height: int
    source_sha256: str

    @property
    def etag(self) -> str:
        return dashboard_preview_etag(self.source_sha256)


@dataclass(frozen=True, slots=True)
class DashboardPreviewSource:
    """Validated immutable source identity for one dashboard preview."""

    asset_id: UUID
    object_key: str
    object_version_id: str | None
    byte_size: int
    source_sha256: str

    @property
    def etag(self) -> str:
        return dashboard_preview_etag(self.source_sha256)


def dashboard_preview_url(*, asset_id: UUID, source_sha256: str) -> str:
    """Return the stable, source-versioned browser URL for a raw master."""

    normalized_sha256 = _source_sha256(source_sha256)
    return (
        f"/dashboard/assets/{asset_id}/previews/{DASHBOARD_PREVIEW_VERSION}/"
        f"{normalized_sha256[:DASHBOARD_PREVIEW_TOKEN_LENGTH]}.jpg"
    )


def dashboard_preview_etag(source_sha256: str) -> str:
    """Return an opaque ETag fixed by source identity and renderer version."""

    normalized_sha256 = _source_sha256(source_sha256)
    digest = hashlib.sha256(
        f"{DASHBOARD_PREVIEW_VERSION}\0{normalized_sha256}".encode("ascii")
    ).hexdigest()
    return f'"{DASHBOARD_PREVIEW_VERSION}-{digest}"'


async def resolve_dashboard_preview_source(
    session: AsyncSession,
    store: ObjectStore,
    *,
    asset_id: UUID,
    source_token: str,
    max_master_bytes: int,
) -> DashboardPreviewSource:
    """Validate the authenticated, content-addressed source without reading S3 bytes."""

    if (
        isinstance(max_master_bytes, bool)
        or not isinstance(max_master_bytes, int)
        or max_master_bytes <= 0
    ):
        raise ValueError("maximum master bytes must be a positive integer")
    token = source_token.strip().lower() if isinstance(source_token, str) else ""
    if not _TOKEN.fullmatch(token):
        raise DashboardPreviewNotFoundError("dashboard preview was not found")

    asset = await session.get(Asset, asset_id)
    if asset is None or asset.kind != AssetKind.RAW_MASTER:
        raise DashboardPreviewNotFoundError("raw master was not found")
    if (
        asset.state != AssetState.AVAILABLE
        or asset.object_key is None
        or asset.sha256 is None
        or asset.byte_size is None
        or asset.width is None
        or asset.height is None
    ):
        raise DashboardPreviewConflictError("raw master is not available")
    source_sha256 = _source_sha256(asset.sha256)
    if not hmac.compare_digest(
        token,
        source_sha256[:DASHBOARD_PREVIEW_TOKEN_LENGTH],
    ):
        # A stale content-addressed URL never falls through to the current
        # source.  That preserves the URL's immutable cache contract.
        raise DashboardPreviewNotFoundError("dashboard preview was not found")
    if asset.storage_backend != store.backend or asset.storage_bucket != store.bucket:
        raise DashboardPreviewConflictError("raw master belongs to another object store")
    if asset.byte_size <= 0 or asset.byte_size > max_master_bytes:
        raise DashboardPreviewConflictError("raw master exceeds the preview safety limit")
    return DashboardPreviewSource(
        asset_id=asset.id,
        object_key=asset.object_key,
        object_version_id=asset.object_version_id,
        byte_size=asset.byte_size,
        source_sha256=source_sha256,
    )


async def load_or_create_dashboard_preview(
    session: AsyncSession,
    store: ObjectStore,
    *,
    asset_id: UUID,
    source_token: str,
    max_master_bytes: int,
) -> DashboardPreview:
    """Load an immutable preview, rendering and storing it once if necessary."""

    source = await resolve_dashboard_preview_source(
        session,
        store,
        asset_id=asset_id,
        source_token=source_token,
        max_master_bytes=max_master_bytes,
    )
    return await load_or_create_resolved_dashboard_preview(store, source=source)


async def load_or_create_resolved_dashboard_preview(
    store: ObjectStore,
    *,
    source: DashboardPreviewSource,
) -> DashboardPreview:
    """Load or render bytes for an already validated immutable source."""

    key = _preview_object_key(
        asset_id=source.asset_id,
        source_sha256=source.source_sha256,
    )
    try:
        cached = await _read_cached_preview(
            store,
            key=key,
            asset_id=source.asset_id,
            source_sha256=source.source_sha256,
        )
        if cached is not None:
            return cached

        # Keep the limiter around the source GET as well as decoding.  A first
        # visit to a large set must not accumulate hundreds of full-size PNGs
        # in the API process while waiting for renderer capacity.
        async with _PREVIEW_CREATION_LIMITER:
            cached = await _read_cached_preview(
                store,
                key=key,
                asset_id=source.asset_id,
                source_sha256=source.source_sha256,
            )
            if cached is not None:
                return cached
            source_bytes = await store.read_bytes(
                source.object_key,
                max_bytes=source.byte_size,
                version_id=source.object_version_id,
            )
            if len(source_bytes) != source.byte_size or not hmac.compare_digest(
                hashlib.sha256(source_bytes).hexdigest(),
                source.source_sha256,
            ):
                raise DashboardPreviewConflictError("raw master identity changed")
            rendered = await render_dashboard_preview_isolated(source_bytes)
            metadata = {
                "kind": "dashboard-preview",
                "preview-version": DASHBOARD_PREVIEW_VERSION,
                "source-asset-id": str(source.asset_id),
                "source-sha256": source.source_sha256,
                "width": str(rendered.width),
                "height": str(rendered.height),
            }
            try:
                await store.write_bytes_if_absent(
                    key=key,
                    body=rendered.data,
                    content_type=DASHBOARD_PREVIEW_CONTENT_TYPE,
                    metadata=metadata,
                    max_bytes=DASHBOARD_PREVIEW_MAX_BYTES,
                )
            except (ObjectAlreadyExistsError, ObjectConflictError):
                # Another API process can still race harmlessly.  The winner's
                # immutable object must validate against the same source.
                existing = await _read_cached_preview(
                    store,
                    key=key,
                    asset_id=source.asset_id,
                    source_sha256=source.source_sha256,
                )
                if existing is None:
                    raise DashboardPreviewStorageError(
                        "dashboard preview write did not become visible"
                    ) from None
                return existing
            return rendered
    except (DashboardPreviewConflictError, DashboardPreviewRenderError):
        raise
    except (ObjectNotFoundError, ObjectTooLargeError) as error:
        raise DashboardPreviewConflictError("raw master changed during preview creation") from error
    except ObjectStoreError as error:
        raise DashboardPreviewStorageError("dashboard preview storage is unavailable") from error


async def render_dashboard_preview_isolated(
    source: bytes,
    *,
    timeout_seconds: float = 30.0,
) -> DashboardPreview:
    """Decode and re-encode outside the API process with a wall-time limit."""

    if not isinstance(source, bytes) or not source:
        raise DashboardPreviewRenderError("raw master bytes are invalid")
    try:
        with fail_after(timeout_seconds):
            preview = await to_process.run_sync(
                _render_dashboard_preview,
                source,
                cancellable=True,
            )
    except (TimeoutError, MemoryError, OSError, RuntimeError, ValueError) as error:
        raise DashboardPreviewRenderError("dashboard preview could not be rendered") from error
    if not isinstance(preview, DashboardPreview):
        raise DashboardPreviewRenderError("dashboard preview renderer returned invalid data")
    return preview


def _render_dashboard_preview(source: bytes) -> DashboardPreview:
    """Pure process-worker renderer; do not call on untrusted bytes in the API process."""

    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
    source_sha256 = hashlib.sha256(source).hexdigest()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(source)) as opened:
                image_format = (opened.format or "").upper()
                width, height = opened.size
                if (
                    image_format not in FORMAT_CONTENT_TYPES
                    or width <= 0
                    or height <= 0
                    or width > MAX_IMAGE_WIDTH
                    or height > MAX_IMAGE_HEIGHT
                    or width * height > MAX_IMAGE_PIXELS
                    or max(width, height) > min(width, height) * MAX_ASPECT_RATIO
                    or int(getattr(opened, "n_frames", 1)) != 1
                ):
                    raise DashboardPreviewRenderError("raw master dimensions are unsafe")
                oriented = ImageOps.exif_transpose(opened)
                oriented.load()
                rgba = oriented.convert("RGBA")
                background = Image.new("RGB", rgba.size, (255, 255, 255))
                background.paste(rgba, mask=rgba.getchannel("A"))

        background.thumbnail(
            (DASHBOARD_PREVIEW_EDGE, DASHBOARD_PREVIEW_EDGE),
            resample=Image.Resampling.LANCZOS,
            reducing_gap=3.0,
        )
        output = BytesIO()
        background.save(
            output,
            format="JPEG",
            quality=DASHBOARD_PREVIEW_QUALITY,
            optimize=True,
            progressive=False,
            subsampling="4:2:0",
        )
        data = output.getvalue()
    except DashboardPreviewRenderError:
        raise
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        MemoryError,
        OSError,
        SyntaxError,
        UnidentifiedImageError,
        ValueError,
    ) as error:
        raise DashboardPreviewRenderError("raw master could not be decoded") from error
    if not data or len(data) > DASHBOARD_PREVIEW_MAX_BYTES:
        raise DashboardPreviewRenderError("dashboard preview exceeds its byte limit")
    try:
        require_metadata_free_image(data, content_type=DASHBOARD_PREVIEW_CONTENT_TYPE)
    except OutboundImagePrivacyError as error:
        raise DashboardPreviewRenderError("dashboard preview contains metadata") from error
    return DashboardPreview(
        data=data,
        sha256=hashlib.sha256(data).hexdigest(),
        width=background.width,
        height=background.height,
        source_sha256=source_sha256,
    )


async def _read_cached_preview(
    store: ObjectStore,
    *,
    key: str,
    asset_id: UUID,
    source_sha256: str,
) -> DashboardPreview | None:
    metadata = await store.head(key)
    if metadata is None:
        return None
    expected_metadata = {
        "kind": "dashboard-preview",
        "preview-version": DASHBOARD_PREVIEW_VERSION,
        "source-asset-id": str(asset_id),
        "source-sha256": source_sha256,
    }
    if (
        metadata.content_type != DASHBOARD_PREVIEW_CONTENT_TYPE
        or metadata.byte_size <= 0
        or metadata.byte_size > DASHBOARD_PREVIEW_MAX_BYTES
        or any(metadata.metadata.get(name) != value for name, value in expected_metadata.items())
    ):
        raise DashboardPreviewConflictError("stored dashboard preview is invalid")
    try:
        width = int(metadata.metadata.get("width", ""))
        height = int(metadata.metadata.get("height", ""))
    except ValueError:
        raise DashboardPreviewConflictError("stored dashboard preview is invalid") from None
    if not 1 <= width <= DASHBOARD_PREVIEW_EDGE or not 1 <= height <= DASHBOARD_PREVIEW_EDGE:
        raise DashboardPreviewConflictError("stored dashboard preview is invalid")
    data = await store.read_bytes(
        key,
        max_bytes=DASHBOARD_PREVIEW_MAX_BYTES,
        version_id=metadata.version_id,
        etag=metadata.etag,
    )
    sha256 = hashlib.sha256(data).hexdigest()
    if len(data) != metadata.byte_size or metadata.metadata.get("sha256") != sha256:
        raise DashboardPreviewConflictError("stored dashboard preview identity changed")
    try:
        require_metadata_free_image(data, content_type=DASHBOARD_PREVIEW_CONTENT_TYPE)
    except OutboundImagePrivacyError as error:
        raise DashboardPreviewConflictError("stored dashboard preview contains metadata") from error
    return DashboardPreview(
        data=data,
        sha256=sha256,
        width=width,
        height=height,
        source_sha256=source_sha256,
    )


def _preview_object_key(*, asset_id: UUID, source_sha256: str) -> str:
    return f"dashboard-previews/v1/{asset_id}/{source_sha256}.jpg"


def _source_sha256(value: str) -> str:
    normalized = value.strip().lower() if isinstance(value, str) else ""
    if not _SHA256.fullmatch(normalized):
        raise DashboardPreviewConflictError("raw master checksum is invalid")
    return normalized
