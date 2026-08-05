from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from gen_automation.db.models import (
    Asset,
    AuditEvent,
    GenerationAttempt,
    GenerationJob,
    Release,
    ReleaseVersion,
)
from gen_automation.domain.enums import (
    AssetKind,
    AssetState,
    GenerationAttemptState,
    GenerationState,
    ReleasePhase,
    ResourceHealth,
)
from gen_automation.services.assets import (
    AssetBusyError,
    AssetQuarantinedError,
    AssetStorageUnavailableError,
    UploadNotReadyError,
    finalize_raw_master,
)
from gen_automation.storage.base import ObjectStore


class CollectionError(Exception):
    """Base error for controller-side master collection."""


class CollectionLeaseError(CollectionError):
    pass


@dataclass(frozen=True)
class ClaimedCollectionJob:
    job_id: UUID
    lease_expires_at: datetime


@dataclass(frozen=True)
class CollectionResult:
    job_id: UUID
    state: GenerationState
    finalized_assets: int
    retry_at: datetime | None = None
    error_code: str | None = None


@dataclass(frozen=True)
class ProgressiveCollectionResult:
    """One bounded attempt to publish the next output from an active GPU job."""

    asset_id: UUID | None
    finalized: bool


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


async def collect_next_ready_running_asset(
    session: AsyncSession,
    store: ObjectStore,
    *,
    worker_id: str,
    max_image_bytes: int,
    verification_lease_seconds: int = 900,
) -> ProgressiveCollectionResult:
    """Verify one visible staging upload without waiting for its batch to finish.

    The GPU worker uploads outputs in ``output_index`` order. Looking only at the
    first unfinished output keeps the active-job poll to one object-store HEAD
    request per cycle. The existing per-asset verification lease remains the
    concurrency boundary, while the generation job stays RUNNING and under the
    provider reconciler's ownership.
    """

    if not worker_id.strip() or len(worker_id) > 200:
        raise ValueError("worker_id is invalid")
    if max_image_bytes <= 0:
        raise ValueError("max_image_bytes must be positive")
    if verification_lease_seconds <= 0:
        raise ValueError("verification_lease_seconds must be positive")

    candidate = (
        await session.execute(
            select(Asset.id, Asset.staging_object_key)
            .join(GenerationJob, GenerationJob.id == Asset.generation_job_id)
            .where(
                GenerationJob.state == GenerationState.RUNNING,
                Asset.kind == AssetKind.RAW_MASTER,
                Asset.state == AssetState.UPLOADING,
                Asset.staging_object_key.is_not(None),
            )
            .order_by(
                GenerationJob.priority,
                GenerationJob.updated_at,
                GenerationJob.id,
                Asset.output_index,
            )
            .limit(1)
        )
    ).one_or_none()
    await session.rollback()
    if candidate is None:
        return ProgressiveCollectionResult(asset_id=None, finalized=False)

    asset_id, staging_object_key = candidate
    assert staging_object_key is not None
    if await store.head(staging_object_key) is None:
        return ProgressiveCollectionResult(asset_id=asset_id, finalized=False)

    try:
        result = await finalize_raw_master(
            session,
            store,
            asset_id=asset_id,
            max_bytes=max_image_bytes,
            verification_lease_seconds=verification_lease_seconds,
            actor=worker_id,
        )
    except (UploadNotReadyError, AssetBusyError, AssetStorageUnavailableError):
        await session.rollback()
        return ProgressiveCollectionResult(asset_id=asset_id, finalized=False)
    except AssetQuarantinedError:
        # The normal post-provider collection pass owns the fail-closed job and
        # release transition. The quarantined asset is durable until that pass.
        await session.rollback()
        return ProgressiveCollectionResult(asset_id=asset_id, finalized=False)
    return ProgressiveCollectionResult(asset_id=asset_id, finalized=not result.replayed)


