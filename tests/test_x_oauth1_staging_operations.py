from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOST_HELPER = ROOT / "infra" / "aws-staging" / "deploy" / "configure-x-oauth1.sh"
POWERSHELL_HELPER = ROOT / "scripts" / "configure-x-oauth1-staging.ps1"


def _read(path: Path) -> str:
    raw = path.read_bytes()
    assert raw.startswith((b"#!", b"[CmdletBinding"))
    assert b"\r" not in raw
    assert b"\x00" not in raw
    return raw.decode("utf-8", errors="strict")


def test_host_helper_has_exact_staging_inputs_and_two_operations() -> None:
    source = _read(HOST_HELPER)

    assert 'account_id="861912887470"' in source
    assert 'region="eu-central-1"' in source
    assert "gen-automation-staging/x/oauth1-[A-Za-z0-9]{6}" in source
    assert "^[1-9][0-9]{0,18}$" in source
    assert '"0:0:700"' in source
    assert '"0:0:600"' in source
    assert '"0:0:755"' in source
    assert "--configure)" in source
    assert "--canary)" in source
    assert '[ "$#" -eq 4 ]' in source
    assert '[ "$#" -eq 0 ]' in source
    assert '*) fail "the requested operation is not allowed"' in source


def test_host_helper_acquires_reviewed_locks_in_fixed_order() -> None:
    source = _read(HOST_HELPER)
    activation_lock = 'exec 8>"$activation_lock"'
    activation_flock = "/usr/bin/flock --exclusive --wait 120 8"
    update_lock = 'exec 9>"$update_lock"'
    update_flock = "/usr/bin/flock --exclusive --wait 120 9"

    offsets = [
        source.index(activation_lock),
        source.index(activation_flock),
        source.index(update_lock),
        source.index(update_flock),
    ]
    assert offsets == sorted(offsets)
    assert "/run/lock/gen-automation-semantic-gateway-activation.lock" in source
    assert "/run/lock/gen-automation-control-plane-update.lock" in source


def test_host_helper_keeps_atomic_rollback_material_root_only() -> None:
    source = _read(HOST_HELPER)

    assert 'config_root="/etc/gen-automation"' in source
    assert '"0:0:700"' in source
    assert '"0:0:600"' in source
    assert 'mktemp "$config_root/.control-plane.env.x-oauth1.rollback.XXXXXX"' in source
    assert '/usr/bin/install -o root -g root -m 0600 "$controller_env" "$backup_env"' in source
    assert "tempfile.mkstemp(" in source
    assert "dir=path.parent" in source
    assert "os.fchmod(descriptor, 0o600)" in source
    assert "os.fchown(descriptor, 0, 0)" in source
    assert "os.fsync(output.fileno())" in source
    assert "os.replace(temporary, path)" in source
    assert "os.replace(temporary, target)" in source
    assert "os.fsync(directory)" in source
    assert "trap cleanup EXIT" in source
    assert "rollback_armed=1" in source
    assert 'systemctl restart "$service_name"' in source
    assert "wait_for_ready || rollback_failed=1" in source
    assert "root-only backup was retained" in source
    assert "/usr/local/libexec/.gen-automation-validate-deployment.rollback.XXXXXX" in source
    assert 'atomic_copy_file "$validator_candidate" "$validator" 0755' in source
    assert 'atomic_copy_file "$validator_backup" "$validator" 0755' in source
    assert "Validator rollback needs operator attention" in source


def test_host_helper_validates_health_and_running_settings_before_success() -> None:
    source = _read(HOST_HELPER)

    assert 'validator="/usr/local/libexec/gen-automation-validate-deployment"' in source
    assert "http://127.0.0.1:8000/api/v1/health/ready" in source
    assert "for _ in $(/usr/bin/seq 1 90)" in source
    assert "for _ in $(/usr/bin/seq 1 60)" in source
    assert "runtime_values_match" in source
    assert "compose config --quiet" in source
    assert "--assert-oauth1-configured" in source
    assert source.count("--assert-safe-to-configure") >= 2
    assert source.index('systemctl restart "$service_name"') < source.rindex(
        "--assert-oauth1-configured"
    )
    assert source.rindex('run_container_check --account-binding "$binding_message"') < (
        source.rindex("install_candidate_validator")
    )
    assert source.rindex("install_candidate_validator") < (source.rindex("rollback_armed=0"))
    idempotent = source[source.index("if runtime_values_match") :]
    idempotent = idempotent[: idempotent.index('backup_env="$(')]
    assert idempotent.index("--account-binding") < idempotent.index("install_candidate_validator")


