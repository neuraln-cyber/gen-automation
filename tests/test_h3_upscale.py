"""Source-size pipeline contracts; no weights, GPU jobs, or paid services."""

import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from gen_automation.config import Settings
from gen_automation.i2v_worker.app import create_i2v_worker_app
from gen_automation.i2v_worker.comfy import ComfyClient
from gen_automation.i2v_worker.h3_upscale import (
    H3_REFINE_DENOISE,
    H3_REFINE_STEPS,
    H3_UPSCALER_BYTES,
    H3_UPSCALER_CODE_REVISION,
    H3_UPSCALER_FILENAME,
    H3_UPSCALER_ROLE,
    H3_UPSCALER_SHA256,
    h3_base_canvas,
)
from gen_automation.i2v_worker.manifest_contract import validated_i2v_manifest_objects
from gen_automation.i2v_worker.models import ModelObject
from gen_automation.i2v_worker.settings import I2VWorkerSettings
from gen_automation.i2v_worker.workflow import (
    WorkflowError,
    load_workflow_template,
    render_workflow,
)
from gen_automation.services.i2v_environment import _worker_model_objects
from tests.test_i2v_h3_readiness import h3_settings
from tests.test_i2v_worker_app import _job, _Supervisor

ROOT = Path(__file__).parents[1]
MODEL_PATHS = {
    role: f"{role}.safetensors"
    for role in ("diffusion_model", "text_encoder", "video_vae", "audio_vae")
}
MODEL_PATHS[H3_UPSCALER_ROLE] = H3_UPSCALER_FILENAME


def _upscaler_object():
    return {
        "role": H3_UPSCALER_ROLE,
        "bucket": "private-models",
        "key": f"worker/i2v/sha256/{H3_UPSCALER_SHA256}",
        "version_id": "immutable-version",
        "sha256": H3_UPSCALER_SHA256,
        "byte_size": H3_UPSCALER_BYTES,
        "install_path": f"models/latent_upscale_models/{H3_UPSCALER_FILENAME}",
    }


def _private_manifest(upscaler=True):
    objects = [
        {
            "role": role,
            "key": "worker/i2v/sha256/" + "a" * 64,
            "version_id": "v1",
            "bytes": 1,
            "sha256": "a" * 64,
            "target_filename": path,
        }
        for role, path in MODEL_PATHS.items()
        if role != H3_UPSCALER_ROLE
    ]
    if upscaler:
        obj = _upscaler_object()
        obj["bytes"] = obj.pop("byte_size")
        obj["target_filename"] = H3_UPSCALER_FILENAME
        obj.pop("install_path")
        objects.append(obj)
    return {"schema": "gen-automation/i2v-private-model-mirror/v1", "objects": objects}


def _environment_settings(upscaler=True):
    raw = json.dumps(_private_manifest(upscaler))
    return Settings.model_construct(
        i2v_profile="minimax_h3",
        i2v_lora_worker_enabled=False,
        i2v_model_manifest_json=SecretStr(raw),
        i2v_model_manifest_sha256=SecretStr(hashlib.sha256(raw.encode()).hexdigest()),
        salad_worker_artifact_bucket=SecretStr("private-models"),
    )


@pytest.mark.parametrize(
    "canvas,expected",
    [
        ((1152, 1504), (768, 992)),
        ((1504, 1152), (992, 768)),
        ((2048, 2048), (768, 768)),
        ((2048, 1024), (1408, 704)),
        ((768, 992), (768, 992)),
        ((576, 1024), (576, 1024)),
    ],
)
def test_base_canvas_preserves_the_existing_generation_envelope(canvas, expected):
    assert h3_base_canvas(*canvas) == expected
    width, height = expected
    assert min(width, height) <= 768 and width * height <= 768 * 1344
    assert width <= canvas[0] and height <= canvas[1]


def test_artifact_and_code_pins_match_the_shipped_sources():
    source = json.loads((ROOT / "i2v-models/h3-latent-upscaler.sources.json").read_text())[
        "sources"
    ][0]
    assert source["role"] == H3_UPSCALER_ROLE
    assert source["sha256"] == H3_UPSCALER_SHA256
    assert source["expected_bytes"] == H3_UPSCALER_BYTES
    assert source["target_filename"] == H3_UPSCALER_FILENAME
    assert H3_UPSCALER_CODE_REVISION in (ROOT / "Dockerfile.i2v-worker").read_text()
    ModelObject.model_validate(_upscaler_object())
    for field, bad in [
        ("sha256", "a" * 64),
        ("byte_size", 1),
        ("install_path", "models/latent_upscale_models/other.safetensors"),
    ]:
        with pytest.raises(ValidationError):
            ModelObject.model_validate({**_upscaler_object(), field: bad})


@pytest.mark.parametrize("upscaler", [False, True])
def test_optional_upscaler_does_not_break_old_manifests(upscaler):
    objects = _worker_model_objects(_environment_settings(upscaler))
    worker = I2VWorkerSettings(
        profile="minimax_h3",
        model_objects_json=SecretStr(objects),
        source_revision="b" * 40,
        private_manifest_source_sha256="c" * 64,
        require_private_delivery=True,
        model_delivery_domain="d123abc.cloudfront.net",
    )
    assert len(worker.model_objects) == (5 if upscaler else 4)
    assert (H3_UPSCALER_ROLE in {item.role for item in worker.model_objects}) is upscaler


