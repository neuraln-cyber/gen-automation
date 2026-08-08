from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import cast
from zipfile import ZIP_STORED, BadZipFile, ZipFile, ZipInfo

from gen_automation.domain.canonical import canonical_json_bytes
from gen_automation.domain.deliverability import (
    PATREON_MAX_DERIVATIVE_IMAGES,
    PATREON_MAX_IMAGE_BYTES,
    PATREON_MAX_TOTAL_IMAGE_BYTES,
)
from gen_automation.integrations.patreon.handoff import (
    PATREON_HANDOFF_SCHEMA,
    PATREON_MAX_ATTESTER_BYTES,
    PATREON_MAX_BODY_BYTES,
    PATREON_MAX_TAG_BYTES,
    PATREON_MAX_TAGS,
    PATREON_MAX_TIER_BYTES,
    PATREON_MAX_TITLE_BYTES,
    PATREON_PART_MANIFEST_SCHEMA,
    PATREON_PUBLIC_PREVIEW_ATTESTATION,
    PATREON_SET_MANIFEST_SCHEMA,
)
from gen_automation.services.outbound_image_privacy import (
    OutboundImagePrivacyError,
    require_metadata_free_image,
)
from gen_automation.storage.images import (
    FORMAT_CONTENT_TYPES,
    ImageVerificationError,
    verify_image_bytes,
)

_MAX_MANIFEST_BYTES = 512 * 1024
_MAX_ARCHIVE_FILES = 105
_CANONICAL_TIMESTAMP = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
_FORMAT_EXTENSIONS: dict[str, frozenset[str]] = {
    "JPEG": frozenset({".jpg", ".jpeg"}),
    "PNG": frozenset({".png"}),
    "WEBP": frozenset({".webp"}),
}


class PatreonBrowserPackageError(ValueError):
    """The handoff archive does not match the browser input contract."""


@dataclass(frozen=True, slots=True)
class PatreonBrowserPackage:
    title: str
    body: str
    tier: str
    tags: tuple[str, ...]
    scheduled_at: str | None
    content_paths: tuple[Path, ...]
    public_preview_path: Path


@dataclass(frozen=True, slots=True)
class _Post:
    title: str
    body: str
    tier: str
    tags: tuple[str, ...]
    scheduled_at: str | None


def load_patreon_browser_package(
    archive_path: Path,
    extraction_root: Path,
    *,
    max_package_bytes: int,
) -> PatreonBrowserPackage:
    """Validate and extract only the UI inputs from a deterministic handoff ZIP."""

    if (
        not archive_path.is_file()
        or archive_path.stat().st_size <= 0
        or archive_path.stat().st_size > max_package_bytes
    ):
        raise PatreonBrowserPackageError("Patreon handoff archive size is invalid")
    extraction_root.mkdir(parents=True, exist_ok=True)
    try:
        with ZipFile(archive_path) as archive:
            infos = archive.infolist()
            if not 5 <= len(infos) <= _MAX_ARCHIVE_FILES:
                raise PatreonBrowserPackageError("Patreon handoff file count is invalid")
            by_name = _validated_entries(infos, max_package_bytes=max_package_bytes)
            manifest_info = by_name.get("manifest.json")
            if manifest_info is not None:
                if manifest_info.file_size > _MAX_MANIFEST_BYTES:
                    raise PatreonBrowserPackageError("Patreon handoff manifest is unavailable")
                manifest = _manifest(archive.read(manifest_info))
                post = _post(manifest)
                content_records, preview_record = _image_records(manifest)
                manifest_names = {"manifest.json"}
            else:
                set_info = by_name.get("set-manifest.json")
                part_info = by_name.get("part-manifest.json")
                if (
                    set_info is None
                    or part_info is None
                    or set_info.file_size > _MAX_MANIFEST_BYTES
                    or part_info.file_size > _MAX_MANIFEST_BYTES
                ):
                    raise PatreonBrowserPackageError("Patreon handoff manifest is unavailable")
                set_manifest = _canonical_manifest(
                    archive.read(set_info),
                    schema=PATREON_SET_MANIFEST_SCHEMA,
                )
                part_manifest = _canonical_manifest(
                    archive.read(part_info),
                    schema=PATREON_PART_MANIFEST_SCHEMA,
                )
                post = _post(set_manifest)
                content_records, preview_record = _multipart_image_records(
                    set_manifest,
                    part_manifest,
                )
                manifest_names = {"set-manifest.json", "part-manifest.json"}
            expected_names = {
                *manifest_names,
                "PUBLICATION_CHECKLIST.md",
                "POST.txt",
                *(record["path"] for record in content_records),
                preview_record["path"],
            }
            if set(by_name) != expected_names:
                raise PatreonBrowserPackageError(
                    "Patreon handoff archive entries do not match its manifest"
                )
            content_paths = tuple(
                _extract_verified_image(
                    archive,
                    by_name,
                    record,
                    extraction_root
                    / f"content-{index:03d}{Path(cast(str, record['path'])).suffix}",
                )
                for index, record in enumerate(content_records, start=1)
            )
            preview_path = _extract_verified_image(
                archive,
                by_name,
                preview_record,
                extraction_root / f"public-preview{Path(cast(str, preview_record['path'])).suffix}",
            )
    except (BadZipFile, KeyError, OSError, json.JSONDecodeError) as error:
        raise PatreonBrowserPackageError("Patreon handoff archive is malformed") from error
    return PatreonBrowserPackage(
        title=post.title,
        body=post.body,
        tier=post.tier,
        tags=post.tags,
        scheduled_at=post.scheduled_at,
        content_paths=content_paths,
        public_preview_path=preview_path,
    )


