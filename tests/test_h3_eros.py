"""Eros isolation: native REF2VA, immutable identities, unchanged owner controls."""

import copy
import json
from pathlib import Path
from uuid import UUID

import pytest
from PIL import Image
from pydantic import SecretStr, ValidationError

from gen_automation.i2v_worker.h3_upscale import H3_UPSCALER_FILENAME, H3_UPSCALER_ROLE
from gen_automation.i2v_worker.h3_variants import (
    H3_EROS_MODELS,
    H3_NATIVE_MODELS,
    h3_eros_models_match,
    h3_job_matches_variant,
    h3_manifest_matches_variant,
)
from gen_automation.i2v_worker.media import prepare_input_image, resolve_generation_settings
from gen_automation.i2v_worker.models import GenerationSettings
from gen_automation.i2v_worker.settings import I2VWorkerSettings
from gen_automation.i2v_worker.workflow import (
    WorkflowError,
    load_workflow_template,
    render_workflow,
)
from gen_automation.services.i2v import _normalized_settings

ROOT = Path(__file__).parents[1]
TEMPLATE = ROOT / "workflows/minimax-h3-eros-ref2va.api.json"


def eros(**updates):
    return GenerationSettings(profile="minimax_h3", h3_model_variant="eros_beta5", **updates)


def graph(settings=None, template=TEMPLATE, guide_filename=None):
    paths = {role: value[2] for role, value in H3_EROS_MODELS.items()}
    paths[H3_UPSCALER_ROLE] = H3_UPSCALER_FILENAME
    return render_workflow(
        load_workflow_template(template, profile="minimax_h3"),
        input_filename="image.png",
        guide_filename=guide_filename,
        positive_prompt="<Picture 1> moves.",
        negative_prompt="blur",
        settings=settings or eros(),
        job_id=UUID(int=1),
        attempt_id=UUID(int=2),
        model_paths=paths,
    )[0]


def objects():
    folders = {
        "diffusion_model": "diffusion_models",
        "text_encoder": "text_encoders",
        "video_vae": "vae",
        "audio_vae": "vae",
    }
    return [
        {
            "role": role,
            "bucket": "models",
            "key": f"worker/i2v/sha256/{sha}",
            "version_id": "immutable",
            "sha256": sha,
            "byte_size": size,
            "install_path": f"models/{folders[role]}/{filename}",
        }
        for role, (sha, size, filename) in H3_EROS_MODELS.items()
    ]


def worker(items):
    return I2VWorkerSettings(
        profile="minimax_h3",
        model_objects_json=SecretStr(json.dumps(items)),
        source_revision="a" * 40,
        private_manifest_source_sha256="b" * 64,
    )


def test_identity_is_exact_and_does_not_fall_back_to_turbo():
    value = worker(objects())
    assert value.h3_model_variant == "eros_beta5"
    assert value.effective_workflow_template.name == TEMPLATE.name
    manifest = {
        role: {"sha256": sha, "bytes": size, "target_filename": filename}
        for role, (sha, size, filename) in H3_EROS_MODELS.items()
    }
    assert h3_eros_models_match(manifest)
    assert h3_manifest_matches_variant(manifest["diffusion_model"], "eros_beta5")
    for role in manifest:
        invalid = copy.deepcopy(manifest)
        invalid[role]["bytes"] += 1
        assert not h3_eros_models_match(invalid)
    for item in objects():
        invalid = objects()
        next(x for x in invalid if x["role"] == item["role"])["byte_size"] += 1
        with pytest.raises(ValidationError, match="exact reviewed model set"):
            worker(invalid)
    invalid = objects()
    invalid[0]["sha256"] = "e" * 64
    invalid[0]["key"] = "worker/i2v/sha256/" + "e" * 64
    with pytest.raises(ValidationError, match="Unknown H3 checkpoint"):
        worker(invalid)
    assert not h3_manifest_matches_variant({"sha256": "e" * 64}, "turbo_v2")


@pytest.mark.parametrize("variant", [None, "turbo_v2", "hybrid_v2", "fl2va_int8"])
def test_earlier_model_requests_never_run_as_eros(variant):
    assert not h3_job_matches_variant(
        {"profile": "minimax_h3", "h3_model_variant": variant}, "eros_beta5"
    )
    with pytest.raises(WorkflowError, match="checkpoint variant"):
        graph(
            GenerationSettings(
                profile="minimax_h3",
                h3_model_variant=variant,
                frame_count=124,
                fps=24,
                scheduler="simple",
            )
        )
    with pytest.raises(WorkflowError, match="checkpoint variant"):
        graph(template=ROOT / "workflows/minimax-h3-native-i2v.api.json")
    assert H3_NATIVE_MODELS["diffusion_model"] != H3_EROS_MODELS["diffusion_model"]


