"""Bounded, restart-safe execution for durable publication attempts."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol, cast
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gen_automation.db.models import (
    AuditEvent,
    PublicationAttempt,
    PublicationEffectEvent,
    PublicationInput,
    PublicationIntent,
    PublicationPackage,
    PublicationStep,
)
from gen_automation.domain.canonical import canonical_sha256
from gen_automation.domain.enums import (
    PublicationAttemptState,
    PublicationIntentState,
    PublicationRetryClass,
    PublicationStepKind,
    PublicationStepState,
    PublicationTarget,
)
from gen_automation.domain.ids import uuid7
from gen_automation.integrations.patreon.handoff import (
    PatreonHandoffError,
    PatreonPackageImage,
    PublicPreviewSafetyAttestation,
    build_patreon_handoff_package,
)
from gen_automation.integrations.x.errors import (
    XAmbiguousError,
    XProtocolError,
    XRetryableError,
    XTerminalError,
)
from gen_automation.integrations.x.models import XPost, XUploadedMedia
from gen_automation.services.publication import (
    PublicationConflictError,
    PublicationDisabledError,
    require_effect_authorization,
    safe_publication_error,
)
from gen_automation.storage.base import (
    ObjectAlreadyExistsError,
    ObjectMetadata,
    ObjectStore,
    ObjectStoreError,
)

_SAFE_RUNTIME_ERROR = "Publication execution failed inside the bounded provider boundary."
_SAFE_UNKNOWN_ERROR = "External effect outcome requires manual reconciliation."
_PACKAGE_CONTENT_TYPE = "application/zip"
_DEFAULT_MAX_PACKAGE_BYTES = 160 * 1024 * 1024
_X_MEDIA_EXPIRY_MARGIN_SECONDS = 30


class PublicationRuntimeError(Exception):
    """Base error for automatic publication execution."""


class PublicationRuntimeContractError(PublicationRuntimeError):
    """A frozen database or object snapshot violates the execution contract."""


class XCredentialUnavailableError(PublicationRuntimeError):
    """A credential could not be safely resolved before an X API effect."""


class XOAuthRotationError(XCredentialUnavailableError):
    """A replacement refresh token could not be durably rotated before publish."""


class XPublicationClient(Protocol):
    async def upload_image(
        self,
        *,
        image: bytes,
        media_type: str,
    ) -> XUploadedMedia: ...

    async def create_post(
        self,
        *,
        text: str,
        media_ids: Sequence[str],
    ) -> XPost: ...


class XOAuthEffectLease(Protocol):
    """One just-in-time, redacted X credential lease.

    Implementations must resolve an approved secret immediately before yielding
    ``client`` and refresh when its access token is missing or near expiry.
    Access-token caching is allowed only inside that approved secret store, never
    in application tables. Any new access token and replacement refresh token must
    be durably persisted before yielding. Implementations must raise
    ``XOAuthRotationError`` rather than discard a replacement token. Tokens must
    never appear in ``repr``, logs, exceptions, database state, or audit data.
    """

    @property
    def client(self) -> XPublicationClient: ...

    @property
    def creator_user_id(self) -> str:
        """Verified ``GET /2/users/me`` ID; no profile fields may be retained."""
        ...


class XOAuthProvider(Protocol):
    """Open a just-in-time OAuth lease for one approved external effect."""

    def open_for_effect(
        self,
        credential_reference: str,
    ) -> AbstractAsyncContextManager[XOAuthEffectLease]: ...


@dataclass(frozen=True, slots=True)
class PublicationCycleResult:
    recovered_expired_lease: bool = False
    claimed_attempt: bool = False
    attempt_id: UUID | None = None
    state: PublicationAttemptState | None = None
    error_code: str | None = None

    @property
    def did_work(self) -> bool:
        return self.recovered_expired_lease or self.claimed_attempt


@dataclass(frozen=True, slots=True)
class _ClaimedAttempt:
    attempt_id: UUID
    worker_id: str


@dataclass(frozen=True, slots=True)
class _EffectContext:
    attempt_id: UUID
    intent_id: UUID
    approval_id: UUID
    step_id: UUID
    kind: PublicationStepKind
    guard_epoch: int
    credential_reference: str | None
    configuration: dict[str, object]


@dataclass(frozen=True, slots=True)
class _InputBytes:
    publication_input: PublicationInput
    body: bytes


@dataclass(frozen=True, slots=True)
class _PatreonPreparedPackage:
    archive_bytes: bytes
    sha256: str
    manifest_sha256: str


async def run_publication_cycle(
    sessions: async_sessionmaker[AsyncSession],
    store: ObjectStore,
    *,
    worker_id: str,
    x_oauth_provider: XOAuthProvider | None,
    expected_x_creator_user_id: str | None,
    lease_seconds: int,
    retry_base_seconds: int,
    retry_max_seconds: int,
    max_package_bytes: int = _DEFAULT_MAX_PACKAGE_BYTES,
    now: datetime | None = None,
    clock: Callable[[], datetime] | None = None,
) -> PublicationCycleResult:
    """Recover or execute at most one publication attempt.

    Provider calls occur only after a short transaction rechecks the durable
    global guard, exact approval digest/lock/expiry, and current release version,
    then records ``effect_started_at`` and commits. No database lock is held over
    provider I/O.
    """

    cycle_at = _as_utc(now or (clock() if clock is not None else datetime.now(UTC)))
    effect_clock = _effect_clock(clock=clock, initial=cycle_at, fixed=now is not None)
    normalized_worker = _bounded_worker(worker_id)
    normalized_lease = _bounded_int(lease_seconds, "lease seconds", 30, 3_600)
    normalized_retry_base = _bounded_int(
        retry_base_seconds,
        "retry base seconds",
        1,
        86_400,
    )
    normalized_retry_max = _bounded_int(
        retry_max_seconds,
        "retry maximum seconds",
        normalized_retry_base,
        7 * 86_400,
    )
    normalized_package_max = _bounded_int(
        max_package_bytes,
        "maximum package bytes",
        1,
        5 * 1024 * 1024 * 1024,
    )
    normalized_creator_user_id = _x_creator_user_id(expected_x_creator_user_id)

    recovered = await _recover_one_expired_lease(sessions, now=cycle_at)
    if recovered:
        return PublicationCycleResult(recovered_expired_lease=True)
    claimed = await _claim_one_attempt(
        sessions,
        worker_id=normalized_worker,
        lease_seconds=normalized_lease,
        now=cycle_at,
    )
    if claimed is None:
        return PublicationCycleResult()
    return await _execute_claimed_attempt(
        sessions,
        store,
        claimed=claimed,
        x_oauth_provider=x_oauth_provider,
        expected_x_creator_user_id=normalized_creator_user_id,
        retry_base_seconds=normalized_retry_base,
        retry_max_seconds=normalized_retry_max,
        max_package_bytes=normalized_package_max,
        lease_seconds=normalized_lease,
        clock=effect_clock,
    )


async def _execute_claimed_attempt(
    sessions: async_sessionmaker[AsyncSession],
    store: ObjectStore,
    *,
    claimed: _ClaimedAttempt,
    x_oauth_provider: XOAuthProvider | None,
    expected_x_creator_user_id: str | None,
    retry_base_seconds: int,
    retry_max_seconds: int,
    max_package_bytes: int,
    lease_seconds: int,
    clock: Callable[[], datetime],
) -> PublicationCycleResult:
    while True:
        irreversible_x_post_request_no: int | None = None
        snapshot_at = _clock_now(clock)
        snapshot = await _next_step_snapshot(
            sessions,
            attempt_id=claimed.attempt_id,
            worker_id=claimed.worker_id,
            now=snapshot_at,
        )
        if snapshot is None:
            state = await _attempt_state(sessions, claimed.attempt_id)
            return PublicationCycleResult(
                claimed_attempt=True,
                attempt_id=claimed.attempt_id,
                state=state,
            )

        try:
            if snapshot.kind == PublicationStepKind.X_MEDIA_UPLOAD:
                input_bytes = await _read_step_input(
                    sessions,
                    store,
                    attempt_id=claimed.attempt_id,
                    step_id=snapshot.step_id,
                )
                effect_at = _clock_now(clock)
                context = await _begin_effect(
                    sessions,
                    attempt_id=claimed.attempt_id,
                    step_id=snapshot.step_id,
                    worker_id=claimed.worker_id,
                    lease_seconds=lease_seconds,
                    now=effect_at,
                )
                if (
                    x_oauth_provider is None
                    or context.credential_reference is None
                    or expected_x_creator_user_id is None
                ):
                    await _defer_before_provider_request(
                        sessions,
                        attempt_id=claimed.attempt_id,
                        step_id=snapshot.step_id,
                        worker_id=claimed.worker_id,
                        error_code="x_credentials_unavailable",
                        retry_base_seconds=retry_base_seconds,
                        retry_max_seconds=retry_max_seconds,
                        now=_clock_now(clock),
                    )
                    return PublicationCycleResult(
                        claimed_attempt=True,
                        attempt_id=claimed.attempt_id,
                        state=PublicationAttemptState.RETRY_WAIT,
                        error_code="x_credentials_unavailable",
                    )
                provider_invoked = False
                provider_returned = False
                provider_request_no: int | None = None
                try:
                    async with x_oauth_provider.open_for_effect(
                        context.credential_reference
                    ) as oauth:
                        if not hmac.compare_digest(
                            oauth.creator_user_id,
                            expected_x_creator_user_id,
                        ):
                            raise XCredentialUnavailableError("X credential account binding failed")
                        provider_request_no = await _mark_provider_request_started(
                            sessions,
                            attempt_id=claimed.attempt_id,
                            step_id=snapshot.step_id,
                            worker_id=claimed.worker_id,
                            lease_seconds=lease_seconds,
                            now=_clock_now(clock),
                        )
                        provider_invoked = True
                        uploaded = await oauth.client.upload_image(
                            image=input_bytes.body,
                            media_type=input_bytes.publication_input.asset_content_type,
                        )
                        provider_returned = True
                except XCredentialUnavailableError:
                    if provider_invoked:
                        await _schedule_retry(
                            sessions,
                            attempt_id=claimed.attempt_id,
                            step_id=snapshot.step_id,
                            worker_id=claimed.worker_id,
                            error_code="x_media_upload_credential_exit_retryable",
                            retry_base_seconds=retry_base_seconds,
                            retry_max_seconds=retry_max_seconds,
                            provider_request_no=provider_request_no,
                            now=_clock_now(clock),
                        )
                        return PublicationCycleResult(
                            claimed_attempt=True,
                            attempt_id=claimed.attempt_id,
                            state=PublicationAttemptState.RETRY_WAIT,
                            error_code="x_media_upload_credential_exit_retryable",
                        )
                    await _defer_before_provider_request(
                        sessions,
                        attempt_id=claimed.attempt_id,
                        step_id=snapshot.step_id,
                        worker_id=claimed.worker_id,
                        error_code="x_credentials_unavailable",
                        retry_base_seconds=retry_base_seconds,
                        retry_max_seconds=retry_max_seconds,
                        now=_clock_now(clock),
                    )
                    return PublicationCycleResult(
                        claimed_attempt=True,
                        attempt_id=claimed.attempt_id,
                        state=PublicationAttemptState.RETRY_WAIT,
                        error_code="x_credentials_unavailable",
                    )
                except PublicationConflictError:
                    if provider_invoked:
                        await _schedule_retry(
                            sessions,
                            attempt_id=claimed.attempt_id,
                            step_id=snapshot.step_id,
                            worker_id=claimed.worker_id,
                            error_code="x_media_upload_authorization_retryable",
                            retry_base_seconds=retry_base_seconds,
                            retry_max_seconds=retry_max_seconds,
                            provider_request_no=provider_request_no,
                            now=_clock_now(clock),
                        )
                        return PublicationCycleResult(
                            claimed_attempt=True,
                            attempt_id=claimed.attempt_id,
                            state=PublicationAttemptState.RETRY_WAIT,
                            error_code="x_media_upload_authorization_retryable",
                        )
                    raise
                except XRetryableError:
                    await _schedule_retry(
                        sessions,
                        attempt_id=claimed.attempt_id,
                        step_id=snapshot.step_id,
                        worker_id=claimed.worker_id,
                        error_code="x_media_upload_retryable",
                        retry_base_seconds=retry_base_seconds,
                        retry_max_seconds=retry_max_seconds,
                        provider_request_no=provider_request_no,
                        now=_clock_now(clock),
                    )
                    return PublicationCycleResult(
                        claimed_attempt=True,
                        attempt_id=claimed.attempt_id,
                        state=PublicationAttemptState.RETRY_WAIT,
                        error_code="x_media_upload_retryable",
                    )
                except XAmbiguousError:
                    await _schedule_retry(
                        sessions,
                        attempt_id=claimed.attempt_id,
                        step_id=snapshot.step_id,
                        worker_id=claimed.worker_id,
                        error_code="x_media_upload_ambiguous_retryable",
                        retry_base_seconds=retry_base_seconds,
                        retry_max_seconds=retry_max_seconds,
                        provider_request_no=provider_request_no,
                        now=_clock_now(clock),
                    )
                    return PublicationCycleResult(
                        claimed_attempt=True,
                        attempt_id=claimed.attempt_id,
                        state=PublicationAttemptState.RETRY_WAIT,
                        error_code="x_media_upload_ambiguous_retryable",
                    )
                except (XTerminalError, TypeError, ValueError):
                    if provider_returned:
                        await _schedule_retry(
                            sessions,
                            attempt_id=claimed.attempt_id,
                            step_id=snapshot.step_id,
                            worker_id=claimed.worker_id,
                            error_code="x_media_upload_context_exit_retryable",
                            retry_base_seconds=retry_base_seconds,
                            retry_max_seconds=retry_max_seconds,
                            provider_request_no=provider_request_no,
                            now=_clock_now(clock),
                        )
                        return PublicationCycleResult(
                            claimed_attempt=True,
                            attempt_id=claimed.attempt_id,
                            state=PublicationAttemptState.RETRY_WAIT,
                            error_code="x_media_upload_context_exit_retryable",
                        )
                    return await _mark_failed_result(
                        sessions,
                        attempt_id=claimed.attempt_id,
                        step_id=snapshot.step_id,
                        worker_id=claimed.worker_id,
                        error_code="x_media_upload_terminal",
                        provider_request_no=provider_request_no,
                        now=_clock_now(clock),
                    )
                except Exception:
                    await _schedule_retry(
                        sessions,
                        attempt_id=claimed.attempt_id,
                        step_id=snapshot.step_id,
                        worker_id=claimed.worker_id,
                        error_code="x_media_upload_unclassified_retryable",
                        retry_base_seconds=retry_base_seconds,
                        retry_max_seconds=retry_max_seconds,
                        provider_request_no=provider_request_no,
                        now=_clock_now(clock),
                    )
                    return PublicationCycleResult(
                        claimed_attempt=True,
                        attempt_id=claimed.attempt_id,
                        state=PublicationAttemptState.RETRY_WAIT,
                        error_code="x_media_upload_unclassified_retryable",
                    )
                await _finish_x_media_upload(
                    sessions,
                    attempt_id=claimed.attempt_id,
                    step_id=snapshot.step_id,
                    worker_id=claimed.worker_id,
                    uploaded=uploaded,
                    expected_size=len(input_bytes.body),
                    provider_request_no=provider_request_no,
                    now=_clock_now(clock),
                )
                continue

            if snapshot.kind == PublicationStepKind.X_CREATE_POST:
                media_check_at = _clock_now(clock)
                media_ids = await _load_x_media_ids(
                    sessions,
                    attempt_id=claimed.attempt_id,
                    now=media_check_at,
                )
                effect_at = _clock_now(clock)
                context = await _begin_effect(
                    sessions,
                    attempt_id=claimed.attempt_id,
                    step_id=snapshot.step_id,
                    worker_id=claimed.worker_id,
                    lease_seconds=lease_seconds,
                    now=effect_at,
                )
                if (
                    x_oauth_provider is None
                    or context.credential_reference is None
                    or expected_x_creator_user_id is None
                ):
                    await _defer_before_provider_request(
                        sessions,
                        attempt_id=claimed.attempt_id,
                        step_id=snapshot.step_id,
                        worker_id=claimed.worker_id,
                        error_code="x_credentials_unavailable",
                        retry_base_seconds=retry_base_seconds,
                        retry_max_seconds=retry_max_seconds,
                        now=_clock_now(clock),
                    )
                    return PublicationCycleResult(
                        claimed_attempt=True,
                        attempt_id=claimed.attempt_id,
                        state=PublicationAttemptState.RETRY_WAIT,
                        error_code="x_credentials_unavailable",
                    )
                text_value = context.configuration.get("text")
                if not isinstance(text_value, str):
                    return await _mark_failed_result(
                        sessions,
                        attempt_id=claimed.attempt_id,
                        step_id=snapshot.step_id,
                        worker_id=claimed.worker_id,
                        error_code="x_configuration_invalid",
                        now=_clock_now(clock),
                    )
                provider_invoked = False
                provider_returned = False
                provider_request_no = None
                try:
                    async with x_oauth_provider.open_for_effect(
                        context.credential_reference
                    ) as oauth:
                        if not hmac.compare_digest(
                            oauth.creator_user_id,
                            expected_x_creator_user_id,
                        ):
                            raise XCredentialUnavailableError("X credential account binding failed")
                        provider_request_no = await _mark_provider_request_started(
                            sessions,
                            attempt_id=claimed.attempt_id,
                            step_id=snapshot.step_id,
                            worker_id=claimed.worker_id,
                            lease_seconds=lease_seconds,
                            now=_clock_now(clock),
                        )
                        irreversible_x_post_request_no = provider_request_no
                        provider_invoked = True
                        post = await oauth.client.create_post(
                            text=text_value,
                            media_ids=media_ids,
                        )
                        provider_returned = True
                except XCredentialUnavailableError:
                    if provider_invoked:
                        return await _mark_unknown_result(
                            sessions,
                            attempt_id=claimed.attempt_id,
                            step_id=snapshot.step_id,
                            worker_id=claimed.worker_id,
                            error_code="x_create_post_credential_exit_unknown",
                            provider_request_no=provider_request_no,
                            now=_clock_now(clock),
                        )
                    await _defer_before_provider_request(
                        sessions,
                        attempt_id=claimed.attempt_id,
                        step_id=snapshot.step_id,
                        worker_id=claimed.worker_id,
                        error_code="x_credentials_unavailable",
                        retry_base_seconds=retry_base_seconds,
                        retry_max_seconds=retry_max_seconds,
                        now=_clock_now(clock),
                    )
                    return PublicationCycleResult(
                        claimed_attempt=True,
                        attempt_id=claimed.attempt_id,
                        state=PublicationAttemptState.RETRY_WAIT,
                        error_code="x_credentials_unavailable",
                    )
                except PublicationConflictError:
                    if provider_invoked:
                        return await _mark_unknown_result(
                            sessions,
                            attempt_id=claimed.attempt_id,
                            step_id=snapshot.step_id,
                            worker_id=claimed.worker_id,
                            error_code="x_create_post_authorization_unknown",
                            provider_request_no=provider_request_no,
                            now=_clock_now(clock),
                        )
                    raise
                except (XRetryableError, XAmbiguousError):
                    # X does not document an idempotency key for POST /2/tweets.
                    # Any transport, timeout, 408/429, or 5xx outcome is UNKNOWN.
                    return await _mark_unknown_result(
                        sessions,
                        attempt_id=claimed.attempt_id,
                        step_id=snapshot.step_id,
                        worker_id=claimed.worker_id,
                        error_code="x_create_post_unknown",
                        provider_request_no=provider_request_no,
                        now=_clock_now(clock),
                    )
                except XProtocolError:
                    # The adapter raises protocol errors after an HTTP success
                    # whose body cannot prove the exact post ID/text. The post
                    # may therefore exist and must never be retried blindly.
                    return await _mark_unknown_result(
                        sessions,
                        attempt_id=claimed.attempt_id,
                        step_id=snapshot.step_id,
                        worker_id=claimed.worker_id,
                        error_code="x_create_post_protocol_unknown",
                        provider_request_no=provider_request_no,
                        now=_clock_now(clock),
                    )
                except (XTerminalError, TypeError, ValueError):
                    if provider_returned:
                        return await _mark_unknown_result(
                            sessions,
                            attempt_id=claimed.attempt_id,
                            step_id=snapshot.step_id,
                            worker_id=claimed.worker_id,
                            error_code="x_create_post_context_exit_unknown",
                            provider_request_no=provider_request_no,
                            now=_clock_now(clock),
                        )
                    return await _mark_failed_result(
                        sessions,
                        attempt_id=claimed.attempt_id,
                        step_id=snapshot.step_id,
                        worker_id=claimed.worker_id,
                        error_code="x_create_post_terminal",
                        provider_request_no=provider_request_no,
                        now=_clock_now(clock),
                    )
                except Exception:
                    return await _mark_unknown_result(
                        sessions,
                        attempt_id=claimed.attempt_id,
                        step_id=snapshot.step_id,
                        worker_id=claimed.worker_id,
                        error_code="x_create_post_unknown",
                        provider_request_no=provider_request_no,
                        now=_clock_now(clock),
                    )
                if post.text != text_value:
                    return await _mark_unknown_result(
                        sessions,
                        attempt_id=claimed.attempt_id,
                        step_id=snapshot.step_id,
                        worker_id=claimed.worker_id,
                        error_code="x_create_post_response_mismatch",
                        provider_request_no=provider_request_no,
                        now=_clock_now(clock),
                    )
                await _finish_x_post(
                    sessions,
                    attempt_id=claimed.attempt_id,
                    step_id=snapshot.step_id,
                    worker_id=claimed.worker_id,
                    post=post,
                    provider_request_no=provider_request_no,
                    now=_clock_now(clock),
                )
                return PublicationCycleResult(
                    claimed_attempt=True,
                    attempt_id=claimed.attempt_id,
                    state=PublicationAttemptState.SUCCEEDED,
                )

            if snapshot.kind == PublicationStepKind.PATREON_PACKAGE:
                prepared = await _prepare_patreon_package(
                    sessions,
                    store,
                    attempt_id=claimed.attempt_id,
                    max_package_bytes=max_package_bytes,
                )
                effect_at = _clock_now(clock)
                context = await _begin_effect(
                    sessions,
                    attempt_id=claimed.attempt_id,
                    step_id=snapshot.step_id,
                    worker_id=claimed.worker_id,
                    lease_seconds=lease_seconds,
                    now=effect_at,
                )
                try:
                    metadata = await _write_or_adopt_package(
                        store,
                        intent_id=context.intent_id,
                        intent_digest=await _intent_digest(
                            sessions,
                            context.intent_id,
                        ),
                        prepared=prepared,
                        max_package_bytes=max_package_bytes,
                    )
                    await _register_package(
                        sessions,
                        store,
                        context=context,
                        prepared=prepared,
                        metadata=metadata,
                        worker_id=claimed.worker_id,
                        now=_clock_now(clock),
                    )
                except (PublicationRuntimeContractError, PatreonHandoffError):
                    return await _mark_failed_result(
                        sessions,
                        attempt_id=claimed.attempt_id,
                        step_id=snapshot.step_id,
                        worker_id=claimed.worker_id,
                        error_code="patreon_package_conflict",
                        now=_clock_now(clock),
                    )
                except ObjectStoreError:
                    await _schedule_retry(
                        sessions,
                        attempt_id=claimed.attempt_id,
                        step_id=snapshot.step_id,
                        worker_id=claimed.worker_id,
                        error_code="patreon_package_storage_retryable",
                        retry_base_seconds=retry_base_seconds,
                        retry_max_seconds=retry_max_seconds,
                        now=_clock_now(clock),
                    )
                    return PublicationCycleResult(
                        claimed_attempt=True,
                        attempt_id=claimed.attempt_id,
                        state=PublicationAttemptState.RETRY_WAIT,
                        error_code="patreon_package_storage_retryable",
                    )
                continue

            if snapshot.kind == PublicationStepKind.PATREON_HANDOFF:
                await _mark_patreon_awaiting_human(
                    sessions,
                    attempt_id=claimed.attempt_id,
                    step_id=snapshot.step_id,
                    worker_id=claimed.worker_id,
                    lease_seconds=lease_seconds,
                    now=_clock_now(clock),
                )
                return PublicationCycleResult(
                    claimed_attempt=True,
                    attempt_id=claimed.attempt_id,
                    state=PublicationAttemptState.AWAITING_HUMAN,
                )
            raise PublicationRuntimeContractError("publication step kind is unsupported")
        except PublicationDisabledError:
            if irreversible_x_post_request_no is not None:
                return await _mark_unknown_result(
                    sessions,
                    attempt_id=claimed.attempt_id,
                    step_id=snapshot.step_id,
                    worker_id=claimed.worker_id,
                    error_code="x_create_post_completion_unknown",
                    provider_request_no=irreversible_x_post_request_no,
                    now=_clock_now(clock),
                )
            await _defer_before_effect(
                sessions,
                attempt_id=claimed.attempt_id,
                step_id=snapshot.step_id,
                worker_id=claimed.worker_id,
                error_code="publication_guard_stopped",
                retry_base_seconds=retry_base_seconds,
                retry_max_seconds=retry_max_seconds,
                now=_clock_now(clock),
            )
            return PublicationCycleResult(
                claimed_attempt=True,
                attempt_id=claimed.attempt_id,
                state=PublicationAttemptState.RETRY_WAIT,
                error_code="publication_guard_stopped",
            )
        except XCredentialUnavailableError:
            if irreversible_x_post_request_no is not None:
                return await _mark_unknown_result(
                    sessions,
                    attempt_id=claimed.attempt_id,
                    step_id=snapshot.step_id,
                    worker_id=claimed.worker_id,
                    error_code="x_create_post_completion_unknown",
                    provider_request_no=irreversible_x_post_request_no,
                    now=_clock_now(clock),
                )
            await _defer_before_provider_request(
                sessions,
                attempt_id=claimed.attempt_id,
                step_id=snapshot.step_id,
                worker_id=claimed.worker_id,
                error_code="x_credentials_unavailable",
                retry_base_seconds=retry_base_seconds,
                retry_max_seconds=retry_max_seconds,
                now=_clock_now(clock),
            )
            return PublicationCycleResult(
                claimed_attempt=True,
                attempt_id=claimed.attempt_id,
                state=PublicationAttemptState.RETRY_WAIT,
                error_code="x_credentials_unavailable",
            )
        except PublicationConflictError:
            if irreversible_x_post_request_no is not None:
                return await _mark_unknown_result(
                    sessions,
                    attempt_id=claimed.attempt_id,
                    step_id=snapshot.step_id,
                    worker_id=claimed.worker_id,
                    error_code="x_create_post_completion_unknown",
                    provider_request_no=irreversible_x_post_request_no,
                    now=_clock_now(clock),
                )
            return await _mark_authorization_expired_result(
                sessions,
                attempt_id=claimed.attempt_id,
                step_id=snapshot.step_id,
                worker_id=claimed.worker_id,
                now=_clock_now(clock),
            )
        except (PublicationRuntimeContractError, PatreonHandoffError, ValueError):
            if irreversible_x_post_request_no is not None:
                return await _mark_unknown_result(
                    sessions,
                    attempt_id=claimed.attempt_id,
                    step_id=snapshot.step_id,
                    worker_id=claimed.worker_id,
                    error_code="x_create_post_completion_unknown",
                    provider_request_no=irreversible_x_post_request_no,
                    now=_clock_now(clock),
                )
            return await _mark_failed_result(
                sessions,
                attempt_id=claimed.attempt_id,
                step_id=snapshot.step_id,
                worker_id=claimed.worker_id,
                error_code="publication_snapshot_invalid",
                now=_clock_now(clock),
            )
        except Exception:
            if irreversible_x_post_request_no is not None:
                return await _mark_unknown_result(
                    sessions,
                    attempt_id=claimed.attempt_id,
                    step_id=snapshot.step_id,
                    worker_id=claimed.worker_id,
                    error_code="x_create_post_completion_unknown",
                    provider_request_no=irreversible_x_post_request_no,
                    now=_clock_now(clock),
                )
            raise


async def _recover_one_expired_lease(
    sessions: async_sessionmaker[AsyncSession],
    *,
    now: datetime,
) -> bool:
    async with sessions() as session:
        attempt = await session.scalar(
            select(PublicationAttempt)
            .where(
                PublicationAttempt.state.in_(
                    (
                        PublicationAttemptState.CLAIMED,
                        PublicationAttemptState.PROCESSING,
                    )
                ),
                PublicationAttempt.lease_expires_at <= now,
            )
            .order_by(PublicationAttempt.lease_expires_at, PublicationAttempt.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if attempt is None:
            await session.rollback()
            return False
        intent = await session.scalar(
            select(PublicationIntent)
            .where(PublicationIntent.id == attempt.intent_id)
            .with_for_update()
        )
        if intent is None:
            raise PublicationRuntimeContractError("publication intent is unavailable")
        step = await session.scalar(
            select(PublicationStep)
            .where(
                PublicationStep.attempt_id == attempt.id,
                PublicationStep.state == PublicationStepState.PROCESSING,
            )
            .order_by(PublicationStep.ordinal)
            .limit(1)
            .with_for_update()
        )
        active_request: PublicationEffectEvent | None = None
        if step is not None:
            latest_start = await session.scalar(
                select(PublicationEffectEvent)
                .where(
                    PublicationEffectEvent.step_id == step.id,
                    PublicationEffectEvent.event_type == "started",
                )
                .order_by(PublicationEffectEvent.request_no.desc())
                .limit(1)
            )
            if latest_start is not None:
                completion = await session.scalar(
                    select(PublicationEffectEvent.id).where(
                        PublicationEffectEvent.step_id == step.id,
                        PublicationEffectEvent.request_no == latest_start.request_no,
                        PublicationEffectEvent.is_completion.is_(True),
                    )
                )
                if completion is None:
                    active_request = latest_start
        attempt.lease_owner = None
        attempt.lease_expires_at = None
        attempt.lock_version += 1
        if (
            step is not None
            and active_request is not None
            and step.effect_completed_at is None
            and step.kind == PublicationStepKind.X_CREATE_POST
        ):
            await _append_effect_completion(
                session,
                step=step,
                request_no=active_request.request_no,
                event_type="unknown",
                remote_identifier=None,
                remote_expires_at=None,
                error_code="expired_effect_lease_unknown",
                now=now,
            )
            step.state = PublicationStepState.UNKNOWN
            step.retry_class = PublicationRetryClass.UNKNOWN
            step.retry_at = None
            step.last_error_code = "expired_effect_lease_unknown"
            step.last_error_detail = _SAFE_UNKNOWN_ERROR
            step.updated_at = now
            step.lock_version += 1
            attempt.state = PublicationAttemptState.UNKNOWN
            attempt.retry_at = None
            attempt.last_error_code = "expired_effect_lease_unknown"
            attempt.last_error_detail = _SAFE_UNKNOWN_ERROR
            intent.state = PublicationIntentState.UNKNOWN
            intent.last_error_code = "expired_effect_lease_unknown"
            intent.last_error_detail = _SAFE_UNKNOWN_ERROR
        elif attempt.attempt_count >= attempt.max_attempts:
            if step is not None:
                if active_request is not None and step.kind == PublicationStepKind.X_MEDIA_UPLOAD:
                    await _append_effect_completion(
                        session,
                        step=step,
                        request_no=active_request.request_no,
                        event_type="retryable",
                        remote_identifier=None,
                        remote_expires_at=None,
                        error_code="expired_media_upload_retryable",
                        now=now,
                    )
                step.state = PublicationStepState.FAILED
                step.retry_class = PublicationRetryClass.TERMINAL
                step.retry_at = None
                step.last_error_code = "publication_attempts_exhausted"
                step.last_error_detail = _SAFE_RUNTIME_ERROR
                step.updated_at = now
                step.lock_version += 1
            attempt.state = PublicationAttemptState.FAILED
            attempt.retry_at = None
            attempt.completed_at = now
            attempt.last_error_code = "publication_attempts_exhausted"
            attempt.last_error_detail = _SAFE_RUNTIME_ERROR
            intent.state = PublicationIntentState.FAILED
            intent.last_error_code = "publication_attempts_exhausted"
            intent.last_error_detail = _SAFE_RUNTIME_ERROR
        else:
            if step is not None:
                if active_request is not None and step.kind == PublicationStepKind.X_MEDIA_UPLOAD:
                    await _append_effect_completion(
                        session,
                        step=step,
                        request_no=active_request.request_no,
                        event_type="retryable",
                        remote_identifier=None,
                        remote_expires_at=None,
                        error_code="expired_media_upload_retryable",
                        now=now,
                    )
                step.state = PublicationStepState.RETRY_WAIT
                step.retry_class = PublicationRetryClass.SAFE_RETRY
                step.retry_at = now
                step.last_error_code = "expired_safe_effect_lease"
                step.last_error_detail = _SAFE_RUNTIME_ERROR
                step.updated_at = now
                step.lock_version += 1
            attempt.state = PublicationAttemptState.RETRY_WAIT
            attempt.retry_at = now
            attempt.last_error_code = "expired_safe_effect_lease"
            attempt.last_error_detail = _SAFE_RUNTIME_ERROR
        session.add(
            _runtime_audit(
                action="publication.attempt_lease_recovered",
                intent_id=intent.id,
                attempt_id=attempt.id,
                detail={
                    "state": attempt.state.value,
                    "requires_reconciliation": (attempt.state == PublicationAttemptState.UNKNOWN),
                },
                now=now,
            )
        )
        await session.commit()
        return True


async def _claim_one_attempt(
    sessions: async_sessionmaker[AsyncSession],
    *,
    worker_id: str,
    lease_seconds: int,
    now: datetime,
) -> _ClaimedAttempt | None:
    async with sessions() as session:
        due = or_(
            PublicationAttempt.state == PublicationAttemptState.QUEUED,
            (
                (PublicationAttempt.state == PublicationAttemptState.RETRY_WAIT)
                & (PublicationAttempt.retry_at <= now)
            ),
        )
        attempt = await session.scalar(
            select(PublicationAttempt)
            .where(due, PublicationAttempt.available_at <= now)
            .order_by(PublicationAttempt.available_at, PublicationAttempt.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if attempt is None:
            await session.rollback()
            return None
        intent = await session.scalar(
            select(PublicationIntent)
            .where(PublicationIntent.id == attempt.intent_id)
            .with_for_update()
        )
        if intent is None:
            raise PublicationRuntimeContractError("publication intent is unavailable")
        if attempt.attempt_count >= attempt.max_attempts:
            attempt.state = PublicationAttemptState.FAILED
            attempt.retry_at = None
            attempt.completed_at = now
            attempt.last_error_code = "publication_attempts_exhausted"
            attempt.last_error_detail = _SAFE_RUNTIME_ERROR
            attempt.lock_version += 1
            intent.state = PublicationIntentState.FAILED
            intent.last_error_code = "publication_attempts_exhausted"
            intent.last_error_detail = _SAFE_RUNTIME_ERROR
            await session.commit()
            return None
        attempt.state = PublicationAttemptState.CLAIMED
        attempt.attempt_count += 1
        attempt.lock_version += 1
        attempt.retry_at = None
        attempt.lease_owner = worker_id
        attempt.lease_expires_at = now + timedelta(seconds=lease_seconds)
        attempt.claimed_at = now
        attempt.completed_at = None
        await session.commit()
        return _ClaimedAttempt(attempt_id=attempt.id, worker_id=worker_id)


async def _next_step_snapshot(
    sessions: async_sessionmaker[AsyncSession],
    *,
    attempt_id: UUID,
    worker_id: str,
    now: datetime,
) -> _EffectContext | None:
    async with sessions() as session:
        attempt = await session.get(PublicationAttempt, attempt_id)
        if (
            attempt is None
            or attempt.lease_owner != worker_id
            or attempt.lease_expires_at is None
            or _as_utc(attempt.lease_expires_at) <= now
            or attempt.state
            not in {
                PublicationAttemptState.CLAIMED,
                PublicationAttemptState.PROCESSING,
            }
        ):
            return None
        intent = await session.get(PublicationIntent, attempt.intent_id)
        if intent is None:
            raise PublicationRuntimeContractError("publication intent is unavailable")
        step = await session.scalar(
            select(PublicationStep)
            .where(
                PublicationStep.attempt_id == attempt.id,
                PublicationStep.state.in_(
                    (
                        PublicationStepState.PENDING,
                        PublicationStepState.RETRY_WAIT,
                    )
                ),
            )
            .order_by(PublicationStep.ordinal)
            .limit(1)
        )
        if step is None:
            return None
        return _EffectContext(
            attempt_id=attempt.id,
            intent_id=intent.id,
            approval_id=attempt.approval_id,
            step_id=step.id,
            kind=step.kind,
            guard_epoch=0,
            credential_reference=intent.credential_reference,
            configuration=cast(dict[str, object], dict(intent.configuration)),
        )


async def _begin_effect(
    sessions: async_sessionmaker[AsyncSession],
    *,
    attempt_id: UUID,
    step_id: UUID,
    worker_id: str,
    lease_seconds: int,
    now: datetime,
) -> _EffectContext:
    async with sessions() as session:
        attempt = await session.scalar(
            select(PublicationAttempt).where(PublicationAttempt.id == attempt_id).with_for_update()
        )
        if (
            attempt is None
            or attempt.lease_owner != worker_id
            or attempt.lease_expires_at is None
            or _as_utc(attempt.lease_expires_at) <= now
            or attempt.state
            not in {
                PublicationAttemptState.CLAIMED,
                PublicationAttemptState.PROCESSING,
            }
        ):
            raise PublicationConflictError("publication attempt lease is unavailable")
        intent = await session.scalar(
            select(PublicationIntent)
            .where(PublicationIntent.id == attempt.intent_id)
            .with_for_update()
        )
        step = await session.scalar(
            select(PublicationStep)
            .where(
                PublicationStep.id == step_id,
                PublicationStep.attempt_id == attempt.id,
            )
            .with_for_update()
        )
        if intent is None or step is None:
            raise PublicationRuntimeContractError("publication effect snapshot is unavailable")
        if step.state not in {
            PublicationStepState.PENDING,
            PublicationStepState.RETRY_WAIT,
        }:
            raise PublicationConflictError("publication step is not effect-ready")
        prior_incomplete = await session.scalar(
            select(PublicationStep.id)
            .where(
                PublicationStep.attempt_id == attempt.id,
                PublicationStep.ordinal < step.ordinal,
                PublicationStep.state != PublicationStepState.SUCCEEDED,
            )
            .limit(1)
        )
        if prior_incomplete is not None:
            raise PublicationRuntimeContractError("publication steps cannot execute out of order")
        _, guard = await require_effect_authorization(
            session,
            intent=intent,
            approval_id=attempt.approval_id,
            now=now,
        )
        attempt.state = PublicationAttemptState.PROCESSING
        attempt.processing_started_at = attempt.processing_started_at or now
        attempt.lease_expires_at = now + timedelta(seconds=lease_seconds)
        attempt.lock_version += 1
        intent.state = PublicationIntentState.PROCESSING
        step.state = PublicationStepState.PROCESSING
        step.retry_at = None
        step.effect_started_at = step.effect_started_at or now
        step.guard_epoch = guard.epoch
        step.updated_at = now
        step.lock_version += 1
        await session.commit()
        return _EffectContext(
            attempt_id=attempt.id,
            intent_id=intent.id,
            approval_id=attempt.approval_id,
            step_id=step.id,
            kind=step.kind,
            guard_epoch=guard.epoch,
            credential_reference=intent.credential_reference,
            configuration=cast(dict[str, object], dict(intent.configuration)),
        )


async def _mark_provider_request_started(
    sessions: async_sessionmaker[AsyncSession],
    *,
    attempt_id: UUID,
    step_id: UUID,
    worker_id: str,
    lease_seconds: int,
    now: datetime,
) -> int:
    """Recheck authorization and durably mark the instant before an X API call."""

    async with sessions() as session:
        attempt = await session.scalar(
            select(PublicationAttempt).where(PublicationAttempt.id == attempt_id).with_for_update()
        )
        if attempt is None:
            raise PublicationConflictError("publication attempt is unavailable")
        intent = await session.scalar(
            select(PublicationIntent)
            .where(PublicationIntent.id == attempt.intent_id)
            .with_for_update()
        )
        step = await session.scalar(
            select(PublicationStep)
            .where(
                PublicationStep.id == step_id,
                PublicationStep.attempt_id == attempt.id,
            )
            .with_for_update()
        )
        if intent is None or step is None:
            raise PublicationRuntimeContractError("X request snapshot is unavailable")
        _require_execution_lease(attempt, worker_id)
        if attempt.lease_expires_at is None or _as_utc(attempt.lease_expires_at) <= now:
            raise PublicationConflictError("publication attempt lease expired")
        if step.state != PublicationStepState.PROCESSING or step.kind not in {
            PublicationStepKind.X_MEDIA_UPLOAD,
            PublicationStepKind.X_CREATE_POST,
        }:
            raise PublicationConflictError("X provider request is not startable")
        latest_request_no = int(
            await session.scalar(
                select(func.max(PublicationEffectEvent.request_no)).where(
                    PublicationEffectEvent.step_id == step.id,
                    PublicationEffectEvent.event_type == "started",
                )
            )
            or 0
        )
        if latest_request_no:
            completed = await session.scalar(
                select(PublicationEffectEvent.id).where(
                    PublicationEffectEvent.step_id == step.id,
                    PublicationEffectEvent.request_no == latest_request_no,
                    PublicationEffectEvent.is_completion.is_(True),
                )
            )
            if completed is None:
                raise PublicationConflictError("previous X provider request outcome is unresolved")
        request_no = latest_request_no + 1
        _, guard = await require_effect_authorization(
            session,
            intent=intent,
            approval_id=attempt.approval_id,
            now=now,
        )
        attempt.lease_expires_at = now + timedelta(seconds=lease_seconds)
        attempt.lock_version += 1
        step.guard_epoch = guard.epoch
        step.updated_at = now
        step.lock_version += 1
        session.add(
            PublicationEffectEvent(
                id=uuid7(),
                step_id=step.id,
                request_no=request_no,
                step_kind=step.kind,
                event_type="started",
                is_completion=False,
                guard_epoch=guard.epoch,
                recorded_at=now,
            )
        )
        await session.commit()
        return request_no


async def _read_step_input(
    sessions: async_sessionmaker[AsyncSession],
    store: ObjectStore,
    *,
    attempt_id: UUID,
    step_id: UUID,
) -> _InputBytes:
    async with sessions() as session:
        row = (
            await session.execute(
                select(PublicationInput, PublicationStep)
                .join(
                    PublicationStep,
                    PublicationStep.publication_input_id == PublicationInput.id,
                )
                .where(
                    PublicationStep.id == step_id,
                    PublicationStep.attempt_id == attempt_id,
                    PublicationStep.kind == PublicationStepKind.X_MEDIA_UPLOAD,
                )
            )
        ).one_or_none()
        if row is None:
            raise PublicationRuntimeContractError("X upload input is unavailable")
        publication_input = row[0]
    return _InputBytes(
        publication_input=publication_input,
        body=await _read_exact_input(store, publication_input),
    )


async def _read_exact_input(
    store: ObjectStore,
    publication_input: PublicationInput,
) -> bytes:
    if (
        store.backend != publication_input.asset_storage_backend
        or store.bucket != publication_input.asset_storage_bucket
    ):
        raise PublicationRuntimeContractError("publication input storage binding is unavailable")
    body = await store.read_bytes(
        publication_input.asset_object_key,
        max_bytes=publication_input.asset_byte_size,
        version_id=publication_input.asset_object_version_id,
    )
    if len(body) != publication_input.asset_byte_size or not hmac.compare_digest(
        hashlib.sha256(body).hexdigest(),
        publication_input.asset_sha256,
    ):
        raise PublicationRuntimeContractError(
            "publication input bytes do not match the frozen snapshot"
        )
    return body


async def _load_x_media_ids(
    sessions: async_sessionmaker[AsyncSession],
    *,
    attempt_id: UUID,
    now: datetime,
) -> tuple[str, ...]:
    async with sessions() as session:
        steps = tuple(
            (
                await session.scalars(
                    select(PublicationStep)
                    .where(
                        PublicationStep.attempt_id == attempt_id,
                        PublicationStep.kind == PublicationStepKind.X_MEDIA_UPLOAD,
                    )
                    .order_by(PublicationStep.ordinal)
                )
            ).all()
        )
        if not steps:
            raise PublicationRuntimeContractError("X upload steps are unavailable")
        media_ids: list[str] = []
        for step in steps:
            if (
                step.state != PublicationStepState.SUCCEEDED
                or step.remote_identifier is None
                or step.remote_expires_at is None
                or _as_utc(step.remote_expires_at)
                <= now + timedelta(seconds=_X_MEDIA_EXPIRY_MARGIN_SECONDS)
            ):
                raise PublicationRuntimeContractError("X uploaded media is incomplete or expired")
            media_ids.append(step.remote_identifier)
        return tuple(media_ids)


async def _finish_x_media_upload(
    sessions: async_sessionmaker[AsyncSession],
    *,
    attempt_id: UUID,
    step_id: UUID,
    worker_id: str,
    uploaded: XUploadedMedia,
    expected_size: int,
    provider_request_no: int,
    now: datetime,
) -> None:
    if uploaded.size != expected_size or uploaded.expires_after_seconds <= 0:
        raise PublicationRuntimeContractError("X upload result is inconsistent")
    async with sessions() as session:
        attempt, intent, step = await _lock_execution_rows(
            session,
            attempt_id=attempt_id,
            step_id=step_id,
        )
        _require_execution_lease(attempt, worker_id)
        await _append_effect_completion(
            session,
            step=step,
            request_no=provider_request_no,
            event_type="succeeded",
            remote_identifier=uploaded.id,
            remote_expires_at=now + timedelta(seconds=uploaded.expires_after_seconds),
            error_code=None,
            now=now,
        )
        step.state = PublicationStepState.SUCCEEDED
        step.retry_class = None
        step.remote_identifier = uploaded.id
        step.remote_expires_at = now + timedelta(seconds=uploaded.expires_after_seconds)
        step.effect_completed_at = now
        step.last_error_code = None
        step.last_error_detail = None
        step.updated_at = now
        step.lock_version += 1
        session.add(
            _runtime_audit(
                action="publication.x_media_uploaded",
                intent_id=intent.id,
                attempt_id=attempt.id,
                detail={
                    "step_id": str(step.id),
                    "step_ordinal": step.ordinal,
                    "remote_identifier_sha256": canonical_sha256(uploaded.id),
                    "expires_at": _canonical_datetime(step.remote_expires_at),
                },
                now=now,
            )
        )
        await session.commit()


async def _finish_x_post(
    sessions: async_sessionmaker[AsyncSession],
    *,
    attempt_id: UUID,
    step_id: UUID,
    worker_id: str,
    post: XPost,
    provider_request_no: int,
    now: datetime,
) -> None:
    async with sessions() as session:
        attempt, intent, step = await _lock_execution_rows(
            session,
            attempt_id=attempt_id,
            step_id=step_id,
        )
        _require_execution_lease(attempt, worker_id)
        await _append_effect_completion(
            session,
            step=step,
            request_no=provider_request_no,
            event_type="succeeded",
            remote_identifier=post.id,
            remote_expires_at=None,
            error_code=None,
            now=now,
        )
        step.state = PublicationStepState.SUCCEEDED
        step.retry_class = None
        step.remote_identifier = post.id
        step.effect_completed_at = now
        step.last_error_code = None
        step.last_error_detail = None
        step.updated_at = now
        step.lock_version += 1
        attempt.state = PublicationAttemptState.SUCCEEDED
        attempt.lease_owner = None
        attempt.lease_expires_at = None
        attempt.retry_at = None
        attempt.completed_at = now
        attempt.last_error_code = None
        attempt.last_error_detail = None
        attempt.lock_version += 1
        intent.state = PublicationIntentState.PUBLISHED
        intent.completed_at = now
        intent.last_error_code = None
        intent.last_error_detail = None
        session.add(
            _runtime_audit(
                action="publication.x_post_created",
                intent_id=intent.id,
                attempt_id=attempt.id,
                detail={
                    "step_id": str(step.id),
                    "remote_identifier_sha256": canonical_sha256(post.id),
                },
                now=now,
            )
        )
        await session.commit()


async def _prepare_patreon_package(
    sessions: async_sessionmaker[AsyncSession],
    store: ObjectStore,
    *,
    attempt_id: UUID,
    max_package_bytes: int,
) -> _PatreonPreparedPackage:
    async with sessions() as session:
        attempt = await session.get(PublicationAttempt, attempt_id)
        if attempt is None:
            raise PublicationRuntimeContractError("publication attempt is unavailable")
        intent = await session.get(PublicationIntent, attempt.intent_id)
        if intent is None or intent.target != PublicationTarget.PATREON:
            raise PublicationRuntimeContractError("Patreon intent is unavailable")
        inputs = tuple(
            (
                await session.scalars(
                    select(PublicationInput)
                    .where(PublicationInput.intent_id == intent.id)
                    .order_by(PublicationInput.ordinal)
                )
            ).all()
        )
        configuration = dict(intent.configuration)
        attester_name = intent.public_preview_attester_name
        attested_at = intent.public_preview_attested_at
        scheduled_at = intent.scheduled_at
    if attester_name is None or attested_at is None:
        raise PublicationRuntimeContractError("Patreon public preview attestation is unavailable")
    content_inputs = [item for item in inputs if item.role == "patreon_content"]
    preview_inputs = [item for item in inputs if item.role == "patreon_preview"]
    if len(preview_inputs) != 1 or not content_inputs:
        raise PublicationRuntimeContractError("Patreon input roles are invalid")
    if any(item.asset_content_type not in {"image/jpeg", "image/png"} for item in inputs):
        raise PublicationRuntimeContractError("Patreon package inputs must be JPEG or PNG")
    content_bytes = await asyncio.gather(
        *(_read_exact_input(store, item) for item in content_inputs)
    )
    preview_bytes = await _read_exact_input(store, preview_inputs[0])

    title = configuration.get("title")
    body = configuration.get("body")
    tier = configuration.get("tier")
    tags = configuration.get("tags")
    if (
        not isinstance(title, str)
        or not isinstance(body, str)
        or not isinstance(tier, str)
        or not isinstance(tags, list)
        or not all(isinstance(tag, str) for tag in tags)
    ):
        raise PublicationRuntimeContractError("Patreon configuration is invalid")
    approved = tuple(
        PatreonPackageImage(
            filename=f"content-{index:03d}{_image_extension(item.asset_content_type)}",
            data=data,
        )
        for index, (item, data) in enumerate(
            zip(content_inputs, content_bytes, strict=True),
            start=1,
        )
    )
    preview = PatreonPackageImage(
        filename=f"public-preview{_image_extension(preview_inputs[0].asset_content_type)}",
        data=preview_bytes,
    )
    package = await asyncio.to_thread(
        build_patreon_handoff_package,
        approved_derivatives=approved,
        public_preview=preview,
        title=title,
        body=body,
        tier=tier,
        tags=cast(list[str], tags),
        scheduled_at=(_as_utc(scheduled_at) if scheduled_at is not None else None),
        public_preview_attestation=PublicPreviewSafetyAttestation(
            safe_for_public=True,
            attested_by=attester_name,
            attested_at=_as_utc(attested_at),
        ),
    )
    if len(package.archive_bytes) > max_package_bytes:
        raise PublicationRuntimeContractError("Patreon package exceeds its byte limit")
    return _PatreonPreparedPackage(
        archive_bytes=package.archive_bytes,
        sha256=package.sha256,
        manifest_sha256=package.manifest_sha256,
    )


async def _write_or_adopt_package(
    store: ObjectStore,
    *,
    intent_id: UUID,
    intent_digest: str,
    prepared: _PatreonPreparedPackage,
    max_package_bytes: int,
) -> ObjectMetadata:
    key = f"publication-packages/{intent_id}/{prepared.sha256}.zip"
    expected_metadata = {
        "sha256": prepared.sha256,
        "intent-digest": intent_digest,
        "manifest-sha256": prepared.manifest_sha256,
        "publication-intent-id": str(intent_id),
    }
    try:
        metadata = await store.write_bytes_if_absent(
            key=key,
            body=prepared.archive_bytes,
            content_type=_PACKAGE_CONTENT_TYPE,
            metadata=expected_metadata,
            max_bytes=max_package_bytes,
        )
    except ObjectAlreadyExistsError:
        existing_metadata = await store.head(key)
        if existing_metadata is None:
            raise PublicationRuntimeContractError(
                "existing Patreon package is unavailable"
            ) from None
        metadata = existing_metadata
    if (
        metadata.key != key
        or metadata.version_id is None
        or metadata.byte_size != len(prepared.archive_bytes)
        or metadata.content_type != _PACKAGE_CONTENT_TYPE
        or any(metadata.metadata.get(k) != v for k, v in expected_metadata.items())
    ):
        raise PublicationRuntimeContractError(
            "existing Patreon package conflicts with the frozen intent"
        )
    stored = await store.read_bytes(
        key,
        max_bytes=max_package_bytes,
        version_id=metadata.version_id,
    )
    if (
        len(stored) != len(prepared.archive_bytes)
        or not hmac.compare_digest(hashlib.sha256(stored).hexdigest(), prepared.sha256)
        or not hmac.compare_digest(stored, prepared.archive_bytes)
    ):
        raise PublicationRuntimeContractError("existing Patreon package bytes do not match")
    return metadata


async def _register_package(
    sessions: async_sessionmaker[AsyncSession],
    store: ObjectStore,
    *,
    context: _EffectContext,
    prepared: _PatreonPreparedPackage,
    metadata: ObjectMetadata,
    worker_id: str,
    now: datetime,
) -> None:
    if metadata.version_id is None:
        raise PublicationRuntimeContractError(
            "package storage did not return an immutable version ID"
        )
    async with sessions() as session:
        attempt, intent, step = await _lock_execution_rows(
            session,
            attempt_id=context.attempt_id,
            step_id=context.step_id,
        )
        _require_execution_lease(attempt, worker_id)
        package = await session.scalar(
            select(PublicationPackage).where(PublicationPackage.intent_id == intent.id)
        )
        if package is None:
            package = PublicationPackage(
                id=uuid7(),
                intent_id=intent.id,
                storage_backend=store.backend,
                storage_bucket=store.bucket,
                object_key=metadata.key,
                object_version_id=metadata.version_id,
                sha256=prepared.sha256,
                manifest_sha256=prepared.manifest_sha256,
                byte_size=metadata.byte_size,
                content_type=_PACKAGE_CONTENT_TYPE,
                created_at=now,
            )
            session.add(package)
            try:
                await session.flush()
            except IntegrityError as error:
                await session.rollback()
                raise PublicationRuntimeContractError(
                    "Patreon package was registered concurrently"
                ) from error
        elif (
            package.storage_backend != store.backend
            or package.storage_bucket != store.bucket
            or package.object_key != metadata.key
            or package.object_version_id != metadata.version_id
            or package.sha256 != prepared.sha256
            or package.manifest_sha256 != prepared.manifest_sha256
            or package.byte_size != metadata.byte_size
            or package.content_type != _PACKAGE_CONTENT_TYPE
        ):
            raise PublicationRuntimeContractError(
                "registered Patreon package conflicts with storage"
            )
        step.state = PublicationStepState.SUCCEEDED
        step.retry_class = None
        step.package_id = package.id
        step.effect_completed_at = now
        step.last_error_code = None
        step.last_error_detail = None
        step.updated_at = now
        step.lock_version += 1
        session.add(
            _runtime_audit(
                action="publication.patreon_package_ready",
                intent_id=intent.id,
                attempt_id=attempt.id,
                detail={
                    "step_id": str(step.id),
                    "package_id": str(package.id),
                    "package_sha256": prepared.sha256,
                    "manifest_sha256": prepared.manifest_sha256,
                    "byte_size": metadata.byte_size,
                },
                now=now,
            )
        )
        await session.commit()


async def _mark_patreon_awaiting_human(
    sessions: async_sessionmaker[AsyncSession],
    *,
    attempt_id: UUID,
    step_id: UUID,
    worker_id: str,
    lease_seconds: int,
    now: datetime,
) -> None:
    # Handoff state itself is not an external provider mutation, but authorization
    # and the durable guard are still checked before exposing the package.
    context = await _begin_effect(
        sessions,
        attempt_id=attempt_id,
        step_id=step_id,
        worker_id=worker_id,
        lease_seconds=lease_seconds,
        now=now,
    )
    async with sessions() as session:
        attempt, intent, step = await _lock_execution_rows(
            session,
            attempt_id=context.attempt_id,
            step_id=context.step_id,
        )
        _require_execution_lease(attempt, worker_id)
        package_id = await session.scalar(
            select(PublicationPackage.id).where(PublicationPackage.intent_id == intent.id)
        )
        if package_id is None:
            raise PublicationRuntimeContractError("Patreon package is unavailable")
        step.state = PublicationStepState.AWAITING_HUMAN
        step.effect_completed_at = now
        step.package_id = package_id
        step.updated_at = now
        step.lock_version += 1
        attempt.state = PublicationAttemptState.AWAITING_HUMAN
        attempt.lease_owner = None
        attempt.lease_expires_at = None
        attempt.retry_at = None
        attempt.lock_version += 1
        intent.state = PublicationIntentState.AWAITING_HUMAN
        session.add(
            _runtime_audit(
                action="publication.patreon_handoff_awaiting_human",
                intent_id=intent.id,
                attempt_id=attempt.id,
                detail={
                    "step_id": str(step.id),
                    "package_id": str(package_id),
                    "publishing_mode": "official_ui_only",
                },
                now=now,
            )
        )
        await session.commit()


async def _schedule_retry(
    sessions: async_sessionmaker[AsyncSession],
    *,
    attempt_id: UUID,
    step_id: UUID,
    worker_id: str,
    error_code: str,
    retry_base_seconds: int,
    retry_max_seconds: int,
    provider_request_no: int | None = None,
    now: datetime,
) -> None:
    async with sessions() as session:
        attempt, intent, step = await _lock_execution_rows(
            session,
            attempt_id=attempt_id,
            step_id=step_id,
        )
        _require_execution_lease(attempt, worker_id)
        if provider_request_no is not None:
            await _append_effect_completion(
                session,
                step=step,
                request_no=provider_request_no,
                event_type="retryable",
                remote_identifier=None,
                remote_expires_at=None,
                error_code=error_code,
                now=now,
            )
        if step.retry_count >= step.max_retries or attempt.attempt_count >= attempt.max_attempts:
            await _transition_failed(
                session,
                attempt=attempt,
                intent=intent,
                step=step,
                error_code=f"{error_code}_exhausted",
                now=now,
            )
            await session.commit()
            return
        step.retry_count += 1
        delay = min(
            retry_max_seconds,
            retry_base_seconds * (2 ** (step.retry_count - 1)),
        )
        retry_at = now + timedelta(seconds=delay)
        step.state = PublicationStepState.RETRY_WAIT
        step.retry_class = PublicationRetryClass.SAFE_RETRY
        step.retry_at = retry_at
        step.last_error_code = error_code
        step.last_error_detail = _SAFE_RUNTIME_ERROR
        step.updated_at = now
        step.lock_version += 1
        attempt.state = PublicationAttemptState.RETRY_WAIT
        attempt.lease_owner = None
        attempt.lease_expires_at = None
        attempt.retry_at = retry_at
        attempt.last_error_code = error_code
        attempt.last_error_detail = _SAFE_RUNTIME_ERROR
        attempt.lock_version += 1
        intent.state = PublicationIntentState.READY
        intent.last_error_code = error_code
        intent.last_error_detail = _SAFE_RUNTIME_ERROR
        await session.commit()


async def _defer_before_effect(
    sessions: async_sessionmaker[AsyncSession],
    *,
    attempt_id: UUID,
    step_id: UUID,
    worker_id: str,
    error_code: str,
    retry_base_seconds: int,
    retry_max_seconds: int,
    now: datetime,
) -> None:
    async with sessions() as session:
        attempt, intent, step = await _lock_execution_rows(
            session,
            attempt_id=attempt_id,
            step_id=step_id,
        )
        _require_execution_lease(attempt, worker_id)
        delay = min(retry_max_seconds, retry_base_seconds)
        retry_at = now + timedelta(seconds=delay)
        step.state = PublicationStepState.RETRY_WAIT
        step.retry_class = PublicationRetryClass.SAFE_RETRY
        step.retry_at = retry_at
        step.last_error_code = error_code
        step.last_error_detail = "Publication remains stopped before external effect."
        step.updated_at = now
        step.lock_version += 1
        attempt.state = PublicationAttemptState.RETRY_WAIT
        attempt.lease_owner = None
        attempt.lease_expires_at = None
        attempt.retry_at = retry_at
        attempt.last_error_code = error_code
        attempt.last_error_detail = "Publication remains stopped before external effect."
        attempt.lock_version += 1
        intent.state = PublicationIntentState.READY
        intent.last_error_code = error_code
        intent.last_error_detail = "Publication remains stopped before external effect."
        await session.commit()


async def _defer_before_provider_request(
    sessions: async_sessionmaker[AsyncSession],
    *,
    attempt_id: UUID,
    step_id: UUID,
    worker_id: str,
    error_code: str,
    retry_base_seconds: int,
    retry_max_seconds: int,
    now: datetime,
) -> None:
    # Credential resolution is contractually guaranteed to occur before an X API
    # request. It is therefore safe to retry even though the durable step records
    # the broader effect boundary as started.
    await _schedule_retry(
        sessions,
        attempt_id=attempt_id,
        step_id=step_id,
        worker_id=worker_id,
        error_code=error_code,
        retry_base_seconds=retry_base_seconds,
        retry_max_seconds=retry_max_seconds,
        now=now,
    )


async def _mark_unknown_result(
    sessions: async_sessionmaker[AsyncSession],
    *,
    attempt_id: UUID,
    step_id: UUID,
    worker_id: str,
    error_code: str,
    provider_request_no: int | None = None,
    now: datetime,
) -> PublicationCycleResult:
    async with sessions() as session:
        attempt, intent, step = await _lock_execution_rows(
            session,
            attempt_id=attempt_id,
            step_id=step_id,
        )
        _require_execution_lease(attempt, worker_id)
        if provider_request_no is not None:
            await _append_effect_completion(
                session,
                step=step,
                request_no=provider_request_no,
                event_type="unknown",
                remote_identifier=None,
                remote_expires_at=None,
                error_code=error_code,
                now=now,
            )
        step.state = PublicationStepState.UNKNOWN
        step.retry_class = PublicationRetryClass.UNKNOWN
        step.retry_at = None
        step.last_error_code = error_code
        step.last_error_detail = _SAFE_UNKNOWN_ERROR
        step.updated_at = now
        step.lock_version += 1
        attempt.state = PublicationAttemptState.UNKNOWN
        attempt.lease_owner = None
        attempt.lease_expires_at = None
        attempt.retry_at = None
        attempt.last_error_code = error_code
        attempt.last_error_detail = _SAFE_UNKNOWN_ERROR
        attempt.lock_version += 1
        intent.state = PublicationIntentState.UNKNOWN
        intent.last_error_code = error_code
        intent.last_error_detail = _SAFE_UNKNOWN_ERROR
        session.add(
            _runtime_audit(
                action="publication.effect_outcome_unknown",
                intent_id=intent.id,
                attempt_id=attempt.id,
                detail={
                    "step_id": str(step.id),
                    "step_kind": step.kind.value,
                    "error_code": error_code,
                    "automatic_retry_allowed": False,
                },
                now=now,
            )
        )
        await session.commit()
        return PublicationCycleResult(
            claimed_attempt=True,
            attempt_id=attempt.id,
            state=PublicationAttemptState.UNKNOWN,
            error_code=error_code,
        )


async def _mark_failed_result(
    sessions: async_sessionmaker[AsyncSession],
    *,
    attempt_id: UUID,
    step_id: UUID,
    worker_id: str,
    error_code: str,
    provider_request_no: int | None = None,
    now: datetime,
) -> PublicationCycleResult:
    async with sessions() as session:
        attempt, intent, step = await _lock_execution_rows(
            session,
            attempt_id=attempt_id,
            step_id=step_id,
        )
        _require_execution_lease(attempt, worker_id)
        if provider_request_no is not None:
            await _append_effect_completion(
                session,
                step=step,
                request_no=provider_request_no,
                event_type="terminal",
                remote_identifier=None,
                remote_expires_at=None,
                error_code=error_code,
                now=now,
            )
        await _transition_failed(
            session,
            attempt=attempt,
            intent=intent,
            step=step,
            error_code=error_code,
            now=now,
        )
        await session.commit()
        return PublicationCycleResult(
            claimed_attempt=True,
            attempt_id=attempt.id,
            state=PublicationAttemptState.FAILED,
            error_code=error_code,
        )


async def _mark_authorization_expired_result(
    sessions: async_sessionmaker[AsyncSession],
    *,
    attempt_id: UUID,
    step_id: UUID,
    worker_id: str,
    now: datetime,
) -> PublicationCycleResult:
    async with sessions() as session:
        attempt, intent, step = await _lock_execution_rows(
            session,
            attempt_id=attempt_id,
            step_id=step_id,
        )
        _require_execution_lease(attempt, worker_id)
        step.state = PublicationStepState.CANCELLED
        step.retry_at = None
        step.last_error_code = "publication_approval_unavailable"
        step.last_error_detail = "Fresh publication approval is required."
        step.updated_at = now
        step.lock_version += 1
        attempt.state = PublicationAttemptState.CANCELLED
        attempt.lease_owner = None
        attempt.lease_expires_at = None
        attempt.retry_at = None
        attempt.completed_at = now
        attempt.last_error_code = "publication_approval_unavailable"
        attempt.last_error_detail = "Fresh publication approval is required."
        attempt.lock_version += 1
        intent.state = PublicationIntentState.AWAITING_APPROVAL
        intent.last_error_code = None
        intent.last_error_detail = None
        await session.commit()
        return PublicationCycleResult(
            claimed_attempt=True,
            attempt_id=attempt.id,
            state=PublicationAttemptState.CANCELLED,
            error_code="publication_approval_unavailable",
        )


async def _transition_failed(
    session: AsyncSession,
    *,
    attempt: PublicationAttempt,
    intent: PublicationIntent,
    step: PublicationStep,
    error_code: str,
    now: datetime,
) -> None:
    code, detail = safe_publication_error(
        code=error_code,
        detail=_SAFE_RUNTIME_ERROR,
    )
    step.state = PublicationStepState.FAILED
    step.retry_class = PublicationRetryClass.TERMINAL
    step.retry_at = None
    step.last_error_code = code
    step.last_error_detail = detail
    step.updated_at = now
    step.lock_version += 1
    attempt.state = PublicationAttemptState.FAILED
    attempt.lease_owner = None
    attempt.lease_expires_at = None
    attempt.retry_at = None
    attempt.completed_at = now
    attempt.last_error_code = code
    attempt.last_error_detail = detail
    attempt.lock_version += 1
    intent.state = PublicationIntentState.FAILED
    intent.last_error_code = code
    intent.last_error_detail = detail


async def _lock_execution_rows(
    session: AsyncSession,
    *,
    attempt_id: UUID,
    step_id: UUID,
) -> tuple[PublicationAttempt, PublicationIntent, PublicationStep]:
    attempt = await session.scalar(
        select(PublicationAttempt).where(PublicationAttempt.id == attempt_id).with_for_update()
    )
    if attempt is None:
        raise PublicationRuntimeContractError("publication attempt is unavailable")
    intent = await session.scalar(
        select(PublicationIntent).where(PublicationIntent.id == attempt.intent_id).with_for_update()
    )
    step = await session.scalar(
        select(PublicationStep)
        .where(
            PublicationStep.id == step_id,
            PublicationStep.attempt_id == attempt.id,
        )
        .with_for_update()
    )
    if intent is None or step is None:
        raise PublicationRuntimeContractError("publication execution row is unavailable")
    return attempt, intent, step


def _require_execution_lease(
    attempt: PublicationAttempt,
    worker_id: str,
) -> None:
    if (
        attempt.lease_owner != worker_id
        or attempt.lease_expires_at is None
        or attempt.state
        not in {
            PublicationAttemptState.CLAIMED,
            PublicationAttemptState.PROCESSING,
        }
    ):
        raise PublicationConflictError("publication attempt lease is unavailable")


async def _attempt_state(
    sessions: async_sessionmaker[AsyncSession],
    attempt_id: UUID,
) -> PublicationAttemptState | None:
    async with sessions() as session:
        return cast(
            PublicationAttemptState | None,
            await session.scalar(
                select(PublicationAttempt.state).where(PublicationAttempt.id == attempt_id)
            ),
        )


async def _intent_digest(
    sessions: async_sessionmaker[AsyncSession],
    intent_id: UUID,
) -> str:
    async with sessions() as session:
        digest = await session.scalar(
            select(PublicationIntent.intent_digest).where(PublicationIntent.id == intent_id)
        )
        if digest is None:
            raise PublicationRuntimeContractError("publication intent is unavailable")
        return digest


async def _append_effect_completion(
    session: AsyncSession,
    *,
    step: PublicationStep,
    request_no: int,
    event_type: str,
    remote_identifier: str | None,
    remote_expires_at: datetime | None,
    error_code: str | None,
    now: datetime,
) -> None:
    if event_type not in {"succeeded", "retryable", "unknown", "terminal"}:
        raise PublicationRuntimeContractError("publication effect completion type is invalid")
    started = await session.scalar(
        select(PublicationEffectEvent)
        .where(
            PublicationEffectEvent.step_id == step.id,
            PublicationEffectEvent.request_no == request_no,
            PublicationEffectEvent.event_type == "started",
            PublicationEffectEvent.is_completion.is_(False),
        )
        .with_for_update()
    )
    if started is None or started.step_kind != step.kind:
        raise PublicationRuntimeContractError("publication effect start event is unavailable")
    existing = await session.scalar(
        select(PublicationEffectEvent.id).where(
            PublicationEffectEvent.step_id == step.id,
            PublicationEffectEvent.request_no == request_no,
            PublicationEffectEvent.is_completion.is_(True),
        )
    )
    if existing is not None:
        raise PublicationRuntimeContractError("publication effect request is already complete")
    session.add(
        PublicationEffectEvent(
            id=uuid7(),
            step_id=step.id,
            request_no=request_no,
            step_kind=step.kind,
            event_type=event_type,
            is_completion=True,
            guard_epoch=started.guard_epoch,
            remote_identifier=remote_identifier,
            remote_expires_at=remote_expires_at,
            error_code=error_code,
            recorded_at=now,
        )
    )
    await session.flush()


def _runtime_audit(
    *,
    action: str,
    intent_id: UUID,
    attempt_id: UUID,
    detail: dict[str, object],
    now: datetime,
) -> AuditEvent:
    return AuditEvent(
        id=uuid7(),
        actor="system:publication-runtime",
        action=action,
        resource_type="publication_intent",
        resource_id=intent_id,
        correlation_id=f"publication-attempt:{attempt_id}",
        detail=detail,
        occurred_at=now,
    )


def _image_extension(content_type: str) -> str:
    if content_type == "image/jpeg":
        return ".jpg"
    if content_type == "image/png":
        return ".png"
    raise PublicationRuntimeContractError("Patreon handoff input is not JPEG or PNG")


def _bounded_worker(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 200
        or any(ord(character) < 33 or ord(character) == 127 for character in value)
    ):
        raise ValueError("worker ID must be 1 to 200 visible characters")
    return value


def _x_creator_user_id(value: str | None) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value.isascii()
        or not value.isdecimal()
        or not 1 <= len(value) <= 19
    ):
        raise ValueError("expected X creator user ID must contain 1 to 19 digits")
    return value


def _bounded_int(
    value: int,
    label: str,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{label} must be between {minimum} and {maximum}")
    return value


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _canonical_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _as_utc(value).isoformat().replace("+00:00", "Z")


def _effect_clock(
    *,
    clock: Callable[[], datetime] | None,
    initial: datetime,
    fixed: bool,
) -> Callable[[], datetime]:
    if clock is not None:
        return clock
    if not fixed:
        return lambda: datetime.now(UTC)
    logical_now = initial

    def tick() -> datetime:
        nonlocal logical_now
        logical_now += timedelta(microseconds=1)
        return logical_now

    return tick


def _clock_now(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("publication runtime clock must return a timezone-aware datetime")
    return value.astimezone(UTC)
