import hashlib
import json
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO
from zipfile import ZIP_STORED, ZipFile, ZipInfo

from PIL import Image

from gen_automation.domain.canonical import canonical_json_bytes
from gen_automation.domain.deliverability import (
    MAX_ACCEPTED_IMAGES_PER_RELEASE,
)
from gen_automation.domain.deliverability import (
    PATREON_MAX_ARCHIVE_BYTES as PATREON_MAX_ARCHIVE_BYTES,
)
from gen_automation.domain.deliverability import (
    PATREON_MAX_DERIVATIVE_IMAGES as PATREON_MAX_DERIVATIVE_IMAGES,
)
from gen_automation.domain.deliverability import (
    PATREON_MAX_IMAGE_BYTES as PATREON_MAX_IMAGE_BYTES,
)
from gen_automation.domain.deliverability import (
    PATREON_MAX_TOTAL_IMAGE_BYTES as PATREON_MAX_TOTAL_IMAGE_BYTES,
)
from gen_automation.storage.images import (
    ImageVerificationError,
    VerifiedImage,
    verify_image_bytes,
)

PATREON_HANDOFF_SCHEMA = "gen-automation.patreon-handoff.v1"
PATREON_SET_MANIFEST_SCHEMA = "gen-automation.patreon-set-manifest.v1"
PATREON_PART_MANIFEST_SCHEMA = "gen-automation.patreon-handoff-part.v1"
PATREON_MAX_TITLE_BYTES = 512
PATREON_MAX_BODY_BYTES = 128 * 1024
PATREON_MAX_TIER_BYTES = 256
PATREON_MAX_TAGS = 20
PATREON_MAX_TAG_BYTES = 128
PATREON_MAX_ATTESTER_BYTES = 256
PATREON_PUBLIC_PREVIEW_ATTESTATION = (
    "I attest that the designated public preview contains no nudity or explicit "
    "sexual content and is safe for Patreon public surfaces."
)

_ARCHIVE_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
_ARCHIVE_MODE = stat.S_IFREG | 0o644
_FORMAT_EXTENSIONS: dict[str, frozenset[str]] = {
    "JPEG": frozenset({".jpg", ".jpeg"}),
    "PNG": frozenset({".png"}),
    "WEBP": frozenset({".webp"}),
}
_ALLOWED_IMAGE_INFO_KEYS: dict[str, frozenset[str]] = {
    "JPEG": frozenset({"jfif", "jfif_version", "jfif_unit", "jfif_density"}),
    "PNG": frozenset(),
    # Pillow exposes these frame-control defaults for a static WebP.
    "WEBP": frozenset({"background", "duration", "loop", "timestamp"}),
}

PUBLICATION_CHECKLIST: tuple[str, ...] = (
    "Verify the package SHA-256 before opening or uploading any file.",
    "Confirm every content image came from a verified DERIVATIVE asset with approved lineage.",
    "Confirm the title, body, tier, tags, and schedule against manifest.json.",
    "Visually re-check public-preview/ and confirm it contains no nudity or explicit content.",
    "Upload only content/ images as the member-facing paid post content.",
    "Use only the designated public-preview/ image for any public preview surface.",
    "Confirm the Patreon creator account is classified Adult/18+ and required "
    "verification is complete.",
    "Set the intended audience/tier and schedule in Patreon's official publishing UI.",
    "Publish or schedule in Patreon's official UI, using the configured signed-in "
    "browser driver when available or the manual package fallback.",
    "Record the resulting Patreon post ID and URL for read-API/webhook reconciliation.",
)


class PatreonHandoffError(ValueError):
    """Base error for a rejected Patreon handoff package input."""


class PatreonPreviewAttestationError(PatreonHandoffError):
    """The designated public preview lacks a complete human safety attestation."""


class PatreonImageValidationError(PatreonHandoffError):
    """An image is malformed, unsafe, mislabeled, or contains metadata."""


