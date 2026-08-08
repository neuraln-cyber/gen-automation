import hashlib
import zlib
from datetime import UTC, datetime
from io import BytesIO
from uuid import uuid4

import pytest
from PIL import Image, PngImagePlugin

from gen_automation.db.models import PublicationInput
from gen_automation.services import finished_set_archives as archives
from gen_automation.services import publication_runtime
from gen_automation.services.outbound_image_privacy import (
    OutboundImagePrivacyError,
    require_metadata_free_image,
)
from gen_automation.storage.memory import MemoryObjectStore, StoredObject


def _encode(image_format: str, **options: object) -> bytes:
    output = BytesIO()
    with Image.new("RGB", (8, 6), color=(12, 34, 56)) as image:
        image.save(output, format=image_format, **options)
    return output.getvalue()


def _private_exif() -> Image.Exif:
    exif = Image.Exif()
    exif[0x010E] = "private prompt"
    return exif


def _png_with_text() -> bytes:
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("prompt", "private prompt")
    return _encode("PNG", pnginfo=metadata)


def _jpeg_with_comment() -> bytes:
    clean = _encode("JPEG")
    comment = b"private-comment"
    segment = b"\xff\xfe" + (len(comment) + 2).to_bytes(2, "big") + comment
    return clean[:2] + segment + clean[2:]


def _png_chunk(chunk_type: bytes, body: bytes) -> bytes:
    checksum = zlib.crc32(chunk_type + body) & 0xFFFFFFFF
    return len(body).to_bytes(4, "big") + chunk_type + body + checksum.to_bytes(4, "big")


def _png_with_unused_palette() -> bytes:
    clean = _encode("PNG")
    ihdr_end = 8 + 12 + 13
    palette = b"private prompt".ljust(768, b"\x00")
    return clean[:ihdr_end] + _png_chunk(b"PLTE", palette) + clean[ihdr_end:]


def _jpeg_with_duplicate_jfif() -> bytes:
    clean = _encode("JPEG")
    first_size = int.from_bytes(clean[4:6], "big")
    first_end = 4 + first_size
    return clean[:first_end] + clean[2:first_end] + clean[first_end:]


def _webp_with_reserved_extended_bytes() -> bytes:
    clean = _encode("WEBP", lossless=True)
    extended = _webp_chunk(
        b"VP8X",
        b"\x00ABC" + (7).to_bytes(3, "little") + (5).to_bytes(3, "little"),
    )
    body = extended + clean[12:]
    return b"RIFF" + (len(body) + 4).to_bytes(4, "little") + b"WEBP" + body


def _webp_chunk(chunk_type: bytes, body: bytes) -> bytes:
    padding = b"\x00" if len(body) % 2 else b""
    return chunk_type + len(body).to_bytes(4, "little") + body + padding


@pytest.mark.parametrize(
    ("image_format", "content_type", "options"),
    (
        ("PNG", "image/png", {}),
        ("JPEG", "image/jpeg", {}),
        ("WEBP", "image/webp", {"lossless": True}),
    ),
)
def test_clean_static_rasters_pass_privacy_check(
    image_format: str,
    content_type: str,
    options: dict[str, object],
) -> None:
    require_metadata_free_image(
        _encode(image_format, **options),
        content_type=content_type,
    )


def test_png_text_exif_and_icc_metadata_fail_privacy_check() -> None:
    payloads = (
        _png_with_text(),
        _encode("PNG", exif=_private_exif()),
        _encode("PNG", icc_profile=b"private-icc-profile"),
    )

    for payload in payloads:
        with pytest.raises(OutboundImagePrivacyError, match="metadata"):
            require_metadata_free_image(payload, content_type="image/png")


def test_jpeg_exif_comment_and_icc_metadata_fail_privacy_check() -> None:
    payloads = (
        _encode("JPEG", exif=_private_exif()),
        _jpeg_with_comment(),
        _encode("JPEG", icc_profile=b"private-icc-profile"),
    )

    for payload in payloads:
        with pytest.raises(OutboundImagePrivacyError, match="metadata"):
            require_metadata_free_image(payload, content_type="image/jpeg")


def test_webp_exif_xmp_and_icc_metadata_fail_privacy_check() -> None:
    payloads = (
        _encode("WEBP", lossless=True, exif=_private_exif()),
        _encode("WEBP", lossless=True, xmp=b"private-xmp"),
        _encode("WEBP", lossless=True, icc_profile=b"private-icc-profile"),
    )

    for payload in payloads:
        with pytest.raises(OutboundImagePrivacyError, match="metadata"):
            require_metadata_free_image(payload, content_type="image/webp")


