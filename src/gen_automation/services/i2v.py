"""Transactional services for the fresh image-to-video queue."""

from collections.abc import Collection
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import case, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from gen_automation.db.models import (
    I2VAttempt,
    I2VInput,
    I2VJob,
    I2VOutput,
    I2VPreset,
    I2VWorkerDeployment,
)
from gen_automation.domain.canonical import canonical_sha256
from gen_automation.domain.i2v import (
    I2VAttemptSnapshot,
    I2VAttemptState,
    I2VClaim,
    I2VInputRegistration,
    I2VInputSnapshot,
    I2VInputSource,
    I2VJobDraft,
    I2VJobSnapshot,
    I2VJobState,
    I2VOutputRegistration,
    I2VOutputSnapshot,
    I2VPresetDraft,
    I2VPresetSnapshot,
    I2VWorkerDeploymentRegistration,
    I2VWorkerDeploymentSnapshot,
    I2VWorkerDeploymentState,
)
from gen_automation.domain.i2v_loras import (
    I2VLoraPromptError,
    I2VLoraSelectionError,
    I2VLoraSettingsKind,
    classify_i2v_lora_settings,
    normalize_i2v_settings,
    validate_i2v_lora_prompt,
)
from gen_automation.domain.ids import uuid7


class I2VError(Exception):
    """Base error for the isolated I2V application service."""


class I2VInputError(I2VError):
    """A command is malformed."""


class I2VNotFoundError(I2VError):
    """A requested I2V resource does not exist."""


class I2VConflictError(I2VError):
    """A command conflicts with the current durable state."""


async def register_i2v_input(
    session: AsyncSession,
    *,
    actor_user_id: UUID,
    registration: I2VInputRegistration,
    now: datetime | None = None,
) -> I2VInputSnapshot:
    timestamp = _now(now)
    if registration.source == I2VInputSource.GENERATION and registration.asset_id is None:
        raise I2VInputError("a generation input must identify its source asset")

    identity = (
        I2VInput.storage_backend == registration.storage_backend,
        I2VInput.storage_bucket == registration.storage_bucket,
        I2VInput.object_key == registration.object_key,
        (
            I2VInput.object_version_id.is_(None)
            if registration.object_version_id is None
            else I2VInput.object_version_id == registration.object_version_id
        ),
    )
    existing = await session.scalar(select(I2VInput).where(*identity).with_for_update())
    if existing is not None:
        if (
            existing.created_by_user_id != actor_user_id
            or existing.sha256 != registration.sha256
            or existing.content_type != registration.content_type
            or existing.width != registration.width
            or existing.height != registration.height
            or existing.byte_size != registration.byte_size
        ):
            raise I2VConflictError("the input object was already registered differently")
        snapshot = _input_snapshot(existing)
        await session.commit()
        return snapshot

    record = I2VInput(
        id=uuid7(),
        created_by_user_id=actor_user_id,
        source=registration.source,
        asset_id=registration.asset_id,
        display_name=_nonempty_text(registration.display_name, "input display name"),
        storage_backend=_nonempty_text(registration.storage_backend, "storage backend"),
        storage_bucket=_nonempty_text(registration.storage_bucket, "storage bucket"),
        object_key=_nonempty_text(registration.object_key, "object key"),
        object_version_id=registration.object_version_id,
        sha256=registration.sha256,
        content_type=registration.content_type,
        width=registration.width,
        height=registration.height,
        byte_size=registration.byte_size,
        input_metadata=dict(registration.metadata),
        created_at=timestamp,
    )
    session.add(record)
    await _commit(session, "input registration conflicts with existing state")
    return _input_snapshot(record)


async def create_i2v_preset(
    session: AsyncSession,
    *,
    actor_user_id: UUID,
    draft: I2VPresetDraft,
    now: datetime | None = None,
) -> I2VPresetSnapshot:
    timestamp = _now(now)
    preset = I2VPreset(
        id=uuid7(),
        created_by_user_id=actor_user_id,
        name=_nonempty_text(draft.name, "preset name"),
        description=draft.description,
        positive_prompt=draft.positive_prompt,
        negative_prompt=draft.negative_prompt,
        settings=_normalized_settings(draft.settings),
        lock_version=1,
        created_at=timestamp,
        updated_at=timestamp,
    )
    session.add(preset)
    await _commit(session, "a preset with this name already exists")
    return _preset_snapshot(preset)


async def update_i2v_preset(
    session: AsyncSession,
    *,
    actor_user_id: UUID,
    preset_id: UUID,
    draft: I2VPresetDraft,
    expected_lock_version: int,
    now: datetime | None = None,
) -> I2VPresetSnapshot:
    preset = await _owned_preset(session, actor_user_id, preset_id, lock=True)
    if expected_lock_version <= 0 or preset.lock_version != expected_lock_version:
        raise I2VConflictError("the preset was changed by another request")
    preset.name = _nonempty_text(draft.name, "preset name")
    preset.description = draft.description
    preset.positive_prompt = draft.positive_prompt
    preset.negative_prompt = draft.negative_prompt
    preset.settings = _normalized_settings(draft.settings)
    preset.lock_version += 1
    preset.updated_at = _now(now)
    await _commit(session, "the preset update conflicts with existing state")
    return _preset_snapshot(preset)


