import json
import math
from collections.abc import AsyncIterable, Mapping
from dataclasses import dataclass
from typing import Protocol

DEFAULT_MAX_WEBHOOK_BODY_BYTES = 256 * 1024
MAX_WEBHOOK_BODY_BYTES = 1024 * 1024
MAX_JSON_DEPTH = 32

_REQUIRED_HEADERS = (
    "webhook-id",
    "webhook-timestamp",
    "webhook-signature",
)
_HEADER_LENGTH_LIMITS = {
    "webhook-id": 512,
    "webhook-timestamp": 32,
    "webhook-signature": 8192,
}

type JsonValue = bool | int | float | str | list[JsonValue] | dict[str, JsonValue] | None
type JsonObject = dict[str, JsonValue]


class SaladWebhookError(Exception):
    """Base error for a rejected Salad webhook."""


class SaladWebhookBodyTooLargeError(SaladWebhookError):
    pass


class SaladWebhookHeaderError(SaladWebhookError):
    pass


class SaladWebhookSignatureError(SaladWebhookError):
    pass


class SaladWebhookPayloadError(SaladWebhookError):
    pass


class WebhookSignatureVerifier(Protocol):
    def verify(self, data: bytes | str, headers: dict[str, str]) -> object: ...


@dataclass(frozen=True)
class VerifiedSaladWebhook:
    webhook_id: str
    webhook_timestamp: int
    payload: JsonObject


def _build_svix_verifier(secret: str | bytes) -> WebhookSignatureVerifier:
    try:
        from svix.webhooks import Webhook
    except ImportError:
        raise RuntimeError("the svix package is required for Salad webhook verification") from None
    return Webhook(secret)


def _extract_required_headers(headers: Mapping[str, str]) -> dict[str, str]:
    selected: dict[str, str] = {}
    for raw_name, value in headers.items():
        if not isinstance(raw_name, str) or not isinstance(value, str):
            raise SaladWebhookHeaderError("webhook headers must be strings")

        name = raw_name.lower()
        if name not in _REQUIRED_HEADERS:
            continue
        if name in selected:
            raise SaladWebhookHeaderError("duplicate webhook header")
        if not value or len(value) > _HEADER_LENGTH_LIMITS[name]:
            raise SaladWebhookHeaderError("invalid webhook header")
        selected[name] = value

    if any(name not in selected for name in _REQUIRED_HEADERS):
        raise SaladWebhookHeaderError("missing required webhook header")
    return selected


def _validated_json_value(value: object, *, depth: int) -> JsonValue:
    if depth > MAX_JSON_DEPTH:
        raise SaladWebhookPayloadError("webhook JSON exceeds the nesting limit")
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise SaladWebhookPayloadError("webhook JSON contains a non-finite number")
        return value
    if isinstance(value, list):
        return [_validated_json_value(item, depth=depth + 1) for item in value]
    if isinstance(value, dict):
        result: JsonObject = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise SaladWebhookPayloadError("webhook JSON object keys must be strings")
            result[key] = _validated_json_value(item, depth=depth + 1)
        return result
    raise SaladWebhookPayloadError("webhook payload is not valid JSON")


def _decode_verified_payload(value: object) -> JsonObject:
    if isinstance(value, (bytes, str)):
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
            raise SaladWebhookPayloadError("webhook payload is not valid JSON") from None

    validated = _validated_json_value(value, depth=0)
    if not isinstance(validated, dict):
        raise SaladWebhookPayloadError("webhook payload must be a JSON object")
    return validated


class SaladWebhookVerifier:
    def __init__(
        self,
        secret: str | bytes,
        *,
        max_body_bytes: int = DEFAULT_MAX_WEBHOOK_BODY_BYTES,
        signature_verifier: WebhookSignatureVerifier | None = None,
    ) -> None:
        if not secret:
            raise ValueError("webhook secret must not be empty")
        if not 1 <= max_body_bytes <= MAX_WEBHOOK_BODY_BYTES:
            raise ValueError(f"max_body_bytes must be between 1 and {MAX_WEBHOOK_BODY_BYTES}")

        self._max_body_bytes = max_body_bytes
        self._signature_verifier = signature_verifier or _build_svix_verifier(secret)

    def verify(
        self,
        *,
        body: bytes,
        headers: Mapping[str, str],
    ) -> VerifiedSaladWebhook:
        if not isinstance(body, bytes):
            raise TypeError("webhook body must be bytes")
        if len(body) > self._max_body_bytes:
            raise SaladWebhookBodyTooLargeError("webhook body exceeds the size limit")

        signed_headers = _extract_required_headers(headers)
        try:
            verified_payload = self._signature_verifier.verify(body, signed_headers)
        except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
            raise SaladWebhookPayloadError("webhook payload is not valid JSON") from None
        except Exception:
            # Svix raises for missing/invalid/stale signatures. Do not expose its
            # error details because headers contain authentication material.
            raise SaladWebhookSignatureError("webhook signature verification failed") from None

        try:
            timestamp = int(signed_headers["webhook-timestamp"])
        except (ValueError, OverflowError):
            raise SaladWebhookHeaderError("invalid webhook timestamp") from None

        return VerifiedSaladWebhook(
            webhook_id=signed_headers["webhook-id"],
            webhook_timestamp=timestamp,
            payload=_decode_verified_payload(verified_payload),
        )

    async def verify_stream(
        self,
        *,
        chunks: AsyncIterable[bytes],
        headers: Mapping[str, str],
    ) -> VerifiedSaladWebhook:
        # Reject malformed requests before reading their bodies. The headers are
        # checked again by verify() immediately before cryptographic verification.
        _extract_required_headers(headers)

        parts: list[bytes] = []
        total = 0
        async for chunk in chunks:
            if not isinstance(chunk, bytes):
                raise TypeError("webhook body chunks must be bytes")
            total += len(chunk)
            if total > self._max_body_bytes:
                raise SaladWebhookBodyTooLargeError("webhook body exceeds the size limit")
            parts.append(chunk)

        return self.verify(body=b"".join(parts), headers=headers)