def _validated_entries(
    infos: list[ZipInfo],
    *,
    max_package_bytes: int,
) -> dict[str, ZipInfo]:
    by_name: dict[str, ZipInfo] = {}
    total = 0
    for info in infos:
        path = Path(info.filename)
        if (
            info.is_dir()
            or info.flag_bits & 0x1
            or info.compress_type != ZIP_STORED
            or path.is_absolute()
            or ".." in path.parts
            or "\\" in info.filename
            or info.filename in by_name
            or info.file_size < 0
        ):
            raise PatreonBrowserPackageError("Patreon handoff contains an unsafe entry")
        total += info.file_size
        if total > max_package_bytes:
            raise PatreonBrowserPackageError("Patreon handoff expands beyond its size limit")
        by_name[info.filename] = info
    return by_name


def _manifest(value: bytes) -> dict[str, object]:
    decoded = _canonical_manifest(value, schema=PATREON_HANDOFF_SCHEMA)
    if decoded.get("publication_mode") != "human_official_ui":
        raise PatreonBrowserPackageError("Patreon handoff publication mode is invalid")
    if decoded.get("reconciliation_mode") != "read_api_and_signed_webhooks_only":
        raise PatreonBrowserPackageError("Patreon handoff reconciliation mode is invalid")
    return decoded


def _canonical_manifest(value: bytes, *, schema: str) -> dict[str, object]:
    decoded = json.loads(value)
    if not isinstance(decoded, dict) or decoded.get("schema") != schema:
        raise PatreonBrowserPackageError("Patreon handoff manifest schema is invalid")
    try:
        if canonical_json_bytes(decoded) != value:
            raise PatreonBrowserPackageError("Patreon handoff manifest is not canonical")
    except (TypeError, ValueError) as error:
        raise PatreonBrowserPackageError("Patreon handoff manifest is not canonical") from error
    return cast(dict[str, object], decoded)


def _multipart_image_records(
    set_manifest: dict[str, object],
    part_manifest: dict[str, object],
) -> tuple[list[dict[str, object]], dict[str, object]]:
    if (
        set_manifest.get("publication_mode") != "human_official_ui"
        or set_manifest.get("reconciliation_mode") != "read_api_and_signed_webhooks_only"
    ):
        raise PatreonBrowserPackageError("Patreon set manifest mode is invalid")
    expected_digest = hashlib.sha256(canonical_json_bytes(set_manifest)).hexdigest()
    raw_records = set_manifest.get("approved_derivatives")
    set_preview = set_manifest.get("public_preview")
    part_records = part_manifest.get("approved_derivatives")
    part_preview = part_manifest.get("public_preview")
    part_number = part_manifest.get("part_number")
    part_count = part_manifest.get("part_count")
    first_ordinal = part_manifest.get("first_ordinal")
    last_ordinal = part_manifest.get("last_ordinal")
    if (
        part_manifest.get("set_manifest_sha256") != expected_digest
        or not isinstance(raw_records, list)
        or not isinstance(set_preview, dict)
        or not isinstance(part_records, list)
        or not isinstance(part_preview, dict)
        or isinstance(part_number, bool)
        or not isinstance(part_number, int)
        or isinstance(part_count, bool)
        or not isinstance(part_count, int)
        or not 1 <= part_number <= part_count
        or isinstance(first_ordinal, bool)
        or not isinstance(first_ordinal, int)
        or isinstance(last_ordinal, bool)
        or not isinstance(last_ordinal, int)
        or not 1 <= first_ordinal <= last_ordinal
        or last_ordinal - first_ordinal + 1 != len(part_records)
        or raw_records[first_ordinal - 1 : last_ordinal] != part_records
    ):
        raise PatreonBrowserPackageError("Patreon archive part manifest is invalid")
    expected_preview = {
        key: value for key, value in set_preview.items() if key != "human_safety_attestation"
    }
    if part_preview != expected_preview:
        raise PatreonBrowserPackageError("Patreon archive preview identity is invalid")
    preview = {
        **part_preview,
        "human_safety_attestation": set_preview.get("human_safety_attestation"),
    }
    manifest: dict[str, object] = {
        "approved_derivatives": part_records,
        "public_preview": preview,
    }
    records, validated_preview = _image_records(
        manifest,
        first_ordinal=first_ordinal,
    )
    return records, validated_preview


