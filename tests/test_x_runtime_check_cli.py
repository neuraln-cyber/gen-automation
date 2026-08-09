from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest

from gen_automation import x_runtime_check_cli
from gen_automation.config import Settings, XAuthMode

REFERENCE = (
    "aws-secrets-manager://arn:aws:secretsmanager:eu-central-1:"
    "861912887470:secret:gen-automation-staging/x/oauth1-AbCdEf"
)
CREATOR_ID = "2244994945"


def _settings(**updates: object) -> Settings:
    values: dict[str, object] = {
        "x_auth_mode": XAuthMode.OAUTH1,
        "x_oauth_secret_reference": REFERENCE,
        "x_creator_user_id": CREATOR_ID,
        "x_oauth_request_timeout_seconds": 30,
        "publishing_enabled": False,
        "patreon_browser_publishing_enabled": False,
    }
    values.update(updates)
    return cast(Settings, SimpleNamespace(**values))


@pytest.mark.asyncio
async def test_account_binding_check_uses_verify_only_and_closes_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []

    class Provider:
        async def verify_account_binding(self, reference: str) -> str:
            calls.append(("verify", reference))
            return CREATOR_ID

        def open_for_effect(self, _reference: str) -> object:
            raise AssertionError("a zero-post canary must not request an effect client")

        async def aclose(self) -> None:
            calls.append(("close", ""))

    def build_provider(**kwargs: object) -> Provider:
        assert kwargs == {
            "configured_reference": REFERENCE,
            "expected_creator_user_id": CREATOR_ID,
            "request_timeout_seconds": 30,
        }
        return Provider()

    monkeypatch.setattr(
        x_runtime_check_cli,
        "build_aws_secrets_manager_x_oauth1_provider",
        build_provider,
    )

    await x_runtime_check_cli._check_account_binding(_settings())

    assert calls == [("verify", REFERENCE), ("close", "")]


@pytest.mark.asyncio
async def test_account_binding_check_fails_closed_on_wrong_mode_before_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_build(**_kwargs: object) -> object:
        raise AssertionError("an invalid runtime must fail before provider construction")

    monkeypatch.setattr(
        x_runtime_check_cli,
        "build_aws_secrets_manager_x_oauth1_provider",
        unexpected_build,
    )

    with pytest.raises(RuntimeError, match="not configured"):
        await x_runtime_check_cli._check_account_binding(_settings(x_auth_mode=XAuthMode.OAUTH2))


def test_exact_configured_check_rejects_each_mismatch() -> None:
    x_runtime_check_cli._assert_oauth1_configured(
        _settings(),
        expected_reference=REFERENCE,
        expected_creator_user_id=CREATOR_ID,
    )

    for settings, reference, creator_id in (
        (_settings(x_auth_mode=XAuthMode.OAUTH2), REFERENCE, CREATOR_ID),
        (_settings(), f"{REFERENCE}x", CREATOR_ID),
        (_settings(), REFERENCE, f"{CREATOR_ID}0"),
    ):
        with pytest.raises(RuntimeError, match="does not match"):
            x_runtime_check_cli._assert_oauth1_configured(
                settings,
                expected_reference=reference,
                expected_creator_user_id=creator_id,
            )


def test_publishing_enabled_check_requires_x_only_runtime_gate() -> None:
    x_runtime_check_cli._assert_publishing_enabled(_settings(publishing_enabled=True))

    for settings in (
        _settings(publishing_enabled=False),
        _settings(publishing_enabled=True, patreon_browser_publishing_enabled=True),
    ):
        with pytest.raises(RuntimeError, match="publishing orchestration configuration"):
            x_runtime_check_cli._assert_publishing_enabled(settings)


@pytest.mark.parametrize(
    "arguments",
    (
        [],
        ["--account-binding", "unexpected"],
        ["--assert-publishing-enabled", "unexpected"],
        ["--assert-oauth1-configured", REFERENCE],
        ["--unknown"],
    ),
)
def test_cli_rejects_every_undocumented_argument_shape(
    arguments: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert x_runtime_check_cli.x_runtime_check_main(arguments) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "Choose exactly one documented X runtime check.\n"


def test_cli_emits_only_fixed_success_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def check(_settings: Settings) -> None:
        return None

    monkeypatch.setattr(x_runtime_check_cli, "Settings", lambda: _settings())
    monkeypatch.setattr(x_runtime_check_cli, "_check_account_binding", check)

    assert x_runtime_check_cli.x_runtime_check_main(["--account-binding"]) == 0
    captured = capsys.readouterr()
    assert captured.out == f"{x_runtime_check_cli.ACCOUNT_BINDING_MESSAGE}\n"
    assert captured.err == ""


def test_cli_emits_only_fixed_publishing_assertion_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        x_runtime_check_cli,
        "Settings",
        lambda: _settings(publishing_enabled=True),
    )

    assert x_runtime_check_cli.x_runtime_check_main(["--assert-publishing-enabled"]) == 0
    captured = capsys.readouterr()
    assert captured.out == f"{x_runtime_check_cli.PUBLISHING_ENABLED_MESSAGE}\n"
    assert captured.err == ""


def test_cli_redacts_unexpected_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    marker = "must-not-be-printed"

    async def fail(_settings: Settings) -> None:
        raise RuntimeError(marker)

    monkeypatch.setattr(x_runtime_check_cli, "Settings", lambda: _settings())
    monkeypatch.setattr(x_runtime_check_cli, "_check_account_binding", fail)

    assert x_runtime_check_cli.x_runtime_check_main(["--account-binding"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "The X runtime check failed safely.\n"
    assert marker not in captured.err
