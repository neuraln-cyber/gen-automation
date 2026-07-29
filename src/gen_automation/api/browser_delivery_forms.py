"""Bounded browser-form contracts for the completed-set delivery dashboard."""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import parse_qs
from uuid import UUID

from fastapi import Request, status

from gen_automation.config import Settings

_FORM_CONTENT_TYPE = "application/x-www-form-urlencoded"
_MAX_FORM_BODY_BYTES = 64 * 1024
_FORM_KEY = re.compile(r"web-delivery-[0-9a-f]{64}")
_HEX = frozenset("0123456789abcdefABCDEF")

PREPARE_OUTPUT_FIELDS = frozenset(
    {
        "csrf_token",
        "idempotency_key",
        "submission_id",
        "watermark_asset_id",
    }
)
PREPARE_DESTINATION_FIELDS = frozenset(
    {
        "csrf_token",
        "idempotency_key",
        "submission_id",
        "patreon_title",
        "patreon_body",
        "patreon_tier",
        "patreon_tags",
        "public_preview_output_id",
        "public_preview_attested_at",
        "public_preview_safe",
        "x_text",
    }
)
PACKAGE_DOWNLOAD_FIELDS = frozenset(
    {
        "csrf_token",
        "expected_intent_digest",
        "expected_lock_version",
    }
)
PATREON_CONFIRM_PRESENT_FIELDS = frozenset(
    {
        "csrf_token",
        "idempotency_key",
        "expected_intent_digest",
        "expected_lock_version",
        "remote_identifier",
        "remote_url",
        "evidence",
        "attestation",
    }
)
PATREON_CONFIRM_ABSENT_FIELDS = frozenset(
    {
        "csrf_token",
        "idempotency_key",
        "expected_intent_digest",
        "expected_lock_version",
        "evidence",
        "attestation",
    }
)


class BrowserDeliveryFormError(ValueError):
    """A delivery form did not match its exact, bounded browser contract."""

    def __init__(self, *, status_code: int, message: str = "delivery form is invalid") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


@dataclass(frozen=True, slots=True)
class PrepareOutputForm:
    csrf_token: str
    idempotency_key: str
    submission_id: UUID
    watermark_asset_id: UUID | None


@dataclass(frozen=True, slots=True)
class PrepareDestinationForm:
    csrf_token: str
    idempotency_key: str
    submission_id: UUID
    patreon_title: str
    patreon_body: str
    patreon_tier: str
    patreon_tags: tuple[str, ...]
    public_preview_output_id: UUID
    public_preview_attested_at: datetime
    public_preview_safe: bool
    x_text: str


@dataclass(frozen=True, slots=True)
class PackageDownloadForm:
    csrf_token: str
    expected_intent_digest: str
    expected_lock_version: int


@dataclass(frozen=True, slots=True)
class PatreonConfirmPresentForm:
    csrf_token: str
    idempotency_key: str
    expected_intent_digest: str
    expected_lock_version: int
    remote_identifier: str
    remote_url: str
    evidence: str
    attestation: str


@dataclass(frozen=True, slots=True)
class PatreonConfirmAbsentForm:
    csrf_token: str
    idempotency_key: str
    expected_intent_digest: str
    expected_lock_version: int
    evidence: str
    attestation: str


async def read_prepare_output_form(request: Request) -> PrepareOutputForm:
    values = await _read_form(request, expected_fields=PREPARE_OUTPUT_FIELDS)
    submission_id = _uuid(values["submission_id"])
    raw_watermark = values["watermark_asset_id"]
    watermark_asset_id: UUID | None = None
    if raw_watermark:
        try:
            watermark_asset_id = UUID(raw_watermark)
        except ValueError:
            raise _bad_request() from None
        if str(watermark_asset_id) != raw_watermark.lower():
            raise _bad_request()
    return PrepareOutputForm(
        csrf_token=_bounded_nonempty(values["csrf_token"], maximum=200),
        idempotency_key=_idempotency_key(values["idempotency_key"]),
        submission_id=submission_id,
        watermark_asset_id=watermark_asset_id,
    )