async def delete_i2v_preset(
    session: AsyncSession,
    *,
    actor_user_id: UUID,
    preset_id: UUID,
) -> None:
    preset = await _owned_preset(session, actor_user_id, preset_id, lock=True)
    await session.delete(preset)
    await _commit(session, "the preset could not be deleted from the current state")


async def list_i2v_presets(
    session: AsyncSession,
    *,
    actor_user_id: UUID,
) -> tuple[I2VPresetSnapshot, ...]:
    presets = (
        await session.scalars(
            select(I2VPreset)
            .where(I2VPreset.created_by_user_id == actor_user_id)
            .order_by(I2VPreset.name, I2VPreset.id)
        )
    ).all()
    return tuple(_preset_snapshot(preset) for preset in presets)


async def create_i2v_job(
    session: AsyncSession,
    *,
    actor_user_id: UUID,
    draft: I2VJobDraft,
    now: datetime | None = None,
) -> I2VJobSnapshot:
    timestamp = _now(now)
    await _lock_queue(session)
    input_record = await session.get(I2VInput, draft.input_id)
    if input_record is None or input_record.created_by_user_id != actor_user_id:
        raise I2VNotFoundError("I2V input was not found")

    preset: I2VPreset | None = None
    if draft.preset_id is not None:
        preset = await _owned_preset(session, actor_user_id, draft.preset_id, lock=False)
    positive_prompt = (
        draft.positive_prompt
        if draft.positive_prompt is not None
        else (preset.positive_prompt if preset is not None else "")
    )
    negative_prompt = (
        draft.negative_prompt
        if draft.negative_prompt is not None
        else (preset.negative_prompt if preset is not None else "")
    )
    settings = dict(preset.settings) if preset is not None else {}
    if draft.settings is not None:
        settings.update(draft.settings)
    settings = _normalized_settings(settings)
    try:
        validate_i2v_lora_prompt(positive_prompt, settings)
    except I2VLoraPromptError as error:
        raise I2VInputError(str(error)) from None
    input_snapshot = _input_snapshot(input_record).model_dump(mode="json")
    preset_snapshot = _preset_snapshot(preset).model_dump(mode="json") if preset is not None else {}
    request = {
        "schema": "i2v-request/v1",
        "input": input_snapshot,
        "preset": preset_snapshot,
        "positive_prompt": positive_prompt,
        "negative_prompt": negative_prompt,
        "settings": settings,
    }
    queue_position = await _next_queue_position(session)
    job = I2VJob(
        id=uuid7(),
        created_by_user_id=actor_user_id,
        input_id=input_record.id,
        preset_id=preset.id if preset is not None else None,
        positive_prompt=positive_prompt,
        negative_prompt=negative_prompt,
        input_snapshot=input_snapshot,
        preset_snapshot=preset_snapshot,
        settings_snapshot=settings,
        request_sha256=_canonical_sha256(request),
        state=I2VJobState.QUEUED,
        queue_position=queue_position,
        attempt_count=0,
        lease_owner=None,
        lease_expires_at=None,
        cancel_requested_at=None,
        completed_at=None,
        last_error_code=None,
        last_error_detail=None,
        created_at=timestamp,
        updated_at=timestamp,
    )
    session.add(job)
    await _commit(session, "the queue changed concurrently; retry generation submission")
    return _job_snapshot(job)


async def list_i2v_jobs(
    session: AsyncSession,
    *,
    actor_user_id: UUID | None = None,
    states: Collection[I2VJobState] | None = None,
    limit: int | None = None,
) -> tuple[I2VJobSnapshot, ...]:
    statement = select(I2VJob)
    if actor_user_id is not None:
        statement = statement.where(I2VJob.created_by_user_id == actor_user_id)
    if states is not None:
        normalized_states = tuple(states)
        if not normalized_states:
            return ()
        statement = statement.where(I2VJob.state.in_(normalized_states))
    statement = statement.order_by(
        case((I2VJob.state == I2VJobState.QUEUED, 0), else_=1),
        I2VJob.queue_position,
        I2VJob.created_at.desc(),
        I2VJob.id,
    )
    if limit is not None:
        statement = statement.limit(_positive_limit(limit))
    jobs = (await session.scalars(statement)).all()
    return tuple(_job_snapshot(job) for job in jobs)