def test_defaults_freeze_without_overwriting_expert_controls():
    v = eros()
    assert (v.steps, v.cfg, v.sampler, v.scheduler, v.video_shift, v.audio_shift) == (
        8,
        1,
        "res_multistep",
        "simple",
        12,
        3,
    )
    assert (v.width, v.height, v.h3_refine_steps) == (768, 1344, 4)
    assert not v.h3_loras and not v.match_source_resolution
    data = {"profile": "minimax_h3", "h3_model_variant": "eros_beta5"}
    before = copy.deepcopy(data)
    assert _normalized_settings(data)["steps"] == 8 and data == before
    v = eros(steps=17, cfg=2.2, sampler="er_sde", scheduler="beta", h3_refine_steps=5)
    assert (v.steps, v.cfg, v.sampler, v.scheduler, v.h3_refine_steps) == (
        17,
        2.2,
        "er_sde",
        "beta",
        5,
    )


def test_ref_graph_has_no_first_frame_builder_or_extra_turbo_lora():
    g = graph()
    assert g["6"]["class_type"] == "MiniMaxH3ReferenceToVideo"
    assert g["6"]["inputs"] == {
        "clip": ["3", 0],
        "vae": ["4", 0],
        "audio_vae": ["5", 0],
        "prompt": "<Picture 1> moves.",
        "width": 768,
        "height": 1344,
        "length": 124,
        "ref_image_size": "match",
        "ref_images.ref_image_0": ["1", 0],
    }
    assert not any("DaSiWa" in n["class_type"] or "Lora" in n["class_type"] for n in g.values())
    assert g["2"]["inputs"]["unet_name"] == H3_EROS_MODELS["diffusion_model"][2]
    assert g["11"]["inputs"]["steps"] == 8


@pytest.mark.parametrize("cfg", [1, 2.5])
@pytest.mark.parametrize("upscale", [False, True])
def test_first_frame_preserves_reference_and_both_passes(cfg, upscale):
    settings = eros(
        h3_image_mode="first_frame",
        cfg=cfg,
        match_source_resolution=upscale,
        width=1152 if upscale else 768,
        height=1504 if upscale else 992,
        h3_refine_steps=1,
    )
    g = graph(settings, guide_filename="guide.png")
    assert g["1"]["inputs"]["image"] == "image.png"
    assert g["6"]["inputs"]["ref_images.ref_image_0"] == ["1", 0]
    assert g["6"]["inputs"]["prompt"] == "<Picture 1> moves."
    assert g["h3-guide-image"]["inputs"]["image"] == "guide.png"
    assert g["h3-first-frame"] == {
        "class_type": "MiniMaxH3AddGuide",
        "inputs": {
            "positive": ["6", 0],
            "latent": ["6", 1],
            "vae": ["4", 0],
            "image": ["h3-guide-image", 0],
            "frame_idx": 0,
        },
    }
    assert g["9"]["inputs"]["positive" if cfg != 1 else "conditioning"] == ["h3-first-frame", 0]
    assert g["12"]["inputs"]["latent_image"] == ["6", 1]
    assert g["11"]["inputs"]["denoise"] == 1
    if upscale:
        assert g["h3-source-upscale"]["inputs"]["conditioning"] == ["h3-first-frame", 0]
        assert g["h3-refine-sigmas"]["inputs"]["steps"] == 1
    else:
        assert "h3-source-upscale" not in g


def test_first_frame_fails_closed_without_prepared_guide():
    with pytest.raises(WorkflowError, match="fitted guide"):
        graph(eros(h3_image_mode="first_frame"))
    for variant in (None, "turbo_v2", "hybrid_v2", "fl2va_int8"):
        with pytest.raises(ValidationError, match="requires Eros"):
            GenerationSettings(
                profile="minimax_h3", h3_model_variant=variant, h3_image_mode="first_frame"
            )


@pytest.mark.parametrize("steps", [1, 4, 9])
@pytest.mark.parametrize("mode", ["reference", "first_frame"])
def test_job_snapshot_roundtrip_never_changes_refinement_count_or_image_mode(steps, mode):
    from gen_automation.services.i2v_runtime import _worker_settings_snapshot

    saved = _normalized_settings(
        {
            "profile": "minimax_h3",
            "h3_model_variant": "eros_beta5",
            "h3_image_mode": mode,
            "h3_refine_steps": steps,
        }
    )
    payload = _worker_settings_snapshot(saved)
    actual = GenerationSettings.model_validate(payload)
    assert actual.h3_image_mode == mode and actual.h3_refine_steps == steps
    assert ("h3_image_mode" in payload) == (mode == "first_frame")
    assert eros().h3_image_mode == "reference"


