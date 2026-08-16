from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from gen_automation.services.i2v_runpod_claims import (
    I2VRunPodClaimError,
    I2VRunPodClaimIdentity,
    create_i2v_runpod_claim_token,
    verify_i2v_runpod_claim_token,
)

NOW = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
CLAIM_KEY = "not-a-real-runpod-key-for-tests"
IDENTITY = I2VRunPodClaimIdentity(
    job_id=UUID("11111111-1111-4111-8111-111111111111"),
    attempt_id=UUID("22222222-2222-4222-8222-222222222222"),
    request_sha256="a" * 64,
    submission_key="b" * 64,
    expires_at=NOW + timedelta(hours=2),
)


def test_claim_token_round_trips_exact_identity() -> None:
    token = create_i2v_runpod_claim_token(secret=CLAIM_KEY, identity=IDENTITY)
    assert (
        verify_i2v_runpod_claim_token(
            token,
            secret=CLAIM_KEY,
            now=NOW,
        )
        == IDENTITY
    )
    assert "runpod-secret" not in token


def test_claim_token_rejects_tampering_and_expiry() -> None:
    token = create_i2v_runpod_claim_token(secret=CLAIM_KEY, identity=IDENTITY)
    with pytest.raises(I2VRunPodClaimError):
        verify_i2v_runpod_claim_token(
            token + "x",
            secret=CLAIM_KEY,
            now=NOW,
        )
    with pytest.raises(I2VRunPodClaimError, match="expired"):
        verify_i2v_runpod_claim_token(
            token,
            secret=CLAIM_KEY,
            now=IDENTITY.expires_at,
        )
