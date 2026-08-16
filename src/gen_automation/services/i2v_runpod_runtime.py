"""Durable single-flight I2V runtime backed by RunPod Serverless Queue."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol, cast

from pydantic import ValidationError
from sqlalchemy import case, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gen_automation.db.models import I2VAttempt, I2VJob, I2VWorkerDeployment
from gen_automation.domain.i2v import (
    I2VAttemptSnapshot,
    I2VAttemptState,
    I2VJobSnapshot,
    I2VJobState,
    I2VOutputRegistration,
    I2VWorkerDeploymentRegistration,
    I2VWorkerDeploymentState,
)
from gen_automation.domain.i2v_loras import I2VLoraSettingsKind, classify_i2v_lora_settings
from gen_automation.i2v_worker.models import I2VResult
from gen_automation.i2v_worker.runpod_models import RunPodI2VInput
from gen_automation.integrations.runpod.client import RunPodClient
from gen_automation.integrations.runpod.errors import (
    RunPodAPIError,
    RunPodRateLimitError,
    RunPodTransportError,
)
from gen_automation.integrations.runpod.models import (
    JSONObject,
    JSONValue,
    RunPodEndpointHealth,
    RunPodJob,
    RunPodJobStatus,
)
from gen_automation.services.i2v import (
    I2V_RUNTIME_WORKER_ID,
    acknowledge_i2v_cancellation,
    claim_next_i2v_job,
    complete_i2v_job,
    fail_i2v_attempt,
    i2v_attempt_snapshot,
    i2v_job_snapshot,
    record_i2v_worker_deployment,
    start_i2v_attempt,
)
from gen_automation.services.i2v_runpod_claims import (
    I2VRunPodClaimIdentity,
    create_i2v_runpod_claim_token,
    i2v_runpod_submission_key,
)

_ACTIVE_JOB_STATES = (
    I2VJobState.CLAIMED,
    I2VJobState.RUNNING,
    I2VJobState.CANCEL_REQUESTED,
)
_ACTIVE_ATTEMPT_STATES = (I2VAttemptState.CREATED, I2VAttemptState.RUNNING)
_DEFINITELY_REJECTED_STATUS_CODES = frozenset({400, 401, 403, 404, 422})
_GPU_CLASS = "RunPod Serverless 32GB+"


class I2VRunPodRuntimeError(Exception):
    """Base redacted runtime error."""


class I2VRunPodRuntimeConfigurationError(I2VRunPodRuntimeError):
    pass


class I2VRunPodJobInputBuilder(Protocol):
    async def build(
        self,
        *,
        job: I2VJobSnapshot,
        attempt: I2VAttemptSnapshot,
    ) -> Mapping[str, JSONValue]: ...

    async def verify_output(
        self,
        *,
        job: I2VJobSnapshot,
        attempt: I2VAttemptSnapshot,
        output: I2VOutputRegistration,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class I2VRunPodRuntimeConfig:
    endpoint_id: str
    worker_image: str
    claim_url: str
    claim_secret: str
    worker_lease_seconds: int
    execution_timeout_seconds: int
    job_ttl_seconds: int
    submission_claim_timeout_seconds: int = 30 * 60
    queue_timeout_seconds: int = 30 * 60
    terminal_grace_seconds: int = 5 * 60
    output_prefix: str = "i2v/outputs"
    reviewed_loras_enabled: bool = False


@dataclass(frozen=True, slots=True)
class I2VRunPodRuntimeCycle:
    action: str
    changed: bool


class I2VRunPodRuntime:
    """Advance at most one durable or provider transition per cycle."""

    def __init__(
        self,
        *,
        config: I2VRunPodRuntimeConfig,
        sessions: async_sessionmaker[AsyncSession],
        runpod_client: RunPodClient,
        input_builder: I2VRunPodJobInputBuilder,
    ) -> None:
        if runpod_client.endpoint_id != config.endpoint_id:
            raise I2VRunPodRuntimeConfigurationError("RunPod endpoint identity mismatch")
        self.config = config
        self.sessions = sessions
        self.runpod_client = runpod_client
        self.input_builder = input_builder
        self.worker_id = I2V_RUNTIME_WORKER_ID

    async def cycle_once(self, *, now: datetime | None = None) -> bool:
        return (await self.run_cycle(now=now)).changed

    async def run_cycle(self, *, now: datetime | None = None) -> I2VRunPodRuntimeCycle:
        timestamp = _as_utc(now or datetime.now(UTC))
        active = await self._active_attempts()
        if active:
            action = await self._advance_attempt(*active[0], now=timestamp)
            if action is not None:
                return I2VRunPodRuntimeCycle(action=action, changed=True)
            return I2VRunPodRuntimeCycle(action="provider_unchanged", changed=False)

        async with self.sessions() as session:
            queued_settings = (
                await session.scalars(
                    select(I2VJob.settings_snapshot)
                    .where(I2VJob.state == I2VJobState.QUEUED)
                    .order_by(I2VJob.queue_position)
                )
            ).all()
        queued_count = sum(
            _dispatch_eligible(
                item,
                reviewed_loras_enabled=self.config.reviewed_loras_enabled,
            )
            for item in queued_settings
        )
        health = await self.runpod_client.health()
        if await self._sync_deployment(health, queued_count=queued_count, now=timestamp):
            return I2VRunPodRuntimeCycle(action="deployment_observed", changed=True)
        if queued_count:
            async with self.sessions() as session:
                claim = await claim_next_i2v_job(
                    session,
                    worker_id=self.worker_id,
                    lease_duration=timedelta(seconds=self.config.worker_lease_seconds),
                    worker_image_digest=self.config.worker_image,
                    reviewed_loras_enabled=self.config.reviewed_loras_enabled,
                    now=timestamp,
                )
            if claim is not None:
                submission_key = _submission_key(claim.job, claim.attempt)
                await self._record_attempt_metadata(
                    claim.job,
                    claim.attempt,
                    provider_job_id=None,
                    updates={
                        "schema": "i2v-runpod-attempt/v1",
                        "submission_key": submission_key,
                        "submission_state": "prepared",
                        "provider": "runpod",
                    },
                    now=timestamp,
                )
                return I2VRunPodRuntimeCycle(action="job_claimed", changed=True)
        return I2VRunPodRuntimeCycle(action="idle", changed=False)

    async def _active_attempts(
        self,
    ) -> tuple[tuple[I2VJobSnapshot, I2VAttemptSnapshot], ...]:
        async with self.sessions() as session:
            rows = (
                await session.execute(
                    select(I2VJob, I2VAttempt)
                    .join(
                        I2VAttempt,
                        (I2VAttempt.job_id == I2VJob.id)
                        & (I2VAttempt.attempt_no == I2VJob.attempt_count),
                    )
                    .where(
                        I2VJob.state.in_(_ACTIVE_JOB_STATES),
                        I2VAttempt.state.in_(_ACTIVE_ATTEMPT_STATES),
                    )
                    .order_by(
                        case((I2VJob.state == I2VJobState.CANCEL_REQUESTED, 0), else_=1),
                        I2VAttempt.created_at,
                        I2VAttempt.id,
                    )
                )
            ).all()
        return tuple(
            (i2v_job_snapshot(job), i2v_attempt_snapshot(attempt)) for job, attempt in rows
        )

    async def _advance_attempt(
        self,
        job: I2VJobSnapshot,
        attempt: I2VAttemptSnapshot,
        *,
        now: datetime,
    ) -> str | None:
        submission_key = _submission_key(job, attempt)
        if attempt.provider_job_id is None:
            if job.state == I2VJobState.CANCEL_REQUESTED:
                async with self.sessions() as session:
                    await acknowledge_i2v_cancellation(
                        session,
                        job_id=job.job_id,
                        attempt_id=attempt.attempt_id,
                        worker_id=self.worker_id,
                        response_metadata={
                            "provider": "runpod",
                            "provider_status": "NOT_BOUND",
                        },
                        now=now,
                    )
                return "unbound_cancellation_acknowledged"
            state = attempt.request_metadata.get("submission_state")
            if state == "prepared":
                return await self._submit_existing(job, attempt, now=now)
            if state in {"submitting", "unknown"}:
                # Never retry an ambiguous paid submission. An accepted worker
                # attaches its provider ID through the one-time claim callback.
                started_at = _metadata_datetime(attempt, "submission_started_at")
                if started_at is None:
                    started_at = attempt.updated_at
                if now >= started_at + timedelta(
                    seconds=self.config.submission_claim_timeout_seconds
                ):
                    await self._fail_attempt(
                        job,
                        attempt,
                        error_code="runpod_submission_unresolved",
                        error_detail="RunPod submission identity was not resolved in time.",
                        now=now,
                    )
                    return "submission_unresolved_failed"
                return None
            await self._record_attempt_metadata(
                job,
                attempt,
                provider_job_id=None,
                updates={
                    "schema": "i2v-runpod-attempt/v1",
                    "submission_key": submission_key,
                    "submission_state": "prepared",
                    "provider": "runpod",
                },
                now=now,
            )
            return "attempt_prepared"
        try:
            remote = await self.runpod_client.get_job(attempt.provider_job_id)
        except RunPodAPIError as error:
            if error.status_code != 404:
                raise
            if job.state == I2VJobState.CANCEL_REQUESTED:
                async with self.sessions() as session:
                    await acknowledge_i2v_cancellation(
                        session,
                        job_id=job.job_id,
                        attempt_id=attempt.attempt_id,
                        worker_id=self.worker_id,
                        response_metadata={
                            "provider": "runpod",
                            "provider_status": "NOT_FOUND",
                            "provider_job_id": attempt.provider_job_id,
                        },
                        now=now,
                    )
                return "missing_provider_cancellation_acknowledged"
            absent_at = _metadata_datetime(attempt, "provider_absence_observed_at")
            if absent_at is not None and now >= absent_at + timedelta(
                seconds=self.config.terminal_grace_seconds
            ):
                await self._fail_attempt(
                    job,
                    attempt,
                    error_code="runpod_provider_job_missing",
                    error_detail="RunPod no longer reports the submitted I2V job.",
                    now=now,
                )
                return "provider_absence_failed"
            await self._record_attempt_metadata(
                job,
                attempt,
                provider_job_id=attempt.provider_job_id,
                updates={"provider_absence_observed_at": now.isoformat()},
                now=now,
            )
            return "provider_absence_observed"
        if remote.id != attempt.provider_job_id:
            raise I2VRunPodRuntimeError("RunPod job identity mismatch")
        if job.state == I2VJobState.CANCEL_REQUESTED:
            return await self._advance_cancellation(job, attempt, remote, now=now)
        if remote.status == RunPodJobStatus.IN_QUEUE:
            queued_at = _metadata_datetime(attempt, "provider_queued_at") or attempt.updated_at
            if now >= queued_at + timedelta(seconds=self.config.queue_timeout_seconds):
                cancel_requested_at = _metadata_datetime(
                    attempt,
                    "queue_timeout_cancel_requested_at",
                )
                if cancel_requested_at is None:
                    await self.runpod_client.cancel(remote.id)
                    await self._record_attempt_metadata(
                        job,
                        attempt,
                        provider_job_id=remote.id,
                        updates={
                            "queue_timeout_cancel_requested_at": now.isoformat(),
                            "provider_status": remote.status.value,
                        },
                        now=now,
                    )
                    return "provider_queue_timeout_cancel_requested"
                if now >= cancel_requested_at + timedelta(
                    seconds=self.config.terminal_grace_seconds
                ):
                    await self._fail_attempt(
                        job,
                        attempt,
                        error_code="runpod_queue_timeout",
                        error_detail="RunPod did not allocate the I2V job in time.",
                        now=now,
                    )
                    return "provider_queue_timeout_failed"
            return await self._observe_nonterminal(job, attempt, remote, now=now)
        if remote.status == RunPodJobStatus.IN_PROGRESS:
            if attempt.state == I2VAttemptState.CREATED:
                async with self.sessions() as session:
                    await start_i2v_attempt(
                        session,
                        job_id=job.job_id,
                        attempt_id=attempt.attempt_id,
                        worker_id=self.worker_id,
                        provider_job_id=remote.id,
                        request_metadata=_safe_attempt_metadata(
                            attempt,
                            provider_status=remote.status,
                            now=now,
                        ),
                        now=now,
                    )
                return "inference_started"
            return await self._observe_nonterminal(job, attempt, remote, now=now)
        if remote.status == RunPodJobStatus.COMPLETED:
            output = _parse_worker_output(remote.output, job=job, attempt=attempt)
            expected = f"{self.config.output_prefix}/{job.job_id}/{attempt.attempt_id}.mp4"
            if output.object_key != expected:
                raise I2VRunPodRuntimeError("I2V worker output key mismatch")
            await self.input_builder.verify_output(job=job, attempt=attempt, output=output)
            async with self.sessions() as session:
                await complete_i2v_job(
                    session,
                    job_id=job.job_id,
                    attempt_id=attempt.attempt_id,
                    worker_id=self.worker_id,
                    output=output,
                    response_metadata=_response_metadata(remote),
                    now=now,
                )
            return "output_completed"
        if remote.status in {
            RunPodJobStatus.FAILED,
            RunPodJobStatus.TIMED_OUT,
            RunPodJobStatus.CANCELLED,
        }:
            async with self.sessions() as session:
                await fail_i2v_attempt(
                    session,
                    job_id=job.job_id,
                    attempt_id=attempt.attempt_id,
                    worker_id=self.worker_id,
                    error_code=f"runpod_{remote.status.value.lower()}",
                    error_detail=f"RunPod reported {remote.status.value} for the I2V job.",
                    response_metadata=_response_metadata(remote),
                    now=now,
                )
            return "provider_terminal_failure"
        raise I2VRunPodRuntimeError("RunPod returned an unsupported job state")

    async def _submit_existing(
        self,
        job: I2VJobSnapshot,
        attempt: I2VAttemptSnapshot,
        *,
        now: datetime,
    ) -> str:
        additions = dict(await self.input_builder.build(job=job, attempt=attempt))
        submission_key = _submission_key(job, attempt)
        expires_at = now + timedelta(seconds=self.config.job_ttl_seconds)
        token = create_i2v_runpod_claim_token(
            secret=self.config.claim_secret,
            identity=I2VRunPodClaimIdentity(
                job_id=job.job_id,
                attempt_id=attempt.attempt_id,
                request_sha256=job.request_sha256,
                submission_key=submission_key,
                expires_at=expires_at,
            ),
        )
        job_payload: JSONObject = {
            "schema": "i2v-job/v2",
            "job_id": str(job.job_id),
            "attempt_id": str(attempt.attempt_id),
            "request_sha256": job.request_sha256,
            "input_snapshot": cast(JSONValue, _worker_input_snapshot(job.input_snapshot)),
            "positive_prompt": job.positive_prompt,
            "negative_prompt": job.negative_prompt,
            "settings_snapshot": cast(JSONValue, job.settings_snapshot),
            "input_grant": additions.get("input_grant"),
            "output_grant": additions.get("output_grant"),
        }
        envelope = RunPodI2VInput.model_validate(
            {
                "schema": "i2v-runpod-input/v1",
                "submission_key": submission_key,
                "job": job_payload,
                "claim": {
                    "method": "POST",
                    "url": self.config.claim_url,
                    "bearer_token": token,
                    "expires_at": expires_at.isoformat(),
                },
                "model_grants": additions.get("model_grants"),
            },
            strict=False,
        )
        await self._record_attempt_metadata(
            job,
            attempt,
            provider_job_id=None,
            updates={"submission_state": "submitting", "submission_started_at": now.isoformat()},
            now=now,
        )
        try:
            remote = await self.runpod_client.submit(
                input_payload=cast(
                    JSONObject,
                    envelope.model_dump(mode="json", by_alias=True),
                ),
                execution_timeout_ms=self.config.execution_timeout_seconds * 1000,
                ttl_ms=self.config.job_ttl_seconds * 1000,
            )
        except RunPodRateLimitError:
            await self._record_attempt_metadata(
                job,
                attempt,
                provider_job_id=None,
                updates={"submission_state": "prepared"},
                now=now,
            )
            return "submission_rate_limited"
        except RunPodAPIError as error:
            if error.status_code in _DEFINITELY_REJECTED_STATUS_CODES:
                async with self.sessions() as session:
                    await fail_i2v_attempt(
                        session,
                        job_id=job.job_id,
                        attempt_id=attempt.attempt_id,
                        worker_id=self.worker_id,
                        error_code="runpod_submission_rejected",
                        error_detail=(
                            f"RunPod rejected the I2V request with HTTP {error.status_code}."
                        ),
                        now=now,
                    )
                return "submission_rejected"
            await self._mark_submission_unknown(job, attempt, now=now)
            return "submission_unknown"
        except RunPodTransportError:
            await self._mark_submission_unknown(job, attempt, now=now)
            return "submission_unknown"
        await self._record_attempt_metadata(
            job,
            attempt,
            provider_job_id=remote.id,
            updates={
                "submission_state": "submitted",
                "provider_status": remote.status.value,
                "provider_observed_at": now.isoformat(),
                **(
                    {"provider_queued_at": now.isoformat()}
                    if remote.status == RunPodJobStatus.IN_QUEUE
                    else {}
                ),
            },
            now=now,
        )
        return "provider_job_submitted"

    async def _advance_cancellation(
        self,
        job: I2VJobSnapshot,
        attempt: I2VAttemptSnapshot,
        remote: RunPodJob,
        *,
        now: datetime,
    ) -> str:
        if remote.status in {
            RunPodJobStatus.COMPLETED,
            RunPodJobStatus.FAILED,
            RunPodJobStatus.TIMED_OUT,
            RunPodJobStatus.CANCELLED,
        }:
            async with self.sessions() as session:
                await acknowledge_i2v_cancellation(
                    session,
                    job_id=job.job_id,
                    attempt_id=attempt.attempt_id,
                    worker_id=self.worker_id,
                    response_metadata=_response_metadata(remote),
                    now=now,
                )
            return "cancellation_acknowledged"
        if attempt.request_metadata.get("cancel_requested_provider_job_id") != remote.id:
            await self.runpod_client.cancel(remote.id)
            await self._record_attempt_metadata(
                job,
                attempt,
                provider_job_id=remote.id,
                updates={
                    "cancel_requested_provider_job_id": remote.id,
                    "cancel_requested_at": now.isoformat(),
                },
                now=now,
            )
            return "provider_cancellation_requested"
        return await self._observe_nonterminal(job, attempt, remote, now=now)

    async def _observe_nonterminal(
        self,
        job: I2VJobSnapshot,
        attempt: I2VAttemptSnapshot,
        remote: RunPodJob,
        *,
        now: datetime,
    ) -> str:
        await self._record_attempt_metadata(
            job,
            attempt,
            provider_job_id=remote.id,
            updates=_safe_attempt_metadata(attempt, provider_status=remote.status, now=now),
            now=now,
        )
        return (
            "provider_pending_observed"
            if remote.status == RunPodJobStatus.IN_QUEUE
            else "provider_running_observed"
        )

    async def _mark_submission_unknown(
        self,
        job: I2VJobSnapshot,
        attempt: I2VAttemptSnapshot,
        *,
        now: datetime,
    ) -> None:
        await self._record_attempt_metadata(
            job,
            attempt,
            provider_job_id=None,
            updates={"submission_state": "unknown", "submission_unknown_at": now.isoformat()},
            now=now,
        )

    async def _fail_attempt(
        self,
        job: I2VJobSnapshot,
        attempt: I2VAttemptSnapshot,
        *,
        error_code: str,
        error_detail: str,
        now: datetime,
    ) -> None:
        async with self.sessions() as session:
            await fail_i2v_attempt(
                session,
                job_id=job.job_id,
                attempt_id=attempt.attempt_id,
                worker_id=self.worker_id,
                error_code=error_code,
                error_detail=error_detail,
                now=now,
            )

    async def _record_attempt_metadata(
        self,
        job: I2VJobSnapshot,
        attempt: I2VAttemptSnapshot,
        *,
        provider_job_id: str | None,
        updates: Mapping[str, JSONValue],
        now: datetime,
    ) -> None:
        metadata = dict(attempt.request_metadata)
        metadata.update(updates)
        async with self.sessions() as session:
            row = await session.scalar(
                select(I2VAttempt)
                .where(
                    I2VAttempt.id == attempt.attempt_id,
                    I2VAttempt.job_id == job.job_id,
                    I2VAttempt.attempt_no == job.attempt_count,
                    I2VAttempt.state.in_(_ACTIVE_ATTEMPT_STATES),
                )
                .with_for_update()
            )
            durable_job = await session.scalar(
                select(I2VJob)
                .where(
                    I2VJob.id == job.job_id,
                    I2VJob.state.in_(_ACTIVE_JOB_STATES),
                )
                .with_for_update()
            )
            if row is None or durable_job is None:
                raise I2VRunPodRuntimeError("I2V attempt changed concurrently")
            if row.provider_job_id not in {None, provider_job_id}:
                raise I2VRunPodRuntimeError("I2V provider job changed concurrently")
            row.provider_job_id = provider_job_id
            row.request_metadata = metadata
            row.worker_id = self.worker_id
            row.updated_at = now
            durable_job.lease_owner = self.worker_id
            durable_job.lease_expires_at = now + timedelta(seconds=self.config.worker_lease_seconds)
            durable_job.updated_at = now
            await session.commit()

    async def _sync_deployment(
        self,
        health: RunPodEndpointHealth,
        *,
        queued_count: int,
        now: datetime,
    ) -> bool:
        state = (
            I2VWorkerDeploymentState.BUSY
            if health.in_progress_jobs
            else I2VWorkerDeploymentState.PROVISIONING
            if health.in_queue_jobs
            else I2VWorkerDeploymentState.READY
        )
        metadata: dict[str, JSONValue] = {
            "provider_status": "available",
            "scale_to_zero": True,
            "queued_job_count": queued_count,
            "provider_in_queue": health.in_queue_jobs,
            "provider_in_progress": health.in_progress_jobs,
            "provider_running_workers": health.running_workers,
            "provider_idle_workers": health.idle_workers,
        }
        async with self.sessions() as session:
            existing = await session.scalar(
                select(I2VWorkerDeployment).where(
                    I2VWorkerDeployment.provider == "runpod",
                    I2VWorkerDeployment.provider_group_id == self.config.endpoint_id,
                )
            )
            fresh = (
                existing is not None
                and existing.last_heartbeat_at is not None
                and _as_utc(existing.last_heartbeat_at) > now - timedelta(minutes=1)
            )
            changed = (
                existing is None
                or existing.state != state
                or existing.worker_image_digest != self.config.worker_image
                or existing.deployment_metadata != metadata
                or not fresh
            )
        if not changed:
            return False
        async with self.sessions() as session:
            await record_i2v_worker_deployment(
                session,
                registration=I2VWorkerDeploymentRegistration(
                    provider="runpod",
                    provider_group_id=self.config.endpoint_id,
                    provider_instance_id=None,
                    state=state,
                    gpu_class=_GPU_CLASS,
                    worker_image_digest=self.config.worker_image,
                    last_heartbeat_at=now,
                    metadata=metadata,
                ),
                now=now,
            )
        return True


def _submission_key(job: I2VJobSnapshot, attempt: I2VAttemptSnapshot) -> str:
    calculated = i2v_runpod_submission_key(
        job_id=job.job_id,
        attempt_id=attempt.attempt_id,
        attempt_no=attempt.attempt_no,
        request_sha256=job.request_sha256,
    )
    stored = attempt.request_metadata.get("submission_key")
    if stored is not None and stored != calculated:
        raise I2VRunPodRuntimeError("stored RunPod submission identity is invalid")
    return calculated


def _safe_attempt_metadata(
    attempt: I2VAttemptSnapshot,
    *,
    provider_status: RunPodJobStatus,
    now: datetime,
) -> dict[str, JSONValue]:
    metadata = dict(attempt.request_metadata)
    metadata.update(
        {
            "schema": "i2v-runpod-attempt/v1",
            "provider": "runpod",
            "submission_state": "submitted",
            "provider_status": provider_status.value,
            "provider_observed_at": now.isoformat(),
        }
    )
    if provider_status == RunPodJobStatus.IN_QUEUE and not isinstance(
        metadata.get("provider_queued_at"),
        str,
    ):
        metadata["provider_queued_at"] = now.isoformat()
    return cast(dict[str, JSONValue], metadata)


def _response_metadata(remote: RunPodJob) -> dict[str, object]:
    return {
        "provider": "runpod",
        "provider_status": remote.status.value,
        "provider_job_id": remote.id,
        "delay_time_ms": remote.delay_time_ms,
        "execution_time_ms": remote.execution_time_ms,
        "worker_id": remote.worker_id,
    }


def _parse_worker_output(
    value: JSONValue,
    *,
    job: I2VJobSnapshot,
    attempt: I2VAttemptSnapshot,
) -> I2VOutputRegistration:
    try:
        result = I2VResult.model_validate_json(
            json.dumps(value, separators=(",", ":")),
            strict=True,
        )
    except (TypeError, ValueError, ValidationError):
        raise I2VRunPodRuntimeError("RunPod I2V output is invalid") from None
    if (
        result.job_id != job.job_id
        or result.attempt_id != attempt.attempt_id
        or result.request_sha256 != job.request_sha256
    ):
        raise I2VRunPodRuntimeError("RunPod I2V output identity mismatch")
    output = result.output
    return I2VOutputRegistration(
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
        metadata=output.metadata,
    )


def _worker_input_snapshot(value: Mapping[str, object]) -> dict[str, object]:
    names = (
        "storage_backend",
        "storage_bucket",
        "object_key",
        "object_version_id",
        "sha256",
        "content_type",
        "width",
        "height",
        "byte_size",
    )
    normalized = {name: value.get(name) for name in names}
    if (
        normalized["storage_backend"] != "s3"
        or not isinstance(normalized["storage_bucket"], str)
        or not isinstance(normalized["object_key"], str)
        or not isinstance(normalized["object_version_id"], str)
        or not isinstance(normalized["sha256"], str)
        or not isinstance(normalized["content_type"], str)
        or not isinstance(normalized["width"], int)
        or not isinstance(normalized["height"], int)
        or not isinstance(normalized["byte_size"], int)
    ):
        raise I2VRunPodRuntimeConfigurationError("durable I2V input snapshot is invalid")
    return normalized


def _dispatch_eligible(
    value: Mapping[str, object],
    *,
    reviewed_loras_enabled: bool,
) -> bool:
    kind = classify_i2v_lora_settings(value)
    return kind == I2VLoraSettingsKind.BASELINE or (
        kind == I2VLoraSettingsKind.REVIEWED and reviewed_loras_enabled
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _metadata_datetime(attempt: I2VAttemptSnapshot, key: str) -> datetime | None:
    value = attempt.request_metadata.get(key)
    if not isinstance(value, str):
        return None
    try:
        return _as_utc(datetime.fromisoformat(value))
    except ValueError:
        return None
