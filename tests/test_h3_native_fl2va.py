"""Native model identity, graph, settings and runtime isolation regressions."""

import copy
import json
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import SecretStr, ValidationError

from gen_automation.i2v_worker.comfy import ComfyClient
from gen_automation.i2v_worker.h3_upscale import H3_UPSCALER_FILENAME, H3_UPSCALER_ROLE
from gen_automation.i2v_worker.h3_variants import (
    H3_NATIVE_MODELS,
    h3_job_matches_variant,
    h3_manifest_matches_variant,
    h3_native_models_match,
)
from gen_automation.i2v_worker.models import GenerationSettings
from gen_automation.i2v_worker.settings import H3_CUSTOM_NODES, I2VWorkerSettings
from gen_automation.i2v_worker.workflow import (
    WorkflowError,
    load_workflow_template,
    render_workflow,
)
from gen_automation.services.i2v import _normalized_settings

ROOT = Path(__file__).parents[1]
TEMPLATE = ROOT / "workflows/minimax-h3-native-i2v.api.json"


def native(**updates):
    return GenerationSettings(profile="minimax_h3", h3_model_variant="fl2va_int8", **updates)


def graph(settings=None, template_path=TEMPLATE):
    paths = {role: value[2] for role, value in H3_NATIVE_MODELS.items()}
    paths[H3_UPSCALER_ROLE] = H3_UPSCALER_FILENAME
    return render_workflow(
        load_workflow_template(template_path, profile="minimax_h3"),
        input_filename="prepared.png",
        positive_prompt="Leaves sway.",
        negative_prompt="blur",
        settings=settings or native(seed=0),
        job_id=UUID(int=1),
        attempt_id=UUID(int=2),
        model_paths=paths,
    )[0]


def test_native_first_frame_graph_has_no_director_rewrite_or_hidden_turbo():
    value = graph()
    assert value["1"]["class_type"] == "LoadImage"
    assert value["6"]["inputs"] == {
        "clip": ["3", 0],
        "vae": ["4", 0],
        "prompt": "Leaves sway.",
        "width": 576,
        "height": 1024,
        "length": 124,
        "first_frame": ["1", 0],
    }
    assert value["3"]["inputs"]["type"] == "minimax"
    assert value["7"]["inputs"] == {"model": ["2", 0], "shift_video": 12, "shift_audio": 3}
    assert value["9"]["inputs"]["model"] == value["11"]["inputs"]["model"] == ["7", 0]
    assert value["8"]["inputs"] == {"noise_seed": 0}
    assert value["12"]["inputs"]["noise"] == ["8", 0]
    assert value["15"]["inputs"]["samples"] == ["12", 0]
    assert not any("DaSiWa" in x["class_type"] or "Lora" in x["class_type"] for x in value.values())


def test_native_loras_are_native_model_only_ordered_and_reused_once():
    loras = [
        {"artifact_id": UUID(int=n + 3), "sha256": str(n + 1) * 64, "strength": strength}
        for n, strength in enumerate((0.8, 0.0, -0.2))
    ]
    value = graph(native(h3_loras=loras, width=1152, height=1504, match_source_resolution=True))
    assert "h3-lora-1" not in value
    assert value["h3-lora-0"]["inputs"] == {
        "model": ["2", 0],
        "lora_name": "managed-h3/" + "1" * 64 + ".safetensors",
        "strength_model": 0.8,
    }
    assert value["h3-lora-2"]["inputs"]["model"] == ["h3-lora-0", 0]
    assert value["h3-lora-2"]["inputs"]["strength_model"] == -0.2
    assert value["7"]["inputs"]["model"] == ["h3-lora-2", 0]
    assert value["h3-source-upscale"]["inputs"]["model"] == ["7", 0]
    assert value["h3-refine-sigmas"]["inputs"]["model"] == ["7", 0]
    assert value["h3-source-upscale"]["inputs"]["conditioning"] == ["6", 0]
    assert value["h3-source-upscale"]["inputs"]["noise"] == ["8", 0]
    assert value["6"]["inputs"]["width"] == 768
    assert value["6"]["inputs"]["height"] == 992
    assert value["6"]["inputs"]["clip"] == ["3", 0]