async def read_prepare_destination_form(request: Request) -> PrepareDestinationForm:
    values = await _read_form(request, expected_fields=PREPARE_DESTINATION_FIELDS)
    submission_id = _uuid(values["submission_id"])
    try:
        preview_output_id = UUID(values["public_preview_output_id"])
    except ValueError:
        raise _bad_request() from None
    if str(preview_output_id) != values["public_preview_output_id"].lower():
        raise _bad_request()
    if values["public_preview_safe"] != "true":
        raise BrowserDeliveryFormError(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            message="Confirm that the selected Patreon public preview is safe for public surfaces.",
        )
    tags = _tags(values["patreon_tags"])
    return PrepareDestinationForm(
        csrf_token=_bounded_nonempty(values["csrf_token"], maximum=200),
        idempotency_key=_idempotency_key(values["idempotency_key"]),
        submission_id=submission_id,
        patreon_title=_bounded_text(values["patreon_title"], maximum=500, required=True),
        patreon_body=_bounded_text(values["patreon_body"], maximum=20_000, required=False),
        patreon_tier=_bounded_text(values["patreon_tier"], maximum=500, required=True),
        patreon_tags=tags,
        public_preview_output_id=preview_output_id,
        public_preview_attested_at=_datetime(values["public_preview_attested_at"]),
        public_preview_safe=True,
        x_text=_bounded_text(values["x_text"], maximum=2_000, required=False),
    )


async def read_package_download_form(request: Request) -> PackageDownloadForm:
    values = await _read_form(request, expected_fields=PACKAGE_DOWNLOAD_FIELDS)
    digest = values["expected_intent_digest"]
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise _bad_request()
    return PackageDownloadForm(
        csrf_token=_bounded_nonempty(values["csrf_token"], maximum=200),
        expected_intent_digest=digest,
        expected_lock_version=_positive_int(values["expected_lock_version"]),
    )


async def read_patreon_confirm_present_form(
    request: Request,
) -> PatreonConfirmPresentForm:
    values = await _read_form(request, expected_fields=PATREON_CONFIRM_PRESENT_FIELDS)
    digest, lock_version = _intent_identity(values)
    return PatreonConfirmPresentForm(
        csrf_token=_bounded_nonempty(values["csrf_token"], maximum=200),
        idempotency_key=_idempotency_key(values["idempotency_key"]),
        expected_intent_digest=digest,
        expected_lock_version=lock_version,
        remote_identifier=_bounded_text(
            values["remote_identifier"],
            maximum=20,
            required=True,
        ),
        remote_url=_bounded_text(values["remote_url"], maximum=2_048, required=True),
        evidence=_bounded_text(values["evidence"], maximum=20_000, required=True),
        attestation=_bounded_text(values["attestation"], maximum=500, required=True),
    )


async def read_patreon_confirm_absent_form(
    request: Request,
) -> PatreonConfirmAbsentForm:
    values = await _read_form(request, expected_fields=PATREON_CONFIRM_ABSENT_FIELDS)
    digest, lock_version = _intent_identity(values)
    return PatreonConfirmAbsentForm(
        csrf_token=_bounded_nonempty(values["csrf_token"], maximum=200),
        idempotency_key=_idempotency_key(values["idempotency_key"]),
        expected_intent_digest=digest,
        expected_lock_version=lock_version,
        evidence=_bounded_text(values["evidence"], maximum=20_000, required=True),
        attestation=_bounded_text(values["attestation"], maximum=500, required=True),
    )


def delivery_csrf_token(settings: Settings, *, session_id: UUID) -> str:
    return _signed_value(settings, session_id=session_id, action="csrf", parts=())


def delivery_form_key(
    settings: Settings,
    *,
    session_id: UUID,
    action: str,
    parts: tuple[str, ...],
) -> str:
    return _signed_value(settings, session_id=session_id, action=action, parts=parts)


def form_key_matches(supplied: str, expected: str) -> bool:
    return hmac.compare_digest(supplied, expected)


