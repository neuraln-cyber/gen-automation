import pytest
from pydantic import ValidationError

from gen_automation.domain.signing import (
    SigningMaterialError,
    derive_public_key,
    encode_base64url,
    sign_message,
    validate_private_key,
    validate_public_key,
    verify_message,
)
from gen_automation.gpu_worker.models import (
    GenerateEnvelope,
    GeneratePayload,
    GeneratePayloadReference,
    ReferencedGenerateEnvelope,
    UploadGrant,
    WorkerEnvironment,
    WorkerSettings,
)
from gen_automation.gpu_worker.security import (
    AuthorizationError,
    calculate_signature,
    verify_authorization,
)

NOW = 2_000_000_000
PRIMARY_PRIVATE_KEY = encode_base64url(bytes(range(1, 33)))
PRIMARY_PUBLIC_KEY = derive_public_key(PRIMARY_PRIVATE_KEY)
ROTATED_PRIVATE_KEY = encode_base64url(bytes(range(33, 65)))
ROTATED_PUBLIC_KEY = derive_public_key(ROTATED_PRIVATE_KEY)
MESSAGE = b"deterministic worker signing test"


def _unsigned_envelope(*, key_id: str = "worker-key-1") -> GenerateEnvelope:
    return GenerateEnvelope(
        version="v1",
        key_id=key_id,
        issued_at=NOW - 5,
        expires_at=NOW + 60,
        payload=GeneratePayload(
            job_id="job-1",
            attempt_id="attempt-1",
            workflow={
                "1": {
                    "class_type": "SaveImage",
                    "inputs": {"images": ["0", 0]},
                }
            },
            uploads=[
                UploadGrant(
                    asset_id="asset-1",
                    upload_attempt_id="upload-1",
                    output_index=0,
                    content_type="image/png",
                    url="https://uploads.example.test/staging/output-0",
                    fields={"key": "private/output-0", "policy": "test-policy"},
                )
            ],
        ),
        signature="A" * 86,
    )


def _signed_envelope(
    private_key: str = PRIMARY_PRIVATE_KEY,
    *,
    key_id: str = "worker-key-1",
) -> GenerateEnvelope:
    unsigned = _unsigned_envelope(key_id=key_id)
    return unsigned.model_copy(update={"signature": calculate_signature(unsigned, private_key)})


def _signed_referenced_envelope() -> ReferencedGenerateEnvelope:
    unsigned = ReferencedGenerateEnvelope(
        version="v2",
        key_id="worker-key-1",
        issued_at=NOW - 5,
        expires_at=NOW + 60,
        payload=GeneratePayloadReference(
            job_id="job-1",
            attempt_id="attempt-1",
            url="https://uploads.example.test/staging/worker-requests/attempt-1/payload.json",
            sha256="a" * 64,
            byte_size=1234,
        ),
        signature="A" * 86,
    )
    return unsigned.model_copy(
        update={"signature": calculate_signature(unsigned, PRIMARY_PRIVATE_KEY)}
    )


def _worker_settings(**verification_keys: str) -> WorkerSettings:
    return WorkerSettings(
        environment=WorkerEnvironment.TEST,
        verification_keys=verification_keys or {"worker-key-1": PRIMARY_PUBLIC_KEY},
        allowed_upload_origin="https://uploads.example.test",
    )


def test_ed25519_signatures_are_canonical_and_verify() -> None:
    validate_private_key(PRIMARY_PRIVATE_KEY)
    validate_public_key(PRIMARY_PUBLIC_KEY)

    signature = sign_message(PRIMARY_PRIVATE_KEY, MESSAGE)

    assert len(PRIMARY_PRIVATE_KEY) == 43
    assert len(PRIMARY_PUBLIC_KEY) == 43
    assert len(signature) == 86
    assert "=" not in signature
    assert verify_message(PRIMARY_PUBLIC_KEY, signature, MESSAGE)
    assert not verify_message(ROTATED_PUBLIC_KEY, signature, MESSAGE)
    assert not verify_message(PRIMARY_PUBLIC_KEY, signature, MESSAGE + b"-tampered")