def test_host_canary_is_a_fixed_get_only_account_binding_check() -> None:
    source = _read(HOST_HELPER)
    canary = source[source.index('if [ "$operation" = "--canary" ]') :]
    canary = canary[: canary.index("if runtime_values_match")]

    assert "--account-binding" in canary
    assert 'run_container_check --account-binding "$binding_message"' in canary
    assert "No media was uploaded and no post was created." in source
    assert "systemctl restart" not in canary
    assert "systemctl stop" not in canary
    assert "/2/media" not in source
    assert "/2/tweets" not in source
    assert "set -x" not in source
    assert "source $controller_env" not in source
    for credential_field in (
        "consumer_key",
        "consumer_secret",
        "access_token",
        "access_token_secret",
        "refresh_token",
        "Authorization",
    ):
        assert credential_field not in source


def test_powershell_helper_has_mutually_exclusive_validated_modes() -> None:
    source = _read(POWERSHELL_HELPER)

    for name in ("DryRun", "Configure", "Canary"):
        assert f'ParameterSetName = "{name}"' in source
    assert "ValueFromRemainingArguments = $true" in source
    assert "$RemainingArguments.Count -ne 0" in source
    assert "gen-automation-staging/x/oauth1-[A-Za-z0-9]{6}" in source
    assert '$creatorIdPattern = "^[1-9][0-9]{0,18}$"' in source
    assert '$awsAccountId = "861912887470"' in source
    assert '$awsRegion = "eu-central-1"' in source
    assert "AWSReservedSSO_GenAutomationStagingDeployer_" in source
    assert "Set-StrictMode -Version Latest" in source
    assert "Invoke-Expression" not in source
    assert "cmd /c" not in source
    for credential_field in (
        "consumer_key",
        "consumer_secret",
        "access_token",
        "access_token_secret",
        "Authorization",
    ):
        assert credential_field not in source


def test_powershell_helper_transfers_only_reviewed_bytes_with_integrity_check() -> None:
    source = _read(POWERSHELL_HELPER)

    assert "[IO.File]::ReadAllBytes($hostHelperPath)" in source
    assert "[IO.File]::ReadAllBytes($validatorPath)" in source
    assert "Get-Sha256Hex -Bytes $hostHelperBytes" in source
    assert "Get-Sha256Hex -Bytes $validatorBytes" in source
    assert "ConvertTo-GzipBase64 -Bytes $hostHelperBytes" in source
    assert "ConvertTo-GzipBase64 -Bytes $validatorBytes" in source
    assert "/usr/bin/base64 --decode" in source
    assert "/usr/bin/gzip --decompress" in source
    assert "/usr/bin/sha256sum --check --status" in source
    assert '/usr/bin/bash -n "$payload_dir/configure-x-oauth1.sh"' in source
    assert '/usr/bin/bash -n "$payload_dir/validate-deployment.sh"' in source
    assert '"$payload_dir/validate-deployment.sh"' in source
    assert '/usr/bin/chmod 0700 "$payload_dir"' in source
    assert (
        '/usr/bin/chmod 0600 "$payload_dir/configure-x-oauth1.sh" '
        '"$payload_dir/validate-deployment.sh"'
    ) in source
    assert "trap '/usr/bin/rm -rf -- \"$payload_dir\"' EXIT" in source
    assert "UTF8.GetByteCount($remoteCommand) -gt 24576" in source
    assert 'if ($Value.Contains("\'"))' in source


def test_powershell_ssm_submission_is_bounded_and_uses_private_json() -> None:
    source = _read(POWERSHELL_HELPER)

    assert '"AWS-RunShellScript"' in source
    assert '"--timeout-seconds", "120"' in source
    assert '"--max-concurrency", "1"' in source
    assert '"--max-errors", "0"' in source
    assert "executionTimeout = @(" in source
    assert '$executionTimeout = "300"' in source
    assert '$executionTimeout = "1200"' in source
    assert "[Text.UTF8Encoding]::new($false)" in source
    assert '"--parameters", $parametersUri' in source
    assert "$parametersPath" in source
    assert "Remove-Item -LiteralPath $parametersPath -Force" in source
    assert "--no-cli-pager" in source


def test_powershell_native_calls_use_bounded_capture() -> None:
    source = _read(POWERSHELL_HELPER)

    assert "function Invoke-CapturedNative" in source
    assert '$ErrorActionPreference = "Continue"' in source
    assert "[Management.Automation.ErrorRecord]" in source
    assert "[object]::ReferenceEquals($Error[$errorIndex], $nativeErrorRecord)" in source
    assert source.count("2>&1") == 1
    poll = source[source.index("$deadline = [DateTime]::UtcNow.AddMinutes(25)") :]
    assert "Invoke-CapturedNative -Executable $awsExecutable" in poll
    assert "$invocationError = $invocationResult.StandardError" in poll
    assert '$invocationError -notmatch "InvocationDoesNotExist"' in poll


