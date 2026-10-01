"""Eros-specific author recipe, native scheduler composition and rollout gates."""

import json
import shutil
import subprocess
from pathlib import Path
from uuid import UUID

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from gen_automation.api.routes.i2v import _validate_generation_profile
from gen_automation.i2v_worker.h3_upscale import H3_UPSCALER_FILENAME, H3_UPSCALER_ROLE
from gen_automation.i2v_worker.h3_variants import H3_EROS_MODELS
from gen_automation.i2v_worker.models import GenerationSettings
from gen_automation.i2v_worker.workflow import load_workflow_template, render_workflow
from gen_automation.services.i2v_runtime import _worker_settings_snapshot

ROOT = Path(__file__).parents[1]


def settings(**kwargs):
    return GenerationSettings(profile="minimax_h3", h3_model_variant="eros_beta5", **kwargs)


def graph(**kwargs):
    value = settings(**kwargs)
    paths = {role: identity[2] for role, identity in H3_EROS_MODELS.items()}
    paths[H3_UPSCALER_ROLE] = H3_UPSCALER_FILENAME
    return render_workflow(
        load_workflow_template(
            ROOT / "workflows/minimax-h3-eros-ref2va.api.json", profile="minimax_h3"
        ),
        input_filename="source.png",
        positive_prompt="<Picture 1> moves.",
        negative_prompt="",
        settings=value,
        job_id=UUID(int=1),
        attempt_id=UUID(int=2),
        model_paths=paths,
    )[0]


@pytest.mark.parametrize("steps,denoise", [(4, 1), (6, 1), (8, 0.5), (6, 0.37), (6, 0)])
def test_beta57_is_exact_native_schedule_not_stock_beta(steps, denoise):
    g = graph(steps=steps, scheduler="beta57", sampler="er_sde", h3_denoise=denoise)
    if denoise == 0:
        assert g["11"]["class_type"] == "BasicScheduler"
        assert g["11"]["inputs"]["denoise"] == 0
        return
    total = int(steps / denoise)
    assert g["11"] == {
        "class_type": "BetaSamplingScheduler",
        "inputs": {"model": ["7", 0], "steps": total, "alpha": 0.5, "beta": 0.7},
    }
    if total > steps:
        assert g["11-tail"]["inputs"] == {"sigmas": ["11", 0], "step": total - steps}
        assert g["12"]["inputs"]["sigmas"] == ["11-tail", 1]
    else:
        assert g["12"]["inputs"]["sigmas"] == ["11", 0]
    assert g["6"]["class_type"] == "MiniMaxH3ReferenceToVideo"
    assert "h3-first-frame" not in g


def test_backend_after_loras_and_same_model_for_both_passes():
    g = graph(
        h3_attention_backend="comfy_kitchen",
        steps=6,
        scheduler="beta57",
        h3_loras=[{"artifact_id": UUID(int=3), "sha256": "a" * 64, "strength": 0.9}],
        width=1152,
        height=1504,
        match_source_resolution=True,
        h3_refine_scheduler="beta57",
        h3_refine_steps=4,
        h3_refine_denoise=0.2,
    )
    assert g["h3-lora-0"]["inputs"]["strength_model"] == 0.9
    assert g["h3-kitchen"]["inputs"] == {
        "model": ["h3-lora-0", 0],
        "attention": "comfy kitchen attention",
    }
    assert g["7"]["inputs"]["model"] == ["h3-kitchen", 0]
    assert g["9"]["inputs"]["model"] == g["h3-source-upscale"]["inputs"]["model"] == ["7", 0]
    assert g["h3-refine-sigmas"]["inputs"]["steps"] == 20
    assert g["h3-refine-sigmas-tail"]["inputs"]["step"] == 16
    assert g["h3-source-upscale"]["inputs"]["sigmas"] == ["h3-refine-sigmas-tail", 1]


