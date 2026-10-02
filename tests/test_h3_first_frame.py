"""Image guide preparation/cleanup and dashboard controls; no real GPU or jobs."""

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from gen_automation.i2v_worker.app import _run_job
from gen_automation.i2v_worker.comfy import ComfyError
from gen_automation.i2v_worker.h3_upscale import (
    H3_UPSCALER_BYTES,
    H3_UPSCALER_FILENAME,
    H3_UPSCALER_ROLE,
    H3_UPSCALER_SHA256,
)
from gen_automation.i2v_worker.models import I2VJob
from gen_automation.i2v_worker.workflow import load_workflow_template
from tests.test_h3_eros import ROOT, TEMPLATE, eros, objects, worker
from tests.test_i2v_worker_app import _job


@pytest.mark.parametrize("upscale", [False, True])
@pytest.mark.parametrize("fail", [False, True])
@pytest.mark.parametrize("contrast", [1.0, 0.9])
async def test_worker_prepares_distinct_whole_image_guide_and_cleans_it(
    tmp_path, monkeypatch, upscale, fail, contrast
):
    runtime = tmp_path / "runtime"
    items = [
        *objects(),
        dict(
            role=H3_UPSCALER_ROLE,
            bucket="models",
            key=f"worker/i2v/sha256/{H3_UPSCALER_SHA256}",
            version_id="immutable",
            byte_size=H3_UPSCALER_BYTES,
            sha256=H3_UPSCALER_SHA256,
            install_path=f"models/latent_upscale_models/{H3_UPSCALER_FILENAME}",
        ),
    ]
    cfg = worker(items).model_copy(update={"runtime_root": runtime, "environment": "test"})
    raw = _job()
    raw["settings_snapshot"] = eros(
        h3_image_mode="first_frame",
        h3_latent_contrast=contrast,
        match_source_resolution=upscale,
        width=768,
        height=992,
        h3_refine_steps=1,
    ).model_dump(mode="json")
    raw["input_snapshot"].update(width=1144, height=1480)
    job = I2VJob.model_validate(raw)
    source = Image.new("RGB", (1144, 1480), (10, 20, 30))
    # Four colored corner regions make a center-crop/stretch regression visible.
    source.paste((200, 0, 0), (0, 0, 120, 120))
    source.paste((0, 200, 0), (1024, 0, 1144, 120))
    source.paste((0, 0, 200), (0, 1360, 120, 1480))
    source.paste((200, 200, 0), (1024, 1360, 1144, 1480))

    async def download(_grant, _snapshot, destination, **_kwargs):
        destination.parent.mkdir(parents=True, exist_ok=True)
        source.save(destination, format="PNG")

    async def execute(graph, _output):
        with Image.open(runtime / "input" / graph["1"]["inputs"]["image"]) as reference:
            assert reference.size == source.size and reference.tobytes() == source.tobytes()
        with Image.open(runtime / "input" / graph["h3-guide-image"]["inputs"]["image"]) as guide:
            assert guide.size == (768, 992)
            for point, rgb in (
                ((15, 15), (200, 0, 0)),
                ((752, 15), (0, 200, 0)),
                ((15, 976), (0, 0, 200)),
                ((752, 976), (200, 200, 0)),
            ):
                assert guide.getpixel(point) == rgb
        if fail:
            raise ComfyError("test failure")
        return (Path("synthetic.mp4"),)

    def finalize(_frames, settings, directory, **_kwargs):
        path = directory / "out.mp4"
        path.write_bytes(b"fake output")
        return path, dict(
            width=768,
            height=992,
            frame_count=124,
            fps=24,
            duration_ms=5167,
            codec="h264",
            pixel_format="yuv420p",
            faststart=True,
            native_width=768,
            native_height=992,
            upscale="none",
            loop_mode="none",
            loop_count=1,
            source_fit="contain_edge_pad",
            match_source_aspect=False,
        )

    monkeypatch.setattr("gen_automation.i2v_worker.app.download_input", download)
    monkeypatch.setattr("gen_automation.i2v_worker.app.finalize_native_video", finalize)
    monkeypatch.setattr(
        "gen_automation.i2v_worker.app.upload_video", AsyncMock(return_value=("v1", 11, "a" * 64))
    )
    kwargs = dict(
        settings=cfg,
        supervisor=SimpleNamespace(comfy_client=SimpleNamespace(execute=execute)),
        workflow=load_workflow_template(TEMPLATE, profile="minimax_h3"),
    )
    if fail:
        with pytest.raises(ComfyError):
            await _run_job(job, **kwargs)
    else:
        result = await _run_job(job, **kwargs)
        assert result.output.metadata["workflow"] == "minimax-h3-eros-ref2va"
        assert result.output.metadata["h3_image_mode"] == "first_frame"
        assert result.output.metadata["h3_refine_steps"] == 1
        assert result.output.metadata["h3_latent_contrast"] == contrast
        assert result.output.metadata["effective_positive_prompt"] == job.positive_prompt
    assert not list((runtime / "input").iterdir())
    assert not (runtime / "jobs" / str(job.attempt_id)).exists()


