import base64
import hashlib
import hmac
import struct
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from argon2 import PasswordHasher, Type
from pydantic import ValidationError
from sqlalchemy import func, select

from gen_automation.auth.operator_settings import AuthenticationOperatorSettings
from gen_automation.auth.security import (
    PasswordManager,
    TotpSecretCipher,
    generate_totp_secret,
    hash_opaque_token,
)
from gen_automation.db.models import AdminSession, AdminUser, AuditEvent
from gen_automation.db.session import Database
from gen_automation.domain.enums import AdminRole
from gen_automation.services.admin_bootstrap import (
    BootstrapOwnerCommand,
    BootstrapOwnerError,
    RecoverOwnerCommand,
    bootstrap_initial_owner,
    recover_owner,
)

SESSION_KEY = base64.urlsafe_b64encode(bytes(range(32))).rstrip(b"=").decode("ascii")
NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
PASSWORD = "a long unique bootstrap password"  # noqa: S105
SECOND_OWNER_PASSWORD = "a different long owner password"  # noqa: S105


def test_operator_settings_have_minimum_secret_scope() -> None:
    settings = AuthenticationOperatorSettings(
        database_url="sqlite+aiosqlite:///:memory:",
        auth_totp_active_key_id="key-1",
        auth_totp_encryption_keys={"key-1": SESSION_KEY},
    )

    assert set(type(settings).model_fields) == {
        "database_url",
        "auth_totp_active_key_id",
        "auth_totp_encryption_keys",
    }
    assert not hasattr(settings, "salad_api_key")
    with pytest.raises(ValidationError):
        AuthenticationOperatorSettings(
            database_url="sqlite+aiosqlite:///:memory:",
            auth_totp_active_key_id="key-1",
            auth_totp_encryption_keys={"key-1": "not-a-key"},
        )


def _password_manager() -> PasswordManager:
    return PasswordManager(
        PasswordHasher(
            time_cost=1,
            memory_cost=8192,
            parallelism=1,
            hash_len=16,
            salt_len=16,
            type=Type.ID,
        )
    )


def _totp_code(secret: str, *, at: datetime = NOW) -> str:
    counter = int(at.timestamp()) // 30
    digest = hmac.new(
        base64.b32decode(secret),
        struct.pack(">Q", counter),
        hashlib.sha1,
    ).digest()
    offset = digest[-1] & 15
    value = int.from_bytes(digest[offset : offset + 4], "big") & 0x7FFFFFFF
    return f"{value % 1_000_000:06d}"


def _wrong_totp_code(secret: str) -> str:
    return f"{(int(_totp_code(secret)) + 1) % 1_000_000:06d}"


