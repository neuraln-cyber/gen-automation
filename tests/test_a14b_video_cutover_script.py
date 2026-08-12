from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "infra" / "aws-staging" / "deploy" / "cutover-video-worker-a14b.sh"
INSTALLER = ROOT / "infra" / "aws-staging" / "deploy" / "install.sh"
IMAGE = (
    "ghcr.io/neuraln-cyber/gen-automation-a14b-registry/"
    "video-worker-a14b-private@sha256:" + "a" * 64
)
REVISION = "d585214403c2b8090dc468b5045db1cf7b06b3ac"


def _script() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _bash() -> str | None:
    discovered = shutil.which("bash")
    if discovered is not None:
        return discovered
    git_bash = Path("C:/Program Files/Git/bin/bash.exe")
    return str(git_bash) if git_bash.is_file() else None


def _function_source(name: str) -> str:
    lines = _script().splitlines()
    start = lines.index(f"{name}() {{")
    for end in range(start + 1, len(lines)):
        if lines[end] == "}":
            return "\n".join(lines[start : end + 1]) + "\n"
    raise AssertionError(f"unterminated shell function: {name}")


def _run_harness(bash: str, path: Path, body: str) -> subprocess.CompletedProcess[str]:
    path.write_text("#!/usr/bin/env bash\nset -Eeuo pipefail\n" + body, encoding="utf-8")
    return subprocess.run(  # noqa: S603
        [bash, str(path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_cutover_script_has_valid_bash_and_offline_argument_validation() -> None:
    bash = _bash()
    if bash is None:
        pytest.skip("Bash is not installed")
    syntax = subprocess.run(  # noqa: S603
        [bash, "-n", str(SCRIPT)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert syntax.returncode == 0, syntax.stderr

    valid = subprocess.run(  # noqa: S603
        [
            bash,
            str(SCRIPT),
            "--validate-only",
            "--image",
            IMAGE,
            "--expected-control-plane-revision",
            REVISION,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert valid.returncode == 0
    assert "no host or provider state was read" in valid.stdout

    invalid = subprocess.run(  # noqa: S603
        [
            bash,
            str(SCRIPT),
            "--validate-only",
            "--image",
            "ghcr.io/neuraln-cyber/gen-automation/video-worker@sha256:" + "a" * 64,
            "--expected-control-plane-revision",
            REVISION,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert invalid.returncode != 0
    assert "no host or provider state was read" not in invalid.stdout


def test_cutover_changes_only_video_image_key_and_preserves_image_lane() -> None:
    script = _script()

    assert 'video_image_key="GEN_AUTOMATION_SALAD_VIDEO_WORKER_IMAGE"' in script
    assert 'image_lane_key="GEN_AUTOMATION_SALAD_WORKER_IMAGE"' in script
    assert 'awk -v image="$new_image" -v key="$video_image_key"' in script
    assert script.count('env_value "$image_lane_key" "$temporary_env"') == 1
    assert script.count('env_value "$image_lane_key" "$controller_env"') >= 2
    assert "IMAGE lane changed during VIDEO cutover" in script
    assert "assert-cutover-safe" in script
    assert "assert-cutover-applied" in script
    assert "20260811_0036" not in script  # migration proof stays in the in-image verifier

    assignment_targets = re.findall(r'^\s*[a-z_]+="(GEN_AUTOMATION_[A-Z0-9_]+)"$', script, re.M)
    assert assignment_targets == [
        "GEN_AUTOMATION_SALAD_VIDEO_WORKER_IMAGE",
        "GEN_AUTOMATION_SALAD_WORKER_IMAGE",
    ]


def test_cutover_rollback_restores_exact_backup_on_every_post_update_failure() -> None:
    script = _script()

    assert "trap cleanup EXIT" in script
    assert script.index("rollback_armed=1") < script.index(
        '/usr/bin/mv -- "$temporary_env" "$controller_env"'
    )
    assert "restore_previous_environment || true" in script
    assert 'atomic_restore_environment_file "$backup_env" "$controller_env"' in script
    assert '/usr/bin/mv -- "$rollback_restore_env" "$target_environment"' in script
    assert '/usr/bin/cmp --silent "$source_backup" "$target_environment"' in script
    assert "elif ! verify_restored_environment; then" in script
    assert 'wait_for_control_plane "$old_video_image"' in script
    assert "Automatic A14B rollback needs operator attention" in script
    assert "Rollback backup retained at $backup_env" in script
    assert script.index("wait_for_cutover_applied") < script.rindex("rollback_armed=0")


def test_atomic_rollback_helper_replaces_inode_with_exact_backup(
    tmp_path: Path,
) -> None:
    bash = _bash()
    if bash is None:
        pytest.skip("Bash is not installed")
    backup = tmp_path / ".control-plane.env.a14b.rollback.ABC123"
    target = tmp_path / "control-plane.env"
    backup.write_bytes(b"GEN_AUTOMATION_ENVIRONMENT=staging\nexact=prior\n")
    target.write_bytes(b"GEN_AUTOMATION_ENVIRONMENT=staging\nexact=candidate\n")
    previous_inode = target.stat().st_ino
    root = tmp_path.as_posix()
    harness = _run_harness(
        bash,
        tmp_path / "atomic-restore.sh",
        (
            f"config_root='{root}'\n"
            "rollback_restore_env=''\n"
            + _function_source("atomic_restore_environment_file")
            + f"atomic_restore_environment_file '{backup.as_posix()}' '{target.as_posix()}'\n"
            + '[ -z "$rollback_restore_env" ]\n'
        ),
    )

    assert harness.returncode == 0, harness.stderr
    assert target.read_bytes() == backup.read_bytes()
    assert target.stat().st_ino != previous_inode
    assert backup.is_file()


def test_failed_rollback_cleanup_retains_backup_and_removes_only_temporaries(
    tmp_path: Path,
) -> None:
    bash = _bash()
    if bash is None:
        pytest.skip("Bash is not installed")
    backup = tmp_path / ".control-plane.env.a14b.rollback.RETAIN"
    update = tmp_path / ".control-plane.env.a14b.update.DELETE"
    restore = tmp_path / ".control-plane.env.a14b.restore.DELETE"
    backup.write_bytes(b"exact prior environment\n")
    update.write_bytes(b"temporary update\n")
    restore.write_bytes(b"temporary restore\n")
    harness = _run_harness(
        bash,
        tmp_path / "retain-backup.sh",
        (
            f"backup_env='{backup.as_posix()}'\n"
            f"temporary_env='{update.as_posix()}'\n"
            f"rollback_restore_env='{restore.as_posix()}'\n"
            "retain_rollback_backup=1\n" + _function_source("cleanup_files") + "cleanup_files\n"
        ),
    )

    assert harness.returncode == 0, harness.stderr
    assert backup.read_bytes() == b"exact prior environment\n"
    assert not update.exists()
    assert not restore.exists()


def test_failed_post_restore_health_check_reports_and_retains_exact_backup(
    tmp_path: Path,
) -> None:
    bash = _bash()
    if bash is None:
        pytest.skip("Bash is not installed")
    backup = tmp_path / ".control-plane.env.a14b.rollback.HEALTHFAIL"
    target = tmp_path / "control-plane.env"
    backup.write_bytes(b"GEN_AUTOMATION_ENVIRONMENT=staging\nexact=prior\n")
    target.write_bytes(b"GEN_AUTOMATION_ENVIRONMENT=staging\nexact=candidate\n")
    root = tmp_path.as_posix()
    harness = _run_harness(
        bash,
        tmp_path / "failed-health-rollback.sh",
        (
            f"config_root='{root}'\n"
            f"backup_env='{backup.as_posix()}'\n"
            f"controller_env='{target.as_posix()}'\n"
            "temporary_env=''\n"
            "rollback_restore_env=''\n"
            "retain_rollback_backup=0\n"
            + _function_source("atomic_restore_environment_file")
            + "verify_restored_environment() { return 1; }\n"
            + _function_source("restore_previous_environment")
            + _function_source("cleanup_files")
            + "rollback_status=0\n"
            + "restore_previous_environment || rollback_status=$?\n"
            + '[ "$rollback_status" -eq 1 ]\n'
            + '[ "$retain_rollback_backup" -eq 1 ]\n'
            + "cleanup_files\n"
            + f"[ -f '{backup.as_posix()}' ]\n"
        ),
    )

    assert harness.returncode == 0, harness.stderr
    assert target.read_bytes() == backup.read_bytes()
    assert backup.read_bytes() == b"GEN_AUTOMATION_ENVIRONMENT=staging\nexact=prior\n"
    assert f"Rollback backup retained at {backup.as_posix()}" in harness.stderr


def test_cutover_binds_exact_control_plane_contract_and_is_installed() -> None:
    script = _script()
    installer = INSTALLER.read_text(encoding="utf-8")

    assert f'minimum_control_plane_revision="{REVISION}"' in script
    assert '[ "$(control_plane_revision)" = "$expected_revision" ]' in script
    assert "migration, control-plane contract, or drained VIDEO preflight failed" in script
    assert '--minimum-control-plane-revision "$minimum_control_plane_revision"' in script
    assert '"$source_dir/cutover-video-worker-a14b.sh"' in installer
    assert "/usr/local/sbin/gen-automation-cutover-video-worker-a14b" in installer


def test_cutover_script_contains_no_registry_credential_channel() -> None:
    script = _script().casefold()

    assert "ghcr_token" not in script
    assert "ghcr_username" not in script
    assert "registry_authentication" not in script
    assert "--password" not in script
    assert "--token" not in script
