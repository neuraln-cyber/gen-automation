"""Security refresh must stay pinned and preserve the generation runtimes."""

import hashlib
import importlib.util
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("filename", ["Dockerfile.worker", "Dockerfile.i2v-worker"])
def test_go_security_patch_is_verified_and_never_resolved_at_build_time(filename: str) -> None:
    dockerfile = (ROOT / filename).read_text()
    patch = (ROOT / "patches/salad-queue-worker/security-dependencies.patch").read_bytes()
    assert hashlib.sha256(patch).hexdigest() in dockerfile
    assert "git apply --check /tmp/security-dependencies.patch" in dockerfile
    assert "git apply /tmp/security-dependencies.patch" in dockerfile
    assert "go get" not in dockerfile
    assert "go mod verify" in dockerfile and "-mod=readonly" in dockerfile
    assert b"+toolchain go1.26.9" in patch
    assert b"+\tgolang.org/x/net v0.60.0" in patch
    assert set(re.findall(rb"^diff --git a/(\S+)", patch, re.MULTILINE)) == {b"go.mod", b"go.sum"}


def test_base_security_lock_has_only_the_three_approved_packages() -> None:
    pins = dict(
        re.findall(
            r"^([\w-]+)==([^\s]+)",
            (ROOT / "requirements-worker-security.lock").read_text(),
            re.MULTILINE,
        )
    )
    assert pins == {"pillow": "13.0.0", "certifi": "2026.10.19", "uv": "0.13.4"}
    for filename in (
        "requirements.lock",
        "requirements-dev.lock",
        "requirements-comfy.lock",
        "requirements-i2v-worker.lock",
    ):
        value = (ROOT / filename).read_text()
        assert "pillow==13.0.0" in value and "certifi==2026.10.19" in value


def test_python_support_archives_are_hash_pinned_and_exclude_the_ml_stack() -> None:
    pins = json.loads((ROOT / "scripts/i2v-python-security-lock.json").read_text())
    dockerfile = (ROOT / "Dockerfile.i2v-worker").read_text()
    assert len(pins) == 11
    assert {p["name"] for p in pins} == {
        "python",
        "libpython",
        "libexpat",
        "libffi",
        "cffi",
        "liblzma",
        "libsqlite",
        "libuuid",
        "libzlib",
        "ncurses",
        "readline",
    }
    assert next(p for p in pins if p["name"] == "python")["version"] == "3.11.17"
    for pin in pins:
        assert pin["url"].startswith("https://conda.anaconda.org/conda-forge/linux-64/")
        assert re.fullmatch(r"[a-f0-9]{64}", pin["sha256"])
        assert isinstance(pin["depends"], list) and isinstance(pin["constrains"], list)
        assert f"ADD --checksum=sha256:{pin['sha256']}" in dockerfile
        assert pin["url"] in dockerfile
    installer = (ROOT / "scripts/install-i2v-python-security.py").read_text()
    assert '"--offline", "--no-deps"' in installer
    assert "spec.match(records[spec.name])" in installer
    assert 'cffi.FFI().new("int *", 42)[0] == 42' in installer
    assert "protected_files(names) == before" in installer
    assert 'destination / "info/index.json"' in installer
    assert installer.index("validate_dependency_edges(proposed") < installer.index(
        '"install", "--yes"'
    )
    assert "(3, 11, 17)" in installer


