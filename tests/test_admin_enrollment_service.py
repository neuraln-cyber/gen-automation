import base64
import hashlib
import hmac
import struct
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from argon2 import PasswordHasher, Type
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from gen_automation.auth.security import (
    PasswordManager,
    TotpSecretCipher,
    hash_opaque_token,
)
from gen_automation.db.models import AdminEnrollment, AdminUser, AuditEvent
from gen_automation.db.session import Database
from gen_automation.domain.enums import (
    AdminEnrollmentState,
    AdminRole,
)
from gen_automation.services.admin_enrollment import (
    AdminEnrollmentConflictError,
    AdminEnrollmentInputError,
    AdminEnrollmentInvalidError,
    AdminEnrollmentService,
)

NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
TOTP_KEY = base64.urlsafe_b64encode(bytes(range(32))).rstrip(b"=").decode("ascii")
OWNER_PASSWORD = "a long unique owner password"  # noqa: S105
INVITED_PASSWORD = "a different strong invited password"  # noqa: S105


@dataclass(frozen=True, slots=True)
class EnrollmentContext:
    database: Database
    service: AdminEnrollmentService
    password_manager: PasswordManager
    cipher: TotpSecretCipher
    owner_id: UUID


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


def _totp_code(secret: str, *, at: datetime) -> str:
    counter = int(at.timestamp()) // 30
    digest = hmac.new(
        base64.b32decode(secret),
        struct.pack(">Q", counter),
        hashlib.sha1,
    ).digest()
    offset = digest[-1] & 15
    value = int.from_bytes(digest[offset : offset + 4], "big") & 0x7FFFFFFF
    return f"{value % 1_000_000:06d}"


@pytest.fixture
async def enrollment_context(tmp_path: Path) -> AsyncIterator[EnrollmentContext]:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'admin-enrollment.db').as_posix()}")
    await database.create_schema()
    password_manager = _password_manager()
    cipher = TotpSecretCipher({"key-1": TOTP_KEY}, active_key_id="key-1")
    service = AdminEnrollmentService(
        password_manager=password_manager,
        totp_cipher=cipher,
    )
    async with database.sessions() as session:
        owner = AdminUser(
            username_normalized="owner@example.test",
            display_name="Owner",
            password_hash=password_manager.hash(OWNER_PASSWORD),
            role=AdminRole.OWNER,
            is_active=True,
            failed_login_count=0,
            password_changed_at=NOW,
            credential_version=1,
            lock_version=1,
        )
        session.add(owner)
        await session.commit()
        context = EnrollmentContext(
            database=database,
            service=service,
            password_manager=password_manager,
            cipher=cipher,
            owner_id=owner.id,
        )
    try:
        yield context
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_invitation_stores_only_token_digest_and_withholds_totp(
    enrollment_context: EnrollmentContext,
) -> None:
    async with enrollment_context.database.sessions() as session:
        invitation = await enrollment_context.service.create_invitation(
            session,
            username=" SECOND-OWNER@EXAMPLE.TEST ",
            display_name="  Second   Owner ",
            role=AdminRole.OWNER,
            invited_by_user_id=enrollment_context.owner_id,
            correlation_id="invite-second-owner",
            ttl_seconds=3600,
            now=NOW,
        )

    assert len(invitation.invite_token) == 43
    assert invitation.username == "second-owner@example.test"
    assert invitation.display_name == "Second Owner"
    assert invitation.role == AdminRole.OWNER
    assert invitation.expires_at == NOW + timedelta(hours=1)
    assert not hasattr(invitation, "totp_secret")

    async with enrollment_context.database.sessions() as session:
        stored = await session.get(AdminEnrollment, invitation.enrollment_id)
        audit = await session.scalar(
            select(AuditEvent).where(AuditEvent.action == "auth.admin_enrollment_invited")
        )
        assert stored is not None
        assert stored.token_sha256 == hash_opaque_token(invitation.invite_token)
        assert invitation.invite_token not in str(stored.__dict__)
        assert stored.totp_secret_ciphertext is not None
        secret = enrollment_context.cipher.decrypt(
            stored.totp_secret_ciphertext,
            subject=f"admin-enrollment:{stored.id}",
        )
        assert len(secret) == 32
        assert secret not in stored.totp_secret_ciphertext
        assert audit is not None
        assert invitation.invite_token not in str(audit.detail)
        assert secret not in str(audit.detail)
        assert "token" not in str(audit.detail).casefold()

    async with enrollment_context.database.sessions() as session:
        inspected = await enrollment_context.service.inspect_capability(
            session,
            invite_token=invitation.invite_token,
            now=NOW + timedelta(minutes=1),
        )
    assert inspected.totp_secret == secret
    assert invitation.invite_token not in inspected.totp_provisioning_uri
    assert f"secret={secret}" in inspected.totp_provisioning_uri


