import json
import math
import time
from collections.abc import Callable

from gen_automation.domain.signing import sign_message, verify_message
from gen_automation.gpu_worker.models import SignedGenerateEnvelope, WorkerSettings


class AuthorizationError(Exception):
    """Raised when worker request authorization fails."""


def _reject_non_finite(value: object) -> None:
    if isinstance(value, float):
        if not math.isfinite(value):
            raise AuthorizationError("invalid authorization")
        return
    if isinstance(value, list):
        for item in value:
            _reject_non_finite(item)
        return
    if isinstance(value, dict):
        for item in value.values():
            _reject_non_finite(item)


def canonical_signing_payload(envelope: SignedGenerateEnvelope) -> bytes:
    unsigned: dict[str, object] = {
        "expires_at": envelope.expires_at,
        "issued_at": envelope.issued_at,
        "key_id": envelope.key_id,
        "payload": envelope.payload.model_dump(mode="json"),
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


def calculate_signature(envelope: SignedGenerateEnvelope, private_key: str) -> str:
    return sign_message(private_key, canonical_signing_payload(envelope))


def verify_authorization(
    envelope: SignedGenerateEnvelope,
    settings: WorkerSettings,
    *,
    now: Callable[[], float] = time.time,
) -> None:
    public_key = settings.verification_keys.get(envelope.key_id)
    current_time = int(now())
    metadata_is_valid = not (
        public_key is None
        or envelope.expires_at <= envelope.issued_at
        or envelope.expires_at - envelope.issued_at > settings.max_signature_ttl_seconds
        or envelope.issued_at > current_time + settings.clock_skew_seconds
        or envelope.expires_at < current_time - settings.clock_skew_seconds
    )

    # Unknown key identifiers still take the Ed25519 verification path using a
    # fixed dummy public key. The response therefore does not enumerate active
    # rotation identifiers.
    signature_is_valid = verify_message(
        public_key,
        envelope.signature,
        canonical_signing_payload(envelope),
    )
    if not metadata_is_valid or not signature_is_valid:
        raise AuthorizationError("invalid authorization")
