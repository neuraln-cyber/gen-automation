import asyncio
import base64
import hashlib
import hmac
import struct
import threading
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from argon2 import PasswordHasher, Type
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.sql.dml import Update

from gen_automation.auth.security import (
    PasswordManager,
    PasswordVerification,
    TotpSecretCipher,
    generate_totp_secret,
)
from gen_automation.db.models import AdminSession, AdminUser, AuditEvent, LoginThrottle
from gen_automation.db.session import Database
from gen_automation.domain.enums import AdminRole
from gen_automation.services.authentication import (
    AuthenticationFailedError,
    AuthenticationPolicy,
    AuthenticationService,
    CsrfValidationError,
    SessionUnauthorizedError,
)

SESSION_KEY = base64.urlsafe_b64encode(bytes(range(32))).rstrip(b"=").decode("ascii")
TOTP_KEY = base64.urlsafe_b64encode(bytes(range(32, 64))).rstrip(b"=").decode("ascii")
NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
VALID_PASSWORD = "a long unique test passphrase"  # noqa: S105
INVALID_PASSWORD = "wrong password"  # noqa: S105
INVALID_CSRF = "different"


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


def _service(
    password_manager: Any,
    *,
    max_failures: int = 3,
    max_password_operations: int = 2,
    max_password_admissions: int | None = None,
) -> AuthenticationService:
    return AuthenticationService(
        password_manager=password_manager,
        totp_cipher=TotpSecretCipher({"totp-key-1": TOTP_KEY}, active_key_id="totp-key-1"),
        session_hmac_key=SESSION_KEY,
        policy=AuthenticationPolicy(
            require_totp=True,
            session_absolute_seconds=3600,
            session_idle_seconds=900,
            recent_auth_seconds=300,
            login_window_seconds=300,
            login_max_failures=max_failures,
            login_lockout_seconds=300,
        ),
        max_password_operations=max_password_operations,
        max_password_admissions=max_password_admissions,
    )


def _totp_code(secret: str, at: datetime) -> str:
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
async def auth_database(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'auth.db').as_posix()}")
    await database.create_schema()
    try:
        yield database
    finally:
        await database.dispose()


async def _seed_user(
    database: Database,
    *,
    password_manager: PasswordManager,
) -> tuple[AdminUser, str]:
    secret = generate_totp_secret()
    async with database.sessions() as session:
        user = AdminUser(
            username_normalized="owner@example.test",
            display_name="Owner",
            password_hash=password_manager.hash(VALID_PASSWORD),
            role=AdminRole.OWNER,
            is_active=True,
            failed_login_count=0,
            password_changed_at=NOW,
            credential_version=1,
            lock_version=1,
        )
        session.add(user)
        await session.flush()
        cipher = TotpSecretCipher({"totp-key-1": TOTP_KEY}, active_key_id="totp-key-1")
        user.totp_secret_ciphertext = cipher.encrypt(
            secret,
            subject=f"admin-user:{user.id}",
        )
        user.totp_confirmed_at = NOW
        await session.commit()
        return user, secret


async def test_login_creates_server_side_session_and_resolves_csrf(
    auth_database: Database,
) -> None:
    manager = _password_manager()
    service = _service(manager)
    user, secret = await _seed_user(auth_database, password_manager=manager)

    async with auth_database.sessions() as session:
        result = await service.login(
            session,
            username=" OWNER@example.test ",
            password=VALID_PASSWORD,
            totp_code=_totp_code(secret, NOW),
            client_context="192.0.2.1",
            user_agent="test-browser",
            correlation_id="login-success",
            now=NOW,
        )

    assert result.user_id == user.id
    assert result.mfa_verified
    assert result.session_token not in repr(result.__dict__.get("password", ""))
    async with auth_database.sessions() as session:
        stored = await session.get(AdminSession, result.session_id)
        assert stored is not None
        assert result.session_token not in stored.token_sha256
        assert result.csrf_token not in stored.csrf_sha256
        principal = await service.resolve_session(
            session,
            session_token=result.session_token,
            now=NOW + timedelta(seconds=61),
        )

    assert principal.user_id == user.id
    service.validate_csrf(
        principal,
        cookie_token=result.csrf_token,
        header_token=result.csrf_token,
    )
    with pytest.raises(CsrfValidationError):
        service.validate_csrf(
            principal,
            cookie_token=result.csrf_token,
            header_token=INVALID_CSRF,
        )