async def claim_collection_jobs(
    session: AsyncSession,
    *,
    worker_id: str,
    limit: int = 10,
    lease_seconds: int = 900,
    now: datetime | None = None,
) -> list[ClaimedCollectionJob]:
    if not worker_id.strip() or len(worker_id) > 200:
        raise ValueError("worker_id is invalid")
    if limit <= 0 or limit > 100:
        raise ValueError("limit must be between 1 and 100")
    if lease_seconds < 60 or lease_seconds > 3600:
        raise ValueError("lease_seconds must be between 60 and 3600")
    claimed_at = _as_utc(now or datetime.now(UTC))
    claimable = or_(
        (
            (GenerationJob.state == GenerationState.COLLECTING)
            & or_(
                GenerationJob.retry_at.is_(None),
                GenerationJob.retry_at <= claimed_at,
            )
            & or_(
                GenerationJob.lease_expires_at.is_(None),
                GenerationJob.lease_expires_at <= claimed_at,
            )
        ),
        (
            (GenerationJob.state == GenerationState.VERIFYING)
            & GenerationJob.lease_expires_at.is_not(None)
            & (GenerationJob.lease_expires_at <= claimed_at)
        ),
    )
    jobs = list(
        (
            await session.scalars(
                select(GenerationJob)
                .where(claimable)
                .order_by(GenerationJob.priority, GenerationJob.updated_at, GenerationJob.id)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        ).all()
    )
    lease_expires_at = claimed_at + timedelta(seconds=lease_seconds)
    result: list[ClaimedCollectionJob] = []
    for job in jobs:
        job.state = GenerationState.VERIFYING
        job.lease_owner = worker_id
        job.lease_expires_at = lease_expires_at
        job.retry_at = None
        job.lock_version += 1
        session.add(
            AuditEvent(
                actor=worker_id,
                action="generation_job.collection_claimed",
                resource_type="generation_job",
                resource_id=job.id,
                correlation_id=str(job.id),
                detail={},
                occurred_at=claimed_at,
            )
        )
        result.append(
            ClaimedCollectionJob(
                job_id=job.id,
                lease_expires_at=lease_expires_at,
            )
        )
    await session.commit()
    return result


def _require_lease(
    job: GenerationJob,
    *,
    worker_id: str,
    now: datetime,
) -> None:
    expires_at = _as_utc(job.lease_expires_at) if job.lease_expires_at is not None else None
    if (
        job.state != GenerationState.VERIFYING
        or job.lease_owner != worker_id
        or expires_at is None
        or expires_at <= now
    ):
        raise CollectionLeaseError("collection lease is not active")


async def _reschedule(
    session: AsyncSession,
    *,
    job_id: UUID,
    worker_id: str,
    retry_at: datetime,
    error_code: str,
    now: datetime,
) -> CollectionResult:
    job = await session.scalar(
        select(GenerationJob).where(GenerationJob.id == job_id).with_for_update()
    )
    if job is None:
        raise CollectionError("generation job was not found")
    _require_lease(job, worker_id=worker_id, now=now)
    job.state = GenerationState.COLLECTING
    job.retry_at = retry_at
    job.lease_owner = None
    job.lease_expires_at = None
    job.last_error_code = error_code
    job.last_error_detail = "Master collection will be retried."
    job.lock_version += 1
    session.add(
        AuditEvent(
            actor=worker_id,
            action="generation_job.collection_deferred",
            resource_type="generation_job",
            resource_id=job.id,
            correlation_id=str(job.id),
            detail={"error_code": error_code, "retry_at": retry_at.isoformat()},
            occurred_at=now,
        )
    )
    await session.commit()
    return CollectionResult(
        job_id=job.id,
        state=job.state,
        finalized_assets=0,
        retry_at=retry_at,
        error_code=error_code,
    )


async def _fail_closed(
    session: AsyncSession,
    *,
    job_id: UUID,
    worker_id: str,
    error_code: str,
    now: datetime,
) -> CollectionResult:
    row = (
        await session.execute(
            select(GenerationJob, Release)
            .join(ReleaseVersion, ReleaseVersion.id == GenerationJob.release_version_id)
            .join(Release, Release.id == ReleaseVersion.release_id)
            .where(GenerationJob.id == job_id)
            .with_for_update()
        )
    ).one_or_none()
    if row is None:
        raise CollectionError("generation job was not found")
    job, release = row
    _require_lease(job, worker_id=worker_id, now=now)
    job.state = GenerationState.DEAD_LETTER
    job.retry_at = None
    job.lease_owner = None
    job.lease_expires_at = None
    job.last_error_code = error_code
    job.last_error_detail = "Master collection failed closed and requires operator review."
    job.lock_version += 1
    release.health = ResourceHealth.BLOCKED
    release.lock_version += 1
    session.add(
        AuditEvent(
            actor=worker_id,
            action="generation_job.collection_failed",
            resource_type="generation_job",
            resource_id=job.id,
            correlation_id=str(job.id),
            detail={"error_code": error_code},
            occurred_at=now,
        )
    )
    await session.commit()
    return CollectionResult(
        job_id=job.id,
        state=job.state,
        finalized_assets=0,
        error_code=error_code,
    )


async def collect_generation_job(
    session: AsyncSession,
    store: ObjectStore,
    *,
    job_id: UUID,
    worker_id: str,
    max_image_bytes: int,
    retry_delay_seconds: int = 30,
    verification_lease_seconds: int = 900,
    upload_grant_ttl_seconds: int = 10800,
    now: datetime | None = None,
) -> CollectionResult:
    processed_at = _as_utc(now or datetime.now(UTC))
    if retry_delay_seconds <= 0 or retry_delay_seconds > 3600:
        raise ValueError("retry_delay_seconds must be between 1 and 3600")
    if upload_grant_ttl_seconds < 3600 or upload_grant_ttl_seconds > 14400:
        raise ValueError("upload_grant_ttl_seconds must be between 3600 and 14400")
    job = await session.scalar(
        select(GenerationJob).where(GenerationJob.id == job_id).with_for_update()
    )
    if job is None:
        raise CollectionError("generation job was not found")
    _require_lease(job, worker_id=worker_id, now=processed_at)
    assets = list(
        (
            await session.scalars(
                select(Asset)
                .where(
                    Asset.generation_job_id == job.id,
                    Asset.kind == AssetKind.RAW_MASTER,
                )
                .order_by(Asset.output_index)
            )
        ).all()
    )
    indices = [asset.output_index for asset in assets]
    if len(assets) != job.expected_output_count or indices != list(
        range(job.expected_output_count)
    ):
        return await _fail_closed(
            session,
            job_id=job.id,
            worker_id=worker_id,
            error_code="asset_contract_mismatch",
            now=processed_at,
        )
    asset_ids = [asset.id for asset in assets]
    upload_grant_started_at = await session.scalar(
        select(
            func.max(
                func.coalesce(
                    GenerationAttempt.submit_started_at,
                    GenerationAttempt.submitted_at,
                )
            )
        ).where(
            GenerationAttempt.job_id == job.id,
            GenerationAttempt.state == GenerationAttemptState.SUCCEEDED,
        )
    )
    # submit_started_at is durably recorded immediately before upload grants are
    # issued. One collection interval avoids expiring slightly ahead of the URL.
    upload_deadline = (
        _as_utc(upload_grant_started_at)
        + timedelta(seconds=upload_grant_ttl_seconds + retry_delay_seconds)
        if upload_grant_started_at is not None
        else None
    )
    await session.rollback()

    finalized = 0
    try:
        for asset_id in asset_ids:
            result = await finalize_raw_master(
                session,
                store,
                asset_id=asset_id,
                max_bytes=max_image_bytes,
                verification_lease_seconds=verification_lease_seconds,
                actor=worker_id,
            )
            finalized += int(not result.replayed)
    except UploadNotReadyError:
        await session.rollback()
        if upload_deadline is not None and processed_at >= upload_deadline:
            return await _fail_closed(
                session,
                job_id=job_id,
                worker_id=worker_id,
                error_code="master_upload_expired",
                now=processed_at,
            )
        return await _reschedule(
            session,
            job_id=job_id,
            worker_id=worker_id,
            retry_at=processed_at + timedelta(seconds=retry_delay_seconds),
            error_code="master_not_ready",
            now=processed_at,
        )
    except (AssetBusyError, AssetStorageUnavailableError):
        await session.rollback()
        return await _reschedule(
            session,
            job_id=job_id,
            worker_id=worker_id,
            retry_at=processed_at + timedelta(seconds=retry_delay_seconds),
            error_code="master_not_ready",
            now=processed_at,
        )
    except AssetQuarantinedError:
        await session.rollback()
        return await _fail_closed(
            session,
            job_id=job_id,
            worker_id=worker_id,
            error_code="master_quarantined",
            now=processed_at,
        )

    row = (
        await session.execute(
            select(GenerationJob, Release)
            .join(ReleaseVersion, ReleaseVersion.id == GenerationJob.release_version_id)
            .join(Release, Release.id == ReleaseVersion.release_id)
            .where(GenerationJob.id == job_id)
            .with_for_update()
        )
    ).one_or_none()
    if row is None:
        raise CollectionError("generation job was not found")
    job, release = row
    _require_lease(job, worker_id=worker_id, now=processed_at)
    available_count = int(
        await session.scalar(
            select(func.count())
            .select_from(Asset)
            .where(
                Asset.generation_job_id == job.id,
                Asset.kind == AssetKind.RAW_MASTER,
                Asset.state == AssetState.AVAILABLE,
            )
        )
        or 0
    )
    if available_count != job.expected_output_count:
        return await _reschedule(
            session,
            job_id=job.id,
            worker_id=worker_id,
            retry_at=processed_at + timedelta(seconds=retry_delay_seconds),
            error_code="master_verification_incomplete",
            now=processed_at,
        )

    job.state = GenerationState.SUCCEEDED
    job.retry_at = None
    job.lease_owner = None
    job.lease_expires_at = None
    job.last_error_code = None
    job.last_error_detail = None
    job.lock_version += 1
    session.add(
        AuditEvent(
            actor=worker_id,
            action="generation_job.masters_collected",
            resource_type="generation_job",
            resource_id=job.id,
            correlation_id=str(job.id),
            detail={"available_asset_count": available_count},
            occurred_at=processed_at,
        )
    )
    remaining_jobs = int(
        await session.scalar(
            select(func.count())
            .select_from(GenerationJob)
            .where(
                GenerationJob.release_version_id == job.release_version_id,
                GenerationJob.state != GenerationState.SUCCEEDED,
                GenerationJob.id != job.id,
            )
        )
        or 0
    )
    if remaining_jobs == 0 and release.phase == ReleasePhase.GENERATING:
        release.phase = ReleasePhase.REVIEWING
        release.health = ResourceHealth.HEALTHY
        release.lock_version += 1
    await session.commit()
    return CollectionResult(
        job_id=job.id,
        state=job.state,
        finalized_assets=finalized,
    )
