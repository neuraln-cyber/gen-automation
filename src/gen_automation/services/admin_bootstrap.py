import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from gen_automation.auth.security import (
    PasswordManager,
    SecretEncryptionError,
    TotpSecretCipher,
    verify_totp,
)
from gen_automation.db.models import AdminSession, AdminUser, AuditEvent
from gen_automation.domain.enums import AdminRole
from gen_automation.domain.ids import uuid7
from gen_automation.services.authentication import (
    AuthenticationFailedError,
    normalize_username,
)

_POSTGRES_BOOTSTRAP_LOCK_KEY = 20031985192981576


class BootstrapOwnerError(ValueError):
    """The one-time owner bootstrap could not be completed safely."""


@dataclass(frozen=True)
class BootstrapOwnerCommand:
    username: str
    display_name: str
    password: str
    totp_secret: str
    totp_code: str
    operator_identity: str
    change_ticket: str


@dataclass(frozen=True)
class BootstrapOwnerResult:
    user_id: str
    username: str
    display_name: str


@dataclass(frozen=True)
class RecoverOwnerCommand:
    username: str
    display_name: str
    password: str
    totp_secret: str
    totp_code: str
    confirmation: str
    operator_identity: str
    change_ticket: str
    force: bool = False
    second_operator_identity: str | None = None
    second_approval_ticket: str | None = None


@dataclass(frozen=True)
class RecoverOwnerResult:
    user_id: str
    username: str
    display_name: str
    created: bool
    revoked_session_count: int


def _normalize_display_name(value: str) -> str:
    normalized = " ".join(unicodedata.normalize("NFKC", value).strip().split())
    if not 1 <= len(normalized) <= 200 or any(ord(character) < 0x20 for character in normalized):
        raise BootstrapOwnerError("display name is invalid")
    return normalized


def _normalize_audit_field(value: str, *, label: str, maximum: int = 200) -> str:
    normalized = " ".join(unicodedata.normalize("NFKC", value).strip().split())
    if not 1 <= len(normalized) <= maximum or any(
        ord(character) < 0x20 for character in normalized
    ):
        raise BootstrapOwnerError(f"{label} is invalid")
    return normalized


async def bootstrap_initial_owner(
    session: AsyncSession,
    *,
    command: BootstrapOwnerCommand,
    password_manager: PasswordManager,
    totp_cipher: TotpSecretCipher,
    now: datetime | None = None,
) -> BootstrapOwnerResult:
    """Atomically create the first and only bootstrap owner."""

    observed_at = now or datetime.now(UTC)
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=UTC)
    else:
        observed_at = observed_at.astimezone(UTC)
    try:
        username = normalize_username(command.username)
    except AuthenticationFailedError:
        raise BootstrapOwnerError("username is invalid") from None
    display_name = _normalize_display_name(command.display_name)
    operator_identity = _normalize_audit_field(
        command.operator_identity,
        label="operator identity",
    )
    change_ticket = _normalize_audit_field(
        command.change_ticket,
        label="change ticket",
        maximum=100,
    )
    matched_counter = verify_totp(
        command.totp_secret,
        command.totp_code,
        unix_time=int(observed_at.timestamp()),
    )
    if matched_counter is None:
        raise BootstrapOwnerError("TOTP confirmation code is invalid")
    password_hash = password_manager.hash(command.password)
    user_id = uuid7()
    encrypted_totp_secret = totp_cipher.encrypt(
        command.totp_secret,
        subject=f"admin-user:{user_id}",
    )

    async with session.begin():
        bind = session.get_bind()
        if bind.dialect.name == "postgresql":
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:lock_key)"),
                {"lock_key": _POSTGRES_BOOTSTRAP_LOCK_KEY},
            )
        user_count = await session.scalar(select(func.count()).select_from(AdminUser))
        if user_count != 0:
            raise BootstrapOwnerError(
                "administrative users already exist; bootstrap is permanently disabled"
            )
        user = AdminUser(
            id=user_id,
            username_normalized=username,
            display_name=display_name,
            password_hash=password_hash,
            role=AdminRole.OWNER,
            is_active=True,
            totp_secret_ciphertext=encrypted_totp_secret,
            totp_confirmed_at=observed_at,
            last_totp_counter=matched_counter,
            failed_login_count=0,
            failed_login_window_started_at=None,
            locked_until=None,
            password_changed_at=observed_at,
            last_login_at=None,
            credential_version=1,
            lock_version=1,
        )
        session.add(user)
        session.add(
            AuditEvent(
                actor=operator_identity,
                action="auth.initial_owner_created",
                resource_type="admin_user",
                resource_id=user_id,
                correlation_id=f"initial-bootstrap:{change_ticket}",
                detail={
                    "change_ticket": change_ticket,
                    "before": None,
                    "after": {
                        "role": AdminRole.OWNER.value,
                        "is_active": True,
                        "credential_version": 1,
                    },
                    "totp_enrolled": True,
                },
                occurred_at=observed_at,
            )
        )

    return BootstrapOwnerResult(
        user_id=str(user_id),
        username=username,
        display_name=display_name,
    )


