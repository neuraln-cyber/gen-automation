import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE_PATH = ROOT / "Dockerfile.mega"
CI_PATH = ROOT / ".github" / "workflows" / "ci.yml"

MEGACMD_VERSION = "2.6.0-2.1"
MEGACMD_SHA256 = "9fb8c9a31a1c40b57fb54b2e8877988d957a8fcd09b78b3131cff28f0276fd0e"


def test_mega_image_has_a_reproducible_default_package() -> None:
    dockerfile = DOCKERFILE_PATH.read_text(encoding="utf-8")

    assert (
        f"ARG MEGACMD_DEB_URL=https://mega.nz/linux/repo/Debian_13/amd64/"
        f"megacmd_{MEGACMD_VERSION}_amd64.deb"
    ) in dockerfile
    assert f"ARG MEGACMD_DEB_SHA256={MEGACMD_SHA256}" in dockerfile
    assert (
        f'test "$(dpkg-deb --field /tmp/megacmd.deb Version)" = "{MEGACMD_VERSION}"' in dockerfile
    )
    assert re.findall(r"^USER\s+(.+)$", dockerfile, flags=re.MULTILINE) == ["10001:10001"]
    assert ":latest" not in dockerfile.casefold()
    assert (
        "COPY scripts/runpod_i2v_seed_volume.py ./scripts/runpod_i2v_seed_volume.py"
    ) in dockerfile
    for command in ("mega-cmd", "mega-whoami", "mega-https"):
        assert f"command -v {command}" in dockerfile


def test_ci_builds_scans_and_generates_an_sbom_for_the_mega_image() -> None:
    ci = CI_PATH.read_text(encoding="utf-8")

    assert "--file Dockerfile.mega" in ci
    assert "--tag gen-automation-control-plane-mega:test" in ci
    assert "image: gen-automation-control-plane-mega:test" in ci
    assert "output-file: control-plane-mega.spdx.json" in ci
    assert "sbom: control-plane-mega.spdx.json" in ci
