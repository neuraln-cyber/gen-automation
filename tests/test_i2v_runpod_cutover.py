from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any, Self

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "infra/aws-staging/deploy/cutover-i2v-runpod.sh"
ENV_EXAMPLE = ROOT / "infra/aws-staging/deploy/control-plane.env.example"
ABSENT = object()
CANONICAL_GPU_NAMES = (
    "NVIDIA GeForce RTX 5090",
    "NVIDIA A40",
    "NVIDIA RTX A6000",
    "NVIDIA L40S",
    "NVIDIA RTX PRO 4500 Blackwell",
)


def _script() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def test_cutover_shell_and_embedded_python_are_syntactically_valid() -> None:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is unavailable")
    subprocess.run([bash, "-n", str(SCRIPT)], check=True)  # noqa: S603
    programs = re.findall(r"<<'PY'\n(.*?)\nPY", _script(), flags=re.DOTALL)
    assert len(programs) == 7
    for index, program in enumerate(programs):
        compile(program, f"cutover-i2v-runpod-{index}", "exec")


def test_cutover_freezes_and_proves_zero_work_before_enabling_runpod() -> None:
    script = _script()

    freeze = script.index("rewrite_env freeze")
    zero_work = script.index("assert_zero_i2v_work ||", freeze)
    secret = script.index("aws ssm get-parameter")
    preseed = script.index('PRESEED_IDENTITY="$(verify_preseed_identity)"', secret)
    provider_health = script.index("\nverify_runpod_provider\n", secret)
    enable = script.index("rewrite_env enable")
    assert secret < preseed < provider_health < freeze < zero_work < enable
    assert "I2VJobState.QUEUED" in script
    assert "I2VAttemptState.CREATED" in script
    assert "--interactive" in script
    assert "--cap-drop ALL" in script
    assert "--cap-add DAC_READ_SEARCH" in script
    assert "--security-opt no-new-privileges:true" in script
    assert script.count("--user 0:0") == 1
    assert script.count("--user 10001:10001") == 1
    assert '"GEN_AUTOMATION_I2V_RUNPOD_ENDPOINT_ID",' in script
    assert '"GEN_AUTOMATION_I2V_RUNPOD_NETWORK_VOLUME_ID",' in script
    assert "salad.com" not in script
    assert script.count("https://api.runpod.ai/v2/") == 1
    assert '+ "/health"' in script
    assert script.count("https://rest.runpod.io/v1/networkvolumes") == 1
    assert script.count("https://rest.runpod.io/v1/pods?computeType=GPU") == 2
    assert script.count("https://api.runpod.io/graphql") == 1
    assert '"https://rest.runpod.io/v1/endpoints/"' in script
    assert '"https://rest.runpod.io/v1/templates/"' in script
    assert 'endpoint.get("flashboot") is not True' in script
    assert 'endpoint["computeType"] != "GPU"' in script
    assert 'endpoint.get("gpuCount") != 1' in script
    assert 'endpoint.get("idleTimeout") != 60' in script
    assert 'endpoint.get("executionTimeoutMs") != 21600000' in script
    assert 'endpoint.get("scalerType") != "QUEUE_DELAY"' in script
    assert 'endpoint.get("scalerValue") != 1' in script
    assert 'allowed_cuda_versions != ["12.8", "12.9", "13.0"]' in script
    assert 'endpoint.get("minCudaVersion") != "12.8"' in script
    assert 'if "dataCenterIds" in endpoint:' in script
    assert 'endpoint_data_centers = endpoint["dataCenterIds"]' in script
    assert 'endpoint_data_centers.split(",")' in script
    assert "endpoint_data_centers != [data_center_id]" in script
    assert 'template.get("imageName") != worker_image' in script
    assert 'environment.get("GEN_I2V_WORKER_REQUIRE_PRESEEDED_VOLUME") != "true"' in script
    assert 'environment.get("GEN_I2V_WORKER_MODEL_OBJECTS_JSON")' in script
    assert "GEN_I2V_WORKER_PRIVATE_MANIFEST_SOURCE_SHA256" in script
    assert "len(model_objects) != 14" in script
    assert "!= preseed_model_objects_sha256" in script
    assert 'volume.get("dataCenterId")' in script
    assert "data_center_id) is None" in script
    assert '"EU-RO-1"' not in script
    assert 'preseed_state="/var/lib/gen-automation/runpod-i2v/preseed-state.json"' in script
    assert 'state.get("status") != "ready"' in script
    assert 'pod["name"].startswith("gen-automation-i2v-")' in script
    assert script.count("--env GEN_AUTOMATION_I2V_ENABLED=true") == 1
    assert script.count("--env GEN_AUTOMATION_I2V_LORA_WORKER_ENABLED=true") == 1