def test_native_defaults_are_pinned_but_expert_settings_remain_editable():
    value = native()
    assert (value.steps, value.cfg, value.sampler, value.scheduler) == (
        20,
        1,
        "res_multistep",
        "simple",
    )
    assert (value.video_shift, value.audio_shift) == (12, 3)
    data = {"profile": "minimax_h3", "h3_model_variant": "fl2va_int8"}
    frozen = copy.deepcopy(data)
    assert _normalized_settings(data)["audio_shift"] == 3 and data == frozen
    value = graph(
        native(
            steps=4,
            cfg=2.5,
            sampler="er_sde",
            scheduler="beta",
            width=1152,
            height=1504,
            match_source_resolution=True,
            h3_refine_cfg=1.8,
            h3_refine_steps=3,
        )
    )
    assert value["9"]["class_type"] == "CFGGuider"
    assert value["9"]["inputs"]["model"] == ["7", 0]
    assert value["h3-negative"]["inputs"] == {"clip": ["3", 0], "text": "blur"}
    assert value["11"]["inputs"]["steps"] == 4
    assert value["11"]["inputs"]["scheduler"] == "beta"
    assert value["h3-source-upscale"]["inputs"]["negative"] == ["h3-negative", 0]


def test_native_diagnostic_and_noop_upscale_use_same_generation():
    value = graph(native(match_source_resolution=True, h3_save_base_video=True))
    assert "h3-source-upscale" not in value
    assert value["h3-base-decode"]["inputs"]["samples"] == ["12", 0]
    assert value["h3-base-decode"]["inputs"]["after_refinement"] == ["13", 0]
    assert sum(n["class_type"] == "SamplerCustomAdvanced" for n in value.values()) == 1


@pytest.mark.parametrize("variant", [None, "turbo_v2", "hybrid_v2"])
def test_old_requests_and_graphs_never_silently_run_as_native(variant):
    assert not h3_job_matches_variant(
        {"profile": "minimax_h3", "h3_model_variant": variant}, "fl2va_int8"
    )
    with pytest.raises(WorkflowError, match="checkpoint variant"):
        graph(
            GenerationSettings(
                profile="minimax_h3",
                h3_model_variant=variant,
                fps=24,
                frame_count=124,
                scheduler="simple",
            )
        )
    with pytest.raises(WorkflowError, match="checkpoint variant"):
        graph(native(), ROOT / "workflows/dasiwa-minimax-h3-i2v-v1.api.json")


def model_objects():
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
        for role, (sha, size, filename) in H3_NATIVE_MODELS.items()
    ]


def worker(objects):
    return I2VWorkerSettings(
        profile="minimax_h3",
        model_objects_json=SecretStr(json.dumps(objects)),
        source_revision="a" * 40,
        private_manifest_source_sha256="b" * 64,
    )


def test_native_worker_derives_identity_and_requires_official_full_model_set():
    objects = model_objects()
    settings = worker(objects)
    assert settings.h3_model_variant == "fl2va_int8"
    assert settings.effective_workflow_template.name == TEMPLATE.name
    manifest = {
        role: {"sha256": sha, "bytes": size, "target_filename": filename}
        for role, (sha, size, filename) in H3_NATIVE_MODELS.items()
    }
    assert h3_native_models_match(manifest)
    assert h3_manifest_matches_variant(manifest["diffusion_model"], "fl2va_int8")
    assert not h3_manifest_matches_variant(manifest["diffusion_model"], "turbo_v2")
    assert not h3_manifest_matches_variant(manifest["diffusion_model"], "hybrid_v2")
    for item in objects:
        invalid = copy.deepcopy(objects)
        next(x for x in invalid if x["role"] == item["role"])["byte_size"] += 1
        with pytest.raises(ValidationError, match="exact official model set"):
            worker(invalid)


@pytest.mark.asyncio
async def test_native_readiness_cannot_pass_without_native_loader_and_conditioning():
    client = ComfyClient(
        base_url="http://127.0.0.1:8188",
        request_timeout_seconds=5,
        network_attempts=1,
        poll_seconds=1,
        profile="minimax_h3",
    )
    try:
        names = {name for name, _ in client.required_nodes}
        assert {
            "MiniMaxH3ImageToVideo",
            "LoraLoaderModelOnly",
            "RandomNoise",
            "SamplerCustomAdvanced",
        } <= names
        assert not any("DaSiWa" in name or "Director" in name for name in names)
        assert H3_CUSTOM_NODES == ("Comfyui-MMH3-UltimateUpscale", "GenAutomationH3")
    finally:
        await client.close()


def test_dashboard_native_defaults_and_no_dasiwa_prompt_builder_claim(client):
    config = client.app.state.settings
    config.i2v_profile = "minimax_h3"
    config.i2v_h3_model_variant = "fl2va_int8"
    config.i2v_h3_advanced_sampling_enabled = True
    page = client.get("/dashboard/animations").text
    assert "MiniMax H3 FL2VA · INT8 Pruned" in page
    assert 'name="steps" value="20"' in page and 'name="audio_shift" value="3"' in page
    assert 'value="res_multistep" selected' in page
    assert "structured builder adds" not in page
    assert "optional community extension" in page
