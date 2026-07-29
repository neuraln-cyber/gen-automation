"""Credentialless application adapter around the official MEGAcmd commands.

Authentication is deliberately outside this module.  Deployment primes a
dedicated MEGAcmd profile once with a writable-folder link and mounts that
profile into the controller.  The application receives only the profile HOME
path; account passwords, session IDs, folder keys, and write auth-keys never
enter process arguments, environment settings, logs, or the database.
"""

from __future__ import annotations

import asyncio
import os
import re
import stat
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final

from gen_automation.integrations.mega.errors import (
    MegaAmbiguousError,
    MegaConfigurationError,
    MegaProtocolError,
    MegaRetryableError,
)

_MAX_CAPTURE_BYTES: Final = 64 * 1024
_HANDLE_PATTERN: Final = re.compile(r"^H:[A-Za-z0-9_-]{8,64}$")
_SAFE_FILENAME_PATTERN: Final = re.compile(r"^[a-z0-9][a-z0-9._-]{0,199}$")
_MEGA_URL_PATTERN: Final = re.compile(
    r"https://mega\.(?:nz|io)/(?:file|folder)/[A-Za-z0-9_-]+#[A-Za-z0-9_-]+"
)
_ALLOWED_ENVIRONMENT_NAMES: Final = (
    "LANG",
    "LC_ALL",
    "PATH",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "TMPDIR",
    "TZ",
)


@dataclass(frozen=True, slots=True)
class MegaRemoteNode:
    """Opaque MEGA node reference.

    MEGA node handles are not authentication credentials.  They are useful only
    inside the already authenticated writable-folder profile.
    """

    handle: str
    remote_path: str

    def __post_init__(self) -> None:
        if _HANDLE_PATTERN.fullmatch(self.handle) is None:
            raise ValueError("MEGA node handle is invalid")
        validate_remote_path(self.remote_path, allow_root=False)


@dataclass(frozen=True, slots=True)
class MegaCommandResult:
    return_code: int
    stdout: bytes
    output_truncated: bool = False


type MegaCommandRunner = Callable[
    [tuple[str, ...], Path, float],
    Awaitable[MegaCommandResult],
]