async def recover_owner(
    session: AsyncSession,
    *,
    command: RecoverOwnerCommand,
    password_manager: PasswordManager,
    totp_cipher: TotpSecretCipher,
    now: datetime | None = None,
) -> RecoverOwnerResult:
    """Explicit offline break-glass reset or creation of one owner identity."""

    observed_at = now or datetime.now(UTC)
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=UTC)
    else:
        observed_at = observed_at.astimezone(UTC)
    try:
        username = normalize_username(command.username)
    except AuthenticationFailedError:
        raise BootstrapOwnerError("username is invalid") from None
    expected_confirmation = (
        f"FORCE RECOVER OWNER {username}" if command.force else f"RECOVER OWNER {username}"
    )
    if command.confirmation != expected_confirmation:
        raise BootstrapOwnerError(f"confirmation must exactly match: {expected_confirmation}")
    display_name = _normalize_display_name(command.display_name)
    operator_identity = _normalize_audit_field(
        command.operator_identity,
        label="operator identity",
    )
    change_ticket = _normalize_audit_field(
        command.change_ticket,
        label="change ticket",
        maximum=100,
    )
    second_operator_identity: str | None = None
    second_approval_ticket: str | None = None
    if command.force:
        second_operator_identity = _normalize_audit_field(
            command.second_operator_identity or "",
            label="second operator identity",
        )
        second_approval_ticket = _normalize_audit_field(
            command.second_approval_ticket or "",
            label="second approval ticket",
            maximum=100,
        )
        if second_operator_identity.casefold() == operator_identity.casefold():
            raise BootstrapOwnerError("forced recovery requires two distinct operator identities")
    elif command.second_operator_identity is not None or command.second_approval_ticket is not None:
        raise BootstrapOwnerError("second-operator fields are only valid for forced recovery")
    matched_counter = verify_totp(
        command.totp_secret,
        command.totp_code,
        unix_time=int(observed_at.timestamp()),
    )
    if matched_counter is None:
        raise BootstrapOwnerError("TOTP confirmation code is invalid")
    password_hash = password_manager.hash(command.password)

    async with session.begin():
        bind = session.get_bind()
        if bind.dialect.name == "postgresql":
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:lock_key)"),
                {"lock_key": _POSTGRES_BOOTSTRAP_LOCK_KEY},
            )
        current_totp_counter = int(observed_at.timestamp()) // 30
        active_owners = list(
            (
                await session.scalars(
                    select(AdminUser)
                    .where(
                        AdminUser.role == AdminRole.OWNER,
                        AdminUser.is_active.is_(True),
                    )
                    .with_for_update()
                )
            ).all()
        )
        for active_owner in active_owners:
            if not password_manager.is_encoded_hash_acceptable(active_owner.password_hash):
                continue
            if (
                active_owner.totp_confirmed_at is None
                or active_owner.totp_secret_ciphertext is None
                or (
                    active_owner.last_totp_counter is not None
                    and active_owner.last_totp_counter > current_totp_counter + 1
                )
            ):
                continue
            try:
                totp_cipher.decrypt(
                    active_owner.totp_secret_ciphertext,
                    subject=f"admin-user:{active_owner.id}",
                )
            except SecretEncryptionError:
                continue
            if not command.force:
                raise BootstrapOwnerError(
                    "break-glass recovery is disabled while a usable active owner exists"
                )
        user = await session.scalar(
            select(AdminUser).where(AdminUser.username_normalized == username).with_for_update()
        )
        created = user is None
        user_id = uuid7() if user is None else user.id
        before = (
            None
            if user is None
            else {
                "role": user.role.value,
                "is_active": user.is_active,
                "credential_version": user.credential_version,
            }
        )
        encrypted_totp_secret = totp_cipher.encrypt(
            command.totp_secret,
            subject=f"admin-user:{user_id}",
        )
        revoked_session_count = 0
        if user is None:
            user = AdminUser(
                id=user_id,
                username_normalized=username,
                display_name=display_name,
                password_hash=password_hash,
                role=AdminRole.OWNER,
                is_active=True,
                totp_secret_ciphertext=encrypted_totp_secret,
                totp_confirmed_at=observed_at,
                last_totp_counter=matched_counter,
                failed_login_count=0,
                failed_login_window_started_at=None,
                locked_until=None,
                password_changed_at=observed_at,
                last_login_at=None,
                credential_version=1,
                lock_version=1,
            )
            session.add(user)
        else:
            user.display_name = display_name
            user.password_hash = password_hash
            user.role = AdminRole.OWNER
            user.is_active = True
            user.totp_secret_ciphertext = encrypted_totp_secret
            user.totp_confirmed_at = observed_at
            user.last_totp_counter = matched_counter
            user.failed_login_count = 0
            user.failed_login_window_started_at = None
            user.locked_until = None
            user.password_changed_at = observed_at
            user.credential_version += 1
            user.lock_version += 1
            revoked = await session.execute(
                update(AdminSession)
                .where(
                    AdminSession.user_id == user.id,
                    AdminSession.revoked_at.is_(None),
                )
                .values(revoked_at=observed_at)
            )
            revoked_session_count = int(getattr(revoked, "rowcount", 0) or 0)
        session.add(
            AuditEvent(
                actor=operator_identity,
                action=(
                    "auth.break_glass_owner_created"
                    if created
                    else "auth.break_glass_owner_recovered"
                ),
                resource_type="admin_user",
                resource_id=user_id,
                correlation_id=f"break-glass:{change_ticket}",
                detail={
                    "change_ticket": change_ticket,
                    "forced": command.force,
                    "second_operator_identity": second_operator_identity,
                    "second_approval_ticket": second_approval_ticket,
                    "before": before,
                    "after": {
                        "role": AdminRole.OWNER.value,
                        "is_active": True,
                        "credential_version": user.credential_version,
                    },
                    "totp_enrolled": True,
                    "revoked_session_count": revoked_session_count,
                },
                occurred_at=observed_at,
            )
        )

    return RecoverOwnerResult(
        user_id=str(user_id),
        username=username,
        display_name=display_name,
        created=created,
        revoked_session_count=revoked_session_count,
    )
