from __future__ import annotations

import re
import shutil
import subprocess
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
    assert len(programs) == 5
    for index, program in enumerate(programs):
        compile(program, f"cutover-i2v-runpod-{index}", "exec")


def test_cutover_freezes_and_proves_zero_work_before_enabling_runpod() -> None:
    script = _script()

    freeze = script.index("rewrite_env freeze")
    zero_work = script.index("assert_zero_i2v_work ||")
    secret = script.index("aws ssm get-parameter")
    preseed = script.index('PRESEED_VOLUME_ID="$(verify_preseed_identity)"', secret)
    endpoint_health = script.index("\nverify_runpod_endpoint\n", secret)
    enable = script.index("rewrite_env enable")
    assert secret < preseed < endpoint_health < freeze < zero_work < enable
    assert "I2VJobState.QUEUED" in script
    assert "I2VAttemptState.CREATED" in script
    assert "--interactive" in script
    assert "--cap-drop ALL" in script
    assert "--cap-add DAC_READ_SEARCH" in script
    assert "--security-opt no-new-privileges:true" in script
    assert "salad.com" not in script
    assert script.count("https://api.runpod.ai/v2/") == 1
    assert script.count("https://rest.runpod.io/v1/endpoints/") == 1
    assert 'preseed_state="/var/lib/gen-automation/runpod-i2v/preseed-state.json"' in script
    assert 'state.get("status") != "ready"' in script
    assert 'endpoint.get("workersMin") != 1' in script


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
    assert '"cutover_env_sha256"' in script
    assert "no active RunPod cutover exists" in script
    assert "rm -rf" not in script


def test_staging_environment_declares_provider_cutover_fields_once() -> None:
    environment = ENV_EXAMPLE.read_text(encoding="utf-8")
    for key in (
        "GEN_AUTOMATION_I2V_RUNPOD_ENABLED",
        "GEN_AUTOMATION_I2V_RUNPOD_ENDPOINT_ID",
        "GEN_AUTOMATION_I2V_RUNPOD_API_KEY",
        "GEN_AUTOMATION_I2V_RUNPOD_CLAIM_URL",
        "GEN_AUTOMATION_I2V_RUNPOD_SUBMISSION_CLAIM_TIMEOUT_SECONDS",
        "GEN_AUTOMATION_I2V_RUNPOD_QUEUE_TIMEOUT_SECONDS",
        "GEN_AUTOMATION_I2V_RUNPOD_TERMINAL_GRACE_SECONDS",
    ):
        assert environment.count(f"{key}=") == 1