class MegaCmdClient:
    """Single-attempt MEGAcmd transport with bounded, non-secret output.

    Mutation commands are never retried here.  The durable caller must inspect
    and adopt the content-addressed remote path before each upload attempt.
    """

    def __init__(
        self,
        *,
        profile_home: Path,
        command_timeout_seconds: float = 900.0,
        runner: MegaCommandRunner | None = None,
    ) -> None:
        if not profile_home.is_absolute():
            raise ValueError("MEGAcmd profile HOME must be absolute")
        normalized_home = Path(os.path.abspath(profile_home))
        if normalized_home == Path(normalized_home.anchor):
            raise ValueError("MEGAcmd profile HOME cannot be a filesystem root")
        if isinstance(command_timeout_seconds, bool) or not 1 <= command_timeout_seconds <= 7200:
            raise ValueError("MEGAcmd command timeout must be between 1 and 7200 seconds")
        self._profile_home = normalized_home
        self._command_timeout_seconds = float(command_timeout_seconds)
        self._runner = runner or _run_megacmd_process

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(profile_home=<mounted-profile>, "
            f"command_timeout_seconds={self._command_timeout_seconds!r})"
        )

    async def ensure_folder(self, remote_folder: str) -> None:
        normalized = validate_remote_path(remote_folder, allow_root=True)
        if normalized == "/":
            return
        await self._run(("mega-mkdir", "-p", normalized), mutation=True)

    async def find_file(self, remote_path: str) -> tuple[MegaRemoteNode, ...]:
        """Return exact-name files below one exact parent folder."""

        normalized = validate_remote_path(remote_path, allow_root=False)
        path = PurePosixPath(normalized)
        filename = validate_remote_filename(path.name)
        parent = str(path.parent)
        result = await self._run(
            (
                "mega-find",
                parent,
                f"--pattern={filename}",
                "--type=f",
                "--print-only-handles",
            ),
            mutation=False,
        )
        handles: set[str] = set()
        for raw_line in result.stdout.splitlines():
            try:
                line = raw_line.decode("ascii").strip()
            except UnicodeDecodeError:
                raise MegaProtocolError("MEGAcmd returned malformed node handles") from None
            if not line:
                continue
            if _HANDLE_PATTERN.fullmatch(line) is None:
                raise MegaProtocolError("MEGAcmd returned malformed node handles")
            handles.add(line)
        return tuple(
            MegaRemoteNode(handle=handle, remote_path=normalized) for handle in sorted(handles)
        )

    async def upload_file(self, local_file: Path, remote_folder: str) -> None:
        """Upload once; ambiguity is resolved by the durable caller on its next pass."""

        normalized_folder = validate_remote_path(remote_folder, allow_root=True)
        normalized_local = await asyncio.to_thread(_validated_local_file, local_file)
        validate_remote_filename(normalized_local.name)
        await self._run(
            ("mega-put", str(normalized_local), normalized_folder),
            mutation=True,
        )

    async def download_node(self, node: MegaRemoteNode, local_folder: Path) -> Path:
        """Download one node handle into an empty private directory."""

        destination = await asyncio.to_thread(
            _validated_empty_directory,
            local_folder,
        )
        await self._run(
            ("mega-get", node.handle, str(destination)),
            mutation=False,
        )
        entries = list(destination.iterdir())
        if len(entries) != 1:
            raise MegaProtocolError("MEGAcmd verification download was not one file")
        downloaded = entries[0]
        if not downloaded.is_file() or downloaded.is_symlink():
            raise MegaProtocolError("MEGAcmd verification download was not a regular file")
        return downloaded

    async def export_node(self, node: MegaRemoteNode) -> str:
        """Create or retrieve a public download URL after terms were accepted manually.

        ``-f`` is intentionally not used: the runtime does not accept MEGA's
        copyright attestation on the operator's behalf.
        """

        existing = await self._run(
            ("mega-export", node.handle),
            mutation=False,
            allow_nonzero=True,
        )
        matched = _MEGA_URL_PATTERN.search(existing.stdout.decode("utf-8", errors="replace"))
        if matched is not None:
            return matched.group(0)
        created = await self._run(("mega-export", "-a", node.handle), mutation=True)
        matched = _MEGA_URL_PATTERN.search(created.stdout.decode("utf-8", errors="replace"))
        if matched is None:
            raise MegaProtocolError("MEGAcmd did not return a valid export URL")
        return matched.group(0)

    async def _run(
        self,
        command: tuple[str, ...],
        *,
        mutation: bool,
        allow_nonzero: bool = False,
    ) -> MegaCommandResult:
        self._assert_profile_available()
        if not command or not command[0].startswith("mega-"):
            raise ValueError("MEGAcmd command is invalid")
        if any(
            not argument or "\x00" in argument or "\r" in argument or "\n" in argument
            for argument in command
        ):
            raise ValueError("MEGAcmd command argument is invalid")
        try:
            result = await self._runner(
                command,
                self._profile_home,
                self._command_timeout_seconds,
            )
        except TimeoutError:
            if mutation:
                raise MegaAmbiguousError(
                    "MEGAcmd mutation timed out with an unknown remote result"
                ) from None
            raise MegaRetryableError("MEGAcmd command timed out") from None
        except OSError:
            if mutation:
                raise MegaAmbiguousError(
                    "MEGAcmd mutation transport failed with an unknown remote result"
                ) from None
            raise MegaRetryableError("MEGAcmd command transport failed") from None
        if result.output_truncated:
            if mutation:
                raise MegaAmbiguousError("MEGAcmd mutation output exceeded its protocol limit")
            raise MegaProtocolError("MEGAcmd output exceeded its protocol limit")
        if result.return_code != 0 and not allow_nonzero:
            if mutation:
                raise MegaAmbiguousError("MEGAcmd mutation returned a non-success result")
            raise MegaRetryableError("MEGAcmd command returned a non-success result")
        return result

    def _assert_profile_available(self) -> None:
        _assert_private_profile_directory(
            self._profile_home,
            unavailable_message="MEGAcmd profile HOME is unavailable or unsafe",
        )
        profile_cache = self._profile_home / ".megaCmd"
        _assert_private_profile_directory(
            profile_cache,
            unavailable_message=(
                "MEGAcmd profile HOME has not been authenticated or its cache is unsafe"
            ),
        )


