from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "runpod_i2v_endpoint.py"


def _module() -> Any:
    spec = importlib.util.spec_from_file_location("runpod_i2v_endpoint", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _spec(module: Any, *, workers_min: int = 0) -> dict[str, dict[str, Any]]:
    model_objects = json.dumps(
        [
            {
                "role": role,
                "bucket": "private-models",
                "key": f"worker/i2v/{role}",
                "version_id": f"version-{role}",
                "byte_size": 1,
                "sha256": f"{index:064x}",
                "install_path": install_path,
            }
            for index, (role, install_path) in enumerate(
                (
                    ("diffusion_model_high", "models/diffusion_models/high.safetensors"),
                    ("diffusion_model_low", "models/diffusion_models/low.safetensors"),
                    ("text_encoder", "models/text_encoders/text.safetensors"),
                    ("vae", "models/vae/Wan/vae.safetensors"),
                ),
                start=1,
            )
        ],
        separators=(",", ":"),
        sort_keys=True,
    )
    return module._spec(
        image="ghcr.io/neuraln-cyber/gen-automation/i2v-worker@sha256:" + "a" * 64,
        source_revision="b" * 40,
        manifest_source_sha256="c" * 64,
        model_objects_json=model_objects,
        model_objects_sha256="d" * 64,
        workers_min=workers_min,
    )


def test_plan_scales_to_zero_and_preserves_provider_neutral_worker_contract() -> None:
    module = _module()
    spec = _spec(module)

    assert spec["volume"] == {
        "name": f"{module.VOLUME_NAME_PREFIX}-{module.DATA_CENTER_ID.casefold()}",
        "size": 100,
        "dataCenterId": "US-IL-1",
    }
    assert spec["endpoint"]["workersMin"] == 0
    assert spec["endpoint"]["workersMax"] == 1
    assert spec["endpoint"]["gpuTypeIds"] == [
        "NVIDIA GeForce RTX 5090",
        "NVIDIA A40",
        "NVIDIA RTX A6000",
        "NVIDIA L40S",
    ]
    assert spec["template"]["env"]["GEN_I2V_WORKER_ALLOWED_GPU_NAMES_CSV"] == (
        ",".join(spec["endpoint"]["gpuTypeIds"])
    )
    assert spec["endpoint"]["flashboot"] is True
    assert spec["template"]["dockerEntrypoint"] == []
    assert spec["template"]["env"]["GEN_I2V_WORKER_LORA_WORKER_ENABLED"] == "true"
    assert spec["template"]["env"]["GEN_I2V_WORKER_REQUIRE_PRESEEDED_VOLUME"] == "true"
    assert "AWS_" not in json.dumps(spec)
    assert "RUNPOD_API_KEY" not in json.dumps(spec)


def test_apply_requires_explicit_spend_release(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _module()
    monkeypatch.delenv(module.SPEND_SWITCH, raising=False)
    monkeypatch.setenv("RUNPOD_API_KEY", "not-used")

    with pytest.raises(RuntimeError, match=module.SPEND_SWITCH):
        module.apply(_spec(module), state_file=tmp_path / "state.json")


def test_apply_creates_exact_volume_template_and_single_endpoint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _module()
    spec = _spec(module)
    state_file = tmp_path / "state.json"
    monkeypatch.setenv(module.SPEND_SWITCH, "true")
    monkeypatch.setenv("RUNPOD_API_KEY", "test-key-never-persisted")
    resources: dict[str, list[dict[str, Any]]] = {
        "/networkvolumes": [],
        "/templates?includeEndpointBoundTemplates=true": [],
        "/endpoints": [],
    }
    mutations: list[tuple[str, str]] = []

    def fake_list(_api_key: str, path: str) -> list[dict[str, Any]]:
        return resources[path]

    def fake_mutate(
        _api_key: str,
        path: str,
        *,
        method: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        persisted = json.loads(state_file.read_text(encoding="utf-8"))
        mutations.append((method, path))
        if path == "/networkvolumes":
            assert persisted["resources"]["volume"]["create_attempted"] is True
            created = {**payload, "id": "volume-1"}
            resources[path].append(created)
            return created
        if path == "/templates":
            assert persisted["resources"]["template"]["create_attempted"] is True
            created = {**payload, "id": "template-1"}
            resources["/templates?includeEndpointBoundTemplates=true"].append(created)
            return created
        assert path == "/endpoints"
        assert persisted["resources"]["endpoint"]["create_attempted"] is True
        created = {**payload, "id": "endpoint-1"}
        resources[path].append(created)
        return created

    monkeypatch.setattr(module, "_list", fake_list)
    monkeypatch.setattr(module, "_mutate", fake_mutate)

    state = module.apply(spec, state_file=state_file)

    assert state["status"] == "ready"
    assert state["endpoint_id"] == "endpoint-1"
    assert state["network_volume_id"] == "volume-1"
    assert mutations == [
        ("POST", "/networkvolumes"),
        ("POST", "/templates"),
        ("POST", "/endpoints"),
    ]
    assert "test-key-never-persisted" not in state_file.read_text(encoding="utf-8")


def test_apply_temporarily_warms_then_scales_existing_endpoint_to_zero(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _module()
    cold_spec = _spec(module)
    warm_spec = _spec(module, workers_min=1)
    state_file = tmp_path / "state.json"
    monkeypatch.setenv(module.SPEND_SWITCH, "true")
    monkeypatch.setenv("RUNPOD_API_KEY", "test-key-never-persisted")
    volume = {**cold_spec["volume"], "id": "volume-1"}
    template = {**cold_spec["template"], "id": "template-1"}
    endpoint = {
        **cold_spec["endpoint"],
        "id": "endpoint-1",
        "templateId": "template-1",
        "networkVolumeId": "volume-1",
    }
    resources = {
        "/networkvolumes": [volume],
        "/templates?includeEndpointBoundTemplates=true": [template],
        "/endpoints": [endpoint],
    }
    patches: list[int] = []

    def fake_list(_api_key: str, path: str) -> list[dict[str, Any]]:
        return resources[path]

    def fake_mutate(
        _api_key: str,
        path: str,
        *,
        method: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        assert path == "/endpoints/endpoint-1"
        assert method == "PATCH"
        assert "computeType" not in payload
        patches.append(int(payload["workersMin"]))
        updated = {**payload, "id": "endpoint-1"}
        resources["/endpoints"][:] = [updated]
        return updated

    monkeypatch.setattr(module, "_list", fake_list)
    monkeypatch.setattr(module, "_mutate", fake_mutate)

    assert module.apply(warm_spec, state_file=state_file)["status"] == "ready"
    assert resources["/endpoints"][0]["workersMin"] == 1
    assert module.apply(cold_spec, state_file=state_file)["status"] == "ready"
    assert resources["/endpoints"][0]["workersMin"] == 0
    assert patches == [1, 0]


def test_apply_updates_existing_template_with_mutable_fields_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _module()
    spec = _spec(module)
    state_file = tmp_path / "state.json"
    monkeypatch.setenv(module.SPEND_SWITCH, "true")
    monkeypatch.setenv("RUNPOD_API_KEY", "test-key-never-persisted")
    volume = {**spec["volume"], "id": "volume-1"}
    template = {
        **spec["template"],
        "id": "template-1",
        "env": {
            key: value
            for key, value in spec["template"]["env"].items()
            if key != "GEN_I2V_WORKER_ALLOWED_GPU_NAMES_CSV"
        },
    }
    endpoint = {
        **spec["endpoint"],
        "id": "endpoint-1",
        "templateId": "template-1",
        "networkVolumeId": "volume-1",
    }
    resources = {
        "/networkvolumes": [volume],
        "/templates?includeEndpointBoundTemplates=true": [template],
        "/endpoints": [endpoint],
    }
    mutations: list[tuple[str, str, dict[str, Any]]] = []

    def fake_list(_api_key: str, path: str) -> list[dict[str, Any]]:
        return resources[path]

    def fake_mutate(
        _api_key: str,
        path: str,
        *,
        method: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        mutations.append((method, path, payload))
        assert path == "/templates/template-1"
        assert method == "PATCH"
        assert "category" not in payload
        assert "isServerless" not in payload
        updated = {**template, **payload}
        resources["/templates?includeEndpointBoundTemplates=true"][:] = [updated]
        return updated

    monkeypatch.setattr(module, "_list", fake_list)
    monkeypatch.setattr(module, "_mutate", fake_mutate)

    assert module.apply(spec, state_file=state_file)["status"] == "ready"
    assert len(mutations) == 1
    assert mutations[0][2]["env"]["GEN_I2V_WORKER_ALLOWED_GPU_NAMES_CSV"] == (
        ",".join(spec["endpoint"]["gpuTypeIds"])
    )
