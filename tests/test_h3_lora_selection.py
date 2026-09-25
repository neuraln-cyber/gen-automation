"""Owner selection through dispatch and worker loading, with synthetic local files only."""

import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx2
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from gen_automation.domain.i2v import I2VAttemptSnapshot, I2VJobSnapshot
from gen_automation.domain.i2v_loras import I2VLoraSettingsKind, classify_i2v_lora_settings
from gen_automation.i2v_worker.h3_loras import materialize_h3_lora
from gen_automation.i2v_worker.media import MediaError
from gen_automation.i2v_worker.models import H3LoraGrant, I2VJob
from gen_automation.i2v_worker.workflow import (
    load_workflow_template,
    lora_provenance,
    render_workflow,
)
from gen_automation.services.h3_loras import H3LoraUnavailableError, resolve_h3_loras
from gen_automation.services.i2v_environment import i2v_salad_runtime_config_from_settings
from gen_automation.services.i2v_media import I2VSignedGrantBuilder
from gen_automation.services.i2v_runtime import (
    I2VRuntimeConfigurationError,
    _validate_fresh_grants,
)
from gen_automation.storage.memory import MemoryObjectStore
from tests.test_h3_lora_library import _body, _capture, _create, _setup
from tests.test_i2v_api import _complete_uploaded_input
from tests.test_i2v_environment import _settings as _control_settings
from tests.test_i2v_h3_readiness import h3_settings
from tests.test_i2v_worker_app import _job, _settings
from tests.test_i2v_worker_comfy import _client

ROOT = Path(__file__).parents[1]


@pytest.mark.parametrize("enabled", [False, True])
def test_h3_selection_gate_reaches_salad_dispatch(enabled):
    settings = _control_settings(lora_profile_enabled=False, lora_worker_enabled=False)
    manifest = json.loads(settings.i2v_model_manifest_json.get_secret_value())
    manifest["objects"] = [
        {
            **manifest["objects"][0],
            "role": role,
            "name": role,
            "target_filename": f"{role}.safetensors",
        }
        for role in ("diffusion_model", "text_encoder", "video_vae", "audio_vae")
    ]
    encoded = json.dumps(manifest)
    settings = settings.model_copy(
        update={
            "i2v_profile": "minimax_h3",
            "i2v_h3_loras_enabled": enabled,
            "i2v_runpod_enabled": False,
            "i2v_model_manifest_json": SecretStr(encoded),
            "i2v_model_manifest_sha256": SecretStr(hashlib.sha256(encoded.encode()).hexdigest()),
            "i2v_salad_gpu_class_id": uuid4(),
        }
    )
    runtime = i2v_salad_runtime_config_from_settings(settings)
    assert runtime.reviewed_loras_enabled is enabled
    assert runtime.profile == "minimax_h3"


def _library(client):
    memory, runtime = _setup(client)
    identifier = _create(client)
    _capture(client, memory, identifier)
    assert client.portal.call(runtime.import_once)
    assert client.portal.call(runtime.lifecycle_once)
    client.app.state.settings.i2v_profile = "minimax_h3"
    client.app.state.settings.i2v_h3_loras_enabled = True
    entry = client.get("/api/v1/loras?library=h3").json()["entries"][0]
    return memory, runtime, entry


def _selection(entry, strength=0.65):
    return {"artifact_id": entry["id"], "sha256": entry["sha256"], "strength": strength}


