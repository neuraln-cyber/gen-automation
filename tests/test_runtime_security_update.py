"""Security refresh must stay pinned and preserve the generation runtimes."""

import hashlib
import importlib.util
import json
import os
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
    assert len(pins) == 12
    assert {p["name"] for p in pins} == {
        "python",
        "libpython",
        "libexpat",
        "libffi",
        "cffi",
        "liblzma",
        "libsqlite",
        "libuuid",
        "libxcrypt",
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
    assert '_crypt.crypt("runtime-smoke", "ab")' in installer
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


@pytest.fixture
def obsolete_python_cache(tmp_path, monkeypatch):
    path = ROOT / "scripts/install-i2v-python-security.py"
    spec = importlib.util.spec_from_file_location("obsolete_python_cache_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    prefix = tmp_path / "conda"
    target = prefix / "pkgs" / module.OBSOLETE_PYTHON_CACHE
    (target / "info").mkdir(parents=True)
    (target / "info/index.json").write_text(
        json.dumps({"name": "python", "version": "3.11.14", "build": "hd63d673_2_cpython"})
    )
    (target / "bin").mkdir()
    (target / "bin/python3.11").write_bytes(b"old Python executable")
    (prefix / "conda-meta").mkdir()
    (prefix / "conda-meta/python-3.11.17.json").write_text(
        json.dumps({"name": "python", "version": "3.11.17", "build": "h5f976f7_1_cpython"})
    )
    monkeypatch.setattr(module, "PREFIX", prefix.resolve())
    return module, prefix, target


def test_only_obsolete_cache_is_removed_and_hardlinked_runtime_survives(obsolete_python_cache):
    module, prefix, target = obsolete_python_cache
    retained = prefix / "lib/retained-runtime-file"
    retained.parent.mkdir()
    cached = target / "retained-runtime-file"
    cached.write_bytes(b"unchanged runtime")
    retained.hardlink_to(cached)
    unrelated = prefix / "pkgs/retained-package/file"
    unrelated.parent.mkdir()
    unrelated.write_bytes(b"unrelated cache")
    module.remove_obsolete_python_cache()
    assert not target.exists()
    assert retained.read_bytes() == b"unchanged runtime"
    assert unrelated.read_bytes() == b"unrelated cache"
    assert (prefix / "conda-meta/python-3.11.17.json").is_file()


@pytest.mark.parametrize(
    "field,value", [("name", "torch"), ("version", "3.11.17"), ("build", "other")]
)
def test_cache_removal_requires_exact_obsolete_identity(obsolete_python_cache, field, value):
    module, _, target = obsolete_python_cache
    metadata = target / "info/index.json"
    record = json.loads(metadata.read_text())
    record[field] = value
    metadata.write_text(json.dumps(record))
    with pytest.raises(AssertionError, match="Unexpected obsolete cache identity"):
        module.remove_obsolete_python_cache()
    assert (target / "bin/python3.11").exists()


def test_cache_removal_refuses_unpatched_installed_python(obsolete_python_cache):
    module, prefix, target = obsolete_python_cache
    (prefix / "conda-meta/python-3.11.17.json").write_text(
        json.dumps({"name": "python", "version": "3.11.14", "build": "hd63d673_2_cpython"})
    )
    with pytest.raises(AssertionError, match="Patched Python must be installed"):
        module.remove_obsolete_python_cache()
    assert target.exists()


def fixture_symlink(link, target, directory, monkeypatch):
    try:
        link.symlink_to(target, target_is_directory=directory)
    except OSError as error:
        if os.name != "nt" or error.winerror != 1314:
            raise
        # Unprivileged Windows cannot create symlinks. Exercise the same guard
        # with a link adapter locally; mandatory Linux CI uses real symlinks.
        if directory:
            link.mkdir()
        else:
            link.touch()
        original_is_symlink = Path.is_symlink
        original_resolve = Path.resolve
        monkeypatch.setattr(Path, "is_symlink", lambda p: p == link or original_is_symlink(p))
        monkeypatch.setattr(
            Path,
            "resolve",
            lambda p, **kwargs: (
                target.resolve(**kwargs) if p == link else original_resolve(p, **kwargs)
            ),
        )


@pytest.mark.parametrize("directory", [False, True])
def test_cache_removal_refuses_retained_symlinks(obsolete_python_cache, directory, monkeypatch):
    module, prefix, target = obsolete_python_cache
    link = prefix / "runtime-link"
    fixture_symlink(
        link, target / "bin" if directory else target / "bin/python3.11", directory, monkeypatch
    )
    with pytest.raises(AssertionError, match="Retained symlink references"):
        module.remove_obsolete_python_cache()
    assert target.exists() and link.exists()


def test_cache_removal_refuses_symlinked_cache_root(obsolete_python_cache, monkeypatch):
    module, prefix, target = obsolete_python_cache
    original = prefix / "pkgs"
    relocated = prefix / "relocated-pkgs"
    original.rename(relocated)
    fixture_symlink(original, relocated, True, monkeypatch)
    with pytest.raises(AssertionError, match="Unexpected cache path"):
        module.remove_obsolete_python_cache()
    assert (relocated / target.name / "bin/python3.11").exists()


def test_cache_cleanup_is_bracketed_by_native_runtime_verification(monkeypatch):
    path = ROOT / "scripts/install-i2v-python-security.py"
    spec = importlib.util.spec_from_file_location("security_cleanup_order_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls = []
    monkeypatch.setattr(module, "verify", lambda: calls.append("verify"))
    monkeypatch.setattr(module, "remove_obsolete_python_cache", lambda: calls.append("clean"))
    module.verify_and_clean()
    assert calls == ["verify", "clean", "verify"]