@pytest.mark.asyncio
async def test_completion_rebinds_totp_creates_active_user_and_consumes_once(
    enrollment_context: EnrollmentContext,
) -> None:
    async with enrollment_context.database.sessions() as session:
        invitation = await enrollment_context.service.create_invitation(
            session,
            username="second-owner@example.test",
            display_name="Second Owner",
            role=AdminRole.OWNER,
            invited_by_user_id=enrollment_context.owner_id,
            correlation_id="invite",
            ttl_seconds=3600,
            now=NOW,
        )
    async with enrollment_context.database.sessions() as session:
        inspection = await enrollment_context.service.inspect_capability(
            session,
            invite_token=invitation.invite_token,
            now=NOW,
        )
    completion_at = NOW + timedelta(minutes=1)
    async with enrollment_context.database.sessions() as session:
        completed = await enrollment_context.service.complete_enrollment(
            session,
            invite_token=invitation.invite_token,
            password=INVITED_PASSWORD,
            totp_code=_totp_code(inspection.totp_secret, at=completion_at),
            correlation_id="complete",
            now=completion_at,
        )

    assert completed.username == "second-owner@example.test"
    assert completed.role == AdminRole.OWNER
    async with enrollment_context.database.sessions() as session:
        stored = await session.get(AdminEnrollment, invitation.enrollment_id)
        user = await session.get(AdminUser, completed.user_id)
        audit = await session.scalar(
            select(AuditEvent).where(AuditEvent.action == "auth.admin_enrollment_completed")
        )
        assert stored is not None
        assert stored.state == AdminEnrollmentState.CONSUMED
        assert stored.totp_secret_ciphertext is None
        assert stored.consumed_by_user_id == user.id
        assert stored.lock_version == 2
        assert user is not None
        assert user.is_active
        assert user.role == AdminRole.OWNER
        assert user.last_totp_counter == int(completion_at.timestamp()) // 30
        assert user.totp_secret_ciphertext is not None
        assert (
            enrollment_context.cipher.decrypt(
                user.totp_secret_ciphertext,
                subject=f"admin-user:{user.id}",
            )
            == inspection.totp_secret
        )
        assert enrollment_context.password_manager.verify(
            user.password_hash,
            INVITED_PASSWORD,
        ).valid
        assert audit is not None
        audit_text = str(audit.detail)
        assert invitation.invite_token not in audit_text
        assert inspection.totp_secret not in audit_text
        assert INVITED_PASSWORD not in audit_text
        assert stored.token_sha256 not in audit_text

    async with enrollment_context.database.sessions() as session:
        with pytest.raises(
            AdminEnrollmentInvalidError,
            match="invalid or expired",
        ):
            await enrollment_context.service.complete_enrollment(
                session,
                invite_token=invitation.invite_token,
                password=INVITED_PASSWORD,
                totp_code=_totp_code(inspection.totp_secret, at=completion_at),
                correlation_id="replay",
                now=completion_at,
            )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(AdminUser)
                .where(AdminUser.username_normalized == "second-owner@example.test")
            )
            == 1
        )