def test_selection_preset_queue_private_grants_and_safe_deletion(client: TestClient):
    memory, runtime, entry = _library(client)
    catalog = client.get("/api/v1/i2v/h3-loras").json()
    assert catalog["profile_enabled"]
    assert catalog["maximum_selections"] == 0
    assert catalog["loras"][0]["catalog_id"] == entry["id"]
    assert "https://" not in str(catalog)
    selection = _selection(entry)
    settings = h3_settings(h3_loras=[selection]).model_dump(mode="json")
    assert classify_i2v_lora_settings(settings) == I2VLoraSettingsKind.REVIEWED
    assets = MemoryObjectStore(bucket="i2v-tests")
    client.app.state.object_store = assets
    source = _complete_uploaded_input(client, assets)
    payload = {"name": "My H3 preset", "positive_prompt": "Leaves sway.", "settings": settings}
    preset = client.post("/api/v1/i2v/presets", json=payload)
    assert preset.status_code == 201, preset.text
    assert preset.json()["settings"]["h3_loras"] == [selection]
    result = client.post(
        "/api/v1/i2v/jobs",
        json={
            "input_id": source["input_id"],
            "preset_id": preset.json()["preset_id"],
        },
    )
    assert result.status_code == 201, result.text
    job = I2VJobSnapshot.model_validate(result.json()["jobs"][0])
    assert job.settings_snapshot["h3_loras"] == [selection]
    assert "https://" not in str(job.settings_snapshot)

    # Signed grants are built from immutable library versions at dispatch, not
    # saved in the preset/job. Spy only on in-memory stores, never AWS.
    memory.presign_download = AsyncMock(wraps=memory.presign_download)
    builder = I2VSignedGrantBuilder(
        store=assets,
        expires_in=3600,
        model_store=memory,
        sessions=client.app.state.database.sessions,
    )
    attempt = I2VAttemptSnapshot.model_construct(attempt_id=uuid4())
    additions = client.portal.call(lambda: builder.build(job=job, attempt=attempt))
    grant = additions["h3_lora_grants"][0]
    assert grant["artifact_id"] == entry["id"]
    assert grant["sha256"] == entry["sha256"]
    assert grant["byte_size"] == len(_body())
    call = memory.presign_download.await_args.kwargs
    assert call["key"] == f"worker/managed-loras/sha256/{entry['sha256']}.safetensors"
    assert call["version_id"]
    # Memory storage uses a non-network scheme; production grants require HTTPS.
    for media in (additions["input_grant"], additions["output_grant"], grant["download"]):
        media["url"] = media["url"].replace("memory://", "https://", 1)
    _validate_fresh_grants(
        additions, job=job, attempt=attempt, output_prefix="i2v/outputs", now=datetime.now(UTC)
    )
    grant["sha256"] = "f" * 64
    with pytest.raises(I2VRuntimeConfigurationError, match="frozen job"):
        _validate_fresh_grants(
            additions, job=job, attempt=attempt, output_prefix="i2v/outputs", now=datetime.now(UTC)
        )

    retired = client.post(
        f"/api/v1/loras/{entry['id']}:retire",
        json={"purge_requested": True},
        headers={"Idempotency-Key": "retire-selected"},
    )
    assert retired.status_code == 200, retired.text
    assert client.get("/api/v1/i2v/h3-loras").json()["loras"] == []
    assert not client.portal.call(runtime.lifecycle_once)  # queued job protects bytes
    assert client.portal.call(lambda: builder.build(job=job, attempt=attempt))["h3_lora_grants"]
    blocked = client.post("/api/v1/i2v/presets", json=payload)
    assert blocked.status_code == 409, blocked.text
    assert client.post(f"/api/v1/i2v/jobs/{job.job_id}:cancel").status_code == 200
    assert client.post(f"/api/v1/i2v/jobs/{job.job_id}:retry").status_code == 409


def test_catalog_and_validation_fail_closed(client: TestClient):
    _, _, entry = _library(client)
    selection = _selection(entry)
    payload = {
        "name": "Selection",
        "settings": h3_settings(h3_loras=[selection]).model_dump(mode="json"),
    }
    client.app.state.settings.i2v_h3_loras_enabled = False
    assert not client.get("/api/v1/i2v/h3-loras").json()["loras"][0]["available"]
    assert client.post("/api/v1/i2v/presets", json=payload).status_code == 409
    client.app.state.settings.i2v_h3_loras_enabled = True
    payload["settings"]["h3_loras"][0]["sha256"] = "f" * 64
    assert client.post("/api/v1/i2v/presets", json=payload).status_code == 409
    payload["settings"]["h3_loras"][0]["sha256"] = entry["sha256"]

    async def check_unowned():
        async with client.app.state.database.sessions() as session:
            with pytest.raises(H3LoraUnavailableError, match="your library"):
                await resolve_h3_loras(session, payload["settings"], actor_user_id=uuid4())

    client.portal.call(check_unowned)


def test_h3_workflow_chains_selected_weights_and_does_not_leak_to_next_job():
    selected = [
        {"artifact_id": str(uuid4()), "sha256": char * 64, "strength": strength}
        for char, strength in [("a", 0.65), ("b", 0), ("c", -0.2)]
    ]
    template = load_workflow_template(
        ROOT / "workflows/dasiwa-minimax-h3-i2v-v1.api.json", profile="minimax_h3"
    )

    def render(settings):
        return render_workflow(
            template,
            input_filename="input.png",
            positive_prompt="Leaves sway.",
            negative_prompt="",
            settings=settings,
            job_id=uuid4(),
            attempt_id=uuid4(),
            model_paths={
                role: f"{role}.safetensors"
                for role in ("diffusion_model", "text_encoder", "video_vae", "audio_vae")
            },
        )[0]

    settings = h3_settings(h3_loras=selected)
    graph = render(settings)
    nodes = [node for node in graph.values() if node["class_type"] == "ManagedH3LoraLoader"]
    assert [node["inputs"]["strength_model"] for node in nodes] == [0.65, -0.2]
    assert nodes[0]["inputs"]["model"] == ["2", 0]
    assert nodes[1]["inputs"]["model"] == ["h3-lora-0", 0]
    assert graph["7"]["inputs"]["model"] == ["h3-lora-2", 0]
    assert lora_provenance(settings) == selected
    baseline = render(h3_settings())
    assert baseline["7"]["inputs"]["model"] == ["2", 0]
    assert not any(node["class_type"] == "ManagedH3LoraLoader" for node in baseline.values())