def _post(manifest: dict[str, object]) -> _Post:
    value = manifest.get("post")
    if not isinstance(value, dict):
        raise PatreonBrowserPackageError("Patreon post metadata is invalid")
    title = value.get("title")
    body = value.get("body")
    tier = value.get("tier")
    tags = value.get("tags")
    scheduled_at = value.get("scheduled_at")
    if not isinstance(tags, list):
        raise PatreonBrowserPackageError("Patreon post metadata is invalid")
    canonical_title = _bounded_text(
        title,
        label="Patreon title",
        max_bytes=PATREON_MAX_TITLE_BYTES,
        allow_empty=False,
        allow_newlines=False,
    )
    canonical_body = _bounded_text(
        body,
        label="Patreon body",
        max_bytes=PATREON_MAX_BODY_BYTES,
        allow_empty=True,
        allow_newlines=True,
    )
    canonical_tier = _bounded_text(
        tier,
        label="Patreon tier",
        max_bytes=PATREON_MAX_TIER_BYTES,
        allow_empty=False,
        allow_newlines=False,
    )
    canonical_tags = _canonical_tags(tags)
    if scheduled_at is not None:
        _validate_canonical_timestamp(scheduled_at, label="Patreon schedule")
    return _Post(
        title=canonical_title,
        body=canonical_body,
        tier=canonical_tier,
        tags=canonical_tags,
        scheduled_at=cast(str | None, scheduled_at),
    )


def _bounded_text(
    value: object,
    *,
    label: str,
    max_bytes: int,
    allow_empty: bool,
    allow_newlines: bool,
) -> str:
    if not isinstance(value, str):
        raise PatreonBrowserPackageError(f"{label} is invalid")
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    if value != normalized or (not allow_empty and not normalized.strip()):
        raise PatreonBrowserPackageError(f"{label} is not canonical")
    for character in normalized:
        codepoint = ord(character)
        if (
            codepoint == 0
            or codepoint == 127
            or (codepoint < 32 and character not in "\n\t")
            or (not allow_newlines and character in "\n\t")
        ):
            raise PatreonBrowserPackageError(f"{label} contains an invalid character")
    try:
        encoded = normalized.encode("utf-8")
    except UnicodeEncodeError as error:
        raise PatreonBrowserPackageError(f"{label} is not valid UTF-8") from error
    if len(encoded) > max_bytes:
        raise PatreonBrowserPackageError(f"{label} exceeds its size limit")
    return normalized


def _canonical_tags(values: list[object]) -> tuple[str, ...]:
    if len(values) > PATREON_MAX_TAGS:
        raise PatreonBrowserPackageError("Patreon tags exceed their count limit")
    normalized: dict[str, str] = {}
    for index, value in enumerate(values):
        tag = _bounded_text(
            value,
            label=f"Patreon tag {index + 1}",
            max_bytes=PATREON_MAX_TAG_BYTES,
            allow_empty=False,
            allow_newlines=False,
        )
        if tag != tag.strip() or tag.casefold() in normalized:
            raise PatreonBrowserPackageError("Patreon tags are not canonical")
        normalized[tag.casefold()] = tag
    canonical = tuple(sorted(normalized.values(), key=lambda tag: (tag.casefold(), tag)))
    if tuple(values) != canonical:
        raise PatreonBrowserPackageError("Patreon tags are not canonical")
    return canonical


def _validate_attestation(value: object) -> None:
    expected_keys = {"safe_for_public", "attested_by", "attested_at", "statement"}
    if (
        not isinstance(value, dict)
        or set(value) != expected_keys
        or value.get("safe_for_public") is not True
        or value.get("statement") != PATREON_PUBLIC_PREVIEW_ATTESTATION
    ):
        raise PatreonBrowserPackageError("Patreon public preview attestation is invalid")
    _bounded_text(
        value.get("attested_by"),
        label="Patreon public preview attester",
        max_bytes=PATREON_MAX_ATTESTER_BYTES,
        allow_empty=False,
        allow_newlines=False,
    )
    _validate_canonical_timestamp(
        value.get("attested_at"),
        label="Patreon public preview attestation time",
    )