@dataclass(frozen=True, slots=True)
class PatreonPackageImage:
    """Caller-designated derivative bytes and their untrusted source filename.

    Opaque image bytes do not prove their asset kind or lineage. Durable
    orchestration must verify those facts before constructing this value.
    """

    filename: str
    data: bytes


@dataclass(frozen=True, slots=True)
class PublicPreviewSafetyAttestation:
    """A recorded human decision that the designated preview is public-safe."""

    safe_for_public: bool
    attested_by: str
    attested_at: datetime


@dataclass(frozen=True, slots=True)
class PatreonHandoffPackage:
    """Immutable, deterministic output of the Patreon handoff builder."""

    archive_bytes: bytes
    sha256: str
    manifest_bytes: bytes
    manifest_sha256: str
    ordered_filenames: tuple[str, ...]
    publication_checklist: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PatreonSetImageRecord:
    """Verified immutable image identity used by the release-wide manifest."""

    ordinal: int
    sha256: str
    byte_size: int
    width: int
    height: int
    image_format: str
    content_type: str


@dataclass(frozen=True, slots=True)
class PatreonHandoffPackagePart:
    """One deterministic ZIP in a release-wide multipart handoff."""

    archive_bytes: bytes
    sha256: str
    set_manifest_bytes: bytes
    set_manifest_sha256: str
    part_manifest_bytes: bytes
    part_manifest_sha256: str
    ordered_filenames: tuple[str, ...]
    publication_checklist: tuple[str, ...]
    part_number: int
    part_count: int
    first_ordinal: int
    last_ordinal: int


@dataclass(frozen=True, slots=True)
class _PackagedImage:
    archive_path: str
    data: bytes
    verified: VerifiedImage

    def manifest_record(self) -> dict[str, str | int]:
        return {
            "path": self.archive_path,
            "sha256": self.verified.sha256,
            "byte_size": self.verified.byte_size,
            "width": self.verified.width,
            "height": self.verified.height,
            "format": self.verified.image_format,
            "content_type": self.verified.content_type,
        }


def _bounded_text(
    value: str,
    *,
    label: str,
    max_bytes: int,
    allow_empty: bool,
    allow_newlines: bool,
) -> str:
    if not isinstance(value, str):
        raise PatreonHandoffError(f"{label} must be a string")
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    if not allow_empty and not normalized.strip():
        raise PatreonHandoffError(f"{label} must not be empty")
    for character in normalized:
        codepoint = ord(character)
        if codepoint == 0 or codepoint == 127 or (codepoint < 32 and character not in "\n\t"):
            raise PatreonHandoffError(f"{label} contains a prohibited control character")
        if not allow_newlines and character in "\n\t":
            raise PatreonHandoffError(f"{label} must be a single line")
    try:
        encoded = normalized.encode("utf-8")
    except UnicodeEncodeError as error:
        raise PatreonHandoffError(f"{label} must be valid UTF-8 text") from error
    if len(encoded) > max_bytes:
        raise PatreonHandoffError(f"{label} exceeds its {max_bytes}-byte limit")
    return normalized


