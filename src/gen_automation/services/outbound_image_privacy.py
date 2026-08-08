"""Container-level privacy checks for externally delivered raster images.

The derivative renderer already creates new pixels-only JPEG and PNG files.
These checks make that behavior an enforced delivery contract: prompt text,
generation settings, EXIF, XMP, ICC profiles, comments, thumbnails, and other
ancillary chunks may remain on private raw masters, but cannot cross an
outbound delivery boundary.

This is a standardized metadata and canonical-container check, not a
steganography detector. Its trust anchor is the fresh internal RGB encoder plus
the immutable derivative lineage; the parser prevents later code from adding
metadata-bearing container fields before delivery.
"""

from __future__ import annotations

import zlib
from collections.abc import Callable


class OutboundImagePrivacyError(ValueError):
    """An outward-facing image contains metadata or has an invalid container."""


def require_metadata_free_image(payload: bytes, *, content_type: str) -> None:
    """Reject raster bytes that contain embedded metadata.

    This is deliberately a container parser rather than a pixel decode. It is
    cheap enough to run both when a derivative is created and immediately
    before it is packaged or uploaded.
    """

    if not isinstance(payload, bytes) or not payload:
        raise OutboundImagePrivacyError("outbound image bytes are invalid")
    validators: dict[str, Callable[[bytes], None]] = {
        "image/jpeg": _require_metadata_free_jpeg,
        "image/png": _require_metadata_free_png,
        "image/webp": _require_metadata_free_webp,
    }
    validator = (
        validators.get(content_type.strip().lower()) if isinstance(content_type, str) else None
    )
    if validator is None:
        raise OutboundImagePrivacyError("outbound image content type is unsupported")
    validator(payload)


def _require_metadata_free_png(payload: bytes) -> None:
    if not payload.startswith(b"\x89PNG\r\n\x1a\n"):
        raise OutboundImagePrivacyError("outbound PNG signature is invalid")
    # The renderer always emits a fresh, 8-bit, truecolor RGB PNG. Restricting
    # the file to that canonical shape prevents optional chunks from becoming
    # an arbitrary private-data carrier.
    pixel_chunks = {b"IHDR", b"IDAT", b"IEND"}
    offset = 8
    chunk_types: list[bytes] = []
    while offset < len(payload):
        if len(payload) - offset < 12:
            raise OutboundImagePrivacyError("outbound PNG chunk is truncated")
        byte_size = int.from_bytes(payload[offset : offset + 4], "big")
        chunk_type = payload[offset + 4 : offset + 8]
        chunk_end = offset + 12 + byte_size
        if chunk_end > len(payload):
            raise OutboundImagePrivacyError("outbound PNG chunk is truncated")
        if chunk_type not in pixel_chunks:
            raise OutboundImagePrivacyError("outbound PNG contains embedded metadata")
        chunk_data = payload[offset + 8 : offset + 8 + byte_size]
        stored_crc = int.from_bytes(payload[offset + 8 + byte_size : chunk_end], "big")
        if zlib.crc32(chunk_type + chunk_data) & 0xFFFFFFFF != stored_crc:
            raise OutboundImagePrivacyError("outbound PNG chunk checksum is invalid")
        if chunk_type == b"IHDR" and (
            byte_size != 13
            or int.from_bytes(chunk_data[:4], "big") <= 0
            or int.from_bytes(chunk_data[4:8], "big") <= 0
            or chunk_data[8:] != b"\x08\x02\x00\x00\x00"
        ):
            raise OutboundImagePrivacyError("outbound PNG header is not canonical RGB")
        chunk_types.append(chunk_type)
        offset = chunk_end
        if chunk_type == b"IEND":
            if byte_size != 0 or offset != len(payload):
                raise OutboundImagePrivacyError("outbound PNG ending is invalid")
            break
    if (
        not chunk_types
        or chunk_types[0] != b"IHDR"
        or chunk_types.count(b"IHDR") != 1
        or b"IDAT" not in chunk_types
        or chunk_types[-1] != b"IEND"
    ):
        raise OutboundImagePrivacyError("outbound PNG structure is invalid")


