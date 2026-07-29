import asyncio
import hashlib
import hmac
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import delete, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from gen_automation.auth.security import (
    PasswordManager,
    PasswordVerification,
    SecretEncryptionError,
    TotpSecretCipher,
    generate_opaque_token,
    hash_opaque_token,
    verify_opaque_token,
    verify_totp,
)
from gen_automation.db.models import (
    AdminSession,
    AdminUser,
    AuditEvent,
    LoginThrottle,
)
from gen_automation.domain.enums import AdminRole

_DUMMY_TOKEN_SHA256 = hashlib.sha256(b"invalid-session-token").hexdigest()
_SESSION_TOUCH_INTERVAL_SECONDS = 60
_THROTTLE_PURGE_BATCH_SIZE = 32
_SESSION_PURGE_BATCH_SIZE = 32
_SESSION_SECURITY_RETENTION = timedelta(days=30)
_DEFAULT_PASSWORD_ADMISSIONS_PER_OPERATION = 4
_MAX_PASSWORD_ADMISSIONS = 64


class AuthenticationFailedError(ValueError):
    """Credentials, MFA, or throttling did not permit a login."""


class SessionUnauthorizedError(ValueError):
    """The opaque browser session is absent, expired, or revoked."""


class CsrfValidationError(ValueError):
    """The state-changing request did not prove same-session intent."""


class RecentAuthenticationRequiredError(ValueError):
    """A sensitive action requires a fresh password and MFA check."""


@dataclass(frozen=True)
class AuthenticationPolicy:
    require_totp: bool
    session_absolute_seconds: int
    session_idle_seconds: int
    recent_auth_seconds: int
    login_window_seconds: int
    login_max_failures: int
    login_lockout_seconds: int

    def __post_init__(self) -> None:
        if not 900 <= self.session_absolute_seconds <= 7 * 86400:
            raise ValueError("absolute session duration is invalid")
        if not 300 <= self.session_idle_seconds <= self.session_absolute_seconds:
            raise ValueError("idle session duration is invalid")
        if not 60 <= self.recent_auth_seconds <= self.session_idle_seconds:
            raise ValueError("recent authentication duration is invalid")
        if not 60 <= self.login_window_seconds <= 3600:
            raise ValueError("login window is invalid")
        if not 3 <= self.login_max_failures <= 20:
            raise ValueError("login failure limit is invalid")
        if not 60 <= self.login_lockout_seconds <= 86400:
            raise ValueError("login lockout duration is invalid")


@dataclass(frozen=True)
class LoginResult:
    session_token: str
    csrf_token: str
    session_id: UUID
    user_id: UUID
    username: str
    display_name: str
    role: AdminRole
    expires_at: datetime
    idle_expires_at: datetime
    mfa_verified: bool


@dataclass(frozen=True)
class AuthenticatedPrincipal:
    session_id: UUID
    user_id: UUID
    username: str
    display_name: str
    role: AdminRole
    csrf_sha256: str
    expires_at: datetime
    idle_expires_at: datetime
    reauthenticated_at: datetime
    mfa_verified_at: datetime | None


def normalize_username(username: str) -> str:
    normalized = unicodedata.normalize("NFKC", username).casefold().strip()
    if not 1 <= len(normalized) <= 200 or any(ord(character) < 0x20 for character in normalized):
        raise AuthenticationFailedError("authentication failed")
    return normalized


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class _PasswordAdmissionLease:
    def __init__(self, release: Callable[[], None]) -> None:
        self._release = release
        self.delegated_to_worker = False
        self._released = False

    def delegate_to_worker(self) -> None:
        self.delegated_to_worker = True

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._release()


