import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient

from gen_automation.app import create_app
from gen_automation.config import Environment, Settings
from gen_automation.integrations.salad.webhooks import SaladWebhookVerifier


@dataclass
class StaticSignatureVerifier:
    result: object
    error: Exception | None = None

    def verify(self, data: bytes | str, headers: dict[str, str]) -> object:
        if self.error is not None:
            raise self.error
        return self.result


def _job_payload(*, job_id: str, status: str = "succeeded") -> dict[str, Any]:
    now = datetime.now(UTC).isoformat()
    return {
        "id": job_id,
        "input": {"private_prompt": "must not be retained"},
        "status": status,
        "events": [{"action": status, "time": now}],
        "create_time": now,
        "update_time": now,
        "metadata": {
            "generation_attempt_id": str(uuid4()),
            "untrusted_private_value": "must not be retained",
        },
        "webhook": "https://example.test/webhooks/salad",
        "output": {"signed_upload_url": "must not be retained"},
    }


def _headers(webhook_id: str = "msg_salad_test") -> dict[str, str]:
    return {
        "webhook-id": webhook_id,
        "webhook-timestamp": str(int(datetime.now(UTC).timestamp())),
        "webhook-signature": "v1,test-signature",
        "content-type": "application/json",
    }


@contextmanager
def _client_with_payload(
    tmp_path: Path,
    payload: dict[str, Any],
) -> Iterator[tuple[TestClient, Path]]:
    database_path = tmp_path / "webhooks.db"
    settings = Settings(
        environment=Environment.TEST,
        database_url=f"sqlite+aiosqlite:///{database_path.as_posix()}",
        auto_create_schema=True,
        session_secret="test-session-secret-with-more-than-32-characters",  # noqa: S106
    )
    with TestClient(create_app(settings)) as client:
        client.app.state.salad_webhook_verifier = SaladWebhookVerifier(
            "test-webhook-secret",
            signature_verifier=StaticSignatureVerifier(payload),
        )
        yield client, database_path


def test_verified_webhook_is_durable_idempotent_and_content_minimized(tmp_path: Path) -> None:
    job_id = str(uuid4())
    payload = _job_payload(job_id=job_id)
    with _client_with_payload(tmp_path, payload) as (client, database_path):
        first = client.post("/webhooks/salad", content=b"signed-body", headers=_headers())
        replay = client.post("/webhooks/salad", content=b"signed-body", headers=_headers())

        assert first.status_code == 204
        assert first.headers["webhook-replayed"] == "false"
        assert replay.status_code == 204
        assert replay.headers["webhook-replayed"] == "true"

    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            """
            SELECT provider_external_job_id, status, event_metadata
            FROM webhook_receipts
            """
        ).fetchone()
        count = connection.execute("SELECT COUNT(*) FROM webhook_receipts").fetchone()

    assert row is not None
    assert row[0] == job_id
    assert row[1] == "received"
    assert "private_prompt" not in row[2]
    assert "signed_upload_url" not in row[2]
    assert "untrusted_private_value" not in row[2]
    assert count == (1,)


def test_same_webhook_id_with_changed_verified_payload_conflicts(tmp_path: Path) -> None:
    first_payload = _job_payload(job_id=str(uuid4()))
    with _client_with_payload(tmp_path, first_payload) as (client, _database_path):
        assert (
            client.post("/webhooks/salad", content=b"first", headers=_headers()).status_code == 204
        )
        client.app.state.salad_webhook_verifier = SaladWebhookVerifier(
            "test-webhook-secret",
            signature_verifier=StaticSignatureVerifier(
                _job_payload(job_id=str(uuid4()), status="failed")
            ),
        )

        conflict = client.post("/webhooks/salad", content=b"second", headers=_headers())

    assert conflict.status_code == 409
    assert conflict.json() == {"detail": "conflicting Salad webhook replay"}


def test_invalid_signature_is_rejected_without_provider_detail(
    client: TestClient,
) -> None:
    client.app.state.salad_webhook_verifier = SaladWebhookVerifier(
        "test-webhook-secret",
        signature_verifier=StaticSignatureVerifier(
            {},
            error=ValueError("secret provider detail"),
        ),
    )

    response = client.post("/webhooks/salad", content=b"private", headers=_headers())

    assert response.status_code == 400
    assert response.json() == {"detail": "invalid Salad webhook"}
    assert "secret" not in response.text


def test_unconfigured_receiver_is_temporarily_unavailable(client: TestClient) -> None:
    response = client.post("/webhooks/salad", content=b"{}", headers=_headers())

    assert response.status_code == 503


def test_body_limit_is_enforced_before_signature_verification(client: TestClient) -> None:
    client.app.state.salad_webhook_verifier = SaladWebhookVerifier(
        "test-webhook-secret",
        max_body_bytes=4,
        signature_verifier=StaticSignatureVerifier({}),
    )

    response = client.post("/webhooks/salad", content=b"12345", headers=_headers())

    assert response.status_code == 413


def test_duplicate_signature_header_is_rejected(client: TestClient) -> None:
    client.app.state.salad_webhook_verifier = SaladWebhookVerifier(
        "test-webhook-secret",
        signature_verifier=StaticSignatureVerifier({}),
    )
    headers = [
        ("webhook-id", "msg_one"),
        ("Webhook-Id", "msg_two"),
        ("webhook-timestamp", str(int(datetime.now(UTC).timestamp()))),
        ("webhook-signature", "v1,test"),
    ]

    response = client.post("/webhooks/salad", content=b"{}", headers=headers)

    assert response.status_code == 400