def test_visible_prompt_preset_and_legacy_mode_are_not_silent_rewrites():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node required for dashboard behavior check")
    script = r"""
const assert=require("node:assert/strict"), fs=require("node:fs"), vm=require("node:vm");
const source=fs.readFileSync("src/gen_automation/static/i2v.js", "utf8");
const fields={h3_image_mode:{},h3_attention_backend:{},h3_refine_steps:{},seed:{}};
let selected, handler, saved=0, errorMessage;
const context=vm.createContext({window:{},isH3:true,h3ModelVariant:"eros_beta5",CSS:{escape:s=>s},
  workerSettingDefaults:{h3_refine_steps:4,h3_loras:[],seed:-1},
  advanced:{querySelector:s=>fields[s.match(/name="(.+)"/)[1]]},
  setLoraSelections:v=>selected=v,syncAspectControls:()=>{},updateDuration:()=>{},
  q:()=>({addEventListener:(_,fn)=>handler=fn}),
  form:{elements:{positive_prompt:{
    value:"Gentle motion.",maxLength:100000,focus:()=>{}}}},
  syncLoraPromptPreview:()=>{}, scheduleDraftSave:()=>saved++,
  announce:(text,error)=>{if(error)errorMessage=text;}});
vm.runInContext(fs.readFileSync("src/gen_automation/static/eros_prompt.js", "utf8"),context);
vm.runInContext(source.slice(source.indexOf("  function applySettings("),
  source.indexOf("  function sourceNativeDimensions(")),context);
vm.runInContext('applySettings({h3_refine_steps:1,seed:42,h3_loras:[{strength:0.4}]})',context);
assert.equal(fields.h3_image_mode.value,"reference");
assert.equal(fields.h3_attention_backend.value,"default");
assert.equal(fields.h3_refine_steps.value,"1");assert.equal(fields.seed.value,"42");
assert.equal(selected[0].strength,0.4);
vm.runInContext('applySettings({h3_image_mode:"first_frame"})',context);
assert.equal(fields.h3_image_mode.value,"first_frame");
const start=source.indexOf('  q("[data-eros-style-preset]")');
const end=source.indexOf('  form.addEventListener("input"',start);
vm.runInContext(source.slice(start,end),context);
assert.equal(saved,0);handler();
const once=context.form.elements.positive_prompt.value;
assert(once.startsWith("[subject_definitions]"));
assert(once.includes("Gentle motion."));assert(once.includes("<Picture 1>"));
assert(once.includes("[detailed_description]"));
handler();assert.equal(context.form.elements.positive_prompt.value,once);
assert.equal(saved,2);
const partial="[integrated_multimodal_description]\nGentle motion.";
context.form.elements.positive_prompt.value=partial;
handler();assert.equal(context.form.elements.positive_prompt.value,partial);
assert.equal(saved,2);assert(errorMessage.includes("not rewritten"));
"""
    subprocess.run([node, "-e", script], cwd=ROOT, check=True, capture_output=True, text=True)  # noqa: S603