@pytest.mark.asyncio
async def test_initial_owner_bootstrap_is_encrypted_audited_and_one_time(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'bootstrap.db').as_posix()}")
    await database.create_schema()
    cipher = TotpSecretCipher({"key-1": SESSION_KEY}, active_key_id="key-1")
    secret = generate_totp_secret()
    command = BootstrapOwnerCommand(
        username=" Owner@Example.Test ",
        display_name="  Primary   Owner ",
        password=PASSWORD,
        totp_secret=secret,
        totp_code=_totp_code(secret),
        operator_identity="operator@example.test",
        change_ticket="CHG-2026-0001",
    )
    try:
        async with database.sessions() as session:
            result = await bootstrap_initial_owner(
                session,
                command=command,
                password_manager=_password_manager(),
                totp_cipher=cipher,
                now=NOW,
            )

        async with database.sessions() as session:
            user = await session.scalar(select(AdminUser))
            audit = await session.scalar(select(AuditEvent))
            assert user is not None
            assert user.role == AdminRole.OWNER
            assert user.username_normalized == "owner@example.test"
            assert user.display_name == "Primary Owner"
            assert user.totp_secret_ciphertext is not None
            assert secret not in user.totp_secret_ciphertext
            assert (
                cipher.decrypt(
                    user.totp_secret_ciphertext,
                    subject=f"admin-user:{user.id}",
                )
                == secret
            )
            assert user.last_totp_counter == int(NOW.timestamp()) // 30
            assert result.user_id == str(user.id)
            assert audit is not None
            assert audit.action == "auth.initial_owner_created"
            assert audit.actor == "operator@example.test"
            assert audit.correlation_id == "initial-bootstrap:CHG-2026-0001"
            assert secret not in str(audit.detail)

        async with database.sessions() as session:
            with pytest.raises(BootstrapOwnerError, match="permanently disabled"):
                await bootstrap_initial_owner(
                    session,
                    command=command,
                    password_manager=_password_manager(),
                    totp_cipher=cipher,
                    now=NOW,
                )
            count = await session.scalar(select(func.count()).select_from(AdminUser))
            assert count == 1
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_break_glass_recovery_reactivates_owner_and_revokes_sessions(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'recovery.db').as_posix()}")
    await database.create_schema()
    cipher = TotpSecretCipher({"key-1": SESSION_KEY}, active_key_id="key-1")
    original_secret = generate_totp_secret()
    original = BootstrapOwnerCommand(
        username="owner@example.test",
        display_name="Original Owner",
        password=PASSWORD,
        totp_secret=original_secret,
        totp_code=_totp_code(original_secret),
        operator_identity="operator@example.test",
        change_ticket="CHG-2026-0002",
    )
    recovery_at = NOW + timedelta(minutes=2)
    replacement_secret = generate_totp_secret()
    replacement_password = "a different long recovery password"  # noqa: S105
    try:
        async with database.sessions() as session:
            created = await bootstrap_initial_owner(
                session,
                command=original,
                password_manager=_password_manager(),
                totp_cipher=cipher,
                now=NOW,
            )
        async with database.sessions() as session:
            user = await session.get(AdminUser, UUID(created.user_id))
            assert user is not None
            user.is_active = False
            user.totp_secret_ciphertext = "v1.key-1.corrupt"  # noqa: S105
            browser_session = AdminSession(
                user_id=user.id,
                token_sha256=hash_opaque_token(
                    base64.urlsafe_b64encode(bytes(range(64, 96))).rstrip(b"=").decode("ascii")
                ),
                csrf_sha256=hash_opaque_token(
                    base64.urlsafe_b64encode(bytes(range(96, 128))).rstrip(b"=").decode("ascii")
                ),
                credential_version=1,
                created_at=NOW,
                last_seen_at=NOW,
                expires_at=NOW + timedelta(hours=1),
                idle_expires_at=NOW + timedelta(minutes=30),
                reauthenticated_at=NOW,
                mfa_verified_at=NOW,
            )
            session.add(browser_session)
            await session.commit()
            browser_session_id = browser_session.id

        async with database.sessions() as session:
            result = await recover_owner(
                session,
                command=RecoverOwnerCommand(
                    username=" OWNER@EXAMPLE.TEST ",
                    display_name="Recovered Owner",
                    password=replacement_password,
                    totp_secret=replacement_secret,
                    totp_code=_totp_code(replacement_secret, at=recovery_at),
                    confirmation="RECOVER OWNER owner@example.test",
                    operator_identity="operator@example.test",
                    change_ticket="INC-2026-0042",
                ),
                password_manager=_password_manager(),
                totp_cipher=cipher,
                now=recovery_at,
            )

        async with database.sessions() as session:
            user = await session.get(AdminUser, UUID(result.user_id))
            browser_session = await session.get(AdminSession, browser_session_id)
            audits = list(
                (await session.scalars(select(AuditEvent).order_by(AuditEvent.occurred_at))).all()
            )
            assert user is not None
            assert user.is_active
            assert user.role == AdminRole.OWNER
            assert user.display_name == "Recovered Owner"
            assert user.credential_version == 2
            assert user.last_totp_counter == int(recovery_at.timestamp()) // 30
            assert user.totp_secret_ciphertext is not None
            assert (
                cipher.decrypt(
                    user.totp_secret_ciphertext,
                    subject=f"admin-user:{user.id}",
                )
                == replacement_secret
            )
            assert (
                _password_manager()
                .verify(
                    user.password_hash,
                    replacement_password,
                )
                .valid
            )
            assert browser_session is not None
            assert browser_session.revoked_at is not None
            assert result.revoked_session_count == 1
            assert audits[-1].action == "auth.break_glass_owner_recovered"
            assert audits[-1].actor == "operator@example.test"
            assert audits[-1].correlation_id == "break-glass:INC-2026-0042"
            assert audits[-1].detail["before"] == {
                "role": "owner",
                "is_active": False,
                "credential_version": 1,
            }
            assert audits[-1].detail["after"]["credential_version"] == 2
            assert replacement_secret not in str(audits[-1].detail)
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_break_glass_refuses_while_healthy_owner_exists(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'healthy.db').as_posix()}")
    await database.create_schema()
    cipher = TotpSecretCipher({"key-1": SESSION_KEY}, active_key_id="key-1")
    original_secret = generate_totp_secret()
    replacement_secret = generate_totp_secret()
    try:
        async with database.sessions() as session:
            await bootstrap_initial_owner(
                session,
                command=BootstrapOwnerCommand(
                    username="owner@example.test",
                    display_name="Owner",
                    password=PASSWORD,
                    totp_secret=original_secret,
                    totp_code=_totp_code(original_secret),
                    operator_identity="operator@example.test",
                    change_ticket="CHG-2026-0003",
                ),
                password_manager=_password_manager(),
                totp_cipher=cipher,
                now=NOW,
            )
        async with database.sessions() as session:
            with pytest.raises(BootstrapOwnerError, match="usable active owner"):
                await recover_owner(
                    session,
                    command=RecoverOwnerCommand(
                        username="second-owner@example.test",
                        display_name="Second Owner",
                        password=SECOND_OWNER_PASSWORD,
                        totp_secret=replacement_secret,
                        totp_code=_totp_code(replacement_secret),
                        confirmation="RECOVER OWNER second-owner@example.test",
                        operator_identity="operator@example.test",
                        change_ticket="INC-2026-0043",
                    ),
                    password_manager=_password_manager(),
                    totp_cipher=cipher,
                    now=NOW,
                )
        async with database.sessions() as session:
            assert await session.scalar(select(func.count()).select_from(AdminUser)) == 1
        async with database.sessions() as session:
            forced = await recover_owner(
                session,
                command=RecoverOwnerCommand(
                    username="second-owner@example.test",
                    display_name="Second Owner",
                    password=SECOND_OWNER_PASSWORD,
                    totp_secret=replacement_secret,
                    totp_code=_totp_code(replacement_secret),
                    confirmation=("FORCE RECOVER OWNER second-owner@example.test"),
                    operator_identity="operator@example.test",
                    change_ticket="INC-2026-0044",
                    force=True,
                    second_operator_identity="approver@example.test",
                    second_approval_ticket="CAB-2026-0099",
                ),
                password_manager=_password_manager(),
                totp_cipher=cipher,
                now=NOW,
            )
            assert forced.created
        async with database.sessions() as session:
            assert await session.scalar(select(func.count()).select_from(AdminUser)) == 2
            forced_audit = await session.scalar(
                select(AuditEvent).where(AuditEvent.action == "auth.break_glass_owner_created")
            )
            assert forced_audit is not None
            assert forced_audit.detail["forced"] is True
            assert forced_audit.detail["second_operator_identity"] == "approver@example.test"
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_invalid_totp_leaves_no_partial_owner(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'invalid.db').as_posix()}")
    await database.create_schema()
    secret = generate_totp_secret()
    command = BootstrapOwnerCommand(
        username="owner",
        display_name="Owner",
        password=PASSWORD,
        totp_secret=secret,
        totp_code=_wrong_totp_code(secret),
        operator_identity="operator@example.test",
        change_ticket="CHG-2026-0004",
    )
    try:
        async with database.sessions() as session:
            with pytest.raises(BootstrapOwnerError, match="TOTP"):
                await bootstrap_initial_owner(
                    session,
                    command=command,
                    password_manager=_password_manager(),
                    totp_cipher=TotpSecretCipher(
                        {"key-1": SESSION_KEY},
                        active_key_id="key-1",
                    ),
                    now=NOW,
                )
        async with database.sessions() as session:
            count = await session.scalar(select(func.count()).select_from(AdminUser))
            assert count == 0
    finally:
        await database.dispose()
