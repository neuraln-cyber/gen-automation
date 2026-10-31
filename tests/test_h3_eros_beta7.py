"""Beta 7 uses the linked immutable checkpoint and its trained VSA recipe."""

import json
from pathlib import Path
from uuid import UUID

import httpx2
import pytest
from pydantic import SecretStr, ValidationError

from gen_automation.i2v_worker.comfy import ComfyClient, ComfyError, _vsa_available
from gen_automation.i2v_worker.h3_upscale import H3_UPSCALER_FILENAME, H3_UPSCALER_ROLE
from gen_automation.i2v_worker.h3_variants import (
    H3_EROS_BETA7_MODELS,
    H3_EROS_MODELS,
    h3_eros_beta7_models_match,
    h3_job_matches_variant,
    h3_manifest_matches_variant,
)
from gen_automation.i2v_worker.models import GenerationSettings
from gen_automation.i2v_worker.settings import I2VWorkerSettings
from gen_automation.i2v_worker.workflow import load_workflow_template, render_workflow
from gen_automation.services.i2v import _normalized_settings
from gen_automation.services.i2v_runtime import _worker_settings_snapshot

ROOT = Path(__file__).parents[1]


def settings(**updates):
    return GenerationSettings(profile="minimax_h3", h3_model_variant="eros_beta7", **updates)


def graph(value=None):
    paths = {role: item[2] for role, item in H3_EROS_BETA7_MODELS.items()}
    paths[H3_UPSCALER_ROLE] = H3_UPSCALER_FILENAME
    return render_workflow(
        load_workflow_template(
            ROOT / "workflows/minimax-h3-eros-ref2va.api.json", profile="minimax_h3"
        ),
        input_filename="reference.png",
        positive_prompt="<Picture 1> moves while wind rustles the leaves.",
        negative_prompt="",
        settings=value or settings(),
        job_id=UUID(int=1),
        attempt_id=UUID(int=2),
        model_paths=paths,
    )[0]


def worker_objects():
    folder = {
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
            "install_path": f"models/{folder[role]}/{filename}",
        }
        for role, (sha, size, filename) in H3_EROS_BETA7_MODELS.items()
    ]


def test_exact_beta7_identity_and_beta5_history_isolation():
    manifest = {
        role: {"sha256": sha, "bytes": size, "target_filename": filename}
        for role, (sha, size, filename) in H3_EROS_BETA7_MODELS.items()
    }
    assert h3_eros_beta7_models_match(manifest)
    assert h3_manifest_matches_variant(manifest["diffusion_model"], "eros_beta7")
    assert not h3_manifest_matches_variant(manifest["diffusion_model"], "eros_beta5")
    assert H3_EROS_BETA7_MODELS["diffusion_model"] != H3_EROS_MODELS["diffusion_model"]
    assert not h3_job_matches_variant(
        {"profile": "minimax_h3", "h3_model_variant": "eros_beta5"}, "eros_beta7"
    )
    worker = I2VWorkerSettings(
        profile="minimax_h3",
        model_objects_json=SecretStr(json.dumps(worker_objects())),
        source_revision="a" * 40,
        private_manifest_source_sha256="b" * 64,
    )
    assert worker.h3_model_variant == "eros_beta7"
    assert worker.effective_workflow_template.name == "minimax-h3-eros-ref2va.api.json"
    invalid = worker_objects()
    invalid[0]["byte_size"] += 1
    with pytest.raises(ValidationError, match="Beta 7 requires the exact reviewed model set"):
        I2VWorkerSettings(
            profile="minimax_h3",
            model_objects_json=SecretStr(json.dumps(invalid)),
            source_revision="a" * 40,
            private_manifest_source_sha256="b" * 64,
        )


def test_beta7_defaults_are_author_starting_point_without_rewriting_old_jobs():
    value = settings()
    assert (value.steps, value.sampler, value.scheduler, value.video_shift, value.audio_shift) == (
        8,
        "er_sde",
        "sgm_uniform",
        12,
        3,
    )
    assert (value.h3_attention_backend, value.h3_refine_steps, value.h3_refine_denoise) == (
        "comfy_kitchen",
        3,
        0.3,
    )
    assert settings(steps=10, sampler="euler", scheduler="simple").steps == 10
    with pytest.raises(ValidationError, match="installed in native Comfy"):
        settings(scheduler="beta57")
    old = GenerationSettings(profile="minimax_h3", h3_model_variant="eros_beta5")
    assert (old.steps, old.sampler, old.scheduler) == (8, "res_multistep", "simple")
    snapshot = _worker_settings_snapshot(_normalized_settings(value.model_dump(mode="json")))
    assert GenerationSettings.model_validate(snapshot).h3_refine_steps == 3


