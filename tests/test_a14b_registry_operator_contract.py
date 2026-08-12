from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "provision-a14b-private-staging.ps1"
IAM = ROOT / "infra" / "aws-staging" / "iam.tf"
RUNBOOK = ROOT / "docs" / "a14b-private-registry-authorization.md"


def test_thin_powershell_wrapper_accepts_only_public_identifiers() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert "a14b_registry_operator_cli" in source
    assert "--image $Image" in source
    assert "--deployment-id $DeploymentId" in source
    assert "gh auth token" not in source
    assert "SecureString" not in source
    assert "PutParameter" not in source
    assert "$env:AWS_SECRET_ACCESS_KEY" not in source
    assert "$env:AWS_SESSION_TOKEN" not in source
    assert "[IO.File]::Write" not in source
    assert "RemainingArguments.Count -ne 0" in source


def test_wrapper_forwards_only_the_reviewed_state_instance() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    parameter_block = source.split("param(", 1)[1].split(")\n\nSet-StrictMode", 1)[0]
    assert re.search(r"\$InstanceId\b", parameter_block, re.IGNORECASE) is None
    assert "Read-Host" not in source
    assert "output -raw control_plane_instance_id" in source
    assert "workspace show" in source
    assert '$workspaceName -ne "default"' in source
    assert '"-chdir=$infraRoot"' in source
    assert '$awsProfile = "gen-automation-staging"' in source
    assert '$awsRegion = "eu-central-1"' in source
    assert '$instanceId = ($tofuOutput -join "").Trim()' in source
    assert "$instanceId -notmatch $instanceIdPattern" in source
    assert source.count("--instance-id $instanceId") == 1
    assert source.index("output -raw control_plane_instance_id") < source.index(
        "--instance-id $instanceId"
    )


def test_wrapper_scrubs_ambient_state_log_secret_and_endpoint_steering() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    for name in (
        "TF_WORKSPACE",
        "TF_DATA_DIR",
        "TF_CLI_CONFIG_FILE",
        "TF_CLI_ARGS",
        "TF_CLI_ARGS_output",
        "TF_CLI_ARGS_workspace",
        "TF_LOG",
        "TF_LOG_PATH",
        "TF_LOG_CORE",
        "TF_LOG_PROVIDER",
        "TOFU_WORKSPACE",
        "TOFU_DATA_DIR",
        "TOFU_CLI_CONFIG_FILE",
        "TOFU_CLI_ARGS",
        "TOFU_CLI_ARGS_output",
        "TOFU_CLI_ARGS_workspace",
        "TOFU_LOG",
        "TOFU_LOG_PATH",
        "TOFU_LOG_CORE",
        "TOFU_LOG_PROVIDER",
        "AWS_ENDPOINT_URL_S3",
        "AWS_S3_ENDPOINT",
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "GH_ENTERPRISE_TOKEN",
    ):
        assert source.count(f'"{name}"') == 1
    assert "Get-ChildItem Env:" not in source
    assert source.count('"HTTP_PROXY"') == 1
    assert source.count('"HTTPS_PROXY"') == 1
    assert source.count('"ALL_PROXY"') == 1
    assert '"http_proxy"' not in source
    assert '"https_proxy"' not in source
    assert '"all_proxy"' not in source