def test_dependency_edges_enforce_forward_reverse_and_optional_constraints() -> None:
    from types import SimpleNamespace

    path = ROOT / "scripts/install-i2v-python-security.py"
    spec = importlib.util.spec_from_file_location("security_edges_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    # Exercise edge selection with an exact-match adapter; real Conda MatchSpec
    # and actual archive metadata are mandatory in the Linux container build.
    class ExactSpec:
        def __init__(self, text):
            self.name, self.version = text.split()

        def match(self, record):
            return record.version == self.version

    library = SimpleNamespace(name="library", version="2", depends=[], constrains=[])
    python = SimpleNamespace(
        name="python", version="3", depends=["library 2"], constrains=["optional 1"]
    )
    retained = SimpleNamespace(name="retained", version="1", depends=["library 2"], constrains=[])
    records = {r.name: r for r in (library, python, retained)}
    module.validate_dependency_edges(records, {"python", "library"}, ExactSpec)
    library.version = "1"
    with pytest.raises(AssertionError, match="python: depends: library 2"):
        module.validate_dependency_edges(records, {"python"}, ExactSpec)
    library.version = "2"
    retained.depends = ["library 1"]
    with pytest.raises(AssertionError, match="retained: depends: library 1"):
        module.validate_dependency_edges(records, {"library"}, ExactSpec)
    retained.depends = []
    retained.constrains = ["library 1"]
    with pytest.raises(AssertionError, match="retained: constrains: library 1"):
        module.validate_dependency_edges(records, {"library"}, ExactSpec)
    retained.constrains = []
    records["optional"] = SimpleNamespace(name="optional", version="2", depends=[], constrains=[])
    with pytest.raises(AssertionError, match="python: constrains: optional 1"):
        module.validate_dependency_edges(records, {"python"}, ExactSpec)
    del records["optional"]
    del records["library"]
    with pytest.raises(AssertionError, match="python: depends: library 2"):
        module.validate_dependency_edges(records, {"python"}, ExactSpec)


def test_archive_metadata_must_match_lock_not_hotfixed_api() -> None:
    from types import SimpleNamespace

    path = ROOT / "scripts/install-i2v-python-security.py"
    spec = importlib.util.spec_from_file_location("security_pins_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    pin = {
        "name": "python",
        "version": "3.11.17",
        "build": "exact",
        "depends": [],
        "constrains": [],
    }
    record = SimpleNamespace(**pin)
    module.verify_pins({"python": record}, [pin])
    record.depends = ["zlib <1.3"]
    with pytest.raises(AssertionError, match="python"):
        module.verify_pins({"python": record}, [pin])
    record.depends = []
    record.constrains = ["python_abi 3.11.*"]
    with pytest.raises(AssertionError, match="python"):
        module.verify_pins({"python": record}, [pin])


def test_publication_fingerprints_every_added_runtime_input() -> None:
    publish = (ROOT / ".github/workflows/publish-images.yml").read_text()
    for name in ("worker_inputs", "i2v_inputs"):
        inputs = publish.split(f"{name}=(", 1)[1].split(")", 1)[0]
        assert "requirements-worker-security.lock" in inputs
        assert "scripts/install-worker-security.py" in inputs
        assert "patches/salad-queue-worker/security-dependencies.patch" in inputs
        if name == "i2v_inputs":
            assert "scripts/install-i2v-python-security.py" in inputs
            assert "scripts/i2v-python-security-lock.json" in inputs


def test_protected_runtime_fingerprint_detects_changes(tmp_path: Path, monkeypatch) -> None:
    path = ROOT / "scripts/install-i2v-python-security.py"
    spec = importlib.util.spec_from_file_location("security_installer_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "PREFIX", tmp_path)
    metadata = tmp_path / "conda-meta"
    metadata.mkdir()
    (metadata / "torch.json").write_text('{"name":"torch","version":"2.9.1"}')
    site = tmp_path / "lib/python3.11/site-packages/torch"
    site.mkdir(parents=True)
    binary = site / "test.so"
    binary.write_bytes(b"original ML binary")
    before = module.protected_files({"python"})
    assert len(before) == 2
    binary.write_bytes(b"different binary")
    assert module.protected_files({"python"}) != before


def test_managed_python_override_rejects_distro_owned_packages(tmp_path, monkeypatch) -> None:
    from types import SimpleNamespace

    path = ROOT / "scripts/install-worker-security.py"
    spec = importlib.util.spec_from_file_location("base_security_installer_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.sysconfig, "get_path", lambda name: str(tmp_path))
    assert module.system_package_install_flags() == []
    marker = tmp_path / "EXTERNALLY-MANAGED"
    marker.write_text("distro managed")
    monkeypatch.setattr(module, "sys", SimpleNamespace(prefix="/usr", version_info=(3, 12)))
    location = "/usr/local/lib/python3.12/dist-packages"
    distribution = SimpleNamespace(
        metadata={"Name": "Pillow"}, locate_file=lambda name: Path(location)
    )
    monkeypatch.setattr(module.importlib.metadata, "distributions", lambda: [distribution])
    assert module.system_package_install_flags() == ["--break-system-packages"]
    location = "/usr/lib/python3/dist-packages"
    with pytest.raises(AssertionError, match="distro-owned"):
        module.system_package_install_flags()
    assert marker.read_text() == "distro managed"
