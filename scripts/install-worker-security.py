"""Replace actual base packages; do not leave vulnerable shadow copies behind."""

import hashlib
import importlib.metadata
import json
import subprocess
import sys
import sysconfig
from pathlib import Path

EXPECTED = {"pillow": "13.0.0", "certifi": "2026.10.19", "uv": "0.13.4"}


def system_package_install_flags() -> list[str]:
    marker = Path(sysconfig.get_path("stdlib")) / "EXTERNALLY-MANAGED"
    if not marker.exists():
        return []
    # The immutable image-worker base deliberately installs its Python/ML
    # wheels under /usr/local using the distro interpreter. Replace only those
    # local wheels; never uninstall a distro-owned package or remove the marker.
    assert sys.prefix == "/usr" and sys.version_info[:2] == (3, 12)
    for distribution in importlib.metadata.distributions():
        if distribution.metadata["Name"].lower() in EXPECTED:
            location = Path(distribution.locate_file("")).resolve()
            assert location.is_relative_to(Path("/usr/local/lib").resolve()), (
                "Refusing to replace a distro-owned package",
                distribution.metadata["Name"],
                str(location),
            )
    return ["--break-system-packages"]


def protected_ml_files() -> dict[str, str]:
    files = {}
    for name in ("torch", "torchvision", "torchaudio"):
        distribution = importlib.metadata.distribution(name)
        for relative in distribution.files or ():
            path = Path(distribution.locate_file(relative))
            if path.is_file() and "__pycache__" not in path.parts:
                with path.open("rb") as stream:
                    files[str(path)] = hashlib.file_digest(stream, "sha256").hexdigest()
    return files


def main() -> None:
    lock = Path(sys.argv[1]).resolve(strict=True)
    install_flags = system_package_install_flags()
    before = protected_ml_files()
    # Conda-owned packages need a real package-manager uninstall before pip
    # replacement, so their files and package inventory both remain accurate.
    conda_meta = Path(sys.prefix) / "conda-meta"
    conda_owned = sorted(
        {
            data["name"]
            for path in conda_meta.glob("*.json")
            if (data := json.loads(path.read_text()))["name"] in EXPECTED
        }
    )
    if conda_owned:
        subprocess.run(  # noqa: S603
            [
                str(Path(sys.prefix) / "bin/conda"),
                "remove",
                "--yes",
                "--offline",
                "--force",
                *conda_owned,
            ],
            check=True,
        )
    subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            *install_flags,
            "--only-binary=:all:",
            "--require-hashes",
            "--no-deps",
            "--upgrade",
            "-r",
            str(lock),
        ],
        check=True,
    )
    assert protected_ml_files() == before, "Security update changed ML package files"
    for distribution in importlib.metadata.distributions():
        name = distribution.metadata["Name"].lower()
        if name in EXPECTED:
            assert distribution.version == EXPECTED[name], (name, distribution.version)
    for name, version in EXPECTED.items():
        assert importlib.metadata.version(name) == version
    print("Base package security versions verified; ML package files unchanged", flush=True)


if __name__ == "__main__":
    main()
