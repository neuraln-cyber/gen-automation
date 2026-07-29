from __future__ import annotations

import asyncio
import getpass
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gen_automation.auth.operator_settings import AuthenticationOperatorSettings
    from gen_automation.services.admin_bootstrap import (
        BootstrapOwnerCommand,
        RecoverOwnerCommand,
    )


class _PromptInputError(ValueError):
    pass


def _required_prompt(label: str) -> str:
    value = input(label).strip()
    if not value:
        raise _PromptInputError("all prompts are required")
    return value


async def _apply_owner_command(
    *,
    settings: AuthenticationOperatorSettings,
    command: BootstrapOwnerCommand | RecoverOwnerCommand,
) -> None:
    from gen_automation.auth.security import PasswordManager, TotpSecretCipher
    from gen_automation.db.session import Database
    from gen_automation.services.admin_bootstrap import (
        BootstrapOwnerCommand,
        bootstrap_initial_owner,
        recover_owner,
    )

    database = Database(settings.database_url)
    try:
        keys = {
            key_id: secret.get_secret_value()
            for key_id, secret in settings.auth_totp_encryption_keys.items()
        }
        cipher = TotpSecretCipher(
            keys,
            active_key_id=settings.auth_totp_active_key_id or "",
        )
        async with database.sessions() as session:
            if isinstance(command, BootstrapOwnerCommand):
                await bootstrap_initial_owner(
                    session,
                    command=command,
                    password_manager=PasswordManager(),
                    totp_cipher=cipher,
                )
            else:
                await recover_owner(
                    session,
                    command=command,
                    password_manager=PasswordManager(),
                    totp_cipher=cipher,
                )
    finally:
        await database.dispose()


def bootstrap_owner_main() -> int:
    """Interactive entry point; secrets are never accepted as command arguments."""

    arguments = sys.argv[1:]
    if arguments not in (
        ["bootstrap-owner"],
        ["recover-owner"],
        ["recover-owner", "--force"],
    ):
        print(
            "Usage: python -m gen_automation.cli {bootstrap-owner|recover-owner [--force]}",
            file=sys.stderr,
        )
        return 2
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print(
            "Owner bootstrap requires an interactive TTY so credentials are not "
            "written to command history or automation logs.",
            file=sys.stderr,
        )
        return 2

    from pydantic import ValidationError

    from gen_automation.auth.operator_settings import AuthenticationOperatorSettings
    from gen_automation.auth.security import (
        PasswordPolicyError,
        generate_totp_secret,
        provisioning_uri,
    )
    from gen_automation.services.admin_bootstrap import (
        BootstrapOwnerCommand,
        BootstrapOwnerError,
        RecoverOwnerCommand,
    )
    from gen_automation.services.authentication import normalize_username

    try:
        settings = AuthenticationOperatorSettings()
    except ValidationError:
        print(
            "Authentication operator configuration is invalid. Provide only the "
            "database URL and TOTP keyring through the job's secret identity.",
            file=sys.stderr,
        )
        return 2

    try:
        username = _required_prompt("Owner username: ")
        display_name = _required_prompt("Owner display name: ")
        password = getpass.getpass("New password (minimum 14 characters): ")
        confirmation = getpass.getpass("Confirm password: ")
        if password != confirmation:
            raise BootstrapOwnerError("password confirmation does not match")

        totp_secret = generate_totp_secret()
        print("\nAdd this account to your authenticator now.")
        print("Keep the following URI and secret private; they are shown only once.")
        print(provisioning_uri(totp_secret, account_name=username))
        print(f"Manual secret: {totp_secret}")
        totp_code = getpass.getpass("Current six-digit authenticator code: ")
        if arguments == ["bootstrap-owner"]:
            operator_identity = _required_prompt("Operator identity (individual account/email): ")
            change_ticket = _required_prompt("Approved change ticket: ")
            command: BootstrapOwnerCommand | RecoverOwnerCommand = BootstrapOwnerCommand(
                username=username,
                display_name=display_name,
                password=password,
                totp_secret=totp_secret,
                totp_code=totp_code,
                operator_identity=operator_identity,
                change_ticket=change_ticket,
            )
        else:
            normalized_username = normalize_username(username)
            forced_recovery = arguments == ["recover-owner", "--force"]
            expected_confirmation = (
                f"FORCE RECOVER OWNER {normalized_username}"
                if forced_recovery
                else f"RECOVER OWNER {normalized_username}"
            )
            operator_identity = _required_prompt("Operator identity (individual account/email): ")
            change_ticket = _required_prompt("Approved change/incident ticket: ")
            second_operator_identity = None
            second_approval_ticket = None
            if forced_recovery:
                second_operator_identity = _required_prompt("Second approving operator identity: ")
                second_approval_ticket = _required_prompt("Second approval/change ticket: ")
            print(
                "Break-glass recovery can reactivate or create an owner and revoke "
                "that identity's active sessions."
            )
            print(f"Type exactly: {expected_confirmation}")
            recovery_confirmation = _required_prompt("Confirmation: ")
            command = RecoverOwnerCommand(
                username=username,
                display_name=display_name,
                password=password,
                totp_secret=totp_secret,
                totp_code=totp_code,
                confirmation=recovery_confirmation,
                operator_identity=operator_identity,
                change_ticket=change_ticket,
                force=forced_recovery,
                second_operator_identity=second_operator_identity,
                second_approval_ticket=second_approval_ticket,
            )
        asyncio.run(_apply_owner_command(settings=settings, command=command))
    except (BootstrapOwnerError, PasswordPolicyError, _PromptInputError) as error:
        print(f"Authentication operator action failed: {error}", file=sys.stderr)
        return 1
    except Exception:
        print(
            "Authentication operator action failed without exposing database or "
            "secret details. Check restricted service logs.",
            file=sys.stderr,
        )
        return 1

    action = "Initial owner created" if arguments == ["bootstrap-owner"] else "Owner recovered"
    print(f"{action}. Wait for the authenticator code to rotate before the first login.")
    return 0


if __name__ == "__main__":
    raise SystemExit(bootstrap_owner_main())
