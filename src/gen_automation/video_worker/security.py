import json
import math
import time
from collections.abc import Callable

from gen_automation.domain.signing import sign_message, verify_message
from gen_automation.video_worker.models import AnimateEnvelope, WorkerSettings


class AuthorizationError(Exception):
    """Raised when a signed worker envelope is invalid."""


def _reject_non_finite(value: object) -> None:
    if isinstance(value, float):
        if not math.isfinite(value):
            raise AuthorizationError("invalid authorization")
    elif isinstance(value, list):
        for item in value:
            _reject_non_finite(item)
    elif isinstance(value, dict):
        for item in value.values():
            _reject_non_finite(item)


def canonical_signing_payload(envelope: AnimateEnvelope) -> bytes:
    unsigned: dict[str, object] = {
        "expires_at": envelope.expires_at,
        "issued_at": envelope.issued_at,
        "key_id": envelope.key_id,
        # Keep standard video-worker.v1 byte-compatible with the original
        # singleton worker while v2 binds the HQ execution contract.
        "payload": envelope.payload.model_dump(mode="json", exclude_none=True),
        "version": envelope.version,
    }
    _reject_non_finite(unsigned)
    try:
        return json.dumps(
            unsigned,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    except (TypeError, ValueError, RecursionError):
        raise AuthorizationError("invalid authorization") from None


def calculate_signature(envelope: AnimateEnvelope, private_key: str) -> str:
    return sign_message(private_key, canonical_signing_payload(envelope))


def verify_authorization(
    envelope: AnimateEnvelope,
    settings: WorkerSettings,
    *,
    now: Callable[[], float] = time.time,
    allow_expired_for_replay: bool = False,
) -> None:
    public_key = settings.verification_keys.get(envelope.key_id)
    current_time = int(now())
    metadata_is_valid = not (
        public_key is None
        or envelope.expires_at <= envelope.issued_at
        or envelope.expires_at - envelope.issued_at > settings.max_signature_ttl_seconds
        or envelope.issued_at > current_time + settings.clock_skew_seconds
        or (
            not allow_expired_for_replay
            and envelope.expires_at < current_time - settings.clock_skew_seconds
        )
    )
    signature_is_valid = verify_message(
        public_key,
        envelope.signature,
        canonical_signing_payload(envelope),
    )
    if not metadata_is_valid or not signature_is_valid:
        raise AuthorizationError("invalid authorization")