@pytest.mark.parametrize(
    "private_key",
    [
        "",
        "not-base64url",
        PRIMARY_PRIVATE_KEY + "=",
        PRIMARY_PRIVATE_KEY + "\n",
        encode_base64url(b"x" * 31),
    ],
)
def test_private_keys_must_be_canonical_raw_ed25519_material(
    private_key: str,
) -> None:
    with pytest.raises(SigningMaterialError):
        validate_private_key(private_key)
    with pytest.raises(SigningMaterialError):
        derive_public_key(private_key)
    with pytest.raises(SigningMaterialError):
        sign_message(private_key, MESSAGE)


@pytest.mark.parametrize(
    "public_key",
    [
        "",
        "not-base64url",
        PRIMARY_PUBLIC_KEY + "=",
        PRIMARY_PUBLIC_KEY + " ",
        encode_base64url(b"x" * 31),
    ],
)
def test_public_keys_must_be_canonical_raw_ed25519_material(
    public_key: str,
) -> None:
    with pytest.raises(SigningMaterialError):
        validate_public_key(public_key)


@pytest.mark.parametrize(
    "signature",
    [
        "",
        "A" * 85,
        "A" * 87,
        "A" * 85 + "=",
        "A" * 85 + "+",
        "A" * 86,
    ],
)
def test_malformed_noncanonical_or_forged_signatures_do_not_verify(
    signature: str,
) -> None:
    assert not verify_message(PRIMARY_PUBLIC_KEY, signature, MESSAGE)


@pytest.mark.parametrize(
    "signature",
    [
        "A" * 85,
        "A" * 87,
        "A" * 85 + "=",
        "A" * 85 + "+",
    ],
)
def test_envelope_rejects_malformed_signature_shape(signature: str) -> None:
    raw = _unsigned_envelope().model_dump(mode="json")
    raw["signature"] = signature

    with pytest.raises(ValidationError):
        GenerateEnvelope.model_validate(raw, strict=True)


def test_worker_public_key_rotation_accepts_both_active_key_ids() -> None:
    settings = _worker_settings(
        **{
            "worker-key-1": PRIMARY_PUBLIC_KEY,
            "worker-key-2": ROTATED_PUBLIC_KEY,
        }
    )

    verify_authorization(
        _signed_envelope(PRIMARY_PRIVATE_KEY, key_id="worker-key-1"),
        settings,
        now=lambda: NOW,
    )
    verify_authorization(
        _signed_envelope(ROTATED_PRIVATE_KEY, key_id="worker-key-2"),
        settings,
        now=lambda: NOW,
    )

    serialized = settings.model_dump_json()
    assert PRIMARY_PRIVATE_KEY not in serialized
    assert ROTATED_PRIVATE_KEY not in serialized


def test_referenced_envelope_signature_binds_the_exact_private_payload_reference() -> None:
    envelope = _signed_referenced_envelope()

    verify_authorization(envelope, _worker_settings(), now=lambda: NOW)

    tampered = envelope.model_copy(
        update={
            "payload": envelope.payload.model_copy(update={"byte_size": 1235}),
        }
    )
    with pytest.raises(AuthorizationError, match="invalid authorization"):
        verify_authorization(tampered, _worker_settings(), now=lambda: NOW)


@pytest.mark.parametrize(
    ("envelope", "settings"),
    [
        (
            _signed_envelope(PRIMARY_PRIVATE_KEY, key_id="unknown-key"),
            _worker_settings(),
        ),
        (
            _signed_envelope(PRIMARY_PRIVATE_KEY),
            _worker_settings(**{"worker-key-1": ROTATED_PUBLIC_KEY}),
        ),
        (
            _signed_envelope(PRIMARY_PRIVATE_KEY).model_copy(update={"expires_at": NOW + 61}),
            _worker_settings(),
        ),
    ],
    ids=["unknown-key-id", "wrong-public-key", "tampered-envelope"],
)
def test_worker_rejects_unknown_keys_wrong_keys_and_tampering(
    envelope: GenerateEnvelope,
    settings: WorkerSettings,
) -> None:
    with pytest.raises(AuthorizationError, match="invalid authorization"):
        verify_authorization(envelope, settings, now=lambda: NOW)
