"""Fail-closed staging checks for the X runtime integration."""

from __future__ import annotations

import asyncio
import hmac
import sys
from collections.abc import Sequence

from sqlalchemy import func, select

from gen_automation.config import Settings, XAuthMode
from gen_automation.db.models import PublicationAttempt, PublicationStep
from gen_automation.db.session import Database
from gen_automation.domain.enums import PublicationAttemptState, PublicationStepState
from gen_automation.services.publication import get_publication_guard
from gen_automation.services.x_oauth1 import build_aws_secrets_manager_x_oauth1_provider

ASSERT_SAFE_MODE = "--assert-safe-to-configure"
ASSERT_CONFIGURED_MODE = "--assert-oauth1-configured"
ACCOUNT_BINDING_MODE = "--account-binding"
SAFE_TO_CONFIGURE_MESSAGE = "Publication is stopped and no publication effect is active."
CONFIGURED_MESSAGE = "The running controller has the exact OAuth 1.0a runtime settings."
ACCOUNT_BINDING_MESSAGE = (
    "X OAuth 1.0a account binding passed. No media was uploaded and no post was created."
)


async def _assert_safe_to_configure(settings: Settings) -> None:
    database = Database(settings.database_url)
    try:
        async with database.sessions() as session:
            guard = await get_publication_guard(session)
            active_attempts = await session.scalar(
                select(func.count())
                .select_from(PublicationAttempt)
                .where(
                    PublicationAttempt.state.in_(
                        (
                            PublicationAttemptState.CLAIMED,
                            PublicationAttemptState.PROCESSING,
                        )
                    )
                )
            )
            active_steps = await session.scalar(
                select(func.count())
                .select_from(PublicationStep)
                .where(PublicationStep.state == PublicationStepState.PROCESSING)
            )
        if guard.enabled or active_attempts != 0 or active_steps != 0:
            raise RuntimeError("publication is not safely stopped")
    finally:
        await database.dispose()


async def _check_account_binding(settings: Settings) -> None:
    if (
        settings.x_auth_mode != XAuthMode.OAUTH1
        or settings.x_oauth_secret_reference is None
        or settings.x_creator_user_id is None
    ):
        raise RuntimeError("the OAuth 1.0a runtime is not configured")
    provider = build_aws_secrets_manager_x_oauth1_provider(
        configured_reference=settings.x_oauth_secret_reference,
        expected_creator_user_id=settings.x_creator_user_id,
        request_timeout_seconds=settings.x_oauth_request_timeout_seconds,
    )
    try:
        creator_user_id = await provider.verify_account_binding(settings.x_oauth_secret_reference)
        if not hmac.compare_digest(creator_user_id, settings.x_creator_user_id):
            raise RuntimeError("the creator binding does not match")
    finally:
        await provider.aclose()


def _assert_oauth1_configured(
    settings: Settings,
    *,
    expected_reference: str,
    expected_creator_user_id: str,
) -> None:
    if (
        settings.x_auth_mode != XAuthMode.OAUTH1
        or settings.x_oauth_secret_reference is None
        or settings.x_creator_user_id is None
        or not hmac.compare_digest(settings.x_oauth_secret_reference, expected_reference)
        or not hmac.compare_digest(settings.x_creator_user_id, expected_creator_user_id)
    ):
        raise RuntimeError("the running OAuth 1.0a configuration does not match")


def x_runtime_check_main(arguments: Sequence[str] | None = None) -> int:
    """Run exactly one non-mutating runtime check and emit only a fixed result."""

    resolved_arguments = list(sys.argv[1:] if arguments is None else arguments)
    valid_simple_mode = resolved_arguments in ([ASSERT_SAFE_MODE], [ACCOUNT_BINDING_MODE])
    valid_configured_mode = len(resolved_arguments) == 3 and resolved_arguments[0] == (
        ASSERT_CONFIGURED_MODE
    )
    if not valid_simple_mode and not valid_configured_mode:
        print("Choose exactly one documented X runtime check.", file=sys.stderr)
        return 2
    try:
        settings = Settings()
        if resolved_arguments == [ASSERT_SAFE_MODE]:
            asyncio.run(_assert_safe_to_configure(settings))
            print(SAFE_TO_CONFIGURE_MESSAGE)
        elif valid_configured_mode:
            _assert_oauth1_configured(
                settings,
                expected_reference=resolved_arguments[1],
                expected_creator_user_id=resolved_arguments[2],
            )
            print(CONFIGURED_MESSAGE)
        else:
            asyncio.run(_check_account_binding(settings))
            print(ACCOUNT_BINDING_MESSAGE)
    except Exception:
        print("The X runtime check failed safely.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through the console contract
    raise SystemExit(x_runtime_check_main())
