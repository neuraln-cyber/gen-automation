from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from gen_automation.auth.security import (
    PasswordManager,
    SecretEncryptionError,
    TotpSecretCipher,
)
from gen_automation.config import Settings
from gen_automation.db.models import AdminUser
from gen_automation.domain.enums import AdminRole
from gen_automation.services.admin_enrollment import AdminEnrollmentService
from gen_automation.services.authentication import (
    AuthenticationPolicy,
    AuthenticationService,
)


@dataclass(frozen=True)
class AuthenticationRuntime:
    service: AuthenticationService
    enrollment_service: AdminEnrollmentService
    password_manager: PasswordManager
    totp_cipher: TotpSecretCipher


def build_authentication_runtime(settings: Settings) -> AuthenticationRuntime:
    """Build the singleton authentication service from validated settings."""

    keys = {
        key_id: secret.get_secret_value()
        for key_id, secret in settings.auth_totp_encryption_keys.items()
    }
    password_manager = PasswordManager()
    totp_cipher = TotpSecretCipher(
        keys,
        active_key_id=settings.auth_totp_active_key_id or "",
    )
    service = AuthenticationService(
        password_manager=password_manager,
        totp_cipher=totp_cipher,
        session_hmac_key=settings.session_secret.get_secret_value(),
        policy=AuthenticationPolicy(
            require_totp=settings.auth_require_totp,
            session_absolute_seconds=settings.auth_session_absolute_seconds,
            session_idle_seconds=settings.auth_session_idle_seconds,
            recent_auth_seconds=settings.auth_recent_auth_seconds,
            login_window_seconds=settings.auth_login_window_seconds,
            login_max_failures=settings.auth_login_max_failures,
            login_lockout_seconds=settings.auth_login_lockout_seconds,
        ),
    )
    enrollment_service = AdminEnrollmentService(
        password_manager=password_manager,
        totp_cipher=totp_cipher,
    )
    return AuthenticationRuntime(
        service=service,
        enrollment_service=enrollment_service,
        password_manager=password_manager,
        totp_cipher=totp_cipher,
    )


async def assert_authentication_bootstrapped(
    session: AsyncSession,
    *,
    runtime: AuthenticationRuntime,
    require_totp: bool,
) -> None:
    """Fail startup unless at least one owner has structurally usable credentials."""

    owners = list(
        (
            await session.scalars(
                select(AdminUser)
                .where(
                    AdminUser.role == AdminRole.OWNER,
                    AdminUser.is_active.is_(True),
                )
                .order_by(AdminUser.id)
            )
        ).all()
    )
    current_totp_counter = int(datetime.now(UTC).timestamp()) // 30
    for owner in owners:
        if not runtime.password_manager.is_encoded_hash_acceptable(owner.password_hash):
            continue
        if not require_totp:
            return
        if (
            owner.totp_confirmed_at is None
            or owner.totp_secret_ciphertext is None
            or (
                owner.last_totp_counter is not None
                and owner.last_totp_counter > current_totp_counter + 1
            )
        ):
            continue
        try:
            runtime.totp_cipher.decrypt(
                owner.totp_secret_ciphertext,
                subject=f"admin-user:{owner.id}",
            )
        except SecretEncryptionError:
            continue
        return

    user_count = await session.scalar(select(func.count()).select_from(AdminUser))
    if user_count == 0:
        raise RuntimeError(
            "authentication has no administrative users; run the one-off "
            "`python -m gen_automation.cli bootstrap-owner` job before app replicas"
        )
    raise RuntimeError(
        "authentication has no active owner with usable credentials; run the "
        "one-off `python -m gen_automation.cli recover-owner` job before app replicas"
    )