def validate_remote_filename(value: str) -> str:
    if _SAFE_FILENAME_PATTERN.fullmatch(value) is None or value in {".", ".."}:
        raise ValueError("MEGA remote filename is invalid")
    return value


def _assert_private_profile_directory(
    path: Path,
    *,
    unavailable_message: str,
) -> None:
    try:
        metadata = path.lstat()
    except OSError:
        raise MegaConfigurationError(unavailable_message) from None
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise MegaConfigurationError(unavailable_message)
    if os.name == "posix":
        effective_uid = _effective_uid()
        if (
            effective_uid is None
            or metadata.st_uid != effective_uid
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise MegaConfigurationError(unavailable_message)


def _effective_uid() -> int | None:
    getter = getattr(os, "geteuid", None)
    if not callable(getter):
        return None
    value = getter()
    return value if isinstance(value, int) else None


def validate_remote_path(value: str, *, allow_root: bool) -> str:
    if (
        not isinstance(value, str)
        or not value.startswith("/")
        or len(value) > 1024
        or "\x00" in value
        or "\r" in value
        or "\n" in value
        or "\\" in value
        or "//" in value
    ):
        raise ValueError("MEGA remote path is invalid")
    path = PurePosixPath(value)
    normalized = str(path)
    if normalized != value.rstrip("/") and not (allow_root and value == "/"):
        raise ValueError("MEGA remote path must be normalized")
    if any(part in {".", ".."} for part in path.parts):
        raise ValueError("MEGA remote path cannot contain traversal components")
    if any("*" in part or "?" in part for part in path.parts):
        raise ValueError("MEGA remote path cannot contain wildcard components")
    if normalized == "/" and not allow_root:
        raise ValueError("MEGA remote path cannot be the root")
    return normalized


async def _run_megacmd_process(
    command: tuple[str, ...],
    profile_home: Path,
    timeout_seconds: float,
) -> MegaCommandResult:
    environment = {
        name: value
        for name in _ALLOWED_ENVIRONMENT_NAMES
        if (value := os.environ.get(name)) is not None
    }
    environment["HOME"] = str(profile_home)
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=profile_home,
        env=environment,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    assert process.stdout is not None
    assert process.stderr is not None
    stdout_task = asyncio.create_task(_drain_bounded(process.stdout))
    stderr_task = asyncio.create_task(_drain_bounded(process.stderr))
    try:
        async with asyncio.timeout(timeout_seconds):
            return_code = await process.wait()
    except (TimeoutError, asyncio.CancelledError):
        process.kill()
        await process.wait()
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
        raise
    stdout, stdout_truncated = await stdout_task
    _, stderr_truncated = await stderr_task
    return MegaCommandResult(
        return_code=return_code,
        stdout=stdout,
        output_truncated=stdout_truncated or stderr_truncated,
    )


async def _drain_bounded(
    stream: asyncio.StreamReader,
) -> tuple[bytes, bool]:
    retained = bytearray()
    truncated = False
    while chunk := await stream.read(8192):
        remaining = _MAX_CAPTURE_BYTES - len(retained)
        if remaining > 0:
            retained.extend(chunk[:remaining])
        if len(chunk) > remaining:
            truncated = True
    return bytes(retained), truncated


def _validated_local_file(path: Path) -> Path:
    normalized = path.resolve()
    if not normalized.is_file() or normalized.is_symlink():
        raise MegaConfigurationError("MEGA upload source is unavailable")
    return normalized


def _validated_empty_directory(path: Path) -> Path:
    normalized = path.resolve()
    if not normalized.is_dir() or normalized.is_symlink():
        raise MegaConfigurationError("MEGA verification directory is unavailable")
    if any(normalized.iterdir()):
        raise MegaConfigurationError("MEGA verification directory must be empty")
    return normalized
