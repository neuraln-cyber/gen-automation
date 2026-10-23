"""Build-only, offline CPython patch update; never resolve or replace the ML stack."""

import hashlib
import json
import platform
import subprocess
import sys
import tempfile
from pathlib import Path

PREFIX = Path("/opt/conda")
ARCHIVES = Path("/opt/i2v-build-security")
LOCK = ARCHIVES / "lock.json"


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def protected_files(names: set[str]) -> dict[str, str]:
    result = {}
    for path in (PREFIX / "conda-meta").glob("*.json"):
        if json.loads(path.read_text())["name"] not in names:
            result[str(path)] = sha256(path)
    # Verify the inherited torch/CUDA binding files themselves, not just versions.
    site = PREFIX / "lib/python3.11/site-packages"
    for package in ("torch", "torchvision", "torchaudio"):
        for path in (site / package).rglob("*"):
            if path.is_file() and "__pycache__" not in path.parts:
                result[str(path)] = sha256(path)
    return result


def runtime_records():
    from conda.core.prefix_data import PrefixData
    from conda.models.records import PackageRecord

    records = {record.name: record for record in PrefixData(str(PREFIX)).iter_records()}
    libc_name, libc_version = platform.libc_ver()
    assert libc_name == "glibc"
    records["__glibc"] = PackageRecord(
        name="__glibc", version=libc_version, build="0", build_number=0
    )
    return records


def verify_pins(records, pinned) -> None:
    for pin in pinned:
        actual = records[pin["name"]]
        assert (actual.version, actual.build) == (pin["version"], pin["build"])
        # Channel API/repodata can be hotfixed separately from immutable archives.
        assert sorted(actual.depends) == sorted(pin["depends"]), actual.name
        assert sorted(actual.constrains) == sorted(pin["constrains"]), actual.name


def validate_dependency_edges(records, names, match_spec) -> None:
    failures = []
    for record in records.values():
        for field in ("depends", "constrains"):
            for requirement in getattr(record, field):
                spec = match_spec(requirement)
                # Check every new edge AND reverse edges from retained packages.
                # Unrelated pre-existing inconsistencies are not silently repaired.
                if record.name not in names and spec.name not in names:
                    continue
                if field == "constrains" and spec.name not in records:
                    continue  # Optional constraints never require installation.
                if spec.name not in records or not spec.match(records[spec.name]):
                    failures.append(f"{record.name}: {field}: {requirement}")
    assert not failures, "; ".join(failures)


def verify() -> None:
    from conda.models.match_spec import MatchSpec

    records = runtime_records()
    pinned = json.loads(LOCK.read_text())
    verify_pins(records, pinned)
    validate_dependency_edges(records, {pin["name"] for pin in pinned}, MatchSpec)
    assert sys.version_info[:3] == (3, 11, 17)
    import _crypt
    import bz2
    import ctypes
    import lzma
    import pyexpat
    import readline
    import sqlite3
    import ssl
    import zlib

    import cffi

    assert cffi.__version__ == "2.1.1"
    assert cffi.FFI().new("int *", 42)[0] == 42
    assert _crypt.crypt("runtime-smoke", "ab").startswith("ab")
    assert all((bz2, ctypes, lzma, pyexpat, readline, sqlite3, ssl, zlib))
    print("Pinned CPython 3.11.17 runtime dependency closure verified", flush=True)


def install() -> None:
    from conda.models.match_spec import MatchSpec
    from conda.models.records import PackageRecord
    from conda_package_handling.api import extract

    assert Path(sys.prefix) == PREFIX
    assert sys.version_info[:2] == (3, 11)
    pinned = json.loads(LOCK.read_text())
    names = {record["name"] for record in pinned}
    assert len(names) == len(pinned) and "python" in names
    assert not any("torch" in name or "cuda" in name or "cudnn" in name for name in names)
    before = protected_files(names)
    packages = []
    proposed = runtime_records()
    with tempfile.TemporaryDirectory(prefix="i2v-python-security-") as temporary:
        for pin in pinned:
            package = ARCHIVES / pin["url"].rsplit("/", 1)[-1]
            assert sha256(package) == pin["sha256"]
            destination = Path(temporary) / pin["name"]
            extract(str(package), dest_dir=str(destination))
            actual = PackageRecord(**json.loads((destination / "info/index.json").read_text()))
            assert actual.name == pin["name"]
            proposed[actual.name] = actual
            packages.append(str(package))
    verify_pins(proposed, pinned)
    validate_dependency_edges(proposed, names, MatchSpec)
    # Exact local package files only: no solver, channel refresh, or ML updates.
    subprocess.run(  # noqa: S603
        [str(PREFIX / "bin/conda"), "install", "--yes", "--offline", "--no-deps", *packages],
        check=True,
    )
    assert protected_files(names) == before, "Unexpected change outside Python support closure"
    subprocess.run(  # noqa: S603
        [str(PREFIX / "bin/python"), str(Path(__file__).resolve()), "verify"], check=True
    )


if __name__ == "__main__":
    if sys.argv[1:] == ["verify"]:
        verify()
    else:
        assert not sys.argv[1:]
        install()
