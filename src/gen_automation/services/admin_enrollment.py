import asyncio
import hashlib
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from gen_automation.auth.security import (
    PasswordManager,
    PasswordPolicyError,
    SecretEncryptionError,
    TotpSecretCipher,
    generate_opaque_token,
    generate_totp_secret,
    hash_opaque_token,
    provisioning_uri,
    verify_totp,
)
from gen_automation.db.models import AdminEnrollment, AdminUser, AuditEvent
from gen_automation.domain.enums import (
    AdminEnrollmentState,
    AdminRole,
)
from gen_automation.domain.ids import uuid7
from gen_automation.services.authentication import (
    AuthenticationFailedError,
    normalize_username,
)

MINIMUM_ENROLLMENT_TTL_SECONDS = 10 * 60
MAXIMUM_ENROLLMENT_TTL_SECONDS = 7 * 24 * 60 * 60
DEFAULT_ENROLLMENT_TTL_SECONDS = 24 * 60 * 60
_INVALID_TOKEN_SHA256 = hashlib.sha256(b"invalid-admin-enrollment-token").hexdigest()
_INVALID_MESSAGE = "administrator enrollment is invalid or expired"
_DEFAULT_HASH_ADMISSIONS = 4
_MAX_HASH_ADMISSIONS = 16


class AdminEnrollmentError(Exception):
    """Base error for normal administrator invitation and enrollment."""


class AdminEnrollmentInputError(AdminEnrollmentError, ValueError):
    pass


class AdminEnrollmentConflictError(AdminEnrollmentError):
    pass


class AdminEnrollmentInvalidError(AdminEnrollmentError):
    """A capability is malformed, unknown, expired, consumed, or revoked."""


class AdminEnrollmentBusyError(AdminEnrollmentError):
    pass


@dataclass(frozen=True, slots=True)
class AdminInvitationResult:
    enrollment_id: UUID
    username: str
    display_name: str
    role: AdminRole
    expires_at: datetime
    invite_token: str


@dataclass(frozen=True, slots=True)
class AdminEnrollmentInspection:
    username: str
    display_name: str
    role: AdminRole
    expires_at: datetime
    totp_secret: str
    totp_provisioning_uri: str


@dataclass(frozen=True, slots=True)
class AdminEnrollmentCompletion:
    user_id: UUID
    username: str
    display_name: str
    role: AdminRole


class _HashAdmissionLease:
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


