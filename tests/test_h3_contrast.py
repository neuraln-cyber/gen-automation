"""Eros output-only contrast and neutral rolling-upgrade compatibility."""

import copy
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from gen_automation.api.routes.i2v import _validate_generation_profile
from gen_automation.i2v_worker.models import GenerationSettings
from gen_automation.i2v_worker.settings import H3_CUSTOM_NODES
from gen_automation.services.i2v_runtime import _worker_settings_snapshot
from tests.test_h3_eros_author import graph, settings

ROOT = Path(__file__).parents[1]


@pytest.mark.parametrize("factor", [0, 0.8, 0.9, 1, 3])
def test_contrast_wire_roundtrip_and_neutral_omission(factor):
    value = settings(h3_latent_contrast=factor)
    frozen = value.model_dump(mode="json")
    before = copy.deepcopy(frozen)
    wire = _worker_settings_snapshot(frozen)
    assert frozen == before
    assert ("h3_latent_contrast" in wire) is (factor != 1)
    assert GenerationSettings.model_validate(wire) == value
    assert settings().h3_latent_contrast == 1


@pytest.mark.parametrize(
    "factor", [-0.01, 3.01, float("inf"), float("nan"), None, "bad", True, "0.9"]
)
def test_invalid_contrast_rejected(factor):
    with pytest.raises(ValidationError):
        settings(h3_latent_contrast=factor)


@pytest.mark.parametrize("variant", [None, "turbo_v2", "hybrid_v2", "fl2va_int8"])
def test_other_models_cannot_use_contrast(variant):
    with pytest.raises(ValidationError, match="requires Eros"):
        GenerationSettings(
            profile="minimax_h3" if variant else "wan22",
            h3_model_variant=variant,
            h3_latent_contrast=0.9,
        )


@pytest.mark.parametrize("upscale,diagnostic", [(False, False), (True, False), (True, True)])
def test_only_video_decode_edges_change_not_sampling_loras_or_audio(upscale, diagnostic):
    kwargs = dict(
        seed=42,
        match_source_resolution=upscale,
        h3_save_base_video=diagnostic,
        width=1152 if upscale else 768,
        height=1504 if upscale else 992,
        steps=6,
        sampler="er_sde",
        scheduler="beta57",
        h3_attention_backend="comfy_kitchen",
    )
    original = graph(**kwargs)
    assert graph(**kwargs, h3_latent_contrast=1) == original
    adjusted = graph(**kwargs, h3_latent_contrast=0.9)
    for decode in ("13", "h3-base-decode") if diagnostic else ("13",):
        contrast = adjusted.pop(f"{decode}-contrast")
        assert contrast == {
            "class_type": "MiniMaxH3LatentContrast",
            "inputs": {
                "samples": original[decode]["inputs"]["samples"],
                "contrast": 0.9,
                "preserve_norm": True,
            },
        }
        assert adjusted[decode]["inputs"]["samples"] == [f"{decode}-contrast", 0]
        adjusted[decode]["inputs"]["samples"] = contrast["inputs"]["samples"]
    assert adjusted == original


def test_gate_and_dashboard_keep_neutral_default(client):
    config = client.app.state.settings
    config.i2v_profile = "minimax_h3"
    config.i2v_h3_model_variant = "eros_beta5"
    config.i2v_h3_advanced_sampling_enabled = True
    value = settings(h3_latent_contrast=0.9).model_dump()
    with pytest.raises(HTTPException, match="matching worker"):
        _validate_generation_profile(config, value, "")
    _validate_generation_profile(config, settings().model_dump(), "")
    page = client.get("/dashboard/animations").text
    assert 'aria-describedby="h3-contrast-help" disabled' in page
    config.i2v_h3_latent_contrast_enabled = True
    _validate_generation_profile(config, value, "")
    page = client.get("/dashboard/animations").text
    assert 'name="h3_latent_contrast" type="number" value="1"' in page
    assert 'aria-describedby="h3-contrast-help" disabled' not in page
    assert "0.8\u20130.9" in page and "not saturation or sharpening" in page
    config.i2v_h3_model_variant = "fl2va_int8"
    assert 'name="h3_latent_contrast"' not in client.get("/dashboard/animations").text


def test_actual_ui_serializes_restores_and_validates_contrast():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node needed to exercise dashboard functions")
    script = r"""
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const src=fs.readFileSync('src/gen_automation/static/i2v.js','utf8');
const field={name:'h3_latent_contrast',type:'number',value:'1',disabled:false};
const ctx=vm.createContext({isH3:true,h3ModelVariant:'eros_beta5',h3AdvancedSamplingEnabled:true,
 workerSettingDefaults:{profile:'minimax_h3',h3_latent_contrast:1},
 state:{loraSelections:new Map(),loraCatalog:[]},loraWriteBlocked:()=>false,
 loraList:{querySelectorAll:()=>[]},root:{querySelector:()=>null},CSS:{escape:x=>x},
 advanced:{querySelector:s=>s.includes('h3_latent_contrast')?field:null,querySelectorAll:()=>[field]},
 sourceResolutionError:()=>'',setLoraSelections:()=>{},syncAspectControls:()=>{},updateDuration:()=>{}});
vm.runInContext(src.slice(src.indexOf('  function collectSettings('),
 src.indexOf('  function sourceNativeDimensions(')),ctx);
for(const value of [0,.8,.9,1,3]){
 ctx.saved={h3_latent_contrast:value};vm.runInContext('applySettings(saved)',ctx);
 assert.equal(field.value,String(value));assert.equal(vm.runInContext('collectSettings()',ctx).h3_latent_contrast,value);
}
vm.runInContext('applySettings({})',ctx);assert.equal(field.value,'1');
for(const value of ['','NaN','Infinity','-1','3.01']){
 field.value=value;assert.throws(()=>vm.runInContext('collectSettings()',ctx),/finite Eros/);
}
field.value='0.9';field.disabled=true;
assert.throws(()=>vm.runInContext('collectSettings()',ctx),/matching worker/);
assert.equal(field.value,'0.9'); // preserve draft, never silently replace by 1
field.value='1';assert.equal(vm.runInContext('collectSettings()',ctx).h3_latent_contrast,1);
"""
    subprocess.run(  # noqa: S603 - fixed local test; no external input
        [node, "-e", script], cwd=ROOT, check=True, capture_output=True, text=True
    )


def test_exact_upstream_node_is_narrowly_registered_and_build_verified():
    entry = (ROOT / "src/gen_automation/i2v_worker/comfy_h3_contrast_entrypoint.py").read_text()
    assert 'NODE_CLASS_MAPPINGS = {"MiniMaxH3LatentContrast": MiniMaxH3LatentContrast}' in entry
    assert 'import_module(".nodes", __package__).MiniMaxH3LatentContrast' in entry
    assert "register_routes(" not in entry
    assert "GenAutomationH3Contrast" in H3_CUSTOM_NODES
    pin = "895e3c471164423f0ea0e8eaf45eb701efe641ae"
    assert pin in (ROOT / "scripts/install-h3-native-nodes.sh").read_text()
    assert pin in (ROOT / "Dockerfile.i2v-worker").read_text()
    assert "verify_contrast(nodes)" in (ROOT / "scripts/verify-h3-comfy-nodes.py").read_text()
