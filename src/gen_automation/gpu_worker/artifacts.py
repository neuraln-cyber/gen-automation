import hashlib
import hmac
import json
import os
import re
import shutil
import stat
import tempfile
import zipfile
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal, NoReturn, Protocol, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

MAX_ARTIFACTS = 64
MAX_ARTIFACT_BYTES = 32 * 1024 * 1024 * 1024
MAX_TOTAL_ARTIFACT_BYTES = 64 * 1024 * 1024 * 1024
MIN_FREE_SPACE_MARGIN_BYTES = 64 * 1024 * 1024
MAX_SAFETENSORS_HEADER_BYTES = 16 * 1024 * 1024
STREAM_READ_BYTES = 1024 * 1024

_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SAFE_TARGET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,223}(?:\.safetensors|\.pt)$")
_URL_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_WINDOWS_RESERVED_NAMES = {
    "aux",
    "clock$",
    "con",
    "nul",
    "prn",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}

Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class ArtifactKind(StrEnum):
    CHECKPOINT = "checkpoint"
    DETECTOR = "detector"
    LORA = "lora"


class ModelArtifactSpec(BaseModel):
    """One immutable model artifact without an embedded URL or credential."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
    )

    logical_name: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    kind: ArtifactKind
    source_object_id: Annotated[str, StringConstraints(min_length=1, max_length=512)] | None = (
        Field(default=None, repr=False)
    )
    downloader_key: Annotated[str, StringConstraints(min_length=1, max_length=128)] | None = Field(
        default=None,
        repr=False,
    )
    sha256: Sha256
    exact_size_bytes: int = Field(ge=10, le=MAX_ARTIFACT_BYTES)
    max_size_bytes: int = Field(ge=10, le=MAX_ARTIFACT_BYTES)
    target_filename: Annotated[str, StringConstraints(min_length=13, max_length=236)]

    @field_validator("logical_name", "downloader_key")
    @classmethod
    def validate_safe_name(cls, value: str | None) -> str | None:
        if value is not None and _SAFE_NAME.fullmatch(value) is None:
            raise ValueError("invalid opaque identifier")
        return value

    @field_validator("source_object_id")
    @classmethod
    def validate_object_identifier(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if (
            value != value.strip()
            or value.startswith(("/", "\\", "//"))
            or _URL_SCHEME.match(value)
            or any(ord(character) < 32 for character in value)
        ):
            raise ValueError("invalid opaque identifier")
        return value

    @field_validator("target_filename")
    @classmethod
    def validate_target_filename(cls, value: str) -> str:
        if (
            _SAFE_TARGET.fullmatch(value) is None
            or Path(value).name != value
            or Path(value).is_absolute()
            or value.split(".", maxsplit=1)[0].casefold() in _WINDOWS_RESERVED_NAMES
        ):
            raise ValueError("invalid artifact target filename")
        return value

    @model_validator(mode="after")
    def validate_source_and_sizes(self) -> "ModelArtifactSpec":
        if (self.source_object_id is None) == (self.downloader_key is None):
            raise ValueError("exactly one artifact source identifier is required")
        if self.exact_size_bytes > self.max_size_bytes:
            raise ValueError("exact artifact size exceeds its maximum")
        suffix = Path(self.target_filename).suffix.casefold()
        if self.kind == ArtifactKind.DETECTOR:
            if suffix != ".pt":
                raise ValueError("detector artifacts must use a PyTorch archive")
        elif suffix != ".safetensors":
            raise ValueError("model artifacts must use Safetensors")
        return self


def _artifact_sort_key(artifact: ModelArtifactSpec) -> tuple[str, ...]:
    return (
        artifact.kind.value,
        artifact.logical_name.casefold(),
        artifact.logical_name,
        artifact.target_filename.casefold(),
        artifact.target_filename,
        artifact.sha256,
    )


def _artifact_canonical_value(artifact: ModelArtifactSpec) -> dict[str, object]:
    return {
        "downloader_key": artifact.downloader_key,
        "exact_size_bytes": artifact.exact_size_bytes,
        "kind": artifact.kind.value,
        "logical_name": artifact.logical_name,
        "max_size_bytes": artifact.max_size_bytes,
        "sha256": artifact.sha256,
        "source_object_id": artifact.source_object_id,
        "target_filename": artifact.target_filename,
    }


def canonical_manifest_bytes(artifacts: Sequence[ModelArtifactSpec]) -> bytes:
    ordered = sorted(artifacts, key=_artifact_sort_key)
    value: dict[str, object] = {
        "artifacts": [_artifact_canonical_value(artifact) for artifact in ordered],
        "version": "v1",
    }
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def calculate_manifest_sha256(artifacts: Sequence[ModelArtifactSpec]) -> str:
    return hashlib.sha256(canonical_manifest_bytes(artifacts)).hexdigest()


class ArtifactManifest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
    )

    version: Literal["v1"]
    artifacts: tuple[ModelArtifactSpec, ...] = Field(min_length=1, max_length=MAX_ARTIFACTS)
    manifest_sha256: Sha256

    @model_validator(mode="after")
    def validate_integrity_and_uniqueness(self) -> "ArtifactManifest":
        canonical_order = tuple(sorted(self.artifacts, key=_artifact_sort_key))
        if self.artifacts != canonical_order:
            raise ValueError("artifact manifest is not in canonical order")

        logical_names: set[str] = set()
        targets: set[tuple[ArtifactKind, str]] = set()
        digests: set[str] = set()
        for artifact in self.artifacts:
            logical_key = artifact.logical_name.casefold()
            target_key = (artifact.kind, artifact.target_filename.casefold())
            if logical_key in logical_names:
                raise ValueError("duplicate artifact logical name")
            if target_key in targets:
                raise ValueError("duplicate artifact target")
            if artifact.sha256 in digests:
                raise ValueError("duplicate artifact digest")
            logical_names.add(logical_key)
            targets.add(target_key)
            digests.add(artifact.sha256)
        if (
            sum(artifact.exact_size_bytes for artifact in self.artifacts) > MAX_TOTAL_ARTIFACT_BYTES
            or sum(artifact.max_size_bytes for artifact in self.artifacts)
            > MAX_TOTAL_ARTIFACT_BYTES
        ):
            raise ValueError("artifact manifest exceeds the aggregate size limit")

        expected = calculate_manifest_sha256(self.artifacts)
        if not hmac.compare_digest(expected, self.manifest_sha256):
            raise ValueError("artifact manifest digest mismatch")
        return self


class ArtifactDownloader(Protocol):
    """Streams bytes for a manifest entry without exposing transport details."""

    def stream(self, artifact: ModelArtifactSpec) -> AsyncIterator[bytes]: ...


class ArtifactBootstrapError(Exception):
    """A deliberately redacted artifact-bootstrap failure."""


@dataclass(frozen=True, slots=True)
class MaterializedArtifact:
    logical_name: str
    kind: ArtifactKind
    target_filename: str
    sha256: str
    size_bytes: int
    adopted_existing: bool


@dataclass(frozen=True, slots=True)
class ArtifactBootstrapResult:
    version: Literal["v1"]
    manifest_sha256: str
    artifacts: tuple[MaterializedArtifact, ...]


class _InvalidArtifactError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class _Root:
    path: Path
    device: int
    inode: int


def _root_from_path(path: Path) -> _Root:
    if not path.is_absolute():
        raise ArtifactBootstrapError("artifact bootstrap failed")
    try:
        path_stat = path.lstat()
        if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISDIR(path_stat.st_mode):
            raise ArtifactBootstrapError("artifact bootstrap failed")
        resolved = path.resolve(strict=True)
        resolved_stat = resolved.stat()
    except (OSError, RuntimeError):
        raise ArtifactBootstrapError("artifact bootstrap failed") from None
    return _Root(path=resolved, device=resolved_stat.st_dev, inode=resolved_stat.st_ino)


def _validate_root_identity(root: _Root) -> None:
    try:
        path_stat = root.path.lstat()
    except OSError:
        raise ArtifactBootstrapError("artifact bootstrap failed") from None
    if (
        stat.S_ISLNK(path_stat.st_mode)
        or not stat.S_ISDIR(path_stat.st_mode)
        or path_stat.st_dev != root.device
        or path_stat.st_ino != root.inode
    ):
        raise ArtifactBootstrapError("artifact bootstrap failed")


def _lstat_optional(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        raise ArtifactBootstrapError("artifact bootstrap failed") from None


def _open_regular(path: Path) -> tuple[int, os.stat_result]:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    before = _lstat_optional(path)
    if before is None:
        raise FileNotFoundError
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise ArtifactBootstrapError("artifact bootstrap failed")
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        raise
    except OSError:
        raise ArtifactBootstrapError("artifact bootstrap failed") from None
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != before.st_dev
            or opened.st_ino != before.st_ino
        ):
            raise ArtifactBootstrapError("artifact bootstrap failed")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor, opened


def _reject_json_constant(_value: str) -> NoReturn:
    raise ValueError("invalid JSON constant")


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON property")
        result[key] = value
    return result


def _validate_safetensors_header(file_object: object, file_size: int) -> None:
    if not hasattr(file_object, "seek") or not hasattr(file_object, "read"):
        raise _InvalidArtifactError
    reader = cast("ReadableBinaryFile", file_object)
    reader.seek(0)
    prefix = reader.read(8)
    if len(prefix) != 8:
        raise _InvalidArtifactError
    header_size = int.from_bytes(prefix, byteorder="little", signed=False)
    if header_size < 2 or header_size > MAX_SAFETENSORS_HEADER_BYTES or header_size > file_size - 8:
        raise _InvalidArtifactError
    header = reader.read(header_size)
    if len(header) != header_size:
        raise _InvalidArtifactError
    try:
        parsed = cast(
            object,
            json.loads(
                header.decode("utf-8"),
                object_pairs_hook=_unique_json_object,
                parse_constant=_reject_json_constant,
            ),
        )
    except (UnicodeDecodeError, ValueError, TypeError, RecursionError):
        raise _InvalidArtifactError from None
    if not isinstance(parsed, dict):
        raise _InvalidArtifactError


def _validate_detector_archive(file_object: object, file_size: int) -> None:
    if not hasattr(file_object, "seek") or not hasattr(file_object, "read"):
        raise _InvalidArtifactError
    reader = cast("ReadableBinaryFile", file_object)
    reader.seek(0)
    if reader.read(4) != b"PK\x03\x04":
        raise _InvalidArtifactError
    reader.seek(0)
    try:
        with zipfile.ZipFile(cast(Any, reader)) as archive:
            members = archive.infolist()
            if (
                not members
                or len(members) > 100_000
                or not any(member.filename.endswith("/data.pkl") for member in members)
            ):
                raise _InvalidArtifactError
            for member in members:
                normalized = member.filename.replace("\\", "/")
                if (
                    not normalized
                    or normalized.startswith("/")
                    or any(part in {"", ".", ".."} for part in normalized.split("/"))
                    or member.file_size < 0
                    or member.file_size > MAX_ARTIFACT_BYTES
                ):
                    raise _InvalidArtifactError
    except (OSError, ValueError, zipfile.BadZipFile, zipfile.LargeZipFile):
        raise _InvalidArtifactError from None
    if file_size < 64:
        raise _InvalidArtifactError


class ReadableBinaryFile(Protocol):
    def seek(self, offset: int, whence: int = 0) -> int: ...

    def read(self, size: int = -1) -> bytes: ...


def _verify_open_file(
    descriptor: int,
    opened: os.stat_result,
    artifact: ModelArtifactSpec,
) -> None:
    with os.fdopen(descriptor, "rb", closefd=True) as file_object:
        if opened.st_size != artifact.exact_size_bytes or opened.st_size > artifact.max_size_bytes:
            raise _InvalidArtifactError
        digest = hashlib.sha256()
        while chunk := file_object.read(STREAM_READ_BYTES):
            digest.update(chunk)
        if not hmac.compare_digest(digest.hexdigest(), artifact.sha256):
            raise _InvalidArtifactError
        if artifact.kind == ArtifactKind.DETECTOR:
            _validate_detector_archive(file_object, opened.st_size)
        else:
            _validate_safetensors_header(file_object, opened.st_size)


def _adopt_existing(path: Path, artifact: ModelArtifactSpec) -> bool:
    existing = _lstat_optional(path)
    if existing is None:
        return False
    if stat.S_ISLNK(existing.st_mode) or not stat.S_ISREG(existing.st_mode):
        raise ArtifactBootstrapError("artifact bootstrap failed")
    try:
        descriptor, opened = _open_regular(path)
    except FileNotFoundError:
        return False
    try:
        _verify_open_file(descriptor, opened, artifact)
    except _InvalidArtifactError:
        return False
    return True


async def _write_download(
    *,
    artifact: ModelArtifactSpec,
    downloader: ArtifactDownloader,
    root: _Root,
    destination: Path,
) -> None:
    _validate_root_identity(root)
    descriptor = -1
    temporary_path: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".artifact-",
            suffix=".tmp",
            dir=root.path,
        )
        temporary_path = Path(temporary_name)
        os.chmod(temporary_path, 0o600)
        digest = hashlib.sha256()
        total_size = 0
        with os.fdopen(descriptor, "w+b", closefd=True) as file_object:
            descriptor = -1
            async for chunk in downloader.stream(artifact):
                if not isinstance(chunk, bytes):
                    raise _InvalidArtifactError
                total_size += len(chunk)
                if total_size > artifact.max_size_bytes or total_size > artifact.exact_size_bytes:
                    raise _InvalidArtifactError
                file_object.write(chunk)
                digest.update(chunk)
            if total_size != artifact.exact_size_bytes:
                raise _InvalidArtifactError
            if not hmac.compare_digest(digest.hexdigest(), artifact.sha256):
                raise _InvalidArtifactError
            file_object.flush()
            os.fsync(file_object.fileno())
            if artifact.kind == ArtifactKind.DETECTOR:
                _validate_detector_archive(file_object, total_size)
            else:
                _validate_safetensors_header(file_object, total_size)
            opened = os.fstat(file_object.fileno())

        temporary_stat = temporary_path.lstat()
        if (
            not stat.S_ISREG(temporary_stat.st_mode)
            or temporary_stat.st_dev != opened.st_dev
            or temporary_stat.st_ino != opened.st_ino
            or temporary_stat.st_size != artifact.exact_size_bytes
        ):
            raise ArtifactBootstrapError("artifact bootstrap failed")

        _validate_root_identity(root)
        destination_stat = _lstat_optional(destination)
        if destination_stat is not None and (
            stat.S_ISLNK(destination_stat.st_mode) or not stat.S_ISREG(destination_stat.st_mode)
        ):
            raise ArtifactBootstrapError("artifact bootstrap failed")
        os.replace(temporary_path, destination)
        temporary_path = None
    except ArtifactBootstrapError:
        raise
    except Exception:
        raise ArtifactBootstrapError("artifact bootstrap failed") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


async def bootstrap_artifacts(
    manifest: ArtifactManifest,
    downloader: ArtifactDownloader,
    *,
    expected_manifest_sha256: Sha256,
    checkpoint_root: Path,
    lora_root: Path,
    detector_root: Path | None = None,
) -> ArtifactBootstrapResult:
    """Materialize a verified manifest into explicitly supplied model roots."""

    if not hmac.compare_digest(manifest.manifest_sha256, expected_manifest_sha256):
        raise ArtifactBootstrapError("artifact bootstrap failed")
    checkpoint = _root_from_path(checkpoint_root)
    lora = _root_from_path(lora_root)
    has_detector = any(artifact.kind == ArtifactKind.DETECTOR for artifact in manifest.artifacts)
    detector = _root_from_path(detector_root) if detector_root is not None else None
    if has_detector and detector is None:
        raise ArtifactBootstrapError("artifact bootstrap failed")
    roots_to_compare = tuple(root for root in (checkpoint, lora, detector) if root is not None)
    identities = {(root.device, root.inode) for root in roots_to_compare}
    if len(identities) != len(roots_to_compare):
        raise ArtifactBootstrapError("artifact bootstrap failed")

    roots = {
        ArtifactKind.CHECKPOINT: checkpoint,
        ArtifactKind.DETECTOR: detector,
        ArtifactKind.LORA: lora,
    }
    materialized: list[MaterializedArtifact] = []
    for artifact in manifest.artifacts:
        root = roots[artifact.kind]
        if root is None:
            raise ArtifactBootstrapError("artifact bootstrap failed")
        _validate_root_identity(root)
        destination = root.path / artifact.target_filename
        adopted = _adopt_existing(destination, artifact)
        if not adopted:
            try:
                free_bytes = shutil.disk_usage(root.path).free
            except OSError:
                raise ArtifactBootstrapError("artifact bootstrap failed") from None
            if free_bytes < artifact.exact_size_bytes + MIN_FREE_SPACE_MARGIN_BYTES:
                raise ArtifactBootstrapError("artifact bootstrap failed")
            await _write_download(
                artifact=artifact,
                downloader=downloader,
                root=root,
                destination=destination,
            )
        materialized.append(
            MaterializedArtifact(
                logical_name=artifact.logical_name,
                kind=artifact.kind,
                target_filename=artifact.target_filename,
                sha256=artifact.sha256,
                size_bytes=artifact.exact_size_bytes,
                adopted_existing=adopted,
            )
        )

    return ArtifactBootstrapResult(
        version="v1",
        manifest_sha256=manifest.manifest_sha256,
        artifacts=tuple(materialized),
    )
