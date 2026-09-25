"""Source-size pipeline contracts; no weights, GPU jobs, or paid services."""

import hashlib
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from gen_automation.config import Settings
from gen_automation.i2v_worker.app import create_i2v_worker_app
from gen_automation.i2v_worker.comfy import ComfyClient
from gen_automation.i2v_worker.comfy_h3_upscale import (
    ManagedH3SourceUpscale,
    h3_refinement_split_params,
)
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


def test_refinement_reuses_loras_and_base_audio_but_anchors_to_full_source():
    graph = _workflow(
        h3_settings(
            width=1152,
            height=1504,
            match_source_resolution=True,
            h3_loras=[{"artifact_id": str(uuid4()), "sha256": "d" * 64, "strength": 0.6}],
        )
    )
    assert (graph["6"]["inputs"]["width"], graph["6"]["inputs"]["height"]) == (768, 992)
    assert graph["7"]["inputs"]["model"] == ["h3-lora-0", 0]
    upscale = graph["h3-source-upscale"]["inputs"]
    assert upscale["model"] == graph["9"]["inputs"]["model"] == ["7", 0]
    assert upscale["first_frame"] == ["1", 0] and upscale["conditioning"] == ["6", 0]
    assert (upscale["width"], upscale["height"]) == (1152, 1504)
    assert graph["h3-refine-sigmas"]["inputs"] == {
        "model": ["7", 0],
        "scheduler": "simple",
        "steps": H3_REFINE_STEPS,
        "denoise": H3_REFINE_DENOISE,
    }
    assert graph["13"]["inputs"]["samples"] == ["h3-source-upscale", 0]
    assert graph["15"]["inputs"]["samples"] == ["12", 0]  # no refinement of original audio


def test_standard_mode_and_small_originals_do_not_add_a_refinement_pass():
    for settings in [h3_settings(), h3_settings(match_source_resolution=True)]:
        graph = _workflow(settings)
        assert len(graph) == 16
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
        assert {"ManagedH3SourceUpscale", "MinimaxH3LatentUpscaler3D", "MMH3SplitUpscale"} <= {
            name for name, _ in new.required_nodes
        }
    finally:
        await old.close()
        await new.close()


class _Tensor:
    def __init__(self, shape):
        self.shape = shape
        self.ndim = len(shape)

    def __getitem__(self, _slice):
        return self


class _Nested:
    def __init__(self, tensors):
        self.tensors = tensors


@pytest.fixture
def adapter(monkeypatch):
    mm = ModuleType("comfy.model_management")
    mm.unload_all_models = Mock()
    nt = ModuleType("comfy.nested_tensor")
    nt.NestedTensor = _Nested
    comfy = ModuleType("comfy")
    comfy.model_management, comfy.nested_tensor = mm, nt
    helpers = ModuleType("node_helpers")
    helpers.conditioning_set_values = Mock(return_value="full-source-conditioning")
    nodes = ModuleType("nodes")
    nodes.NODE_CLASS_MAPPINGS = {
        name: SimpleNamespace(execute=Mock())
        for name in (
            "MinimaxH3LatentUpscaler3D",
            "MMH3TemporalSplitParamsV10",
            "MMH3SpatialSplitParamsV10",
            "MMH3SplitUpscale",
        )
    }
    for name, module in {
        "comfy": comfy,
        "comfy.model_management": mm,
        "comfy.nested_tensor": nt,
        "node_helpers": helpers,
        "nodes": nodes,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)
    up = nodes.NODE_CLASS_MAPPINGS["MinimaxH3LatentUpscaler3D"].execute
    refine = nodes.NODE_CLASS_MAPPINGS["MMH3SplitUpscale"].execute
    high = _Tensor((1, 24, 41, 94, 72))
    up.return_value = ({"samples": high},)
    refine.return_value = ({"samples": _Nested((high, "do-not-use-refined-audio"))},)
    for name in ("MMH3TemporalSplitParamsV10", "MMH3SpatialSplitParamsV10"):
        nodes.NODE_CLASS_MAPPINGS[name].execute.return_value = ({"test": name},)
    kwargs = dict(
        latent={"samples": _Nested((_Tensor((1, 24, 41, 62, 48)), _Tensor((1, 32, 2, 400))))},
        conditioning="base-conditioning",
        first_frame=_Tensor((1, 1504, 1152, 3)),
        vae=SimpleNamespace(encode=Mock(return_value="high-keyframe")),
        model="patched-h3",
        noise="seeded-noise",
        sampler="euler",
        sigmas="refine-sigmas",
        width=1152,
        height=1504,
        model_name=H3_UPSCALER_FILENAME,
    )
    return kwargs, up, refine, helpers, mm


def test_adapter_separates_video_and_keeps_audio_identity(adapter):
    kwargs, up, refine, helpers, mm = adapter
    result = ManagedH3SourceUpscale().upscale(**kwargs)[0]["samples"]
    original_video, original_audio = kwargs["latent"]["samples"].tensors
    assert result.tensors[1] is original_audio
    assert up.call_args.kwargs["latent"]["samples"] is original_video
    assert up.call_args.kwargs["force_unload"] is True
    assert up.call_args.kwargs["mode"] == {
        "mode": "target dimensions",
        "width": 1152,
        "height": 1504,
    }
    assert refine.call_args.kwargs["latent"]["samples"].tensors[1] is original_audio
    assert refine.call_args.kwargs["conditioning"] == "full-source-conditioning"
    assert refine.call_args.kwargs["model"] == "patched-h3"
    helpers.conditioning_set_values.assert_called_once_with(
        "base-conditioning",
        {"minimax_keyframes": [{"resolved_frame_index": 0, "latent": "high-keyframe"}]},
    )
    mm.unload_all_models.assert_called_once()


def test_refiner_uses_only_native_supported_boundary_anchors(adapter):
    kwargs, _, refine, _, _ = adapter
    nodes = sys.modules["nodes"].NODE_CLASS_MAPPINGS
    temporal, spatial = h3_refinement_split_params(nodes)
    nodes["MMH3TemporalSplitParamsV10"].execute.assert_called_once_with(
        chunk_frames=56,
        temporal_overlap_frames=22,
        anchor_strength=0.999,
        motion_anchor_frames="0",
        identity_anchor_frames=0,
    )
    ManagedH3SourceUpscale().upscale(**kwargs)
    assert refine.call_args.kwargs["temporal_split_param"] is temporal
    assert refine.call_args.kwargs["spatial_split_param"] is spatial


@pytest.mark.parametrize("stage", ["upscale", "refine"])
def test_adapter_rejects_changed_duration_or_dimensions(adapter, stage):
    kwargs, up, refine, _, _ = adapter
    bad = _Tensor((1, 24, 40, 94, 72))
    if stage == "upscale":
        up.return_value = ({"samples": bad},)
    else:
        refine.return_value = ({"samples": _Nested((bad, "audio"))},)
    with pytest.raises(ValueError, match="duration"):
        ManagedH3SourceUpscale().upscale(**kwargs)
