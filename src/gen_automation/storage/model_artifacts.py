"""Bounded, content-addressed storage for managed LoRA Safetensors artifacts."""

import hashlib
import json
import math
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import cast
from urllib.parse import urlsplit
from uuid import UUID

import anyio

from gen_automation.integrations.civitai.client import CivitaiClient
from gen_automation.integrations.civitai.models import CivitaiResolvedLora, JSONValue
from gen_automation.storage.base import (
    MultipartPart,
    MultipartUpload,
    ObjectAlreadyExistsError,
    ObjectMetadata,
    ObjectNotFoundError,
    ObjectStore,
    ObjectStoreError,
    ObjectTooLargeError,
    PresignedUpload,
)

MAX_MANAGED_LORA_BYTES = 4 * 1024 * 1024 * 1024
MAX_SAFETENSORS_HEADER_BYTES = 16 * 1024 * 1024
MAX_SAFETENSORS_TENSORS = 100_000
DEFAULT_MULTIPART_PART_BYTES = 8 * 1024 * 1024
MIN_MULTIPART_PART_BYTES = 5 * 1024 * 1024
MAX_MULTIPART_PART_BYTES = 100 * 1024 * 1024
MAX_PROVENANCE_BYTES = 16 * 1024
QUARANTINE_CONTENT_TYPE = "application/octet-stream"
FINAL_CONTENT_TYPE = "application/x-safetensors"
MODEL_STORAGE_CLASS = "INTELLIGENT_TIERING"
QUARANTINE_KEY_PREFIX = "onboarding/loras"
FINAL_KEY_PREFIX = "worker/managed-loras/sha256"

_QUARANTINE_KEY = re.compile(
    r"^onboarding/loras/([0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})/source\.safetensors$"
)
_FINAL_KEY = re.compile(r"^worker/managed-loras/sha256/([0-9a-f]{64})\.safetensors$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]*\.safetensors$", re.I)
_DTYPE = re.compile(r"^[A-Z][A-Z0-9_]{0,31}$")
_DTYPE_BITS = {
    "BOOL": 8,
    "U8": 8,
    "I8": 8,
    "F8_E4M3": 8,
    "F8_E5M2": 8,
    "F8_E8M0": 8,
    "I16": 16,
    "U16": 16,
    "F16": 16,
    "BF16": 16,
    "I32": 32,
    "U32": 32,
    "F32": 32,
    "I64": 64,
    "U64": 64,
    "F64": 64,
    "C64": 64,
    "C128": 128,
    "I4": 4,
    "U4": 4,
    "F4": 4,
    "F6_E2M3": 6,
    "F6_E3M2": 6,
}
_LORA_TENSOR_MARKERS = (
    "lora_",
    "lora.",
    "lokr_",
    "loha_",
    "hada_",
    "dora_",
    "lycoris",
    "boft_",
    "oft_blocks",
)
_FORBIDDEN_PROVENANCE_KEY_PARTS = (
    "api_key",
    "apikey",
    "authorization",
    "bearer",
    "credential",
    "password",
    "presigned",
    "secret",
    "signature",
    "token",
)


class ModelArtifactError(Exception):
    """Safe base error for managed model-artifact operations."""


class ModelArtifactValidationError(ModelArtifactError):
    """The candidate is not a bounded, structurally valid Safetensors artifact."""


class ModelArtifactIntegrityError(ModelArtifactError):
    """Immutable object identity, size, or digest verification failed."""


class ModelArtifactCleanupError(ModelArtifactError):
    """An exact-version cleanup or multipart abort could not be completed."""


@dataclass(frozen=True, slots=True, repr=False)
class QuarantineUpload:
    bucket: str
    key: str
    target_filename: str
    max_bytes: int
    expires_in: int
    grant: PresignedUpload = field(repr=False)

    def __repr__(self) -> str:
        return (
            f"QuarantineUpload(bucket={self.bucket!r}, key={self.key!r}, "
            f"target_filename={self.target_filename!r}, max_bytes={self.max_bytes}, "
            f"expires_in={self.expires_in}, grant=<redacted>)"
        )


