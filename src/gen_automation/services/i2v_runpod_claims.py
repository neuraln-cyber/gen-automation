"""Short-lived exact-attempt tokens for the RunPod worker claim callback."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from gen_automation.domain.canonical import canonical_sha256

_TOKEN_SCHEMA = "i2v-runpod-claim/v1"  # noqa: S105 - schema identifier, not a secret


class I2VRunPodClaimError(Exception):
    """A redacted claim-token failure."""


@dataclass(frozen=True, slots=True)
class I2VRunPodClaimIdentity:
    job_id: UUID
    attempt_id: UUID
    request_sha256: str
    submission_key: str
    expires_at: datetime


def i2v_runpod_submission_key(
    *,
    job_id: UUID,
    attempt_id: UUID,
    attempt_no: int,
    request_sha256: str,
) -> str:
    return canonical_sha256(
        {
            "schema": "i2v-runpod-submission-key/v1",
            "job_id": str(job_id),
            "attempt_id": str(attempt_id),
            "attempt_no": attempt_no,
            "request_sha256": request_sha256,
        }
    )


def create_i2v_runpod_claim_token(
    *,
    secret: str,
    identity: I2VRunPodClaimIdentity,
) -> str:
    key = _secret_bytes(secret)
    expires_at = _as_utc(identity.expires_at)
    payload = {
        "schema": _TOKEN_SCHEMA,
        "job_id": str(identity.job_id),
        "attempt_id": str(identity.attempt_id),
        "request_sha256": _sha(identity.request_sha256),
        "submission_key": _sha(identity.submission_key),
        "expires_at": int(expires_at.timestamp()),
    }
    encoded = _b64(
        json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode()
    )
    signature = _b64(hmac.new(key, encoded.encode(), hashlib.sha256).digest())
    return f"{encoded}.{signature}"


def verify_i2v_runpod_claim_token(
    token: str,
    *,
    secret: str,
    now: datetime | None = None,
) -> I2VRunPodClaimIdentity:
    key = _secret_bytes(secret)
    try:
        encoded, supplied = token.split(".", 1)
        expected = _b64(hmac.new(key, encoded.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(supplied, expected):
            raise ValueError
        raw = json.loads(_unb64(encoded))
        if not isinstance(raw, dict) or raw.get("schema") != _TOKEN_SCHEMA:
            raise ValueError
        identity = I2VRunPodClaimIdentity(
            job_id=UUID(str(raw["job_id"])),
            attempt_id=UUID(str(raw["attempt_id"])),
            request_sha256=_sha(raw["request_sha256"]),
            submission_key=_sha(raw["submission_key"]),
            expires_at=datetime.fromtimestamp(int(raw["expires_at"]), tz=UTC),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise I2VRunPodClaimError("invalid RunPod worker claim") from None
    if identity.expires_at <= _as_utc(now or datetime.now(UTC)):
        raise I2VRunPodClaimError("expired RunPod worker claim")
    return identity


def _secret_bytes(value: str) -> bytes:
    encoded = value.encode()
    if len(encoded) < 20:
        raise I2VRunPodClaimError("invalid RunPod worker claim secret")
    return encoded


def _sha(value: object) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError
    try:
        int(value, 16)
    except ValueError:
        raise ValueError from None
    if value != value.lower():
        raise ValueError
    return value


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + ("=" * (-len(value) % 4)))


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise I2VRunPodClaimError("invalid RunPod worker claim expiry")
    return value.astimezone(UTC)