def test_wrapper_scrubs_tofu_environment_and_restores_case_insensitive_proxy(
    tmp_path: Path,
) -> None:
    if os.name != "nt":
        pytest.skip("the executable wrapper contract is Windows-only")
    executable = shutil.which("powershell") or shutil.which("pwsh")
    if executable is None:
        pytest.skip("PowerShell is not installed")
    capture = tmp_path / "tofu-capture.txt"
    tf_log = tmp_path / "terraform-sensitive.log"
    tofu_log = tmp_path / "tofu-sensitive.log"
    (tmp_path / "gen-automation-staging-aws-config").write_text(
        "[profile gen-automation-staging]\nregion=eu-central-1\n",
        encoding="utf-8",
    )
    fake_tofu = tmp_path / "tofu.cmd"
    checks = "\n".join(
        f'if defined {name} (echo leak-{name}>>"%TEST_CAPTURE%" & exit /b 91)'
        for name in (
            "TF_WORKSPACE",
            "TF_DATA_DIR",
            "TF_CLI_ARGS",
            "TF_CLI_ARGS_output",
            "TF_LOG",
            "TF_LOG_PATH",
            "TOFU_WORKSPACE",
            "TOFU_DATA_DIR",
            "TOFU_CLI_ARGS",
            "TOFU_CLI_ARGS_output",
            "TOFU_LOG",
            "TOFU_LOG_PATH",
            "TOFU_LOG_CORE",
            "AWS_ENDPOINT_URL_S3",
            "AWS_S3_ENDPOINT",
            "GH_TOKEN",
            "GITHUB_TOKEN",
            "GH_ENTERPRISE_TOKEN",
            "HTTP_PROXY",
        )
    )
    fake_tofu.write_text(
        "@echo off\n"
        "setlocal\n"
        f"{checks}\n"
        'if not "%AWS_PROFILE%"=="gen-automation-staging" '
        '(echo leak-AWS_PROFILE>>"%TEST_CAPTURE%" & exit /b 91)\n'
        'if not "%AWS_REGION%"=="eu-central-1" '
        '(echo leak-AWS_REGION>>"%TEST_CAPTURE%" & exit /b 91)\n'
        'if not "%TF_INPUT%"=="0" (echo leak-TF_INPUT>>"%TEST_CAPTURE%" & exit /b 91)\n'
        'echo(%*|"%SystemRoot%\\System32\\findstr.exe" '
        '/L /E /C:" workspace show" >nul\n'
        "if not errorlevel 1 "
        '(echo clean-workspace>>"%TEST_CAPTURE%"&echo default&exit /b 0)\n'
        'echo(%*|"%SystemRoot%\\System32\\findstr.exe" '
        '/L /E /C:" output -raw control_plane_instance_id" >nul\n'
        "if not errorlevel 1 "
        '(echo clean-output>>"%TEST_CAPTURE%"&echo invalid-instance&exit /b 0)\n'
        'echo unexpected-call>>"%TEST_CAPTURE%"\n'
        "exit /b 92\n",
        encoding="ascii",
    )
    # Windows treats environment names case-insensitively, but this test process can
    # inherit duplicate case variants.  De-duplicate them before PowerShell launches
    # the fake .cmd application, matching the environment block a normal shell has.
    environment = {name.upper(): value for name, value in os.environ.items()}
    environment.update(
        {
            name.upper(): value
            for name, value in {
                "PATH": f"{tmp_path}{os.pathsep}{environment['PATH']}",
                "TEMP": str(tmp_path),
                "TMP": str(tmp_path),
                "TEST_CAPTURE": str(capture),
                "TF_WORKSPACE": "hostile-workspace",
                "TF_DATA_DIR": str(tmp_path / "hostile-data"),
                "TF_CLI_ARGS_output": "-state=hostile.tfstate",
                "TF_LOG": "TRACE",
                "TF_LOG_PATH": str(tf_log),
                "TOFU_WORKSPACE": "hostile-tofu-workspace",
                "TOFU_DATA_DIR": str(tmp_path / "hostile-tofu-data"),
                "TOFU_CLI_ARGS_output": "-state=hostile-tofu.tfstate",
                "TOFU_LOG": "TRACE",
                "TOFU_LOG_PATH": str(tofu_log),
                "TOFU_LOG_CORE": "TRACE",
                "AWS_ENDPOINT_URL_S3": "https://hostile-s3.invalid",
                "AWS_S3_ENDPOINT": "https://legacy-hostile-s3.invalid",
                "GH_TOKEN": "ambient-gh-token",
                "GITHUB_TOKEN": "ambient-github-token",
                "GH_ENTERPRISE_TOKEN": "ambient-enterprise-token",
                "HTTP_PROXY": "http://proxy-sentinel.invalid:8080",
            }.items()
        }
    )
    script_literal = str(SCRIPT).replace("'", "''")
    image = (
        "ghcr.io/neuraln-cyber/gen-automation-a14b-registry/"
        "video-worker-a14b-private@sha256:" + "a" * 64
    )
    command = (
        f"try {{ & '{script_literal}' -Image '{image}' "
        "-DeploymentId 'd32be515-170f-416a-a356-3c70ef30db52' } catch {} ; "
        "Write-Output ('RESTORE_HTTP_PROXY=' + $env:HTTP_PROXY); "
        "Write-Output ('RESTORE_TF_WORKSPACE=' + $env:TF_WORKSPACE); "
        "Write-Output ('RESTORE_GH_TOKEN=' + $env:GH_TOKEN)"
    )
    result = subprocess.run(  # noqa: S603 - fixed shell and repository-owned script
        [executable, "-NoProfile", "-NonInteractive", "-Command", command],
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert capture.exists(), (
        result.stdout.decode(errors="replace"),
        result.stderr.decode(errors="replace"),
    )
    assert capture.read_text(encoding="utf-8").splitlines() == [
        "clean-workspace",
        "clean-output",
    ]
    output = result.stdout.decode(errors="replace")
    assert "RESTORE_HTTP_PROXY=http://proxy-sentinel.invalid:8080" in output
    assert "RESTORE_TF_WORKSPACE=hostile-workspace" in output
    assert "RESTORE_GH_TOKEN=ambient-gh-token" in output
    assert not tf_log.exists()
    assert not tofu_log.exists()


def test_powershell_wrapper_parses_without_execution() -> None:
    executable = shutil.which("powershell") or shutil.which("pwsh")
    if executable is None:
        pytest.skip("PowerShell is not installed")
    command = (
        "$errors=$null; "
        f"[System.Management.Automation.Language.Parser]::ParseFile('{SCRIPT}',"
        "[ref]$null,[ref]$errors) > $null; "
        "if ($errors.Count -ne 0) { exit 1 }"
    )
    result = subprocess.run(  # noqa: S603 - fixed parser and repository-owned file
        [executable, "-NoProfile", "-NonInteractive", "-Command", command],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")


def test_control_plane_parameter_read_boundary_allows_only_two_exact_parameters() -> None:
    source = IAM.read_text(encoding="utf-8")
    runpod = 'parameter/${local.name}/runpod/inference-api-key"'
    a14b = 'parameter/${local.name}/a14b/ghcr-pull-once"'
    assert source.count(runpod) == 2
    assert source.count(a14b) == 2
    assert 'sid     = "ReadExactRuntimeParameters"' in source
    assert 'sid    = "DenyOtherRuntimeParameterReads"' in source
    assert 'effect = "Deny"' in source
    assert "not_resources = [" in source
    assert '"ssm:GetParameter"' in source
    assert '"ssm:GetParameterHistory"' in source
    assert '"ssm:GetParameters"' in source
    assert '"ssm:GetParametersByPath"' in source
    assert "AmazonSSMManagedInstanceCore" in source


def test_runbook_never_instructs_operator_to_materialize_the_token() -> None:
    source = RUNBOOK.read_text(encoding="utf-8")
    assert "provision-a14b-private-staging.ps1" in source
    assert "-InstanceId" not in source
    assert "control_plane_instance_id" in source
    assert "/gen-automation-staging/a14b/ghcr-pull-once" in source
    assert "Overwrite=False" not in source
    assert "gh auth token" not in source
    assert "environment variable, file, or" in source
    assert "Never\nblindly retry" in source