@dataclass(frozen=True, slots=True)
class SafetensorsInspection:
    header_bytes: int
    data_bytes: int
    tensor_count: int
    metadata_keys: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StoredModelArtifact:
    bucket: str
    key: str
    version_id: str
    etag: str
    size_bytes: int
    sha256: str
    target_filename: str
    provenance: dict[str, JSONValue]


class ModelArtifactStore:
    """Validate and promote exact S3 versions without involving a GPU."""

    def __init__(
        self,
        store: ObjectStore,
        *,
        multipart_part_bytes: int = DEFAULT_MULTIPART_PART_BYTES,
    ) -> None:
        if (
            isinstance(multipart_part_bytes, bool)
            or not isinstance(multipart_part_bytes, int)
            or not MIN_MULTIPART_PART_BYTES <= multipart_part_bytes <= MAX_MULTIPART_PART_BYTES
        ):
            raise ValueError("multipart part size must be between 5 MiB and 100 MiB")
        self.store = store
        self.multipart_part_bytes = multipart_part_bytes

    async def create_quarantine_upload(
        self,
        *,
        upload_id: UUID | str,
        filename: str,
        expires_in: int = 600,
    ) -> QuarantineUpload:
        identifier = _canonical_uuid(upload_id)
        target_filename = _target_filename(filename)
        if (
            isinstance(expires_in, bool)
            or not isinstance(expires_in, int)
            or not 60 <= expires_in <= 900
        ):
            raise ValueError("quarantine upload expiry must be between 60 and 900 seconds")
        key = f"{QUARANTINE_KEY_PREFIX}/{identifier}/source.safetensors"
        grant = await self.store.presign_upload(
            key=key,
            content_type=QUARANTINE_CONTENT_TYPE,
            metadata={
                "artifact-kind": "managed-lora",
                "format": "safetensors",
                "upload-id": identifier,
            },
            expires_in=expires_in,
            max_bytes=MAX_MANAGED_LORA_BYTES,
        )
        return QuarantineUpload(
            bucket=self.store.bucket,
            key=key,
            target_filename=target_filename,
            max_bytes=MAX_MANAGED_LORA_BYTES,
            expires_in=expires_in,
            grant=grant,
        )

    async def promote_quarantine(
        self,
        *,
        quarantine_key: str,
        version_id: str,
        etag: str | None,
        target_filename: str,
        provenance: Mapping[str, JSONValue] | None = None,
        expected_sha256: str | None = None,
        expected_size_bytes: int | None = None,
    ) -> StoredModelArtifact:
        _validate_quarantine_key(quarantine_key)
        _version_id(version_id)
        target = _target_filename(target_filename)
        durable_provenance = _provenance(provenance or {"provider": "manual"})
        if expected_sha256 is not None:
            expected_sha256 = _expected_sha256(expected_sha256)
        if expected_size_bytes is not None and (
            isinstance(expected_size_bytes, bool)
            or not isinstance(expected_size_bytes, int)
            or not 10 <= expected_size_bytes <= MAX_MANAGED_LORA_BYTES
        ):
            raise ValueError("expected LoRA byte size is invalid")

        source = await self.store.head(quarantine_key, version_id=version_id)
        if source is None:
            raise ObjectNotFoundError("quarantine object version does not exist")
        _validate_source_metadata(source, supplied_etag=etag)
        if expected_size_bytes is not None and source.byte_size != expected_size_bytes:
            raise ModelArtifactIntegrityError("manual LoRA byte size does not match")
        await self._inspect_exact(source)
        digest = await self.store.sha256(
            source.key,
            max_bytes=MAX_MANAGED_LORA_BYTES,
            version_id=version_id,
            etag=source.etag,
        )
        if digest.byte_size != source.byte_size:
            raise ModelArtifactIntegrityError("manual LoRA changed while hashing")
        if expected_sha256 is not None and digest.sha256 != expected_sha256:
            raise ModelArtifactIntegrityError("manual LoRA SHA-256 does not match")
        final_key = _final_key(digest.sha256)
        metadata = _final_metadata(digest.sha256, source="manual")
        try:
            promoted = await self.store.copy_if_absent(
                source_key=source.key,
                destination_key=final_key,
                content_type=FINAL_CONTENT_TYPE,
                metadata=metadata,
                source_version_id=version_id,
                source_etag=source.etag,
                storage_class=MODEL_STORAGE_CLASS,
            )
            _validate_promoted(promoted, sha256=digest.sha256, size_bytes=digest.byte_size)
        except ObjectAlreadyExistsError:
            promoted = await self._require_existing(final_key, sha256=digest.sha256)
        return _stored(
            self.store.bucket,
            promoted,
            sha256=digest.sha256,
            target_filename=target,
            provenance=durable_provenance,
        )

    async def delete_quarantine_exact(self, *, key: str, version_id: str) -> None:
        """Delete an immutable staging version only after catalog commit.

        Quarantine retention is deliberately outside ``promote_quarantine`` so
        a controller crash between S3 promotion and the approval/catalog
        transaction remains replayable from the exact uploaded source.  The
        bucket lifecycle is the bounded fallback if this best-effort cleanup
        cannot complete.
        """

        _validate_quarantine_key(key)
        _version_id(version_id)
        try:
            await self.store.delete(key, version_id=version_id)
        except ObjectStoreError:
            raise ModelArtifactCleanupError(
                "cataloged LoRA but could not delete its exact quarantine version"
            ) from None

    async def ingest_civitai(
        self,
        resolved: CivitaiResolvedLora,
        client: CivitaiClient,
        *,
        provenance: Mapping[str, JSONValue] | None = None,
        progress: Callable[[int], Awaitable[None]] | None = None,
    ) -> StoredModelArtifact:
        expected_sha256 = _expected_sha256(resolved.sha256)
        target = _target_filename(resolved.target_filename)
        durable: dict[str, JSONValue] = resolved.durable_provenance()
        if provenance is not None:
            durable["onboarding"] = _provenance(provenance)
        durable_provenance = _provenance(durable)
        final_key = _final_key(expected_sha256)
        existing = await self.store.head(final_key)
        if existing is not None:
            verified = await self._require_existing(final_key, sha256=expected_sha256)
            if progress is not None:
                await progress(verified.byte_size)
            return _stored(
                self.store.bucket,
                verified,
                sha256=expected_sha256,
                target_filename=target,
                provenance=durable_provenance,
            )

        upload = await self.store.create_multipart_upload(
            key=final_key,
            content_type=FINAL_CONTENT_TYPE,
            metadata=_final_metadata(expected_sha256, source="civitai"),
            storage_class=MODEL_STORAGE_CLASS,
        )
        completed = False
        aborted = False
        try:
            parts, total_bytes, actual_sha256 = await self._stream_civitai_parts(
                upload,
                resolved=resolved,
                client=client,
                progress=progress,
            )
            if actual_sha256 != expected_sha256:
                raise ModelArtifactIntegrityError("Civitai LoRA SHA-256 does not match metadata")
            try:
                promoted = await self.store.complete_multipart_upload(
                    upload,
                    parts=parts,
                    total_bytes=total_bytes,
                )
                completed = True
            except ObjectAlreadyExistsError:
                await self._abort_upload(upload)
                aborted = True
                promoted = await self._require_existing(final_key, sha256=expected_sha256)
            _validate_promoted(
                promoted,
                sha256=expected_sha256,
                size_bytes=total_bytes,
            )
            return _stored(
                self.store.bucket,
                promoted,
                sha256=expected_sha256,
                target_filename=target,
                provenance=durable_provenance,
            )
        except BaseException:
            if not completed and not aborted:
                await self._abort_upload(upload)
            raise

    async def delete_exact(self, *, key: str, version_id: str) -> None:
        if _FINAL_KEY.fullmatch(key) is None:
            raise ValueError("managed LoRA key is outside the content-addressed prefix")
        _version_id(version_id)
        if await self.store.head(key, version_id=version_id) is None:
            return
        try:
            await self.store.delete(key, version_id=version_id)
        except ObjectNotFoundError:
            return
        if await self.store.head(key, version_id=version_id) is not None:
            raise ModelArtifactCleanupError("exact managed LoRA version was not deleted")

    async def _stream_civitai_parts(
        self,
        upload: MultipartUpload,
        *,
        resolved: CivitaiResolvedLora,
        client: CivitaiClient,
        progress: Callable[[int], Awaitable[None]] | None,
    ) -> tuple[tuple[MultipartPart, ...], int, str]:
        digest = hashlib.sha256()
        inspector = _SafetensorsStreamInspector()
        buffer = bytearray()
        parts: list[MultipartPart] = []
        total = 0
        async with client.open_download(resolved, max_bytes=MAX_MANAGED_LORA_BYTES) as chunks:
            async for chunk in chunks:
                if not isinstance(chunk, bytes) or not chunk:
                    raise ModelArtifactValidationError("Civitai download emitted an invalid chunk")
                total += len(chunk)
                if total > MAX_MANAGED_LORA_BYTES:
                    raise ObjectTooLargeError("Civitai LoRA exceeds 4 GiB")
                digest.update(chunk)
                inspector.feed(chunk)
                buffer.extend(chunk)
                while len(buffer) >= self.multipart_part_bytes:
                    part_body = bytes(buffer[: self.multipart_part_bytes])
                    del buffer[: self.multipart_part_bytes]
                    parts.append(
                        await self.store.upload_part(
                            upload,
                            part_number=len(parts) + 1,
                            body=part_body,
                        )
                    )
                    if progress is not None:
                        await progress(total - len(buffer))
        if buffer:
            parts.append(
                await self.store.upload_part(
                    upload,
                    part_number=len(parts) + 1,
                    body=bytes(buffer),
                )
            )
            if progress is not None:
                await progress(total)
        if not parts or total < 10:
            raise ModelArtifactValidationError("Civitai LoRA is empty or truncated")
        inspector.finalize(total)
        return tuple(parts), total, digest.hexdigest()

    async def _inspect_exact(self, metadata: ObjectMetadata) -> SafetensorsInspection:
        if metadata.version_id is None:
            raise ModelArtifactIntegrityError("object storage versioning is required")
        prefix = await self.store.read_range(
            metadata.key,
            start=0,
            length=8,
            version_id=metadata.version_id,
            etag=metadata.etag,
        )
        header_length = _header_length(prefix)
        document = await self.store.read_range(
            metadata.key,
            start=0,
            length=8 + header_length,
            version_id=metadata.version_id,
            etag=metadata.etag,
        )
        return _inspect_safetensors_header(document[8:], total_bytes=metadata.byte_size)

    async def _require_existing(self, key: str, *, sha256: str) -> ObjectMetadata:
        existing = await self.store.head(key)
        if existing is None:
            raise ObjectNotFoundError("content-addressed LoRA disappeared")
        _validate_promoted(existing, sha256=sha256, size_bytes=existing.byte_size)
        await self._inspect_exact(existing)
        if existing.version_id is None:
            raise ModelArtifactIntegrityError("object storage versioning is required")
        digest = await self.store.sha256(
            existing.key,
            max_bytes=MAX_MANAGED_LORA_BYTES,
            version_id=existing.version_id,
            etag=existing.etag,
        )
        if digest.sha256 != sha256 or digest.byte_size != existing.byte_size:
            raise ModelArtifactIntegrityError("existing content-addressed LoRA is corrupt")
        return existing

    async def _abort_upload(self, upload: MultipartUpload) -> None:
        try:
            with anyio.CancelScope(shield=True):
                await self.store.abort_multipart_upload(upload)
        except ObjectStoreError:
            raise ModelArtifactCleanupError(
                "could not abort failed LoRA multipart upload"
            ) from None