class AuthenticationService:
    """Database-backed password, TOTP, session, CSRF, and throttle service."""

    def __init__(
        self,
        *,
        password_manager: PasswordManager,
        totp_cipher: TotpSecretCipher,
        session_hmac_key: str,
        policy: AuthenticationPolicy,
        max_password_operations: int = 2,
        max_password_admissions: int | None = None,
    ) -> None:
        if not 1 <= max_password_operations <= 8:
            raise ValueError("password operation concurrency is invalid")
        password_admission_limit = (
            max_password_operations * _DEFAULT_PASSWORD_ADMISSIONS_PER_OPERATION
            if max_password_admissions is None
            else max_password_admissions
        )
        if not 1 <= password_admission_limit <= _MAX_PASSWORD_ADMISSIONS:
            raise ValueError("password operation admission limit is invalid")
        self._password_manager = password_manager
        self._totp_cipher = totp_cipher
        self._session_hmac_key = session_hmac_key.encode("ascii")
        self.policy = policy
        self._password_semaphore = asyncio.Semaphore(max_password_operations)
        self._password_admission_limit = password_admission_limit
        self._password_admissions = 0
        self._password_tasks: set[asyncio.Task[PasswordVerification]] = set()

    def _context_digest(self, purpose: str, *parts: str) -> str:
        payload = "\0".join((purpose, *parts)).encode("utf-8")
        return hmac.new(self._session_hmac_key, payload, hashlib.sha256).hexdigest()

    async def _run_opportunistic_maintenance(
        self,
        session: AsyncSession,
        *,
        now: datetime,
    ) -> None:
        retention_seconds = max(
            self.policy.login_window_seconds,
            self.policy.login_lockout_seconds,
        )
        stale_before = now - timedelta(seconds=retention_seconds)
        throttle_candidate = aliased(LoginThrottle)
        stale_throttle_ids = (
            select(throttle_candidate.id)
            .where(
                throttle_candidate.updated_at < stale_before,
                or_(
                    throttle_candidate.blocked_until.is_(None),
                    throttle_candidate.blocked_until <= now,
                ),
            )
            .order_by(throttle_candidate.updated_at, throttle_candidate.id)
            .limit(_THROTTLE_PURGE_BATCH_SIZE)
        )
        await session.execute(
            delete(LoginThrottle).where(
                LoginThrottle.id.in_(stale_throttle_ids),
                LoginThrottle.updated_at < stale_before,
                or_(
                    LoginThrottle.blocked_until.is_(None),
                    LoginThrottle.blocked_until <= now,
                ),
            )
        )

        terminal_before = now - _SESSION_SECURITY_RETENTION
        session_candidate = aliased(AdminSession)
        purgeable_session_ids = (
            select(session_candidate.id)
            .where(
                or_(
                    session_candidate.revoked_at <= terminal_before,
                    session_candidate.expires_at <= terminal_before,
                    session_candidate.idle_expires_at <= terminal_before,
                )
            )
            .order_by(session_candidate.expires_at, session_candidate.id)
            .limit(_SESSION_PURGE_BATCH_SIZE)
        )
        await session.execute(
            delete(AdminSession).where(
                AdminSession.id.in_(purgeable_session_ids),
                or_(
                    AdminSession.revoked_at <= terminal_before,
                    AdminSession.expires_at <= terminal_before,
                    AdminSession.idle_expires_at <= terminal_before,
                ),
            )
        )

    async def _get_or_create_throttle(
        self,
        session: AsyncSession,
        *,
        key_sha256: str,
        now: datetime,
    ) -> LoginThrottle:
        throttle = await session.scalar(
            select(LoginThrottle).where(LoginThrottle.key_sha256 == key_sha256).with_for_update()
        )
        if throttle is not None:
            return throttle
        candidate = LoginThrottle(
            key_sha256=key_sha256,
            failure_count=0,
            window_started_at=now,
            blocked_until=None,
            updated_at=now,
        )
        try:
            async with session.begin_nested():
                session.add(candidate)
                await session.flush()
            return candidate
        except IntegrityError:
            resolved_throttle: LoginThrottle | None = await session.scalar(
                select(LoginThrottle)
                .where(LoginThrottle.key_sha256 == key_sha256)
                .with_for_update()
            )
            if resolved_throttle is None:
                raise
            return resolved_throttle

    def _reset_expired_throttle(self, throttle: LoginThrottle, now: datetime) -> None:
        window_started_at = _as_utc(throttle.window_started_at)
        if now >= window_started_at + timedelta(seconds=self.policy.login_window_seconds):
            throttle.failure_count = 0
            throttle.window_started_at = now
            throttle.blocked_until = None
        elif throttle.blocked_until is not None and now >= _as_utc(throttle.blocked_until):
            throttle.blocked_until = None

    def _reset_expired_user_failures(self, user: AdminUser, now: datetime) -> None:
        if user.locked_until is not None:
            if now < _as_utc(user.locked_until):
                return
            user.failed_login_count = 0
            user.failed_login_window_started_at = None
            user.locked_until = None
        started_at = user.failed_login_window_started_at
        if started_at is not None and now >= _as_utc(started_at) + timedelta(
            seconds=self.policy.login_window_seconds
        ):
            user.failed_login_count = 0
            user.failed_login_window_started_at = None

    def _record_failure(
        self,
        *,
        throttle: LoginThrottle,
        user: AdminUser | None,
        now: datetime,
    ) -> None:
        if throttle.failure_count == 0:
            throttle.window_started_at = now
        throttle.failure_count += 1
        throttle.updated_at = now
        if throttle.failure_count >= self.policy.login_max_failures:
            throttle.blocked_until = now + timedelta(seconds=self.policy.login_lockout_seconds)
        if user is None:
            return
        if user.failed_login_count == 0:
            user.failed_login_window_started_at = now
        user.failed_login_count += 1
        user.lock_version += 1

    def _try_admit_password_operation(self) -> bool:
        if self._password_admissions >= self._password_admission_limit:
            return False
        self._password_admissions += 1
        return True

    def _release_password_admission(self) -> None:
        if self._password_admissions <= 0:
            raise RuntimeError("password operation admission accounting underflow")
        self._password_admissions -= 1

    async def _verify_password(
        self,
        encoded_hash: str | None,
        password: str,
        admission: _PasswordAdmissionLease,
    ) -> PasswordVerification:
        await self._password_semaphore.acquire()

        try:
            task = asyncio.create_task(
                asyncio.to_thread(
                    self._password_manager.verify,
                    encoded_hash,
                    password,
                )
            )
        except BaseException:
            self._password_semaphore.release()
            raise

        self._password_tasks.add(task)
        admission.delegate_to_worker()

        def release_capacity(done_task: asyncio.Task[PasswordVerification]) -> None:
            self._password_tasks.discard(done_task)
            if not done_task.cancelled():
                done_task.exception()
            self._password_semaphore.release()
            admission.release()

        task.add_done_callback(release_capacity)
        # Shielding leaves the executor-backed task alive when the request is
        # cancelled. Its callback releases capacity only after Argon2 returns.
        return await asyncio.shield(task)

    async def login(
        self,
        session: AsyncSession,
        *,
        username: str,
        password: str,
        totp_code: str | None,
        client_context: str,
        user_agent: str | None,
        correlation_id: str,
        now: datetime | None = None,
    ) -> LoginResult:
        observed_at = _as_utc(now or datetime.now(UTC))
        normalized_username = normalize_username(username)
        if not self._try_admit_password_operation():
            raise AuthenticationFailedError("authentication failed")
        admission = _PasswordAdmissionLease(self._release_password_admission)
        try:
            throttle_key = self._context_digest(
                "login-throttle",
                client_context[:200],
            )
            await self._run_opportunistic_maintenance(session, now=observed_at)
            throttle = await self._get_or_create_throttle(
                session,
                key_sha256=throttle_key,
                now=observed_at,
            )
            self._reset_expired_throttle(throttle, observed_at)
            if throttle.blocked_until is not None and observed_at < _as_utc(throttle.blocked_until):
                await session.commit()
                raise AuthenticationFailedError("authentication failed")

            user = await session.scalar(
                select(AdminUser)
                .where(AdminUser.username_normalized == normalized_username)
                .with_for_update()
            )
            if user is not None:
                self._reset_expired_user_failures(user, observed_at)
            user_is_locked = (
                user is not None
                and user.locked_until is not None
                and observed_at < _as_utc(user.locked_until)
            )
            verification = await self._verify_password(
                (
                    user.password_hash
                    if user is not None and user.is_active and not user_is_locked
                    else None
                ),
                password,
                admission,
            )

            matched_counter: int | None = None
            mfa_required = self.policy.require_totp or (
                user is not None and user.totp_confirmed_at is not None
            )
            valid = bool(user is not None and user.is_active and not user_is_locked)
            valid = valid and verification.valid
            if valid and mfa_required:
                if (
                    user is None
                    or user.totp_confirmed_at is None
                    or user.totp_secret_ciphertext is None
                    or totp_code is None
                ):
                    valid = False
                else:
                    try:
                        totp_secret = self._totp_cipher.decrypt(
                            user.totp_secret_ciphertext,
                            subject=f"admin-user:{user.id}",
                        )
                    except SecretEncryptionError:
                        valid = False
                    else:
                        matched_counter = verify_totp(
                            totp_secret,
                            totp_code,
                            unix_time=int(observed_at.timestamp()),
                            last_used_counter=user.last_totp_counter,
                        )
                        valid = matched_counter is not None

            if not valid or user is None:
                self._record_failure(
                    throttle=throttle,
                    user=user,
                    now=observed_at,
                )
                if throttle.failure_count == self.policy.login_max_failures:
                    session.add(
                        AuditEvent(
                            actor="anonymous",
                            action="auth.login_blocked",
                            resource_type="login_throttle",
                            resource_id=throttle.id,
                            correlation_id=correlation_id,
                            detail={"throttle_key_sha256": throttle_key},
                            occurred_at=observed_at,
                        )
                    )
                session.add(
                    AuditEvent(
                        actor="anonymous",
                        action="auth.login_failed",
                        resource_type="admin_user",
                        resource_id=user.id if user is not None else UUID(int=0),
                        correlation_id=correlation_id,
                        detail={"throttle_key_sha256": throttle_key},
                        occurred_at=observed_at,
                    )
                )
                await session.commit()
                raise AuthenticationFailedError("authentication failed")

            if matched_counter is not None:
                claimed_user_id = await session.scalar(
                    update(AdminUser)
                    .where(
                        AdminUser.id == user.id,
                        or_(
                            AdminUser.last_totp_counter.is_(None),
                            AdminUser.last_totp_counter < matched_counter,
                        ),
                    )
                    .values(last_totp_counter=matched_counter)
                    .returning(AdminUser.id)
                )
                if claimed_user_id is None:
                    self._record_failure(
                        throttle=throttle,
                        user=user,
                        now=observed_at,
                    )
                    session.add(
                        AuditEvent(
                            actor="anonymous",
                            action="auth.totp_counter_claim_rejected",
                            resource_type="admin_user",
                            resource_id=user.id,
                            correlation_id=correlation_id,
                            detail={"throttle_key_sha256": throttle_key},
                            occurred_at=observed_at,
                        )
                    )
                    await session.commit()
                    raise AuthenticationFailedError("authentication failed")
            if verification.replacement_hash is not None:
                user.password_hash = verification.replacement_hash
            user.failed_login_count = 0
            user.failed_login_window_started_at = None
            user.locked_until = None
            user.last_login_at = observed_at
            user.lock_version += 1
            await session.delete(throttle)

            session_token = generate_opaque_token()
            csrf_token = generate_opaque_token()
            expires_at = observed_at + timedelta(seconds=self.policy.session_absolute_seconds)
            idle_expires_at = min(
                expires_at,
                observed_at + timedelta(seconds=self.policy.session_idle_seconds),
            )
            browser_session = AdminSession(
                user_id=user.id,
                token_sha256=hash_opaque_token(session_token),
                csrf_sha256=hash_opaque_token(csrf_token),
                credential_version=user.credential_version,
                client_context_hmac=self._context_digest("client", client_context[:200]),
                user_agent_hmac=(
                    self._context_digest("user-agent", user_agent[:500]) if user_agent else None
                ),
                created_at=observed_at,
                last_seen_at=observed_at,
                expires_at=expires_at,
                idle_expires_at=idle_expires_at,
                reauthenticated_at=observed_at,
                mfa_verified_at=observed_at if matched_counter is not None else None,
                revoked_at=None,
            )
            session.add(browser_session)
            await session.flush()
            session.add(
                AuditEvent(
                    actor=str(user.id),
                    action="auth.login_succeeded",
                    resource_type="admin_session",
                    resource_id=browser_session.id,
                    correlation_id=correlation_id,
                    detail={
                        "mfa_verified": matched_counter is not None,
                        "role": user.role.value,
                    },
                    occurred_at=observed_at,
                )
            )
            await session.commit()
            return LoginResult(
                session_token=session_token,
                csrf_token=csrf_token,
                session_id=browser_session.id,
                user_id=user.id,
                username=user.username_normalized,
                display_name=user.display_name,
                role=user.role,
                expires_at=expires_at,
                idle_expires_at=idle_expires_at,
                mfa_verified=matched_counter is not None,
            )
        finally:
            if not admission.delegated_to_worker:
                admission.release()

    async def reauthenticate_session(
        self,
        session: AsyncSession,
        *,
        principal: AuthenticatedPrincipal,
        password: str,
        totp_code: str | None,
        correlation_id: str,
        now: datetime | None = None,
    ) -> AuthenticatedPrincipal:
        """Refresh recent-auth state on an existing session after password and TOTP."""

        observed_at = _as_utc(now or datetime.now(UTC))
        if not self._try_admit_password_operation():
            raise AuthenticationFailedError("authentication failed")
        admission = _PasswordAdmissionLease(self._release_password_admission)
        try:
            row = (
                await session.execute(
                    select(AdminSession, AdminUser)
                    .join(AdminUser, AdminUser.id == AdminSession.user_id)
                    .where(
                        AdminSession.id == principal.session_id,
                        AdminUser.id == principal.user_id,
                    )
                    .with_for_update()
                )
            ).one_or_none()
            browser_session: AdminSession | None
            user: AdminUser | None
            if row is None:
                browser_session = None
                user = None
            else:
                browser_session, user = row

            session_is_valid = bool(
                browser_session is not None
                and user is not None
                and browser_session.revoked_at is None
                and user.is_active
                and browser_session.credential_version == user.credential_version
                and observed_at < _as_utc(browser_session.expires_at)
                and observed_at < _as_utc(browser_session.idle_expires_at)
            )
            user_is_locked = bool(
                user is not None
                and user.locked_until is not None
                and observed_at < _as_utc(user.locked_until)
            )
            verification = await self._verify_password(
                (
                    user.password_hash
                    if session_is_valid and user is not None and not user_is_locked
                    else None
                ),
                password,
                admission,
            )

            matched_counter: int | None = None
            valid = bool(
                session_is_valid
                and user is not None
                and browser_session is not None
                and not user_is_locked
                and verification.valid
            )
            if valid:
                if (
                    user is None
                    or user.totp_confirmed_at is None
                    or user.totp_secret_ciphertext is None
                    or totp_code is None
                ):
                    valid = False
                else:
                    try:
                        totp_secret = self._totp_cipher.decrypt(
                            user.totp_secret_ciphertext,
                            subject=f"admin-user:{user.id}",
                        )
                    except SecretEncryptionError:
                        valid = False
                    else:
                        matched_counter = verify_totp(
                            totp_secret,
                            totp_code,
                            unix_time=int(observed_at.timestamp()),
                            last_used_counter=user.last_totp_counter,
                        )
                        valid = matched_counter is not None

            if not valid or user is None or browser_session is None or matched_counter is None:
                session.add(
                    AuditEvent(
                        actor=str(principal.user_id),
                        action="auth.reauthentication_failed",
                        resource_type="admin_session",
                        resource_id=principal.session_id,
                        correlation_id=correlation_id,
                        detail={"reason": "credentials_or_mfa"},
                        occurred_at=observed_at,
                    )
                )
                await session.commit()
                raise AuthenticationFailedError("authentication failed")

            claimed_user_id = await session.scalar(
                update(AdminUser)
                .where(
                    AdminUser.id == user.id,
                    or_(
                        AdminUser.last_totp_counter.is_(None),
                        AdminUser.last_totp_counter < matched_counter,
                    ),
                )
                .values(last_totp_counter=matched_counter)
                .returning(AdminUser.id)
            )
            if claimed_user_id is None:
                session.add(
                    AuditEvent(
                        actor=str(principal.user_id),
                        action="auth.reauthentication_failed",
                        resource_type="admin_session",
                        resource_id=principal.session_id,
                        correlation_id=correlation_id,
                        detail={"reason": "totp_counter_claim_rejected"},
                        occurred_at=observed_at,
                    )
                )
                await session.commit()
                raise AuthenticationFailedError("authentication failed")

            if verification.replacement_hash is not None:
                user.password_hash = verification.replacement_hash
            user.failed_login_count = 0
            user.failed_login_window_started_at = None
            user.locked_until = None
            user.lock_version += 1
            browser_session.reauthenticated_at = observed_at
            browser_session.mfa_verified_at = observed_at
            session.add(
                AuditEvent(
                    actor=str(user.id),
                    action="auth.session_reauthenticated",
                    resource_type="admin_session",
                    resource_id=browser_session.id,
                    correlation_id=correlation_id,
                    detail={"mfa_verified": True},
                    occurred_at=observed_at,
                )
            )
            await session.commit()
            return AuthenticatedPrincipal(
                session_id=browser_session.id,
                user_id=user.id,
                username=user.username_normalized,
                display_name=user.display_name,
                role=user.role,
                csrf_sha256=browser_session.csrf_sha256,
                expires_at=_as_utc(browser_session.expires_at),
                idle_expires_at=_as_utc(browser_session.idle_expires_at),
                reauthenticated_at=observed_at,
                mfa_verified_at=observed_at,
            )
        finally:
            if not admission.delegated_to_worker:
                admission.release()

    async def resolve_session(
        self,
        session: AsyncSession,
        *,
        session_token: str,
        now: datetime | None = None,
    ) -> AuthenticatedPrincipal:
        observed_at = _as_utc(now or datetime.now(UTC))
        try:
            token_sha256 = hash_opaque_token(session_token)
        except ValueError:
            token_sha256 = _DUMMY_TOKEN_SHA256
        row = (
            await session.execute(
                select(AdminSession, AdminUser)
                .join(AdminUser, AdminUser.id == AdminSession.user_id)
                .where(AdminSession.token_sha256 == token_sha256)
                .with_for_update()
            )
        ).one_or_none()
        if row is None:
            raise SessionUnauthorizedError("authentication required")
        browser_session, user = row
        invalid = (
            browser_session.revoked_at is not None
            or not user.is_active
            or browser_session.credential_version != user.credential_version
            or observed_at >= _as_utc(browser_session.expires_at)
            or observed_at >= _as_utc(browser_session.idle_expires_at)
            or (self.policy.require_totp and browser_session.mfa_verified_at is None)
        )
        if invalid:
            if browser_session.revoked_at is None:
                browser_session.revoked_at = observed_at
                await session.commit()
            raise SessionUnauthorizedError("authentication required")

        last_seen_at = _as_utc(browser_session.last_seen_at)
        if observed_at >= last_seen_at + timedelta(seconds=_SESSION_TOUCH_INTERVAL_SECONDS):
            browser_session.last_seen_at = observed_at
            browser_session.idle_expires_at = min(
                _as_utc(browser_session.expires_at),
                observed_at + timedelta(seconds=self.policy.session_idle_seconds),
            )
            await session.commit()
        return AuthenticatedPrincipal(
            session_id=browser_session.id,
            user_id=user.id,
            username=user.username_normalized,
            display_name=user.display_name,
            role=user.role,
            csrf_sha256=browser_session.csrf_sha256,
            expires_at=_as_utc(browser_session.expires_at),
            idle_expires_at=_as_utc(browser_session.idle_expires_at),
            reauthenticated_at=_as_utc(browser_session.reauthenticated_at),
            mfa_verified_at=(
                _as_utc(browser_session.mfa_verified_at)
                if browser_session.mfa_verified_at is not None
                else None
            ),
        )

    @staticmethod
    def validate_csrf(
        principal: AuthenticatedPrincipal,
        *,
        cookie_token: str | None,
        header_token: str | None,
    ) -> None:
        cookie_value = cookie_token or ""
        header_value = header_token or ""
        same_token = hmac.compare_digest(cookie_value, header_value)
        if not same_token or not verify_opaque_token(principal.csrf_sha256, cookie_value):
            raise CsrfValidationError("CSRF validation failed")

    def require_recent_authentication(
        self,
        principal: AuthenticatedPrincipal,
        *,
        now: datetime | None = None,
    ) -> None:
        observed_at = _as_utc(now or datetime.now(UTC))
        if observed_at >= principal.reauthenticated_at + timedelta(
            seconds=self.policy.recent_auth_seconds
        ):
            raise RecentAuthenticationRequiredError("recent authentication required")

    async def logout(
        self,
        session: AsyncSession,
        *,
        principal: AuthenticatedPrincipal,
        correlation_id: str,
        now: datetime | None = None,
    ) -> None:
        observed_at = _as_utc(now or datetime.now(UTC))
        browser_session = await session.scalar(
            select(AdminSession).where(AdminSession.id == principal.session_id).with_for_update()
        )
        if browser_session is not None and browser_session.revoked_at is None:
            browser_session.revoked_at = observed_at
            session.add(
                AuditEvent(
                    actor=str(principal.user_id),
                    action="auth.logout",
                    resource_type="admin_session",
                    resource_id=principal.session_id,
                    correlation_id=correlation_id,
                    detail={},
                    occurred_at=observed_at,
                )
            )
        await session.commit()
