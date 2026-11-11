"""Keep large CI images bounded without dropping runtime or security gates."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/ci.yml"
IMAGES = (
    ("control-plane", None),
    ("gpu-worker", "Smoke-test GPU worker Python runtime"),
    ("i2v-worker", "Smoke-test DaSiWa I2V worker Python runtime"),
    ("semantic-gateway", None),
    ("control-plane-mega", None),
    ("patreon-browser", "Smoke-test Patreon Chrome runtime as non-root"),
)


@pytest.mark.parametrize("image,smoke", IMAGES)
def test_each_image_is_tested_and_inventoried_before_releasing_it(
    image: str, smoke: str | None
) -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    steps = re.split(r"(?=      - name: )", workflow.split("  container:\n", 1)[1])
    build = next(i for i, step in enumerate(steps) if f"--tag gen-automation-{image}:test" in step)
    sbom = next(i for i, step in enumerate(steps) if f"image: gen-automation-{image}:test" in step)
    release = next(i for i, step in enumerate(steps) if f"Release tested {image} image" in step)
    scan = next(i for i, step in enumerate(steps) if f"sbom: {image}.spdx.json" in step)
    assert build < sbom < release < scan
    if smoke is not None:
        smoke_index = next(i for i, step in enumerate(steps) if f"- name: {smoke}\n" in step)
        assert build < smoke_index < sbom
        assert "docker run --rm" in steps[smoke_index]
        assert "continue-on-error" not in steps[smoke_index]
        assert "        if:" not in steps[smoke_index]
    assert "anchore/sbom-action@e22c389904149dbc22b58101806040fa8d37a610" in steps[sbom]
    assert "syft-version: v1.42.3" in steps[sbom]
    assert f"output-file: {image}.spdx.json" in steps[sbom]
    assert "        if:" not in steps[sbom]
    assert f"test -s {image}.spdx.json" in steps[release]
    assert f"docker image rm gen-automation-{image}:test\n" in steps[release]
    assert "docker builder prune --all --force" in steps[release]
    assert "docker system prune" not in steps[release]
    assert "rm -" not in steps[release]
    assert "free -m" in steps[release]
    assert "df -h /" in steps[release]
    assert "docker system df" in steps[release]
    next_builds = [i for i, step in enumerate(steps) if i > build and "- name: Build " in step]
    if next_builds:
        assert release < min(next_builds)


def test_lifetime_change_retains_four_jobs_and_all_six_scan_gates() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    jobs = workflow.split("jobs:\n", 1)[1]
    assert re.findall(r"^  ([a-z-]+):$", jobs, re.MULTILINE) == [
        "secrets",
        "verify",
        "postgresql-migrations",
        "container",
    ]
    container = workflow.split("  container:\n", 1)[1]
    assert "runs-on: ubuntu-24.04" in container
    assert container.count("      - name: Build ") == 6
    assert container.count("      - name: Release tested ") == 6
    assert container.count("anchore/sbom-action@") == 6
    assert container.count("anchore/scan-action@") == 6
    for image, _ in IMAGES:
        scan = next(
            step
            for step in re.split(r"(?=      - name: )", container)
            if f"sbom: {image}.spdx.json" in step
        )
        assert "fail-build: true" in scan
        assert "severity-cutoff: critical" in scan
        assert "only-fixed: true" in scan
        assert "continue-on-error" not in scan