def _validate_canonical_timestamp(value: object, *, label: str) -> None:
    if not isinstance(value, str) or _CANONICAL_TIMESTAMP.fullmatch(value) is None:
        raise PatreonBrowserPackageError(f"{label} is invalid")
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as error:
        raise PatreonBrowserPackageError(f"{label} is invalid") from error


def _image_records(
    manifest: dict[str, object],
    *,
    first_ordinal: int = 1,
) -> tuple[list[dict[str, object]], dict[str, object]]:
    content = manifest.get("approved_derivatives")
    preview = manifest.get("public_preview")
    if (
        not isinstance(content, list)
        or not content
        or len(content) > PATREON_MAX_DERIVATIVE_IMAGES
        or not all(isinstance(record, dict) for record in content)
        or not isinstance(preview, dict)
    ):
        raise PatreonBrowserPackageError("Patreon image metadata is invalid")
    attestation = preview.get("human_safety_attestation")
    _validate_attestation(attestation)
    records = cast(list[dict[str, object]], content)
    total_image_bytes = 0
    for index, record in enumerate(records, start=first_ordinal):
        if record.get("ordinal") != index:
            raise PatreonBrowserPackageError("Patreon content ordering is invalid")
        total_image_bytes += _validate_image_record(
            record,
            prefix="content/",
            expected_keys={
                "ordinal",
                "path",
                "sha256",
                "byte_size",
                "width",
                "height",
                "format",
                "content_type",
            },
        )
    preview_record = cast(dict[str, object], preview)
    total_image_bytes += _validate_image_record(
        preview_record,
        prefix="public-preview/",
        expected_keys={
            "path",
            "sha256",
            "byte_size",
            "width",
            "height",
            "format",
            "content_type",
            "human_safety_attestation",
        },
    )
    if total_image_bytes > PATREON_MAX_TOTAL_IMAGE_BYTES:
        raise PatreonBrowserPackageError("Patreon image collection exceeds its size limit")
    return records, preview_record


def _validate_image_record(
    record: dict[str, object],
    *,
    prefix: str,
    expected_keys: set[str],
) -> int:
    path = record.get("path")
    digest = record.get("sha256")
    size = record.get("byte_size")
    width = record.get("width")
    height = record.get("height")
    image_format = record.get("format")
    content_type = record.get("content_type")
    if (
        set(record) != expected_keys
        or not isinstance(path, str)
        or not path.startswith(prefix)
        or "/" in path[len(prefix) :]
        or not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
        or not isinstance(size, int)
        or isinstance(size, bool)
        or size <= 0
        or size > PATREON_MAX_IMAGE_BYTES
        or not isinstance(width, int)
        or isinstance(width, bool)
        or width <= 0
        or not isinstance(height, int)
        or isinstance(height, bool)
        or height <= 0
        or not isinstance(image_format, str)
        or image_format not in _FORMAT_EXTENSIONS
        or Path(path).suffix.lower() not in _FORMAT_EXTENSIONS.get(image_format, frozenset())
        or not isinstance(content_type, str)
        or content_type != FORMAT_CONTENT_TYPES.get(image_format, (None, None))[0]
    ):
        raise PatreonBrowserPackageError("Patreon image record is invalid")
    return size


def _extract_verified_image(
    archive: ZipFile,
    by_name: dict[str, ZipInfo],
    record: dict[str, object],
    target: Path,
) -> Path:
    source_path = cast(str, record["path"])
    info = by_name.get(source_path)
    if info is None or info.file_size != record["byte_size"]:
        raise PatreonBrowserPackageError("Patreon image entry does not match its manifest")
    body = archive.read(info)
    if hashlib.sha256(body).hexdigest() != record["sha256"]:
        raise PatreonBrowserPackageError("Patreon image digest does not match its manifest")
    try:
        verified = verify_image_bytes(body)
    except ImageVerificationError as error:
        raise PatreonBrowserPackageError(
            "Patreon image entry is not a safe static image"
        ) from error
    if (
        verified.byte_size != record["byte_size"]
        or verified.sha256 != record["sha256"]
        or verified.width != record["width"]
        or verified.height != record["height"]
        or verified.image_format != record["format"]
        or verified.content_type != record["content_type"]
        or Path(source_path).suffix.lower()
        not in _FORMAT_EXTENSIONS.get(verified.image_format, frozenset())
    ):
        raise PatreonBrowserPackageError("Patreon image metadata does not match decoded bytes")
    try:
        require_metadata_free_image(body, content_type=verified.content_type)
    except OutboundImagePrivacyError as error:
        raise PatreonBrowserPackageError(
            "Patreon image entry contains embedded metadata"
        ) from error
    target.write_bytes(body)
    return target