@pytest.mark.asyncio
async def test_invalid_totp_and_password_leave_pending_invitation(
    enrollment_context: EnrollmentContext,
) -> None:
    async with enrollment_context.database.sessions() as session:
        invitation = await enrollment_context.service.create_invitation(
            session,
            username="admin@example.test",
            display_name="Admin",
            role=AdminRole.ADMIN,
            invited_by_user_id=enrollment_context.owner_id,
            correlation_id="invite",
            now=NOW,
        )
    async with enrollment_context.database.sessions() as session:
        inspection = await enrollment_context.service.inspect_capability(
            session,
            invite_token=invitation.invite_token,
            now=NOW,
        )
    valid_code = _totp_code(inspection.totp_secret, at=NOW)
    wrong_code = f"{(int(valid_code) + 1) % 1_000_000:06d}"

    async with enrollment_context.database.sessions() as session:
        with pytest.raises(AdminEnrollmentInvalidError, match="invalid or expired"):
            await enrollment_context.service.complete_enrollment(
                session,
                invite_token=invitation.invite_token,
                password=INVITED_PASSWORD,
                totp_code=wrong_code,
                correlation_id="wrong-totp",
                now=NOW,
            )
    async with enrollment_context.database.sessions() as session:
        with pytest.raises(AdminEnrollmentInputError, match="password"):
            await enrollment_context.service.complete_enrollment(
                session,
                invite_token=invitation.invite_token,
                password="too short",  # noqa: S106
                totp_code=valid_code,
                correlation_id="weak-password",
                now=NOW,
            )
    async with enrollment_context.database.sessions() as session:
        stored = await session.get(AdminEnrollment, invitation.enrollment_id)
        assert stored is not None
        assert stored.state == AdminEnrollmentState.PENDING
        assert stored.totp_secret_ciphertext is not None
        assert (
            await session.scalar(
                select(func.count())
                .select_from(AdminUser)
                .where(AdminUser.username_normalized == "admin@example.test")
            )
            == 0
        )


@pytest.mark.asyncio
async def test_invalid_expired_and_revoked_capabilities_use_generic_error(
    enrollment_context: EnrollmentContext,
) -> None:
    async with enrollment_context.database.sessions() as session:
        expired = await enrollment_context.service.create_invitation(
            session,
            username="expired@example.test",
            display_name="Expired",
            role=AdminRole.REVIEWER,
            invited_by_user_id=enrollment_context.owner_id,
            correlation_id="expired",
            ttl_seconds=600,
            now=NOW,
        )
    async with enrollment_context.database.sessions() as session:
        revoked = await enrollment_context.service.create_invitation(
            session,
            username="revoked@example.test",
            display_name="Revoked",
            role=AdminRole.PUBLISHER,
            invited_by_user_id=enrollment_context.owner_id,
            correlation_id="revoked",
            now=NOW,
        )
    async with enrollment_context.database.sessions() as session:
        await enrollment_context.service.revoke_invitation(
            session,
            enrollment_id=revoked.enrollment_id,
            revoked_by_user_id=enrollment_context.owner_id,
            correlation_id="revoke",
            now=NOW + timedelta(minutes=1),
        )

    messages: list[str] = []
    for token, observed_at in (
        ("malformed", NOW),
        (expired.invite_token, NOW + timedelta(minutes=10)),
        (revoked.invite_token, NOW + timedelta(minutes=1)),
    ):
        async with enrollment_context.database.sessions() as session:
            with pytest.raises(AdminEnrollmentInvalidError) as raised:
                await enrollment_context.service.inspect_capability(
                    session,
                    invite_token=token,
                    now=observed_at,
                )
            messages.append(str(raised.value))
    assert messages == [
        "administrator enrollment is invalid or expired",
        "administrator enrollment is invalid or expired",
        "administrator enrollment is invalid or expired",
    ]


@pytest.mark.asyncio
async def test_expired_pending_username_is_revoked_before_reinvitation(
    enrollment_context: EnrollmentContext,
) -> None:
    async with enrollment_context.database.sessions() as session:
        first = await enrollment_context.service.create_invitation(
            session,
            username="repeat@example.test",
            display_name="First",
            role=AdminRole.ADMIN,
            invited_by_user_id=enrollment_context.owner_id,
            correlation_id="first",
            ttl_seconds=600,
            now=NOW,
        )
    async with enrollment_context.database.sessions() as session:
        with pytest.raises(AdminEnrollmentConflictError, match="pending"):
            await enrollment_context.service.create_invitation(
                session,
                username="repeat@example.test",
                display_name="Still Pending",
                role=AdminRole.ADMIN,
                invited_by_user_id=enrollment_context.owner_id,
                correlation_id="duplicate",
                now=NOW + timedelta(minutes=5),
            )
    async with enrollment_context.database.sessions() as session:
        replacement = await enrollment_context.service.create_invitation(
            session,
            username="repeat@example.test",
            display_name="Replacement",
            role=AdminRole.ADMIN,
            invited_by_user_id=enrollment_context.owner_id,
            correlation_id="replacement",
            now=NOW + timedelta(minutes=11),
        )

    assert replacement.enrollment_id != first.enrollment_id
    async with enrollment_context.database.sessions() as session:
        original = await session.get(AdminEnrollment, first.enrollment_id)
        current = await session.get(AdminEnrollment, replacement.enrollment_id)
        assert original is not None
        assert original.state == AdminEnrollmentState.REVOKED
        assert original.totp_secret_ciphertext is None
        assert original.revoked_by_user_id == enrollment_context.owner_id
        assert current is not None
        assert current.state == AdminEnrollmentState.PENDING


