import asyncio
import base64
import hashlib
import hmac
import json
import struct
from datetime import UTC, datetime, timedelta
from pathlib import Path

from argon2 import PasswordHasher, Type
from fastapi.testclient import TestClient
from sqlalchemy import select

from gen_automation.app import create_app
from gen_automation.auth.security import (
    PasswordManager,
    TotpSecretCipher,
    generate_totp_secret,
)
from gen_automation.config import Environment, Settings
from gen_automation.db.models import (
    AdminEnrollment,
    AdminSession,
    AdminUser,
    AuditEvent,
)
from gen_automation.db.session import Database
from gen_automation.domain.enums import AdminEnrollmentState, AdminRole

SESSION_KEY = base64.urlsafe_b64encode(bytes(range(32))).rstrip(b"=").decode("ascii")
TOTP_KEY = base64.urlsafe_b64encode(bytes(range(32, 64))).rstrip(b"=").decode("ascii")
OWNER_PASSWORD = "a long unique enrollment API owner password"  # noqa: S105
ENROLLED_PASSWORD = "a long unique enrolled owner password"  # noqa: S105
ORIGIN = "http://testserver"


def _settings(database_path: Path) -> Settings:
    return Settings(
        environment=Environment.TEST,
        public_base_url=ORIGIN,
        database_url=f"sqlite+aiosqlite:///{database_path.as_posix()}",
        auth_enabled=True,
        auth_require_totp=True,
        session_secret=SESSION_KEY,
        auth_totp_active_key_id="key-1",
        auth_totp_encryption_keys={"key-1": TOTP_KEY},
        auth_enrollment_invite_ttl_seconds=3600,
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


def _totp_code(secret: str, unix_time: int) -> str:
    counter = unix_time // 30
    digest = hmac.new(
        base64.b32decode(secret),
        struct.pack(">Q", counter),
        hashlib.sha1,
    ).digest()
    offset = digest[-1] & 15
    value = int.from_bytes(digest[offset : offset + 4], "big") & 0x7FFFFFFF
    return f"{value % 1_000_000:06d}"


async def _prepare_owner(settings: Settings) -> str:
    database = Database(settings.database_url)
    try:
        await database.create_schema()
        secret = generate_totp_secret()
        now = datetime.now(UTC)
        async with database.sessions() as session:
            owner = AdminUser(
                username_normalized="owner@example.test",
                display_name="Owner",
                password_hash=_password_manager().hash(OWNER_PASSWORD),
                role=AdminRole.OWNER,
                is_active=True,
                failed_login_count=0,
                password_changed_at=now,
                credential_version=1,
                lock_version=1,
            )
            session.add(owner)
            await session.flush()
            owner.totp_secret_ciphertext = TotpSecretCipher(
                {"key-1": TOTP_KEY},
                active_key_id="key-1",
            ).encrypt(secret, subject=f"admin-user:{owner.id}")
            owner.totp_confirmed_at = now
            await session.commit()
        return secret
    finally:
        await database.dispose()


def _login(client: TestClient, owner_secret: str) -> str:
    response = client.post(
        "/api/v1/auth/login",
        json={
            "username": "owner@example.test",
            "password": OWNER_PASSWORD,
            "totp_code": _totp_code(
                owner_secret,
                int(datetime.now(UTC).timestamp()),
            ),
        },
        headers={"Origin": ORIGIN, "Sec-Fetch-Site": "same-origin"},
    )
    assert response.status_code == 200
    csrf = client.cookies.get("gen_csrf")
    assert csrf is not None
    return csrf


def test_owner_invites_and_capability_enrolls_second_owner(tmp_path: Path) -> None:
    settings = _settings(tmp_path / "enrollment-api.db")
    owner_secret = asyncio.run(_prepare_owner(settings))
    invitation_path = "/api/v1/auth/admin-enrollments/invitations"
    inspect_path = "/api/v1/auth/admin-enrollments/inspect"
    complete_path = "/api/v1/auth/admin-enrollments/complete"

    with TestClient(
        create_app(settings),
        base_url=ORIGIN,
        client=("192.0.2.20", 50000),
    ) as client:
        unauthenticated = client.post(
            invitation_path,
            json={
                "username": "second-owner@example.test",
                "display_name": "Second Owner",
                "role": "owner",
            },
            headers={"Origin": ORIGIN},
        )
        assert unauthenticated.status_code == 401

        csrf = _login(client, owner_secret)
        no_csrf = client.post(
            invitation_path,
            json={
                "username": "second-owner@example.test",
                "display_name": "Second Owner",
                "role": "owner",
            },
            headers={"Origin": ORIGIN},
        )
        assert no_csrf.status_code == 403
        wrong_origin = client.post(
            invitation_path,
            json={
                "username": "second-owner@example.test",
                "display_name": "Second Owner",
                "role": "owner",
            },
            headers={
                "Origin": "https://attacker.example",
                "Sec-Fetch-Site": "cross-site",
                "X-CSRF-Token": csrf,
            },
        )
        assert wrong_origin.status_code == 403
        non_json = client.post(
            invitation_path,
            content=json.dumps(
                {
                    "username": "second-owner@example.test",
                    "display_name": "Second Owner",
                    "role": "owner",
                }
            ),
            headers={
                "Content-Type": "text/plain",
                "Origin": ORIGIN,
                "X-CSRF-Token": csrf,
            },
        )
        assert non_json.status_code == 415

        invitation = client.post(
            invitation_path,
            json={
                "username": "second-owner@example.test",
                "display_name": "Second Owner",
                "role": "owner",
            },
            headers={
                "Origin": ORIGIN,
                "Sec-Fetch-Site": "same-origin",
                "X-CSRF-Token": csrf,
            },
        )
        assert invitation.status_code == 201
        invitation_body = invitation.json()
        token = invitation_body["invite_token"]
        assert len(token) == 43
        assert invitation_body["role"] == "owner"
        assert "totp_secret" not in invitation_body
        assert token not in str(invitation.request.url)
        assert invitation.headers["cache-control"] == "no-store"

        client.cookies.clear()
        missing_origin = client.post(inspect_path, json={"invite_token": token})
        assert missing_origin.status_code == 403
        inspection = client.post(
            inspect_path,
            json={"invite_token": token},
            headers={"Origin": ORIGIN},
        )
        assert inspection.status_code == 200
        inspection_body = inspection.json()
        totp_secret = inspection_body["totp_secret"]
        assert len(totp_secret) == 32
        assert f"secret={totp_secret}" in inspection_body["totp_provisioning_uri"]
        assert token not in inspection.text
        assert token not in str(inspection.request.url)

        complete = client.post(
            complete_path,
            json={
                "invite_token": token,
                "password": ENROLLED_PASSWORD,
                "totp_code": _totp_code(
                    totp_secret,
                    int(datetime.now(UTC).timestamp()),
                ),
            },
            headers={"Origin": ORIGIN},
        )
        assert complete.status_code == 201
        assert complete.json()["role"] == "owner"
        assert token not in complete.text
        assert totp_secret not in complete.text
        assert "password" not in complete.text.casefold()

        consumed = client.post(
            inspect_path,
            json={"invite_token": token},
            headers={"Origin": ORIGIN},
        )
        malformed = client.post(
            inspect_path,
            json={"invite_token": "malformed"},
            headers={"Origin": ORIGIN},
        )
        assert consumed.status_code == malformed.status_code == 400
        assert (
            consumed.json()
            == malformed.json()
            == {"detail": "administrator enrollment is invalid or expired"}
        )

    async def assert_database_state() -> None:
        database = Database(settings.database_url)
        try:
            async with database.sessions() as session:
                enrollment = await session.scalar(select(AdminEnrollment))
                second_owner = await session.scalar(
                    select(AdminUser).where(
                        AdminUser.username_normalized == "second-owner@example.test"
                    )
                )
                audits = list(
                    (
                        await session.scalars(
                            select(AuditEvent).where(AuditEvent.resource_type == "admin_enrollment")
                        )
                    ).all()
                )
                assert enrollment is not None
                assert enrollment.state == AdminEnrollmentState.CONSUMED
                assert enrollment.totp_secret_ciphertext is None
                assert second_owner is not None
                assert second_owner.role == AdminRole.OWNER
                assert second_owner.is_active
                audit_text = " ".join(str(audit.detail) for audit in audits)
                assert token not in audit_text
                assert totp_secret not in audit_text
                assert ENROLLED_PASSWORD not in audit_text
                assert enrollment.token_sha256 not in audit_text
        finally:
            await database.dispose()

    asyncio.run(assert_database_state())


def test_user_manager_requires_owner_and_recent_authentication(tmp_path: Path) -> None:
    settings = _settings(tmp_path / "user-manager-security.db")
    owner_secret = asyncio.run(_prepare_owner(settings))
    path = "/api/v1/auth/admin-enrollments/invitations"
    body = {
        "username": "admin@example.test",
        "display_name": "Admin",
        "role": "admin",
    }
    with TestClient(
        create_app(settings),
        base_url=ORIGIN,
        client=("192.0.2.21", 50000),
    ) as client:
        csrf = _login(client, owner_secret)

        async def make_session_stale() -> None:
            database = Database(settings.database_url)
            try:
                async with database.sessions() as session:
                    browser_session = await session.scalar(select(AdminSession))
                    assert browser_session is not None
                    old = datetime.now(UTC) - timedelta(hours=2)
                    browser_session.created_at = old
                    browser_session.reauthenticated_at = old
                    await session.commit()
            finally:
                await database.dispose()

        asyncio.run(make_session_stale())
        stale = client.post(
            path,
            json=body,
            headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
        )
        assert stale.status_code == 401
        assert stale.json()["detail"] == "recent authentication required"

    settings = _settings(tmp_path / "user-manager-role.db")
    owner_secret = asyncio.run(_prepare_owner(settings))
    with TestClient(
        create_app(settings),
        base_url=ORIGIN,
        client=("192.0.2.22", 50000),
    ) as client:
        csrf = _login(client, owner_secret)

        async def remove_owner_permission() -> None:
            database = Database(settings.database_url)
            try:
                async with database.sessions() as session:
                    owner = await session.scalar(select(AdminUser))
                    assert owner is not None
                    owner.role = AdminRole.ADMIN
                    await session.commit()
            finally:
                await database.dispose()

        asyncio.run(remove_owner_permission())
        forbidden = client.post(
            path,
            json=body,
            headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
        )
        assert forbidden.status_code == 403
        assert forbidden.json()["detail"] == "permission denied"