class _SafetensorsStreamInspector:
    def __init__(self) -> None:
        self._prefix = bytearray()
        self._required: int | None = None

    def feed(self, chunk: bytes) -> None:
        offset = 0
        if len(self._prefix) < 8:
            amount = min(8 - len(self._prefix), len(chunk))
            self._prefix.extend(chunk[:amount])
            offset = amount
            if len(self._prefix) == 8:
                self._required = 8 + _header_length(bytes(self._prefix))
        if (
            self._required is not None
            and len(self._prefix) < self._required
            and offset < len(chunk)
        ):
            amount = min(self._required - len(self._prefix), len(chunk) - offset)
            self._prefix.extend(chunk[offset : offset + amount])

    def finalize(self, total_bytes: int) -> SafetensorsInspection:
        if self._required is None or len(self._prefix) != self._required:
            raise ModelArtifactValidationError("Safetensors header is truncated")
        return _inspect_safetensors_header(bytes(self._prefix[8:]), total_bytes=total_bytes)


def _header_length(prefix: bytes) -> int:
    if len(prefix) != 8:
        raise ModelArtifactValidationError("Safetensors header prefix is truncated")
    header_length = int.from_bytes(prefix, "little", signed=False)
    if not 2 <= header_length <= MAX_SAFETENSORS_HEADER_BYTES:
        raise ModelArtifactValidationError("Safetensors header length is invalid")
    return header_length


