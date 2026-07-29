import base64
import importlib.util
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest

from gen_automation.integrations.salad.webhooks import (
    SaladWebhookBodyTooLargeError,
    SaladWebhookHeaderError,
    SaladWebhookPayloadError,
    SaladWebhookSignatureError,
    SaladWebhookVerifier,
)


@dataclass
class RecordingVerifier:
    result: object
    error: Exception | None = None
    calls: list[tuple[bytes | str, dict[str, str]]] = field(default_factory=list)

    def verify(self, data: bytes | str, headers: dict[str, str]) -> object:
        self.calls.append((data, headers))
        if self.error is not None:
            raise self.error
        return self.result


def signed_headers() -> dict[str, str]:
    return {
        "Webhook-Id": "msg_test",
        "Webhook-Timestamp": "1735689600",
        "Webhook-Signature": "v1,test-signature",
        "Content-Type": "application/json",
    }


def test_verifies_exact_raw_body_before_returning_typed_envelope() -> None:
    raw_body = b'{\n  "id": "job-1", "status": "succeeded"\n}\n'
    signature_verifier = RecordingVerifier(result={"id": "job-1", "status": "succeeded"})
    verifier = SaladWebhookVerifier(
        "test-webhook-secret",
        signature_verifier=signature_verifier,
    )

    envelope = verifier.verify(body=raw_body, headers=signed_headers())

    assert signature_verifier.calls == [
        (
            raw_body,
            {
                "webhook-id": "msg_test",
                "webhook-timestamp": "1735689600",
                "webhook-signature": "v1,test-signature",
            },
        )
    ]
    assert envelope.webhook_id == "msg_test"
    assert envelope.webhook_timestamp == 1735689600
    assert envelope.payload == {"id": "job-1", "status": "succeeded"}


@pytest.mark.parametrize(
    "missing_header",
    ["webhook-id", "webhook-timestamp", "webhook-signature"],
)
def test_missing_required_header_fails_before_verification(missing_header: str) -> None:
    signature_verifier = RecordingVerifier(result={})
    verifier = SaladWebhookVerifier(
        "test-webhook-secret",
        signature_verifier=signature_verifier,
    )
    headers = {
        key: value for key, value in signed_headers().items() if key.lower() != missing_header
    }

    with pytest.raises(SaladWebhookHeaderError, match="missing required"):
        verifier.verify(body=b"{}", headers=headers)

    assert signature_verifier.calls == []


def test_duplicate_case_variant_header_fails_closed() -> None:
    signature_verifier = RecordingVerifier(result={})
    verifier = SaladWebhookVerifier(
        "test-webhook-secret",
        signature_verifier=signature_verifier,
    )
    headers = signed_headers()
    headers["webhook-id"] = "different-message"

    with pytest.raises(SaladWebhookHeaderError, match="duplicate"):
        verifier.verify(body=b"{}", headers=headers)

    assert signature_verifier.calls == []


def test_oversized_body_fails_before_verification() -> None:
    signature_verifier = RecordingVerifier(result={})
    verifier = SaladWebhookVerifier(
        "test-webhook-secret",
        max_body_bytes=4,
        signature_verifier=signature_verifier,
    )

    with pytest.raises(SaladWebhookBodyTooLargeError, match="size limit"):
        verifier.verify(body=b"12345", headers=signed_headers())

    assert signature_verifier.calls == []


@pytest.mark.asyncio
async def test_streaming_limit_stops_before_later_chunks() -> None:
    signature_verifier = RecordingVerifier(result={})
    verifier = SaladWebhookVerifier(
        "test-webhook-secret",
        max_body_bytes=4,
        signature_verifier=signature_verifier,
    )
    chunks_read = 0

    async def chunks() -> AsyncIterator[bytes]:
        nonlocal chunks_read
        for chunk in (b"123", b"45", b"must-not-be-read"):
            chunks_read += 1
            yield chunk

    with pytest.raises(SaladWebhookBodyTooLargeError, match="size limit"):
        await verifier.verify_stream(chunks=chunks(), headers=signed_headers())

    assert chunks_read == 2
    assert signature_verifier.calls == []


@pytest.mark.asyncio
async def test_streaming_path_preserves_bytes() -> None:
    signature_verifier = RecordingVerifier(result={"id": "job-1"})
    verifier = SaladWebhookVerifier(
        "test-webhook-secret",
        signature_verifier=signature_verifier,
    )

    async def chunks() -> AsyncIterator[bytes]:
        yield b'{ "id":'
        yield b' "job-1" }\n'

    envelope = await verifier.verify_stream(chunks=chunks(), headers=signed_headers())

    assert signature_verifier.calls[0][0] == b'{ "id": "job-1" }\n'
    assert envelope.payload == {"id": "job-1"}


def test_signature_failure_is_redacted() -> None:
    signature_verifier = RecordingVerifier(
        result={},
        error=ValueError("provider detail containing authentication material"),
    )
    verifier = SaladWebhookVerifier(
        "test-webhook-secret",
        signature_verifier=signature_verifier,
    )

    with pytest.raises(
        SaladWebhookSignatureError,
        match=r"^webhook signature verification failed$",
    ) as raised:
        verifier.verify(body=b'{"private":"payload"}', headers=signed_headers())

    assert raised.value.__cause__ is None
    assert "authentication material" not in str(raised.value)
    assert "private" not in str(raised.value)


@pytest.mark.parametrize(
    "verified_payload",
    [
        b"{not-json",
        ["top-level", "array"],
        {"score": float("nan")},
        {"unexpected": object()},
    ],
)
def test_invalid_verified_payload_is_rejected(verified_payload: object) -> None:
    signature_verifier = RecordingVerifier(result=verified_payload)
    verifier = SaladWebhookVerifier(
        "test-webhook-secret",
        signature_verifier=signature_verifier,
    )

    with pytest.raises(SaladWebhookPayloadError):
        verifier.verify(body=b"signed-body", headers=signed_headers())

    assert len(signature_verifier.calls) == 1


def test_invalid_timestamp_is_rejected_after_signature_verification() -> None:
    signature_verifier = RecordingVerifier(result={})
    verifier = SaladWebhookVerifier(
        "test-webhook-secret",
        signature_verifier=signature_verifier,
    )
    headers = signed_headers()
    headers["Webhook-Timestamp"] = "not-a-timestamp"

    with pytest.raises(SaladWebhookHeaderError, match="timestamp"):
        verifier.verify(body=b"{}", headers=headers)

    assert len(signature_verifier.calls) == 1


@pytest.mark.skipif(importlib.util.find_spec("svix") is None, reason="svix is not installed")
def test_current_svix_package_contract() -> None:
    from svix.webhooks import Webhook

    secret = "whsec_" + base64.b64encode(b"salad-test-webhook-secret-material").decode()
    body = b'{"id":"job-1","status":"succeeded"}'
    message_id = "msg_real_contract"
    timestamp = datetime.now(tz=UTC).replace(microsecond=0)
    signature = Webhook(secret).sign(message_id, timestamp, body.decode())
    verifier = SaladWebhookVerifier(secret)

    envelope = verifier.verify(
        body=body,
        headers={
            "webhook-id": message_id,
            "webhook-timestamp": str(int(timestamp.timestamp())),
            "webhook-signature": signature,
        },
    )

    assert envelope.payload["id"] == "job-1"
