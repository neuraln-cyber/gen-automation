"""Guard the one-binary security replacement without changing browser APIs."""

import importlib.util
import io
import subprocess
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def node_runtime(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "node_security_test", ROOT / "scripts/install-patreon-node-security.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    package = (tmp_path / "playwright").resolve()
    (package / "driver").mkdir(parents=True)
    binary = package / "driver/node"
    binary.write_bytes(b"old-runtime")
    retained = package / "driver/cli.js"
    retained.write_bytes(b"retained driver API")
    replacement = (tmp_path / "patched-node").resolve()
    replacement.write_bytes(b"patched-runtime")
    monkeypatch.setattr(module, "OLD_SHA256", module.digest(binary))
    monkeypatch.setattr(module, "NEW_SHA256", module.digest(replacement))
    monkeypatch.setattr(
        module.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout="v24.22.2\n")
    )
    return module, package, binary, replacement, retained


def test_replaces_only_old_node_and_preserves_driver(node_runtime, monkeypatch):
    module, package, binary, replacement, retained = node_runtime
    # Linux CI exercises a real symlink. Unprivileged Windows uses the same
    # adapter as the existing obsolete-runtime cleanup tests.
    from tests.test_runtime_security_update import fixture_symlink

    original_symlink = Path.symlink_to

    def create_link(path, target):
        monkeypatch.setattr(Path, "symlink_to", original_symlink)
        fixture_symlink(path, target, False, monkeypatch)
        if path.read_bytes() != replacement.read_bytes():
            # Windows adapter does not provide filesystem link contents.
            path.write_bytes(replacement.read_bytes())

    monkeypatch.setattr(Path, "symlink_to", create_link)
    module.replace_node(package, replacement)
    assert binary.resolve() == replacement
    assert retained.read_bytes() == b"retained driver API"
    assert binary.read_bytes() == b"patched-runtime"


@pytest.mark.parametrize("which", ["old", "replacement"])
def test_checksum_mismatch_leaves_old_runtime_untouched(node_runtime, which):
    module, package, binary, replacement, _ = node_runtime
    (binary if which == "old" else replacement).write_bytes(b"unrecognized")
    original = binary.read_bytes()
    with pytest.raises(AssertionError, match="checksum"):
        module.replace_node(package, replacement)
    assert binary.read_bytes() == original


@pytest.mark.parametrize("which", ["package", "driver", "binary", "replacement"])
def test_indirect_path_is_rejected_before_mutation(node_runtime, monkeypatch, which):
    module, package, binary, replacement, _ = node_runtime
    selected = {
        "package": package,
        "driver": binary.parent,
        "binary": binary,
        "replacement": replacement,
    }[which]
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda p: p == selected or original(p))
    with pytest.raises(AssertionError, match="path symlink"):
        module.replace_node(package, replacement)
    assert binary.read_bytes() == b"old-runtime"


def test_wrong_reported_version_is_rejected(node_runtime, monkeypatch):
    module, package, binary, replacement, _ = node_runtime
    monkeypatch.setattr(
        module.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout="v24.22.1")
    )
    with pytest.raises(AssertionError, match="Node version"):
        module.replace_node(package, replacement)
    assert binary.read_bytes() == b"old-runtime"


def test_failed_native_brotli_smoke_leaves_old_runtime_untouched(node_runtime, monkeypatch):
    module, package, binary, replacement, _ = node_runtime

    def run(command, **kwargs):
        if command[1] == "-e":
            raise subprocess.CalledProcessError(1, command)
        return SimpleNamespace(stdout="v24.22.2")

    monkeypatch.setattr(module.subprocess, "run", run)
    with pytest.raises(subprocess.CalledProcessError):
        module.replace_node(package, replacement)
    assert binary.read_bytes() == b"old-runtime"


def test_archive_extracts_only_exact_binary_and_license(node_runtime, tmp_path, monkeypatch):
    module, *_ = node_runtime
    archive = tmp_path / "node.tar.xz"
    with tarfile.open(archive, "w:xz") as output:
        for name in ("bin/node", "LICENSE", "bin/npm", "../escape"):
            member = tarfile.TarInfo(f"node-v24.22.2-linux-x64/{name}")
            member.size = 4
            output.addfile(member, io.BytesIO(b"test"))
    monkeypatch.setattr(module, "ARCHIVE_SHA256", module.digest(archive))
    destination = tmp_path / "extracted"
    module.extract_node(archive, destination)
    assert sorted(
        str(p.relative_to(destination)).replace("\\", "/")
        for p in destination.rglob("*")
        if p.is_file()
    ) == ["LICENSE", "bin/node"]
    assert not (tmp_path / "escape").exists()
    with pytest.raises(AssertionError):
        module.extract_node(archive, destination)


def test_docker_and_ci_use_verified_runtime_without_framework_or_browser_upgrade():
    dockerfile = (ROOT / "Dockerfile.patreon-browser").read_text()
    assert "48cc0e21e048790e54c0cf5749c2725005313d652651c40b7a21feb9697aaf75" in dockerfile
    assert "https://nodejs.org/dist/v24.22.2/node-v24.22.2-linux-x64.tar.xz" in dockerfile
    assert "PLAYWRIGHT_NODEJS_PATH=/opt/patreon-node/bin/node" in dockerfile
    assert "python3.12 /tmp/install-patreon-node-security.py" in dockerfile
    assert "google-chrome-beta_153.0.8010.12-1_amd64.deb" in dockerfile
    assert "playwright==1.61.0" in (ROOT / "requirements-patreon-browser.lock").read_text()
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    assert "assert os.getuid() == 10001" in workflow
    assert 'strip() == "v24.22.2"' in workflow
    assert "with sync_playwright() as playwright:" in workflow
    assert 'page.title() == "patreon-browser-ready"' in workflow