@pytest.mark.asyncio
async def test_database_enforces_pending_username_and_lifecycle_constraints(
    enrollment_context: EnrollmentContext,
) -> None:
    async with enrollment_context.database.sessions() as session:
        invitation = await enrollment_context.service.create_invitation(
            session,
            username="constrained@example.test",
            display_name="Constrained",
            role=AdminRole.ADMIN,
            invited_by_user_id=enrollment_context.owner_id,
            correlation_id="constrained",
            now=NOW,
        )

    async with enrollment_context.database.sessions() as session:
        original = await session.get(AdminEnrollment, invitation.enrollment_id)
        assert original is not None
        session.add(
            AdminEnrollment(
                username_normalized=original.username_normalized,
                display_name="Duplicate",
                role=AdminRole.ADMIN,
                token_sha256="f" * 64,
                totp_secret_ciphertext=original.totp_secret_ciphertext,
                state=AdminEnrollmentState.PENDING,
                invited_by_user_id=enrollment_context.owner_id,
                invited_at=NOW,
                expires_at=NOW + timedelta(hours=1),
                lock_version=1,
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()

        original = await session.get(AdminEnrollment, invitation.enrollment_id)
        assert original is not None
        original.state = AdminEnrollmentState.CONSUMED
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()

        original = await session.get(AdminEnrollment, invitation.enrollment_id)
        assert original is not None
        assert original.state == AdminEnrollmentState.PENDING


@pytest.mark.asyncio
async def test_concurrent_username_claim_is_generic_and_rolls_back_completion(
    enrollment_context: EnrollmentContext,
) -> None:
    async with enrollment_context.database.sessions() as session:
        invitation = await enrollment_context.service.create_invitation(
            session,
            username="claimed@example.test",
            display_name="Claimed",
            role=AdminRole.OWNER,
            invited_by_user_id=enrollment_context.owner_id,
            correlation_id="invite",
            now=NOW,
        )
    async with enrollment_context.database.sessions() as session:
        inspection = await enrollment_context.service.inspect_capability(
            session,
            invite_token=invitation.invite_token,
            now=NOW,
        )
    async with enrollment_context.database.sessions() as session:
        session.add(
            AdminUser(
                username_normalized="claimed@example.test",
                display_name="Concurrent Claim",
                password_hash=enrollment_context.password_manager.hash(
                    "a strong concurrent claim password"
                ),
                role=AdminRole.OWNER,
                is_active=True,
                failed_login_count=0,
                password_changed_at=NOW,
                credential_version=1,
                lock_version=1,
            )
        )
        await session.commit()

    async with enrollment_context.database.sessions() as session:
        with pytest.raises(AdminEnrollmentInvalidError, match="invalid or expired"):
            await enrollment_context.service.complete_enrollment(
                session,
                invite_token=invitation.invite_token,
                password=INVITED_PASSWORD,
                totp_code=_totp_code(inspection.totp_secret, at=NOW),
                correlation_id="completion-race",
                now=NOW,
            )
    async with enrollment_context.database.sessions() as session:
        stored = await session.get(AdminEnrollment, invitation.enrollment_id)
        assert stored is not None
        assert stored.state == AdminEnrollmentState.PENDING
        assert stored.consumed_by_user_id is None
        assert (
            await session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.action == "auth.admin_enrollment_completed")
            )
            == 0
        )