async def test_reauthentication_refreshes_existing_session_and_audits_outcome(
    auth_database: Database,
) -> None:
    manager = _password_manager()
    service = _service(manager)
    _user, secret = await _seed_user(auth_database, password_manager=manager)
    async with auth_database.sessions() as session:
        login = await service.login(
            session,
            username="owner@example.test",
            password=VALID_PASSWORD,
            totp_code=_totp_code(secret, NOW),
            client_context="192.0.2.1",
            user_agent=None,
            correlation_id="initial-login",
            now=NOW,
        )
    async with auth_database.sessions() as session:
        principal = await service.resolve_session(
            session,
            session_token=login.session_token,
            now=NOW,
        )

    reauthenticated_at = NOW + timedelta(seconds=30)
    async with auth_database.sessions() as session:
        refreshed = await service.reauthenticate_session(
            session,
            principal=principal,
            password=VALID_PASSWORD,
            totp_code=_totp_code(secret, reauthenticated_at),
            correlation_id="reauth-success",
            now=reauthenticated_at,
        )

    assert refreshed.session_id == login.session_id
    assert refreshed.reauthenticated_at == reauthenticated_at
    assert refreshed.mfa_verified_at == reauthenticated_at
    async with auth_database.sessions() as session:
        session_count = await session.scalar(select(func.count()).select_from(AdminSession))
        stored = await session.get(AdminSession, login.session_id)
        success_event = await session.scalar(
            select(AuditEvent).where(
                AuditEvent.action == "auth.session_reauthenticated",
                AuditEvent.correlation_id == "reauth-success",
            )
        )
    assert session_count == 1
    assert stored is not None
    assert stored.reauthenticated_at.replace(tzinfo=UTC) == reauthenticated_at
    assert stored.mfa_verified_at is not None
    assert stored.mfa_verified_at.replace(tzinfo=UTC) == reauthenticated_at
    assert success_event is not None
    assert success_event.detail == {"mfa_verified": True}

    failed_at = reauthenticated_at + timedelta(seconds=30)
    async with auth_database.sessions() as session:
        with pytest.raises(AuthenticationFailedError, match="authentication failed"):
            await service.reauthenticate_session(
                session,
                principal=refreshed,
                password=INVALID_PASSWORD,
                totp_code=_totp_code(secret, failed_at),
                correlation_id="reauth-failed",
                now=failed_at,
            )
    async with auth_database.sessions() as session:
        session_count = await session.scalar(select(func.count()).select_from(AdminSession))
        stored = await session.get(AdminSession, login.session_id)
        failure_event = await session.scalar(
            select(AuditEvent).where(
                AuditEvent.action == "auth.reauthentication_failed",
                AuditEvent.correlation_id == "reauth-failed",
            )
        )
    assert session_count == 1
    assert stored is not None
    assert stored.reauthenticated_at.replace(tzinfo=UTC) == reauthenticated_at
    assert failure_event is not None
    assert failure_event.detail == {"reason": "credentials_or_mfa"}
    assert INVALID_PASSWORD not in repr(failure_event.detail)


async def test_login_rejects_replayed_totp_counter(
    auth_database: Database,
) -> None:
    manager = _password_manager()
    service = _service(manager)
    _user, secret = await _seed_user(auth_database, password_manager=manager)
    code = _totp_code(secret, NOW)

    async with auth_database.sessions() as session:
        await service.login(
            session,
            username="owner@example.test",
            password=VALID_PASSWORD,
            totp_code=code,
            client_context="192.0.2.1",
            user_agent=None,
            correlation_id="first-login",
            now=NOW,
        )
    async with auth_database.sessions() as session:
        with pytest.raises(AuthenticationFailedError):
            await service.login(
                session,
                username="owner@example.test",
                password=VALID_PASSWORD,
                totp_code=code,
                client_context="192.0.2.2",
                user_agent=None,
                correlation_id="replay-login",
                now=NOW,
            )