@pytest.mark.skipif(
    os.name != "nt" or shutil.which("powershell.exe") is None,
    reason="Windows PowerShell native-stream regression probe",
)
def test_powershell_native_capture_is_windows_safe_and_redacted() -> None:
    powershell = shutil.which("powershell.exe")
    assert powershell is not None
    probe = r"""
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$tokens = $null
$parseErrors = $null
$helperPath = $env:GEN_AUTOMATION_TEST_POWERSHELL_HELPER
$ast = [Management.Automation.Language.Parser]::ParseFile(
    $helperPath,
    [ref]$tokens,
    [ref]$parseErrors
)
if ($parseErrors.Count -ne 0) {
    throw "The production helper did not parse."
}
foreach ($functionName in @("Invoke-CapturedNative", "Invoke-CheckedNative")) {
    $functionAst = $ast.Find(
        {
            param($node)
            $node -is [Management.Automation.Language.FunctionDefinitionAst] -and
                $node.Name -eq $functionName
        },
        $true
    )
    if ($null -eq $functionAst) {
        throw "A required production function was not found."
    }
    . ([ScriptBlock]::Create($functionAst.Extent.Text))
}

$child = Join-Path $PSHOME "powershell.exe"
$Error.Clear()
$successOutput = Invoke-CheckedNative `
    -Executable $child `
    -Arguments @(
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        "[Console]::Error.WriteLine('native-warning-marker'); " +
            "[Console]::Out.WriteLine('bounded-output'); exit 0"
    ) `
    -FailureMessage "bounded success failure"
if ($successOutput -ne "bounded-output") {
    throw "Successful native stderr contaminated stdout."
}
if ($Error.Count -ne 0) {
    throw "Successful native stderr remained in the automatic error collection."
}
if ($ErrorActionPreference -ne "Stop") {
    throw "The error preference was not restored after success."
}

$Error.Clear()
$caughtMessage = $null
try {
    Invoke-CheckedNative `
        -Executable $child `
        -Arguments @(
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "[Console]::Error.WriteLine(''); " +
                "[Console]::Error.WriteLine('native-secret-marker'); exit 7"
        ) `
        -FailureMessage "bounded native failure"
}
catch {
    $caughtMessage = $_.Exception.Message
}
if ($caughtMessage -ne "bounded native failure") {
    throw "A native failure escaped the bounded error contract."
}
$retainedErrors = ($Error | ForEach-Object { $_.ToString() }) -join "`n"
if ($retainedErrors.Contains("native-secret-marker")) {
    throw "Native stderr remained in the automatic error collection."
}
if ($ErrorActionPreference -ne "Stop") {
    throw "The error preference was not restored after failure."
}
Write-Output "native-capture-probe-passed"
"""
    environment = os.environ.copy()
    environment["GEN_AUTOMATION_TEST_POWERSHELL_HELPER"] = str(POWERSHELL_HELPER)
    result = subprocess.run(  # noqa: S603 - executable is resolved by shutil.which.
        [powershell, "-NoProfile", "-NonInteractive", "-Command", probe],
        check=False,
        capture_output=True,
        env=environment,
        text=True,
        timeout=20,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "native-capture-probe-passed"


def test_powershell_poll_accepts_only_exact_success_and_is_locally_bounded() -> None:
    source = _read(POWERSHELL_HELPER)

    assert "$deadline = [DateTime]::UtcNow.AddMinutes(25)" in source
    assert "while ([DateTime]::UtcNow -lt $deadline)" in source
    for pending_status in ("Pending", "InProgress", "Delayed"):
        assert f'"{pending_status}" {{ Start-Sleep -Seconds 3; continue }}' in source
    assert '"Success" {' in source
    assert "$invocation.ResponseCode -ne 0" in source
    assert "$invocation.CommandId -ne $commandId" in source
    assert "$invocation.InstanceId -ne $instanceId" in source
    assert '$invocationError -notmatch "InvocationDoesNotExist"' in source
    assert "$expectedOutputs -notcontains $invocation.Output.Trim()" in source
    assert "default { throw" in source
    assert "The bounded staging command did not finish before its local deadline." in source
    assert (
        '$canaryOutput = "X OAuth 1.0a account binding passed. '
        'No media was uploaded and no post was created."'
    ) in source


def test_powershell_dry_run_returns_before_ssm_submission() -> None:
    source = _read(POWERSHELL_HELPER)

    dry_run = source.index("if ($DryRun)")
    send_command = source.index('"ssm", "send-command"')
    assert dry_run < send_command
    assert "Dry run passed. No AWS command was submitted." in source[dry_run:send_command]