@pytest.mark.parametrize(
    ("payload", "content_type"),
    (
        (_png_with_unused_palette(), "image/png"),
        (_jpeg_with_duplicate_jfif(), "image/jpeg"),
        (_webp_with_reserved_extended_bytes(), "image/webp"),
    ),
)
def test_optional_container_fields_cannot_carry_private_bytes(
    payload: bytes,
    content_type: str,
) -> None:
    with Image.open(BytesIO(payload)) as image:
        image.load()
        assert image.size == (8, 6)
    with pytest.raises(OutboundImagePrivacyError):
        require_metadata_free_image(payload, content_type=content_type)


@pytest.mark.parametrize(
    ("payload", "content_type"),
    (
        (b"", "image/png"),
        (b"not-a-png", "image/png"),
        (b"\xff\xd8\xff\xd9", "image/jpeg"),
        (b"RIFF\x04\x00\x00\x00WEBP", "image/webp"),
        (_encode("PNG"), "image/gif"),
    ),
)
def test_unsupported_and_malformed_images_fail_closed(
    payload: bytes,
    content_type: str,
) -> None:
    with pytest.raises(OutboundImagePrivacyError):
        require_metadata_free_image(payload, content_type=content_type)


@pytest.mark.asyncio
async def test_finished_set_archive_rejects_metadata_bearing_derivative() -> None:
    body = _png_with_text()
    store = MemoryObjectStore(bucket="privacy-boundary")
    key = "full/private.png"
    version_id = "private-version"
    store.objects[key] = StoredObject(
        body=body,
        content_type="image/png",
        metadata={"sha256": hashlib.sha256(body).hexdigest()},
        version_id=version_id,
    )
    output = archives._OutputRecord(
        ordinal=1,
        generation_ordinal=0,
        generation_job_id=uuid4(),
        generation_queue_position=1,
        source_output_index=0,
        review_display_order=1,
        ranking_rank=1,
        selection_id=uuid4(),
        output_id=uuid4(),
        object_key=key,
        object_version_id=version_id,
        sha256=hashlib.sha256(body).hexdigest(),
        content_type="image/png",
        image_format="PNG",
        width=8,
        height=6,
        byte_size=len(body),
        path="content/001.png",
    )
    manifest = b'{"schema":"finished-set-manifest/v1"}'
    plan = archives._ArchivePlan(
        archive_id=uuid4(),
        review_task_id=uuid4(),
        release_version_id=uuid4(),
        selection_count=1,
        outputs=(output,),
        manifest=manifest,
        manifest_sha256=hashlib.sha256(manifest).hexdigest(),
    )

    with pytest.raises(archives._FinishedSetArchiveContractError, match="embedded metadata"):
        await archives._build_part(
            store,
            plan=plan,
            outputs=(output,),
            part_number=1,
            part_count=1,
            max_archive_bytes=1024 * 1024,
        )


@pytest.mark.asyncio
async def test_publication_runtime_rejects_metadata_bearing_derivative() -> None:
    body = _png_with_text()
    store = MemoryObjectStore(bucket="privacy-publication")
    key = "derivatives/private.png"
    version_id = "private-version"
    store.objects[key] = StoredObject(
        body=body,
        content_type="image/png",
        metadata={"sha256": hashlib.sha256(body).hexdigest()},
        version_id=version_id,
    )
    publication_input = PublicationInput(
        id=uuid4(),
        intent_id=uuid4(),
        ordinal=1,
        role="x_teaser",
        derivative_output_id=uuid4(),
        derivative_recipe_id=uuid4(),
        asset_id=uuid4(),
        derivative_target="x_teaser",
        asset_storage_backend=store.backend,
        asset_storage_bucket=store.bucket,
        asset_object_key=key,
        asset_object_version_id=version_id,
        asset_sha256=hashlib.sha256(body).hexdigest(),
        asset_content_type="image/png",
        asset_image_format="PNG",
        asset_width=8,
        asset_height=6,
        asset_byte_size=len(body),
        frozen_at=datetime(2026, 8, 8, 12, tzinfo=UTC),
    )

    with pytest.raises(
        publication_runtime.PublicationRuntimeContractError,
        match="embedded image metadata",
    ):
        await publication_runtime._read_exact_input(store, publication_input)
