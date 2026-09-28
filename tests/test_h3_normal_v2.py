"""Normal V2 defaults, unrestricted native sampling and coherent rollback contracts."""

import copy
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from gen_automation.api.routes.i2v import _validate_generation_profile
from gen_automation.config import Settings
from gen_automation.i2v_worker.h3_sampling import H3_EXPERT_DEFAULTS, H3_SAMPLERS, H3_SCHEDULERS
from gen_automation.i2v_worker.h3_variants import (
    H3_NORMAL_V2_BYTES,
    H3_NORMAL_V2_FILENAME,
    H3_NORMAL_V2_SHA256,
    h3_job_matches_variant,
    h3_manifest_matches_variant,
)
from gen_automation.i2v_worker.models import GenerationSettings
from gen_automation.services.i2v import _dispatch_eligible as claim_eligible
from gen_automation.services.i2v import _normalized_settings
from gen_automation.services.i2v_runtime import _dispatch_eligible, _worker_settings_snapshot
from tests.test_h3_upscale import _workflow
from tests.test_i2v_h3_readiness import h3_settings

ROOT = Path(__file__).parents[1]


def normal(**changes):
    return GenerationSettings(profile="minimax_h3", h3_model_variant="hybrid_v2", **changes)


def test_creator_recipe_is_default_not_a_restriction():
    value = normal()
    assert (value.sampler, value.scheduler, value.steps, value.cfg) == (
        "res_multistep",
        "simple",
        20,
        1,
    )
    assert (value.video_shift, value.audio_shift) == (12, 4)
    assert normal(steps=4, cfg=2.7, sampler="lcm", video_shift=8).steps == 4
    assert normal(steps=1, cfg=0).cfg == 0
    assert normal(steps=80, cfg=12, sampler="er_sde", scheduler="beta").scheduler == "beta"


@pytest.mark.parametrize("variant", [None, "turbo_v2", "hybrid_v2"])
def test_all_pinned_native_sampler_scheduler_choices_are_available(variant):
    for sampler in H3_SAMPLERS:
        for scheduler in H3_SCHEDULERS:
            assert (
                h3_settings(h3_model_variant=variant, sampler=sampler, scheduler=scheduler).sampler
                == sampler
            )


@pytest.mark.parametrize(
    "changes",
    [
        {"steps": 0},
        {"steps": 1.5},
        {"steps": 10001},
        {"cfg": -1},
        {"cfg": 101},
        {"cfg": float("nan")},
        {"cfg": float("inf")},
        {"sampler": "not_installed"},
        {"scheduler": "not_installed"},
        {"video_shift": 0},
        {"audio_shift": 101},
        {"h3_denoise": -1},
        {"h3_refine_cfg": float("inf")},
        {"h3_refine_steps": 0},
        {"h3_refine_sampler": "not_installed"},
        {"h3_refine_scheduler": "not_installed"},
    ],
)
def test_native_numeric_and_installed_node_contract_stays_valid(changes):
    with pytest.raises(ValidationError):
        normal(**changes)


def test_legacy_wan_sampling_does_not_expand():
    for fields in (
        {"sampler": "er_sde"},
        {"scheduler": "beta"},
        {"cfg": 0},
        {"h3_refine_steps": 2},
        {"h3_model_variant": "hybrid_v2"},
    ):
        with pytest.raises(ValidationError):
            GenerationSettings(**fields)
    assert GenerationSettings().sampler == "euler"