@pytest.mark.parametrize(
    (
        "endpoint_data_centers",
        "endpoint_flashboot",
        "scheduler_locations",
        "gpu_names",
        "accepted",
    ),
    [
        (ABSENT, True, "US-IL-1", CANONICAL_GPU_NAMES, True),
        (ABSENT, None, "US-IL-1", CANONICAL_GPU_NAMES, False),
        (ABSENT, None, "EU-RO-1", CANONICAL_GPU_NAMES, False),
        (None, None, "US-IL-1", CANONICAL_GPU_NAMES, False),
        ("US-IL-1", True, "US-IL-1", CANONICAL_GPU_NAMES, True),
        (["US-IL-1"], True, "US-IL-1", CANONICAL_GPU_NAMES, True),
        ("US-IL-1", False, "US-IL-1", CANONICAL_GPU_NAMES, False),
        ("US-IL-1,", True, "US-IL-1", CANONICAL_GPU_NAMES, False),
        (",US-IL-1", True, "US-IL-1", CANONICAL_GPU_NAMES, False),
        ("US-IL-1,EU-RO-1", True, "US-IL-1", CANONICAL_GPU_NAMES, False),
        (ABSENT, True, "US-IL-1", CANONICAL_GPU_NAMES[:-1], False),
        (
            ABSENT,
            True,
            "US-IL-1",
            (*CANONICAL_GPU_NAMES, "NVIDIA H100 80GB HBM3"),
            False,
        ),
        (ABSENT, True, "US-IL-1", tuple(reversed(CANONICAL_GPU_NAMES)), False),
    ],
)
def test_provider_verifier_accepts_documented_datacenter_serializations(
    monkeypatch: pytest.MonkeyPatch,
    endpoint_data_centers: object,
    endpoint_flashboot: bool | None,
    scheduler_locations: str,
    gpu_names: tuple[str, ...],
    accepted: bool,
) -> None:
    program = next(
        item
        for item in re.findall(r"<<'PY'\n(.*?)\nPY", _script(), flags=re.DOTALL)
        if "https://rest.runpod.io/v1/networkvolumes" in item
    )
    endpoint_id = "endpoint-1"
    volume_id = "volume-1"
    template_id = "template-1"
    image = "ghcr.io/example/i2v-worker@sha256:" + "a" * 64
    revision = "b" * 40
    model_objects = json.dumps(
        [{"role": f"role-{index}"} for index in range(14)],
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    model_objects_sha256 = hashlib.sha256(model_objects.encode()).hexdigest()
    manifest_source_sha256 = "c" * 64
    endpoint: dict[str, object] = {
        "id": endpoint_id,
        "name": "gen-automation-i2v-staging",
        "networkVolumeId": volume_id,
        "networkVolumeIds": [volume_id],
        "workersMin": 0,
        "workersMax": 1,
        "computeType": "GPU",
        "gpuCount": 1,
        "allowedCudaVersions": ["12.8", "12.9", "13.0"],
        "minCudaVersion": "12.8",
        "idleTimeout": 60,
        "executionTimeoutMs": 21600000,
        "scalerType": "QUEUE_DELAY",
        "scalerValue": 1,
        "templateId": template_id,
        "gpuTypeIds": list(gpu_names),
    }
    if endpoint_data_centers is not ABSENT:
        endpoint["dataCenterIds"] = endpoint_data_centers
    if endpoint_flashboot is not None:
        endpoint["flashboot"] = endpoint_flashboot
    payloads = {
        "https://rest.runpod.io/v1/networkvolumes": [
            {"id": volume_id, "dataCenterId": "US-IL-1", "size": 100}
        ],
        "https://rest.runpod.io/v1/pods?computeType=GPU": [],
        f"https://rest.runpod.io/v1/endpoints/{endpoint_id}": endpoint,
        f"https://rest.runpod.io/v1/templates/{template_id}": {
            "id": template_id,
            "imageName": image,
            "env": {
                "GEN_I2V_WORKER_SOURCE_REVISION": revision,
                "GEN_I2V_WORKER_MODEL_OBJECTS_JSON": model_objects,
                "GEN_I2V_WORKER_PRIVATE_MANIFEST_SOURCE_SHA256": (manifest_source_sha256),
                "GEN_I2V_WORKER_VOLUME_ROOT": "/runpod-volume",
                "GEN_I2V_WORKER_REQUIRE_PRESEEDED_VOLUME": "true",
                "GEN_I2V_WORKER_LORA_WORKER_ENABLED": "true",
                "GEN_I2V_WORKER_ALLOWED_GPU_NAMES_CSV": ",".join(gpu_names),
            },
        },
        "https://api.runpod.io/graphql": {
            "data": {
                "myself": {
                    "endpoints": [
                        {
                            "id": endpoint_id,
                            "locations": scheduler_locations,
                            "networkVolumeId": volume_id,
                            "templateId": template_id,
                            "workersMin": 0,
                            "workersMax": 1,
                            "pods": [],
                        }
                    ]
                }
            }
        },
    }

    class _Response:
        def __init__(self, payload: object) -> None:
            self._body = json.dumps(payload).encode()

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def read(self, _: int) -> bytes:
            return self._body

    def fake_urlopen(request: Any, *, timeout: int) -> _Response:
        assert timeout == 30
        if request.full_url == "https://api.runpod.io/graphql":
            assert request.method == "POST"
            assert request.headers["Authorization"] == "Bearer test-key"
        return _Response(payloads[request.full_url])

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    environment = {
        "CUTOVER_RUNPOD_MODE": "serverless",
        "CUTOVER_ENDPOINT_ID": endpoint_id,
        "CUTOVER_NETWORK_VOLUME_ID": volume_id,
        "CUTOVER_WORKER_IMAGE": image,
        "CUTOVER_WORKER_SOURCE_REVISION": revision,
        "CUTOVER_RUNPOD_KEY": "test-key",
        "CUTOVER_PRESEED_VOLUME_ID": volume_id,
        "CUTOVER_PRESEED_MODEL_OBJECTS_SHA256": model_objects_sha256,
        "CUTOVER_PRESEED_MANIFEST_SOURCE_SHA256": manifest_source_sha256,
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    compiled = compile(program, "cutover-i2v-runpod-provider", "exec")
    if accepted:
        exec(compiled, {})  # noqa: S102
    else:
        with pytest.raises(SystemExit, match="RunPod provider verification failed"):
            exec(compiled, {})  # noqa: S102


@pytest.mark.parametrize(
    ("jobs", "workers", "accepted"),
    [
        (
            {"inQueue": 0, "inProgress": 0},
            {"idle": 1, "ready": 1, "initializing": 0, "running": 0},
            True,
        ),
        (
            {"inQueue": 1, "inProgress": 0},
            {"initializing": 0, "running": 0},
            False,
        ),
        (
            {"inQueue": 0, "inProgress": 1},
            {"initializing": 0, "running": 0},
            False,
        ),
        (
            {"inQueue": 0, "inProgress": 0},
            {"initializing": 1, "running": 0},
            False,
        ),
        (
            {"inQueue": 0, "inProgress": 0},
            {"initializing": 0, "running": 1},
            False,
        ),
        (
            {"inQueue": False, "inProgress": 0},
            {"initializing": 0, "running": 0},
            False,
        ),
    ],
)
def test_manual_rollback_serverless_health_must_have_zero_work(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    jobs: dict[str, int | bool],
    workers: dict[str, int],
    accepted: bool,
) -> None:
    program = next(
        item
        for item in re.findall(r"<<'PY'\n(.*?)\nPY", _script(), flags=re.DOTALL)
        if "RunPod rollback work verification failed" in item
    )
    marker = tmp_path / "state.json"
    marker.write_text(
        json.dumps(
            {
                "schema": "gen-automation/i2v-runpod-cutover/v1",
                "runpod_mode": "serverless",
                "endpoint_id": "endpoint-1",
                "network_volume_id": "volume-1",
            }
        ),
        encoding="utf-8",
    )

    class _Response:
        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def read(self, _: int) -> bytes:
            return json.dumps({"jobs": jobs, "workers": workers}).encode()

    def fake_urlopen(request: Any, *, timeout: int) -> _Response:
        assert timeout == 30
        assert request.method == "GET"
        assert request.full_url == "https://api.runpod.ai/v2/endpoint-1/health"
        assert request.headers["Authorization"] == "Bearer test-key"
        return _Response()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("CUTOVER_RUNPOD_KEY", "test-key")
    monkeypatch.setattr(sys, "argv", ["cutover-i2v-runpod", str(marker)])
    compiled = compile(program, "cutover-i2v-runpod-zero-serverless", "exec")
    if accepted:
        exec(compiled, {})  # noqa: S102
    else:
        with pytest.raises(SystemExit, match="RunPod rollback work verification failed"):
            exec(compiled, {})  # noqa: S102


@pytest.mark.parametrize(
    ("pods", "accepted"),
    [
        ([], True),
        ([{"name": "unrelated", "networkVolumeId": "volume-1"}], True),
        (
            [
                {
                    "name": "gen-automation-i2v-other-volume",
                    "networkVolumeId": "volume-2",
                }
            ],
            True,
        ),
        (
            [
                {
                    "name": "gen-automation-i2v-active",
                    "networkVolume": {"id": "volume-1"},
                }
            ],
            False,
        ),
    ],
)
def test_manual_rollback_pod_mode_rejects_exact_managed_pod(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    pods: list[dict[str, object]],
    accepted: bool,
) -> None:
    program = next(
        item
        for item in re.findall(r"<<'PY'\n(.*?)\nPY", _script(), flags=re.DOTALL)
        if "RunPod rollback work verification failed" in item
    )
    marker = tmp_path / "state.json"
    marker.write_text(
        json.dumps(
            {
                "schema": "gen-automation/i2v-runpod-cutover/v1",
                "runpod_mode": "pod",
                "endpoint_id": None,
                "network_volume_id": "volume-1",
            }
        ),
        encoding="utf-8",
    )

    class _Response:
        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def read(self, _: int) -> bytes:
            return json.dumps(pods).encode()

    def fake_urlopen(request: Any, *, timeout: int) -> _Response:
        assert timeout == 30
        assert request.method == "GET"
        assert request.full_url == "https://rest.runpod.io/v1/pods?computeType=GPU"
        return _Response()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("CUTOVER_RUNPOD_KEY", "test-key")
    monkeypatch.setattr(sys, "argv", ["cutover-i2v-runpod", str(marker)])
    compiled = compile(program, "cutover-i2v-runpod-zero-pod", "exec")
    if accepted:
        exec(compiled, {})  # noqa: S102
    else:
        with pytest.raises(SystemExit, match="RunPod rollback work verification failed"):
            exec(compiled, {})  # noqa: S102


def test_cutover_freeze_allows_both_provider_coordinates_to_be_empty(tmp_path: Path) -> None:
    program = re.findall(r"<<'PY'\n(.*?)\nPY", _script(), flags=re.DOTALL)[0]
    source = tmp_path / "source.env"
    target = tmp_path / "target.env"
    source.write_text(
        "GEN_AUTOMATION_PUBLIC_BASE_URL=https://staging.example\n",
        encoding="utf-8",
    )
    environment = {
        **os.environ,
        "CUTOVER_MODE": "freeze",
        "CUTOVER_RUNPOD_MODE": "",
        "CUTOVER_ENDPOINT_ID": "",
        "CUTOVER_NETWORK_VOLUME_ID": "",
        "CUTOVER_WORKER_IMAGE": "",
        "CUTOVER_WORKER_SOURCE_REVISION": "",
        "CUTOVER_RUNPOD_KEY": "",
    }

    subprocess.run(  # noqa: S603 - executes only the repository's embedded Python.
        [sys.executable, "-c", program, str(source), str(target)],
        check=True,
        env=environment,
    )

    frozen = target.read_text(encoding="utf-8")
    assert "GEN_AUTOMATION_I2V_RUNPOD_ENDPOINT_ID=\n" in frozen
    assert "GEN_AUTOMATION_I2V_RUNPOD_NETWORK_VOLUME_ID=\n" in frozen


def test_cutover_enable_pins_exact_worker_image_and_source(tmp_path: Path) -> None:
    program = re.findall(r"<<'PY'\n(.*?)\nPY", _script(), flags=re.DOTALL)[0]
    source = tmp_path / "source.env"
    target = tmp_path / "target.env"
    source.write_text(
        "GEN_AUTOMATION_PUBLIC_BASE_URL=https://staging.example\n",
        encoding="utf-8",
    )
    image = "ghcr.io/neuraln-cyber/gen-automation/i2v-worker@sha256:" + "a" * 64
    revision = "b" * 40
    environment = {
        **os.environ,
        "CUTOVER_MODE": "enable",
        "CUTOVER_RUNPOD_MODE": "pod",
        "CUTOVER_ENDPOINT_ID": "",
        "CUTOVER_NETWORK_VOLUME_ID": "volume123",
        "CUTOVER_WORKER_IMAGE": image,
        "CUTOVER_WORKER_SOURCE_REVISION": revision,
        "CUTOVER_RUNPOD_KEY": "not-a-real-runpod-api-key",
    }

    subprocess.run(  # noqa: S603 - executes only the repository's embedded Python.
        [sys.executable, "-c", program, str(source), str(target)],
        check=True,
        env=environment,
    )

    enabled = target.read_text(encoding="utf-8")
    assert f"GEN_AUTOMATION_I2V_WORKER_IMAGE={image}\n" in enabled
    assert f"GEN_AUTOMATION_I2V_WORKER_SOURCE_REVISION={revision}\n" in enabled


def test_cutover_serverless_enable_pins_endpoint_and_volume(tmp_path: Path) -> None:
    program = re.findall(r"<<'PY'\n(.*?)\nPY", _script(), flags=re.DOTALL)[0]
    source = tmp_path / "source.env"
    target = tmp_path / "target.env"
    source.write_text(
        "GEN_AUTOMATION_PUBLIC_BASE_URL=https://staging.example\n",
        encoding="utf-8",
    )
    image = "ghcr.io/neuraln-cyber/gen-automation/i2v-worker@sha256:" + "a" * 64
    revision = "b" * 40
    environment = {
        **os.environ,
        "CUTOVER_MODE": "enable",
        "CUTOVER_RUNPOD_MODE": "serverless",
        "CUTOVER_ENDPOINT_ID": "endpoint123",
        "CUTOVER_NETWORK_VOLUME_ID": "volume123",
        "CUTOVER_WORKER_IMAGE": image,
        "CUTOVER_WORKER_SOURCE_REVISION": revision,
        "CUTOVER_RUNPOD_KEY": "not-a-real-runpod-api-key",
    }

    subprocess.run(  # noqa: S603 - executes only the repository's embedded Python.
        [sys.executable, "-c", program, str(source), str(target)],
        check=True,
        env=environment,
    )

    enabled = target.read_text(encoding="utf-8")
    assert "GEN_AUTOMATION_I2V_RUNPOD_MODE=serverless\n" in enabled
    assert "GEN_AUTOMATION_I2V_RUNPOD_ENDPOINT_ID=endpoint123\n" in enabled
    assert "GEN_AUTOMATION_I2V_RUNPOD_NETWORK_VOLUME_ID=volume123\n" in enabled
    assert "GEN_AUTOMATION_I2V_RUNPOD_QUEUE_TIMEOUT_SECONDS=21600\n" in enabled


def test_cutover_reads_one_fixed_secret_and_never_prints_it() -> None:
    script = _script()

    assert 'ssm_parameter="/gen-automation-staging/runpod/inference-api-key"' in script
    assert script.count("aws ssm get-parameter") == 1
    assert "set -x" not in script
    assert "CUTOVER_RUNPOD_KEY" in script
    assert "printf '%s' \"$CUTOVER_RUNPOD_KEY\"" not in script
    assert 'echo "$CUTOVER_RUNPOD_KEY"' not in script


def test_cutover_has_exact_automatic_and_manual_rollback_contract() -> None:
    script = _script()

    assert 'lock_file="/run/lock/gen-automation-control-plane-update.lock"' in script
    flock = script.index("flock --exclusive --wait 120")
    rollback = script.index('if [ "$operation" = "rollback" ]; then')
    marker_check = script.index('python3 - "$marker" "$env_file"', rollback)
    secret = script.index("  load_runpod_key", marker_check)
    arm = script.index("  arm_manual_rollback_recovery", secret)
    stop = script.index('  systemctl stop "$service_name"', arm)
    inactive = script.index("ActiveState", stop)
    database_zero = script.index("  assert_zero_i2v_work ||", inactive)
    provider_zero = script.index("  assert_zero_runpod_work ||", database_zero)
    restore = script.index("\n  restore_original\n", provider_zero)
    disarm = script.index("  rollback_recovery_armed=0", restore)
    assert flock < rollback < marker_check < secret < arm < stop
    assert stop < inactive < database_zero < provider_zero < restore < disarm
    assert 'systemctl show --property=ActiveState --value "$service_name"' in script
    assert "Cutover failed; restoring the prior provider configuration." in script
    assert "if restore_original; then" in script
    assert "Manual rollback failed; restoring the active RunPod configuration." in script
    assert "if recover_manual_rollback_service; then" in script
    recovery = script.index("recover_manual_rollback_service()")
    recovery_restore = script.index(
        'replace_env_atomically "$rollback_recovery_env" recovery', recovery
    )
    recovery_start = script.index('systemctl restart --no-block "$service_name"', recovery_restore)
    recovery_ready = script.index("wait_for_control_plane || recovery_status=1", recovery_start)
    assert recovery < recovery_restore < recovery_start < recovery_ready
    assert script.count('rmdir -- "$active_root"') == 2
    assert '"cutover_env_sha256"' in script
    assert "no active RunPod cutover exists" in script
    assert "rm -rf" not in script


def test_staging_environment_declares_provider_cutover_fields_once() -> None:
    environment = ENV_EXAMPLE.read_text(encoding="utf-8")
    for key in (
        "GEN_AUTOMATION_I2V_RUNPOD_ENABLED",
        "GEN_AUTOMATION_I2V_RUNPOD_MODE",
        "GEN_AUTOMATION_I2V_RUNPOD_ENDPOINT_ID",
        "GEN_AUTOMATION_I2V_RUNPOD_NETWORK_VOLUME_ID",
        "GEN_AUTOMATION_I2V_RUNPOD_API_KEY",
        "GEN_AUTOMATION_I2V_RUNPOD_CLAIM_URL",
        "GEN_AUTOMATION_I2V_RUNPOD_SUBMISSION_CLAIM_TIMEOUT_SECONDS",
        "GEN_AUTOMATION_I2V_RUNPOD_QUEUE_TIMEOUT_SECONDS",
        "GEN_AUTOMATION_I2V_RUNPOD_TERMINAL_GRACE_SECONDS",
    ):
        assert environment.count(f"{key}=") == 1
