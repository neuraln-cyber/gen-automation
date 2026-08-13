from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from gen_automation.i2v_worker import media
from gen_automation.i2v_worker.models import GenerationSettings


def test_source_sized_ping_pong_delivery_is_deterministic(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    frames = tuple(tmp_path / f"source-{index}.png" for index in range(9))
    for index, frame in enumerate(frames):
        frame.write_bytes(bytes([index]))
    settings = GenerationSettings(
        frame_count=9,
        width=768,
        height=992,
        upscale="source",
        loop=True,
        loop_count=2,
    )
    command: tuple[str, ...] | None = None

    def run(received: tuple[str, ...], **_kwargs: Any) -> None:
        nonlocal command
        command = received

    def probe(
        _path: Path,
        _settings: GenerationSettings,
        *,
        width: int,
        height: int,
        frame_count: int,
    ) -> dict[str, Any]:
        assert (width, height, frame_count) == (1144, 1480, 32)
        return {
            "width": width,
            "height": height,
            "frame_count": frame_count,
            "fps": 16.0,
            "duration_ms": 2000,
            "codec": "h264",
            "pixel_format": "yuv420p",
            "faststart": True,
        }

    monkeypatch.setattr(media.subprocess, "run", run)
    monkeypatch.setattr(media, "_probe_video", probe)

    _output, metadata = media.encode_video(
        frames,
        settings,
        tmp_path / "job",
        source_width=1144,
        source_height=1480,
    )

    assert command is not None
    assert command[command.index("-frames:v") + 1] == "32"
    assert command[command.index("-vf") + 1] == (
        "scale=1144:1480:flags=lanczos:force_original_aspect_ratio=increase,crop=1144:1480,setsar=1"
    )
    assert metadata["native_width"] == 768
    assert metadata["native_height"] == 992
    assert metadata["loop_mode"] == "ping_pong"
    assert metadata["loop_count"] == 2
    assert metadata["source_fit"] == "contain_edge_pad"
    materialized = tuple(sorted((tmp_path / "job/frames").glob("*.png")))
    assert [item.read_bytes()[0] for item in materialized] == [
        0,
        1,
        2,
        3,
        4,
        5,
        6,
        7,
        8,
        7,
        6,
        5,
        4,
        3,
        2,
        1,
    ] * 2


def test_native_non_loop_delivery_keeps_the_generation_contract() -> None:
    settings = GenerationSettings(frame_count=9, width=768, height=992)

    assert media._output_dimensions(settings, source_width=1144, source_height=1480) == (
        768,
        992,
    )
    assert media._output_frame_indices(settings) == tuple(range(9))


def test_source_delivery_rounds_only_odd_yuv420p_dimensions() -> None:
    settings = GenerationSettings(upscale="source")

    assert media._output_dimensions(settings, source_width=1145, source_height=1481) == (
        1144,
        1480,
    )
    assert media._output_dimensions(settings, source_width=1920, source_height=1080) == (
        1920,
        1080,
    )


def test_source_delivery_filter_preserves_aspect_and_removes_only_padding() -> None:
    assert media._delivery_scale_filter(
        native_width=768,
        native_height=992,
        target_width=1144,
        target_height=1480,
    ) == (
        "scale=1144:1480:flags=lanczos:force_original_aspect_ratio=increase,crop=1144:1480,setsar=1"
    )
    assert (
        media._delivery_scale_filter(
            native_width=1024,
            native_height=576,
            target_width=1920,
            target_height=1080,
        )
        == "scale=1920:1080:flags=lanczos:force_original_aspect_ratio=increase,"
        "crop=1920:1080,setsar=1"
    )
    assert (
        media._delivery_scale_filter(
            native_width=1024,
            native_height=576,
            target_width=1024,
            target_height=576,
        )
        is None
    )


def test_automatic_native_shape_matches_landscape_and_portrait_sources() -> None:
    automatic = GenerationSettings(match_source_aspect=True)

    landscape = media.resolve_generation_settings(
        automatic,
        source_width=1920,
        source_height=1080,
    )
    portrait = media.resolve_generation_settings(
        automatic,
        source_width=1144,
        source_height=1480,
    )

    assert (landscape.width, landscape.height) == (1024, 576)
    assert (portrait.width, portrait.height) == (768, 992)
    assert 520_000 <= landscape.width * landscape.height <= 830_000
    assert 520_000 <= portrait.width * portrait.height <= 830_000


def test_input_preparation_contains_every_corner_without_cropping(tmp_path: Path) -> None:
    source = tmp_path / "source.png"
    destination = tmp_path / "prepared.png"
    image = Image.new("RGB", (286, 370), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 30, 30), fill="red")
    draw.rectangle((255, 0, 285, 30), fill="green")
    draw.rectangle((0, 339, 30, 369), fill="blue")
    draw.rectangle((255, 339, 285, 369), fill="yellow")
    image.save(source)

    media.prepare_input_image(source, destination, width=768, height=992)

    with Image.open(destination) as prepared:
        assert prepared.size == (768, 992)
        colors = prepared.convert("RGB").getcolors(maxcolors=768 * 992)
    assert colors is not None
    observed = {color for _count, color in colors}
    assert (255, 0, 0) in observed
    assert (0, 128, 0) in observed
    assert (0, 0, 255) in observed
    assert (255, 255, 0) in observed
