from __future__ import annotations

import importlib.util
import json
import urllib.request
from pathlib import Path
from typing import Any, Self

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "runpod_i2v_endpoint.py"
ADOPTED_VOLUME_ID = "6c4m45zvpo"


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
        network_volume_id=ADOPTED_VOLUME_ID,
        workers_min=workers_min,
    )


def test_plan_scales_to_zero_and_preserves_provider_neutral_worker_contract() -> None:
    module = _module()
    spec = _spec(module)

    assert spec["volume"] == {
        "id": ADOPTED_VOLUME_ID,
        "name": module.VOLUME_NAME_PREFIX,
        "size": 100,
        "dataCenterId": "EU-RO-1",
    }
    assert spec["endpoint"]["workersMin"] == 0
    assert spec["endpoint"]["workersMax"] == 1
    assert spec["endpoint"]["gpuTypeIds"] == [
        "NVIDIA GeForce RTX 5090",
        "NVIDIA A40",
        "NVIDIA RTX A6000",
        "NVIDIA L40S",
        "NVIDIA RTX PRO 4500 Blackwell",
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


def test_adopted_preseed_state_binds_the_exact_volume_id(tmp_path: Path) -> None:
    module = _module()
    state_file = tmp_path / "preseed-state.json"
    state_file.write_text(
        json.dumps(
            {
                "schema": module.PRESEED_STATE_SCHEMA,
                "status": "ready",
                "network_volume_id": ADOPTED_VOLUME_ID,
                "model_objects_sha256": "d" * 64,
                "artifact_identity_sha256": "e" * 64,
            }
        ),
        encoding="utf-8",
    )

    assert (
        module._read_adopted_preseed_volume_id(
            state_file,
            model_objects_sha256="d" * 64,
            artifact_identity_sha256="e" * 64,
        )
        == ADOPTED_VOLUME_ID
    )

    state = json.loads(state_file.read_text(encoding="utf-8"))
    state["model_objects_sha256"] = "f" * 64
    state_file.write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(RuntimeError, match="adopted RunPod preseed state is invalid"):
        module._read_adopted_preseed_volume_id(
            state_file,
            model_objects_sha256="d" * 64,
            artifact_identity_sha256="e" * 64,
        )


def test_graphql_readback_uses_bearer_auth_without_leaking_key_in_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()

    class _Response:
        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def read(self, _: int) -> bytes:
            return json.dumps({"data": {"myself": {"endpoints": [{"id": "endpoint-1"}]}}}).encode()

    def fake_urlopen(request: Any, *, timeout: int) -> _Response:
        assert request.full_url == module.RUNPOD_GRAPHQL_ROOT
        assert request.headers["Authorization"] == "Bearer test-secret-key"
        assert timeout == 60
        return _Response()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    assert module._graphql_endpoint("test-secret-key", "endpoint-1") == {"id": "endpoint-1"}


def test_graphql_readback_rejects_partial_data_with_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()

    class _Response:
        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def read(self, _: int) -> bytes:
            return json.dumps(
                {
                    "errors": [{"message": "partial failure"}],
                    "data": {"myself": {"endpoints": [{"id": "endpoint-1"}]}},
                }
            ).encode()

    monkeypatch.setattr(urllib.request, "urlopen", lambda *_args, **_kwargs: _Response())

    with pytest.raises(RuntimeError, match="GraphQL endpoint verification failed"):
        module._graphql_endpoint("test-secret-key", "endpoint-1")


def test_scheduler_readback_rejects_region_volume_mismatch() -> None:
    module = _module()

    with pytest.raises(RuntimeError, match="scheduler drift: locations, networkVolumeId"):
        module._verify_endpoint_scheduler(
            {
                "locations": "EU-RO-1",
                "networkVolumeId": "volume-eu",
                "templateId": "template-1",
                "workersMin": 0,
                "workersMax": 1,
                "pods": [],
            },
            expected_data_center_id="US-IL-1",
            template_id="template-1",
            volume_id="volume-us",
            workers_min=0,
            workers_max=1,
        )


@pytest.mark.parametrize(
    ("field", "drifted"),
    [
        ("computeType", "CPU"),
        ("gpuCount", 2),
        ("allowedCudaVersions", ["12.8"]),
        ("minCudaVersion", None),
        ("flashboot", False),
        ("idleTimeout", 120),
        ("executionTimeoutMs", 1),
        ("scalerType", "REQUEST_COUNT"),
        ("scalerValue", 2),
    ],
)
def test_endpoint_readback_rejects_cost_or_runtime_plan_drift(
    field: str,
    drifted: object,
) -> None:
    module = _module()
    expected = _spec(module)["endpoint"]
    actual = {
        **expected,
        "id": "endpoint-1",
        "templateId": "template-1",
        "networkVolumeId": "volume-1",
        "networkVolumeIds": ["volume-1"],
        field: drifted,
    }

    with pytest.raises(RuntimeError, match=rf"endpoint drift:.*{field}"):
        module._verify_endpoint(
            actual,
            expected,
            template_id="template-1",
            volume_id="volume-1",
        )


def test_endpoint_readback_allows_only_proven_rest_field_omissions() -> None:
    module = _module()
    expected = _spec(module)["endpoint"]
    actual = {
        **expected,
        "id": "endpoint-1",
        "templateId": "template-1",
        "networkVolumeId": "volume-1",
        "networkVolumeIds": ["volume-1"],
    }
    actual.pop("dataCenterIds")
    actual.pop("computeType")

    module._verify_endpoint(
        actual,
        expected,
        template_id="template-1",
        volume_id="volume-1",
    )


def test_apply_requires_explicit_spend_release(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _module()
    monkeypatch.delenv(module.SPEND_SWITCH, raising=False)
    monkeypatch.setenv("RUNPOD_API_KEY", "not-used")

    with pytest.raises(RuntimeError, match=module.SPEND_SWITCH):
        module.apply(_spec(module), state_file=tmp_path / "state.json")


def test_apply_rejects_same_shape_replacement_volume_before_any_mutation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _module()
    spec = _spec(module)
    replacement = {**spec["volume"], "id": "same-shape-replacement"}
    listed: list[str] = []
    mutations: list[tuple[str, str]] = []
    monkeypatch.setenv(module.SPEND_SWITCH, "true")
    monkeypatch.setenv("RUNPOD_API_KEY", "test-key-never-persisted")

    def fake_list(_api_key: str, path: str) -> list[dict[str, Any]]:
        listed.append(path)
        assert path == "/networkvolumes"
        return [replacement]

    def fake_mutate(
        _api_key: str,
        path: str,
        *,
        method: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        del payload
        mutations.append((method, path))
        raise AssertionError("provider mutation must not be attempted")

    monkeypatch.setattr(module, "_list", fake_list)
    monkeypatch.setattr(module, "_mutate", fake_mutate)

    state_file = tmp_path / "state.json"
    with pytest.raises(RuntimeError, match="network volume drift: id"):
        module.apply(spec, state_file=state_file)

    assert listed == ["/networkvolumes"]
    assert mutations == []
    assert not state_file.exists()


def test_apply_reuses_exact_adopted_volume_and_creates_template_and_endpoint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _module()
    spec = _spec(module)
    state_file = tmp_path / "state.json"
    monkeypatch.setenv(module.SPEND_SWITCH, "true")
    monkeypatch.setenv("RUNPOD_API_KEY", "test-key-never-persisted")
    resources: dict[str, list[dict[str, Any]]] = {
        "/networkvolumes": [dict(spec["volume"])],
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
    monkeypatch.setattr(
        module,
        "_graphql_endpoint",
        lambda _api_key, endpoint_id: {
            "id": endpoint_id,
            "locations": module.DATA_CENTER_ID,
            "networkVolumeId": ADOPTED_VOLUME_ID,
            "templateId": "template-1",
            "workersMin": 0,
            "workersMax": 1,
            "pods": [],
        },
    )

    state = module.apply(spec, state_file=state_file)

    assert state["status"] == "ready"
    assert state["endpoint_id"] == "endpoint-1"
    assert state["network_volume_id"] == ADOPTED_VOLUME_ID
    assert mutations == [
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
    volume = dict(cold_spec["volume"])
    template = {**cold_spec["template"], "id": "template-1"}
    endpoint = {
        **cold_spec["endpoint"],
        "id": "endpoint-1",
        "templateId": "template-1",
        "networkVolumeId": ADOPTED_VOLUME_ID,
        "networkVolumeIds": [ADOPTED_VOLUME_ID],
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
    monkeypatch.setattr(
        module,
        "_graphql_endpoint",
        lambda _api_key, endpoint_id: {
            "id": endpoint_id,
            "locations": module.DATA_CENTER_ID,
            "networkVolumeId": ADOPTED_VOLUME_ID,
            "templateId": "template-1",
            "workersMin": resources["/endpoints"][0]["workersMin"],
            "workersMax": 1,
            "pods": [],
        },
    )

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
    volume = dict(spec["volume"])
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
        "networkVolumeId": ADOPTED_VOLUME_ID,
        "networkVolumeIds": [ADOPTED_VOLUME_ID],
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
    monkeypatch.setattr(
        module,
        "_graphql_endpoint",
        lambda _api_key, endpoint_id: {
            "id": endpoint_id,
            "locations": module.DATA_CENTER_ID,
            "networkVolumeId": ADOPTED_VOLUME_ID,
            "templateId": "template-1",
            "workersMin": 0,
            "workersMax": 1,
            "pods": [],
        },
    )

    assert module.apply(spec, state_file=state_file)["status"] == "ready"
    assert len(mutations) == 1
    assert mutations[0][2]["env"]["GEN_I2V_WORKER_ALLOWED_GPU_NAMES_CSV"] == (
        ",".join(spec["endpoint"]["gpuTypeIds"])
    )
