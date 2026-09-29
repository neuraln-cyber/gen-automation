"""Single-image FL2VA-family graph; native runtime checks also run in the image."""

import copy
import json
from pathlib import Path
from uuid import UUID

import pytest

from gen_automation.i2v_worker.workflow import load_workflow_template, render_workflow
from tests.test_h3_upscale import MODEL_PATHS
from tests.test_i2v_h3_readiness import h3_settings

ROOT = Path(__file__).parents[1]


@pytest.mark.parametrize("upscale", [False, True])
@pytest.mark.parametrize("strengths", [[], [0.9], [0.8, -0.5]])
def test_i2va_uses_one_first_frame_and_creator_structured_builder(upscale, strengths):
    template = load_workflow_template(
        ROOT / "workflows/dasiwa-minimax-h3-i2v-v1.api.json", profile="minimax_h3"
    )
    original = copy.deepcopy(template)
    prompt = 'A "quoted" scene.\nSlow camera motion; 雨. <Picture 1> stays recognizable.'
    selections = [
        {"artifact_id": str(UUID(int=i + 1)), "sha256": str(i + 1) * 64, "strength": weight}
        for i, weight in enumerate(strengths)
    ]
    settings = h3_settings(
        h3_model_variant="hybrid_v2",
        steps=20,
        sampler="res_multistep",
        match_source_resolution=upscale,
        width=1152 if upscale else 768,
        height=1504 if upscale else 992,
        h3_loras=selections,
    )
    snapshot = settings.model_dump()
    graph, _, _ = render_workflow(
        template,
        input_filename="prepared.png",
        positive_prompt=prompt,
        negative_prompt="",
        settings=settings,
        job_id=UUID(int=11),
        attempt_id=UUID(int=12),
        model_paths=MODEL_PATHS,
    )
    director = graph["1"]["inputs"]
    assert director["mode"] == "I2VA"
    assert director["fl2va_model"] == ["h3-torch-settings", 0]
    assert "ref2va_model" not in director and "external_prompt_overwrite" not in director
    assert director["prompt"] == prompt
    assert json.loads(director["builder_state"]) == {
        "version": 1,
        "mode": "I2VA",
        "prompt_mode": "structured",
        "imd": prompt,
        "soundscape": "",
        "music": "N/A",
    }
    assert json.loads(director["timeline_data"])["items"] == [
        {"type": "image", "slot": 0, "value": "prepared.png", "enabled": True}
    ]
    stacks = [n for n in graph.values() if n["class_type"] == "DaSiWa_LTX2LoraLoader"]
    assert len(stacks) == 1 and stacks[0]["inputs"]["model"] == ["1", 5]
    assert [r["str"] for r in json.loads(stacks[0]["inputs"]["stack_data"])] == strengths
    assert graph["6"]["inputs"]["guide"] == ["1", 0]
    assert graph["12"]["inputs"]["latent_image"] == ["6", 1]
    assert graph["2"]["inputs"]["unet_name"] == MODEL_PATHS["diffusion_model"]
    assert graph["10"]["inputs"]["sampler_name"] == "res_multistep"
    assert graph["11"]["inputs"]["steps"] == 20
    assert settings.model_dump() == snapshot and template == original


def test_dashboard_describes_the_single_image_mode_and_author_prompt_format():
    ui = (ROOT / "src/gen_automation/templates/dashboard/i2v.html").read_text(encoding="utf-8")
    assert "FL2VA-family I2VA" in ui
    assert "no last-frame image" in ui
    assert "structured builder adds the first-frame alignment" in ui
    assert "Creator REF2VA mode" not in ui
