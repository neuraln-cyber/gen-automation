from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "infra/aws-staging/deploy/cutover-i2v-runpod.sh"
ENV_EXAMPLE = ROOT / "infra/aws-staging/deploy/control-plane.env.example"


def _script() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def test_cutover_shell_and_embedded_python_are_syntactically_valid() -> None:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is unavailable")
    subprocess.run([bash, "-n", str(SCRIPT)], check=True)  # noqa: S603
    programs = re.findall(r"<<'PY'\n(.*?)\nPY", _script(), flags=re.DOTALL)
    assert len(programs) == 6
    for index, program in enumerate(programs):
        compile(program, f"cutover-i2v-runpod-{index}", "exec")


def test_cutover_freezes_and_proves_zero_work_before_enabling_runpod() -> None:
    script = _script()

    freeze = script.index("rewrite_env freeze")
    zero_work = script.index("assert_zero_i2v_work ||")
    secret = script.index("aws ssm get-parameter")
    preseed = script.index('PRESEED_VOLUME_ID="$(verify_preseed_identity)"', secret)
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
    assert "https://api.runpod.ai/v2/" not in script
    assert script.count("https://rest.runpod.io/v1/networkvolumes") == 1
    assert script.count("https://rest.runpod.io/v1/pods?computeType=GPU") == 1
    assert '"https://rest.runpod.io/v1/endpoints/"' in script
    assert '"https://rest.runpod.io/v1/templates/"' in script
    assert 'endpoint.get("flashboot") is not True' in script
    assert 'endpoint.get("dataCenterIds") != [data_center_id]' in script
    assert 'template.get("imageName") != worker_image' in script
    assert 'environment.get("GEN_I2V_WORKER_REQUIRE_PRESEEDED_VOLUME") != "true"' in script
    assert 'volume.get("dataCenterId")' in script
    assert "data_center_id) is None" in script
    assert '"EU-RO-1"' not in script
    assert 'preseed_state="/var/lib/gen-automation/runpod-i2v/preseed-state.json"' in script
    assert 'state.get("status") != "ready"' in script
    assert 'pod["name"].startswith("gen-automation-i2v-")' in script
    assert script.count("--env GEN_AUTOMATION_I2V_ENABLED=true") == 1
    assert script.count("--env GEN_AUTOMATION_I2V_LORA_WORKER_ENABLED=true") == 1


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
    assert "flock --exclusive --wait 120" in script
    assert "Cutover failed; restoring the prior provider configuration." in script
    assert "if restore_original; then" in script
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
