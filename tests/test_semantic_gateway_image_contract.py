import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE_PATH = ROOT / "Dockerfile.semantic-gateway"
CI_PATH = ROOT / ".github" / "workflows" / "ci.yml"

DOCKERFILE_FRONTEND = (
    "docker/dockerfile:1.7.1@"
    "sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e"
)
PYTHON_IMAGE = (
    "python:3.12-slim@sha256:cab2dbf575e971934a81e4622f5aba17aa7929719bd7e31033a3a83b97fd0464"
)


def _dockerfile() -> str:
    return DOCKERFILE_PATH.read_text(encoding="utf-8")


def test_semantic_gateway_image_is_immutable_and_non_root() -> None:
    dockerfile = _dockerfile()
    from_lines = [
        line.strip() for line in dockerfile.splitlines() if line.strip().upper().startswith("FROM ")
    ]

    assert dockerfile.splitlines()[0] == f"# syntax={DOCKERFILE_FRONTEND}"
    assert from_lines == [f"FROM {PYTHON_IMAGE}"]
    assert re.findall(r"^USER\s+(.+)$", dockerfile, flags=re.MULTILINE) == ["10001:10001"]
    assert "ARG " not in dockerfile
    assert ":latest" not in dockerfile.casefold()


def test_ci_builds_scans_and_generates_an_sbom_for_the_semantic_gateway() -> None:
    ci = CI_PATH.read_text(encoding="utf-8")

    assert "--file Dockerfile.semantic-gateway" in ci
    assert "--tag gen-automation-semantic-gateway:test" in ci
    assert "image: gen-automation-semantic-gateway:test" in ci
    assert "output-file: semantic-gateway.spdx.json" in ci
    assert "sbom: semantic-gateway.spdx.json" in ci
