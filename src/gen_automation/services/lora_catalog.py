"""Transactional persistence services for managed LoRAs and import jobs.

Storage and Civitai I/O intentionally live elsewhere.  These functions own the
durable state machine, authorization, idempotency, exact-version attachment,
content deduplication, and append-only audit trail.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from gen_automation.db.models import (
    AdminUser,
    AuditEvent,
    IdempotencyRecord,
    LoraImportJob,
    ManagedLoraArtifact,
    ModelArtifactApproval,
)
from gen_automation.domain.canonical import canonical_sha256
from gen_automation.domain.enums import (
    AdminRole,
    ApprovalStatus,
    LoraImportJobState,
    LoraImportSource,
    ManagedLoraLifecycle,
    ModelArtifactKind,
)
from gen_automation.domain.ids import uuid7
from gen_automation.domain.lora_catalog import (
    CIVITAI_COMMERCIAL_USE_OVERRIDE_METADATA_KEY,
    CIVITAI_COMMERCIAL_USE_OVERRIDE_SCHEMA,
    LORA_MODEL_FAMILY_METADATA_KEY,
    CivitaiLoraImportCreate,
    LoraDependencySummary,
    ManualLoraImportCreate,
    ManualUploadCompletion,
    VerifiedLoraArtifact,
    validate_lora_durable_metadata,
)

_IDEMPOTENCY_KEY_MAX_LENGTH = 200
_SAFE_ERROR_DETAIL_MAX_LENGTH = 1_000
_SAFE_WORKER_ID_MAX_LENGTH = 200
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_S3_BUCKET_PATTERN = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_MUTATING_ROLES = frozenset({AdminRole.OWNER, AdminRole.ADMIN})
_STATIC_DUPLICATE_CODE = "already_available_static"
_STATIC_DUPLICATE_DETAIL = (
    "This LoRA is already available through the protected static worker manifest."
)
_OWNER_CANCELLABLE_STATES = frozenset(
    {
        LoraImportJobState.AWAITING_UPLOAD,
        LoraImportJobState.QUEUED,
        LoraImportJobState.RETRY_WAIT,
        LoraImportJobState.FAILED,
    }
)

LoraDependencySummaryHook = Callable[
    [AsyncSession, UUID],
    Awaitable[LoraDependencySummary],
]


class LoraCatalogError(Exception):
    """Base error for managed LoRA persistence."""


class LoraCatalogInputError(LoraCatalogError, ValueError):
    pass


class LoraCatalogNotFoundError(LoraCatalogError):
    pass


class LoraCatalogConflictError(LoraCatalogError):
    pass


@dataclass(frozen=True, slots=True)
class LoraImportJobSnapshot:
    job_id: UUID
    source_type: LoraImportSource
    state: LoraImportJobState
    display_name: str
    canonical_source_url: str
    license_url: str
    civitai_model_id: int | None
    civitai_version_id: int | None
    civitai_file_id: int | None
    staging_bucket: str | None
    staging_object_key: str | None
    staging_object_version_id: str | None
    staging_object_etag: str | None
    staging_byte_size: int | None
    target_filename: str
    expected_sha256: str | None
    expected_byte_size: int | None
    expected_metadata: dict[str, Any]
    commercial_use_override_attested: bool
    trigger_words: tuple[str, ...]
    progress_bytes: int
    total_bytes: int | None
    attempts: int
    max_attempts: int
    available_at: datetime
    lease_expires_at: datetime | None
    last_error_code: str | None
    last_error_detail: str | None
    result_artifact_id: UUID | None
    requested_by_user_id: UUID
    lock_version: int
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    last_progress_at: datetime | None
    completed_at: datetime | None


@dataclass(frozen=True, slots=True)
class ManagedLoraArtifactSnapshot:
    artifact_id: UUID
    artifact_sha256: str
    display_name: str
    source_type: LoraImportSource
    canonical_source_url: str
    license_url: str
    civitai_model_id: int | None
    civitai_version_id: int | None
    civitai_file_id: int | None
    provenance: dict[str, Any]
    storage_bucket: str
    object_key: str
    object_version_id: str
    object_etag: str
    byte_size: int
    target_filename: str
    approval_id: UUID
    trigger_words: tuple[str, ...]
    lifecycle: ManagedLoraLifecycle
    purge_requested: bool
    registered_by_user_id: UUID
    lock_version: int
    created_at: datetime
    updated_at: datetime
    activated_at: datetime | None
    retirement_requested_at: datetime | None
    retired_at: datetime | None
    restored_at: datetime | None
    purged_at: datetime | None
    lifecycle_error_code: str | None
    lifecycle_error_detail: str | None
    lifecycle_error_count: int
    lifecycle_retry_at: datetime | None


@dataclass(frozen=True, slots=True)
class LoraImportMutationResult:
    job: LoraImportJobSnapshot
    changed: bool
    replayed: bool


@dataclass(frozen=True, slots=True)
class ManagedLoraMutationResult:
    artifact: ManagedLoraArtifactSnapshot
    dependencies: LoraDependencySummary
    changed: bool
    replayed: bool


@dataclass(frozen=True, slots=True)
class LoraImportClaim:
    job: LoraImportJobSnapshot
    worker_id: str
    attempt: int


async def create_manual_import_job(
    session: AsyncSession,
    *,
    command: ManualLoraImportCreate,
    model_bucket: str,
    actor_user_id: UUID,
    idempotency_key: str,
    max_attempts: int = 3,
    now: datetime | None = None,
) -> LoraImportMutationResult:
    """Create a manual import with a server-derived, unguessable quarantine key."""

    created_at = _as_utc(now or datetime.now(UTC))
    bucket = _model_bucket(model_bucket)
    attempts_limit = _max_attempts(max_attempts)
    key = _idempotency_key(idempotency_key)
    request_sha256 = _request_sha256(
        action="create_manual",
        actor_user_id=actor_user_id,
        command={
            **command.model_dump(mode="json"),
            "model_bucket": bucket,
            "max_attempts": attempts_limit,
        },
    )
    replay = await _idempotency_replay(
        session,
        scope=_scope("create_manual"),
        key=key,
        request_sha256=request_sha256,
    )
    if replay is not None:
        return await _job_replay_result(session, replay)
    await _require_actor(session, actor_user_id)

    job_id = uuid7()
    job = LoraImportJob(
        id=job_id,
        source_type=LoraImportSource.MANUAL,
        state=LoraImportJobState.AWAITING_UPLOAD,
        display_name=command.display_name,
        canonical_source_url=str(command.canonical_source_url),
        license_url=str(command.license_url),
        commercial_use_attested=True,
        adult_use_attested=True,
        civitai_model_id=None,
        civitai_version_id=None,
        civitai_file_id=None,
        staging_bucket=bucket,
        staging_object_key=f"onboarding/loras/{job_id}/source.safetensors",
        staging_object_version_id=None,
        staging_object_etag=None,
        staging_byte_size=None,
        target_filename=command.target_filename,
        expected_sha256=command.expected_sha256,
        expected_byte_size=command.expected_byte_size,
        expected_metadata=_import_expected_metadata(command),
        trigger_words=command.trigger_words,
        progress_bytes=0,
        total_bytes=command.expected_byte_size,
        attempts=0,
        max_attempts=attempts_limit,
        available_at=created_at,
        lease_owner=None,
        lease_expires_at=None,
        last_error_code=None,
        last_error_detail=None,
        result_artifact_id=None,
        requested_by_user_id=actor_user_id,
        cancelled_by_user_id=None,
        lock_version=1,
        started_at=None,
        last_progress_at=None,
        completed_at=None,
        created_at=created_at,
        updated_at=created_at,
    )
    session.add(job)
    await _record_owner_mutation(
        session,
        scope=_scope("create_manual"),
        key=key,
        request_sha256=request_sha256,
        actor_user_id=actor_user_id,
        action="lora.import.manual_created",
        resource_type="lora_import_job",
        resource_id=job.id,
        changed=True,
        detail={
            "source_type": LoraImportSource.MANUAL.value,
            "staging_object_key": job.staging_object_key,
            "expected_sha256_present": command.expected_sha256 is not None,
            "expected_byte_size": command.expected_byte_size,
        },
        now=created_at,
    )
    return LoraImportMutationResult(job=_job_snapshot(job), changed=True, replayed=False)


async def create_civitai_import_job(
    session: AsyncSession,
    *,
    command: CivitaiLoraImportCreate,
    actor_user_id: UUID,
    idempotency_key: str,
    max_attempts: int = 3,
    now: datetime | None = None,
) -> LoraImportMutationResult:
    created_at = _as_utc(now or datetime.now(UTC))
    attempts_limit = _max_attempts(max_attempts)
    key = _idempotency_key(idempotency_key)
    idempotency_command = command.model_dump(mode="json")
    if not command.commercial_use_override_attested:
        # Preserve the v1 hash produced before this optional flag existed so a
        # lost-response retry can safely cross the deployment boundary.
        idempotency_command.pop("commercial_use_override_attested", None)
    request_sha256 = _request_sha256(
        action="create_civitai",
        actor_user_id=actor_user_id,
        command={
            **idempotency_command,
            "max_attempts": attempts_limit,
        },
    )
    replay = await _idempotency_replay(
        session,
        scope=_scope("create_civitai"),
        key=key,
        request_sha256=request_sha256,
    )
    if replay is not None:
        return await _job_replay_result(session, replay)
    await _require_actor(session, actor_user_id)
    if CIVITAI_COMMERCIAL_USE_OVERRIDE_METADATA_KEY in command.expected_metadata:
        raise LoraCatalogInputError("Civitai commercial-use override metadata is server-managed")
    expected_metadata = _import_expected_metadata(command)
    if command.commercial_use_override_attested:
        expected_metadata[CIVITAI_COMMERCIAL_USE_OVERRIDE_METADATA_KEY] = {
            "schema": CIVITAI_COMMERCIAL_USE_OVERRIDE_SCHEMA,
            "attested": True,
            "attested_by_user_id": str(actor_user_id),
            "attested_at": created_at.isoformat(),
        }
    try:
        validate_lora_durable_metadata(expected_metadata)
    except ValueError as error:
        raise LoraCatalogInputError(str(error)) from error
    job = LoraImportJob(
        source_type=LoraImportSource.CIVITAI,
        state=LoraImportJobState.QUEUED,
        display_name=command.display_name,
        canonical_source_url=str(command.canonical_source_url),
        license_url=str(command.license_url),
        commercial_use_attested=True,
        adult_use_attested=True,
        civitai_model_id=command.civitai_model_id,
        civitai_version_id=command.civitai_version_id,
        civitai_file_id=command.civitai_file_id,
        staging_bucket=None,
        staging_object_key=None,
        staging_object_version_id=None,
        staging_object_etag=None,
        staging_byte_size=None,
        target_filename=command.target_filename,
        expected_sha256=command.expected_sha256,
        expected_byte_size=command.expected_byte_size,
        expected_metadata=expected_metadata,
        trigger_words=command.trigger_words,
        progress_bytes=0,
        total_bytes=command.expected_byte_size,
        attempts=0,
        max_attempts=attempts_limit,
        available_at=created_at,
        lease_owner=None,
        lease_expires_at=None,
        last_error_code=None,
        last_error_detail=None,
        result_artifact_id=None,
        requested_by_user_id=actor_user_id,
        cancelled_by_user_id=None,
        lock_version=1,
        started_at=None,
        last_progress_at=None,
        completed_at=None,
        created_at=created_at,
        updated_at=created_at,
    )
    session.add(job)
    await session.flush()
    await _record_owner_mutation(
        session,
        scope=_scope("create_civitai"),
        key=key,
        request_sha256=request_sha256,
        actor_user_id=actor_user_id,
        action="lora.import.civitai_created",
        resource_type="lora_import_job",
        resource_id=job.id,
        changed=True,
        detail={
            "source_type": LoraImportSource.CIVITAI.value,
            "civitai_model_id": command.civitai_model_id,
            "civitai_version_id": command.civitai_version_id,
            "civitai_file_id": command.civitai_file_id,
            "commercial_use_override_attested": (command.commercial_use_override_attested),
        },
        now=created_at,
    )
    return LoraImportMutationResult(job=_job_snapshot(job), changed=True, replayed=False)


def _import_expected_metadata(
    command: ManualLoraImportCreate | CivitaiLoraImportCreate,
) -> dict[str, Any]:
    metadata = dict(command.expected_metadata)
    metadata[LORA_MODEL_FAMILY_METADATA_KEY] = command.model_family.value
    return metadata


async def mark_manual_upload_complete(
    session: AsyncSession,
    *,
    job_id: UUID,
    completion: ManualUploadCompletion,
    expected_lock_version: int,
    actor_user_id: UUID,
    idempotency_key: str,
    now: datetime | None = None,
) -> LoraImportMutationResult:
    completed_at = _as_utc(now or datetime.now(UTC))
    expected_version = _lock_version(expected_lock_version)
    key = _idempotency_key(idempotency_key)
    request_sha256 = _request_sha256(
        action="manual_upload_complete",
        actor_user_id=actor_user_id,
        command={
            "job_id": str(job_id),
            "expected_lock_version": expected_version,
            **completion.model_dump(mode="json"),
        },
    )
    replay = await _idempotency_replay(
        session,
        scope=_scope("manual_upload_complete"),
        key=key,
        request_sha256=request_sha256,
    )
    if replay is not None:
        return await _job_replay_result(session, replay)
    await _require_actor(session, actor_user_id)
    job = await _locked_job(session, job_id)
    _require_expected_lock(job.lock_version, expected_version)
    if job.source_type != LoraImportSource.MANUAL:
        raise LoraCatalogConflictError("only a manual import can accept an upload version")
    if job.state != LoraImportJobState.AWAITING_UPLOAD:
        raise LoraCatalogConflictError("manual import is no longer awaiting an upload")
    if (
        job.staging_object_version_id is not None
        or job.staging_object_etag is not None
        or job.staging_byte_size is not None
    ):
        raise LoraCatalogConflictError("manual import already has a frozen upload version")
    if job.expected_byte_size is not None and job.expected_byte_size != completion.byte_size:
        raise LoraCatalogConflictError("uploaded object size does not match the expected size")

    job.staging_object_version_id = completion.object_version_id
    job.staging_object_etag = completion.object_etag
    job.staging_byte_size = completion.byte_size
    job.total_bytes = completion.byte_size
    job.state = LoraImportJobState.QUEUED
    job.available_at = completed_at
    job.lock_version += 1
    job.updated_at = completed_at
    await _record_owner_mutation(
        session,
        scope=_scope("manual_upload_complete"),
        key=key,
        request_sha256=request_sha256,
        actor_user_id=actor_user_id,
        action="lora.import.manual_upload_frozen",
        resource_type="lora_import_job",
        resource_id=job.id,
        changed=True,
        detail={
            "object_version_id": completion.object_version_id,
            "object_etag": completion.object_etag,
            "byte_size": completion.byte_size,
        },
        now=completed_at,
    )
    return LoraImportMutationResult(job=_job_snapshot(job), changed=True, replayed=False)


async def get_lora_import_job(
    session: AsyncSession,
    *,
    job_id: UUID,
    actor_user_id: UUID,
) -> LoraImportJobSnapshot:
    await _require_actor(session, actor_user_id, lock=False)
    job = await session.get(LoraImportJob, job_id)
    if job is None:
        raise LoraCatalogNotFoundError("LoRA import job was not found")
    return _job_snapshot(job)


async def list_lora_import_jobs(
    session: AsyncSession,
    *,
    actor_user_id: UUID,
    limit: int = 100,
) -> tuple[LoraImportJobSnapshot, ...]:
    await _require_actor(session, actor_user_id, lock=False)
    bounded_limit = _limit(limit)
    rows = (
        await session.scalars(
            select(LoraImportJob)
            .order_by(LoraImportJob.created_at.desc(), LoraImportJob.id.desc())
            .limit(bounded_limit)
        )
    ).all()
    return tuple(_job_snapshot(row) for row in rows)


async def get_managed_lora(
    session: AsyncSession,
    *,
    artifact_id: UUID,
    actor_user_id: UUID,
) -> ManagedLoraArtifactSnapshot:
    await _require_actor(session, actor_user_id, lock=False)
    artifact = await session.get(ManagedLoraArtifact, artifact_id)
    if artifact is None:
        raise LoraCatalogNotFoundError("managed LoRA was not found")
    return _artifact_snapshot(artifact)


async def list_managed_loras(
    session: AsyncSession,
    *,
    actor_user_id: UUID,
    limit: int | None = 100,
) -> tuple[ManagedLoraArtifactSnapshot, ...]:
    await _require_actor(session, actor_user_id, lock=False)
    statement = select(ManagedLoraArtifact).order_by(
        ManagedLoraArtifact.created_at.desc(),
        ManagedLoraArtifact.id.desc(),
    )
    if limit is not None:
        statement = statement.limit(_limit(limit))
    rows = (await session.scalars(statement)).all()
    return tuple(_artifact_snapshot(row) for row in rows)


async def retry_lora_import_job(
    session: AsyncSession,
    *,
    job_id: UUID,
    expected_lock_version: int,
    actor_user_id: UUID,
    idempotency_key: str,
    now: datetime | None = None,
) -> LoraImportMutationResult:
    retried_at = _as_utc(now or datetime.now(UTC))
    return await _mutate_job_owner_action(
        session,
        action="retry",
        job_id=job_id,
        expected_lock_version=expected_lock_version,
        actor_user_id=actor_user_id,
        idempotency_key=idempotency_key,
        now=retried_at,
    )


async def cancel_lora_import_job(
    session: AsyncSession,
    *,
    job_id: UUID,
    expected_lock_version: int,
    actor_user_id: UUID,
    idempotency_key: str,
    now: datetime | None = None,
) -> LoraImportMutationResult:
    cancelled_at = _as_utc(now or datetime.now(UTC))
    return await _mutate_job_owner_action(
        session,
        action="cancel",
        job_id=job_id,
        expected_lock_version=expected_lock_version,
        actor_user_id=actor_user_id,
        idempotency_key=idempotency_key,
        now=cancelled_at,
    )


async def retire_managed_lora(
    session: AsyncSession,
    *,
    artifact_id: UUID,
    expected_lock_version: int,
    purge_requested: bool,
    dependency_summary_hook: LoraDependencySummaryHook,
    actor_user_id: UUID,
    idempotency_key: str,
    now: datetime | None = None,
) -> ManagedLoraMutationResult:
    requested_at = _as_utc(now or datetime.now(UTC))
    return await _mutate_managed_lora(
        session,
        action="retire",
        artifact_id=artifact_id,
        expected_lock_version=expected_lock_version,
        purge_requested=purge_requested,
        dependency_summary_hook=dependency_summary_hook,
        actor_user_id=actor_user_id,
        idempotency_key=idempotency_key,
        now=requested_at,
    )


async def restore_managed_lora(
    session: AsyncSession,
    *,
    artifact_id: UUID,
    expected_lock_version: int,
    dependency_summary_hook: LoraDependencySummaryHook,
    actor_user_id: UUID,
    idempotency_key: str,
    now: datetime | None = None,
) -> ManagedLoraMutationResult:
    restored_at = _as_utc(now or datetime.now(UTC))
    return await _mutate_managed_lora(
        session,
        action="restore",
        artifact_id=artifact_id,
        expected_lock_version=expected_lock_version,
        purge_requested=False,
        dependency_summary_hook=dependency_summary_hook,
        actor_user_id=actor_user_id,
        idempotency_key=idempotency_key,
        now=restored_at,
    )


async def mark_managed_lora_active(
    session: AsyncSession,
    *,
    artifact_id: UUID,
    expected_lock_version: int,
    worker_id: str,
    idempotency_key: str,
    now: datetime | None = None,
) -> ManagedLoraMutationResult:
    """Record a verified provider manifest activation under an exact lock."""

    activated_at = _as_utc(now or datetime.now(UTC))
    return await _runtime_lifecycle_transition(
        session,
        action="activate",
        artifact_id=artifact_id,
        expected_lock_version=expected_lock_version,
        worker_id=worker_id,
        idempotency_key=idempotency_key,
        dependency_summary_hook=None,
        deleted_object_key=None,
        deleted_object_version_id=None,
        now=activated_at,
    )


async def mark_managed_lora_retired(
    session: AsyncSession,
    *,
    artifact_id: UUID,
    expected_lock_version: int,
    dependency_summary_hook: LoraDependencySummaryHook,
    worker_id: str,
    idempotency_key: str,
    now: datetime | None = None,
) -> ManagedLoraMutationResult:
    """Finish retirement only after the runtime dependency hook is empty."""

    retired_at = _as_utc(now or datetime.now(UTC))
    return await _runtime_lifecycle_transition(
        session,
        action="finish_retirement",
        artifact_id=artifact_id,
        expected_lock_version=expected_lock_version,
        worker_id=worker_id,
        idempotency_key=idempotency_key,
        dependency_summary_hook=dependency_summary_hook,
        deleted_object_key=None,
        deleted_object_version_id=None,
        now=retired_at,
    )


async def mark_managed_lora_purged(
    session: AsyncSession,
    *,
    artifact_id: UUID,
    expected_lock_version: int,
    deleted_object_key: str,
    deleted_object_version_id: str,
    worker_id: str,
    idempotency_key: str,
    now: datetime | None = None,
) -> ManagedLoraMutationResult:
    """Record purge only after the caller deleted the exact frozen S3 version."""

    purged_at = _as_utc(now or datetime.now(UTC))
    return await _runtime_lifecycle_transition(
        session,
        action="purge",
        artifact_id=artifact_id,
        expected_lock_version=expected_lock_version,
        worker_id=worker_id,
        idempotency_key=idempotency_key,
        dependency_summary_hook=None,
        deleted_object_key=deleted_object_key,
        deleted_object_version_id=deleted_object_version_id,
        now=purged_at,
    )


async def claim_next_lora_import_job(
    session: AsyncSession,
    *,
    worker_id: str,
    lease_seconds: int,
    now: datetime | None = None,
) -> LoraImportClaim | None:
    """Internal lease primitive for a future bounded storage/provider worker."""

    claimed_at = _as_utc(now or datetime.now(UTC))
    normalized_worker = _worker_id(worker_id)
    if not 30 <= lease_seconds <= 3_600:
        raise LoraCatalogInputError("LoRA import lease must be between 30 and 3600 seconds")
    due = or_(
        and_(
            LoraImportJob.state.in_((LoraImportJobState.QUEUED, LoraImportJobState.RETRY_WAIT)),
            LoraImportJob.available_at <= claimed_at,
        ),
        and_(
            LoraImportJob.state == LoraImportJobState.CLAIMED,
            LoraImportJob.lease_expires_at.is_not(None),
            LoraImportJob.lease_expires_at <= claimed_at,
        ),
    )
    job = await session.scalar(
        select(LoraImportJob)
        .where(due, LoraImportJob.attempts < LoraImportJob.max_attempts)
        .order_by(LoraImportJob.available_at, LoraImportJob.created_at, LoraImportJob.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if job is None:
        await session.rollback()
        return None
    job.state = LoraImportJobState.CLAIMED
    job.attempts += 1
    job.lease_owner = normalized_worker
    job.lease_expires_at = claimed_at + timedelta(seconds=lease_seconds)
    job.started_at = job.started_at or claimed_at
    job.completed_at = None
    job.last_error_code = None
    job.last_error_detail = None
    job.lock_version += 1
    job.updated_at = claimed_at
    session.add(
        AuditEvent(
            actor=normalized_worker,
            action="lora.import.claimed",
            resource_type="lora_import_job",
            resource_id=job.id,
            correlation_id=f"claim:{job.id}:{job.attempts}",
            detail={"attempt": job.attempts},
            occurred_at=claimed_at,
        )
    )
    await session.commit()
    return LoraImportClaim(
        job=_job_snapshot(job),
        worker_id=normalized_worker,
        attempt=job.attempts,
    )


async def recover_exhausted_lora_import_lease(
    session: AsyncSession,
    *,
    worker_id: str,
    now: datetime | None = None,
) -> LoraImportJobSnapshot | None:
    """Terminalize one expired final-attempt lease after a worker crash."""

    recovered_at = _as_utc(now or datetime.now(UTC))
    normalized_worker = _worker_id(worker_id)
    job = await session.scalar(
        select(LoraImportJob)
        .where(
            LoraImportJob.state == LoraImportJobState.CLAIMED,
            LoraImportJob.lease_expires_at.is_not(None),
            LoraImportJob.lease_expires_at <= recovered_at,
            LoraImportJob.attempts >= LoraImportJob.max_attempts,
        )
        .order_by(LoraImportJob.lease_expires_at, LoraImportJob.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if job is None:
        await session.rollback()
        return None
    exhausted_attempt = job.attempts
    job.state = LoraImportJobState.FAILED
    job.lease_owner = None
    job.lease_expires_at = None
    job.last_error_code = "lora_import_attempts_exhausted"
    job.last_error_detail = "The final import worker lease expired before completion."
    job.completed_at = recovered_at
    job.lock_version += 1
    job.updated_at = recovered_at
    session.add(
        AuditEvent(
            actor=normalized_worker,
            action="lora.import.failed",
            resource_type="lora_import_job",
            resource_id=job.id,
            correlation_id=f"recover:{job.id}:{exhausted_attempt}",
            detail={
                "attempt": exhausted_attempt,
                "error_code": "lora_import_attempts_exhausted",
                "recovered_expired_lease": True,
            },
            occurred_at=recovered_at,
        )
    )
    await session.commit()
    return _job_snapshot(job)


async def heartbeat_lora_import_job(
    session: AsyncSession,
    *,
    job_id: UUID,
    worker_id: str,
    expected_attempt: int,
    progress_bytes: int,
    lease_seconds: int,
    now: datetime | None = None,
) -> LoraImportJobSnapshot:
    """Persist bounded transfer progress and renew the exact claimed lease."""

    progressed_at = _as_utc(now or datetime.now(UTC))
    normalized_worker = _worker_id(worker_id)
    if progress_bytes < 0:
        raise LoraCatalogInputError("LoRA import progress cannot be negative")
    if not 30 <= lease_seconds <= 3_600:
        raise LoraCatalogInputError("LoRA import lease must be between 30 and 3600 seconds")
    job = await _locked_job(session, job_id)
    _require_claim(job, worker_id=normalized_worker, expected_attempt=expected_attempt)
    if progress_bytes < job.progress_bytes:
        raise LoraCatalogConflictError("LoRA import progress cannot move backwards")
    if job.total_bytes is not None and progress_bytes > job.total_bytes:
        if job.source_type == LoraImportSource.CIVITAI:
            # Provider sizeKB is advisory. Preserve honest progress and grow
            # the UI estimate while SHA-256 and the 4-GiB stream bound remain
            # the Civitai integrity/security anchors.
            job.total_bytes = progress_bytes
        else:
            raise LoraCatalogConflictError("LoRA import progress exceeds the frozen size")
    job.progress_bytes = progress_bytes
    job.last_progress_at = progressed_at
    job.lease_expires_at = progressed_at + timedelta(seconds=lease_seconds)
    job.lock_version += 1
    job.updated_at = progressed_at
    await session.commit()
    return _job_snapshot(job)


async def complete_lora_import_job(
    session: AsyncSession,
    *,
    job_id: UUID,
    verified: VerifiedLoraArtifact,
    worker_id: str,
    expected_attempt: int,
    now: datetime | None = None,
) -> LoraImportMutationResult:
    """Atomically attach an approval, managed object identity, and job result.

    The caller may have inserted and flushed the compliance approval in the same
    transaction immediately before this call.  This function validates that
    approval and commits it together with the catalog row and completed job.
    """

    completed_at = _as_utc(now or datetime.now(UTC))
    normalized_worker = _worker_id(worker_id)
    if expected_attempt <= 0:
        raise LoraCatalogInputError("expected LoRA import attempt must be positive")
    await session.flush()
    job = await _locked_job(session, job_id)
    _require_claim(job, worker_id=normalized_worker, expected_attempt=expected_attempt)
    _validate_verified_job(job, verified)
    existing = await session.scalar(
        select(ManagedLoraArtifact)
        .where(
            ManagedLoraArtifact.artifact_sha256 == verified.artifact_sha256,
            ManagedLoraArtifact.lifecycle != ManagedLoraLifecycle.PURGED,
        )
        .with_for_update()
    )
    if existing is None:
        approval = await session.scalar(
            select(ModelArtifactApproval)
            .where(ModelArtifactApproval.id == verified.approval_id)
            .with_for_update(read=True)
        )
        _validate_lora_approval(job, verified, approval)
        artifact = ManagedLoraArtifact(
            artifact_sha256=verified.artifact_sha256,
            display_name=job.display_name,
            source_type=job.source_type,
            canonical_source_url=job.canonical_source_url,
            license_url=job.license_url,
            civitai_model_id=job.civitai_model_id,
            civitai_version_id=job.civitai_version_id,
            civitai_file_id=job.civitai_file_id,
            provenance={
                "schema": "managed-lora-provenance/v1",
                "expected": job.expected_metadata,
                "verified": verified.provenance,
            },
            storage_bucket=verified.storage_bucket,
            object_key=verified.object_key,
            object_version_id=verified.object_version_id,
            object_etag=verified.object_etag,
            byte_size=verified.byte_size,
            target_filename=_runtime_target_filename(verified.artifact_sha256),
            approval_id=verified.approval_id,
            trigger_words=job.trigger_words,
            lifecycle=ManagedLoraLifecycle.PENDING_ACTIVATION,
            purge_requested=False,
            registered_by_user_id=job.requested_by_user_id,
            retirement_requested_by_user_id=None,
            restored_by_user_id=None,
            activated_at=None,
            retirement_requested_at=None,
            retired_at=None,
            restored_at=None,
            purged_at=None,
            lock_version=1,
            created_at=completed_at,
            updated_at=completed_at,
        )
        session.add(artifact)
        await session.flush()
        terminal_state = LoraImportJobState.COMPLETED
        session.add(
            AuditEvent(
                actor=normalized_worker,
                action="lora.artifact.registered",
                resource_type="managed_lora_artifact",
                resource_id=artifact.id,
                correlation_id=f"import:{job.id}:{expected_attempt}",
                detail={
                    "artifact_sha256": artifact.artifact_sha256,
                    "approval_id": str(artifact.approval_id),
                    "object_version_id": artifact.object_version_id,
                },
                occurred_at=completed_at,
            )
        )
    else:
        artifact = existing
        approval = await session.scalar(
            select(ModelArtifactApproval)
            .where(ModelArtifactApproval.id == existing.approval_id)
            .with_for_update(read=True)
        )
        _validate_managed_duplicate(verified, artifact=existing, approval=approval)
        terminal_state = LoraImportJobState.DUPLICATE

    job.state = terminal_state
    job.result_artifact_id = artifact.id
    job.progress_bytes = verified.byte_size
    job.total_bytes = verified.byte_size
    job.lease_owner = None
    job.lease_expires_at = None
    job.last_error_code = None
    job.last_error_detail = None
    job.last_progress_at = completed_at
    job.completed_at = completed_at
    job.lock_version += 1
    job.updated_at = completed_at
    session.add(
        AuditEvent(
            actor=normalized_worker,
            action=(
                "lora.import.completed"
                if terminal_state == LoraImportJobState.COMPLETED
                else "lora.import.duplicate"
            ),
            resource_type="lora_import_job",
            resource_id=job.id,
            correlation_id=f"import:{job.id}:{expected_attempt}",
            detail={
                "attempt": expected_attempt,
                "artifact_id": str(artifact.id),
                "artifact_sha256": artifact.artifact_sha256,
            },
            occurred_at=completed_at,
        )
    )
    try:
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        raise LoraCatalogConflictError(
            "verified LoRA conflicts with an existing catalog identity"
        ) from error
    return LoraImportMutationResult(job=_job_snapshot(job), changed=True, replayed=False)


async def complete_static_lora_import_duplicate(
    session: AsyncSession,
    *,
    job_id: UUID,
    artifact_sha256: str,
    approval_id: UUID,
    worker_id: str,
    expected_attempt: int,
    now: datetime | None = None,
) -> LoraImportMutationResult:
    """Finish against an approved static-manifest LoRA without making it purgeable."""

    completed_at = _as_utc(now or datetime.now(UTC))
    normalized_worker = _worker_id(worker_id)
    if _SHA256_PATTERN.fullmatch(artifact_sha256) is None:
        raise LoraCatalogInputError("static duplicate SHA-256 is invalid")
    if expected_attempt <= 0:
        raise LoraCatalogInputError("expected LoRA import attempt must be positive")
    job = await _locked_job(session, job_id)
    _require_claim(job, worker_id=normalized_worker, expected_attempt=expected_attempt)
    if job.expected_sha256 is not None and job.expected_sha256 != artifact_sha256:
        raise LoraCatalogConflictError("static LoRA hash does not match the expected hash")
    managed = await session.scalar(
        select(ManagedLoraArtifact)
        .where(
            ManagedLoraArtifact.artifact_sha256 == artifact_sha256,
            ManagedLoraArtifact.lifecycle != ManagedLoraLifecycle.PURGED,
        )
        .with_for_update(read=True)
    )
    if managed is not None:
        raise LoraCatalogConflictError(
            "managed LoRA duplicates must reference the managed catalog row"
        )
    approval = await session.scalar(
        select(ModelArtifactApproval)
        .where(ModelArtifactApproval.id == approval_id)
        .with_for_update(read=True)
    )
    if (
        approval is None
        or approval.status != ApprovalStatus.APPROVED
        or not approval.is_current
        or approval.kind != ModelArtifactKind.LORA
        or approval.artifact_sha256 != artifact_sha256
        or not approval.commercial_use_approved
        or not approval.adult_use_approved
        or not approval.safetensors_verified
        or approval.revoked_at is not None
        or approval.revoked_by_user_id is not None
    ):
        raise LoraCatalogConflictError(
            "static duplicate does not have a matching current compliance approval"
        )

    job.state = LoraImportJobState.DUPLICATE
    job.result_artifact_id = None
    job.progress_bytes = job.total_bytes or 0
    job.lease_owner = None
    job.lease_expires_at = None
    job.last_error_code = _STATIC_DUPLICATE_CODE
    job.last_error_detail = _STATIC_DUPLICATE_DETAIL
    job.last_progress_at = completed_at
    job.completed_at = completed_at
    job.lock_version += 1
    job.updated_at = completed_at
    session.add(
        AuditEvent(
            actor=normalized_worker,
            action="lora.import.already_available_static",
            resource_type="lora_import_job",
            resource_id=job.id,
            correlation_id=f"import:{job.id}:{expected_attempt}",
            detail={
                "attempt": expected_attempt,
                "artifact_sha256": artifact_sha256,
                "approval_id": str(approval_id),
            },
            occurred_at=completed_at,
        )
    )
    await session.commit()
    return LoraImportMutationResult(job=_job_snapshot(job), changed=True, replayed=False)


async def fail_lora_import_job(
    session: AsyncSession,
    *,
    job_id: UUID,
    worker_id: str,
    expected_attempt: int,
    error_code: str,
    error_detail: str,
    retryable: bool,
    retry_at: datetime | None = None,
    now: datetime | None = None,
) -> LoraImportJobSnapshot:
    failed_at = _as_utc(now or datetime.now(UTC))
    normalized_worker = _worker_id(worker_id)
    code = _safe_error_code(error_code)
    detail = _safe_error_detail(error_detail)
    job = await _locked_job(session, job_id)
    _require_claim(job, worker_id=normalized_worker, expected_attempt=expected_attempt)
    should_retry = retryable and job.attempts < job.max_attempts
    job.state = LoraImportJobState.RETRY_WAIT if should_retry else LoraImportJobState.FAILED
    job.available_at = _as_utc(retry_at) if should_retry and retry_at is not None else failed_at
    if should_retry and job.available_at < failed_at:
        raise LoraCatalogInputError("LoRA retry time cannot be in the past")
    job.lease_owner = None
    job.lease_expires_at = None
    job.last_error_code = code
    job.last_error_detail = detail
    job.completed_at = None if should_retry else failed_at
    job.lock_version += 1
    job.updated_at = failed_at
    session.add(
        AuditEvent(
            actor=normalized_worker,
            action=("lora.import.retry_wait" if should_retry else "lora.import.failed"),
            resource_type="lora_import_job",
            resource_id=job.id,
            correlation_id=f"import:{job.id}:{expected_attempt}",
            detail={"attempt": expected_attempt, "error_code": code},
            occurred_at=failed_at,
        )
    )
    await session.commit()
    return _job_snapshot(job)


async def _mutate_job_owner_action(
    session: AsyncSession,
    *,
    action: str,
    job_id: UUID,
    expected_lock_version: int,
    actor_user_id: UUID,
    idempotency_key: str,
    now: datetime,
) -> LoraImportMutationResult:
    expected_version = _lock_version(expected_lock_version)
    key = _idempotency_key(idempotency_key)
    request_sha256 = _request_sha256(
        action=action,
        actor_user_id=actor_user_id,
        command={"job_id": str(job_id), "expected_lock_version": expected_version},
    )
    replay = await _idempotency_replay(
        session,
        scope=_scope(action),
        key=key,
        request_sha256=request_sha256,
    )
    if replay is not None:
        return await _job_replay_result(session, replay)
    await _require_actor(session, actor_user_id)
    job = await _locked_job(session, job_id)
    _require_expected_lock(job.lock_version, expected_version)
    changed = True
    detail: dict[str, Any]
    if action == "retry":
        if job.state != LoraImportJobState.FAILED:
            raise LoraCatalogConflictError("only a failed LoRA import can be retried")
        if job.attempts >= job.max_attempts:
            raise LoraCatalogConflictError("LoRA import exhausted its retry limit")
        previous_error_code = job.last_error_code
        job.state = LoraImportJobState.QUEUED
        job.available_at = now
        job.last_error_code = None
        job.last_error_detail = None
        job.completed_at = None
        detail = {"previous_error_code": previous_error_code}
    elif action == "cancel":
        if job.state not in _OWNER_CANCELLABLE_STATES:
            raise LoraCatalogConflictError("LoRA import cannot be cancelled in its current state")
        job.state = LoraImportJobState.CANCELLED
        job.cancelled_by_user_id = actor_user_id
        job.completed_at = now
        detail = {"previous_error_code": job.last_error_code}
    else:
        raise AssertionError("unsupported LoRA job mutation")
    job.lock_version += 1
    job.updated_at = now
    await _record_owner_mutation(
        session,
        scope=_scope(action),
        key=key,
        request_sha256=request_sha256,
        actor_user_id=actor_user_id,
        action=f"lora.import.{action}ed" if action == "retry" else "lora.import.cancelled",
        resource_type="lora_import_job",
        resource_id=job.id,
        changed=changed,
        detail=detail,
        now=now,
    )
    return LoraImportMutationResult(job=_job_snapshot(job), changed=changed, replayed=False)


async def _mutate_managed_lora(
    session: AsyncSession,
    *,
    action: str,
    artifact_id: UUID,
    expected_lock_version: int,
    purge_requested: bool,
    dependency_summary_hook: LoraDependencySummaryHook,
    actor_user_id: UUID,
    idempotency_key: str,
    now: datetime,
) -> ManagedLoraMutationResult:
    expected_version = _lock_version(expected_lock_version)
    key = _idempotency_key(idempotency_key)
    request_sha256 = _request_sha256(
        action=action,
        actor_user_id=actor_user_id,
        command={
            "artifact_id": str(artifact_id),
            "expected_lock_version": expected_version,
            "purge_requested": purge_requested,
        },
    )
    replay = await _idempotency_replay(
        session,
        scope=_scope(action),
        key=key,
        request_sha256=request_sha256,
    )
    if replay is not None:
        return await _artifact_replay_result(session, replay)
    await _require_actor(session, actor_user_id)
    artifact = await session.scalar(
        select(ManagedLoraArtifact).where(ManagedLoraArtifact.id == artifact_id).with_for_update()
    )
    if artifact is None:
        raise LoraCatalogNotFoundError("managed LoRA was not found")
    _require_expected_lock(artifact.lock_version, expected_version)
    dependencies = await dependency_summary_hook(session, artifact.id)
    if not isinstance(dependencies, LoraDependencySummary):
        raise LoraCatalogConflictError("LoRA dependency hook returned an invalid summary")

    changed = False
    if action == "retire":
        if artifact.lifecycle == ManagedLoraLifecycle.PURGED:
            raise LoraCatalogConflictError("a purged LoRA cannot be retired again")
        if artifact.lifecycle in {
            ManagedLoraLifecycle.PENDING_ACTIVATION,
            ManagedLoraLifecycle.ACTIVE,
        }:
            artifact.lifecycle = ManagedLoraLifecycle.RETIRING
            artifact.retirement_requested_at = now
            artifact.retirement_requested_by_user_id = actor_user_id
            changed = True
        if purge_requested and not artifact.purge_requested:
            artifact.purge_requested = True
            changed = True
    elif action == "restore":
        if artifact.lifecycle == ManagedLoraLifecycle.PURGED:
            raise LoraCatalogConflictError("a purged LoRA cannot be restored")
        if artifact.lifecycle == ManagedLoraLifecycle.RETIRING:
            artifact.lifecycle = (
                ManagedLoraLifecycle.ACTIVE
                if artifact.activated_at is not None and artifact.retired_at is None
                else ManagedLoraLifecycle.PENDING_ACTIVATION
            )
            artifact.restored_at = now
            artifact.restored_by_user_id = actor_user_id
            changed = True
        elif artifact.lifecycle == ManagedLoraLifecycle.RETIRED:
            artifact.lifecycle = ManagedLoraLifecycle.PENDING_ACTIVATION
            artifact.restored_at = now
            artifact.restored_by_user_id = actor_user_id
            changed = True
        if artifact.purge_requested:
            artifact.purge_requested = False
            changed = True
    else:
        raise AssertionError("unsupported managed LoRA mutation")
    if changed:
        artifact.lifecycle_error_code = None
        artifact.lifecycle_error_detail = None
        artifact.lifecycle_error_count = 0
        artifact.lifecycle_retry_at = None
        artifact.lock_version += 1
        artifact.updated_at = now

    await _record_owner_mutation(
        session,
        scope=_scope(action),
        key=key,
        request_sha256=request_sha256,
        actor_user_id=actor_user_id,
        action=(
            f"lora.artifact.{action}_requested" if action == "retire" else "lora.artifact.restored"
        ),
        resource_type="managed_lora_artifact",
        resource_id=artifact.id,
        changed=changed,
        detail={
            "lifecycle": artifact.lifecycle.value,
            "purge_requested": artifact.purge_requested,
            "dependencies": dependencies.model_dump(mode="json"),
        },
        now=now,
        extra_response={"dependencies": dependencies.model_dump(mode="json")},
    )
    return ManagedLoraMutationResult(
        artifact=_artifact_snapshot(artifact),
        dependencies=dependencies,
        changed=changed,
        replayed=False,
    )


async def _runtime_lifecycle_transition(
    session: AsyncSession,
    *,
    action: str,
    artifact_id: UUID,
    expected_lock_version: int,
    worker_id: str,
    idempotency_key: str,
    dependency_summary_hook: LoraDependencySummaryHook | None,
    deleted_object_key: str | None,
    deleted_object_version_id: str | None,
    now: datetime,
) -> ManagedLoraMutationResult:
    expected_version = _lock_version(expected_lock_version)
    normalized_worker = _worker_id(worker_id)
    key = _idempotency_key(idempotency_key)
    request_sha256 = _system_request_sha256(
        action=action,
        worker_id=normalized_worker,
        command={
            "artifact_id": str(artifact_id),
            "expected_lock_version": expected_version,
            "deleted_object_key": deleted_object_key,
            "deleted_object_version_id": deleted_object_version_id,
        },
    )
    replay = await _idempotency_replay(
        session,
        scope=_scope(action),
        key=key,
        request_sha256=request_sha256,
    )
    if replay is not None:
        return await _artifact_replay_result(session, replay)
    artifact = await session.scalar(
        select(ManagedLoraArtifact).where(ManagedLoraArtifact.id == artifact_id).with_for_update()
    )
    if artifact is None:
        raise LoraCatalogNotFoundError("managed LoRA was not found")
    _require_expected_lock(artifact.lock_version, expected_version)
    dependencies = LoraDependencySummary()

    if action == "activate":
        if artifact.lifecycle != ManagedLoraLifecycle.PENDING_ACTIVATION:
            raise LoraCatalogConflictError("only a pending LoRA can become active")
        approval = await session.scalar(
            select(ModelArtifactApproval)
            .where(ModelArtifactApproval.id == artifact.approval_id)
            .with_for_update(read=True)
        )
        if (
            approval is None
            or approval.status != ApprovalStatus.APPROVED
            or not approval.is_current
            or approval.kind != ModelArtifactKind.LORA
            or approval.artifact_sha256 != artifact.artifact_sha256
            or approval.revoked_at is not None
        ):
            raise LoraCatalogConflictError("pending LoRA approval is no longer current")
        artifact.lifecycle = ManagedLoraLifecycle.ACTIVE
        artifact.activated_at = now
        artifact.retired_at = None
        audit_action = "lora.artifact.activated"
    elif action == "finish_retirement":
        if artifact.lifecycle != ManagedLoraLifecycle.RETIRING:
            raise LoraCatalogConflictError("only a retiring LoRA can become retired")
        if dependency_summary_hook is None:
            raise LoraCatalogConflictError("retirement dependency hook is required")
        dependencies = await dependency_summary_hook(session, artifact.id)
        if not isinstance(dependencies, LoraDependencySummary):
            raise LoraCatalogConflictError("LoRA dependency hook returned an invalid summary")
        if dependencies.has_dependencies:
            raise LoraCatalogConflictError("LoRA still has runtime dependencies")
        artifact.lifecycle = ManagedLoraLifecycle.RETIRED
        artifact.retired_at = now
        audit_action = "lora.artifact.retired"
    elif action == "purge":
        if artifact.lifecycle != ManagedLoraLifecycle.RETIRED:
            raise LoraCatalogConflictError("only a retired LoRA can become purged")
        if not artifact.purge_requested:
            raise LoraCatalogConflictError("LoRA purge was not requested by an administrator")
        if (
            deleted_object_key != artifact.object_key
            or deleted_object_version_id != artifact.object_version_id
        ):
            raise LoraCatalogConflictError("purge proof does not match the exact LoRA version")
        artifact.lifecycle = ManagedLoraLifecycle.PURGED
        artifact.purged_at = now
        audit_action = "lora.artifact.purged"
    else:
        raise AssertionError("unsupported LoRA runtime transition")

    artifact.lifecycle_error_code = None
    artifact.lifecycle_error_detail = None
    artifact.lifecycle_error_count = 0
    artifact.lifecycle_retry_at = None
    artifact.lock_version += 1
    artifact.updated_at = now
    await _record_system_mutation(
        session,
        scope=_scope(action),
        key=key,
        request_sha256=request_sha256,
        worker_id=normalized_worker,
        action=audit_action,
        artifact=artifact,
        dependencies=dependencies,
        now=now,
    )
    return ManagedLoraMutationResult(
        artifact=_artifact_snapshot(artifact),
        dependencies=dependencies,
        changed=True,
        replayed=False,
    )


async def _record_system_mutation(
    session: AsyncSession,
    *,
    scope: str,
    key: str,
    request_sha256: str,
    worker_id: str,
    action: str,
    artifact: ManagedLoraArtifact,
    dependencies: LoraDependencySummary,
    now: datetime,
) -> None:
    session.add(
        IdempotencyRecord(
            scope=scope,
            idempotency_key=key,
            request_sha256=request_sha256,
            response_status=200,
            response_body={
                "schema": "lora-catalog-mutation/v1",
                "resource_type": "managed_lora_artifact",
                "resource_id": str(artifact.id),
                "changed": True,
                "dependencies": dependencies.model_dump(mode="json"),
            },
            created_at=now,
            expires_at=None,
        )
    )
    session.add(
        AuditEvent(
            actor=worker_id,
            action=action,
            resource_type="managed_lora_artifact",
            resource_id=artifact.id,
            correlation_id=key,
            detail={
                "lifecycle": artifact.lifecycle.value,
                "lock_version": artifact.lock_version,
                "object_key": artifact.object_key,
                "object_version_id": artifact.object_version_id,
                "dependencies": dependencies.model_dump(mode="json"),
            },
            occurred_at=now,
        )
    )
    try:
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        replay = await _idempotency_replay(
            session,
            scope=scope,
            key=key,
            request_sha256=request_sha256,
        )
        if replay is not None:
            raise LoraCatalogConflictError(
                "an identical LoRA transition completed concurrently; retry the request"
            ) from error
        raise LoraCatalogConflictError("LoRA runtime transition conflicts") from error


async def _record_owner_mutation(
    session: AsyncSession,
    *,
    scope: str,
    key: str,
    request_sha256: str,
    actor_user_id: UUID,
    action: str,
    resource_type: str,
    resource_id: UUID,
    changed: bool,
    detail: dict[str, Any],
    now: datetime,
    extra_response: dict[str, Any] | None = None,
) -> None:
    response_body: dict[str, Any] = {
        "schema": "lora-catalog-mutation/v1",
        "resource_type": resource_type,
        "resource_id": str(resource_id),
        "changed": changed,
    }
    if extra_response:
        response_body.update(extra_response)
    session.add(
        IdempotencyRecord(
            scope=scope,
            idempotency_key=key,
            request_sha256=request_sha256,
            response_status=200,
            response_body=response_body,
            created_at=now,
            expires_at=None,
        )
    )
    session.add(
        AuditEvent(
            actor=str(actor_user_id),
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            correlation_id=key,
            detail={"changed": changed, **detail},
            occurred_at=now,
        )
    )
    try:
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        replay = await _idempotency_replay(
            session,
            scope=scope,
            key=key,
            request_sha256=request_sha256,
        )
        if replay is not None:
            # A concurrent identical request won.  The public caller performs
            # the preflight replay path on its next invocation.
            raise LoraCatalogConflictError(
                "an identical LoRA mutation completed concurrently; retry the request"
            ) from error
        raise LoraCatalogConflictError("LoRA mutation conflicts with current state") from error


async def _idempotency_replay(
    session: AsyncSession,
    *,
    scope: str,
    key: str,
    request_sha256: str,
) -> dict[str, Any] | None:
    record = await session.scalar(
        select(IdempotencyRecord).where(
            IdempotencyRecord.scope == scope,
            IdempotencyRecord.idempotency_key == key,
        )
    )
    if record is None:
        return None
    if record.request_sha256 != request_sha256:
        raise LoraCatalogConflictError("idempotency key was already used for another request")
    body = record.response_body
    if (
        not isinstance(body, dict)
        or body.get("schema") != "lora-catalog-mutation/v1"
        or body.get("resource_type") not in {"lora_import_job", "managed_lora_artifact"}
        or not isinstance(body.get("resource_id"), str)
        or not isinstance(body.get("changed"), bool)
    ):
        raise LoraCatalogConflictError("LoRA idempotency record is invalid")
    try:
        UUID(body["resource_id"])
    except (TypeError, ValueError):
        raise LoraCatalogConflictError("LoRA idempotency record is invalid") from None
    return body


async def _job_replay_result(
    session: AsyncSession,
    body: dict[str, Any],
) -> LoraImportMutationResult:
    if body["resource_type"] != "lora_import_job":
        raise LoraCatalogConflictError("LoRA idempotency resource type is invalid")
    job = await session.get(LoraImportJob, UUID(body["resource_id"]))
    if job is None:
        raise LoraCatalogConflictError("LoRA idempotency job no longer exists")
    return LoraImportMutationResult(
        job=_job_snapshot(job),
        changed=bool(body["changed"]),
        replayed=True,
    )


async def _artifact_replay_result(
    session: AsyncSession,
    body: dict[str, Any],
) -> ManagedLoraMutationResult:
    if body["resource_type"] != "managed_lora_artifact":
        raise LoraCatalogConflictError("LoRA idempotency resource type is invalid")
    artifact = await session.get(ManagedLoraArtifact, UUID(body["resource_id"]))
    if artifact is None:
        raise LoraCatalogConflictError("LoRA idempotency artifact no longer exists")
    try:
        dependencies = LoraDependencySummary.model_validate(body.get("dependencies", {}))
    except ValueError:
        raise LoraCatalogConflictError("LoRA idempotency dependency summary is invalid") from None
    return ManagedLoraMutationResult(
        artifact=_artifact_snapshot(artifact),
        dependencies=dependencies,
        changed=bool(body["changed"]),
        replayed=True,
    )


async def _require_actor(
    session: AsyncSession,
    actor_user_id: UUID,
    *,
    lock: bool = True,
) -> AdminUser:
    statement = select(AdminUser).where(
        AdminUser.id == actor_user_id,
        AdminUser.is_active.is_(True),
    )
    if lock:
        statement = statement.with_for_update(read=True)
    actor = await session.scalar(statement)
    if actor is None or actor.role not in _MUTATING_ROLES:
        raise LoraCatalogConflictError("LoRA actor is not an active owner or administrator")
    return actor


async def _locked_job(session: AsyncSession, job_id: UUID) -> LoraImportJob:
    job = await session.scalar(
        select(LoraImportJob).where(LoraImportJob.id == job_id).with_for_update()
    )
    if job is None:
        raise LoraCatalogNotFoundError("LoRA import job was not found")
    return job


def _validate_verified_job(job: LoraImportJob, verified: VerifiedLoraArtifact) -> None:
    if job.expected_sha256 is not None and job.expected_sha256 != verified.artifact_sha256:
        raise LoraCatalogConflictError("verified LoRA hash does not match the expected hash")
    if (
        job.source_type == LoraImportSource.MANUAL
        and job.expected_byte_size is not None
        and job.expected_byte_size != verified.byte_size
    ):
        raise LoraCatalogConflictError("verified LoRA size does not match the expected size")
    if job.staging_byte_size is not None and job.staging_byte_size != verified.byte_size:
        raise LoraCatalogConflictError("verified LoRA size does not match the frozen upload")


def _runtime_target_filename(artifact_sha256: str) -> str:
    """Give every managed artifact a collision-free filename inside ComfyUI."""

    return f"managed-{artifact_sha256}.safetensors"


def _validate_lora_approval(
    job: LoraImportJob,
    verified: VerifiedLoraArtifact,
    approval: ModelArtifactApproval | None,
) -> None:
    if (
        approval is None
        or approval.status != ApprovalStatus.APPROVED
        or not approval.is_current
        or approval.kind != ModelArtifactKind.LORA
        or approval.artifact_sha256 != verified.artifact_sha256
        or approval.storage_key != verified.object_key
        or approval.source_url != job.canonical_source_url
        or approval.license_url != job.license_url
        or not approval.commercial_use_approved
        or not approval.adult_use_approved
        or not approval.safetensors_verified
        or approval.revoked_at is not None
        or approval.revoked_by_user_id is not None
    ):
        raise LoraCatalogConflictError(
            "verified LoRA does not have a matching current compliance approval"
        )


def _validate_managed_duplicate(
    verified: VerifiedLoraArtifact,
    *,
    artifact: ManagedLoraArtifact,
    approval: ModelArtifactApproval | None,
) -> None:
    if (
        verified.approval_id != artifact.approval_id
        or verified.storage_bucket != artifact.storage_bucket
        or verified.object_key != artifact.object_key
        or verified.object_version_id != artifact.object_version_id
        or verified.object_etag != artifact.object_etag
        or verified.byte_size != artifact.byte_size
        or approval is None
        or approval.status != ApprovalStatus.APPROVED
        or not approval.is_current
        or approval.kind != ModelArtifactKind.LORA
        or approval.artifact_sha256 != artifact.artifact_sha256
        or approval.storage_key != artifact.object_key
        or approval.revoked_at is not None
        or approval.revoked_by_user_id is not None
    ):
        raise LoraCatalogConflictError(
            "duplicate LoRA does not match the existing managed catalog identity"
        )


def _require_claim(job: LoraImportJob, *, worker_id: str, expected_attempt: int) -> None:
    if (
        job.state != LoraImportJobState.CLAIMED
        or job.lease_owner != worker_id
        or job.attempts != expected_attempt
        or job.lease_expires_at is None
    ):
        raise LoraCatalogConflictError("LoRA import lease is stale or owned by another worker")


def _job_snapshot(job: LoraImportJob) -> LoraImportJobSnapshot:
    return LoraImportJobSnapshot(
        job_id=UUID(str(job.id)),
        source_type=job.source_type,
        state=job.state,
        display_name=job.display_name,
        canonical_source_url=job.canonical_source_url,
        license_url=job.license_url,
        civitai_model_id=job.civitai_model_id,
        civitai_version_id=job.civitai_version_id,
        civitai_file_id=job.civitai_file_id,
        staging_bucket=job.staging_bucket,
        staging_object_key=job.staging_object_key,
        staging_object_version_id=job.staging_object_version_id,
        staging_object_etag=job.staging_object_etag,
        staging_byte_size=job.staging_byte_size,
        target_filename=job.target_filename,
        expected_sha256=job.expected_sha256,
        expected_byte_size=job.expected_byte_size,
        expected_metadata=dict(job.expected_metadata),
        commercial_use_override_attested=_commercial_use_override_attested(job),
        trigger_words=tuple(job.trigger_words),
        progress_bytes=job.progress_bytes,
        total_bytes=job.total_bytes,
        attempts=job.attempts,
        max_attempts=job.max_attempts,
        available_at=_as_utc(job.available_at),
        lease_expires_at=_optional_utc(job.lease_expires_at),
        last_error_code=job.last_error_code,
        last_error_detail=job.last_error_detail,
        result_artifact_id=(
            UUID(str(job.result_artifact_id)) if job.result_artifact_id is not None else None
        ),
        requested_by_user_id=UUID(str(job.requested_by_user_id)),
        lock_version=job.lock_version,
        created_at=_as_utc(job.created_at),
        updated_at=_as_utc(job.updated_at),
        started_at=_optional_utc(job.started_at),
        last_progress_at=_optional_utc(job.last_progress_at),
        completed_at=_optional_utc(job.completed_at),
    )


def _commercial_use_override_attested(job: LoraImportJob) -> bool:
    marker = job.expected_metadata.get(CIVITAI_COMMERCIAL_USE_OVERRIDE_METADATA_KEY)
    return bool(
        job.source_type == LoraImportSource.CIVITAI
        and isinstance(marker, dict)
        and marker.get("schema") == CIVITAI_COMMERCIAL_USE_OVERRIDE_SCHEMA
        and marker.get("attested") is True
        and marker.get("attested_by_user_id") == str(job.requested_by_user_id)
        and isinstance(marker.get("attested_at"), str)
        and bool(marker["attested_at"])
    )


def _artifact_snapshot(artifact: ManagedLoraArtifact) -> ManagedLoraArtifactSnapshot:
    return ManagedLoraArtifactSnapshot(
        artifact_id=UUID(str(artifact.id)),
        artifact_sha256=artifact.artifact_sha256,
        display_name=artifact.display_name,
        source_type=artifact.source_type,
        canonical_source_url=artifact.canonical_source_url,
        license_url=artifact.license_url,
        civitai_model_id=artifact.civitai_model_id,
        civitai_version_id=artifact.civitai_version_id,
        civitai_file_id=artifact.civitai_file_id,
        provenance=dict(artifact.provenance),
        storage_bucket=artifact.storage_bucket,
        object_key=artifact.object_key,
        object_version_id=artifact.object_version_id,
        object_etag=artifact.object_etag,
        byte_size=artifact.byte_size,
        target_filename=artifact.target_filename,
        approval_id=UUID(str(artifact.approval_id)),
        trigger_words=tuple(artifact.trigger_words),
        lifecycle=artifact.lifecycle,
        purge_requested=artifact.purge_requested,
        registered_by_user_id=UUID(str(artifact.registered_by_user_id)),
        lock_version=artifact.lock_version,
        created_at=_as_utc(artifact.created_at),
        updated_at=_as_utc(artifact.updated_at),
        activated_at=_optional_utc(artifact.activated_at),
        retirement_requested_at=_optional_utc(artifact.retirement_requested_at),
        retired_at=_optional_utc(artifact.retired_at),
        restored_at=_optional_utc(artifact.restored_at),
        purged_at=_optional_utc(artifact.purged_at),
        lifecycle_error_code=artifact.lifecycle_error_code,
        lifecycle_error_detail=artifact.lifecycle_error_detail,
        lifecycle_error_count=artifact.lifecycle_error_count,
        lifecycle_retry_at=_optional_utc(artifact.lifecycle_retry_at),
    )


def _request_sha256(*, action: str, actor_user_id: UUID, command: dict[str, Any]) -> str:
    return canonical_sha256(
        {
            "schema": "lora-catalog-command/v1",
            "action": action,
            "actor_user_id": str(actor_user_id),
            "command": command,
        }
    )


def _system_request_sha256(
    *,
    action: str,
    worker_id: str,
    command: dict[str, Any],
) -> str:
    return canonical_sha256(
        {
            "schema": "lora-catalog-system-command/v1",
            "action": action,
            "worker_id": worker_id,
            "command": command,
        }
    )


def _scope(action: str) -> str:
    return f"lora-catalog:{action}:v1"


def _idempotency_key(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > _IDEMPOTENCY_KEY_MAX_LENGTH
        or any(ord(character) < 32 for character in value)
    ):
        raise LoraCatalogInputError("idempotency key must be 1 to 200 visible characters")
    return value


def _worker_id(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > _SAFE_WORKER_ID_MAX_LENGTH
        or any(ord(character) < 32 for character in value)
    ):
        raise LoraCatalogInputError("LoRA worker id must be trimmed visible text")
    return value


def _model_bucket(value: str) -> str:
    if not isinstance(value, str) or _S3_BUCKET_PATTERN.fullmatch(value) is None:
        raise LoraCatalogInputError("configured LoRA model bucket is invalid")
    return value


def _lock_version(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise LoraCatalogInputError("expected LoRA lock version must be positive")
    return value


def _require_expected_lock(actual: int, expected: int) -> None:
    if actual != expected:
        raise LoraCatalogConflictError("LoRA resource lock version is stale")


def _max_attempts(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 10:
        raise LoraCatalogInputError("LoRA import max attempts must be between 1 and 10")
    return value


def _limit(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 200:
        raise LoraCatalogInputError("LoRA list limit must be between 1 and 200")
    return value


def _safe_error_code(value: str) -> str:
    if re.fullmatch(r"[a-z][a-z0-9_]{0,99}", value) is None:
        raise LoraCatalogInputError("LoRA import error code is invalid")
    return value


def _safe_error_detail(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > _SAFE_ERROR_DETAIL_MAX_LENGTH
        or any(ord(character) < 32 and character not in "\n\t" for character in value)
    ):
        raise LoraCatalogInputError("LoRA import error detail is invalid")
    return value


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _optional_utc(value: datetime | None) -> datetime | None:
    return _as_utc(value) if value is not None else None