def test_worker_requires_exact_grants_and_finite_strengths():
    selected = {"artifact_id": str(uuid4()), "sha256": "a" * 64, "strength": 1.0}
    job = _job()
    job["negative_prompt"] = ""
    job["settings_snapshot"] = h3_settings(h3_loras=[selected]).model_dump(mode="json")
    with pytest.raises(ValidationError, match="grant"):
        I2VJob.model_validate(job)
    job["h3_lora_grants"] = [
        {
            **{k: selected[k] for k in ("artifact_id", "sha256")},
            "byte_size": 1,
            "download": job["input_grant"],
        }
    ]
    assert I2VJob.model_validate(job).settings_snapshot.h3_loras[0].strength == 1
    for strength in (float("nan"), float("inf"), True, "1", 101):
        with pytest.raises(ValidationError):
            h3_settings(h3_loras=[{**selected, "strength": strength}])
    with pytest.raises(ValidationError, match="unique"):
        h3_settings(h3_loras=[selected, selected])


def _download_fixture(tmp_path, monkeypatch, *, corrupt=False, resume=False):
    body = b"synthetic-neutral-adapter-fixture"
    sha = hashlib.sha256(body).hexdigest()
    settings = _settings(tmp_path).model_copy(
        update={
            "require_private_delivery": True,
            "model_delivery_domain": "d123abc.cloudfront.net",
            "artifact_chunk_bytes": 8,
            "artifact_download_concurrency": 2,
            "network_attempts": 1,
        }
    )
    grant = H3LoraGrant.model_validate(
        {
            "artifact_id": str(uuid4()),
            "sha256": sha,
            "byte_size": len(body),
            "download": {
                "method": "GET",
                "url": f"https://d123abc.cloudfront.net/models/worker/managed-loras/sha256/{sha}.safetensors",
                "expires_at": datetime.now(UTC) + timedelta(hours=1),
            },
        }
    )
    ranges = []

    def handle(request):
        start, end = map(int, request.headers["Range"].removeprefix("bytes=").split("-"))
        ranges.append((start, end))
        data = body[start : end + 1]
        if corrupt:
            data = b"x" * len(data)
        return httpx2.Response(
            206, content=data, headers={"Content-Range": f"bytes {start}-{end}/{len(body)}"}
        )

    original = httpx2.Client
    monkeypatch.setattr(
        "gen_automation.i2v_worker.h3_loras.httpx2.Client",
        lambda **kwargs: original(transport=httpx2.MockTransport(handle), **kwargs),
    )
    if resume:
        root = settings.comfy_root / "models/loras/managed-h3"
        root.mkdir(parents=True)
        (root / f".{sha}.partial").write_bytes(body[:8])
    return body, grant, settings, ranges


@pytest.mark.parametrize("resume", [False, True])
def test_worker_private_parallel_download_hash_and_cache(tmp_path, monkeypatch, resume):
    body, grant, settings, ranges = _download_fixture(tmp_path, monkeypatch, resume=resume)
    path = materialize_h3_lora(grant, settings)
    assert path.read_bytes() == body
    assert min(start for start, _ in ranges) == (8 if resume else 0)
    count = len(ranges)
    assert materialize_h3_lora(grant, settings) == path
    assert len(ranges) == count


def test_worker_rejects_corrupt_bytes_off_route_and_naive_expiry(tmp_path, monkeypatch):
    _, grant, settings, ranges = _download_fixture(tmp_path, monkeypatch, corrupt=True)
    with pytest.raises(MediaError, match="checksum"):
        materialize_h3_lora(grant, settings)
    assert not list((settings.comfy_root / "models/loras/managed-h3").glob("*.safetensors"))
    invalid = grant.model_dump(mode="json")
    invalid["download"]["url"] = "https://other.test/file"
    ranges.clear()
    with pytest.raises(MediaError, match="private delivery"):
        materialize_h3_lora(H3LoraGrant.model_validate(invalid), settings)
    assert not ranges
    invalid["download"]["expires_at"] = "2026-09-25T12:00:00"
    with pytest.raises(ValidationError, match="timezone"):
        H3LoraGrant.model_validate(invalid)