def test_beta7_explicit_settings_survive_wire_normalization():
    original = settings(h3_refine_steps=1, h3_refine_denoise=0.2, h3_attention_backend="default")
    snapshot = _worker_settings_snapshot(_normalized_settings(original.model_dump(mode="json")))
    restored = GenerationSettings.model_validate(snapshot)
    assert restored == original


def test_vsa_is_between_optional_lora_backend_and_both_sampling_passes():
    value = settings(
        h3_loras=[{"artifact_id": UUID(int=3), "sha256": "a" * 64, "strength": 0.4}],
        match_source_resolution=True,
        width=1152,
        height=1504,
    )
    workflow = graph(value)
    assert workflow["6"]["class_type"] == "MiniMaxH3ReferenceToVideo"
    assert workflow["h3-kitchen"]["inputs"]["model"] == ["h3-lora-0", 0]
    assert workflow["h3-eros-beta7-vsa"]["inputs"] == {
        "model": ["h3-kitchen", 0],
        "selection": "vsa",
        "selection.keep_percent": 10.0,
        "start_percent": 0.0,
        "end_percent": 1.0,
        "dense_blocks": "0,1",
        "min_tokens": 12288,
        "extra_tokens": 0,
        "sink_conditioning": "exact_kv_and_rows",
        "verbose": True,
    }
    assert workflow["7"]["inputs"]["model"] == ["h3-eros-beta7-vsa", 0]
    assert workflow["9"]["inputs"]["model"] == ["7", 0]
    assert workflow["h3-source-upscale"]["inputs"]["model"] == ["7", 0]
    assert workflow["11"]["inputs"]["scheduler"] == "sgm_uniform"
    assert workflow["h3-refine-sigmas"]["inputs"]["steps"] == 3
    assert workflow["h3-refine-sigmas"]["inputs"]["denoise"] == 0.3
    assert workflow["6"]["inputs"]["prompt"] == "<Picture 1> moves while wind rustles the leaves."


def test_beta5_graph_does_not_inherit_vsa():
    value = GenerationSettings(profile="minimax_h3", h3_model_variant="eros_beta5")
    assert "h3-eros-beta7-vsa" not in graph(value)


def test_vsa_schema_requires_exact_native_option():
    schema = {
        "BlockSparseAttention": {
            "input": {
                "required": {
                    "selection": [
                        "COMFY_DYNAMICCOMBO_V3",
                        {"options": [{"key": "sol-attn"}, {"key": "vsa"}]},
                    ]
                }
            }
        }
    }
    assert _vsa_available(schema)
    schema["BlockSparseAttention"]["input"]["required"]["selection"][1]["options"].pop()
    assert not _vsa_available(schema)


@pytest.mark.asyncio
async def test_missing_native_vsa_fails_before_prompt_post(tmp_path):
    requests = []

    def handler(request):
        requests.append(request.url.path)
        return httpx2.Response(200, json={"BlockSparseAttention": {"input": {"required": {}}}})

    client = ComfyClient(
        base_url="http://127.0.0.1:8188",
        request_timeout_seconds=5,
        network_attempts=1,
        poll_seconds=0.01,
        profile="minimax_h3",
        h3_model_variant="eros_beta7",
    )
    await client.client.aclose()
    client.client = httpx2.AsyncClient(
        base_url="http://127.0.0.1:8188", transport=httpx2.MockTransport(handler)
    )
    try:
        assert any(name == "BlockSparseAttention" for name, _ in client.required_nodes)
        with pytest.raises(ComfyError, match="VSA attention is unavailable"):
            await client.execute({"h3-eros-beta7-vsa": {}}, tmp_path)
    finally:
        await client.close()
    assert requests == ["/object_info/BlockSparseAttention"]


def test_dashboard_beta7_defaults_and_prompt_guidance(client):
    config = client.app.state.settings
    config.i2v_profile = "minimax_h3"
    config.i2v_h3_model_variant = "eros_beta7"
    config.i2v_h3_advanced_sampling_enabled = True
    page = client.get("/dashboard/animations").text
    assert "H3 Eros Max Beta 7" in page and "VSA REF2VA" in page
    assert 'name="steps" value="8"' in page
    assert 'value="er_sde" selected' in page
    assert 'value="sgm_uniform" selected' in page
    assert 'value="comfy_kitchen" selected' in page
    assert 'name="h3_refine_steps" type="number" value="3"' in page
    assert 'name="h3_refine_denoise" type="number" value="0.3"' in page
    assert "overall_soundscape" in page and "old six-section Beta 5 scaffold" in page
    assert "eros_prompt.js" not in page and "data-eros-style-preset" not in page
