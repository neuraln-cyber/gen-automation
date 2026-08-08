from __future__ import annotations

import zlib
from io import BytesIO

from PIL import Image

PRIVATE_MASTER_PROMPT = "private prompt: raw master only"

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_PNG_DELIVERY_CHUNKS = frozenset({b"IHDR", b"IDAT", b"IEND"})
_WEBP_DELIVERY_CHUNKS = frozenset({b"VP8 ", b"VP8L", b"VP8X", b"ALPH"})


def assert_private_master_metadata_present(data: bytes) -> None:
    with Image.open(BytesIO(data)) as image:
        image.load()
        assert image.format == "PNG"
        assert image.info["prompt"] == PRIVATE_MASTER_PROMPT
        assert image.info["Software"] == "/private/generator/workstation"


def assert_delivery_metadata_absent(data: bytes) -> None:
    with Image.open(BytesIO(data)) as image:
        image.load()
        image_format = image.format
        assert not image.getexif()
        text_metadata = getattr(image, "text", None)
        assert not isinstance(text_metadata, dict) or not text_metadata

    if image_format == "PNG":
        _assert_png_delivery_chunks(data)
    elif image_format == "JPEG":
        _assert_jpeg_delivery_segments(data)
    elif image_format == "WEBP":
        _assert_webp_delivery_chunks(data)
    else:
        raise AssertionError(f"unsupported delivery image format: {image_format}")


def _assert_png_delivery_chunks(data: bytes) -> None:
    assert data.startswith(_PNG_SIGNATURE)
    offset = len(_PNG_SIGNATURE)
    chunk_types: list[bytes] = []
    while offset < len(data):
        assert offset + 12 <= len(data)
        byte_size = int.from_bytes(data[offset : offset + 4], "big")
        chunk_type = data[offset + 4 : offset + 8]
        payload_end = offset + 8 + byte_size
        chunk_end = payload_end + 4
        assert chunk_end <= len(data)
        expected_crc = int.from_bytes(data[payload_end:chunk_end], "big")
        assert zlib.crc32(data[offset + 4 : payload_end]) & 0xFFFFFFFF == expected_crc
        assert chunk_type in _PNG_DELIVERY_CHUNKS
        if chunk_type == b"IHDR":
            assert byte_size == 13
            assert data[offset + 16 : payload_end] == b"\x08\x02\x00\x00\x00"
        chunk_types.append(chunk_type)
        offset = chunk_end
        if chunk_type == b"IEND":
            assert byte_size == 0
            assert offset == len(data)
            break
    assert chunk_types[0] == b"IHDR"
    assert b"IDAT" in chunk_types
    assert chunk_types[-1] == b"IEND"


def _assert_jpeg_delivery_segments(data: bytes) -> None:
    assert data.startswith(b"\xff\xd8")
    offset = 2
    app0_count = 0
    while offset < len(data):
        assert data[offset] == 0xFF
        while offset < len(data) and data[offset] == 0xFF:
            offset += 1
        assert offset < len(data)
        marker = data[offset]
        offset += 1
        if marker == 0xD9:
            assert app0_count == 1
            assert offset == len(data)
            return
        assert marker not in {*range(0xD0, 0xD8), 0xD8, 0x01}
        assert offset + 2 <= len(data)
        segment_size = int.from_bytes(data[offset : offset + 2], "big")
        assert segment_size >= 2
        payload_start = offset + 2
        segment_end = offset + segment_size
        assert segment_end <= len(data)
        payload = data[payload_start:segment_end]
        if marker == 0xE0:
            app0_count += 1
            assert app0_count == 1
            assert payload == b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
        else:
            assert marker not in range(0xE0, 0xF0)
            assert marker != 0xFE
        offset = segment_end
        if marker == 0xDA:
            assert app0_count == 1
            _assert_jpeg_scan_ends_cleanly(data, offset)
            return
    raise AssertionError("JPEG is missing an end-of-image marker")


def _assert_jpeg_scan_ends_cleanly(data: bytes, offset: int) -> None:
    while offset < len(data):
        if data[offset] != 0xFF:
            offset += 1
            continue
        while offset < len(data) and data[offset] == 0xFF:
            offset += 1
        assert offset < len(data)
        marker = data[offset]
        offset += 1
        if marker == 0x00 or marker in range(0xD0, 0xD8):
            continue
        assert marker == 0xD9
        assert offset == len(data)
        return
    raise AssertionError("JPEG scan is missing an end-of-image marker")


def _assert_webp_delivery_chunks(data: bytes) -> None:
    assert data[:4] == b"RIFF"
    assert data[8:12] == b"WEBP"
    assert int.from_bytes(data[4:8], "little") + 8 == len(data)
    offset = 12
    chunk_types: list[bytes] = []
    while offset < len(data):
        assert offset + 8 <= len(data)
        chunk_type = data[offset : offset + 4]
        byte_size = int.from_bytes(data[offset + 4 : offset + 8], "little")
        payload_start = offset + 8
        payload_end = payload_start + byte_size
        assert payload_end <= len(data)
        assert chunk_type in _WEBP_DELIVERY_CHUNKS
        if chunk_type == b"VP8X":
            assert byte_size == 10
            assert data[payload_start] & ~0x10 == 0
            assert data[payload_start + 1 : payload_start + 4] == b"\x00\x00\x00"
        if byte_size % 2:
            assert data[payload_end] == 0
        chunk_types.append(chunk_type)
        offset = payload_end + (byte_size % 2)
    assert offset == len(data)
    assert tuple(chunk_types) in {
        (b"VP8 ",),
        (b"VP8L",),
        (b"VP8X", b"VP8 "),
        (b"VP8X", b"VP8L"),
        (b"VP8X", b"ALPH", b"VP8 "),
    }