def test_historical_settings_remain_unchanged():
    old = settings().model_dump(mode="json")
    payload = _worker_settings_snapshot(old)
    assert "h3_attention_backend" not in payload
    parsed = GenerationSettings.model_validate(payload)
    assert (parsed.steps, parsed.sampler, parsed.scheduler) == (8, "res_multistep", "simple")
    assert parsed.h3_attention_backend == "default"
    assert "h3-kitchen" not in graph()
    new = settings(h3_attention_backend="comfy_kitchen", scheduler="beta57", steps=6)
    assert GenerationSettings.model_validate(_worker_settings_snapshot(new.model_dump())) == new


@pytest.mark.parametrize("variant", ["fl2va_int8", "hybrid_v2", "turbo_v2"])
@pytest.mark.parametrize(
    "extra",
    [
        {"scheduler": "beta57"},
        {"h3_refine_scheduler": "beta57"},
        {"h3_attention_backend": "comfy_kitchen"},
    ],
)
def test_recipe_does_not_change_other_models(variant, extra):
    with pytest.raises(ValidationError):
        GenerationSettings(profile="minimax_h3", h3_model_variant=variant, **extra)


def test_beta57_bounds_fail_before_rendering():
    with pytest.raises(ValidationError, match="total schedule"):
        settings(scheduler="beta57", h3_denoise=0.0001)


def test_submission_and_ui_gate_until_matching_worker(client):
    config = client.app.state.settings
    config.i2v_profile = "minimax_h3"
    config.i2v_h3_model_variant = "eros_beta5"
    config.i2v_h3_advanced_sampling_enabled = True
    value = settings(scheduler="beta57", steps=6, h3_attention_backend="comfy_kitchen").model_dump()
    with pytest.raises(HTTPException, match="matching worker"):
        _validate_generation_profile(config, value, "")
    assert 'value="beta57"' not in client.get("/dashboard/animations").text
    config.i2v_h3_eros_author_recipe_enabled = True
    config.i2v_h3_first_frame_enabled = True
    _validate_generation_profile(config, value, "")
    page = client.get("/dashboard/animations").text
    for expected in (
        'name="steps" value="6"',
        'value="er_sde" selected',
        'value="beta57" selected',
        'value="reference" selected',
        'value="comfy_kitchen" selected',
    ):
        assert expected in page
    assert "eros_prompt.js" in page and "Build editable REF2VA prompt" in page
    config.i2v_h3_model_variant = "fl2va_int8"
    page = client.get("/dashboard/animations").text
    assert "eros_prompt.js" not in page and 'value="beta57"' not in page


def test_editable_prompt_scaffold_preserves_user_text_and_structured_prompts():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed for the actual dashboard prompt function")
    source = ROOT / "src/gen_automation/static/eros_prompt.js"
    code = """
const fs = require('fs'), vm = require('vm');
const context = {window: {}};
vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), context);
const build = context.window.buildErosReferencePrompt;
const original = 'The character waves slowly. Keep the camera still.';
const reference = build(original);
const guided = build(original, true);
let rejected = false;
try { build('[integrated_multimodal_description] keep me'); } catch (_) { rejected = true; }
console.log(JSON.stringify({reference, guided,
  preserved: build(reference) === reference, rejected}));
"""
    result = subprocess.run(  # noqa: S603 - fixed local test program; no external input
        [node, "-e", code, str(source)], check=True, capture_output=True, text=True
    )
    value = json.loads(result.stdout)
    assert value["preserved"] and value["rejected"]
    assert "The character waves slowly. Keep the camera still." in value["reference"]
    assert "[reference generation]" in value["reference"]
    assert "[keyframe completion + reference generation]" in value["guided"]
    assert "[integrated_multimodal_description]" not in value["reference"]
    assert "[detailed_description]" in value["reference"]
    assert value["reference"].index("visual style shown") < value["reference"].index(
        "[Shot 1] <Subject"
    )