def test_cfg_and_independent_refinement_settings_reach_actual_graph():
    value = normal(
        steps=4,
        cfg=2.5,
        sampler="er_sde",
        scheduler="beta",
        h3_denoise=0.9,
        width=1152,
        height=1504,
        match_source_resolution=True,
        h3_refine_steps=6,
        h3_refine_cfg=1.8,
        h3_refine_sampler="euler",
        h3_refine_scheduler="simple",
        h3_refine_denoise=0.4,
    )
    graph = _workflow(value)
    assert graph["9"]["class_type"] == "CFGGuider"
    assert graph["9"]["inputs"]["cfg"] == 2.5
    assert graph["9"]["inputs"]["negative"] == ["h3-negative", 0]
    assert graph["h3-negative"]["class_type"] == "CLIPTextEncode"
    assert graph["h3-negative"]["inputs"]["clip"] == ["h3-lora-stack", 1]
    assert graph["10"]["inputs"]["sampler_name"] == "er_sde"
    assert graph["11"]["inputs"] == {
        "model": ["h3-lora-stack", 0],
        "steps": 4,
        "scheduler": "beta",
        "denoise": 0.9,
    }
    assert graph["h3-refine-sigmas"]["inputs"] == {
        "model": ["h3-lora-stack", 0],
        "steps": 6,
        "scheduler": "simple",
        "denoise": 0.4,
    }
    assert graph["h3-source-upscale"]["inputs"]["negative"] == ["h3-negative", 0]
    assert graph["h3-source-upscale"]["inputs"]["cfg"] == 1.8
    assert graph["h3-source-upscale"]["inputs"]["sampler"] == ["h3-refine-sampler", 0]
    assert graph["h3-refine-sampler"]["inputs"]["sampler_name"] == "euler"
    assert graph["1"]["inputs"]["mode"] == "REF2VA"
    assert graph["15"]["inputs"]["samples"] == ["12", 0]


def test_default_cfg_preserves_creator_basic_guider_and_refinement():
    graph = _workflow(normal(width=1152, height=1504, match_source_resolution=True))
    assert graph["9"]["class_type"] == "BasicGuider"
    assert "h3-negative" not in graph
    assert "negative" not in graph["h3-source-upscale"]["inputs"]
    assert graph["h3-refine-sigmas"]["inputs"]["steps"] == 1
    assert graph["h3-refine-sigmas"]["inputs"]["denoise"] == 0.2


def test_rollout_gate_and_checkpoint_guard_fail_closed():
    config = Settings.model_construct(i2v_profile="minimax_h3")
    legacy = h3_settings().model_dump()
    _validate_generation_profile(config, legacy, "")
    with pytest.raises(HTTPException, match="matching worker"):
        _validate_generation_profile(config, {**legacy, "cfg": 2.5}, "")
    with pytest.raises(HTTPException, match="different H3 checkpoint"):
        _validate_generation_profile(config, normal().model_dump(), "")
    config.i2v_h3_model_variant = "hybrid_v2"
    config.i2v_h3_advanced_sampling_enabled = True
    _validate_generation_profile(config, normal(steps=4, cfg=2).model_dump(), "motion blur")
    with pytest.raises(HTTPException, match="different H3 checkpoint"):
        _validate_generation_profile(config, legacy, "")
    with pytest.raises(HTTPException) as error:
        _validate_generation_profile(config, {**normal().model_dump(), "video_shift": "broken"}, "")
    assert error.value.status_code == 422


def test_old_payloads_remain_accepted_by_old_strict_workers():
    legacy = h3_settings().model_dump()
    frozen = copy.deepcopy(legacy)
    wire = _worker_settings_snapshot(legacy)
    assert "h3_model_variant" not in wire
    assert not set(H3_EXPERT_DEFAULTS) & wire.keys()
    assert legacy == frozen
    assert _worker_settings_snapshot(normal().model_dump())["h3_model_variant"] == "hybrid_v2"


def test_job_claim_and_runtime_cannot_cross_checkpoint_variants():
    legacy, current = h3_settings().model_dump(), normal(steps=4).model_dump()
    assert h3_job_matches_variant(legacy, "turbo_v2")
    assert not h3_job_matches_variant(legacy, "hybrid_v2")
    for predicate in (claim_eligible, _dispatch_eligible):
        assert not predicate(
            legacy, reviewed_loras_enabled=True, profile="minimax_h3", h3_model_variant="hybrid_v2"
        )
        assert predicate(
            current, reviewed_loras_enabled=True, profile="minimax_h3", h3_model_variant="hybrid_v2"
        )
        assert not predicate(
            current, reviewed_loras_enabled=True, profile="minimax_h3", h3_model_variant="turbo_v2"
        )


def test_normal_preset_defaults_are_frozen_without_rewriting_input():
    input_value = {"profile": "minimax_h3", "h3_model_variant": "hybrid_v2"}
    frozen = copy.deepcopy(input_value)
    result = _normalized_settings(input_value)
    assert result["steps"] == 20 and result["sampler"] == "res_multistep"
    assert input_value == frozen