def _signed_value(
    settings: Settings,
    *,
    session_id: UUID,
    action: str,
    parts: tuple[str, ...],
) -> str:
    context = "\x1f".join(("gen-automation-browser-delivery-v1", str(session_id), action, *parts))
    digest = hmac.new(
        settings.session_secret.get_secret_value().encode(),
        context.encode(),
        hashlib.sha256,
    ).hexdigest()
    return f"web-delivery-{digest}"


async def _read_form(
    request: Request,
    *,
    expected_fields: frozenset[str],
) -> dict[str, str]:
    content_type = request.headers.get("content-type", "")
    if content_type.partition(";")[0].strip().lower() != _FORM_CONTENT_TYPE:
        raise BrowserDeliveryFormError(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
        )
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError:
            raise _bad_request() from None
        if declared_length < 0:
            raise _bad_request()
        if declared_length > _MAX_FORM_BODY_BYTES:
            raise BrowserDeliveryFormError(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            )

    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > _MAX_FORM_BODY_BYTES:
            raise BrowserDeliveryFormError(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            )
        body.extend(chunk)
    try:
        encoded = bytes(body).decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise _bad_request() from None
    if not _valid_percent_encoding(encoded):
        raise _bad_request()
    try:
        parsed = parse_qs(
            encoded,
            keep_blank_values=True,
            strict_parsing=True,
            encoding="utf-8",
            errors="strict",
            max_num_fields=len(expected_fields),
        )
    except (UnicodeDecodeError, ValueError):
        raise _bad_request() from None
    if set(parsed) != expected_fields or any(len(parsed[field]) != 1 for field in expected_fields):
        raise _bad_request()
    return {field: parsed[field][0] for field in expected_fields}


def _tags(value: str) -> tuple[str, ...]:
    if len(value) > 2_000:
        raise _bad_request()
    tags: list[str] = []
    for raw in value.split(","):
        tag = raw.strip()
        if not tag:
            continue
        if len(tag) > 100 or tag.casefold() in {item.casefold() for item in tags}:
            raise _bad_request()
        tags.append(tag)
    if len(tags) > 25:
        raise _bad_request()
    return tuple(tags)


def _intent_identity(values: dict[str, str]) -> tuple[str, int]:
    digest = values["expected_intent_digest"]
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise _bad_request()
    return digest, _positive_int(values["expected_lock_version"])


def _bounded_text(value: str, *, maximum: int, required: bool) -> str:
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    if len(normalized) > maximum or (required and not normalized.strip()):
        raise _bad_request()
    if any(
        ord(character) == 0
        or ord(character) == 127
        or (ord(character) < 32 and character not in "\n\t")
        for character in normalized
    ):
        raise _bad_request()
    return normalized


def _bounded_nonempty(value: str, *, maximum: int) -> str:
    if not 1 <= len(value) <= maximum:
        raise _bad_request()
    return value


def _idempotency_key(value: str) -> str:
    if _FORM_KEY.fullmatch(value) is None:
        raise _bad_request()
    return value


def _positive_int(value: str) -> int:
    if not value or len(value) > 10 or not value.isascii() or not value.isdecimal():
        raise _bad_request()
    parsed = int(value)
    if not 1 <= parsed <= 2_147_483_647 or str(parsed) != value:
        raise _bad_request()
    return parsed


def _uuid(value: str) -> UUID:
    try:
        parsed = UUID(value)
    except ValueError:
        raise _bad_request() from None
    if str(parsed) != value.lower():
        raise _bad_request()
    return parsed


def _datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise _bad_request() from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise _bad_request()
    return parsed.astimezone(UTC)


def _valid_percent_encoding(value: str) -> bool:
    index = 0
    while index < len(value):
        if value[index] != "%":
            index += 1
            continue
        if index + 2 >= len(value) or value[index + 1] not in _HEX or value[index + 2] not in _HEX:
            return False
        index += 3
    return True


def _bad_request() -> BrowserDeliveryFormError:
    return BrowserDeliveryFormError(status_code=status.HTTP_400_BAD_REQUEST)