def _require_metadata_free_jpeg(payload: bytes) -> None:
    if len(payload) < 4 or not payload.startswith(b"\xff\xd8"):
        raise OutboundImagePrivacyError("outbound JPEG signature is invalid")
    offset = 2
    saw_scan = False
    saw_jfif = False
    saw_frame = False
    segment_count = 0
    while offset < len(payload):
        was_in_scan = saw_scan
        _marker_start, marker, offset = _jpeg_marker(payload, offset, in_scan=was_in_scan)
        if marker == 0xD9:  # EOI
            if offset != len(payload):
                raise OutboundImagePrivacyError("outbound JPEG has trailing data")
            if not was_in_scan or not saw_frame or not saw_jfif:
                raise OutboundImagePrivacyError("outbound JPEG structure is not canonical")
            return
        if was_in_scan:
            raise OutboundImagePrivacyError("outbound JPEG has multiple or trailing segments")
        if marker == 0xD8:
            raise OutboundImagePrivacyError("outbound JPEG contains a repeated start marker")
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:
            saw_scan = False
            continue
        if len(payload) - offset < 2:
            raise OutboundImagePrivacyError("outbound JPEG segment is truncated")
        segment_size = int.from_bytes(payload[offset : offset + 2], "big")
        if segment_size < 2 or offset + segment_size > len(payload):
            raise OutboundImagePrivacyError("outbound JPEG segment is truncated")
        segment = payload[offset + 2 : offset + segment_size]
        if marker == 0xE0:
            # Pillow emits one standard JFIF header with no embedded thumbnail.
            # It is structural interoperability data, not creator metadata.
            if (
                saw_jfif
                or segment_count != 0
                or segment != b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
            ):
                raise OutboundImagePrivacyError("outbound JPEG contains APP0 metadata")
            saw_jfif = True
        elif 0xE1 <= marker <= 0xEF or marker == 0xFE:
            raise OutboundImagePrivacyError("outbound JPEG contains embedded metadata")
        if marker == 0xC0:
            if saw_frame or len(segment) < 9:
                raise OutboundImagePrivacyError("outbound JPEG frame is invalid")
            saw_frame = True
        elif marker in {
            0xC1,
            0xC2,
            0xC3,
            0xC5,
            0xC6,
            0xC7,
            0xC9,
            0xCA,
            0xCB,
            0xCD,
            0xCE,
            0xCF,
        }:
            raise OutboundImagePrivacyError("outbound JPEG frame is not canonical baseline")
        if marker == 0xDA and (not saw_frame or len(segment) < 6):
            raise OutboundImagePrivacyError("outbound JPEG scan is invalid")
        offset += segment_size
        saw_scan = marker == 0xDA
        segment_count += 1
    raise OutboundImagePrivacyError("outbound JPEG ending is missing")


def _jpeg_marker(payload: bytes, offset: int, *, in_scan: bool) -> tuple[int, int, int]:
    if in_scan:
        while offset < len(payload):
            marker_start = payload.find(b"\xff", offset)
            if marker_start < 0 or marker_start + 1 >= len(payload):
                raise OutboundImagePrivacyError("outbound JPEG scan is truncated")
            marker_offset = marker_start + 1
            while marker_offset < len(payload) and payload[marker_offset] == 0xFF:
                marker_offset += 1
            if marker_offset >= len(payload):
                raise OutboundImagePrivacyError("outbound JPEG scan is truncated")
            marker = payload[marker_offset]
            if marker == 0x00 or 0xD0 <= marker <= 0xD7:
                offset = marker_offset + 1
                continue
            return marker_start, marker, marker_offset + 1
        raise OutboundImagePrivacyError("outbound JPEG scan is truncated")
    if payload[offset] != 0xFF:
        raise OutboundImagePrivacyError("outbound JPEG marker is invalid")
    marker_start = offset
    while offset < len(payload) and payload[offset] == 0xFF:
        offset += 1
    if offset >= len(payload) or payload[offset] in {0x00, 0xFF}:
        raise OutboundImagePrivacyError("outbound JPEG marker is invalid")
    return marker_start, payload[offset], offset + 1


def _require_metadata_free_webp(payload: bytes) -> None:
    if (
        len(payload) < 20
        or not payload.startswith(b"RIFF")
        or payload[8:12] != b"WEBP"
        or int.from_bytes(payload[4:8], "little") + 8 != len(payload)
    ):
        raise OutboundImagePrivacyError("outbound WebP container is invalid")
    pixel_chunks = {b"VP8 ", b"VP8L", b"VP8X", b"ALPH"}
    offset = 12
    chunk_types: list[bytes] = []
    while offset < len(payload):
        if len(payload) - offset < 8:
            raise OutboundImagePrivacyError("outbound WebP chunk is truncated")
        chunk_type = payload[offset : offset + 4]
        byte_size = int.from_bytes(payload[offset + 4 : offset + 8], "little")
        data_start = offset + 8
        data_end = data_start + byte_size
        padded_end = data_end + (byte_size % 2)
        if padded_end > len(payload) or chunk_type not in pixel_chunks:
            raise OutboundImagePrivacyError("outbound WebP contains embedded metadata")
        if chunk_type == b"VP8X":
            flags = payload[data_start] if data_start < data_end else 0xFF
            if (
                byte_size != 10
                or flags & ~0x10
                or payload[data_start + 1 : data_start + 4] != b"\x00\x00\x00"
            ):
                raise OutboundImagePrivacyError("outbound WebP extended metadata is present")
        if byte_size % 2 and payload[data_end] != 0:
            raise OutboundImagePrivacyError("outbound WebP padding contains private data")
        chunk_types.append(chunk_type)
        offset = padded_end
    valid_layouts = {
        (b"VP8 ",),
        (b"VP8L",),
        (b"VP8X", b"VP8 "),
        (b"VP8X", b"VP8L"),
        (b"VP8X", b"ALPH", b"VP8 "),
    }
    if offset != len(payload) or tuple(chunk_types) not in valid_layouts:
        raise OutboundImagePrivacyError("outbound WebP image data is missing")