def test_first_frame_gate_requires_worker_activation(client):
    from fastapi import HTTPException

    from gen_automation.api.routes.i2v import _validate_generation_profile

    config = client.app.state.settings
    config.i2v_profile = "minimax_h3"
    config.i2v_h3_model_variant = "eros_beta5"
    config.i2v_h3_advanced_sampling_enabled = True
    values = eros(h3_image_mode="first_frame").model_dump(mode="json")
    with pytest.raises(HTTPException, match="matching Eros worker"):
        _validate_generation_profile(config, values, "")
    config.i2v_h3_first_frame_enabled = True
    _validate_generation_profile(config, values, "")
    page = client.get("/dashboard/animations").text
    assert 'value="first_frame" selected' in page
    assert "data-eros-style-preset" in page


def test_loras_once_for_both_passes_with_preserved_strengths_and_ref_conditioning():
    loras = [
        {"artifact_id": UUID(int=i + 3), "sha256": str(i + 1) * 64, "strength": strength}
        for i, strength in enumerate((0.4, 0, -0.2))
    ]
    g = graph(
        eros(
            h3_loras=loras,
            width=1152,
            height=1504,
            match_source_resolution=True,
            cfg=2.5,
            h3_refine_cfg=1.8,
            h3_save_base_video=True,
        )
    )
    assert "h3-lora-1" not in g
    assert g["h3-lora-0"]["inputs"]["strength_model"] == 0.4
    assert g["h3-lora-2"]["inputs"]["strength_model"] == -0.2
    assert g["h3-lora-2"]["inputs"]["model"] == ["h3-lora-0", 0]
    assert g["7"]["inputs"]["model"] == ["h3-lora-2", 0]
    assert g["h3-source-upscale"]["inputs"]["model"] == g["9"]["inputs"]["model"] == ["7", 0]
    assert g["h3-source-upscale"]["inputs"]["conditioning"] == ["6", 0]
    assert g["h3-source-upscale"]["inputs"]["negative"] == ["h3-negative", 0]
    assert g["h3-refine-sigmas"]["inputs"]["steps"] == 4
    assert g["h3-base-decode"]["inputs"]["samples"] == ["12", 0]


@pytest.mark.parametrize("width,height", [(512, 768), (1000, 1000), (5000, 250), (1024, 1792)])
def test_eros_source_canvas_is_native_grid_within_budget(width, height):
    v = resolve_generation_settings(
        eros(match_source_aspect=True), source_width=width, source_height=height
    )
    assert v.width % 32 == v.height % 32 == 0
    assert v.width * v.height <= 768 * 1344 and max(v.width, v.height) <= 2048
    GenerationSettings.model_validate(v.model_dump())


def test_ref_image_is_not_rescaled_or_edge_padded_before_native_node(tmp_path):
    image = Image.new("RGB", (97, 131), (10, 20, 30))
    source, target = tmp_path / "in.png", tmp_path / "out.png"
    image.save(source)
    prepare_input_image(source, target, width=768, height=1344, reference_image=True)
    with Image.open(target) as actual:
        assert actual.size == image.size and actual.tobytes() == image.tobytes()


def test_dashboard_eros_defaults_and_isolated_recipe(client):
    config = client.app.state.settings
    config.i2v_profile = "minimax_h3"
    config.i2v_h3_model_variant = "eros_beta5"
    config.i2v_h3_advanced_sampling_enabled = True
    page = client.get("/dashboard/animations").text
    assert "H3 Eros Max Beta 5 · TURBO Hybrid INT8 · REF2VA" in page
    assert 'name="steps" value="8"' in page and 'name="audio_shift" value="3"' in page
    assert 'value="res_multistep" selected' in page
    assert 'name="h3_refine_steps" type="number" value="4"' in page
    assert "not an Eros creator-certified preset" in page and "structured builder adds" not in page
    assert "already baked in" in page and "not a forced first frame" in page
    script = (ROOT / "src/gen_automation/static/i2v.js").read_text(encoding="utf-8")
    assert "i2v-draft-v3:${videoProfile}:${h3ModelVariant}" in script
    assert 'const cleanEros = modelChanged && h3ModelVariant === "eros_beta5"' in script
    assert "applySettings(cleanEros ? { ...workerSettingDefaults" in script
    assert 'item.settings.h3_model_variant || "turbo_v2") === h3ModelVariant' in script
