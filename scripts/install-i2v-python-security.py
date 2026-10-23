"""Build-only, offline CPython patch update; never resolve or replace the ML stack."""

import hashlib
import json
import platform
import subprocess
import sys
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


def verify() -> None:
    from conda.core.prefix_data import PrefixData
    from conda.models.match_spec import MatchSpec
    from conda.models.records import PackageRecord

    records = {record.name: record for record in PrefixData(str(PREFIX)).iter_records()}
    libc_name, libc_version = platform.libc_ver()
    assert libc_name == "glibc"
    records["__glibc"] = PackageRecord(
        name="__glibc", version=libc_version, build="0", build_number=0
    )
    for pinned in json.loads(LOCK.read_text()):
        actual = records[pinned["name"]]
        assert (actual.version, actual.build) == (pinned["version"], pinned["build"])
        # Validate the installed artifact's dependencies, not just the copied lock.
        for requirement in actual.depends:
            spec = MatchSpec(requirement)
            assert spec.name in records and spec.match(records[spec.name]), requirement
    assert sys.version_info[:3] == (3, 11, 17)
    import bz2
    import ctypes
    import lzma
    import pyexpat
    import readline
    import sqlite3
    import ssl
    import zlib

    assert all((bz2, ctypes, lzma, pyexpat, readline, sqlite3, ssl, zlib))
    print("Pinned CPython 3.11.17 runtime dependency closure verified", flush=True)


def install() -> None:
    assert Path(sys.prefix) == PREFIX
    assert sys.version_info[:2] == (3, 11)
    pinned = json.loads(LOCK.read_text())
    names = {record["name"] for record in pinned}
    assert len(names) == len(pinned) and "python" in names
    assert not any("torch" in name or "cuda" in name or "cudnn" in name for name in names)
    before = protected_files(names)
    packages = []
    for record in pinned:
        package = ARCHIVES / record["url"].rsplit("/", 1)[-1]
        assert sha256(package) == record["sha256"]
        packages.append(str(package))
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
