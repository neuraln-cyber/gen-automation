from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

import pytest
from fastapi import Response
from pydantic import ValidationError

from gen_automation.api.routes import publications
from gen_automation.domain.enums import AdminRole, PublicationIntentState
from gen_automation.domain.publication import PublicationCancellationCreate
from gen_automation.services.authentication import AuthenticatedPrincipal
from gen_automation.services.publication import PublicationCancellationResult


def _publisher() -> AuthenticatedPrincipal:
    now = datetime.now(UTC)
    return AuthenticatedPrincipal(
        session_id=uuid4(),
        user_id=uuid4(),
        username="publisher",
        display_name="Publisher",
        role=AdminRole.PUBLISHER,
        csrf_sha256="a" * 64,
        expires_at=now,
        idle_expires_at=now,
        reauthenticated_at=now,
        mfa_verified_at=now,
    )


def test_publication_cancellation_command_is_strict_and_bounded() -> None:
    valid = {
        "expected_intent_digest": "a" * 64,
        "expected_lock_version": 3,
        "attestation": "cancel before effect",
    }

    assert PublicationCancellationCreate.model_validate(valid).expected_lock_version == 3
    with pytest.raises(ValidationError):
        PublicationCancellationCreate.model_validate({**valid, "unexpected": True})
    with pytest.raises(ValidationError):
        PublicationCancellationCreate.model_validate(
            {**valid, "expected_intent_digest": "not-a-digest"}
        )
    with pytest.raises(ValidationError):
        PublicationCancellationCreate.model_validate({**valid, "expected_lock_version": 0})


@pytest.mark.asyncio
async def test_publisher_can_cancel_through_route_with_idempotency_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal = _publisher()
    intent_id = uuid4()
    response = Response()
    calls: list[dict[str, object]] = []

    async def cancel(_session: object, **kwargs: object) -> PublicationCancellationResult:
        calls.append(kwargs)
        return PublicationCancellationResult(
            intent_id=intent_id,
            intent_lock_version=8,
            state=PublicationIntentState.CANCELLED,
            replayed=True,
        )

    monkeypatch.setattr(publications, "cancel_publication_intent", cancel)
    command = PublicationCancellationCreate(
        expected_intent_digest="b" * 64,
        expected_lock_version=7,
        attestation="exact fixed attestation",
    )

    result = await publications.post_publication_cancellation(
        intent_id,
        command,
        cast(Any, object()),
        principal,
        "cancel-route-key",
        response,
    )

    assert result.intent_id == intent_id
    assert result.intent_lock_version == 8
    assert result.state == PublicationIntentState.CANCELLED
    assert result.replayed
    assert response.headers["Idempotency-Replayed"] == "true"
    assert calls == [
        {
            "intent_id": intent_id,
            "expected_intent_digest": "b" * 64,
            "expected_lock_version": 7,
            "actor_user_id": principal.user_id,
            "actor_role": AdminRole.PUBLISHER,
            "attestation": "exact fixed attestation",
            "idempotency_key": "cancel-route-key",
        }
    ]