def _inspect_safetensors_header(
    header: bytes,
    *,
    total_bytes: int,
) -> SafetensorsInspection:
    if len(header) > MAX_SAFETENSORS_HEADER_BYTES or total_bytes < 8 + len(header):
        raise ModelArtifactValidationError("Safetensors header is invalid")
    try:
        value = json.loads(
            header,
            object_pairs_hook=_unique_header_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
        raise ModelArtifactValidationError("Safetensors header is invalid JSON") from None
    if not isinstance(value, dict):
        raise ModelArtifactValidationError("Safetensors header must be an object")
    document = cast(dict[str, object], value)
    metadata_keys: tuple[str, ...] = ()
    metadata_value = document.pop("__metadata__", None)
    if metadata_value is not None:
        if not isinstance(metadata_value, dict) or len(metadata_value) > 1_000:
            raise ModelArtifactValidationError("Safetensors metadata is invalid")
        for key, item in metadata_value.items():
            if (
                not isinstance(key, str)
                or not isinstance(item, str)
                or not key
                or len(key) > 500
                or len(item) > 10_000
            ):
                raise ModelArtifactValidationError("Safetensors metadata is invalid")
        metadata_keys = tuple(sorted(metadata_value))
    if not document or len(document) > MAX_SAFETENSORS_TENSORS:
        raise ModelArtifactValidationError("Safetensors file has no bounded tensor table")

    data_bytes = total_bytes - 8 - len(header)
    ranges: list[tuple[int, int]] = []
    has_lora_tensor = False
    for name, raw_spec in document.items():
        if (
            not isinstance(name, str)
            or not name
            or len(name) > 1_000
            or any(ord(character) < 32 or ord(character) == 127 for character in name)
            or not isinstance(raw_spec, dict)
        ):
            raise ModelArtifactValidationError("Safetensors tensor entry is invalid")
        spec = cast(dict[str, object], raw_spec)
        if set(spec) != {"dtype", "shape", "data_offsets"}:
            raise ModelArtifactValidationError("Safetensors tensor schema is invalid")
        dtype = spec["dtype"]
        shape = spec["shape"]
        offsets = spec["data_offsets"]
        if (
            not isinstance(dtype, str)
            or _DTYPE.fullmatch(dtype) is None
            or dtype not in _DTYPE_BITS
        ):
            raise ModelArtifactValidationError("Safetensors tensor dtype is invalid")
        if (
            not isinstance(shape, list)
            or len(shape) > 32
            or any(
                isinstance(dimension, bool)
                or not isinstance(dimension, int)
                or dimension < 0
                or dimension > 2**63 - 1
                for dimension in shape
            )
        ):
            raise ModelArtifactValidationError("Safetensors tensor shape is invalid")
        if (
            not isinstance(offsets, list)
            or len(offsets) != 2
            or any(isinstance(item, bool) or not isinstance(item, int) for item in offsets)
        ):
            raise ModelArtifactValidationError("Safetensors tensor offsets are invalid")
        start, end = cast(list[int], offsets)
        if start < 0 or end < start or end > data_bytes:
            raise ModelArtifactValidationError("Safetensors tensor offsets are outside the file")
        element_count = math.prod(cast(list[int], shape))
        expected_tensor_bytes = (element_count * _DTYPE_BITS[dtype] + 7) // 8
        if end - start != expected_tensor_bytes:
            raise ModelArtifactValidationError(
                "Safetensors tensor shape, dtype, and offsets are inconsistent"
            )
        folded_name = name.casefold()
        has_lora_tensor = has_lora_tensor or any(
            marker in folded_name for marker in _LORA_TENSOR_MARKERS
        )
        ranges.append((start, end))

    cursor = 0
    for start, end in sorted(ranges):
        if start == end:
            continue
        if start != cursor:
            raise ModelArtifactValidationError("Safetensors tensor data has a gap or overlap")
        cursor = end
    if cursor != data_bytes:
        raise ModelArtifactValidationError("Safetensors tensor data does not cover the file")
    if not has_lora_tensor:
        raise ModelArtifactValidationError("Safetensors file does not contain LoRA tensor names")
    return SafetensorsInspection(
        header_bytes=len(header),
        data_bytes=data_bytes,
        tensor_count=len(document),
        metadata_keys=metadata_keys,
    )


def _unique_header_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate Safetensors header key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _canonical_uuid(value: UUID | str) -> str:
    try:
        parsed = value if isinstance(value, UUID) else UUID(value)
    except (AttributeError, TypeError, ValueError):
        raise ValueError("LoRA upload id must be a UUID") from None
    canonical = str(parsed)
    if isinstance(value, str) and value.casefold() != canonical:
        raise ValueError("LoRA upload id must use canonical UUID text")
    return canonical


def _target_filename(value: str) -> str:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or not 13 <= len(value) <= 236
        or PurePosixPath(value).name != value
        or _SAFE_FILENAME.fullmatch(value) is None
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("LoRA target filename must be a safe .safetensors basename")
    return value


def _validate_quarantine_key(value: str) -> None:
    match = _QUARANTINE_KEY.fullmatch(value)
    if match is None:
        raise ValueError("LoRA quarantine key is outside the onboarding prefix")
    _canonical_uuid(match.group(1))


def _version_id(value: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 1_024
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("object version id must be trimmed visible text")


def _expected_sha256(value: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError("LoRA SHA-256 must be lowercase hexadecimal")
    return value


def _final_key(sha256: str) -> str:
    return f"{FINAL_KEY_PREFIX}/{sha256}.safetensors"


def _final_metadata(sha256: str, *, source: str) -> dict[str, str]:
    return {
        "artifact-kind": "managed-lora",
        "format": "safetensors",
        "sha256": sha256,
        "source": source,
    }


def _validate_source_metadata(metadata: ObjectMetadata, *, supplied_etag: str | None) -> None:
    if metadata.version_id is None:
        raise ModelArtifactIntegrityError("object storage versioning is required")
    if metadata.etag is None:
        raise ModelArtifactIntegrityError("quarantine object ETag is missing")
    if supplied_etag is not None and metadata.etag != supplied_etag.strip('"'):
        raise ModelArtifactIntegrityError("quarantine object ETag does not match")
    if not 10 <= metadata.byte_size <= MAX_MANAGED_LORA_BYTES:
        raise ObjectTooLargeError("manual LoRA size is outside the managed limit")
    if metadata.content_type not in {QUARANTINE_CONTENT_TYPE, FINAL_CONTENT_TYPE}:
        raise ModelArtifactValidationError("manual LoRA content type is invalid")


def _validate_promoted(metadata: ObjectMetadata, *, sha256: str, size_bytes: int) -> None:
    if metadata.version_id is None or metadata.etag is None:
        raise ModelArtifactIntegrityError("object storage versioning and ETags are required")
    if (
        metadata.byte_size != size_bytes
        or metadata.content_type != FINAL_CONTENT_TYPE
        or metadata.metadata.get("sha256") != sha256
        or metadata.metadata.get("artifact-kind") != "managed-lora"
        or metadata.metadata.get("format") != "safetensors"
    ):
        raise ModelArtifactIntegrityError("promoted LoRA object metadata is inconsistent")


def _stored(
    bucket: str,
    metadata: ObjectMetadata,
    *,
    sha256: str,
    target_filename: str,
    provenance: dict[str, JSONValue],
) -> StoredModelArtifact:
    if metadata.version_id is None or metadata.etag is None:
        raise ModelArtifactIntegrityError("stored LoRA immutable identity is missing")
    return StoredModelArtifact(
        bucket=bucket,
        key=metadata.key,
        version_id=metadata.version_id,
        etag=metadata.etag,
        size_bytes=metadata.byte_size,
        sha256=sha256,
        target_filename=target_filename,
        provenance=provenance,
    )


def _provenance(value: Mapping[str, JSONValue]) -> dict[str, JSONValue]:
    def inspect(item: object, *, depth: int, counter: list[int]) -> None:
        if depth > 16:
            raise ValueError("LoRA provenance is too deeply nested")
        counter[0] += 1
        if counter[0] > 2_000:
            raise ValueError("LoRA provenance has too many items")
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str) or not key or len(key) > 200:
                    raise ValueError("LoRA provenance key is invalid")
                folded = key.casefold().replace("-", "_")
                if any(part in folded for part in _FORBIDDEN_PROVENANCE_KEY_PARTS):
                    raise ValueError("LoRA provenance must not contain credentials")
                inspect(child, depth=depth + 1, counter=counter)
        elif isinstance(item, list):
            for child in item:
                inspect(child, depth=depth + 1, counter=counter)
        elif isinstance(item, str):
            if len(item) > 2_048 or any(ord(character) < 32 for character in item):
                raise ValueError("LoRA provenance text is invalid")
            if item.startswith(("http://", "https://")):
                if not _canonical_provenance_url(item):
                    raise ValueError("LoRA provenance URL is not canonical HTTPS")
        elif isinstance(item, float) and not math.isfinite(item):
            raise ValueError("LoRA provenance number is invalid")
        elif not isinstance(item, int | float | bool | None):
            raise ValueError("LoRA provenance contains a non-JSON value")

    copied = dict(value)
    inspect(copied, depth=0, counter=[0])
    encoded = json.dumps(
        copied,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    if len(encoded) > MAX_PROVENANCE_BYTES:
        raise ValueError("LoRA provenance exceeds the durable size limit")
    return cast(dict[str, JSONValue], json.loads(encoded))


def _canonical_provenance_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.fragment
    ):
        return False
    if not parsed.query:
        return True
    return bool(
        (parsed.hostname or "").casefold() == "civitai.com"
        and re.fullmatch(r"/models/[1-9][0-9]*", parsed.path)
        and re.fullmatch(r"modelVersionId=[1-9][0-9]*", parsed.query)
    )