def test_native_loader_rejects_zero_matched_weights(monkeypatch):
    native = ModuleType("nodes")

    class Loader:
        def load_lora_model_only(self, model, _name, strength):
            patches = {key: list(value) for key, value in model.patches.items()}
            if self.compatible and strength:
                patches.setdefault("layer", []).append((strength,))
            return (SimpleNamespace(patches=patches),)

    native.LoraLoaderModelOnly = Loader
    monkeypatch.setitem(sys.modules, "nodes", native)
    spec = importlib.util.spec_from_file_location(
        "isolated_h3_loader", ROOT / "src/gen_automation/i2v_worker/comfy_h3_node.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    loader = module.ManagedH3LoraLoader()
    name = f"managed-h3/{'a' * 64}.safetensors"
    source = SimpleNamespace(patches={})
    loader.compatible = False
    with pytest.raises(ValueError, match="no compatible"):
        loader.load_lora_model_only(source, name, 0.65)
    loader.compatible = True
    assert loader.load_lora_model_only(source, name, 0.65)[0].patches["layer"] == [(0.65,)]
    assert source.patches == {}
    with pytest.raises(ValueError, match="outside"):
        loader.load_lora_model_only(source, "../other.safetensors", 1)


@pytest.mark.asyncio
async def test_incompatible_file_is_an_actionable_worker_error(tmp_path):
    from gen_automation.i2v_worker.comfy import ComfyLoraError

    def handle(request):
        if request.url.path == "/prompt":
            return httpx2.Response(200, json={"prompt_id": "test-h3"})
        return httpx2.Response(
            200,
            json={
                "test-h3": {
                    "status": {
                        "status_str": "error",
                        "messages": [
                            [
                                "execution_error",
                                {
                                    "node_type": "ManagedH3LoraLoader",
                                    "exception_message": "private details",
                                },
                            ]
                        ],
                    }
                }
            },
        )

    client = await _client(handle)
    try:
        with pytest.raises(ComfyLoraError, match="selected H3 LoRA could not be applied"):
            await client.execute({}, tmp_path)
    finally:
        await client.close()


def test_frontend_roundtrips_selection_strengths_and_clear_without_wan_fields():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is needed for frontend behavior checks")
    script = r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync("src/gen_automation/static/i2v.js", "utf8");
function section(name, next) {
  return source.slice(source.indexOf(`  function ${name}(`), source.indexOf(`  function ${next}(`));
}
const state = {
  loraSelections: new Map(), loraCatalogLoaded: true, loraProfileEnabled: true,
  loraCatalog: [{catalog_id: "owned-id", sha256: "a".repeat(64), available: true}],
};
const context = vm.createContext({
  isH3: true, state, workerSettingDefaults: {profile: "minimax_h3", h3_loras: []},
  loraList: {querySelectorAll: () => []}, root: {querySelector: () => null},
  advanced: {querySelectorAll: () => [], querySelector: () => null},
  CSS: {escape: x => x}, renderLoraCatalog() {}, syncLoraPromptPreview() {},
  syncAspectControls() {}, updateDuration() {}, announce() {},
  loraBlockMessage: () => "unavailable",
});
vm.runInContext(section("collectSettings", "sourceNativeDimensions") +
                section("setLoraSelections", "renderLoraCatalog") +
                section("unavailableLoraSelections", "loraBlockMessage"), context);
context.saved = {h3_loras: [{artifact_id: "owned-id", sha256: "a".repeat(64), strength: 0.65}]};
vm.runInContext("applySettings(saved)", context);
let collected = JSON.parse(JSON.stringify(vm.runInContext("collectSettings()", context)));
assert.deepEqual(collected.h3_loras, context.saved.h3_loras);
assert.deepEqual(collected.loras, []);
state.loraSelections.set("owned-id", 0);
assert.equal(vm.runInContext("collectSettings().h3_loras[0].strength", context), 0);
state.loraSelections.set("missing-id", 1);
assert.throws(() => vm.runInContext("collectSettings()", context), /unavailable/);
vm.runInContext("applySettings({})", context);
assert.equal(vm.runInContext("collectSettings().h3_loras.length", context), 0);
"""
    subprocess.run(  # noqa: S603 - fixed local test script and discovered Node executable
        [node, "-e", script], cwd=ROOT, check=True, capture_output=True, text=True
    )
