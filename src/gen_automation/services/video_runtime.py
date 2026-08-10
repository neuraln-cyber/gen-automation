"""Crash-safe Salad execution for short Animation Studio jobs.

The runtime deliberately uses an at-most-once provider submission protocol.
Salad Job Queues do not expose an idempotency key, so a transport failure after
POST is never followed by a blind second POST.  The deterministic submission
metadata is reconciled through ``list_jobs``; an outcome that remains ambiguous
is failed closed after a bounded observation window.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gen_automation.db.models import (
    Asset,
    AssetLineage,
    AuditEvent,
    ProviderBudgetGuard,
    SaladDeployment,
    VideoGenerationAttempt,
    VideoGenerationJob,
    VideoGenerationOutput,
)
from gen_automation.domain.canonical import canonical_sha256
from gen_automation.domain.enums import (
    AssetKind,
    AssetState,
    BudgetState,
    DesiredDeploymentState,
    SaladDeploymentPurpose,
    SaladDeploymentState,
)
from gen_automation.domain.ids import uuid7
from gen_automation.domain.video import (
    VideoGenerationAttemptState,
    VideoGenerationState,
)
from gen_automation.integrations.salad import (
    SALAD_QUEUE_JOB_PAGE_SIZE,
    SaladAPIError,
    SaladClient,
    SaladCloudError,
    SaladJobStatus,
    SaladProtocolError,
    SaladQueueJob,
)
from gen_automation.services.budgets import (
    BudgetError,
    BudgetSnapshot,
    reevaluate_budget_guard,
)
from gen_automation.storage.base import ObjectMetadata, ObjectStore, ObjectStoreError
from gen_automation.video_worker.models import (
    AnimateEnvelope,
    AnimatePayload,
    AnimateResponse,
    SourceDownloadGrant,
    VideoUploadGrant,
)
from gen_automation.video_worker.profiles import (
    PINNED_VIDEO_PROFILE,
    PINNED_VIDEO_PROFILE_SHA256,
)
from gen_automation.video_worker.security import calculate_signature

_ACTIVE_JOB_STATES = frozenset(
    {
        VideoGenerationState.CLAIMED,
        VideoGenerationState.SUBMITTING,
        VideoGenerationState.RUNNING,
        VideoGenerationState.COLLECTING,
        VideoGenerationState.VERIFYING,
        VideoGenerationState.UNKNOWN,
        VideoGenerationState.CANCEL_REQUESTED,
    }
)
_ACTIVE_ATTEMPT_STATES = frozenset(
    {
        VideoGenerationAttemptState.CREATED,
        VideoGenerationAttemptState.SUBMITTING,
        VideoGenerationAttemptState.SUBMITTED,
        VideoGenerationAttemptState.RUNNING,
        VideoGenerationAttemptState.UNKNOWN,
        VideoGenerationAttemptState.CANCEL_REQUESTED,
    }
)
_REMOTE_ATTEMPT_STATES = frozenset(
    {
        VideoGenerationAttemptState.SUBMITTED,
        VideoGenerationAttemptState.RUNNING,
        VideoGenerationAttemptState.UNKNOWN,
        VideoGenerationAttemptState.CANCEL_REQUESTED,
    }
)
_SOURCE_CONTENT_TYPES = frozenset({"image/png", "image/jpeg", "image/webp"})
_OUTPUT_CONTENT_TYPE = "video/mp4"
_METADATA_KIND = "animation-video/v1"
_LINEAGE_RECIPE = "wan2.2-ti2v-5b-comfy-v1"
_RECONCILIATION_BATCH_COUNT = 10
_RECONCILIATION_PAGES_PER_BATCH = 4
_RECONCILIATION_LEGACY_PAGE_SIZE = 100
_RECONCILIATION_NEXT_OFFSET_KEY = "reconciliation_next_offset"
_RECONCILIATION_PAGE_SIZE_KEY = "reconciliation_page_size"
_RECONCILIATION_LEGACY_NEXT_PAGE_KEY = "reconciliation_next_page"
_MAX_RECONCILIATION_BATCH_TIMEOUT_SECONDS = 30.0
_MAX_WORKER_ENVELOPE_BYTES = 128 * 1024


def _reconciliation_start_offset(metadata: Mapping[str, Any]) -> int:
    raw_offset = metadata.get(_RECONCILIATION_NEXT_OFFSET_KEY)
    raw_page_size = metadata.get(_RECONCILIATION_PAGE_SIZE_KEY)
    if (
        isinstance(raw_offset, int)
        and not isinstance(raw_offset, bool)
        and raw_offset >= 0
        and isinstance(raw_page_size, int)
        and not isinstance(raw_page_size, bool)
        and 1 <= raw_page_size <= SALAD_QUEUE_JOB_PAGE_SIZE
        and raw_offset % raw_page_size == 0
    ):
        # Overlap rather than skip when a future compatible page size does not
        # divide the current provider page size exactly.
        return (raw_offset // SALAD_QUEUE_JOB_PAGE_SIZE) * SALAD_QUEUE_JOB_PAGE_SIZE

    raw_legacy_page = metadata.get(_RECONCILIATION_LEGACY_NEXT_PAGE_KEY, 1)
    legacy_page = (
        raw_legacy_page
        if isinstance(raw_legacy_page, int)
        and not isinstance(raw_legacy_page, bool)
        and raw_legacy_page >= 1
        else 1
    )
    return (legacy_page - 1) * _RECONCILIATION_LEGACY_PAGE_SIZE


class VideoRuntimeError(RuntimeError):
    """A redacted runtime failure safe for controller supervision."""


@dataclass(frozen=True, slots=True)
class VideoRuntimeConfig:
    enabled: bool
    queue_name: str = ""
    worker_image_digest: str = ""
    signing_key_id: str = ""
    signing_private_key: str = field(default="", repr=False)
    signature_ttl_seconds: int = 7200
    grant_ttl_seconds: int = 14_400
    max_output_bytes: int = 256 * 1024 * 1024
    max_queued_jobs: int = 3
    max_hourly_cost_microusd: int = 350_000
    attempt_watchdog_seconds: int = 6300
    cancel_grace_seconds: int = 300
    retry_delay_seconds: int = 60
    reconciliation_interval_seconds: int = 30
    unresolved_submission_seconds: int = 900
    reconciliation_batch_timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if self.enabled and (not self.queue_name or len(self.queue_name) > 200):
            raise ValueError("video queue name is invalid")
        if self.enabled and "@sha256:" not in self.worker_image_digest:
            raise ValueError("video worker image must be digest pinned")
        if self.enabled and (not self.signing_key_id or not self.signing_private_key):
            raise ValueError("video worker signing configuration is incomplete")
        if not 5 <= self.signature_ttl_seconds <= 7200:
            raise ValueError("video signature TTL is invalid")
        if not self.signature_ttl_seconds <= self.grant_ttl_seconds <= 14_400:
            raise ValueError("video grant TTL is invalid")
        if self.max_output_bytes <= 0 or not 1 <= self.max_queued_jobs <= 3:
            raise ValueError("video runtime limits are invalid")
        if self.max_hourly_cost_microusd <= 0 or self.attempt_watchdog_seconds <= 0:
            raise ValueError("video cost reservation is invalid")
        if (
            not 1 <= self.cancel_grace_seconds <= self.attempt_watchdog_seconds
            or self.retry_delay_seconds <= 0
            or self.reconciliation_interval_seconds <= 0
            or self.unresolved_submission_seconds <= 0
            or not (
                0
                < self.reconciliation_batch_timeout_seconds
                <= _MAX_RECONCILIATION_BATCH_TIMEOUT_SECONDS
            )
        ):
            raise ValueError("video retry timing is invalid")
        if self.enabled and self.signature_ttl_seconds < (
            self.attempt_watchdog_seconds + self.reconciliation_interval_seconds
        ):
            raise ValueError("video signature TTL cannot cover one queued render")
        if self.enabled and self.grant_ttl_seconds < (
            (self.attempt_watchdog_seconds * 2) + self.reconciliation_interval_seconds
        ):
            raise ValueError("video grant TTL cannot cover queued execution and upload")

    @property
    def conservative_reservation_microusd(self) -> int:
        return math.ceil(self.max_hourly_cost_microusd * self.attempt_watchdog_seconds / 3600)


@dataclass(frozen=True, slots=True)
class _PreparedSubmission:
    attempt_id: UUID
    queue_name: str
    input: dict[str, Any]
    metadata: dict[str, str]


@dataclass(frozen=True, slots=True)
class _ObservationClaim:
    attempt_id: UUID
    deployment_id: UUID
    queue_name: str
    container_group_name: str
    external_id: str
    cancel_requested: bool
    cancel_requested_at: datetime | None
    submitted_at: datetime
    running_started_at: datetime | None
    lease_owner: str
    quarantine_provider_ids: tuple[str, ...]
    quarantine_error_code: str | None


class VideoRuntime:
    """Durable bounded queue feeding a single-replica Salad video worker."""

    def __init__(
        self,
        *,
        config: VideoRuntimeConfig,
        sessions: async_sessionmaker[AsyncSession],
        salad: SaladClient,
        store: ObjectStore,
        worker_id: str,
    ) -> None:
        if not worker_id or len(worker_id) > 200:
            raise ValueError("video runtime worker ID is invalid")
        self.config = config
        self.sessions = sessions
        self.salad = salad
        self.store = store
        self.worker_id = worker_id

    async def run_once(self) -> bool:
        """Advance one durable phase, prioritizing already-started work."""

        if not self.config.enabled:
            return False
        if await self.observe_once():
            return True
        if await self.submit_once():
            return True
        return await self.claim_once()

    async def claim_once(self, *, now: datetime | None = None) -> bool:
        if not self.config.enabled:
            return False
        claimed_at = _utc(now)
        async with self.sessions() as session:
            async with session.begin():
                # Global lock order is budget guard -> deployment -> work row.
                # Deployment reconciliation and the provider kill switch use
                # that same order, so do not move this below the deployment
                # lock even though it means a cheap budget reevaluation on an
                # otherwise idle cycle.
                budget = await self._lock_budget(session, now=claimed_at)
                if budget is None:
                    return False
                budget_snapshot, budget_guard = budget
                deployment = await session.scalar(
                    select(SaladDeployment)
                    .where(
                        SaladDeployment.is_current.is_(True),
                        SaladDeployment.purpose == SaladDeploymentPurpose.VIDEO,
                        SaladDeployment.state == SaladDeploymentState.ACTIVE,
                        SaladDeployment.desired_state == DesiredDeploymentState.ACTIVE,
                        SaladDeployment.provider_queue_id.is_not(None),
                        SaladDeployment.provider_container_group_id.is_not(None),
                        SaladDeployment.min_replicas == 0,
                        SaladDeployment.max_replicas == 1,
                    )
                    .with_for_update()
                )
                if deployment is None:
                    return False
                if deployment.worker_image_digest != self.config.worker_image_digest:
                    return False

                active_count = int(
                    await session.scalar(
                        select(func.count(VideoGenerationJob.id)).where(
                            VideoGenerationJob.state.in_(_ACTIVE_JOB_STATES)
                        )
                    )
                    or 0
                )
                if active_count >= self.config.max_queued_jobs:
                    return False

                job = await session.scalar(
                    select(VideoGenerationJob)
                    .where(
                        VideoGenerationJob.provider == "salad",
                        (
                            (VideoGenerationJob.state == VideoGenerationState.QUEUED)
                            | (
                                (VideoGenerationJob.state == VideoGenerationState.RETRY_WAIT)
                                & (
                                    (VideoGenerationJob.retry_at.is_(None))
                                    | (VideoGenerationJob.retry_at <= claimed_at)
                                )
                            )
                        ),
                    )
                    .order_by(
                        VideoGenerationJob.priority,
                        VideoGenerationJob.created_at,
                        VideoGenerationJob.id,
                    )
                    .with_for_update(skip_locked=True)
                )
                if job is None:
                    return False
                if job.attempt_count >= job.max_attempts:
                    _terminal_job(
                        job,
                        state=VideoGenerationState.FAILED,
                        now=claimed_at,
                        error_code="video_attempts_exhausted",
                    )
                    return True
                if not _profile_matches(job):
                    _terminal_job(
                        job,
                        state=VideoGenerationState.FAILED,
                        now=claimed_at,
                        error_code="video_profile_identity_mismatch",
                    )
                    return True

                source = await session.scalar(
                    select(Asset).where(Asset.id == job.source_asset_id).with_for_update()
                )
                if source is None or not _source_matches(job, source, self.store):
                    _terminal_job(
                        job,
                        state=VideoGenerationState.FAILED,
                        now=claimed_at,
                        error_code="video_source_identity_mismatch",
                    )
                    return True
                if job.source_object_version_id is None:
                    _terminal_job(
                        job,
                        state=VideoGenerationState.FAILED,
                        now=claimed_at,
                        error_code="video_source_version_missing",
                    )
                    return True

                if not self._budget_accepts(
                    job=job,
                    snapshot=budget_snapshot,
                    guard=budget_guard,
                ):
                    return False

                attempt_id = uuid7()
                output_asset_id = uuid7()
                upload_attempt_id = uuid7()
                attempt_no = job.attempt_count + 1
                submission_key = canonical_sha256(
                    {
                        "schema": _METADATA_KIND,
                        "job_id": str(job.id),
                        "attempt_id": str(attempt_id),
                        "attempt_no": attempt_no,
                        "request_sha256": job.request_sha256,
                    }
                )
                object_key = (
                    f"video-staging/{source.release_id}/{job.id}/{attempt_id}/"
                    f"{output_asset_id}/{upload_attempt_id}.mp4"
                )
                reservation = max(
                    job.estimated_cost_microusd,
                    self.config.conservative_reservation_microusd,
                )
                output_asset = Asset(
                    id=output_asset_id,
                    release_id=source.release_id,
                    generation_job_id=None,
                    output_index=None,
                    kind=AssetKind.DERIVATIVE,
                    state=AssetState.UPLOADING,
                    storage_backend=self.store.backend,
                    storage_bucket=self.store.bucket,
                    staging_object_key=object_key,
                    asset_metadata={
                        "schema": _METADATA_KIND,
                        "video_job_id": str(job.id),
                        "video_attempt_id": str(attempt_id),
                        "upload_attempt_id": str(upload_attempt_id),
                        "declared_content_type": _OUTPUT_CONTENT_TYPE,
                    },
                )
                attempt = VideoGenerationAttempt(
                    id=attempt_id,
                    video_generation_job_id=job.id,
                    salad_deployment_id=deployment.id,
                    attempt_no=attempt_no,
                    provider="salad",
                    submission_key=submission_key,
                    request_sha256=job.request_sha256,
                    state=VideoGenerationAttemptState.CREATED,
                    worker_image_digest=deployment.worker_image_digest,
                    request_metadata={
                        "schema": _METADATA_KIND,
                        "output_asset_id": str(output_asset_id),
                        "upload_attempt_id": str(upload_attempt_id),
                        "output_object_key": object_key,
                        "profile_id": job.profile_key,
                    },
                    reserved_cost_microusd=reservation,
                    created_at=claimed_at,
                    updated_at=claimed_at,
                )
                session.add_all((output_asset, attempt))
                job.state = VideoGenerationState.CLAIMED
                job.attempt_count = attempt_no
                job.reserved_cost_microusd = reservation
                job.retry_at = None
                job.lease_owner = None
                job.lease_expires_at = None
                job.last_error_code = None
                job.last_error_detail = None
                job.lock_version += 1
                job.updated_at = claimed_at
                await session.flush()
                return True

    async def submit_once(self, *, now: datetime | None = None) -> bool:
        submitted_at = _utc(now)
        attempt_id = await self._next_unsubmitted_attempt_id(now=submitted_at)
        if attempt_id is None:
            return False

        async with self.sessions() as session:
            attempt_snapshot = await session.get(VideoGenerationAttempt, attempt_id)
            job_snapshot = (
                await session.get(
                    VideoGenerationJob,
                    attempt_snapshot.video_generation_job_id,
                )
                if attempt_snapshot is not None
                else None
            )
            pristine = (
                attempt_snapshot is not None
                and attempt_snapshot.state == VideoGenerationAttemptState.CREATED
                and attempt_snapshot.submit_started_at is None
                and attempt_snapshot.provider_external_id is None
            )
            slot_available = True
            if pristine and self.config.enabled and attempt_snapshot is not None:
                slot_available = await self._provider_submission_slot_available(
                    session,
                    deployment_id=attempt_snapshot.salad_deployment_id,
                )
        if not self.config.enabled and (
            job_snapshot is None
            or job_snapshot.state != VideoGenerationState.CANCEL_REQUESTED
            or pristine
        ):
            return False
        if pristine and not slot_available:
            return False
        if pristine:
            # No POST can exist for a never-started attempt. Avoid scanning a
            # long retained queue history; reconciliation is only required
            # after SUBMITTING/UNKNOWN introduces a real ambiguity window.
            matches: list[SaladQueueJob] = []
            search_complete = True
        else:
            try:
                matches, search_complete, next_offset = await self._find_provider_jobs(attempt_id)
            except VideoRuntimeError:
                await self._fail_attempt(
                    attempt_id,
                    code="video_deployment_identity_mismatch",
                    retryable=False,
                    now=submitted_at,
                )
                return True
        if len(matches) > 1:
            await self._quarantine_provider_jobs(
                attempt_id,
                matches,
                code="video_duplicate_provider_jobs",
                now=submitted_at,
            )
            return True
        if len(matches) == 1:
            await self._bind_provider_job(attempt_id, matches[0], now=submitted_at)
            return True
        if not search_complete:
            if next_offset is not None:
                await self._record_reconciliation_cursor(
                    attempt_id,
                    next_offset=next_offset,
                    now=submitted_at,
                )
                return True
            return False

        async with self.sessions() as session:
            row = (
                await session.execute(
                    select(VideoGenerationAttempt, VideoGenerationJob)
                    .join(
                        VideoGenerationJob,
                        VideoGenerationJob.id == VideoGenerationAttempt.video_generation_job_id,
                    )
                    .where(VideoGenerationAttempt.id == attempt_id)
                    .with_for_update()
                )
            ).one_or_none()
            if row is None:
                return False
            attempt, job = row
            if attempt.provider_external_id is not None:
                return False
            if attempt.state != VideoGenerationAttemptState.CREATED:
                attempt.last_observed_at = submitted_at
                attempt.updated_at = submitted_at
                if (
                    attempt.submit_started_at is not None
                    and (submitted_at - _as_utc(attempt.submit_started_at)).total_seconds()
                    >= self.config.unresolved_submission_seconds
                ):
                    cancellation_pending = job.state == VideoGenerationState.CANCEL_REQUESTED
                    await session.rollback()
                    await self._fail_attempt(
                        attempt_id,
                        code=(
                            "video_provider_cancelled"
                            if cancellation_pending
                            else "video_submission_outcome_unresolved"
                        ),
                        retryable=False,
                        cancelled=cancellation_pending,
                        now=submitted_at,
                    )
                    return True
                await session.commit()
                return False

        try:
            prepared = await self._prepare_submission(attempt_id, now=submitted_at)
        except (ObjectStoreError, ValidationError, ValueError):
            await self._fail_attempt(
                attempt_id,
                code="video_submission_preparation_failed",
                retryable=True,
                now=submitted_at,
            )
            return True

        async with self.sessions() as session:
            async with session.begin():
                if attempt_snapshot is None:
                    return False
                deployment = await session.scalar(
                    select(SaladDeployment)
                    .where(SaladDeployment.id == attempt_snapshot.salad_deployment_id)
                    .with_for_update()
                )
                if deployment is None or not await self._provider_submission_slot_available(
                    session,
                    deployment_id=deployment.id,
                ):
                    return False
                row = (
                    await session.execute(
                        select(VideoGenerationAttempt, VideoGenerationJob)
                        .join(
                            VideoGenerationJob,
                            VideoGenerationJob.id == VideoGenerationAttempt.video_generation_job_id,
                        )
                        .where(VideoGenerationAttempt.id == attempt_id)
                        .with_for_update()
                    )
                ).one_or_none()
                if row is None:
                    return False
                attempt, job = row
                if (
                    attempt.state != VideoGenerationAttemptState.CREATED
                    or attempt.provider_external_id is not None
                ):
                    return False
                attempt.state = VideoGenerationAttemptState.SUBMITTING
                attempt.submit_started_at = submitted_at
                attempt.updated_at = submitted_at
                job.state = VideoGenerationState.SUBMITTING
                job.updated_at = submitted_at
                job.lock_version += 1

        try:
            remote = await self.salad.create_job(
                prepared.queue_name,
                input=cast(Any, prepared.input),
                metadata=prepared.metadata,
            )
        except SaladAPIError as error:
            if 400 <= error.status_code < 500 and error.status_code not in {408, 429}:
                await self._fail_attempt(
                    attempt_id,
                    code="video_provider_rejected_submission",
                    retryable=True,
                    now=submitted_at,
                )
            else:
                await self._mark_unknown(
                    attempt_id,
                    code="video_submission_outcome_unknown",
                    now=submitted_at,
                )
            return True
        except Exception:
            await self._mark_unknown(
                attempt_id,
                code="video_submission_outcome_unknown",
                now=submitted_at,
            )
            return True

        await self._bind_provider_job(attempt_id, remote, now=submitted_at)
        return True

    async def disable_once(self, *, now: datetime | None = None) -> bool:
        """Drain/cancel video work without admitting or POSTing new work."""

        disabled_at = _utc(now)
        async with self.sessions() as session:
            job_id = await session.scalar(
                select(VideoGenerationJob.id)
                .where(
                    VideoGenerationJob.state.in_(
                        {
                            VideoGenerationState.QUEUED,
                            VideoGenerationState.RETRY_WAIT,
                            *_ACTIVE_JOB_STATES,
                        }
                    ),
                    VideoGenerationJob.state != VideoGenerationState.CANCEL_REQUESTED,
                )
                .order_by(VideoGenerationJob.created_at, VideoGenerationJob.id)
                .limit(1)
            )
            if job_id is not None:
                changed = await request_video_cancellation(
                    session,
                    job_id=job_id,
                    now=disabled_at,
                )
                await session.commit()
                return changed
        if await self.observe_once(now=disabled_at):
            return True
        # With every live parent already CANCEL_REQUESTED, submit_once may only
        # reconcile an ambiguous in-flight POST. Its disabled-mode guard above
        # forbids pristine CREATED attempts and therefore forbids a new POST.
        return await self.submit_once(now=disabled_at)

    async def observe_once(self, *, now: datetime | None = None) -> bool:
        observed_at = _utc(now)
        context = await self._next_remote_attempt(now=observed_at)
        if context is None:
            return False
        if (
            context.cancel_requested
            and context.cancel_requested_at is not None
            and (observed_at - context.cancel_requested_at).total_seconds()
            >= self.config.cancel_grace_seconds
        ):
            await self._engage_cancellation_deployment_stop(
                context,
                now=observed_at,
            )
        if context.quarantine_provider_ids:
            return await self._observe_quarantined_provider_jobs(
                context,
                now=observed_at,
            )
        if context.cancel_requested:
            try:
                await self.salad.cancel_job(context.queue_name, context.external_id)
            except SaladCloudError:
                # Cancellation may have reached Salad; continue with the
                # authoritative GET instead of releasing or duplicating work.
                pass
        try:
            remote = await self.salad.get_job(context.queue_name, context.external_id)
        except SaladAPIError as error:
            if error.status_code == 404:
                if (
                    observed_at - context.submitted_at
                ).total_seconds() >= self.config.attempt_watchdog_seconds:
                    await self._fail_attempt(
                        context.attempt_id,
                        code="video_provider_job_not_found_watchdog",
                        retryable=False,
                        cancelled=context.cancel_requested,
                        now=observed_at,
                        expected_lease_owner=context.lease_owner,
                    )
                else:
                    await self._mark_unknown(
                        context.attempt_id,
                        code="video_provider_job_not_found",
                        now=observed_at,
                        expected_lease_owner=context.lease_owner,
                    )
                return True
            raise

        if remote.status in {SaladJobStatus.PENDING, SaladJobStatus.RUNNING}:
            if remote.status == SaladJobStatus.PENDING:
                watchdog_started_at = context.submitted_at
                watchdog_seconds = (
                    self.config.signature_ttl_seconds - self.config.reconciliation_interval_seconds
                )
                watchdog_code = "video_queue_wait_watchdog_expired"
            else:
                watchdog_started_at = context.running_started_at or observed_at
                watchdog_seconds = self.config.attempt_watchdog_seconds
                watchdog_code = "video_attempt_watchdog_expired"
            if (observed_at - watchdog_started_at).total_seconds() >= watchdog_seconds:
                try:
                    await self.salad.cancel_job(context.queue_name, context.external_id)
                except SaladCloudError:
                    await self._mark_unknown(
                        context.attempt_id,
                        code="video_attempt_watchdog_cancel_unknown",
                        now=observed_at,
                        expected_lease_owner=context.lease_owner,
                    )
                    return True
                await self._mark_cancel_requested(
                    context.attempt_id,
                    code=watchdog_code,
                    now=observed_at,
                    expected_lease_owner=context.lease_owner,
                )
                return True
            await self._record_remote_progress(
                context.attempt_id,
                remote,
                now=observed_at,
                expected_lease_owner=context.lease_owner,
            )
            return True
        if remote.status == SaladJobStatus.SUCCEEDED:
            await self._collect_success(
                context.attempt_id,
                remote,
                now=observed_at,
                expected_lease_owner=context.lease_owner,
            )
            return True
        if remote.status == SaladJobStatus.CANCELLED:
            await self._fail_attempt(
                context.attempt_id,
                code="video_provider_cancelled",
                retryable=not context.cancel_requested,
                cancelled=context.cancel_requested,
                now=observed_at,
                expected_lease_owner=context.lease_owner,
            )
            return True
        await self._fail_attempt(
            context.attempt_id,
            code=(
                "video_provider_cancelled" if context.cancel_requested else "video_provider_failed"
            ),
            retryable=not context.cancel_requested,
            cancelled=context.cancel_requested,
            now=observed_at,
            expected_lease_owner=context.lease_owner,
        )
        return True

    async def _observe_quarantined_provider_jobs(
        self,
        context: _ObservationClaim,
        *,
        now: datetime,
    ) -> bool:
        all_terminal = True
        for external_id in context.quarantine_provider_ids:
            try:
                await self.salad.cancel_job(context.queue_name, external_id)
            except SaladCloudError:
                pass
            try:
                remote = await self.salad.get_job(context.queue_name, external_id)
            except SaladAPIError as error:
                if (
                    error.status_code != 404
                    or (now - context.submitted_at).total_seconds()
                    < self.config.attempt_watchdog_seconds
                ):
                    all_terminal = False
            except SaladCloudError:
                all_terminal = False
            else:
                if remote.status not in {
                    SaladJobStatus.SUCCEEDED,
                    SaladJobStatus.FAILED,
                    SaladJobStatus.CANCELLED,
                }:
                    all_terminal = False
        error_code = context.quarantine_error_code or "video_provider_identity_mismatch"
        if all_terminal:
            await self._fail_attempt(
                context.attempt_id,
                code=error_code,
                retryable=False,
                now=now,
                expected_lease_owner=context.lease_owner,
            )
        else:
            await self._mark_cancel_requested(
                context.attempt_id,
                code=error_code,
                now=now,
                expected_lease_owner=context.lease_owner,
            )
        return True

    async def _engage_cancellation_deployment_stop(
        self,
        context: _ObservationClaim,
        *,
        now: datetime,
    ) -> None:
        """Persist a VIDEO-only stop before issuing the provider side effect."""

        provider_stop_required = False
        async with self.sessions() as session:
            async with session.begin():
                deployment = await session.scalar(
                    select(SaladDeployment)
                    .where(SaladDeployment.id == context.deployment_id)
                    .with_for_update()
                )
                if (
                    deployment is None
                    or deployment.purpose != SaladDeploymentPurpose.VIDEO
                    or deployment.container_group_name != context.container_group_name
                    or deployment.queue_name != context.queue_name
                ):
                    return
                changed = deployment.desired_state != DesiredDeploymentState.STOPPED
                provider_stop_required = deployment.state not in {
                    SaladDeploymentState.DRAINING,
                    SaladDeploymentState.STOPPED,
                }
                deployment.desired_state = DesiredDeploymentState.STOPPED
                deployment.reconcile_after = now
                deployment.last_error_code = "video_cancellation_grace_expired"
                deployment.last_error_detail = (
                    "A video provider job ignored cancellation; the video lane is stopping."
                )
                deployment.updated_at = now
                deployment.lock_version += 1

                rows = (
                    await session.execute(
                        select(VideoGenerationAttempt, VideoGenerationJob)
                        .join(
                            VideoGenerationJob,
                            VideoGenerationJob.id == VideoGenerationAttempt.video_generation_job_id,
                        )
                        .where(
                            VideoGenerationAttempt.salad_deployment_id == deployment.id,
                            VideoGenerationAttempt.state.in_(_ACTIVE_ATTEMPT_STATES),
                        )
                        .with_for_update()
                    )
                ).all()
                for attempt, job in rows:
                    _set_cancel_requested_at(attempt, now)
                    attempt.last_observed_at = None
                    if attempt.provider_external_id is not None:
                        attempt.state = VideoGenerationAttemptState.CANCEL_REQUESTED
                        attempt.error_code = "video_deployment_stop_pending"
                        attempt.error_detail = (
                            "The video lane is stopping pending provider confirmation."
                        )
                        job.state = VideoGenerationState.CANCEL_REQUESTED
                        job.last_error_code = "video_deployment_stop_pending"
                        job.last_error_detail = (
                            "The video lane is stopping pending provider confirmation."
                        )
                    elif (
                        attempt.state == VideoGenerationAttemptState.CREATED
                        and attempt.submit_started_at is None
                    ):
                        # This sibling is definitely unsubmitted. Cancel it
                        # locally without manufacturing a provider identity or
                        # retaining a reservation that can no longer be spent.
                        attempt.state = VideoGenerationAttemptState.FAILED
                        attempt.completed_at = now
                        attempt.error_code = "video_deployment_stopped_before_submission"
                        attempt.error_detail = "The video lane stopped before submission."
                        attempt.actual_cost_microusd = 0
                        attempt.billed_duration_ms = 0
                        try:
                            output_asset_id, _upload_id, _key = _attempt_output_identity(attempt)
                        except VideoRuntimeError:
                            output_asset_id = None
                        if output_asset_id is not None:
                            asset = await session.scalar(
                                select(Asset).where(Asset.id == output_asset_id).with_for_update()
                            )
                            if asset is not None and asset.state != AssetState.AVAILABLE:
                                asset.state = AssetState.QUARANTINED
                                asset.verification_error_code = (
                                    "video_deployment_stopped_before_submission"
                                )
                                asset.verification_error_detail = (
                                    "The video lane stopped before submission."
                                )
                                asset.purge_after = now + timedelta(days=1)
                        job.state = VideoGenerationState.CANCELLED
                        job.completed_at = now
                        job.retry_at = None
                        job.reserved_cost_microusd = 0
                        job.lease_owner = None
                        job.lease_expires_at = None
                        job.last_error_code = "video_deployment_stopped_before_submission"
                        job.last_error_detail = "The video lane stopped before submission."
                    else:
                        # A POST may have succeeded without returning an ID.
                        # Preserve the ambiguous attempt state and reservation;
                        # deterministic metadata reconciliation remains the
                        # only safe way to prove there is no provider job.
                        attempt.error_code = "video_deployment_stop_reconciliation_pending"
                        attempt.error_detail = (
                            "The video lane is stopping while submission is reconciled."
                        )
                        job.state = VideoGenerationState.CANCEL_REQUESTED
                        job.last_error_code = "video_deployment_stop_reconciliation_pending"
                        job.last_error_detail = (
                            "The video lane is stopping while submission is reconciled."
                        )
                    attempt.updated_at = now
                    job.updated_at = now
                    job.lock_version += 1
                never_admitted = (
                    (
                        await session.scalars(
                            select(VideoGenerationJob)
                            .where(
                                VideoGenerationJob.state.in_(
                                    {
                                        VideoGenerationState.QUEUED,
                                        VideoGenerationState.RETRY_WAIT,
                                    }
                                )
                            )
                            .with_for_update()
                        )
                    ).all()
                    if deployment.is_current
                    else []
                )
                for job in never_admitted:
                    job.state = VideoGenerationState.CANCELLED
                    job.completed_at = now
                    job.retry_at = None
                    job.reserved_cost_microusd = 0
                    job.last_error_code = "video_deployment_stopped_before_admission"
                    job.last_error_detail = "The video lane stopped before admission."
                    job.updated_at = now
                    job.lock_version += 1
                if changed:
                    session.add(
                        AuditEvent(
                            actor="system:video-runtime",
                            action="salad_deployment.video_cancel_kill_switch_engaged",
                            resource_type="salad_deployment",
                            resource_id=deployment.id,
                            correlation_id=f"video-cancel-stop:{deployment.id}",
                            detail={
                                "error_code": "video_cancellation_grace_expired",
                                "video_attempt_id": str(context.attempt_id),
                            },
                            occurred_at=now,
                        )
                    )

        if not provider_stop_required:
            return
        try:
            await self.salad.stop_container_group(context.container_group_name)
        except SaladCloudError:
            # The durable STOPPED intent is reconciled by the deployment loop.
            return

        async with self.sessions() as session:
            async with session.begin():
                deployment = await session.scalar(
                    select(SaladDeployment)
                    .where(SaladDeployment.id == context.deployment_id)
                    .with_for_update()
                )
                if (
                    deployment is None
                    or deployment.purpose != SaladDeploymentPurpose.VIDEO
                    or deployment.desired_state != DesiredDeploymentState.STOPPED
                    or deployment.state == SaladDeploymentState.STOPPED
                ):
                    return
                deployment.state = SaladDeploymentState.DRAINING
                deployment.reconcile_after = now
                deployment.last_error_code = "video_deployment_stop_pending"
                deployment.last_error_detail = (
                    "Provider accepted the video lane stop; terminal confirmation is pending."
                )
                deployment.updated_at = now
                deployment.lock_version += 1

    async def _lock_budget(
        self,
        session: AsyncSession,
        *,
        now: datetime,
    ) -> tuple[BudgetSnapshot, ProviderBudgetGuard] | None:
        try:
            snapshot = await reevaluate_budget_guard(session, provider="salad", now=now)
        except BudgetError:
            return None
        guard = await session.scalar(
            select(ProviderBudgetGuard)
            .where(ProviderBudgetGuard.provider == "salad")
            .with_for_update()
        )
        if guard is None or guard.state != BudgetState.OPEN or snapshot.state != BudgetState.OPEN:
            return None
        return snapshot, guard

    def _budget_accepts(
        self,
        *,
        job: VideoGenerationJob,
        snapshot: BudgetSnapshot,
        guard: ProviderBudgetGuard,
    ) -> bool:
        reservation = max(
            job.estimated_cost_microusd,
            self.config.conservative_reservation_microusd,
        )
        # BudgetSnapshot already includes reservations for every active image
        # and video attempt.  Add only this candidate; re-summing active video
        # rows here would double-count the first queued variants.
        return (
            snapshot.daily_committed_microusd + reservation <= guard.daily_limit_microusd
            and snapshot.monthly_committed_microusd + reservation <= guard.monthly_limit_microusd
        )

    async def _provider_submission_slot_available(
        self,
        session: AsyncSession,
        *,
        deployment_id: UUID,
    ) -> bool:
        deployment = await session.get(SaladDeployment, deployment_id)
        if (
            deployment is None
            or deployment.purpose != SaladDeploymentPurpose.VIDEO
            or not deployment.is_current
            or deployment.state != SaladDeploymentState.ACTIVE
            or deployment.desired_state != DesiredDeploymentState.ACTIVE
            or deployment.worker_image_digest != self.config.worker_image_digest
        ):
            return False
        rows = (
            await session.execute(
                select(
                    VideoGenerationAttempt.state,
                    VideoGenerationAttempt.provider_state,
                    VideoGenerationAttempt.provider_external_id,
                ).where(
                    VideoGenerationAttempt.salad_deployment_id == deployment_id,
                    VideoGenerationAttempt.state.in_(
                        {
                            VideoGenerationAttemptState.SUBMITTING,
                            VideoGenerationAttemptState.SUBMITTED,
                            VideoGenerationAttemptState.RUNNING,
                            VideoGenerationAttemptState.UNKNOWN,
                            VideoGenerationAttemptState.CANCEL_REQUESTED,
                        }
                    ),
                )
            )
        ).all()
        if any(external_id is None for _state, _provider_state, external_id in rows):
            # An ambiguous POST may already occupy the only provider slot.
            return False
        running = sum(
            1
            for state, provider_state, _external_id in rows
            if state == VideoGenerationAttemptState.RUNNING
            or provider_state == SaladJobStatus.RUNNING.value
        )
        pending = len(rows) - running
        # With one replica, admit either the first job or exactly one pending
        # job behind one confirmed running job. Remaining variants stay
        # CLAIMED locally so their signed grants are minted just in time.
        return (not rows) or (running == 1 and pending == 0)

    async def _next_unsubmitted_attempt_id(self, *, now: datetime) -> UUID | None:
        observation_due = now - timedelta(seconds=self.config.reconciliation_interval_seconds)
        async with self.sessions() as session:
            return await session.scalar(
                select(VideoGenerationAttempt.id)
                .join(
                    VideoGenerationJob,
                    VideoGenerationJob.id == VideoGenerationAttempt.video_generation_job_id,
                )
                .where(
                    VideoGenerationAttempt.provider_external_id.is_(None),
                    or_(
                        VideoGenerationAttempt.state == VideoGenerationAttemptState.CREATED,
                        and_(
                            VideoGenerationAttempt.state.in_(
                                {
                                    VideoGenerationAttemptState.SUBMITTING,
                                    VideoGenerationAttemptState.UNKNOWN,
                                }
                            ),
                            or_(
                                VideoGenerationAttempt.last_observed_at.is_(None),
                                VideoGenerationAttempt.last_observed_at <= observation_due,
                            ),
                        ),
                    ),
                    VideoGenerationJob.state.in_(
                        {
                            VideoGenerationState.CLAIMED,
                            VideoGenerationState.SUBMITTING,
                            VideoGenerationState.UNKNOWN,
                            VideoGenerationState.CANCEL_REQUESTED,
                        }
                    ),
                )
                .order_by(VideoGenerationAttempt.created_at, VideoGenerationAttempt.id)
                .limit(1)
            )

    async def _find_provider_jobs(
        self,
        attempt_id: UUID,
    ) -> tuple[list[SaladQueueJob], bool, int | None]:
        async with self.sessions() as session:
            attempt = await session.get(VideoGenerationAttempt, attempt_id)
            if attempt is None:
                return [], True, None
            deployment = await session.get(SaladDeployment, attempt.salad_deployment_id)
            if (
                deployment is None
                or deployment.purpose != SaladDeploymentPurpose.VIDEO
                or deployment.worker_image_digest != attempt.worker_image_digest
            ):
                raise VideoRuntimeError("video deployment identity is invalid")
            expected = {
                "kind": _METADATA_KIND,
                "video_job_id": str(attempt.video_generation_job_id),
                "video_attempt_id": str(attempt.id),
                "submission_key": attempt.submission_key,
            }
            start_offset = _reconciliation_start_offset(attempt.request_metadata)
            next_page = (start_offset // SALAD_QUEUE_JOB_PAGE_SIZE) + 1
        matches: dict[UUID, SaladQueueJob] = {}
        for batch_index in range(_RECONCILIATION_BATCH_COUNT):
            batch_start_page = next_page + (batch_index * _RECONCILIATION_PAGES_PER_BATCH)
            pages = tuple(
                range(batch_start_page, batch_start_page + _RECONCILIATION_PAGES_PER_BATCH)
            )
            async with asyncio.timeout(self.config.reconciliation_batch_timeout_seconds):
                raw_results = await asyncio.gather(
                    *(
                        self.salad.list_jobs(
                            deployment.queue_name,
                            page=page,
                            page_size=SALAD_QUEUE_JOB_PAGE_SIZE,
                        )
                        for page in pages
                    ),
                    return_exceptions=True,
                )
            provider_error = next(
                (result for result in raw_results if isinstance(result, SaladCloudError)),
                None,
            )
            if provider_error is not None:
                raise provider_error
            unexpected = next(
                (result for result in raw_results if isinstance(result, BaseException)),
                None,
            )
            if unexpected is not None:
                raise RuntimeError("unexpected video provider history scan failure") from unexpected
            results = cast(list[Any], raw_results)
            if any(len(result.items) > SALAD_QUEUE_JOB_PAGE_SIZE for result in results):
                raise SaladProtocolError("Salad queue job page exceeded the requested size")

            first_short_index = next(
                (
                    index
                    for index, result in enumerate(results)
                    if len(result.items) < SALAD_QUEUE_JOB_PAGE_SIZE
                ),
                None,
            )
            if first_short_index is not None and any(
                result.items for result in results[first_short_index + 1 :]
            ):
                raise SaladProtocolError("Salad queue job pagination snapshot was inconsistent")

            process_count = len(results) if first_short_index is None else first_short_index + 1
            for result in results[:process_count]:
                for remote in result.items:
                    if all(remote.metadata.get(key) == value for key, value in expected.items()):
                        matches[remote.id] = remote
            if first_short_index is not None:
                return list(matches.values()), True, None

        pages_scanned = _RECONCILIATION_BATCH_COUNT * _RECONCILIATION_PAGES_PER_BATCH
        next_offset = ((next_page - 1) + pages_scanned) * SALAD_QUEUE_JOB_PAGE_SIZE
        return list(matches.values()), False, next_offset

    async def _record_reconciliation_cursor(
        self,
        attempt_id: UUID,
        *,
        next_offset: int,
        now: datetime,
    ) -> None:
        if next_offset < 0 or next_offset % SALAD_QUEUE_JOB_PAGE_SIZE != 0:
            raise VideoRuntimeError("video reconciliation cursor is invalid")
        legacy_next_page = (next_offset // _RECONCILIATION_LEGACY_PAGE_SIZE) + 1
        async with self.sessions() as session:
            async with session.begin():
                row = (
                    await session.execute(
                        select(VideoGenerationAttempt, VideoGenerationJob)
                        .join(
                            VideoGenerationJob,
                            VideoGenerationJob.id == VideoGenerationAttempt.video_generation_job_id,
                        )
                        .where(VideoGenerationAttempt.id == attempt_id)
                        .with_for_update()
                    )
                ).one_or_none()
                if row is None:
                    return
                attempt, job = row
                if attempt.provider_external_id is not None or attempt.state not in {
                    VideoGenerationAttemptState.SUBMITTING,
                    VideoGenerationAttemptState.UNKNOWN,
                }:
                    return
                attempt.request_metadata = {
                    **attempt.request_metadata,
                    _RECONCILIATION_NEXT_OFFSET_KEY: next_offset,
                    _RECONCILIATION_PAGE_SIZE_KEY: SALAD_QUEUE_JOB_PAGE_SIZE,
                    _RECONCILIATION_LEGACY_NEXT_PAGE_KEY: legacy_next_page,
                }
                attempt.last_observed_at = now
                attempt.updated_at = now
                job.last_error_code = "video_submission_history_reconciliation"
                job.last_error_detail = "Provider queue history reconciliation is continuing."
                job.updated_at = now
                job.lock_version += 1

    async def _prepare_submission(
        self,
        attempt_id: UUID,
        *,
        now: datetime,
    ) -> _PreparedSubmission:
        async with self.sessions() as session:
            row = (
                await session.execute(
                    select(
                        VideoGenerationAttempt,
                        VideoGenerationJob,
                        Asset,
                        SaladDeployment,
                    )
                    .join(
                        VideoGenerationJob,
                        VideoGenerationJob.id == VideoGenerationAttempt.video_generation_job_id,
                    )
                    .join(Asset, Asset.id == VideoGenerationJob.source_asset_id)
                    .join(
                        SaladDeployment,
                        SaladDeployment.id == VideoGenerationAttempt.salad_deployment_id,
                    )
                    .where(VideoGenerationAttempt.id == attempt_id)
                )
            ).one_or_none()
            if row is None:
                raise ValueError("video attempt is unavailable")
            attempt, job, source, deployment = row
            output_asset_id, upload_attempt_id, output_key = _attempt_output_identity(attempt)
            if (
                deployment.purpose != SaladDeploymentPurpose.VIDEO
                or not deployment.is_current
                or deployment.state != SaladDeploymentState.ACTIVE
                or deployment.desired_state != DesiredDeploymentState.ACTIVE
                or deployment.worker_image_digest != attempt.worker_image_digest
                or (
                    self.config.enabled
                    and deployment.worker_image_digest != self.config.worker_image_digest
                )
            ):
                raise ValueError("video deployment identity changed")
            if not _source_matches(job, source, self.store):
                raise ValueError("video source identity changed")
            if job.source_object_version_id is None:
                raise ValueError("video source version is unavailable")

        source_url = await self.store.presign_download(
            key=job.source_object_key,
            version_id=job.source_object_version_id,
            expires_in=self.config.grant_ttl_seconds,
        )
        upload = await self.store.presign_upload(
            key=output_key,
            content_type=_OUTPUT_CONTENT_TYPE,
            metadata={
                "schema": _METADATA_KIND,
                "video-job-id": str(job.id),
                "video-attempt-id": str(attempt.id),
                "output-asset-id": str(output_asset_id),
                "upload-attempt-id": str(upload_attempt_id),
            },
            expires_in=self.config.grant_ttl_seconds,
            max_bytes=self.config.max_output_bytes,
        )
        if upload.method != "POST" or upload.headers:
            raise ValueError("video upload grant contract is unsupported")
        issued_at = int(now.timestamp())
        payload = AnimatePayload(
            job_id=str(job.id),
            attempt_id=str(attempt.id),
            profile_id=cast(Any, job.profile_key),
            source=SourceDownloadGrant(
                asset_id=str(job.source_asset_id),
                url=source_url,
                content_type=cast(Any, job.source_content_type),
                size_bytes=job.source_byte_size,
                sha256=job.source_sha256,
            ),
            upload=VideoUploadGrant(
                asset_id=str(output_asset_id),
                upload_attempt_id=str(upload_attempt_id),
                content_type=cast(Any, _OUTPUT_CONTENT_TYPE),
                url=upload.url,
                fields=upload.fields,
            ),
            prompt=job.prompt,
            negative_prompt=job.negative_prompt,
            seed=job.seed,
            native_frame_count=cast(Any, job.frame_count),
            fps=cast(Any, job.fps),
            width=cast(Any, job.width),
            height=cast(Any, job.height),
            loop_mode=cast(Any, job.loop_mode),
        )
        unsigned = AnimateEnvelope(
            version="video-worker.v1",
            key_id=self.config.signing_key_id,
            issued_at=issued_at,
            expires_at=issued_at + self.config.signature_ttl_seconds,
            payload=payload,
            signature="A" * 86,
        )
        envelope = AnimateEnvelope(
            **unsigned.model_dump(mode="python", exclude={"signature"}),
            signature=calculate_signature(unsigned, self.config.signing_private_key),
        )
        serialized_size = len(
            json.dumps(
                envelope.model_dump(mode="json"),
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        )
        if serialized_size > _MAX_WORKER_ENVELOPE_BYTES:
            raise ValueError("video worker envelope exceeds its bounded input")
        return _PreparedSubmission(
            attempt_id=attempt.id,
            queue_name=deployment.queue_name,
            input=envelope.model_dump(mode="json"),
            metadata={
                "kind": _METADATA_KIND,
                "video_job_id": str(job.id),
                "video_attempt_id": str(attempt.id),
                "submission_key": attempt.submission_key,
            },
        )

    async def _quarantine_provider_jobs(
        self,
        attempt_id: UUID,
        remotes: list[SaladQueueJob],
        *,
        code: str,
        now: datetime,
    ) -> None:
        async with self.sessions() as session:
            async with session.begin():
                row = (
                    await session.execute(
                        select(VideoGenerationAttempt, VideoGenerationJob)
                        .join(
                            VideoGenerationJob,
                            VideoGenerationJob.id == VideoGenerationAttempt.video_generation_job_id,
                        )
                        .where(VideoGenerationAttempt.id == attempt_id)
                        .with_for_update()
                    )
                ).one_or_none()
                if row is None:
                    return
                _mark_provider_quarantine_locked(
                    row[0],
                    row[1],
                    provider_ids=tuple(str(remote.id) for remote in remotes),
                    code=code,
                    now=now,
                )

    async def _bind_provider_job(
        self,
        attempt_id: UUID,
        remote: SaladQueueJob,
        *,
        now: datetime,
    ) -> None:
        async with self.sessions() as session:
            async with session.begin():
                row = (
                    await session.execute(
                        select(VideoGenerationAttempt, VideoGenerationJob)
                        .join(
                            VideoGenerationJob,
                            VideoGenerationJob.id == VideoGenerationAttempt.video_generation_job_id,
                        )
                        .where(VideoGenerationAttempt.id == attempt_id)
                        .with_for_update()
                    )
                ).one_or_none()
                if row is None:
                    return
                attempt, job = row
                if job.state in {
                    VideoGenerationState.SUCCEEDED,
                    VideoGenerationState.FAILED,
                    VideoGenerationState.CANCELLED,
                } or attempt.state in {
                    VideoGenerationAttemptState.SUCCEEDED,
                    VideoGenerationAttemptState.FAILED,
                    VideoGenerationAttemptState.CANCELLED,
                }:
                    return
                expected = _provider_metadata(attempt)
                superseded_deployment = (
                    self.config.enabled
                    and attempt.worker_image_digest != self.config.worker_image_digest
                )
                identity_matches = all(
                    remote.metadata.get(key) == value for key, value in expected.items()
                ) and _remote_input_matches(
                    remote,
                    attempt=attempt,
                    job=job,
                    signing_key_id=self.config.signing_key_id,
                    signing_private_key=self.config.signing_private_key,
                    verify_signature=self.config.enabled and not superseded_deployment,
                )
                if not identity_matches or superseded_deployment:
                    _mark_provider_quarantine_locked(
                        attempt,
                        job,
                        provider_ids=(str(remote.id),),
                        code=(
                            "video_deployment_superseded"
                            if superseded_deployment
                            else "video_provider_identity_mismatch"
                        ),
                        now=now,
                    )
                    return
                if attempt.provider_external_id not in {None, str(remote.id)}:
                    _mark_provider_quarantine_locked(
                        attempt,
                        job,
                        provider_ids=(attempt.provider_external_id, str(remote.id)),
                        code="video_provider_identity_mismatch",
                        now=now,
                    )
                    return
                attempt.provider_external_id = str(remote.id)
                attempt.submitted_at = attempt.submitted_at or now
                attempt.last_observed_at = now
                attempt.provider_state = remote.status.value
                cancellation_pending = job.state == VideoGenerationState.CANCEL_REQUESTED
                attempt.state = (
                    VideoGenerationAttemptState.CANCEL_REQUESTED
                    if cancellation_pending
                    else (
                        VideoGenerationAttemptState.RUNNING
                        if remote.status == SaladJobStatus.RUNNING
                        else VideoGenerationAttemptState.SUBMITTED
                    )
                )
                attempt.started_at = now if remote.status == SaladJobStatus.RUNNING else None
                attempt.updated_at = now
                if not cancellation_pending:
                    job.state = VideoGenerationState.RUNNING
                job.last_error_code = None
                job.last_error_detail = None
                job.updated_at = now
                job.lock_version += 1

    async def _next_remote_attempt(
        self,
        *,
        now: datetime,
    ) -> _ObservationClaim | None:
        observation_due = now - timedelta(seconds=self.config.reconciliation_interval_seconds)
        lease_owner = f"{self.worker_id[:120]}:observe:{uuid7()}"
        lease_seconds = min(
            self.config.attempt_watchdog_seconds,
            max(120, self.config.reconciliation_interval_seconds * 4),
        )
        async with self.sessions() as session:
            async with session.begin():
                row = (
                    await session.execute(
                        select(VideoGenerationAttempt, VideoGenerationJob)
                        .join(
                            VideoGenerationJob,
                            VideoGenerationJob.id == VideoGenerationAttempt.video_generation_job_id,
                        )
                        .where(
                            VideoGenerationAttempt.provider_external_id.is_not(None),
                            VideoGenerationAttempt.state.in_(_REMOTE_ATTEMPT_STATES),
                            or_(
                                VideoGenerationAttempt.last_observed_at.is_(None),
                                VideoGenerationAttempt.last_observed_at <= observation_due,
                            ),
                            or_(
                                VideoGenerationJob.lease_owner.is_(None),
                                VideoGenerationJob.lease_expires_at <= now,
                            ),
                        )
                        .order_by(
                            VideoGenerationAttempt.last_observed_at.asc().nulls_first(),
                            VideoGenerationAttempt.created_at,
                        )
                        .limit(1)
                        .with_for_update(skip_locked=True)
                    )
                ).one_or_none()
                if row is None:
                    return None
                attempt, job = row
                external_id = attempt.provider_external_id
                if external_id is None:
                    return None
                deployment = await session.get(SaladDeployment, attempt.salad_deployment_id)
                if (
                    deployment is None
                    or deployment.purpose != SaladDeploymentPurpose.VIDEO
                    or deployment.worker_image_digest != attempt.worker_image_digest
                ):
                    return None
                # Commit a unique, expiring observation lease before provider
                # I/O. Every mutation verifies this token, so a stale
                # controller cannot overwrite a newer observation.
                attempt.last_observed_at = now
                attempt.updated_at = now
                job.lease_owner = lease_owner
                job.lease_expires_at = now + timedelta(seconds=lease_seconds)
                job.updated_at = now
                raw_quarantine_ids = attempt.request_metadata.get(
                    "quarantine_provider_ids",
                    [],
                )
                quarantine_ids = (
                    tuple(value for value in raw_quarantine_ids if isinstance(value, str) and value)
                    if isinstance(raw_quarantine_ids, list)
                    else ()
                )
                raw_quarantine_code = attempt.request_metadata.get("quarantine_error_code")
                cancellation_pending = (
                    job.state == VideoGenerationState.CANCEL_REQUESTED
                    or attempt.state == VideoGenerationAttemptState.CANCEL_REQUESTED
                )
                cancel_requested_at = _cancel_requested_at(attempt)
                if cancellation_pending and cancel_requested_at is None:
                    cancel_requested_at = _set_cancel_requested_at(attempt, now)
                return _ObservationClaim(
                    attempt_id=attempt.id,
                    deployment_id=deployment.id,
                    queue_name=deployment.queue_name,
                    container_group_name=deployment.container_group_name,
                    external_id=external_id,
                    cancel_requested=cancellation_pending,
                    cancel_requested_at=cancel_requested_at,
                    submitted_at=_as_utc(attempt.submit_started_at or attempt.created_at),
                    running_started_at=(
                        _as_utc(attempt.started_at) if attempt.started_at is not None else None
                    ),
                    lease_owner=lease_owner,
                    quarantine_provider_ids=quarantine_ids,
                    quarantine_error_code=(
                        raw_quarantine_code if isinstance(raw_quarantine_code, str) else None
                    ),
                )

    async def _record_remote_progress(
        self,
        attempt_id: UUID,
        remote: SaladQueueJob,
        *,
        now: datetime,
        expected_lease_owner: str,
    ) -> None:
        async with self.sessions() as session:
            async with session.begin():
                row = (
                    await session.execute(
                        select(VideoGenerationAttempt, VideoGenerationJob)
                        .join(
                            VideoGenerationJob,
                            VideoGenerationJob.id == VideoGenerationAttempt.video_generation_job_id,
                        )
                        .where(VideoGenerationAttempt.id == attempt_id)
                        .with_for_update()
                    )
                ).one_or_none()
                if row is None:
                    return
                attempt, job = row
                if job.lease_owner != expected_lease_owner:
                    return
                if attempt.provider_external_id != str(remote.id):
                    await self._fail_locked(
                        session,
                        attempt,
                        job,
                        code="video_provider_identity_mismatch",
                        retryable=False,
                        now=now,
                        expected_lease_owner=expected_lease_owner,
                    )
                    return
                attempt.last_observed_at = now
                attempt.provider_state = remote.status.value
                cancellation_pending = (
                    job.state == VideoGenerationState.CANCEL_REQUESTED
                    or attempt.state == VideoGenerationAttemptState.CANCEL_REQUESTED
                )
                if cancellation_pending:
                    attempt.state = VideoGenerationAttemptState.CANCEL_REQUESTED
                else:
                    attempt.state = (
                        VideoGenerationAttemptState.RUNNING
                        if remote.status == SaladJobStatus.RUNNING
                        else VideoGenerationAttemptState.SUBMITTED
                    )
                if remote.status == SaladJobStatus.RUNNING:
                    attempt.started_at = attempt.started_at or now
                attempt.updated_at = now
                if not cancellation_pending:
                    job.state = VideoGenerationState.RUNNING
                job.lease_owner = None
                job.lease_expires_at = None
                job.updated_at = now

    async def _collect_success(
        self,
        attempt_id: UUID,
        remote: SaladQueueJob,
        *,
        now: datetime,
        expected_lease_owner: str,
    ) -> None:
        try:
            response = AnimateResponse.model_validate(remote.output, strict=True)
        except ValidationError:
            await self._fail_attempt(
                attempt_id,
                code="video_worker_response_invalid",
                retryable=True,
                now=now,
                expected_lease_owner=expected_lease_owner,
            )
            return

        async with self.sessions() as session:
            row = (
                await session.execute(
                    select(VideoGenerationAttempt, VideoGenerationJob)
                    .join(
                        VideoGenerationJob,
                        VideoGenerationJob.id == VideoGenerationAttempt.video_generation_job_id,
                    )
                    .where(VideoGenerationAttempt.id == attempt_id)
                )
            ).one_or_none()
            if row is None:
                return
            attempt, job = row
            if job.lease_owner != expected_lease_owner:
                return
            output_asset_id, upload_attempt_id, output_key = _attempt_output_identity(attempt)
            if not _response_matches(
                response,
                remote=remote,
                attempt=attempt,
                job=job,
                output_asset_id=output_asset_id,
                upload_attempt_id=upload_attempt_id,
                max_output_bytes=self.config.max_output_bytes,
            ):
                await self._fail_attempt(
                    attempt_id,
                    code="video_worker_identity_mismatch",
                    retryable=False,
                    now=now,
                    expected_lease_owner=expected_lease_owner,
                )
                return

        try:
            metadata = await self.store.head(output_key)
            if metadata is None or not _head_matches(
                metadata,
                response=response,
                job_id=job.id,
                attempt_id=attempt.id,
                output_asset_id=output_asset_id,
                upload_attempt_id=upload_attempt_id,
            ):
                raise ValueError("video output object identity mismatch")
            digest = await self._object_sha256(metadata)
            if digest != response.output_sha256:
                raise ValueError("video output digest mismatch")
        except (ObjectStoreError, ValueError):
            await self._fail_attempt(
                attempt_id,
                code="video_output_verification_failed",
                retryable=True,
                now=now,
                expected_lease_owner=expected_lease_owner,
            )
            return

        async with self.sessions() as session:
            async with session.begin():
                output_row = (
                    await session.execute(
                        select(VideoGenerationAttempt, VideoGenerationJob, Asset)
                        .join(
                            VideoGenerationJob,
                            VideoGenerationJob.id == VideoGenerationAttempt.video_generation_job_id,
                        )
                        .join(Asset, Asset.id == output_asset_id)
                        .where(VideoGenerationAttempt.id == attempt_id)
                        .with_for_update()
                    )
                ).one_or_none()
                if output_row is None:
                    return
                attempt, job, asset = output_row
                if job.lease_owner != expected_lease_owner:
                    return
                existing = await session.scalar(
                    select(VideoGenerationOutput).where(
                        VideoGenerationOutput.video_generation_job_id == job.id
                    )
                )
                if existing is not None:
                    return
                if (
                    attempt.provider_external_id != str(remote.id)
                    or asset.staging_object_key != output_key
                    or asset.state != AssetState.UPLOADING
                ):
                    await self._fail_locked(
                        session,
                        attempt,
                        job,
                        code="video_output_identity_changed",
                        retryable=False,
                        now=now,
                        expected_lease_owner=expected_lease_owner,
                    )
                    return

                lineage = AssetLineage(
                    id=uuid7(),
                    parent_asset_id=job.source_asset_id,
                    child_asset_id=asset.id,
                    relation="animated_from",
                    recipe_version=_LINEAGE_RECIPE,
                    created_at=now,
                )
                asset.state = AssetState.AVAILABLE
                asset.staging_object_key = None
                asset.object_key = output_key
                asset.object_version_id = metadata.version_id
                asset.sha256 = response.output_sha256
                asset.content_type = _OUTPUT_CONTENT_TYPE
                # The shared Asset table predates video derivatives and names
                # this media-format column ``image_format``.  Store the actual
                # container format until the schema is generalized.
                asset.image_format = "mp4"
                asset.width = response.width
                asset.height = response.height
                asset.byte_size = response.output_size_bytes
                asset.available_at = now
                asset.asset_metadata = {
                    **asset.asset_metadata,
                    "video_format": "mp4",
                    "fps": response.fps,
                    "native_frame_count": response.native_frame_count,
                    "output_frame_count": response.output_frame_count,
                    "duration_ms": round(response.output_duration_seconds * 1000),
                    "loop_mode": response.loop_mode,
                }
                output = VideoGenerationOutput(
                    id=uuid7(),
                    video_generation_job_id=job.id,
                    successful_attempt_id=attempt.id,
                    source_asset_id=job.source_asset_id,
                    asset_id=asset.id,
                    asset_lineage_id=lineage.id,
                    storage_backend=asset.storage_backend,
                    storage_bucket=asset.storage_bucket,
                    object_key=output_key,
                    object_version_id=metadata.version_id,
                    sha256=response.output_sha256,
                    content_type=_OUTPUT_CONTENT_TYPE,
                    video_format="mp4",
                    width=response.width,
                    height=response.height,
                    byte_size=response.output_size_bytes,
                    frame_count=response.output_frame_count,
                    fps=response.fps,
                    duration_ms=round(response.output_duration_seconds * 1000),
                    created_at=now,
                )
                elapsed_ms, actual_cost = self._cost(attempt, now)
                attempt.state = VideoGenerationAttemptState.SUCCEEDED
                attempt.provider_state = remote.status.value
                attempt.last_observed_at = now
                attempt.response_metadata = response.model_dump(mode="json")
                attempt.completed_at = now
                attempt.actual_cost_microusd = actual_cost
                attempt.billed_duration_ms = elapsed_ms
                attempt.updated_at = now
                job.state = VideoGenerationState.SUCCEEDED
                job.completed_at = now
                job.actual_cost_microusd += actual_cost
                job.billed_duration_ms += elapsed_ms
                job.cost_metadata = {
                    **job.cost_metadata,
                    "runtime_cost_basis": "conservative_elapsed_upper_bound",
                    "runtime_hourly_cap_microusd": self.config.max_hourly_cost_microusd,
                }
                job.reserved_cost_microusd = 0
                job.lease_owner = None
                job.lease_expires_at = None
                job.last_error_code = None
                job.last_error_detail = None
                job.updated_at = now
                job.lock_version += 1
                session.add_all((lineage, output))

    async def _object_sha256(self, metadata: ObjectMetadata) -> str:
        stored = metadata.metadata.get("sha256")
        if stored is not None and len(stored) == 64:
            return stored
        if metadata.version_id is not None:
            digest = await self.store.sha256(
                metadata.key,
                max_bytes=self.config.max_output_bytes,
                version_id=metadata.version_id,
                etag=metadata.etag,
            )
            if digest.byte_size != metadata.byte_size:
                raise ValueError("video output size changed")
            return digest.sha256
        body = await self.store.read_bytes(
            metadata.key,
            max_bytes=self.config.max_output_bytes,
            etag=metadata.etag,
        )
        if len(body) != metadata.byte_size:
            raise ValueError("video output size changed")
        return hashlib.sha256(body).hexdigest()

    async def _mark_unknown(
        self,
        attempt_id: UUID,
        *,
        code: str,
        now: datetime,
        expected_lease_owner: str | None = None,
    ) -> None:
        async with self.sessions() as session:
            async with session.begin():
                row = (
                    await session.execute(
                        select(VideoGenerationAttempt, VideoGenerationJob)
                        .join(
                            VideoGenerationJob,
                            VideoGenerationJob.id == VideoGenerationAttempt.video_generation_job_id,
                        )
                        .where(VideoGenerationAttempt.id == attempt_id)
                        .with_for_update()
                    )
                ).one_or_none()
                if row is None:
                    return
                attempt, job = row
                if expected_lease_owner is not None and job.lease_owner != expected_lease_owner:
                    return
                if job.state in {
                    VideoGenerationState.SUCCEEDED,
                    VideoGenerationState.FAILED,
                    VideoGenerationState.CANCELLED,
                }:
                    return
                cancellation_pending = job.state == VideoGenerationState.CANCEL_REQUESTED
                attempt.state = VideoGenerationAttemptState.UNKNOWN
                attempt.error_code = code
                attempt.error_detail = "The provider submission outcome is being reconciled."
                attempt.last_observed_at = now
                attempt.updated_at = now
                if not cancellation_pending:
                    job.state = VideoGenerationState.UNKNOWN
                job.last_error_code = code
                job.last_error_detail = "The provider submission outcome is being reconciled."
                if expected_lease_owner is not None:
                    job.lease_owner = None
                    job.lease_expires_at = None
                job.updated_at = now
                job.lock_version += 1

    async def _mark_cancel_requested(
        self,
        attempt_id: UUID,
        *,
        code: str,
        now: datetime,
        expected_lease_owner: str,
    ) -> None:
        async with self.sessions() as session:
            async with session.begin():
                row = (
                    await session.execute(
                        select(VideoGenerationAttempt, VideoGenerationJob)
                        .join(
                            VideoGenerationJob,
                            VideoGenerationJob.id == VideoGenerationAttempt.video_generation_job_id,
                        )
                        .where(VideoGenerationAttempt.id == attempt_id)
                        .with_for_update()
                    )
                ).one_or_none()
                if row is None:
                    return
                attempt, job = row
                if job.lease_owner != expected_lease_owner:
                    return
                if attempt.provider_external_id is None:
                    return
                if job.state in {
                    VideoGenerationState.SUCCEEDED,
                    VideoGenerationState.FAILED,
                    VideoGenerationState.CANCELLED,
                }:
                    job.lease_owner = None
                    job.lease_expires_at = None
                    return
                attempt.state = VideoGenerationAttemptState.CANCEL_REQUESTED
                _set_cancel_requested_at(attempt, now)
                attempt.error_code = code
                attempt.error_detail = "Provider cancellation is awaiting confirmation."
                attempt.updated_at = now
                job.state = VideoGenerationState.CANCEL_REQUESTED
                job.last_error_code = code
                job.last_error_detail = "Provider cancellation is awaiting confirmation."
                job.lease_owner = None
                job.lease_expires_at = None
                job.updated_at = now
                job.lock_version += 1

    async def _fail_attempt(
        self,
        attempt_id: UUID,
        *,
        code: str,
        retryable: bool,
        now: datetime,
        cancelled: bool = False,
        expected_lease_owner: str | None = None,
    ) -> None:
        async with self.sessions() as session:
            async with session.begin():
                row = (
                    await session.execute(
                        select(VideoGenerationAttempt, VideoGenerationJob)
                        .join(
                            VideoGenerationJob,
                            VideoGenerationJob.id == VideoGenerationAttempt.video_generation_job_id,
                        )
                        .where(VideoGenerationAttempt.id == attempt_id)
                        .with_for_update()
                    )
                ).one_or_none()
                if row is None:
                    return
                await self._fail_locked(
                    session,
                    row[0],
                    row[1],
                    code=code,
                    retryable=retryable,
                    now=now,
                    cancelled=cancelled,
                    expected_lease_owner=expected_lease_owner,
                )

    async def _fail_locked(
        self,
        session: AsyncSession,
        attempt: VideoGenerationAttempt,
        job: VideoGenerationJob,
        *,
        code: str,
        retryable: bool,
        now: datetime,
        cancelled: bool = False,
        expected_lease_owner: str | None = None,
    ) -> None:
        if expected_lease_owner is not None and job.lease_owner != expected_lease_owner:
            return
        if job.state in {
            VideoGenerationState.SUCCEEDED,
            VideoGenerationState.FAILED,
            VideoGenerationState.CANCELLED,
        } or attempt.state in {
            VideoGenerationAttemptState.SUCCEEDED,
            VideoGenerationAttemptState.FAILED,
            VideoGenerationAttemptState.CANCELLED,
        }:
            if expected_lease_owner is not None:
                job.lease_owner = None
                job.lease_expires_at = None
            return
        existing_output = await session.scalar(
            select(VideoGenerationOutput.id).where(
                VideoGenerationOutput.video_generation_job_id == job.id
            )
        )
        if existing_output is not None:
            if expected_lease_owner is not None:
                job.lease_owner = None
                job.lease_expires_at = None
            return
        elapsed_ms, actual_cost = self._cost(attempt, now)
        attempt.state = (
            VideoGenerationAttemptState.CANCELLED
            if cancelled and attempt.provider_external_id is not None
            else VideoGenerationAttemptState.FAILED
        )
        attempt.completed_at = now
        attempt.error_code = code
        attempt.error_detail = "Animation generation did not complete."
        attempt.actual_cost_microusd = actual_cost
        attempt.billed_duration_ms = elapsed_ms
        attempt.updated_at = now
        output_asset_id, _upload_attempt_id, _key = _attempt_output_identity(attempt)
        asset = await session.scalar(
            select(Asset).where(Asset.id == output_asset_id).with_for_update()
        )
        if asset is not None and asset.state != AssetState.AVAILABLE:
            asset.state = AssetState.QUARANTINED
            asset.verification_error_code = code
            asset.verification_error_detail = "Animation output was not accepted."
            asset.purge_after = now + timedelta(days=1)

        job.actual_cost_microusd += actual_cost
        job.billed_duration_ms += elapsed_ms
        job.cost_metadata = {
            **job.cost_metadata,
            "runtime_cost_basis": "conservative_elapsed_upper_bound",
            "runtime_hourly_cap_microusd": self.config.max_hourly_cost_microusd,
        }
        job.reserved_cost_microusd = 0
        if expected_lease_owner is not None:
            job.lease_owner = None
            job.lease_expires_at = None
        job.last_error_code = code
        job.last_error_detail = "Animation generation did not complete."
        job.lock_version += 1
        job.updated_at = now
        if cancelled:
            job.state = VideoGenerationState.CANCELLED
            job.completed_at = now
        elif retryable and job.attempt_count < job.max_attempts:
            job.state = VideoGenerationState.RETRY_WAIT
            job.retry_at = now + timedelta(seconds=self.config.retry_delay_seconds)
            job.completed_at = None
        else:
            job.state = VideoGenerationState.FAILED
            job.completed_at = now

    def _cost(self, attempt: VideoGenerationAttempt, now: datetime) -> tuple[int, int]:
        # Salad does not return authoritative billing on the queue response.
        # Persist a conservative elapsed-time upper bound, capped by the
        # reservation. Definitely-unsubmitted work has zero provider usage.
        if attempt.submit_started_at is None:
            return 0, 0
        elapsed_ms = max(
            0,
            math.ceil((now - _as_utc(attempt.submit_started_at)).total_seconds() * 1000),
        )
        cost = math.ceil(self.config.max_hourly_cost_microusd * elapsed_ms / 3_600_000)
        return elapsed_ms, min(cost, attempt.reserved_cost_microusd)


async def request_video_cancellation(
    session: AsyncSession,
    *,
    job_id: UUID,
    actor_user_id: UUID | None = None,
    now: datetime | None = None,
) -> bool:
    """Request cancellation without issuing a provider side effect in the API process."""

    requested_at = _utc(now)
    query = select(VideoGenerationJob).where(VideoGenerationJob.id == job_id)
    if actor_user_id is not None:
        query = query.where(VideoGenerationJob.created_by_user_id == actor_user_id)
    job = await session.scalar(query.with_for_update())
    if job is None:
        return False
    if job.state in {
        VideoGenerationState.SUCCEEDED,
        VideoGenerationState.FAILED,
        VideoGenerationState.CANCELLED,
    }:
        return True
    previous_state = job.state
    attempt = await session.scalar(
        select(VideoGenerationAttempt)
        .where(VideoGenerationAttempt.video_generation_job_id == job.id)
        .order_by(VideoGenerationAttempt.attempt_no.desc())
        .limit(1)
        .with_for_update()
    )
    if job.state in {
        VideoGenerationState.QUEUED,
        VideoGenerationState.RETRY_WAIT,
    } and (attempt is None or attempt.state not in _ACTIVE_ATTEMPT_STATES):
        # There is no live provider work to cancel. In particular, a retry-wait
        # job's latest attempt is already terminal and must never be resurrected
        # or charged a second time merely because the parent is cancelled.
        job.state = VideoGenerationState.CANCELLED
        job.completed_at = requested_at
        job.retry_at = None
        job.reserved_cost_microusd = 0
    elif attempt is None:
        job.state = VideoGenerationState.CANCELLED
        job.completed_at = requested_at
        job.reserved_cost_microusd = 0
    elif (
        attempt.provider_external_id is None
        and attempt.state == VideoGenerationAttemptState.CREATED
    ):
        # The persistence contract reserves CANCELLED for a confirmed remote
        # job identity. A local pre-submit cancellation is terminal FAILED at
        # the attempt level while its parent job is correctly CANCELLED.
        attempt.state = VideoGenerationAttemptState.FAILED
        attempt.completed_at = requested_at
        attempt.error_code = "video_cancelled_before_submission"
        attempt.error_detail = "Animation generation was cancelled."
        attempt.updated_at = requested_at
        try:
            output_asset_id, _upload_attempt_id, _key = _attempt_output_identity(attempt)
        except VideoRuntimeError:
            output_asset_id = None
        if output_asset_id is not None:
            asset = await session.scalar(
                select(Asset).where(Asset.id == output_asset_id).with_for_update()
            )
            if asset is not None and asset.state != AssetState.AVAILABLE:
                asset.state = AssetState.QUARANTINED
                asset.verification_error_code = "video_cancelled_before_submission"
                asset.verification_error_detail = "Animation generation was cancelled."
                asset.purge_after = requested_at + timedelta(days=1)
        job.state = VideoGenerationState.CANCELLED
        job.completed_at = requested_at
        job.reserved_cost_microusd = 0
    elif attempt.provider_external_id is None:
        # SUBMITTING/UNKNOWN without an external ID is the POST ambiguity
        # window. Keep reconciling the deterministic metadata; terminally
        # cancelling here could orphan a provider job that accepted the POST.
        job.state = VideoGenerationState.CANCEL_REQUESTED
        _set_cancel_requested_at(attempt, requested_at)
        job.last_error_code = "video_cancel_reconciliation_pending"
        job.last_error_detail = "Cancellation is waiting for provider reconciliation."
        attempt.error_code = "video_cancel_reconciliation_pending"
        attempt.error_detail = "Cancellation is waiting for provider reconciliation."
        attempt.updated_at = requested_at
    else:
        job.state = VideoGenerationState.CANCEL_REQUESTED
        attempt.state = VideoGenerationAttemptState.CANCEL_REQUESTED
        _set_cancel_requested_at(attempt, requested_at)
        attempt.last_observed_at = None
        attempt.updated_at = requested_at
    job.updated_at = requested_at
    job.lock_version += 1
    if job.state != previous_state:
        session.add(
            AuditEvent(
                actor=(str(actor_user_id) if actor_user_id is not None else "system:video-runtime"),
                action="video_generation.cancellation_requested",
                resource_type="video_generation_job",
                resource_id=job.id,
                correlation_id=f"video-cancel:{job.id}",
                detail={
                    "previous_state": previous_state.value,
                    "state": job.state.value,
                    "attempt_id": str(attempt.id) if attempt is not None else None,
                },
                occurred_at=requested_at,
            )
        )
    await session.flush()
    return True


def _mark_provider_quarantine_locked(
    attempt: VideoGenerationAttempt,
    job: VideoGenerationJob,
    *,
    provider_ids: tuple[str, ...],
    code: str,
    now: datetime,
) -> None:
    unique_ids = tuple(dict.fromkeys(value for value in provider_ids if value))
    if not unique_ids:
        return
    attempt.provider_external_id = attempt.provider_external_id or unique_ids[0]
    attempt.submitted_at = attempt.submitted_at or now
    attempt.state = VideoGenerationAttemptState.CANCEL_REQUESTED
    attempt.last_observed_at = None
    attempt.error_code = code
    attempt.error_detail = "Known provider jobs are quarantined pending terminal confirmation."
    attempt.request_metadata = {
        **attempt.request_metadata,
        "quarantine_provider_ids": list(unique_ids),
        "quarantine_error_code": code,
    }
    _set_cancel_requested_at(attempt, now)
    attempt.updated_at = now
    job.state = VideoGenerationState.CANCEL_REQUESTED
    job.last_error_code = code
    job.last_error_detail = "Known provider jobs are quarantined pending terminal confirmation."
    job.updated_at = now
    job.lock_version += 1


def _cancel_requested_at(attempt: VideoGenerationAttempt) -> datetime | None:
    raw = attempt.request_metadata.get("cancel_requested_at")
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    try:
        return datetime.fromtimestamp(raw, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _set_cancel_requested_at(
    attempt: VideoGenerationAttempt,
    now: datetime,
) -> datetime:
    existing = _cancel_requested_at(attempt)
    if existing is not None:
        return existing
    attempt.request_metadata = {
        **attempt.request_metadata,
        "cancel_requested_at": int(now.timestamp()),
    }
    return now


def _attempt_output_identity(attempt: VideoGenerationAttempt) -> tuple[UUID, UUID, str]:
    try:
        output_asset_id = UUID(str(attempt.request_metadata["output_asset_id"]))
        upload_attempt_id = UUID(str(attempt.request_metadata["upload_attempt_id"]))
        output_key = str(attempt.request_metadata["output_object_key"])
    except (KeyError, TypeError, ValueError):
        raise VideoRuntimeError("video attempt identity is invalid") from None
    if not output_key or len(output_key) > 1024:
        raise VideoRuntimeError("video attempt identity is invalid")
    return output_asset_id, upload_attempt_id, output_key


def _provider_metadata(attempt: VideoGenerationAttempt) -> dict[str, str]:
    return {
        "kind": _METADATA_KIND,
        "video_job_id": str(attempt.video_generation_job_id),
        "video_attempt_id": str(attempt.id),
        "submission_key": attempt.submission_key,
    }


def _source_matches(job: VideoGenerationJob, source: Asset, store: ObjectStore) -> bool:
    return (
        source.state == AssetState.AVAILABLE
        and source.storage_backend == store.backend == job.source_storage_backend
        and source.storage_bucket == store.bucket == job.source_storage_bucket
        and source.object_key == job.source_object_key
        and source.object_version_id == job.source_object_version_id
        and source.sha256 == job.source_sha256
        and source.content_type == job.source_content_type
        and source.image_format == job.source_image_format
        and source.width == job.source_width
        and source.height == job.source_height
        and source.byte_size == job.source_byte_size
        and job.source_content_type in _SOURCE_CONTENT_TYPES
    )


def _profile_matches(job: VideoGenerationJob) -> bool:
    return (
        job.profile_key == PINNED_VIDEO_PROFILE.profile_id
        and job.profile_version == PINNED_VIDEO_PROFILE.adapter_revision
        and job.profile_sha256 == PINNED_VIDEO_PROFILE_SHA256
        and len(job.prompt) <= 4000
        and len(job.negative_prompt) <= 4000
    )


def _response_matches(
    response: AnimateResponse,
    *,
    remote: SaladQueueJob,
    attempt: VideoGenerationAttempt,
    job: VideoGenerationJob,
    output_asset_id: UUID,
    upload_attempt_id: UUID,
    max_output_bytes: int,
) -> bool:
    expected_output_frames = (job.frame_count * 2) - 2
    expected_native_duration = job.frame_count / job.fps
    expected_duration = expected_output_frames / job.fps
    return (
        attempt.provider_external_id == str(remote.id)
        and response.version == "video-worker.v1"
        and response.status == "succeeded"
        and response.job_id == str(job.id)
        and response.attempt_id == str(attempt.id)
        and response.profile_id == job.profile_key
        and response.source_asset_id == str(job.source_asset_id)
        and response.output_asset_id == str(output_asset_id)
        and response.upload_attempt_id == str(upload_attempt_id)
        and len(response.output_sha256) == 64
        and response.output_sha256 == response.output_sha256.lower()
        and all(character in "0123456789abcdef" for character in response.output_sha256)
        and 0 < response.output_size_bytes <= max_output_bytes
        and response.loop_mode == job.loop_mode
        and response.fps == job.fps
        and response.width == job.width
        and response.height == job.height
        and response.native_frame_count == job.frame_count
        and math.isclose(
            response.native_duration_seconds,
            expected_native_duration,
            rel_tol=0,
            abs_tol=1e-6,
        )
        and response.output_frame_count == expected_output_frames
        and math.isclose(
            response.output_duration_seconds,
            expected_duration,
            rel_tol=0,
            abs_tol=1e-6,
        )
    )


def _remote_input_matches(
    remote: SaladQueueJob,
    *,
    attempt: VideoGenerationAttempt,
    job: VideoGenerationJob,
    signing_key_id: str,
    signing_private_key: str,
    verify_signature: bool = True,
) -> bool:
    try:
        envelope = AnimateEnvelope.model_validate(remote.input, strict=True)
        output_asset_id, upload_attempt_id, _output_key = _attempt_output_identity(attempt)
        payload = envelope.payload
        expected_signature = (
            calculate_signature(envelope, signing_private_key)
            if verify_signature
            else envelope.signature
        )
    except (ValidationError, ValueError, VideoRuntimeError):
        return False
    return (
        envelope.version == "video-worker.v1"
        and (not verify_signature or envelope.key_id == signing_key_id)
        and (not verify_signature or hmac.compare_digest(envelope.signature, expected_signature))
        and payload.job_id == str(job.id)
        and payload.attempt_id == str(attempt.id)
        and payload.profile_id == job.profile_key
        and payload.source.asset_id == str(job.source_asset_id)
        and payload.source.content_type == job.source_content_type
        and payload.source.size_bytes == job.source_byte_size
        and payload.source.sha256 == job.source_sha256
        and payload.upload.asset_id == str(output_asset_id)
        and payload.upload.upload_attempt_id == str(upload_attempt_id)
        and payload.upload.content_type == _OUTPUT_CONTENT_TYPE
        and payload.prompt == job.prompt
        and payload.negative_prompt == job.negative_prompt
        and payload.seed == job.seed
        and payload.native_frame_count == job.frame_count
        and payload.fps == job.fps
        and payload.width == job.width
        and payload.height == job.height
        and payload.loop_mode == job.loop_mode
    )


def _head_matches(
    metadata: ObjectMetadata,
    *,
    response: AnimateResponse,
    job_id: UUID,
    attempt_id: UUID,
    output_asset_id: UUID,
    upload_attempt_id: UUID,
) -> bool:
    return (
        metadata.byte_size == response.output_size_bytes
        and metadata.content_type == _OUTPUT_CONTENT_TYPE
        and metadata.metadata.get("schema") == _METADATA_KIND
        and metadata.metadata.get("video-job-id") == str(job_id)
        and metadata.metadata.get("video-attempt-id") == str(attempt_id)
        and metadata.metadata.get("output-asset-id") == str(output_asset_id)
        and metadata.metadata.get("upload-attempt-id") == str(upload_attempt_id)
    )


def _terminal_job(
    job: VideoGenerationJob,
    *,
    state: VideoGenerationState,
    now: datetime,
    error_code: str,
) -> None:
    job.state = state
    job.completed_at = now
    job.last_error_code = error_code
    job.last_error_detail = "Animation generation could not be started."
    job.reserved_cost_microusd = 0
    job.updated_at = now
    job.lock_version += 1


def _utc(value: datetime | None) -> datetime:
    return _as_utc(value or datetime.now(UTC))


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
