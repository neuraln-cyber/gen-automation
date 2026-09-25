"""Original-pixel sizing contracts; synthetic media, no models or provider jobs."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi import HTTPException
from PIL import Image
from pydantic import ValidationError

from gen_automation.api.routes.i2v import _validate_generation_profile
from gen_automation.config import Settings
from gen_automation.i2v_worker import media
from gen_automation.i2v_worker.models import GenerationSettings, source_resolution_canvas
from gen_automation.services.i2v_runtime import _worker_settings_snapshot
from tests.test_i2v_h3_readiness import h3_settings

ROOT = Path(__file__).parents[1]


def test_original_resolution_uses_padded_native_canvas_not_legacy_sizing():
    requested = h3_settings(match_source_resolution=True, match_source_aspect=True)
    resolved = media.resolve_generation_settings(requested, source_width=1144, source_height=1480)
    assert (resolved.width, resolved.height) == (1152, 1504)
    assert resolved.match_source_resolution
    assert (requested.width, requested.height) == (576, 1024)
    with pytest.raises(ValidationError):
        h3_settings(width=1152, height=1504)  # existing standard mode stays unchanged
    with pytest.raises(ValidationError):
        GenerationSettings(match_source_resolution=True)


@pytest.mark.parametrize("size", [(1145, 1480), (1144, 1481), (32, 2050), (16, 32)])
def test_unencodable_original_sizes_fail_explicitly(size):
    with pytest.raises(ValueError):
        source_resolution_canvas(*size)


@pytest.mark.parametrize("size", [(1144, 1480), (1150, 1502), (1152, 1504), (1480, 1144)])
def test_padding_retains_every_original_pixel(tmp_path, size):
    source = tmp_path / "source.png"
    prepared = tmp_path / "prepared.png"
    pixels = bytes(range(256)) * ((size[0] * size[1] * 3 // 256) + 1)
    image = Image.frombytes("RGB", size, pixels[: size[0] * size[1] * 3])
    image.save(source)
    width, height = source_resolution_canvas(*size)
    media.prepare_input_image(
        source, prepared, width=width, height=height, preserve_source_resolution=True
    )
    left, top = (width - size[0]) // 4 * 2, (height - size[1]) // 4 * 2
    with Image.open(prepared) as result:
        assert result.size == (width, height)
        assert result.crop((left, top, left + size[0], top + size[1])).tobytes() == image.tobytes()


def test_source_resolution_requires_explicit_matching_worker_gate():
    configured = Settings.model_construct(i2v_profile="minimax_h3")
    value = h3_settings(match_source_resolution=True).model_dump()
    with pytest.raises(HTTPException, match="matching H3 worker"):
        _validate_generation_profile(configured, value, "")
    configured.i2v_h3_source_resolution_enabled = True
    _validate_generation_profile(configured, value, "")
    assert "match_source_resolution" not in _worker_settings_snapshot(
        {"profile": "minimax_h3", "match_source_resolution": False}
    )
    assert _worker_settings_snapshot(value)["match_source_resolution"] is True


def test_dashboard_resolution_gate_and_label(client):
    client.app.state.settings.i2v_profile = "minimax_h3"
    page = client.get("/dashboard/animations").text
    assert 'name="match_source_resolution" disabled' in page
    client.app.state.settings.i2v_h3_source_resolution_enabled = True
    page = client.get("/dashboard/animations").text
    assert 'name="match_source_resolution">' in page
    assert "Original image resolution" in page
    assert "data-resolution-summary" in page


def test_frontend_reports_exact_output_and_model_canvas():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for browser sizing contracts")
    script = r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync("src/gen_automation/static/i2v.js", "utf8");
const elements = {
  match_source_resolution: {checked: true}, match_source_aspect: {checked: true},
  width: {}, height: {},
};
const summary = {};
const state = {selected: {sourceWidth: 1144, sourceHeight: 1480}};
const context = vm.createContext({
  form: {elements}, isH3: true, sourceResolutionEnabled: true, state, q: () => summary,
});
vm.runInContext(source.slice(source.indexOf("  function sourceNativeDimensions("),
  source.indexOf("  function setLoraSelections(")), context);
vm.runInContext("syncAspectControls()", context);
assert.equal(elements.width.value, "1152");
assert.equal(elements.height.value, "1504");
assert.equal(elements.width.disabled, true);
assert.equal(elements.match_source_aspect.checked, false);
assert.match(summary.textContent, /Output: 1144 \u00d7 1480/);
assert.match(summary.textContent, /1152 \u00d7 1504/);
state.selected.sourceWidth = 1145;
assert.match(vm.runInContext("sourceResolutionError()", context), /even width/);
state.selected.sourceWidth = 1144;
context.sourceResolutionEnabled = false;
assert.match(vm.runInContext("sourceResolutionError()", context), /worker update/);
"""
    subprocess.run([node, "-e", script], cwd=ROOT, check=True, capture_output=True, text=True)  # noqa: S603


def test_real_h3_delivery_is_1144_by_1480_and_retains_audio(tmp_path):
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("FFmpeg is required for the synthetic video contract")
    source = tmp_path / "synthetic.mp4"
    subprocess.run(  # noqa: S603
        [
            ffmpeg,
            "-nostdin",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=1152x1504:r=24",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000",
            "-t",
            "5.166667",
            "-frames:v",
            "124",
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
            str(source),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
    settings = h3_settings(width=1152, height=1504, match_source_resolution=True)
    output, metadata = media.finalize_native_video(
        (source,), settings, tmp_path, source_width=1144, source_height=1480
    )
    assert (metadata["width"], metadata["height"]) == (1144, 1480)
    assert (metadata["native_width"], metadata["native_height"]) == (1152, 1504)
    assert metadata["upscale"] == "none"
    assert metadata["source_fit"] == "original_pixels_edge_pad_crop"
    probe = subprocess.run(  # noqa: S603
        [ffprobe, "-v", "error", "-show_entries", "stream=codec_type", "-of", "json", str(output)],
        check=True,
        capture_output=True,
        timeout=15,
    )
    assert {stream["codec_type"] for stream in json.loads(probe.stdout)["streams"]} == {
        "video",
        "audio",
    }