def test_private_manifest_rejects_substituted_upscaler():
    manifest = _private_manifest()
    manifest["objects"][-1]["sha256"] = "a" * 64
    with pytest.raises(ValueError, match="invalid H3 upscaler"):
        validated_i2v_manifest_objects(manifest, reviewed_loras_enabled=False, profile="minimax_h3")


def test_control_plane_cannot_enable_source_size_with_the_old_manifest():
    raw = json.dumps(_private_manifest(False))
    with pytest.raises(ValidationError, match="source-resolution delivery requires the pinned"):
        Settings(
            environment="test",
            i2v_enabled=True,
            i2v_profile="minimax_h3",
            i2v_h3_source_resolution_enabled=True,
            i2v_model_manifest_json=SecretStr(raw),
            i2v_model_manifest_sha256=SecretStr(hashlib.sha256(raw.encode()).hexdigest()),
        )


def _workflow(settings, paths=None):
    return render_workflow(
        load_workflow_template(
            ROOT / "workflows/dasiwa-minimax-h3-i2v-v1.api.json", profile="minimax_h3"
        ),
        input_filename="prepared.png",
        positive_prompt="Leaves move gently.",
        negative_prompt="",
        settings=settings,
        job_id=uuid4(),
        attempt_id=uuid4(),
        model_paths=MODEL_PATHS if paths is None else paths,
    )[0]


def test_author_refinement_reuses_pre_shift_loras_conditioning_and_base_audio():
    graph = _workflow(
        h3_settings(
            width=1152,
            height=1504,
            match_source_resolution=True,
            h3_loras=[{"artifact_id": str(uuid4()), "sha256": "d" * 64, "strength": 0.6}],
        )
    )
    assert (graph["1"]["inputs"]["width"], graph["1"]["inputs"]["height"]) == (768, 992)
    assert graph["7"]["inputs"]["model"] == ["h3-lora-stack", 0]
    assert graph["6"]["inputs"]["clip"] == ["h3-lora-stack", 1]
    upscale = graph["h3-source-upscale"]["inputs"]
    assert upscale["model"] == ["h3-lora-stack", 0]
    assert graph["9"]["inputs"]["model"] == ["h3-preview", 0]
    assert "first_frame" not in upscale and upscale["conditioning"] == ["6", 0]
    params = graph["h3-upscale-params"]["inputs"]
    assert (params["width"], params["height"]) == (1152, 1504)
    assert graph["h3-refine-sigmas"]["inputs"] == {
        "model": ["h3-lora-stack", 0],
        "scheduler": "simple",
        "steps": H3_REFINE_STEPS,
        "denoise": H3_REFINE_DENOISE,
    }
    assert graph["13"]["inputs"]["samples"] == ["h3-source-upscale", 0]
    assert graph["15"]["inputs"]["samples"] == ["12", 0]  # no refinement of original audio


def test_standard_mode_and_small_originals_do_not_add_a_refinement_pass():
    for settings in [h3_settings(), h3_settings(match_source_resolution=True)]:
        graph = _workflow(settings)
        assert len(graph) == 20
        assert graph["13"]["inputs"]["samples"] == ["12", 0]


def test_missing_upscaler_fails_before_workflow_submission():
    with pytest.raises(WorkflowError, match="pinned latent upscaler"):
        _workflow(
            h3_settings(match_source_resolution=True),
            {role: path for role, path in MODEL_PATHS.items() if role != H3_UPSCALER_ROLE},
        )


def test_worker_rejects_source_size_jobs_before_any_inference(tmp_path):
    settings = I2VWorkerSettings(
        profile="minimax_h3",
        environment="test",
        model_objects_json=SecretStr(_worker_model_objects(_environment_settings(False))),
        source_revision="b" * 40,
        private_manifest_source_sha256="c" * 64,
        comfy_root=tmp_path / "comfy",
        runtime_root=tmp_path / "runtime",
        workflow_template=ROOT / "workflows/dasiwa-minimax-h3-i2v-v1.api.json",
    )
    job = _job()
    job["negative_prompt"] = ""
    job["settings_snapshot"] = h3_settings(match_source_resolution=True).model_dump(mode="json")
    app = create_i2v_worker_app(settings, supervisor=_Supervisor())
    with TestClient(app) as client:
        response = client.post("/jobs/i2v", json=job)
    assert response.status_code == 409 and "upscaler" in response.text


async def test_readiness_requires_all_upscaler_nodes_only_for_enabled_worker():
    kwargs = dict(
        base_url="http://127.0.0.1:8188",
        request_timeout_seconds=5,
        network_attempts=1,
        poll_seconds=1,
        profile="minimax_h3",
    )
    old = ComfyClient(**kwargs)
    new = ComfyClient(**kwargs, source_resolution_enabled=True)
    try:
        assert not any("Upscale" in name for name, _ in old.required_nodes)
        assert {
            "MMH3UltimateUpscale",
            "MMH3LatentUpscaleWithModelParams",
            "MMH3TemporalSplitParams",
        } <= {name for name, _ in new.required_nodes}
    finally:
        await old.close()
        await new.close()
