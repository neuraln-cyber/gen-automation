from __future__ import annotations

import pytest

from gen_automation.services.civitai_secrets import (
    CivitaiCredentialError,
    _secret_value,
)


@pytest.mark.parametrize(
    ("payload", "expected"),
    (
        ("plain-token", "plain-token"),
        ('"json-token"', "json-token"),
        ('{"api_key":"legacy-token"}', "legacy-token"),
        (
            '{"schema":"gen-automation/civitai/v1","api_token":"current-token"}',
            "current-token",
        ),
    ),
)
def test_secret_value_accepts_supported_formats(payload: str, expected: str) -> None:
    assert _secret_value(payload) == expected


@pytest.mark.parametrize(
    "payload",
    (
        '{"schema":"gen-automation/civitai/v2","api_token":"token"}',
        '{"schema":"gen-automation/civitai/v1","api_key":"token"}',
        '{"schema":"gen-automation/civitai/v1","api_token":"token","extra":true}',
        '{"schema":"gen-automation/civitai/v1","api_token":"first","api_token":"second"}',
    ),
)
def test_secret_value_rejects_unknown_or_ambiguous_json(payload: str) -> None:
    with pytest.raises(CivitaiCredentialError, match="invalid format"):
        _secret_value(payload)
