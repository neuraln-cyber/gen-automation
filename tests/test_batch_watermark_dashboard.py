import json
import re
from io import BytesIO
from pathlib import Path
from uuid import uuid4
from zipfile import ZipFile

from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from gen_automation.api.routes import batch_watermark_dashboard as batch_routes
from gen_automation.services.watermarks import RegisteredWatermarkPayload
from gen_automation.storage.memory import MemoryObjectStore

DASHBOARD_SCRIPT = Path(__file__).parents[1] / "src" / "gen_automation" / "static" / "dashboard.js"


def _png(color: tuple[int, int, int], size: tuple[int, int]) -> bytes:
    image = Image.new("RGB", size, color)
    output = BytesIO()
    try:
        image.save(output, format="PNG")
        return output.getvalue()
    finally:
        image.close()


def _watermark_png() -> bytes:
    image = Image.new("RGBA", (80, 30), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rectangle((8, 5, 71, 24), fill=(255, 20, 120, 230))
    output = BytesIO()
    try:
        image.save(output, format="PNG")
        return output.getvalue()
    finally:
        image.close()


def _csrf(page: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', page)
    assert match is not None
    return match.group(1)


def _form(
    csrf_token: str,
    *,
    archive_kind: str,
    watermark_asset_id: str = "",
    positions: tuple[str, ...] = ("bottom_right",),
) -> dict[str, str]:
    return {
        "csrf_token": csrf_token,
        "watermark_asset_id": watermark_asset_id,
        "watermark_placements": json.dumps(
            [{"index": index, "position": position} for index, position in enumerate(positions)]
        ),
        "archive_kind": archive_kind,
    }


def test_owner_can_open_transient_batch_watermark_workspace(client: TestClient) -> None:
    response = client.get("/dashboard/watermarking")

    assert response.status_code == 200
    assert "Batch watermarking" in response.text
    assert "data-batch-watermark-files" in response.text
    assert 'data-batch-watermark-download="both"' in response.text
    assert "Nothing is saved to the library" in response.text
    assert 'href="/dashboard/watermarking"' in response.text
    assert "blob:" in response.headers["content-security-policy"]


def test_original_only_zip_keeps_exact_uploaded_bytes(client: TestClient) -> None:
    page = client.get("/dashboard/watermarking")
    first = _png((10, 40, 180), (48, 32))
    second = _png((180, 40, 10), (31, 57))

    response = client.post(
        "/dashboard/watermarking:download",
        data=_form(
            _csrf(page.text),
            archive_kind="originals",
            positions=("top_left", "bottom_right"),
        ),
        files=(
            ("images", ("first image.png", first, "image/png")),
            ("images", ("../second.png", second, "image/png")),
        ),
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["x-accel-buffering"] == "no"
    with ZipFile(BytesIO(response.content)) as archive:
        assert archive.namelist() == [
            "originals/001-first-image.png",
            "originals/002-second.png",
        ]
        assert archive.read("originals/001-first-image.png") == first
        assert archive.read("originals/002-second.png") == second


def test_both_versions_are_full_resolution_with_per_image_corners(
    client: TestClient,
    monkeypatch,
) -> None:
    watermark_id = uuid4()
    watermark = _watermark_png()

    async def read_watermark(*_args, **_kwargs) -> RegisteredWatermarkPayload:
        return RegisteredWatermarkPayload(
            asset_id=watermark_id,
            display_name="Neural Nymphs",
            sha256="a" * 64,
            width=80,
            height=30,
            data=watermark,
        )

    monkeypatch.setattr(batch_routes, "read_registered_watermark", read_watermark)
    client.app.state.object_store = MemoryObjectStore(bucket="batch-watermark-tests")
    page = client.get("/dashboard/watermarking")
    first = _png((10, 40, 180), (120, 80))
    second = _png((180, 40, 10), (75, 135))

    response = client.post(
        "/dashboard/watermarking:download",
        data=_form(
            _csrf(page.text),
            archive_kind="both",
            watermark_asset_id=str(watermark_id),
            positions=("top_left", "bottom_right"),
        ),
        files=(
            ("images", ("first.png", first, "image/png")),
            ("images", ("second.png", second, "image/png")),
        ),
    )

    assert response.status_code == 200
    with ZipFile(BytesIO(response.content)) as archive:
        assert archive.read("originals/001-first.png") == first
        assert archive.read("originals/002-second.png") == second
        first_watermarked = archive.read("watermarked/001-first-watermarked.png")
        second_watermarked = archive.read("watermarked/002-second-watermarked.png")
    with Image.open(BytesIO(first_watermarked)) as first_image:
        assert first_image.size == (120, 80)
    with Image.open(BytesIO(second_watermarked)) as second_image:
        assert second_image.size == (75, 135)


def test_batch_rejects_missing_per_image_placement(client: TestClient) -> None:
    page = client.get("/dashboard/watermarking")
    response = client.post(
        "/dashboard/watermarking:download",
        data=_form(_csrf(page.text), archive_kind="originals", positions=()),
        files=(("images", ("first.png", _png((1, 2, 3), (20, 20)), "image/png")),),
    )

    assert response.status_code == 422
    assert response.json() == {"detail": "Choose a watermark corner for every image."}


def test_batch_download_uses_native_streaming_instead_of_buffering_a_blob() -> None:
    script = DASHBOARD_SCRIPT.read_text(encoding="utf-8")
    start = script.index("const downloadBatch = (archiveKind) =>")
    end = script.index("fileInput.addEventListener", start)
    download = script[start:end]

    assert "form.requestSubmit()" in download
    assert "response.blob()" not in download
    assert "new FormData()" not in download


def test_streaming_zip_buffer_drains_entries_before_archive_closes() -> None:
    sink = batch_routes._StreamingZipBuffer()
    with ZipFile(sink, mode="w") as archive:
        archive.writestr("first.txt", b"first")
        first_chunk = sink.take()
        archive.writestr("second.txt", b"second")
        second_chunk = sink.take()
    final_chunk = sink.take()
    sink.close()

    assert first_chunk.startswith(b"PK\x03\x04")
    assert second_chunk.startswith(b"PK\x03\x04")
    with ZipFile(BytesIO(first_chunk + second_chunk + final_chunk)) as archive:
        assert archive.read("first.txt") == b"first"
        assert archive.read("second.txt") == b"second"