def _canonical_datetime(value: datetime, label: str) -> str:
    if not isinstance(value, datetime):
        raise PatreonHandoffError(f"{label} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise PatreonHandoffError(f"{label} must include a timezone")
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _canonical_tags(tags: Sequence[str]) -> tuple[str, ...]:
    if isinstance(tags, (str, bytes)):
        raise PatreonHandoffError("tags must be a sequence of strings")
    values = tuple(tags)
    if len(values) > PATREON_MAX_TAGS:
        raise PatreonHandoffError(f"tags must not contain more than {PATREON_MAX_TAGS} entries")
    normalized: dict[str, str] = {}
    for index, value in enumerate(values):
        tag = _bounded_text(
            value.strip() if isinstance(value, str) else value,
            label=f"tags[{index}]",
            max_bytes=PATREON_MAX_TAG_BYTES,
            allow_empty=False,
            allow_newlines=False,
        )
        folded = tag.casefold()
        if folded in normalized:
            raise PatreonHandoffError("tags must be unique, ignoring case")
        normalized[folded] = tag
    return tuple(sorted(normalized.values(), key=lambda tag: (tag.casefold(), tag)))


def _source_basename(filename: str, *, label: str) -> str:
    normalized = _bounded_text(
        filename,
        label=label,
        max_bytes=4_096,
        allow_empty=False,
        allow_newlines=False,
    ).replace("\\", "/")
    basename = normalized.rsplit("/", 1)[-1].strip()
    if basename in {"", ".", ".."}:
        raise PatreonImageValidationError(f"{label} has no safe basename")
    return basename


def _expected_format_from_filename(filename: str, *, label: str) -> str:
    basename = _source_basename(filename, label=label)
    stem, separator, suffix = basename.rpartition(".")
    if not separator or not stem:
        raise PatreonImageValidationError(f"{label} must include a supported image extension")
    extension = f".{suffix.casefold()}"
    expected_format = next(
        (
            image_format
            for image_format, extensions in _FORMAT_EXTENSIONS.items()
            if extension in extensions
        ),
        None,
    )
    if expected_format is None:
        raise PatreonImageValidationError(f"{label} has an unsupported image extension")
    return expected_format


def _verify_metadata_absence(data: bytes, verified: VerifiedImage, label: str) -> None:
    allowed_keys = _ALLOWED_IMAGE_INFO_KEYS[verified.image_format]
    try:
        with Image.open(BytesIO(data)) as image:
            image.load()
            metadata_keys = {str(key) for key in image.info}
            unexpected_keys = metadata_keys - allowed_keys
            if unexpected_keys:
                keys = ", ".join(sorted(unexpected_keys))
                raise PatreonImageValidationError(
                    f"{label} contains prohibited image metadata keys: {keys}"
                )
            if image.getexif():
                raise PatreonImageValidationError(f"{label} contains prohibited EXIF metadata")
            text_metadata = getattr(image, "text", None)
            if isinstance(text_metadata, dict) and text_metadata:
                raise PatreonImageValidationError(f"{label} contains prohibited text metadata")
    except PatreonImageValidationError:
        raise
    except (MemoryError, OSError, SyntaxError) as error:
        raise PatreonImageValidationError(
            f"{label} could not be safely decoded for metadata verification"
        ) from error


def _package_image(
    image: PatreonPackageImage,
    *,
    archive_path_prefix: str,
    ordinal: int | None,
    label: str,
) -> _PackagedImage:
    if not isinstance(image, PatreonPackageImage):
        raise PatreonImageValidationError(f"{label} must be a PatreonPackageImage")
    if not isinstance(image.data, bytes):
        raise PatreonImageValidationError(f"{label} data must be immutable bytes")
    if not image.data:
        raise PatreonImageValidationError(f"{label} must not be empty")
    if len(image.data) > PATREON_MAX_IMAGE_BYTES:
        raise PatreonImageValidationError(
            f"{label} exceeds the {PATREON_MAX_IMAGE_BYTES}-byte image limit"
        )

    expected_format = _expected_format_from_filename(
        image.filename,
        label=f"{label} filename",
    )
    try:
        verified = verify_image_bytes(image.data)
    except ImageVerificationError as error:
        raise PatreonImageValidationError(f"{label} is not a safe static image") from error
    if verified.image_format != expected_format:
        raise PatreonImageValidationError(
            f"{label} filename extension does not match its decoded format"
        )
    _verify_metadata_absence(image.data, verified, label)

    if ordinal is None:
        archive_path = f"{archive_path_prefix}/preview.{verified.extension}"
    else:
        archive_path = f"{archive_path_prefix}/{ordinal:03d}.{verified.extension}"
    return _PackagedImage(
        archive_path=archive_path,
        data=image.data,
        verified=verified,
    )


def _check_image_collection_bounds(
    derivatives: tuple[PatreonPackageImage, ...],
    public_preview: PatreonPackageImage,
) -> None:
    if not derivatives:
        raise PatreonHandoffError("at least one approved derivative image is required")
    if len(derivatives) > PATREON_MAX_DERIVATIVE_IMAGES:
        raise PatreonHandoffError(
            f"approved derivatives must not exceed {PATREON_MAX_DERIVATIVE_IMAGES} images"
        )
    all_images = (*derivatives, public_preview)
    for index, image in enumerate(all_images):
        if not isinstance(image, PatreonPackageImage):
            raise PatreonImageValidationError(f"image input {index} must be a PatreonPackageImage")
        if not isinstance(image.data, bytes):
            raise PatreonImageValidationError(f"image input {index} must contain bytes")
        if len(image.data) > PATREON_MAX_IMAGE_BYTES:
            raise PatreonImageValidationError(
                f"image input {index} exceeds the {PATREON_MAX_IMAGE_BYTES}-byte image limit"
            )
    total_bytes = sum(len(image.data) for image in all_images)
    if total_bytes > PATREON_MAX_TOTAL_IMAGE_BYTES:
        raise PatreonImageValidationError(
            "combined derivative and public-preview images exceed the "
            f"{PATREON_MAX_TOTAL_IMAGE_BYTES}-byte package limit"
        )


def _publication_checklist_bytes() -> bytes:
    lines = ["# Patreon publication checklist", ""]
    lines.extend(f"- [ ] {item}" for item in PUBLICATION_CHECKLIST)
    return ("\n".join(lines) + "\n").encode("utf-8")


def _post_document_bytes(
    *,
    title: str,
    body: str,
    tier: str,
    tags: tuple[str, ...],
    scheduled_at: str | None,
) -> bytes:
    tag_text = ", ".join(tags) if tags else "(none)"
    schedule_text = scheduled_at or "Publish immediately"
    return (
        "PATREON POST HANDOFF\n"
        "\n"
        "TITLE\n"
        f"{title}\n"
        "\n"
        "TIER / AUDIENCE\n"
        f"{tier}\n"
        "\n"
        "TAGS\n"
        f"{tag_text}\n"
        "\n"
        "SCHEDULE\n"
        f"{schedule_text}\n"
        "\n"
        "BODY\n"
        f"{body}\n"
    ).encode()


def _file_record(path: str, data: bytes, role: str) -> dict[str, str | int]:
    return {
        "path": path,
        "role": role,
        "sha256": hashlib.sha256(data).hexdigest(),
        "byte_size": len(data),
    }


def _zip_info(path: str) -> ZipInfo:
    info = ZipInfo(path, date_time=_ARCHIVE_TIMESTAMP)
    info.compress_type = ZIP_STORED
    info.create_system = 3
    info.external_attr = _ARCHIVE_MODE << 16
    info.extra = b""
    info.comment = b""
    return info


def _build_archive(entries: Sequence[tuple[str, bytes]]) -> bytes:
    output = BytesIO()
    with ZipFile(output, mode="w", compression=ZIP_STORED, strict_timestamps=True) as archive:
        archive.comment = b""
        for path, data in entries:
            archive.writestr(_zip_info(path), data)
    return output.getvalue()


def _set_image_manifest_record(
    record: PatreonSetImageRecord,
    *,
    preview: bool,
) -> dict[str, str | int]:
    label = "public preview" if preview else f"approved derivative {record.ordinal}"
    if not isinstance(record, PatreonSetImageRecord):
        raise PatreonHandoffError(f"{label} manifest record is invalid")
    if (
        isinstance(record.ordinal, bool)
        or not isinstance(record.ordinal, int)
        or record.ordinal < (0 if preview else 1)
        or (preview and record.ordinal != 0)
        or not isinstance(record.sha256, str)
        or len(record.sha256) != 64
        or any(character not in "0123456789abcdef" for character in record.sha256)
        or isinstance(record.byte_size, bool)
        or not 1 <= record.byte_size <= PATREON_MAX_IMAGE_BYTES
        or isinstance(record.width, bool)
        or not isinstance(record.width, int)
        or record.width <= 0
        or isinstance(record.height, bool)
        or not isinstance(record.height, int)
        or record.height <= 0
        or record.image_format not in _FORMAT_EXTENSIONS
    ):
        raise PatreonHandoffError(f"{label} manifest record is invalid")
    expected_content_type = {
        "JPEG": "image/jpeg",
        "PNG": "image/png",
        "WEBP": "image/webp",
    }[record.image_format]
    if record.content_type != expected_content_type:
        raise PatreonHandoffError(f"{label} manifest record is invalid")
    extension = {
        "JPEG": "jpg",
        "PNG": "png",
        "WEBP": "webp",
    }[record.image_format]
    path = (
        f"public-preview/preview.{extension}"
        if preview
        else f"content/{record.ordinal:03d}.{extension}"
    )
    result: dict[str, str | int] = {
        "path": path,
        "sha256": record.sha256,
        "byte_size": record.byte_size,
        "width": record.width,
        "height": record.height,
        "format": record.image_format,
        "content_type": record.content_type,
    }
    if not preview:
        result = {"ordinal": record.ordinal, **result}
    return result


def build_patreon_set_manifest(
    *,
    approved_derivatives: Sequence[PatreonSetImageRecord],
    public_preview: PatreonSetImageRecord,
    title: str,
    body: str,
    tier: str,
    tags: Sequence[str],
    scheduled_at: datetime | None,
    public_preview_attestation: PublicPreviewSafetyAttestation | None,
) -> tuple[bytes, str]:
    """Build the one canonical ordered manifest shared by every archive part."""

    if public_preview_attestation is None or public_preview_attestation.safe_for_public is not True:
        raise PatreonPreviewAttestationError(
            "an affirmative human public-preview safety attestation is required"
        )
    if isinstance(approved_derivatives, (str, bytes)):
        raise PatreonHandoffError("approved_derivatives must be a sequence of records")
    records = tuple(approved_derivatives)
    if not 1 <= len(records) <= MAX_ACCEPTED_IMAGES_PER_RELEASE:
        raise PatreonHandoffError(
            "approved derivatives must contain between 1 and "
            f"{MAX_ACCEPTED_IMAGES_PER_RELEASE} images"
        )
    if tuple(record.ordinal for record in records) != tuple(range(1, len(records) + 1)):
        raise PatreonHandoffError("approved derivative manifest ordinals must be contiguous")

    canonical_title = _bounded_text(
        title,
        label="title",
        max_bytes=PATREON_MAX_TITLE_BYTES,
        allow_empty=False,
        allow_newlines=False,
    )
    canonical_body = _bounded_text(
        body,
        label="body",
        max_bytes=PATREON_MAX_BODY_BYTES,
        allow_empty=True,
        allow_newlines=True,
    )
    canonical_tier = _bounded_text(
        tier,
        label="tier",
        max_bytes=PATREON_MAX_TIER_BYTES,
        allow_empty=False,
        allow_newlines=False,
    )
    canonical_tags = _canonical_tags(tags)
    canonical_schedule = (
        _canonical_datetime(scheduled_at, "scheduled_at") if scheduled_at is not None else None
    )
    attested_by = _bounded_text(
        public_preview_attestation.attested_by,
        label="public preview attester",
        max_bytes=PATREON_MAX_ATTESTER_BYTES,
        allow_empty=False,
        allow_newlines=False,
    )
    attested_at = _canonical_datetime(
        public_preview_attestation.attested_at,
        "public preview attestation time",
    )
    part_count = (len(records) + PATREON_MAX_DERIVATIVE_IMAGES - 1) // (
        PATREON_MAX_DERIVATIVE_IMAGES
    )
    preview_record = _set_image_manifest_record(public_preview, preview=True)
    manifest: dict[str, object] = {
        "schema": PATREON_SET_MANIFEST_SCHEMA,
        "publication_mode": "human_official_ui",
        "reconciliation_mode": "read_api_and_signed_webhooks_only",
        "archive_parts": {
            "count": part_count,
            "max_derivatives_per_part": PATREON_MAX_DERIVATIVE_IMAGES,
        },
        "post": {
            "title": canonical_title,
            "body": canonical_body,
            "tier": canonical_tier,
            "tags": list(canonical_tags),
            "scheduled_at": canonical_schedule,
        },
        "public_preview": {
            **preview_record,
            "human_safety_attestation": {
                "safe_for_public": True,
                "attested_by": attested_by,
                "attested_at": attested_at,
                "statement": PATREON_PUBLIC_PREVIEW_ATTESTATION,
            },
        },
        "approved_derivatives": [
            _set_image_manifest_record(record, preview=False) for record in records
        ],
        "publication_checklist": list(PUBLICATION_CHECKLIST),
    }
    manifest_bytes = canonical_json_bytes(manifest)
    return manifest_bytes, hashlib.sha256(manifest_bytes).hexdigest()