class AdminEnrollmentService:
    """One-time capability enrollment with bounded password-hash work."""

    def __init__(
        self,
        *,
        password_manager: PasswordManager,
        totp_cipher: TotpSecretCipher,
        max_password_operations: int = 1,
        max_password_admissions: int = _DEFAULT_HASH_ADMISSIONS,
    ) -> None:
        if not 1 <= max_password_operations <= 4:
            raise ValueError("enrollment password operation concurrency is invalid")
        if not 1 <= max_password_admissions <= _MAX_HASH_ADMISSIONS:
            raise ValueError("enrollment password operation admission limit is invalid")
        self._password_manager = password_manager
        self._totp_cipher = totp_cipher
        self._password_semaphore = asyncio.Semaphore(max_password_operations)
        self._password_admission_limit = max_password_admissions
        self._password_admissions = 0
        self._password_tasks: set[asyncio.Task[str]] = set()

    async def create_invitation(
        self,
        session: AsyncSession,
        *,
        username: str,
        display_name: str,
        role: AdminRole,
        invited_by_user_id: UUID,
        correlation_id: str,
        ttl_seconds: int = DEFAULT_ENROLLMENT_TTL_SECONDS,
        now: datetime | None = None,
    ) -> AdminInvitationResult:
        observed_at = _as_utc(now or datetime.now(UTC))
        normalized_username = _normalize_invited_username(username)
        normalized_display_name = _normalize_display_name(display_name)
        normalized_role = _normalize_role(role)
        normalized_ttl = _validate_ttl(ttl_seconds)
        inviter = await session.scalar(
            select(AdminUser)
            .where(
                AdminUser.id == invited_by_user_id,
                AdminUser.is_active.is_(True),
                AdminUser.role == AdminRole.OWNER,
            )
            .with_for_update()
        )
        if inviter is None:
            raise AdminEnrollmentConflictError(
                "an active owner is required to invite administrators"
            )
        existing_user_id = await session.scalar(
            select(AdminUser.id).where(AdminUser.username_normalized == normalized_username)
        )
        if existing_user_id is not None:
            raise AdminEnrollmentConflictError("an administrator with this username already exists")

        pending = await session.scalar(
            select(AdminEnrollment)
            .where(
                AdminEnrollment.username_normalized == normalized_username,
                AdminEnrollment.state == AdminEnrollmentState.PENDING,
            )
            .with_for_update()
        )
        if pending is not None:
            if observed_at < _as_utc(pending.expires_at):
                raise AdminEnrollmentConflictError(
                    "a pending enrollment already exists for this username"
                )
            pending.state = AdminEnrollmentState.REVOKED
            pending.totp_secret_ciphertext = None
            pending.revoked_by_user_id = invited_by_user_id
            pending.revoked_at = observed_at
            pending.lock_version += 1
            session.add(
                AuditEvent(
                    actor=str(invited_by_user_id),
                    action="auth.admin_enrollment_expired",
                    resource_type="admin_enrollment",
                    resource_id=pending.id,
                    correlation_id=correlation_id,
                    detail={
                        "username": pending.username_normalized,
                        "role": pending.role.value,
                        "expired_at": _as_utc(pending.expires_at).isoformat(),
                    },
                    occurred_at=observed_at,
                )
            )
            await session.flush()

        enrollment_id = uuid7()
        invite_token = generate_opaque_token()
        totp_secret = generate_totp_secret()
        expires_at = observed_at + timedelta(seconds=normalized_ttl)
        enrollment = AdminEnrollment(
            id=enrollment_id,
            username_normalized=normalized_username,
            display_name=normalized_display_name,
            role=normalized_role,
            token_sha256=hash_opaque_token(invite_token),
            totp_secret_ciphertext=self._totp_cipher.encrypt(
                totp_secret,
                subject=f"admin-enrollment:{enrollment_id}",
            ),
            state=AdminEnrollmentState.PENDING,
            invited_by_user_id=invited_by_user_id,
            invited_at=observed_at,
            expires_at=expires_at,
            lock_version=1,
        )
        session.add(enrollment)
        session.add(
            AuditEvent(
                actor=str(invited_by_user_id),
                action="auth.admin_enrollment_invited",
                resource_type="admin_enrollment",
                resource_id=enrollment.id,
                correlation_id=correlation_id,
                detail={
                    "username": normalized_username,
                    "display_name": normalized_display_name,
                    "role": normalized_role.value,
                    "expires_at": expires_at.isoformat(),
                },
                occurred_at=observed_at,
            )
        )
        try:
            await session.commit()
        except IntegrityError as error:
            await session.rollback()
            raise AdminEnrollmentConflictError(
                "administrator invitation conflicts with current state"
            ) from error
        return AdminInvitationResult(
            enrollment_id=enrollment.id,
            username=enrollment.username_normalized,
            display_name=enrollment.display_name,
            role=enrollment.role,
            expires_at=expires_at,
            invite_token=invite_token,
        )

    async def inspect_capability(
        self,
        session: AsyncSession,
        *,
        invite_token: str,
        now: datetime | None = None,
    ) -> AdminEnrollmentInspection:
        observed_at = _as_utc(now or datetime.now(UTC))
        enrollment = await self._load_valid_enrollment(
            session,
            invite_token=invite_token,
            now=observed_at,
            lock=False,
        )
        try:
            secret = self._totp_cipher.decrypt(
                _required_ciphertext(enrollment),
                subject=f"admin-enrollment:{enrollment.id}",
            )
            uri = provisioning_uri(
                secret,
                account_name=enrollment.username_normalized,
            )
        except (SecretEncryptionError, ValueError):
            raise AdminEnrollmentInvalidError(_INVALID_MESSAGE) from None
        return AdminEnrollmentInspection(
            username=enrollment.username_normalized,
            display_name=enrollment.display_name,
            role=enrollment.role,
            expires_at=_as_utc(enrollment.expires_at),
            totp_secret=secret,
            totp_provisioning_uri=uri,
        )

    async def complete_enrollment(
        self,
        session: AsyncSession,
        *,
        invite_token: str,
        password: str,
        totp_code: str,
        correlation_id: str,
        now: datetime | None = None,
    ) -> AdminEnrollmentCompletion:
        observed_at = _as_utc(now or datetime.now(UTC))
        enrollment = await self._load_valid_enrollment(
            session,
            invite_token=invite_token,
            now=observed_at,
            lock=True,
        )
        try:
            secret = self._totp_cipher.decrypt(
                _required_ciphertext(enrollment),
                subject=f"admin-enrollment:{enrollment.id}",
            )
        except SecretEncryptionError:
            raise AdminEnrollmentInvalidError(_INVALID_MESSAGE) from None
        matched_counter = verify_totp(
            secret,
            totp_code,
            unix_time=int(observed_at.timestamp()),
        )
        if matched_counter is None:
            raise AdminEnrollmentInvalidError(_INVALID_MESSAGE)
        if not self._try_admit_password_operation():
            raise AdminEnrollmentBusyError("administrator enrollment is temporarily unavailable")
        admission = _HashAdmissionLease(self._release_password_admission)
        try:
            try:
                password_hash = await self._hash_password(password, admission)
            except PasswordPolicyError:
                raise AdminEnrollmentInputError(
                    "password does not meet the length policy"
                ) from None

            user_id = uuid7()
            encrypted_user_secret = self._totp_cipher.encrypt(
                secret,
                subject=f"admin-user:{user_id}",
            )
            user = AdminUser(
                id=user_id,
                username_normalized=enrollment.username_normalized,
                display_name=enrollment.display_name,
                password_hash=password_hash,
                role=enrollment.role,
                is_active=True,
                totp_secret_ciphertext=encrypted_user_secret,
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
            try:
                session.add(user)
                await session.flush()
                consumed_id = await session.scalar(
                    update(AdminEnrollment)
                    .where(
                        AdminEnrollment.id == enrollment.id,
                        AdminEnrollment.token_sha256 == enrollment.token_sha256,
                        AdminEnrollment.state == AdminEnrollmentState.PENDING,
                        AdminEnrollment.expires_at > observed_at,
                        AdminEnrollment.lock_version == enrollment.lock_version,
                    )
                    .values(
                        state=AdminEnrollmentState.CONSUMED,
                        totp_secret_ciphertext=None,
                        consumed_by_user_id=user.id,
                        consumed_at=observed_at,
                        lock_version=enrollment.lock_version + 1,
                    )
                    .returning(AdminEnrollment.id)
                    .execution_options(synchronize_session=False)
                )
                if consumed_id is None:
                    await session.rollback()
                    raise AdminEnrollmentInvalidError(_INVALID_MESSAGE)
                session.add(
                    AuditEvent(
                        actor=str(user.id),
                        action="auth.admin_enrollment_completed",
                        resource_type="admin_enrollment",
                        resource_id=enrollment.id,
                        correlation_id=correlation_id,
                        detail={
                            "user_id": str(user.id),
                            "username": user.username_normalized,
                            "display_name": user.display_name,
                            "role": user.role.value,
                            "invited_by_user_id": str(enrollment.invited_by_user_id),
                            "totp_enrolled": True,
                        },
                        occurred_at=observed_at,
                    )
                )
                await session.commit()
            except IntegrityError as error:
                await session.rollback()
                raise AdminEnrollmentInvalidError(_INVALID_MESSAGE) from error
            return AdminEnrollmentCompletion(
                user_id=user.id,
                username=user.username_normalized,
                display_name=user.display_name,
                role=user.role,
            )
        finally:
            if not admission.delegated_to_worker:
                admission.release()

    async def revoke_invitation(
        self,
        session: AsyncSession,
        *,
        enrollment_id: UUID,
        revoked_by_user_id: UUID,
        correlation_id: str,
        now: datetime | None = None,
    ) -> None:
        """Revoke a pending capability; API exposure is intentionally deferred."""

        observed_at = _as_utc(now or datetime.now(UTC))
        owner_id = await session.scalar(
            select(AdminUser.id).where(
                AdminUser.id == revoked_by_user_id,
                AdminUser.is_active.is_(True),
                AdminUser.role == AdminRole.OWNER,
            )
        )
        if owner_id is None:
            raise AdminEnrollmentConflictError("an active owner is required to revoke enrollment")
        enrollment = await session.scalar(
            select(AdminEnrollment).where(AdminEnrollment.id == enrollment_id).with_for_update()
        )
        if enrollment is None or enrollment.state != AdminEnrollmentState.PENDING:
            raise AdminEnrollmentConflictError("pending enrollment was not found")
        enrollment.state = AdminEnrollmentState.REVOKED
        enrollment.totp_secret_ciphertext = None
        enrollment.revoked_by_user_id = revoked_by_user_id
        enrollment.revoked_at = observed_at
        enrollment.lock_version += 1
        session.add(
            AuditEvent(
                actor=str(revoked_by_user_id),
                action="auth.admin_enrollment_revoked",
                resource_type="admin_enrollment",
                resource_id=enrollment.id,
                correlation_id=correlation_id,
                detail={
                    "username": enrollment.username_normalized,
                    "role": enrollment.role.value,
                },
                occurred_at=observed_at,
            )
        )
        await session.commit()

    async def _load_valid_enrollment(
        self,
        session: AsyncSession,
        *,
        invite_token: str,
        now: datetime,
        lock: bool,
    ) -> AdminEnrollment:
        token_sha256 = _capability_digest(invite_token)
        statement = select(AdminEnrollment).where(AdminEnrollment.token_sha256 == token_sha256)
        if lock:
            statement = statement.with_for_update()
        enrollment = await session.scalar(statement)
        if (
            enrollment is None
            or enrollment.state != AdminEnrollmentState.PENDING
            or now >= _as_utc(enrollment.expires_at)
            or enrollment.totp_secret_ciphertext is None
        ):
            raise AdminEnrollmentInvalidError(_INVALID_MESSAGE)
        return enrollment

    def _try_admit_password_operation(self) -> bool:
        if self._password_admissions >= self._password_admission_limit:
            return False
        self._password_admissions += 1
        return True

    def _release_password_admission(self) -> None:
        if self._password_admissions <= 0:
            raise RuntimeError("enrollment password admission accounting underflow")
        self._password_admissions -= 1

    async def _hash_password(
        self,
        password: str,
        admission: _HashAdmissionLease,
    ) -> str:
        await self._password_semaphore.acquire()
        try:
            task = asyncio.create_task(asyncio.to_thread(self._password_manager.hash, password))
        except BaseException:
            self._password_semaphore.release()
            raise
        self._password_tasks.add(task)
        admission.delegate_to_worker()

        def release_capacity(done_task: asyncio.Task[str]) -> None:
            self._password_tasks.discard(done_task)
            if not done_task.cancelled():
                done_task.exception()
            self._password_semaphore.release()
            admission.release()

        task.add_done_callback(release_capacity)
        return await asyncio.shield(task)


def _normalize_invited_username(value: str) -> str:
    try:
        return normalize_username(value)
    except AuthenticationFailedError:
        raise AdminEnrollmentInputError("username is invalid") from None


def _normalize_display_name(value: str) -> str:
    normalized = " ".join(unicodedata.normalize("NFKC", value).strip().split())
    if not 1 <= len(normalized) <= 200 or any(ord(character) < 0x20 for character in normalized):
        raise AdminEnrollmentInputError("display name is invalid")
    return normalized


def _normalize_role(value: AdminRole) -> AdminRole:
    try:
        return AdminRole(value)
    except ValueError:
        raise AdminEnrollmentInputError("administrator role is invalid") from None


def _validate_ttl(value: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not MINIMUM_ENROLLMENT_TTL_SECONDS <= value <= MAXIMUM_ENROLLMENT_TTL_SECONDS
    ):
        raise AdminEnrollmentInputError("enrollment expiry must be between 10 minutes and 7 days")
    return value


def _capability_digest(invite_token: str) -> str:
    try:
        return hash_opaque_token(invite_token)
    except (TypeError, ValueError):
        return _INVALID_TOKEN_SHA256


def _required_ciphertext(enrollment: AdminEnrollment) -> str:
    if enrollment.totp_secret_ciphertext is None:
        raise AdminEnrollmentInvalidError(_INVALID_MESSAGE)
    return enrollment.totp_secret_ciphertext


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
