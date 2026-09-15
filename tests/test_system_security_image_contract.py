import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "filename",
    ("Dockerfile", "Dockerfile.mega", "Dockerfile.semantic-gateway", "Dockerfile.patreon-browser"),
)
def test_debian_security_update_is_pinned_and_limited_to_existing_packages(filename: str) -> None:
    dockerfile = (ROOT / filename).read_text(encoding="utf-8")
    logical = re.sub(r"\\\r?\n\s*", " ", dockerfile)
    logical = re.sub(r"[ \t]+", " ", logical)
    security_update = (
        "RUN apt-get update "
        "&& apt-get install --yes --no-install-recommends --no-remove --only-upgrade "
        "libc6=2.41-12+deb13u4 "
        "libc-bin=2.41-12+deb13u4 "
        "perl-base=5.40.1-6+deb13u1 "
        "&& rm -rf /var/lib/apt/lists/*"
    )

    assert security_update in logical
    assert logical.count("--only-upgrade") == 1
    assert "apt-get upgrade" not in logical
    assert "apt-get dist-upgrade" not in logical
    assert logical.index(security_update) < logical.index("python3.12 -m pip install")
    assert "python:3.12.14-slim@sha256:" in dockerfile


def test_ci_scans_all_built_images_without_relaxing_the_security_gate() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    scans = re.findall(
        r"      - name: Scan [^\n]+ image SBOM\n.*?(?=      - name:|\Z)",
        workflow,
        flags=re.DOTALL,
    )

    assert len(scans) == 6
    for scan in scans:
        sbom = re.search(r"sbom: ([\w-]+\.spdx\.json)", scan)
        assert sbom is not None
        assert f"!cancelled() && hashFiles('{sbom.group(1)}') != ''" in scan
        assert "fail-build: true" in scan
        assert "severity-cutoff: critical" in scan
        assert "only-fixed: true" in scan
        assert "continue-on-error" not in scan