def test_source_pin_changes_only_the_diffusion_model_and_preserves_turbo_rollback():
    old = json.loads((ROOT / "i2v-models/dasiwa-minimax-h3-turbo-v2.sources.json").read_text())
    new = json.loads((ROOT / "i2v-models/dasiwa-minimax-h3-normal-v2.sources.json").read_text())

    def by_role(doc):
        return {s["role"]: s for s in doc["sources"]}

    old, new = by_role(old), by_role(new)
    assert set(old) == set(new)
    for role in set(old) - {"diffusion_model"}:
        assert old[role] == new[role]
    source = new["diffusion_model"]
    assert "3314675?fileId=3203130" in source["url"]
    assert source["sha256"] == H3_NORMAL_V2_SHA256
    assert source["expected_bytes"] == H3_NORMAL_V2_BYTES
    assert source["target_filename"] == H3_NORMAL_V2_FILENAME
    manifest = {**source, "bytes": source["expected_bytes"]}
    assert h3_manifest_matches_variant(manifest, "hybrid_v2")
    assert not h3_manifest_matches_variant(manifest, "turbo_v2")
    assert not h3_manifest_matches_variant({**manifest, "bytes": 1}, "hybrid_v2")


def test_dashboard_exposes_native_controls_and_correct_normal_defaults(client):
    config = client.app.state.settings
    config.i2v_profile = "minimax_h3"
    assert 'name="cfg"' not in client.get("/dashboard/animations").text
    config.i2v_h3_model_variant = "hybrid_v2"
    config.i2v_h3_advanced_sampling_enabled = True
    page = client.get("/dashboard/animations").text
    assert "Hybrid V2 · non-Turbo" in page
    for name in (
        "steps",
        "cfg",
        "sampler",
        "scheduler",
        "video_shift",
        "audio_shift",
        *H3_EXPERT_DEFAULTS,
    ):
        assert f'name="{name}"' in page
    assert 'name="steps" value="20"' in page
    assert 'value="res_multistep" selected' in page
    assert 'value="er_sde"' in page and 'value="beta"' in page
    assert (
        'name="negative_prompt" rows="4" maxlength="100000" data-negative-prompt disabled'
        not in page
    )


def test_draft_migration_preserves_content_and_old_draft_not_old_turbo_defaults():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node required for frontend contract")
    script = r"""
const assert=require("node:assert/strict"), fs=require("node:fs"), vm=require("node:vm");
const source=fs.readFileSync("src/gen_automation/static/i2v.js","utf8");
const old={positive_prompt:"keep",negative_prompt:"negative",batch_count:"2",
  settings:{steps:4,cfg:1,video_shift:8,h3_loras:[{artifact_id:"one",strength:0.9}],seed:42}};
const saved=new Map([["old",JSON.stringify(old)]]);
const elements={positive_prompt:{},negative_prompt:{},batch_count:{}};
let applied;
const context=vm.createContext({JSON,form:{elements},draftKey:"new",legacyDraftKey:"old",isH3:true,
  h3AdvancedSamplingEnabled:true,h3ModelVariant:"hybrid_v2",
  h3SamplingDefaults:{steps:20,cfg:1,video_shift:12,sampler:"res_multistep",scheduler:"simple"},
  draftState:{},localStorage:{getItem:k=>saved.get(k),
    removeItem:()=>assert.fail("must preserve draft")},applySettings:v=>applied=v});
vm.runInContext(source.slice(source.indexOf("  function restoreDraft()"),
  source.indexOf("  async function loadPresets()")),context);
vm.runInContext("restoreDraft()",context);
assert.equal(applied.steps,20);assert.equal(applied.video_shift,12);assert.equal(applied.seed,42);
assert.equal(applied.h3_loras[0].strength,0.9);assert.equal(elements.positive_prompt.value,"keep");
assert.equal(saved.get("old"),JSON.stringify(old));
saved.set("new",JSON.stringify({...old,settings:{...old.settings,h3_model_variant:"hybrid_v2",steps:4,cfg:2.5,sampler:"er_sde",scheduler:"beta"}}));
vm.runInContext("restoreDraft()",context);
assert.equal(applied.steps,4);assert.equal(applied.cfg,2.5);assert.equal(applied.sampler,"er_sde");
"""
    subprocess.run([node, "-e", script], cwd=ROOT, check=True, capture_output=True, text=True)  # noqa: S603