async def reorder_i2v_queue(
    session: AsyncSession,
    *,
    actor_user_id: UUID,
    job_id: UUID,
    before_job_id: UUID | None = None,
    after_job_id: UUID | None = None,
    now: datetime | None = None,
) -> tuple[I2VJobSnapshot, ...]:
    if before_job_id is not None and after_job_id is not None:
        raise I2VInputError("choose either a before or after queue anchor")
    if job_id in {before_job_id, after_job_id}:
        raise I2VInputError("a queue item cannot be anchored to itself")
    timestamp = _now(now)
    await _lock_queue(session)
    jobs = await _locked_queue(session)
    moving = next((job for job in jobs if job.id == job_id), None)
    if moving is None or moving.created_by_user_id != actor_user_id:
        raise I2VNotFoundError("queued I2V job was not found")
    ordered = [job for job in jobs if job.id != job_id]
    anchor_id = before_job_id if before_job_id is not None else after_job_id
    if anchor_id is None:
        insertion = len(ordered)
    else:
        anchor = next((job for job in ordered if job.id == anchor_id), None)
        if anchor is None:
            raise I2VNotFoundError("queue anchor was not found")
        insertion = ordered.index(anchor) + (1 if after_job_id is not None else 0)
    ordered.insert(insertion, moving)
    await _write_queue_positions(session, ordered, timestamp)
    for queued_job in ordered:
        await session.refresh(queued_job)
    snapshots = tuple(_job_snapshot(queued_job) for queued_job in ordered)
    await _commit(session, "the queue changed concurrently; reload and retry")
    return snapshots


async def request_i2v_job_cancellation(
    session: AsyncSession,
    *,
    actor_user_id: UUID,
    job_id: UUID,
    now: datetime | None = None,
) -> I2VJobSnapshot:
    timestamp = _now(now)
    await _lock_queue(session)
    job = await _owned_job(session, actor_user_id, job_id, lock=True)
    if job.state == I2VJobState.QUEUED:
        job.state = I2VJobState.CANCELLED
        job.queue_position = None
        job.cancel_requested_at = timestamp
        job.completed_at = timestamp
        await _compact_queue(session, timestamp)
    elif job.state in {I2VJobState.CLAIMED, I2VJobState.RUNNING}:
        job.state = I2VJobState.CANCEL_REQUESTED
        job.cancel_requested_at = timestamp
    elif job.state in {I2VJobState.CANCEL_REQUESTED, I2VJobState.CANCELLED}:
        return _job_snapshot(job)
    else:
        raise I2VConflictError("a completed I2V job cannot be cancelled")
    job.updated_at = timestamp
    await _commit(session, "the job changed concurrently; reload and retry")
    return _job_snapshot(job)


async def retry_i2v_job(
    session: AsyncSession,
    *,
    actor_user_id: UUID,
    job_id: UUID,
    now: datetime | None = None,
) -> I2VJobSnapshot:
    timestamp = _now(now)
    await _lock_queue(session)
    job = await _owned_job(session, actor_user_id, job_id, lock=True)
    if job.state not in {I2VJobState.FAILED, I2VJobState.CANCELLED}:
        raise I2VConflictError("only failed or cancelled I2V jobs can be retried")
    settings = _normalized_settings(dict(job.settings_snapshot))
    try:
        validate_i2v_lora_prompt(job.positive_prompt, settings)
    except I2VLoraPromptError as error:
        raise I2VInputError(str(error)) from None
    queue_position = await _next_queue_position(session)
    job.state = I2VJobState.QUEUED
    job.queue_position = queue_position
    job.lease_owner = None
    job.lease_expires_at = None
    job.cancel_requested_at = None
    job.completed_at = None
    job.last_error_code = None
    job.last_error_detail = None
    job.updated_at = timestamp
    await _commit(session, "the queue changed concurrently; reload and retry")
    return _job_snapshot(job)


async def claim_next_i2v_job(
    session: AsyncSession,
    *,
    worker_id: str,
    lease_duration: timedelta,
    worker_deployment_id: UUID | None = None,
    worker_image_digest: str | None = None,
    reviewed_loras_enabled: bool = False,
    now: datetime | None = None,
) -> I2VClaim | None:
    normalized_worker = _nonempty_text(worker_id, "worker id")
    if lease_duration <= timedelta(0):
        raise I2VInputError("lease duration must be positive")
    timestamp = _now(now)
    await _lock_queue(session)
    queued_jobs = (
        await session.scalars(
            select(I2VJob)
            .where(I2VJob.state == I2VJobState.QUEUED)
            .order_by(I2VJob.queue_position, I2VJob.created_at, I2VJob.id)
            .with_for_update(skip_locked=True)
        )
    ).all()
    job = next(
        (
            queued
            for queued in queued_jobs
            if _dispatch_eligible(
                queued.settings_snapshot,
                reviewed_loras_enabled=reviewed_loras_enabled,
            )
        ),
        None,
    )
    if job is None:
        return None
    job.state = I2VJobState.CLAIMED
    job.queue_position = None
    job.attempt_count += 1
    job.lease_owner = normalized_worker
    job.lease_expires_at = timestamp + lease_duration
    job.updated_at = timestamp
    attempt = I2VAttempt(
        id=uuid7(),
        job_id=job.id,
        worker_deployment_id=worker_deployment_id,
        attempt_no=job.attempt_count,
        state=I2VAttemptState.CREATED,
        worker_id=normalized_worker,
        worker_image_digest=worker_image_digest,
        provider_job_id=None,
        request_metadata={},
        response_metadata={},
        started_at=None,
        completed_at=None,
        error_code=None,
        error_detail=None,
        created_at=timestamp,
        updated_at=timestamp,
    )
    session.add(attempt)
    # A rollback-paused LoRA row must retain its durable queue state. Gaps are
    # valid queue positions and are compacted by the next explicit queue edit.
    if all(
        _dispatch_eligible(
            queued.settings_snapshot,
            reviewed_loras_enabled=reviewed_loras_enabled,
        )
        for queued in queued_jobs
    ):
        await _compact_queue(session, timestamp)
    await _commit(session, "the next I2V job was claimed concurrently")
    return I2VClaim(job=_job_snapshot(job), attempt=_attempt_snapshot(attempt))