async def test_lost_totp_counter_claim_is_audited_without_secret_material(
    auth_database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _password_manager()
    service = _service(manager)
    user, secret = await _seed_user(auth_database, password_manager=manager)

    async with auth_database.sessions() as session:
        original_scalar = session.scalar

        async def reject_atomic_claim(
            statement: Any,
            *args: Any,
            **kwargs: Any,
        ) -> Any:
            if isinstance(statement, Update):
                return None
            return await original_scalar(statement, *args, **kwargs)

        monkeypatch.setattr(session, "scalar", reject_atomic_claim)
        with pytest.raises(AuthenticationFailedError):
            await service.login(
                session,
                username=user.username_normalized,
                password=VALID_PASSWORD,
                totp_code=_totp_code(secret, NOW),
                client_context="192.0.2.44",
                user_agent=None,
                correlation_id="lost-counter-claim",
                now=NOW,
            )

    async with auth_database.sessions() as session:
        event = await session.scalar(
            select(AuditEvent).where(
                AuditEvent.action == "auth.totp_counter_claim_rejected",
                AuditEvent.correlation_id == "lost-counter-claim",
            )
        )
    assert event is not None
    assert event.resource_id == user.id
    assert set(event.detail) == {"throttle_key_sha256"}
    serialized_detail = repr(event.detail)
    assert secret not in serialized_detail
    assert _totp_code(secret, NOW) not in serialized_detail


async def test_failed_logins_are_generic_and_durably_throttled(
    auth_database: Database,
) -> None:
    manager = _password_manager()
    service = _service(manager)
    await _seed_user(auth_database, password_manager=manager)

    for attempt in range(3):
        async with auth_database.sessions() as session:
            with pytest.raises(AuthenticationFailedError, match="authentication failed"):
                await service.login(
                    session,
                    username="owner@example.test",
                    password=INVALID_PASSWORD,
                    totp_code="000000",
                    client_context="192.0.2.1",
                    user_agent=None,
                    correlation_id=f"failed-{attempt}",
                    now=NOW,
                )

    async with auth_database.sessions() as session:
        with pytest.raises(AuthenticationFailedError, match="authentication failed"):
            await service.login(
                session,
                username="a-different-name@example.test",
                password=INVALID_PASSWORD,
                totp_code="000000",
                client_context="192.0.2.1",
                user_agent=None,
                correlation_id="context-blocked",
                now=NOW,
            )

    async with auth_database.sessions() as session:
        user = await session.scalar(select(AdminUser))
        throttle = await session.scalar(select(LoginThrottle))
        blocked_event = await session.scalar(
            select(AuditEvent).where(AuditEvent.action == "auth.login_blocked")
        )
        assert user is not None and user.locked_until is None
        assert user.failed_login_count == 3
        assert throttle is not None and throttle.blocked_until is not None
        assert throttle.blocked_until.replace(tzinfo=UTC) == NOW + timedelta(minutes=5)
        assert blocked_event is not None
        assert blocked_event.resource_id == throttle.id
        assert set(blocked_event.detail) == {"throttle_key_sha256"}


async def test_fake_usernames_share_context_throttle_and_cleanup_is_bounded(
    auth_database: Database,
) -> None:
    manager = _password_manager()
    service = _service(manager, max_failures=20)

    for attempt in range(10):
        async with auth_database.sessions() as session:
            with pytest.raises(AuthenticationFailedError):
                await service.login(
                    session,
                    username=f"missing-{attempt}@example.test",
                    password=INVALID_PASSWORD,
                    totp_code="000000",
                    client_context="198.51.100.24",
                    user_agent=None,
                    correlation_id=f"fake-user-{attempt}",
                    now=NOW,
                )

    stale_at = NOW - timedelta(seconds=601)
    async with auth_database.sessions() as session:
        for index in range(40):
            session.add(
                LoginThrottle(
                    key_sha256=hashlib.sha256(f"stale-{index}".encode()).hexdigest(),
                    failure_count=1,
                    window_started_at=stale_at,
                    blocked_until=None,
                    updated_at=stale_at,
                )
            )
        await session.commit()

    async with auth_database.sessions() as session:
        with pytest.raises(AuthenticationFailedError):
            await service.login(
                session,
                username="one-more-missing-user@example.test",
                password=INVALID_PASSWORD,
                totp_code="000000",
                client_context="198.51.100.24",
                user_agent=None,
                correlation_id="cleanup-trigger",
                now=NOW,
            )

    async with auth_database.sessions() as session:
        throttle_count = await session.scalar(select(func.count()).select_from(LoginThrottle))
        active_throttle = await session.scalar(
            select(LoginThrottle).where(LoginThrottle.updated_at == NOW)
        )
        stale_count = await session.scalar(
            select(func.count())
            .select_from(LoginThrottle)
            .where(LoginThrottle.updated_at == stale_at)
        )

    assert throttle_count == 9
    assert active_throttle is not None
    assert active_throttle.failure_count == 11
    assert stale_count == 8


async def test_opportunistic_session_cleanup_is_bounded_and_preserves_active_session(
    auth_database: Database,
) -> None:
    manager = _password_manager()
    service = _service(manager, max_failures=20)
    user, _secret = await _seed_user(auth_database, password_manager=manager)

    def browser_session(
        index: int,
        *,
        created_at: datetime,
        expires_at: datetime,
        idle_expires_at: datetime,
        revoked_at: datetime | None = None,
    ) -> AdminSession:
        return AdminSession(
            user_id=user.id,
            token_sha256=hashlib.sha256(f"session-{index}".encode()).hexdigest(),
            csrf_sha256=hashlib.sha256(f"csrf-{index}".encode()).hexdigest(),
            credential_version=1,
            created_at=created_at,
            last_seen_at=created_at,
            expires_at=expires_at,
            idle_expires_at=idle_expires_at,
            reauthenticated_at=created_at,
            revoked_at=revoked_at,
        )

    old_created_at = NOW - timedelta(days=40)
    async with auth_database.sessions() as session:
        for index in range(15):
            session.add(
                browser_session(
                    index,
                    created_at=old_created_at,
                    expires_at=NOW - timedelta(days=31),
                    idle_expires_at=NOW - timedelta(days=32),
                )
            )
        for index in range(15, 30):
            session.add(
                browser_session(
                    index,
                    created_at=old_created_at,
                    expires_at=NOW + timedelta(hours=1),
                    idle_expires_at=NOW + timedelta(minutes=15),
                    revoked_at=NOW - timedelta(days=31),
                )
            )
        for index in range(30, 40):
            session.add(
                browser_session(
                    index,
                    created_at=old_created_at,
                    expires_at=NOW + timedelta(hours=1),
                    idle_expires_at=NOW - timedelta(days=31),
                )
            )
        active = browser_session(
            40,
            created_at=NOW - timedelta(minutes=5),
            expires_at=NOW + timedelta(hours=1),
            idle_expires_at=NOW + timedelta(minutes=15),
        )
        session.add(active)
        recently_revoked = browser_session(
            41,
            created_at=NOW - timedelta(days=1),
            expires_at=NOW + timedelta(hours=1),
            idle_expires_at=NOW + timedelta(minutes=15),
            revoked_at=NOW - timedelta(minutes=1),
        )
        session.add(recently_revoked)
        await session.commit()
        active_id = active.id
        recently_revoked_id = recently_revoked.id

    async with auth_database.sessions() as session:
        with pytest.raises(AuthenticationFailedError):
            await service.login(
                session,
                username="maintenance-trigger@example.test",
                password=INVALID_PASSWORD,
                totp_code="000000",
                client_context="198.51.100.88",
                user_agent=None,
                correlation_id="session-cleanup",
                now=NOW,
            )

    async with auth_database.sessions() as session:
        remaining_count = await session.scalar(select(func.count()).select_from(AdminSession))
        purgeable_count = await session.scalar(
            select(func.count())
            .select_from(AdminSession)
            .where(
                or_(
                    AdminSession.revoked_at <= NOW - timedelta(days=30),
                    AdminSession.expires_at <= NOW - timedelta(days=30),
                    AdminSession.idle_expires_at <= NOW - timedelta(days=30),
                )
            )
        )
        active = await session.get(AdminSession, active_id)
        recently_revoked = await session.get(AdminSession, recently_revoked_id)

    assert remaining_count == 10
    assert purgeable_count == 8
    assert active is not None
    assert active.revoked_at is None
    assert active.expires_at.replace(tzinfo=UTC) > NOW
    assert active.idle_expires_at.replace(tzinfo=UTC) > NOW
    assert recently_revoked is not None
    assert recently_revoked.revoked_at is not None
    assert recently_revoked.revoked_at.replace(tzinfo=UTC) == NOW - timedelta(minutes=1)


async def test_failures_do_not_create_or_extend_legacy_user_lock(
    auth_database: Database,
) -> None:
    manager = _password_manager()
    service = _service(manager, max_failures=20)
    user, _secret = await _seed_user(auth_database, password_manager=manager)
    legacy_lock_expiry = NOW + timedelta(minutes=10)

    async with auth_database.sessions() as session:
        stored = await session.get(AdminUser, user.id)
        assert stored is not None
        stored.failed_login_count = 3
        stored.failed_login_window_started_at = NOW
        stored.locked_until = legacy_lock_expiry
        await session.commit()

    for attempt_at in (NOW, NOW + timedelta(minutes=1)):
        async with auth_database.sessions() as session:
            with pytest.raises(AuthenticationFailedError):
                await service.login(
                    session,
                    username=user.username_normalized,
                    password=INVALID_PASSWORD,
                    totp_code="000000",
                    client_context=f"203.0.113.{attempt_at.minute + 1}",
                    user_agent=None,
                    correlation_id=f"legacy-lock-{attempt_at.minute}",
                    now=attempt_at,
                )

    async with auth_database.sessions() as session:
        stored = await session.get(AdminUser, user.id)
        assert stored is not None
        assert stored.locked_until is not None
        assert stored.locked_until.replace(tzinfo=UTC) == legacy_lock_expiry

    after_expiry = legacy_lock_expiry + timedelta(seconds=1)
    async with auth_database.sessions() as session:
        with pytest.raises(AuthenticationFailedError):
            await service.login(
                session,
                username=user.username_normalized,
                password=INVALID_PASSWORD,
                totp_code="000000",
                client_context="203.0.113.250",
                user_agent=None,
                correlation_id="legacy-lock-expired",
                now=after_expiry,
            )
    async with auth_database.sessions() as session:
        stored = await session.get(AdminUser, user.id)
        assert stored is not None
        assert stored.locked_until is None
        assert stored.failed_login_count == 1


class _BlockingPasswordManager:
    def __init__(self) -> None:
        self.first_started = threading.Event()
        self.release_first = threading.Event()
        self.second_started = threading.Event()
        self._call_lock = threading.Lock()
        self.calls = 0
        self.active = 0
        self.maximum_active = 0

    def verify(self, encoded_hash: str | None, password: str) -> PasswordVerification:
        del encoded_hash, password
        with self._call_lock:
            self.calls += 1
            call_number = self.calls
            self.active += 1
            self.maximum_active = max(self.maximum_active, self.active)
        try:
            if call_number == 1:
                self.first_started.set()
                if not self.release_first.wait(timeout=5):
                    raise RuntimeError("test password verifier timed out")
            else:
                self.second_started.set()
            return PasswordVerification(valid=False)
        finally:
            with self._call_lock:
                self.active -= 1


async def test_cancelled_login_holds_argon_capacity_until_thread_finishes(
    auth_database: Database,
) -> None:
    manager = _BlockingPasswordManager()
    service = _service(
        manager,
        max_failures=20,
        max_password_operations=1,
        max_password_admissions=2,
    )

    async def attempt(correlation_id: str, client_context: str) -> None:
        async with auth_database.sessions() as session:
            await service.login(
                session,
                username="missing@example.test",
                password=INVALID_PASSWORD,
                totp_code="000000",
                client_context=client_context,
                user_agent=None,
                correlation_id=correlation_id,
                now=NOW,
            )

    first = asyncio.create_task(attempt("cancel-first", "192.0.2.10"))
    assert await asyncio.to_thread(manager.first_started.wait, 1)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    second = asyncio.create_task(attempt("queued-second", "192.0.2.11"))
    loop = asyncio.get_running_loop()
    admission_deadline = loop.time() + 1
    while service._password_admissions != 2:
        assert not second.done()
        if loop.time() >= admission_deadline:
            pytest.fail("second request did not reach the bounded password queue")
        await asyncio.sleep(0.001)
    assert not manager.second_started.is_set()

    with pytest.raises(AuthenticationFailedError):
        await attempt("admission-rejected", "192.0.2.12")
    assert manager.calls == 1

    manager.release_first.set()
    with pytest.raises(AuthenticationFailedError):
        await second

    assert manager.second_started.is_set()
    assert manager.calls == 2
    assert manager.maximum_active == 1


async def test_credential_version_and_logout_revoke_sessions(
    auth_database: Database,
) -> None:
    manager = _password_manager()
    service = _service(manager)
    _user, secret = await _seed_user(auth_database, password_manager=manager)
    async with auth_database.sessions() as session:
        result = await service.login(
            session,
            username="owner@example.test",
            password=VALID_PASSWORD,
            totp_code=_totp_code(secret, NOW),
            client_context="192.0.2.1",
            user_agent=None,
            correlation_id="login",
            now=NOW,
        )
    async with auth_database.sessions() as session:
        principal = await service.resolve_session(
            session,
            session_token=result.session_token,
            now=NOW,
        )
        await service.logout(
            session,
            principal=principal,
            correlation_id="logout",
            now=NOW,
        )
    async with auth_database.sessions() as session:
        with pytest.raises(SessionUnauthorizedError):
            await service.resolve_session(
                session,
                session_token=result.session_token,
                now=NOW,
            )

    next_time = NOW + timedelta(seconds=30)
    async with auth_database.sessions() as session:
        user = await session.scalar(select(AdminUser))
        assert user is not None
        user.last_totp_counter = None
        await session.commit()
    async with auth_database.sessions() as session:
        replacement = await service.login(
            session,
            username="owner@example.test",
            password=VALID_PASSWORD,
            totp_code=_totp_code(secret, next_time),
            client_context="192.0.2.1",
            user_agent=None,
            correlation_id="replacement-login",
            now=next_time,
        )
    async with auth_database.sessions() as session:
        user = await session.scalar(select(AdminUser).with_for_update())
        assert user is not None
        user.credential_version += 1
        await session.commit()
    async with auth_database.sessions() as session:
        with pytest.raises(SessionUnauthorizedError):
            await service.resolve_session(
                session,
                session_token=replacement.session_token,
                now=next_time,
            )


async def test_sqlite_test_database_enforces_auth_foreign_keys(
    auth_database: Database,
) -> None:
    manager = _password_manager()
    user, _secret = await _seed_user(auth_database, password_manager=manager)

    async with auth_database.sessions() as session:
        stored = await session.get(AdminUser, user.id)
        assert stored is not None
        await session.delete(stored)
        await session.commit()

    async with auth_database.sessions() as session:
        assert await session.get(AdminUser, user.id) is None

    # A dangling session cannot be created even under the local SQLite test backend.
    async with auth_database.sessions() as session:
        session.add(
            AdminSession(
                user_id=user.id,
                token_sha256="a" * 64,
                csrf_sha256="b" * 64,
                credential_version=1,
                created_at=NOW,
                last_seen_at=NOW,
                expires_at=NOW + timedelta(hours=1),
                idle_expires_at=NOW + timedelta(minutes=15),
                reauthenticated_at=NOW,
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()
