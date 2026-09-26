"""Native H3 MP4 export regression: synthetic media only, never a GPU job."""

import copy
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gen_automation.i2v_worker import media
from gen_automation.i2v_worker.app import _run_job
from gen_automation.i2v_worker.models import I2VJob
from gen_automation.i2v_worker.workflow import load_workflow_template
from tests.test_h3_base_video_diagnostic import diagnostic_settings
from tests.test_h3_upscale import MODEL_PATHS, ROOT
from tests.test_i2v_h3_readiness import h3_settings
from tests.test_i2v_worker_app import _job


def _ffmpeg(*args):
    return subprocess.run(  # noqa: S603
        [shutil.which("ffmpeg"), "-nostdin", "-v", "error", *map(str, args)],
        check=True,
        capture_output=True,
        timeout=60,
    )


def _streams(path):
    result = subprocess.run(  # noqa: S603
        [
            shutil.which("ffprobe"),
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,sample_aspect_ratio,width,height,nb_frames,avg_frame_rate",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    return json.loads(result.stdout)["streams"]


@pytest.mark.parametrize("size,source_size", [((768, 992), False), ((1152, 1504), True)])
def test_native_export_normalizes_missing_sar_without_changing_pictures(
    tmp_path, size, source_size
):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg is required for the real native export contract")
    width, height = size
    source = tmp_path / "synthetic.mp4"
    _ffmpeg(
        "-f",
        "lavfi",
        "-i",
        f"color=c=blue:s={width}x{height}:r=24",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=48000",
        "-t",
        "5.166667",
        "-frames:v",
        "124",
        "-vf",
        "setsar=0",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-movflags",
        "+faststart",
        source,
    )
    # Reproduce the live ComfyUI export, not the old fixture's explicit square SAR.
    assert "sample_aspect_ratio" not in _streams(source)[0]
    settings = h3_settings(width=width, height=height, match_source_resolution=source_size)
    with pytest.raises(media.MediaError) as before:
        media._probe_video(source, settings, width=width, height=height, frame_count=124)
    assert before.value.reason == media.MediaFailureReason.PIXEL_ASPECT

    output, metadata = media.finalize_native_video(
        (source,), settings, tmp_path, source_width=width, source_height=height
    )
    streams = _streams(output)
    assert streams[0]["sample_aspect_ratio"] == "1:1"
    assert {s["codec_type"] for s in streams} == {"video", "audio"}
    assert (metadata["width"], metadata["height"], metadata["frame_count"], metadata["fps"]) == (
        width,
        height,
        124,
        24,
    )
    assert metadata["faststart"] is True
    # Decoded pictures are byte-identical: no added resize, encoding loss or GPU pass.
    hashes = [
        _ffmpeg("-i", p, "-map", "0:v:0", "-f", "hash", "-hash", "sha256", "-").stdout
        for p in (source, output)
    ]
    assert hashes[0] == hashes[1]


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("codec_name", "hevc", media.MediaFailureReason.CODEC),
        ("pix_fmt", "yuv444p", media.MediaFailureReason.PIXEL_FORMAT),
        ("width", 992, media.MediaFailureReason.DIMENSIONS),
        ("height", 768, media.MediaFailureReason.DIMENSIONS),
        ("nb_read_frames", "123", media.MediaFailureReason.FRAME_COUNT),
        ("avg_frame_rate", "25/1", media.MediaFailureReason.FPS),
        ("sample_aspect_ratio", "4:3", media.MediaFailureReason.PIXEL_ASPECT),
        ("sample_aspect_ratio", None, media.MediaFailureReason.PIXEL_ASPECT),
    ],
)
def test_export_contract_remains_strict_with_specific_reasons(monkeypatch, field, value, reason):
    stream = dict(
        codec_name="h264",
        pix_fmt="yuv420p",
        width=768,
        height=992,
        nb_read_frames="124",
        avg_frame_rate="24/1",
        sample_aspect_ratio="1:1",
    )
    if value is None:
        stream.pop(field)
    else:
        stream[field] = value
    payload = {"streams": [stream], "format": {"duration": "5.167"}}
    monkeypatch.setattr(
        media.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=json.dumps(payload))
    )
    monkeypatch.setattr(media, "_has_faststart", lambda p: True)
    with pytest.raises(media.MediaError) as failed:
        media._probe_video(Path("unused"), h3_settings(), width=768, height=992, frame_count=124)
    assert failed.value.reason == reason


@pytest.mark.parametrize(
    "stage", ["base_finalization", "base_upload", "final_finalization", "final_upload"]
)
async def test_export_failures_log_only_stage_and_reason_and_cleanup(tmp_path, monkeypatch, stage):
    raw = copy.deepcopy(_job())
    raw["settings_snapshot"] = diagnostic_settings().model_dump(mode="json")
    raw["input_snapshot"].update(width=1144, height=1480)
    raw["base_video_grant"] = {**raw["output_grant"], "object_key": "i2v/output.base.mp4"}
    job = I2VJob.model_validate(raw)
    runtime = tmp_path / "runtime"
    secret = "sensitive prompt https://private.invalid/media?signature=secret"  # noqa: S105
    calls = []

    async def download(_grant, _snapshot, destination, **kwargs):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"source")

    def prepare(_source, destination, **kwargs):
        destination.write_bytes(b"prepared")

    def finalize(paths, settings, root, **kwargs):
        current = "base_finalization" if root.name == "base" else "final_finalization"
        if current == stage:
            raise media.MediaError(secret, reason=media.MediaFailureReason.PIXEL_ASPECT)
        output = root / "video.mp4"
        output.write_bytes(b"video")
        return output, dict(
            width=settings.width, height=settings.height, frame_count=124, fps=24, duration_ms=5167
        )

    async def upload(path, grant, **kwargs):
        current = "base_upload" if path.parent.name == "base" else "final_upload"
        if current == stage:
            raise media.MediaError(secret, reason=media.MediaFailureReason.UPLOAD)
        return "version", 5, "a" * 64

    monkeypatch.setattr("gen_automation.i2v_worker.app.download_input", download)
    monkeypatch.setattr("gen_automation.i2v_worker.app.prepare_input_image", prepare)
    monkeypatch.setattr("gen_automation.i2v_worker.app.finalize_native_video", finalize)
    monkeypatch.setattr("gen_automation.i2v_worker.app.upload_video", upload)
    monkeypatch.setattr(
        "gen_automation.i2v_worker.app._LOGGER.warning",
        lambda message, *args: calls.append((message, args)),
    )
    settings = SimpleNamespace(
        runtime_root=runtime,
        environment="test",
        network_timeout_seconds=5,
        network_attempts=1,
        profile="minimax_h3",
        model_objects=[
            SimpleNamespace(role=k, install_path=f"models/any/{v}") for k, v in MODEL_PATHS.items()
        ],
    )
    with pytest.raises(media.MediaError):
        await _run_job(
            job,
            settings=settings,
            supervisor=SimpleNamespace(
                comfy_client=SimpleNamespace(
                    execute=AsyncMock(return_value=(Path("final.mp4"), Path("base.mp4")))
                )
            ),
            workflow=load_workflow_template(
                ROOT / "workflows/dasiwa-minimax-h3-i2v-v1.api.json", profile="minimax_h3"
            ),
        )
    assert len(calls) == 1 and calls[0][1][0] == stage
    assert calls[0][1][1] in ("video_pixel_aspect_invalid", "output_upload_failed")
    assert secret not in repr(calls)
    assert not (runtime / "jobs" / str(job.attempt_id)).exists()