async def start_i2v_attempt(
    session: AsyncSession,
    *,
    job_id: UUID,
    attempt_id: UUID,
    worker_id: str,
    provider_job_id: str | None = None,
    request_metadata: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> I2VClaim:
    timestamp = _now(now)
    job, attempt = await _locked_claim(session, job_id, attempt_id, worker_id, timestamp)
    if job.state != I2VJobState.CLAIMED or attempt.state != I2VAttemptState.CREATED:
        raise I2VConflictError("the I2V attempt is not ready to start")
    job.state = I2VJobState.RUNNING
    job.updated_at = timestamp
    attempt.state = I2VAttemptState.RUNNING
    attempt.provider_job_id = provider_job_id
    attempt.request_metadata = dict(request_metadata or {})
    attempt.started_at = timestamp
    attempt.updated_at = timestamp
    await _commit(session, "the I2V attempt changed concurrently")
    return I2VClaim(job=_job_snapshot(job), attempt=_attempt_snapshot(attempt))


async def renew_i2v_job_lease(
    session: AsyncSession,
    *,
    job_id: UUID,
    worker_id: str,
    lease_duration: timedelta,
    now: datetime | None = None,
) -> I2VJobSnapshot:
    if lease_duration <= timedelta(0):
        raise I2VInputError("lease duration must be positive")
    timestamp = _now(now)
    job = await session.scalar(select(I2VJob).where(I2VJob.id == job_id).with_for_update())
    if job is None:
        raise I2VNotFoundError("I2V job was not found")
    _require_worker_claim(job, worker_id, timestamp)
    job.lease_expires_at = timestamp + lease_duration
    job.updated_at = timestamp
    await session.commit()
    return _job_snapshot(job)


async def adopt_validated_i2v_provider_attempt(
    session: AsyncSession,
    *,
    job_id: UUID,
    attempt_id: UUID,
    attempt_no: int,
    provider_job_id: str,
    previous_provider_job_id: str | None,
    previous_worker_id: str,
    worker_id: str,
    lease_duration: timedelta,
    now: datetime | None = None,
) -> I2VClaim:
    """Transfer an exact provider-backed attempt after its remote identity was verified."""

    normalized_provider_job_id = _nonempty_text(provider_job_id, "provider job id")
    normalized_previous_provider_job_id = (
        _nonempty_text(previous_provider_job_id, "previous provider job id")
        if previous_provider_job_id is not None
        else None
    )
    if normalized_previous_provider_job_id not in {None, normalized_provider_job_id}:
        raise I2VConflictError("the provider-backed I2V attempt changed concurrently")
    normalized_previous_worker = _nonempty_text(previous_worker_id, "previous worker id")
    normalized_worker = _nonempty_text(worker_id, "worker id")
    if attempt_no <= 0:
        raise I2VInputError("attempt number must be positive")
    if lease_duration <= timedelta(0):
        raise I2VInputError("lease duration must be positive")
    timestamp = _now(now)
    job = await session.scalar(select(I2VJob).where(I2VJob.id == job_id).with_for_update())
    attempt = await session.scalar(
        select(I2VAttempt)
        .where(
            I2VAttempt.id == attempt_id,
            I2VAttempt.job_id == job_id,
            I2VAttempt.attempt_no == attempt_no,
        )
        .with_for_update()
    )
    if (
        job is None
        or attempt is None
        or job.state not in {I2VJobState.CLAIMED, I2VJobState.RUNNING, I2VJobState.CANCEL_REQUESTED}
        or attempt.state not in {I2VAttemptState.CREATED, I2VAttemptState.RUNNING}
        or job.attempt_count != attempt_no
        or job.lease_owner != normalized_previous_worker
        or attempt.worker_id != normalized_previous_worker
        or attempt.provider_job_id != normalized_previous_provider_job_id
    ):
        raise I2VConflictError("the provider-backed I2V attempt changed concurrently")
    job.lease_owner = normalized_worker
    job.lease_expires_at = timestamp + lease_duration
    job.updated_at = timestamp
    attempt.worker_id = normalized_worker
    attempt.provider_job_id = normalized_provider_job_id
    attempt.updated_at = timestamp
    await _commit(session, "the provider-backed I2V attempt changed concurrently")
    return I2VClaim(job=_job_snapshot(job), attempt=_attempt_snapshot(attempt))


async def fail_i2v_attempt(
    session: AsyncSession,
    *,
    job_id: UUID,
    attempt_id: UUID,
    worker_id: str,
    error_code: str,
    error_detail: str,
    response_metadata: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> I2VJobSnapshot:
    timestamp = _now(now)
    job, attempt = await _locked_claim(session, job_id, attempt_id, worker_id, timestamp)
    if job.state not in {I2VJobState.CLAIMED, I2VJobState.RUNNING}:
        raise I2VConflictError("the I2V job is not running")
    attempt.state = I2VAttemptState.FAILED
    attempt.response_metadata = dict(response_metadata or {})
    attempt.completed_at = timestamp
    attempt.error_code = _nonempty_text(error_code, "error code")
    attempt.error_detail = error_detail
    attempt.updated_at = timestamp
    job.state = I2VJobState.FAILED
    job.lease_owner = None
    job.lease_expires_at = None
    job.completed_at = timestamp
    job.last_error_code = attempt.error_code
    job.last_error_detail = error_detail
    job.updated_at = timestamp
    await _commit(session, "the I2V attempt changed concurrently")
    return _job_snapshot(job)


async def acknowledge_i2v_cancellation(
    session: AsyncSession,
    *,
    job_id: UUID,
    attempt_id: UUID,
    worker_id: str,
    response_metadata: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> I2VJobSnapshot:
    timestamp = _now(now)
    job, attempt = await _locked_claim(session, job_id, attempt_id, worker_id, timestamp)
    if job.state != I2VJobState.CANCEL_REQUESTED:
        raise I2VConflictError("the I2V job has no pending cancellation")
    attempt.state = I2VAttemptState.CANCELLED
    attempt.response_metadata = dict(response_metadata or {})
    attempt.completed_at = timestamp
    attempt.updated_at = timestamp
    job.state = I2VJobState.CANCELLED
    job.lease_owner = None
    job.lease_expires_at = None
    job.completed_at = timestamp
    job.updated_at = timestamp
    await _commit(session, "the I2V cancellation changed concurrently")
    return _job_snapshot(job)


async def complete_i2v_job(
    session: AsyncSession,
    *,
    job_id: UUID,
    attempt_id: UUID,
    worker_id: str,
    output: I2VOutputRegistration,
    response_metadata: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> I2VOutputSnapshot:
    timestamp = _now(now)
    job, attempt = await _locked_claim(session, job_id, attempt_id, worker_id, timestamp)
    if job.state not in {I2VJobState.CLAIMED, I2VJobState.RUNNING}:
        raise I2VConflictError("the I2V job is not running")
    attempt.state = I2VAttemptState.SUCCEEDED
    attempt.response_metadata = dict(response_metadata or {})
    attempt.completed_at = timestamp
    attempt.updated_at = timestamp
    job.state = I2VJobState.SUCCEEDED
    job.lease_owner = None
    job.lease_expires_at = None
    job.completed_at = timestamp
    job.updated_at = timestamp
    record = I2VOutput(
        id=uuid7(),
        job_id=job.id,
        attempt_id=attempt.id,
        storage_backend=_nonempty_text(output.storage_backend, "storage backend"),
        storage_bucket=_nonempty_text(output.storage_bucket, "storage bucket"),
        object_key=_nonempty_text(output.object_key, "object key"),
        object_version_id=output.object_version_id,
        sha256=output.sha256,
        content_type=output.content_type,
        width=output.width,
        height=output.height,
        frame_count=output.frame_count,
        fps=output.fps,
        duration_ms=output.duration_ms,
        byte_size=output.byte_size,
        output_metadata=dict(output.metadata),
        created_at=timestamp,
    )
    session.add(record)
    await _commit(session, "the I2V output conflicts with an existing result")
    return _output_snapshot(record)


async def list_recent_i2v_outputs(
    session: AsyncSession,
    *,
    actor_user_id: UUID | None = None,
    limit: int | None = None,
) -> tuple[I2VOutputSnapshot, ...]:
    statement = select(I2VOutput).join(I2VJob, I2VJob.id == I2VOutput.job_id)
    if actor_user_id is not None:
        statement = statement.where(I2VJob.created_by_user_id == actor_user_id)
    statement = statement.order_by(I2VOutput.created_at.desc(), I2VOutput.id.desc())
    if limit is not None:
        statement = statement.limit(_positive_limit(limit))
    outputs = (await session.scalars(statement)).all()
    return tuple(_output_snapshot(output) for output in outputs)


async def recover_expired_i2v_jobs(
    session: AsyncSession,
    *,
    now: datetime | None = None,
) -> tuple[I2VJobSnapshot, ...]:
    """Recover abandoned claims without imposing a retry ceiling.

    A cancellation remains a cancellation.  Other expired work returns to the end
    of the FIFO queue with its immutable request and monotonically increasing
    attempt counter intact.
    """

    timestamp = _now(now)
    await _lock_queue(session)
    jobs = list(
        (
            await session.scalars(
                select(I2VJob)
                .where(
                    I2VJob.state.in_(
                        (
                            I2VJobState.CLAIMED,
                            I2VJobState.RUNNING,
                            I2VJobState.CANCEL_REQUESTED,
                        )
                    ),
                    I2VJob.lease_expires_at <= timestamp,
                )
                .order_by(I2VJob.lease_expires_at, I2VJob.created_at, I2VJob.id)
                .with_for_update(skip_locked=True)
            )
        ).all()
    )
    if not jobs:
        return ()
    next_position = await _next_queue_position(session)
    for job in jobs:
        attempt = await session.scalar(
            select(I2VAttempt)
            .where(
                I2VAttempt.job_id == job.id,
                I2VAttempt.attempt_no == job.attempt_count,
            )
            .with_for_update()
        )
        if attempt is not None and attempt.state in {
            I2VAttemptState.CREATED,
            I2VAttemptState.RUNNING,
        }:
            attempt.state = (
                I2VAttemptState.CANCELLED
                if (job.state == I2VJobState.CANCEL_REQUESTED)
                else I2VAttemptState.FAILED
            )
            attempt.completed_at = timestamp
            attempt.error_code = "lease_expired"
            attempt.error_detail = "the worker lease expired before completion"
            attempt.updated_at = timestamp
        job.lease_owner = None
        job.lease_expires_at = None
        job.updated_at = timestamp
        if job.state == I2VJobState.CANCEL_REQUESTED:
            job.state = I2VJobState.CANCELLED
            job.completed_at = timestamp
        else:
            job.state = I2VJobState.QUEUED
            job.queue_position = next_position
            next_position += 1
            job.completed_at = None
            job.last_error_code = "lease_expired"
            job.last_error_detail = "the worker lease expired; generation was requeued"
    await _commit(session, "expired I2V claims changed concurrently")
    return tuple(_job_snapshot(job) for job in jobs)


async def record_i2v_worker_deployment(
    session: AsyncSession,
    *,
    registration: I2VWorkerDeploymentRegistration,
    deployment_id: UUID | None = None,
    now: datetime | None = None,
) -> I2VWorkerDeploymentSnapshot:
    timestamp = _now(now)
    provider = _nonempty_text(registration.provider, "worker provider")
    gpu_class = _nonempty_text(registration.gpu_class, "worker GPU class")
    worker_image_digest = _nonempty_text(registration.worker_image_digest, "worker image digest")
    provider_group_id = _optional_text(registration.provider_group_id, "provider group id")
    provider_instance_id = _optional_text(registration.provider_instance_id, "provider instance id")
    deployment: I2VWorkerDeployment | None = None
    if deployment_id is not None:
        deployment = await session.scalar(
            select(I2VWorkerDeployment)
            .where(I2VWorkerDeployment.id == deployment_id)
            .with_for_update()
        )
        if deployment is None:
            raise I2VNotFoundError("I2V worker deployment was not found")
    elif provider_group_id is not None:
        deployment = await session.scalar(
            select(I2VWorkerDeployment)
            .where(
                I2VWorkerDeployment.provider == provider,
                I2VWorkerDeployment.provider_group_id == provider_group_id,
            )
            .with_for_update()
        )
    if deployment is None:
        deployment = I2VWorkerDeployment(
            id=uuid7(),
            provider=provider,
            provider_group_id=provider_group_id,
            created_at=timestamp,
        )
        session.add(deployment)
    deployment.provider_instance_id = provider_instance_id
    deployment.state = registration.state
    deployment.gpu_class = gpu_class
    deployment.worker_image_digest = worker_image_digest
    deployment.current_job_id = registration.current_job_id
    deployment.started_at = registration.started_at
    deployment.ready_at = registration.ready_at
    deployment.stopped_at = registration.stopped_at
    deployment.last_heartbeat_at = registration.last_heartbeat_at
    deployment.deployment_metadata = dict(registration.metadata)
    deployment.updated_at = timestamp
    await _commit(session, "the worker deployment changed concurrently")
    return _deployment_snapshot(deployment)


async def get_i2v_worker_deployment(
    session: AsyncSession,
    *,
    deployment_id: UUID | None = None,
) -> I2VWorkerDeploymentSnapshot | None:
    statement = select(I2VWorkerDeployment)
    if deployment_id is not None:
        statement = statement.where(I2VWorkerDeployment.id == deployment_id)
    else:
        statement = statement.order_by(
            case(
                (I2VWorkerDeployment.state == I2VWorkerDeploymentState.BUSY, 0),
                (I2VWorkerDeployment.state == I2VWorkerDeploymentState.READY, 1),
                else_=2,
            ),
            I2VWorkerDeployment.updated_at.desc(),
            I2VWorkerDeployment.id.desc(),
        ).limit(1)
    deployment = await session.scalar(statement)
    return _deployment_snapshot(deployment) if deployment is not None else None


async def _owned_preset(
    session: AsyncSession,
    actor_user_id: UUID,
    preset_id: UUID,
    *,
    lock: bool,
) -> I2VPreset:
    statement = select(I2VPreset).where(
        I2VPreset.id == preset_id,
        I2VPreset.created_by_user_id == actor_user_id,
    )
    if lock:
        statement = statement.with_for_update()
    preset = await session.scalar(statement)
    if preset is None:
        raise I2VNotFoundError("I2V preset was not found")
    return preset


async def _owned_job(
    session: AsyncSession,
    actor_user_id: UUID,
    job_id: UUID,
    *,
    lock: bool,
) -> I2VJob:
    statement = select(I2VJob).where(
        I2VJob.id == job_id,
        I2VJob.created_by_user_id == actor_user_id,
    )
    if lock:
        statement = statement.with_for_update()
    job = await session.scalar(statement)
    if job is None:
        raise I2VNotFoundError("I2V job was not found")
    return job


async def _locked_claim(
    session: AsyncSession,
    job_id: UUID,
    attempt_id: UUID,
    worker_id: str,
    now: datetime,
) -> tuple[I2VJob, I2VAttempt]:
    job = await session.scalar(select(I2VJob).where(I2VJob.id == job_id).with_for_update())
    attempt = await session.scalar(
        select(I2VAttempt)
        .where(I2VAttempt.id == attempt_id, I2VAttempt.job_id == job_id)
        .with_for_update()
    )
    if job is None or attempt is None:
        raise I2VNotFoundError("I2V claim was not found")
    _require_worker_claim(job, worker_id, now)
    if attempt.worker_id != worker_id or attempt.attempt_no != job.attempt_count:
        raise I2VConflictError("the I2V attempt belongs to another worker")
    return job, attempt


def _require_worker_claim(job: I2VJob, worker_id: str, now: datetime) -> None:
    if job.lease_owner != _nonempty_text(worker_id, "worker id"):
        raise I2VConflictError("the I2V job belongs to another worker")
    if job.lease_expires_at is None or _as_utc(job.lease_expires_at) <= now:
        raise I2VConflictError("the I2V worker lease has expired")


async def _lock_queue(session: AsyncSession) -> None:
    bind = session.get_bind()
    if bind.dialect.name == "postgresql":
        await session.execute(text("SELECT pg_advisory_xact_lock(749220037)"))


async def _locked_queue(session: AsyncSession) -> list[I2VJob]:
    return list(
        (
            await session.scalars(
                select(I2VJob)
                .where(I2VJob.state == I2VJobState.QUEUED)
                .order_by(I2VJob.queue_position, I2VJob.created_at, I2VJob.id)
                .with_for_update()
            )
        ).all()
    )


async def _next_queue_position(session: AsyncSession) -> int:
    current = await session.scalar(
        select(func.max(I2VJob.queue_position)).where(I2VJob.state == I2VJobState.QUEUED)
    )
    return int(current or 0) + 1


async def _compact_queue(session: AsyncSession, now: datetime) -> None:
    await _write_queue_positions(session, await _locked_queue(session), now)


async def _write_queue_positions(
    session: AsyncSession,
    jobs: list[I2VJob],
    now: datetime,
) -> None:
    if not jobs:
        return
    maximum = max(int(job.queue_position or 0) for job in jobs)
    offset = maximum + len(jobs) + 1
    for index, job in enumerate(jobs, start=1):
        job.queue_position = offset + index
        job.updated_at = now
    await session.flush()
    for index, job in enumerate(jobs, start=1):
        job.queue_position = index
        job.updated_at = now
    await session.flush()


async def _commit(session: AsyncSession, conflict_message: str) -> None:
    try:
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        raise I2VConflictError(conflict_message) from error


def _input_snapshot(record: I2VInput) -> I2VInputSnapshot:
    return I2VInputSnapshot(
        input_id=UUID(str(record.id)),
        created_by_user_id=UUID(str(record.created_by_user_id)),
        source=record.source,
        asset_id=UUID(str(record.asset_id)) if record.asset_id is not None else None,
        display_name=record.display_name,
        storage_backend=record.storage_backend,
        storage_bucket=record.storage_bucket,
        object_key=record.object_key,
        object_version_id=record.object_version_id,
        sha256=record.sha256,
        content_type=record.content_type,
        width=record.width,
        height=record.height,
        byte_size=record.byte_size,
        metadata=dict(record.input_metadata),
        created_at=_as_utc(record.created_at),
    )


def _preset_snapshot(preset: I2VPreset) -> I2VPresetSnapshot:
    return I2VPresetSnapshot(
        preset_id=UUID(str(preset.id)),
        created_by_user_id=UUID(str(preset.created_by_user_id)),
        name=preset.name,
        description=preset.description,
        positive_prompt=preset.positive_prompt,
        negative_prompt=preset.negative_prompt,
        settings=dict(preset.settings),
        lock_version=preset.lock_version,
        created_at=_as_utc(preset.created_at),
        updated_at=_as_utc(preset.updated_at),
    )


def _job_snapshot(job: I2VJob) -> I2VJobSnapshot:
    return I2VJobSnapshot(
        job_id=UUID(str(job.id)),
        created_by_user_id=UUID(str(job.created_by_user_id)),
        input_id=UUID(str(job.input_id)),
        preset_id=UUID(str(job.preset_id)) if job.preset_id is not None else None,
        positive_prompt=job.positive_prompt,
        negative_prompt=job.negative_prompt,
        input_snapshot=dict(job.input_snapshot),
        preset_snapshot=dict(job.preset_snapshot),
        settings_snapshot=dict(job.settings_snapshot),
        request_sha256=job.request_sha256,
        state=job.state,
        queue_position=job.queue_position,
        attempt_count=job.attempt_count,
        lease_owner=job.lease_owner,
        lease_expires_at=_optional_utc(job.lease_expires_at),
        cancel_requested_at=_optional_utc(job.cancel_requested_at),
        completed_at=_optional_utc(job.completed_at),
        last_error_code=job.last_error_code,
        last_error_detail=job.last_error_detail,
        created_at=_as_utc(job.created_at),
        updated_at=_as_utc(job.updated_at),
    )


def _attempt_snapshot(attempt: I2VAttempt) -> I2VAttemptSnapshot:
    return I2VAttemptSnapshot(
        attempt_id=UUID(str(attempt.id)),
        job_id=UUID(str(attempt.job_id)),
        worker_deployment_id=(
            UUID(str(attempt.worker_deployment_id))
            if attempt.worker_deployment_id is not None
            else None
        ),
        attempt_no=attempt.attempt_no,
        state=attempt.state,
        worker_id=attempt.worker_id,
        worker_image_digest=attempt.worker_image_digest,
        provider_job_id=attempt.provider_job_id,
        request_metadata=dict(attempt.request_metadata),
        response_metadata=dict(attempt.response_metadata),
        started_at=_optional_utc(attempt.started_at),
        completed_at=_optional_utc(attempt.completed_at),
        error_code=attempt.error_code,
        error_detail=attempt.error_detail,
        created_at=_as_utc(attempt.created_at),
        updated_at=_as_utc(attempt.updated_at),
    )


def _output_snapshot(output: I2VOutput) -> I2VOutputSnapshot:
    return I2VOutputSnapshot(
        output_id=UUID(str(output.id)),
        job_id=UUID(str(output.job_id)),
        attempt_id=UUID(str(output.attempt_id)),
        storage_backend=output.storage_backend,
        storage_bucket=output.storage_bucket,
        object_key=output.object_key,
        object_version_id=output.object_version_id,
        sha256=output.sha256,
        content_type=output.content_type,
        width=output.width,
        height=output.height,
        frame_count=output.frame_count,
        fps=output.fps,
        duration_ms=output.duration_ms,
        byte_size=output.byte_size,
        metadata=dict(output.output_metadata),
        created_at=_as_utc(output.created_at),
    )


def _deployment_snapshot(deployment: I2VWorkerDeployment) -> I2VWorkerDeploymentSnapshot:
    return I2VWorkerDeploymentSnapshot(
        deployment_id=UUID(str(deployment.id)),
        provider=deployment.provider,
        provider_group_id=deployment.provider_group_id,
        provider_instance_id=deployment.provider_instance_id,
        state=deployment.state,
        gpu_class=deployment.gpu_class,
        worker_image_digest=deployment.worker_image_digest,
        current_job_id=(
            UUID(str(deployment.current_job_id)) if deployment.current_job_id is not None else None
        ),
        started_at=_optional_utc(deployment.started_at),
        ready_at=_optional_utc(deployment.ready_at),
        stopped_at=_optional_utc(deployment.stopped_at),
        last_heartbeat_at=_optional_utc(deployment.last_heartbeat_at),
        metadata=dict(deployment.deployment_metadata),
        created_at=_as_utc(deployment.created_at),
        updated_at=_as_utc(deployment.updated_at),
    )


def _canonical_sha256(value: dict[str, Any]) -> str:
    try:
        return canonical_sha256(value)
    except (TypeError, ValueError) as error:
        raise I2VInputError("I2V settings must be finite JSON data") from error


def _normalized_settings(value: dict[str, Any]) -> dict[str, Any]:
    try:
        return normalize_i2v_settings(value)
    except I2VLoraSelectionError as error:
        raise I2VInputError(str(error)) from error


def _dispatch_eligible(
    value: dict[str, Any],
    *,
    reviewed_loras_enabled: bool,
) -> bool:
    kind = classify_i2v_lora_settings(value)
    return kind == I2VLoraSettingsKind.BASELINE or (
        kind == I2VLoraSettingsKind.REVIEWED and reviewed_loras_enabled
    )


def _nonempty_text(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise I2VInputError(f"{label} must be non-empty trimmed text")
    return value


def _optional_text(value: str | None, label: str) -> str | None:
    return _nonempty_text(value, label) if value is not None else None


def _positive_limit(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise I2VInputError("limit must be positive")
    return value


def _now(value: datetime | None) -> datetime:
    return _as_utc(value or datetime.now(UTC))


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _optional_utc(value: datetime | None) -> datetime | None:
    return _as_utc(value) if value is not None else None