def build_patreon_handoff_package_part(
    *,
    approved_derivatives: Sequence[PatreonPackageImage],
    public_preview: PatreonPackageImage,
    set_manifest_bytes: bytes,
    part_number: int,
) -> PatreonHandoffPackagePart:
    """Build one bounded archive whose content is anchored to the set manifest."""

    if not isinstance(set_manifest_bytes, bytes):
        raise PatreonHandoffError("set manifest must be immutable bytes")
    try:
        set_manifest = json.loads(set_manifest_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PatreonHandoffError("set manifest is invalid") from error
    if (
        not isinstance(set_manifest, dict)
        or set_manifest.get("schema") != PATREON_SET_MANIFEST_SCHEMA
        or canonical_json_bytes(set_manifest) != set_manifest_bytes
    ):
        raise PatreonHandoffError("set manifest is invalid")
    raw_parts = set_manifest.get("archive_parts")
    raw_records = set_manifest.get("approved_derivatives")
    raw_preview = set_manifest.get("public_preview")
    post = set_manifest.get("post")
    if (
        not isinstance(raw_parts, dict)
        or not isinstance(raw_records, list)
        or not isinstance(raw_preview, dict)
        or not isinstance(post, dict)
    ):
        raise PatreonHandoffError("set manifest is invalid")
    part_count = raw_parts.get("count")
    if (
        isinstance(part_number, bool)
        or not isinstance(part_number, int)
        or isinstance(part_count, bool)
        or not isinstance(part_count, int)
        or not 1 <= part_number <= part_count
    ):
        raise PatreonHandoffError("archive part identity is invalid")
    first_ordinal = (part_number - 1) * PATREON_MAX_DERIVATIVE_IMAGES + 1
    expected_records = raw_records[
        first_ordinal - 1 : first_ordinal - 1 + PATREON_MAX_DERIVATIVE_IMAGES
    ]
    derivatives = tuple(approved_derivatives)
    if not derivatives or len(derivatives) != len(expected_records):
        raise PatreonHandoffError("archive part image count does not match the set manifest")
    packaged_derivatives = tuple(
        _package_image(
            image,
            archive_path_prefix="content",
            ordinal=first_ordinal + index,
            label=f"approved derivative {first_ordinal + index}",
        )
        for index, image in enumerate(derivatives)
    )
    packaged_preview = _package_image(
        public_preview,
        archive_path_prefix="public-preview",
        ordinal=None,
        label="public preview",
    )
    actual_records = [
        {"ordinal": first_ordinal + index, **image.manifest_record()}
        for index, image in enumerate(packaged_derivatives)
    ]
    preview_record = packaged_preview.manifest_record()
    expected_preview_record = {
        key: value for key, value in raw_preview.items() if key != "human_safety_attestation"
    }
    if actual_records != expected_records or preview_record != expected_preview_record:
        raise PatreonImageValidationError(
            "archive part bytes do not match the release-wide manifest"
        )
    total_image_bytes = sum(len(image.data) for image in packaged_derivatives) + len(
        packaged_preview.data
    )
    if total_image_bytes > PATREON_MAX_TOTAL_IMAGE_BYTES:
        raise PatreonImageValidationError(
            "archive part images exceed the aggregate image-byte limit"
        )

    title = post.get("title")
    body = post.get("body")
    tier = post.get("tier")
    tags = post.get("tags")
    scheduled_at = post.get("scheduled_at")
    if (
        not isinstance(title, str)
        or not isinstance(body, str)
        or not isinstance(tier, str)
        or not isinstance(tags, list)
        or not all(isinstance(tag, str) for tag in tags)
        or (scheduled_at is not None and not isinstance(scheduled_at, str))
    ):
        raise PatreonHandoffError("set manifest post metadata is invalid")
    checklist_bytes = _publication_checklist_bytes()
    post_document_bytes = _post_document_bytes(
        title=title,
        body=body,
        tier=tier,
        tags=tuple(tags),
        scheduled_at=scheduled_at,
    )
    set_manifest_sha256 = hashlib.sha256(set_manifest_bytes).hexdigest()
    last_ordinal = first_ordinal + len(packaged_derivatives) - 1
    part_manifest: dict[str, object] = {
        "schema": PATREON_PART_MANIFEST_SCHEMA,
        "set_manifest_sha256": set_manifest_sha256,
        "part_number": part_number,
        "part_count": part_count,
        "first_ordinal": first_ordinal,
        "last_ordinal": last_ordinal,
        "approved_derivatives": actual_records,
        "public_preview": preview_record,
    }
    part_manifest_bytes = canonical_json_bytes(part_manifest)
    entries: tuple[tuple[str, bytes], ...] = (
        ("set-manifest.json", set_manifest_bytes),
        ("part-manifest.json", part_manifest_bytes),
        ("PUBLICATION_CHECKLIST.md", checklist_bytes),
        ("POST.txt", post_document_bytes),
        (packaged_preview.archive_path, packaged_preview.data),
        *((image.archive_path, image.data) for image in packaged_derivatives),
    )
    archive_bytes = _build_archive(entries)
    if len(archive_bytes) > PATREON_MAX_ARCHIVE_BYTES:
        raise PatreonHandoffError(
            f"Patreon handoff archive exceeds the {PATREON_MAX_ARCHIVE_BYTES}-byte limit"
        )
    return PatreonHandoffPackagePart(
        archive_bytes=archive_bytes,
        sha256=hashlib.sha256(archive_bytes).hexdigest(),
        set_manifest_bytes=set_manifest_bytes,
        set_manifest_sha256=set_manifest_sha256,
        part_manifest_bytes=part_manifest_bytes,
        part_manifest_sha256=hashlib.sha256(part_manifest_bytes).hexdigest(),
        ordered_filenames=tuple(path for path, _data in entries),
        publication_checklist=PUBLICATION_CHECKLIST,
        part_number=part_number,
        part_count=part_count,
        first_ordinal=first_ordinal,
        last_ordinal=last_ordinal,
    )


def build_patreon_handoff_package(
    *,
    approved_derivatives: Sequence[PatreonPackageImage],
    public_preview: PatreonPackageImage,
    title: str,
    body: str,
    tier: str,
    tags: Sequence[str],
    scheduled_at: datetime | None,
    public_preview_attestation: PublicPreviewSafetyAttestation | None,
) -> PatreonHandoffPackage:
    """Build a deterministic human-publishing package from approved derivatives.

    This pure function cannot prove that opaque bytes are derivatives rather than
    raw masters. Durable orchestration must verify DERIVATIVE asset kind, approved
    lineage, and metadata safety before calling it. The signature has no prompt,
    provider credential, storage key, or environment/configuration input.

    This function performs synchronous Pillow decoding and ZIP assembly. Run it in
    the project's bounded CPU worker, not on the API event loop.
    """
    if public_preview_attestation is None or public_preview_attestation.safe_for_public is not True:
        raise PatreonPreviewAttestationError(
            "an affirmative human public-preview safety attestation is required"
        )
    attested_by = _bounded_text(
        public_preview_attestation.attested_by,
        label="public preview attester",
        max_bytes=PATREON_MAX_ATTESTER_BYTES,
        allow_empty=False,
        allow_newlines=False,
    )
    attested_at = _canonical_datetime(
        public_preview_attestation.attested_at,
        "public preview attestation time",
    )

    canonical_title = _bounded_text(
        title,
        label="title",
        max_bytes=PATREON_MAX_TITLE_BYTES,
        allow_empty=False,
        allow_newlines=False,
    )
    canonical_body = _bounded_text(
        body,
        label="body",
        max_bytes=PATREON_MAX_BODY_BYTES,
        allow_empty=True,
        allow_newlines=True,
    )
    canonical_tier = _bounded_text(
        tier,
        label="tier",
        max_bytes=PATREON_MAX_TIER_BYTES,
        allow_empty=False,
        allow_newlines=False,
    )
    canonical_tags = _canonical_tags(tags)
    canonical_schedule = (
        _canonical_datetime(scheduled_at, "scheduled_at") if scheduled_at is not None else None
    )

    if isinstance(approved_derivatives, (str, bytes)):
        raise PatreonHandoffError("approved_derivatives must be a sequence of images")
    derivatives = tuple(approved_derivatives)
    _check_image_collection_bounds(derivatives, public_preview)

    packaged_derivatives = tuple(
        _package_image(
            image,
            archive_path_prefix="content",
            ordinal=index,
            label=f"approved derivative {index}",
        )
        for index, image in enumerate(derivatives, start=1)
    )
    packaged_preview = _package_image(
        public_preview,
        archive_path_prefix="public-preview",
        ordinal=None,
        label="public preview",
    )

    checklist_bytes = _publication_checklist_bytes()
    post_document_bytes = _post_document_bytes(
        title=canonical_title,
        body=canonical_body,
        tier=canonical_tier,
        tags=canonical_tags,
        scheduled_at=canonical_schedule,
    )
    manifest: dict[str, object] = {
        "schema": PATREON_HANDOFF_SCHEMA,
        "publication_mode": "human_official_ui",
        "reconciliation_mode": "read_api_and_signed_webhooks_only",
        "post": {
            "title": canonical_title,
            "body": canonical_body,
            "tier": canonical_tier,
            "tags": list(canonical_tags),
            "scheduled_at": canonical_schedule,
        },
        "public_preview": {
            **packaged_preview.manifest_record(),
            "human_safety_attestation": {
                "safe_for_public": True,
                "attested_by": attested_by,
                "attested_at": attested_at,
                "statement": PATREON_PUBLIC_PREVIEW_ATTESTATION,
            },
        },
        "approved_derivatives": [
            {
                "ordinal": index,
                **image.manifest_record(),
            }
            for index, image in enumerate(packaged_derivatives, start=1)
        ],
        "supporting_files": [
            _file_record(
                "PUBLICATION_CHECKLIST.md",
                checklist_bytes,
                "publication_checklist",
            ),
            _file_record("POST.txt", post_document_bytes, "human_post_copy"),
        ],
        "publication_checklist": list(PUBLICATION_CHECKLIST),
    }
    manifest_bytes = canonical_json_bytes(manifest)
    entries: tuple[tuple[str, bytes], ...] = (
        ("manifest.json", manifest_bytes),
        ("PUBLICATION_CHECKLIST.md", checklist_bytes),
        ("POST.txt", post_document_bytes),
        (packaged_preview.archive_path, packaged_preview.data),
        *((image.archive_path, image.data) for image in packaged_derivatives),
    )
    archive_bytes = _build_archive(entries)
    if len(archive_bytes) > PATREON_MAX_ARCHIVE_BYTES:
        raise PatreonHandoffError(
            f"Patreon handoff archive exceeds the {PATREON_MAX_ARCHIVE_BYTES}-byte limit"
        )
    return PatreonHandoffPackage(
        archive_bytes=archive_bytes,
        sha256=hashlib.sha256(archive_bytes).hexdigest(),
        manifest_bytes=manifest_bytes,
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        ordered_filenames=tuple(path for path, _data in entries),
        publication_checklist=PUBLICATION_CHECKLIST,
    )
