import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE_PATH = ROOT / "Dockerfile"

DOCKERFILE_FRONTEND = (
    "docker/dockerfile:1.7.1@"
    "sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e"
)
PYTHON_IMAGE = (
    "python:3.12-slim@sha256:cab2dbf575e971934a81e4622f5aba17aa7929719bd7e31033a3a83b97fd0464"
)


def _dockerfile() -> str:
    return DOCKERFILE_PATH.read_text(encoding="utf-8")


def _logical_lines(value: str) -> str:
    joined = re.sub(r"\\\r?\n\s*", " ", value)
    return re.sub(r"[ \t]+", " ", joined)


def test_control_plane_base_and_frontend_are_immutable() -> None:
    dockerfile = _dockerfile()
    from_lines = [
        line.strip() for line in dockerfile.splitlines() if line.strip().upper().startswith("FROM ")
    ]

    assert dockerfile.splitlines()[0] == f"# syntax={DOCKERFILE_FRONTEND}"
    assert from_lines == [f"FROM {PYTHON_IMAGE}"]
    assert "ARG " not in dockerfile
    assert ":latest" not in dockerfile.casefold()


def test_control_plane_dependencies_are_hash_locked_binary_wheels() -> None:
    dockerfile = _logical_lines(_dockerfile())

    assert (
        "python3.12 -m pip install --only-binary=:all: "
        "--require-hashes --no-deps -r requirements.lock" in dockerfile
    )
    assert "python3.12 -m pip check" in dockerfile
    assert "assert sys.version_info[:2] == (3, 12)" in dockerfile
    assert "apt-get" not in dockerfile


def test_control_plane_runs_as_fixed_non_root_user_with_private_home() -> None:
    dockerfile = _dockerfile()
    users = re.findall(r"^USER\s+(.+)$", dockerfile, flags=re.MULTILINE)

    assert users == ["10001:10001"]
    assert "--uid 10001" in dockerfile
    assert "--gid app" in dockerfile
    assert "--create-home" in dockerfile
    assert "--home-dir /home/app" in dockerfile
    assert "HOME=/home/app" in dockerfile
    assert dockerfile.index("COPY src ./src") < dockerfile.index("USER 10001:10001")


def test_control_plane_command_does_not_trust_forwarded_headers() -> None:
    dockerfile = _dockerfile()
    commands = re.findall(r"^CMD\s+(.+)$", dockerfile, flags=re.MULTILINE)

    assert commands == [
        '["python3.12", "-m", "uvicorn", "gen_automation.app:app", '
        '"--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers", '
        '"--no-access-log", "--no-server-header", "--no-date-header"]'
    ]
    assert "--proxy-headers" not in commands[0].replace("--no-proxy-headers", "")


def test_control_plane_has_internal_liveness_healthcheck() -> None:
    dockerfile = _logical_lines(_dockerfile())

    assert "HEALTHCHECK --interval=20s --timeout=3s --start-period=30s --retries=3" in dockerfile
    assert "http://127.0.0.1:8000/api/v1/health/live" in dockerfile


def test_owner_bootstrap_module_is_an_executable_noninteractive_fail_closed_entrypoint() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "gen_automation.cli", "bootstrap-owner"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 2
    assert "interactive TTY" in result.stderr
    assert "Traceback" not in result.stderr
