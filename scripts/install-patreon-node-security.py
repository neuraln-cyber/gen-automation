"""Replace only Playwright's audited Node binary, retaining its JS/API and Chrome."""

import hashlib
import importlib.metadata
import os
import platform
import subprocess
import tarfile
from pathlib import Path

PACKAGE = Path("/usr/local/lib/python3.12/site-packages/playwright")
REPLACEMENT = Path("/opt/patreon-node/bin/node")
OLD_SHA256 = "62d66443847de1f527f74afe715900b12884ace52136dc9cd8e91e61acc2f527"
NEW_SHA256 = "904a6d3484241d466989f9df9ba3aab822b1496191bdb28230eb491a55c32a52"
ARCHIVE_SHA256 = "48cc0e21e048790e54c0cf5749c2725005313d652651c40b7a21feb9697aaf75"


def extract_node(archive: Path, destination: Path) -> None:
    assert digest(archive) == ARCHIVE_SHA256, "Unexpected Node archive checksum"
    assert not destination.exists() and not destination.is_symlink()
    assert destination.parent.resolve(strict=True) == destination.parent.absolute()
    # Do not unpack npm, other executables, or arbitrary paths from the archive.
    with tarfile.open(archive, "r:xz") as source:
        members = [
            source.getmember(f"node-v24.22.2-linux-x64/{p}") for p in ("bin/node", "LICENSE")
        ]
        assert all(member.isfile() for member in members), "Unexpected Node archive member"
        destination.mkdir()
        (destination / "bin").mkdir()
        for member, relative in zip(members, ("bin/node", "LICENSE"), strict=True):
            stream = source.extractfile(member)
            assert stream is not None
            with stream, (destination / relative).open("xb") as target:
                while block := stream.read(1024 * 1024):
                    target.write(block)
        (destination / "bin/node").chmod(0o755)
        (destination / "LICENSE").chmod(0o444)


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def package_files(package: Path, binary: Path) -> dict[str, str]:
    return {
        str(path.relative_to(package)): digest(path)
        for path in package.rglob("*")
        if path.is_file() and path != binary and "__pycache__" not in path.parts
    }


def replace_node(package: Path, replacement: Path) -> None:
    binary = package / "driver/node"
    for path in (package, binary.parent, binary, replacement):
        assert not path.is_symlink(), "Unexpected Node path symlink"
        assert path.resolve(strict=True) == path.absolute(), "Indirect Node path"
    assert binary.is_file() and replacement.is_file()
    assert not replacement.is_relative_to(package), "Replacement must be external to package"
    assert digest(binary) == OLD_SHA256, "Unexpected bundled Node checksum"
    assert digest(replacement) == NEW_SHA256, "Unexpected replacement Node checksum"
    before = package_files(package, binary)
    result = subprocess.run(  # noqa: S603
        [str(replacement), "--version"], check=True, capture_output=True, text=True
    )
    assert result.stdout.strip() == "v24.22.2", "Unexpected replacement Node version"
    # Exercise the fixed embedded Brotli codec before removing the old runtime.
    subprocess.run(  # noqa: S603
        [
            str(replacement),
            "-e",
            "const z=require('node:zlib'),a=require('node:assert/strict');"
            "const b=Buffer.from('runtime-smoke');"
            "a.deepEqual(z.brotliDecompressSync(z.brotliCompressSync(b)),b)",
        ],
        check=True,
    )
    # This removes the actual vulnerable executable, not its scanner metadata.
    # Keep the default driver path working as well as PLAYWRIGHT_NODEJS_PATH.
    binary.unlink()
    binary.symlink_to(replacement)
    assert binary.resolve(strict=True) == replacement
    assert digest(binary) == NEW_SHA256
    assert package_files(package, binary) == before, "Playwright JS/API changed"


def main() -> None:
    import playwright
    from playwright._impl._driver import compute_driver_executable

    assert platform.system() == "Linux" and platform.machine() == "x86_64"
    assert importlib.metadata.version("playwright") == "1.61.0"
    assert Path(playwright.__file__).parent == PACKAGE
    assert os.environ["PLAYWRIGHT_NODEJS_PATH"] == str(REPLACEMENT)
    assert compute_driver_executable()[0] == str(REPLACEMENT)
    # Docker ADD supplies this checksum-pinned build-only file, not a shared runtime temp file.
    extract_node(Path("/tmp/patreon-node.tar.xz"), REPLACEMENT.parent.parent)  # noqa: S108
    replace_node(PACKAGE, REPLACEMENT)
    print("Verified Node 24.22.2; Playwright 1.61.0 JS/API and Chrome unchanged", flush=True)


if __name__ == "__main__":
    main()
