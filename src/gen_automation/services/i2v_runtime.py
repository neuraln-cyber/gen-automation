"""Durable one-step runtime for the fresh Salad-backed I2V lane."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol, cast
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gen_automation.db.models import I2VAttempt, I2VJob, I2VWorkerDeployment
from gen_automation.domain.canonical import canonical_sha256
from gen_automation.domain.i2v import (
    I2VAttemptSnapshot,
    I2VAttemptState,
    I2VJobSnapshot,
    I2VJobState,
    I2VOutputRegistration,
    I2VWorkerDeploymentRegistration,
    I2VWorkerDeploymentState,
)
from gen_automation.integrations.salad.errors import (
    SaladAPIError,
    SaladRateLimitError,
    SaladTransportError,
)
from gen_automation.integrations.salad.models import (
    JSONObject,
    JSONValue,
    SaladJobStatus,
    SaladQueueJob,
)
from gen_automation.services.i2v import (
    acknowledge_i2v_cancellation,
    adopt_validated_i2v_provider_attempt,
    claim_next_i2v_job,
    complete_i2v_job,
    fail_i2v_attempt,
    record_i2v_worker_deployment,
    recover_expired_i2v_jobs,
    start_i2v_attempt,
)
from gen_automation.services.i2v_salad import (
    I2V_SALAD_JOB_SCHEMA,
    I2VInfrastructureMutation,
    I2VProviderObservation,
    I2VSaladClient,
    I2VSaladConfig,
    I2VSaladConflictError,
    ensure_i2v_infrastructure_step,
    find_i2v_submission,
    i2v_submission_metadata,
    observe_i2v_provider,
    parse_i2v_worker_output,
    validate_i2v_group_contract,
)

_ACTIVE_JOB_STATES = (
    I2VJobState.CLAIMED,
    I2VJobState.RUNNING,
    I2VJobState.CANCEL_REQUESTED,
)
_ACTIVE_ATTEMPT_STATES = (I2VAttemptState.CREATED, I2VAttemptState.RUNNING)
_DEFINITELY_REJECTED_STATUS_CODES = frozenset({400, 401, 403, 404, 422})
I2V_SINGLETON_WORKER_ID = "controller:i2v:singleton"
_RESTART_RECONCILIATION_WAITING = "restart_reconciliation_waiting"


class I2VRuntimeError(Exception):
    """Base runtime error."""


class I2VRuntimeConfigurationError(I2VRuntimeError):
    pass


class I2VJobInputBuilder(Protocol):
    async def build(
        self,
        *,
        job: I2VJobSnapshot,
        attempt: I2VAttemptSnapshot,
    ) -> Mapping[str, JSONValue]:
        """Return fresh, short-lived input/output grants for one submission."""

    async def verify_output(
        self,
        *,
        job: I2VJobSnapshot,
        attempt: I2VAttemptSnapshot,
        output: I2VOutputRegistration,
    ) -> None:
        """Verify the exact uploaded object before completing the job."""


class I2VRuntimeEnvironmentProvider(Protocol):
    async def resolve(self) -> Mapping[str, str]:
        """Return fresh worker bootstrap values without durable secret storage."""


@dataclass(frozen=True)
class I2VRuntimeConfig:
    salad: I2VSaladConfig
    output_prefix: str = "i2v/outputs"


@dataclass(frozen=True)
class I2VRuntimeCycle:
    action: str
    changed: bool


class I2VRuntime:
    """Advance at most one externally meaningful I2V transition per cycle."""

    def __init__(
        self,
        *,
        config: I2VRuntimeConfig,
        sessions: async_sessionmaker[AsyncSession],
        salad_client: I2VSaladClient,
        worker_id: str,
        input_builder: I2VJobInputBuilder | None,
        environment_provider: I2VRuntimeEnvironmentProvider | None = None,
    ) -> None:
        if not worker_id or worker_id != worker_id.strip():
            raise I2VRuntimeConfigurationError("I2V runtime worker ID is invalid")
        self.config = config
        self.sessions = sessions
        self.salad_client = salad_client
        self.worker_id = worker_id
        self.input_builder = input_builder
        self.environment_provider = environment_provider
        self._runtime_prepared = False

    async def cycle_once(self, *, now: datetime | None = None) -> bool:
        return (await self.run_cycle(now=now)).changed

    async def run_cycle(self, *, now: datetime | None = None) -> I2VRuntimeCycle:
        timestamp = _as_utc(now or datetime.now(UTC))
        infrastructure = await ensure_i2v_infrastructure_step(
            self.salad_client,
            self.config.salad,
        )
        if infrastructure.mutation == I2VInfrastructureMutation.QUEUE_CREATED:
            return I2VRuntimeCycle(action="queue_created", changed=True)
        if infrastructure.mutation == I2VInfrastructureMutation.GROUP_CREATED:
            return I2VRuntimeCycle(action="group_created", changed=True)
        if infrastructure.mutation == I2VInfrastructureMutation.GROUP_CONTRACT_REPAIRED:
            return I2VRuntimeCycle(action="group_contract_repaired", changed=True)
        if infrastructure.group is None:
            raise I2VSaladConflictError("I2V infrastructure reconciliation omitted its group")

        async with self.sessions() as session:
            active_count = int(
                await session.scalar(
                    select(func.count(I2VAttempt.id))
                    .join(I2VJob, I2VJob.id == I2VAttempt.job_id)
                    .where(
                        I2VJob.state.in_(_ACTIVE_JOB_STATES),
                        I2VAttempt.state.in_(_ACTIVE_ATTEMPT_STATES),
                        I2VAttempt.attempt_no == I2VJob.attempt_count,
                    )
                )
                or 0
            )
            queued_count = int(
                await session.scalar(
                    select(func.count(I2VJob.id)).where(I2VJob.state == I2VJobState.QUEUED)
                )
                or 0
            )
        observation = await observe_i2v_provider(
            self.salad_client,
            self.config.salad,
            active_job_count=active_count,
        )

        # Reconcile/cancel the oldest active attempt before adding provider work.
        for active in await self._active_attempts():
            action = await self._advance_attempt(*active, now=timestamp)
            if action is not None:
                return I2VRuntimeCycle(
                    action=action,
                    changed=action != _RESTART_RECONCILIATION_WAITING,
                )

        if await self._sync_deployment(
            observation,
            queued_count=queued_count,
            active_count=active_count,
            now=timestamp,
        ):
            return I2VRuntimeCycle(action="deployment_observed", changed=True)

        group_stopped = observation.state == I2VWorkerDeploymentState.STOPPED
        if group_stopped and (queued_count > 0 or active_count > 0):
            if not self._runtime_prepared:
                if self.environment_provider is None:
                    raise I2VRuntimeConfigurationError(
                        "production I2V startup requires fresh runtime credentials"
                    )
                environment = dict(await self.environment_provider.resolve())
                if not environment:
                    raise I2VRuntimeConfigurationError("I2V runtime environment is empty")
                updated = await self.salad_client.update_container_group(
                    self.config.salad.container_group_name,
                    {
                        "container": {
                            "environment_variables": cast(JSONValue, environment),
                            "priority": self.config.salad.priority,
                        },
                        "queue_autoscaler": (self.config.salad.queue_autoscaler_configuration()),
                    },
                )
                validate_i2v_group_contract(updated, self.config.salad)
                self._runtime_prepared = True
                return I2VRuntimeCycle(action="runtime_credentials_refreshed", changed=True)
            await self.salad_client.start_container_group(self.config.salad.container_group_name)
            self._runtime_prepared = False
            return I2VRuntimeCycle(action="group_start_requested", changed=True)

        # The durable FIFO is unbounded. Prefetch controls only the number handed
        # to Salad at once, so operators can reorder everything still in PostgreSQL.
        if not group_stopped and active_count < self.config.salad.prefetch and queued_count > 0:
            action = await self._claim_and_submit(now=timestamp)
            if action is not None:
                return I2VRuntimeCycle(action=action, changed=True)

        # Required recovery occurs before every attempted claim. Provider-backed
        # attempts were handled above, so this only recovers truly abandoned work.
        if queued_count > 0 and active_count >= self.config.salad.prefetch:
            return I2VRuntimeCycle(action="prefetch_full", changed=False)
        async with self.sessions() as session:
            recovered = await recover_expired_i2v_jobs(session, now=timestamp)
        if recovered:
            return I2VRuntimeCycle(action="expired_claims_recovered", changed=True)

        if await self._should_stop_warm_group(observation, now=timestamp):
            await self.salad_client.stop_container_group(self.config.salad.container_group_name)
            return I2VRuntimeCycle(action="group_stop_requested", changed=True)
        return I2VRuntimeCycle(action="idle", changed=False)

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
            return tuple((_job_snapshot(job), _attempt_snapshot(attempt)) for job, attempt in rows)

    async def _advance_attempt(
        self,
        job: I2VJobSnapshot,
        attempt: I2VAttemptSnapshot,
        *,
        now: datetime,
    ) -> str | None:
        submission_key = _submission_key(job, attempt)
        remote = None
        if attempt.provider_job_id is not None:
            try:
                remote = await self.salad_client.get_job(
                    self.config.salad.queue_name,
                    attempt.provider_job_id,
                )
            except SaladAPIError as error:
                if error.status_code != 404:
                    raise
                # A machine disappearing is not a job disappearing. Scan the queue
                # by durable submission identity before changing the attempt.
                remote = await find_i2v_submission(
                    self.salad_client,
                    queue_name=self.config.salad.queue_name,
                    submission_key=submission_key,
                )
                if remote is None:
                    if job.lease_owner != self.worker_id or attempt.worker_id != self.worker_id:
                        return _RESTART_RECONCILIATION_WAITING
                    return await self._record_provider_absence(job, attempt, now=now)
        else:
            state = attempt.request_metadata.get("submission_state")
            if state in {"unknown", "submitting"}:
                remote = await find_i2v_submission(
                    self.salad_client,
                    queue_name=self.config.salad.queue_name,
                    submission_key=submission_key,
                )
                if remote is None:
                    if _lease_expired(job, now=now):
                        return None
                    if job.lease_owner != self.worker_id or attempt.worker_id != self.worker_id:
                        return _RESTART_RECONCILIATION_WAITING
                    if job.state == I2VJobState.CANCEL_REQUESTED:
                        async with self.sessions() as session:
                            await acknowledge_i2v_cancellation(
                                session,
                                job_id=job.job_id,
                                attempt_id=attempt.attempt_id,
                                worker_id=self.worker_id,
                                now=now,
                            )
                        return "unsubmitted_cancellation_acknowledged"
                    return await self._submit_existing(job, attempt, now=now)
            elif state == "prepared":
                # A controller may have died after Salad accepted the POST but
                # before its provider ID was persisted. Search the deterministic
                # submission identity before any retry, including on the stable
                # singleton after a process restart.
                remote = await find_i2v_submission(
                    self.salad_client,
                    queue_name=self.config.salad.queue_name,
                    submission_key=submission_key,
                )
                if remote is None:
                    if job.lease_owner != self.worker_id or attempt.worker_id != self.worker_id:
                        if _lease_expired(job, now=now):
                            return None
                        return _RESTART_RECONCILIATION_WAITING
                    if _lease_expired(job, now=now):
                        return None
                    if job.state == I2VJobState.CANCEL_REQUESTED:
                        async with self.sessions() as session:
                            await acknowledge_i2v_cancellation(
                                session,
                                job_id=job.job_id,
                                attempt_id=attempt.attempt_id,
                                worker_id=self.worker_id,
                                now=now,
                            )
                        return "unsubmitted_cancellation_acknowledged"
                    return await self._submit_existing(job, attempt, now=now)
            else:
                # Legacy/malformed fresh-lane rows fail closed: first persist the
                # stable submission identity, then submit on a later cycle.
                if job.lease_owner != self.worker_id or attempt.worker_id != self.worker_id:
                    if _lease_expired(job, now=now):
                        return None
                    return _RESTART_RECONCILIATION_WAITING
                await self._record_attempt_metadata(
                    job,
                    attempt,
                    provider_job_id=None,
                    updates={
                        "schema": "i2v-runtime-attempt/v1",
                        "submission_key": submission_key,
                        "submission_state": "prepared",
                    },
                    now=now,
                )
                return "attempt_prepared"

        if remote is None:
            return None
        _validate_remote_identity(remote.metadata, job, attempt, submission_key)
        if attempt.provider_job_id is not None and attempt.provider_job_id != str(remote.id):
            raise I2VRuntimeError("Salad I2V job ID does not match the durable provider job")
        adopted = False
        owner_changed = job.lease_owner != self.worker_id or attempt.worker_id != self.worker_id
        provider_id_changed = attempt.provider_job_id != str(remote.id)
        if owner_changed or provider_id_changed:
            previous_worker_id = job.lease_owner
            if previous_worker_id is None or attempt.worker_id != previous_worker_id:
                raise I2VRuntimeError(
                    "provider-backed I2V attempt is not eligible for restart adoption"
                )
            if owner_changed and (
                self.worker_id != I2V_SINGLETON_WORKER_ID
                or previous_worker_id == I2V_SINGLETON_WORKER_ID
            ):
                # Only the configured singleton may migrate a legacy per-process
                # owner. A retired controller therefore cannot steal it back.
                return _RESTART_RECONCILIATION_WAITING
            async with self.sessions() as session:
                claim = await adopt_validated_i2v_provider_attempt(
                    session,
                    job_id=job.job_id,
                    attempt_id=attempt.attempt_id,
                    attempt_no=attempt.attempt_no,
                    provider_job_id=str(remote.id),
                    previous_provider_job_id=attempt.provider_job_id,
                    previous_worker_id=previous_worker_id,
                    worker_id=self.worker_id,
                    lease_duration=timedelta(seconds=self.config.salad.worker_lease_seconds),
                    now=now,
                )
            job, attempt = claim.job, claim.attempt
            adopted = owner_changed
        if job.lease_expires_at is None or job.lease_expires_at <= now:
            # Provider evidence wins over a stale controller lease. Refresh first,
            # then apply the provider transition next cycle. There is no execution
            # deadline for a pending pull or a running inference.
            await self._record_attempt_metadata(
                job,
                attempt,
                provider_job_id=str(remote.id),
                updates=_safe_attempt_metadata(
                    attempt,
                    submission_key=submission_key,
                    provider_job_id=str(remote.id),
                    provider_status=remote.status,
                    now=now,
                ),
                now=now,
            )
            return "provider_backed_lease_refreshed"
        if job.state == I2VJobState.CANCEL_REQUESTED:
            if remote.status in {
                SaladJobStatus.CANCELLED,
                SaladJobStatus.FAILED,
                SaladJobStatus.SUCCEEDED,
            }:
                async with self.sessions() as session:
                    await acknowledge_i2v_cancellation(
                        session,
                        job_id=job.job_id,
                        attempt_id=attempt.attempt_id,
                        worker_id=self.worker_id,
                        response_metadata={
                            "provider_status": remote.status.value,
                            "provider_job_id": str(remote.id),
                        },
                        now=now,
                    )
                return "cancellation_acknowledged"
            if attempt.request_metadata.get("cancel_requested_provider_job_id") != str(remote.id):
                await self.salad_client.cancel_job(
                    self.config.salad.queue_name,
                    remote.id,
                )
                await self._record_attempt_metadata(
                    job,
                    attempt,
                    provider_job_id=str(remote.id),
                    updates={
                        "cancel_requested_provider_job_id": str(remote.id),
                        "cancel_requested_at": now.isoformat(),
                    },
                    now=now,
                )
                return "provider_cancellation_requested"
            action = await self._observe_nonterminal(job, attempt, remote, now=now)
            return action or ("provider_attempt_adopted" if adopted else None)

        if remote.status == SaladJobStatus.PENDING:
            # Pending includes provider capacity, image pulling and container
            # startup. None of these starts the inference clock or a watchdog.
            action = await self._observe_nonterminal(job, attempt, remote, now=now)
            return action or ("provider_attempt_adopted" if adopted else None)
        if remote.status == SaladJobStatus.RUNNING:
            if attempt.state == I2VAttemptState.CREATED:
                async with self.sessions() as session:
                    await start_i2v_attempt(
                        session,
                        job_id=job.job_id,
                        attempt_id=attempt.attempt_id,
                        worker_id=self.worker_id,
                        provider_job_id=str(remote.id),
                        request_metadata=_safe_attempt_metadata(
                            attempt,
                            submission_key=submission_key,
                            provider_job_id=str(remote.id),
                            provider_status=remote.status,
                            now=now,
                        ),
                        now=now,
                    )
                return "inference_started"
            action = await self._observe_nonterminal(job, attempt, remote, now=now)
            return action or ("provider_attempt_adopted" if adopted else None)
        if remote.status == SaladJobStatus.SUCCEEDED:
            _validate_worker_result_identity(remote.output, job=job, attempt=attempt)
            output = parse_i2v_worker_output(remote.output)
            expected_output_key = (
                f"{self.config.output_prefix}/{job.job_id}/{attempt.attempt_id}.mp4"
            )
            if output.object_key != expected_output_key:
                raise I2VRuntimeError(
                    "I2V worker returned an output outside its deterministic grant"
                )
            if self.input_builder is None:
                raise I2VRuntimeConfigurationError("I2V output verifier is unavailable")
            await self.input_builder.verify_output(job=job, attempt=attempt, output=output)
            async with self.sessions() as session:
                await complete_i2v_job(
                    session,
                    job_id=job.job_id,
                    attempt_id=attempt.attempt_id,
                    worker_id=self.worker_id,
                    output=output,
                    response_metadata={
                        "provider_status": remote.status.value,
                        "provider_job_id": str(remote.id),
                    },
                    now=now,
                )
            return "output_completed"
        if remote.status in {SaladJobStatus.FAILED, SaladJobStatus.CANCELLED}:
            async with self.sessions() as session:
                await fail_i2v_attempt(
                    session,
                    job_id=job.job_id,
                    attempt_id=attempt.attempt_id,
                    worker_id=self.worker_id,
                    error_code=f"salad_{remote.status.value}",
                    error_detail=f"Salad reported {remote.status.value} for the I2V job.",
                    response_metadata={
                        "provider_status": remote.status.value,
                        "provider_job_id": str(remote.id),
                    },
                    now=now,
                )
            return "provider_terminal_failure"
        return "provider_attempt_adopted" if adopted else None

    async def _claim_and_submit(self, *, now: datetime) -> str | None:
        # Recover first, as required. Active provider attempts were reconciled by
        # the caller, preventing a slow pull or long inference from being requeued.
        async with self.sessions() as session:
            recovered = await recover_expired_i2v_jobs(session, now=now)
        if recovered:
            return "expired_claims_recovered"
        async with self.sessions() as session:
            claim = await claim_next_i2v_job(
                session,
                worker_id=self.worker_id,
                lease_duration=timedelta(seconds=self.config.salad.worker_lease_seconds),
                now=now,
            )
        if claim is None:
            return None
        submission_key = _submission_key(claim.job, claim.attempt)
        await self._record_attempt_metadata(
            claim.job,
            claim.attempt,
            provider_job_id=None,
            updates={
                "schema": "i2v-runtime-attempt/v1",
                "submission_key": submission_key,
                "submission_state": "prepared",
            },
            now=now,
        )
        # Submission is deliberately deferred to the next cycle: claiming is this
        # cycle's sole durable mutation and makes crash recovery deterministic.
        return "job_claimed"

    async def _submit_existing(
        self,
        job: I2VJobSnapshot,
        attempt: I2VAttemptSnapshot,
        *,
        now: datetime,
    ) -> str:
        if self.input_builder is None:
            raise I2VRuntimeConfigurationError(
                "production I2V submission requires a fresh signed-grant builder"
            )
        submission_key = _submission_key(job, attempt)
        additions = dict(await self.input_builder.build(job=job, attempt=attempt))
        _validate_fresh_grants(
            additions,
            job=job,
            attempt=attempt,
            output_prefix=self.config.output_prefix,
            now=now,
        )
        payload: JSONObject = {
            "schema": I2V_SALAD_JOB_SCHEMA,
            "job_id": str(job.job_id),
            "attempt_id": str(attempt.attempt_id),
            "request_sha256": job.request_sha256,
            "input_snapshot": cast(JSONValue, _worker_input_snapshot(job.input_snapshot)),
            "positive_prompt": job.positive_prompt,
            "negative_prompt": job.negative_prompt,
            "settings_snapshot": cast(JSONValue, job.settings_snapshot),
            **additions,
        }
        metadata = i2v_submission_metadata(
            job_id=job.job_id,
            attempt_id=attempt.attempt_id,
            submission_key=submission_key,
            request_sha256=job.request_sha256,
        )
        try:
            remote = await self.salad_client.create_job(
                self.config.salad.queue_name,
                input=payload,
                metadata=metadata,
            )
        except SaladRateLimitError:
            # Salad definitively rejected this request before accepting a job.
            return "submission_rate_limited"
        except SaladAPIError as error:
            if error.status_code in _DEFINITELY_REJECTED_STATUS_CODES:
                async with self.sessions() as session:
                    await fail_i2v_attempt(
                        session,
                        job_id=job.job_id,
                        attempt_id=attempt.attempt_id,
                        worker_id=self.worker_id,
                        error_code="salad_submission_rejected",
                        error_detail=(
                            f"Salad rejected the I2V request with HTTP {error.status_code}."
                        ),
                        now=now,
                    )
                return "submission_rejected"
            await self._mark_submission_unknown(job, attempt, submission_key, now=now)
            return "submission_unknown"
        except SaladTransportError:
            await self._mark_submission_unknown(job, attempt, submission_key, now=now)
            return "submission_unknown"
        _validate_remote_identity(remote.metadata, job, attempt, submission_key)
        await self._record_attempt_metadata(
            job,
            attempt,
            provider_job_id=str(remote.id),
            updates={
                "submission_state": "submitted",
                "provider_status": remote.status.value,
                "provider_observed_at": now.isoformat(),
                "provider_absence_confirmations": 0,
            },
            now=now,
        )
        return "provider_job_submitted"

    async def _mark_submission_unknown(
        self,
        job: I2VJobSnapshot,
        attempt: I2VAttemptSnapshot,
        submission_key: str,
        *,
        now: datetime,
    ) -> None:
        await self._record_attempt_metadata(
            job,
            attempt,
            provider_job_id=None,
            updates={
                "submission_key": submission_key,
                "submission_state": "unknown",
                "submission_unknown_at": now.isoformat(),
            },
            now=now,
        )

    async def _observe_nonterminal(
        self,
        job: I2VJobSnapshot,
        attempt: I2VAttemptSnapshot,
        remote: SaladQueueJob,
        *,
        now: datetime,
    ) -> str | None:
        provider_id = str(remote.id)
        provider_status = remote.status
        lease_refresh_at = now + timedelta(
            seconds=max(1, self.config.salad.worker_lease_seconds // 2)
        )
        if (
            attempt.provider_job_id == provider_id
            and attempt.request_metadata.get("provider_status") == provider_status.value
            and job.lease_expires_at is not None
            and job.lease_expires_at > lease_refresh_at
        ):
            return None
        metadata = _safe_attempt_metadata(
            attempt,
            submission_key=_submission_key(job, attempt),
            provider_job_id=provider_id,
            provider_status=provider_status,
            now=now,
        )
        await self._record_attempt_metadata(
            job,
            attempt,
            provider_job_id=provider_id,
            updates=metadata,
            now=now,
        )
        return (
            "provider_pending_observed"
            if provider_status == SaladJobStatus.PENDING
            else "provider_running_observed"
        )

    async def _record_provider_absence(
        self,
        job: I2VJobSnapshot,
        attempt: I2VAttemptSnapshot,
        *,
        now: datetime,
    ) -> str:
        confirmations = attempt.request_metadata.get("provider_absence_confirmations", 0)
        if not isinstance(confirmations, int) or isinstance(confirmations, bool):
            confirmations = 0
        await self._record_attempt_metadata(
            job,
            attempt,
            provider_job_id=attempt.provider_job_id,
            updates={
                "provider_absence_confirmations": confirmations + 1,
                "provider_absence_observed_at": now.isoformat(),
            },
            now=now,
        )
        # Do not resubmit a known provider job. Reallocation can temporarily make
        # instance/job observations disagree; keeping the attempt is safer than a
        # duplicate generation.
        return "provider_absence_observed"

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
                    I2VJob.lease_owner == self.worker_id,
                    I2VJob.state.in_(_ACTIVE_JOB_STATES),
                )
                .with_for_update()
            )
            if row is None or durable_job is None:
                raise I2VRuntimeError("I2V attempt changed concurrently")
            row.provider_job_id = provider_job_id
            row.request_metadata = metadata
            row.updated_at = now
            # A lease is crash-recovery metadata, not an execution deadline. Every
            # confirmed pending/running observation moves it forward indefinitely.
            durable_job.lease_expires_at = now + timedelta(
                seconds=self.config.salad.worker_lease_seconds
            )
            durable_job.updated_at = now
            await session.commit()

    async def _sync_deployment(
        self,
        observation: I2VProviderObservation,
        *,
        queued_count: int,
        active_count: int,
        now: datetime,
    ) -> bool:
        async with self.sessions() as session:
            existing = await session.scalar(
                select(I2VWorkerDeployment)
                .where(
                    I2VWorkerDeployment.provider == "salad",
                    I2VWorkerDeployment.provider_group_id == observation.group_id,
                )
                .with_for_update()
            )
            existing_metadata = dict(existing.deployment_metadata) if existing else {}
            idle_since = existing_metadata.get("idle_since")
            if queued_count > 0 or active_count > 0:
                idle_since = None
            elif not isinstance(idle_since, str):
                idle_since = now.isoformat()
            metadata: dict[str, JSONValue] = {
                "provider_status": observation.provider_status,
                "replicas": observation.replicas,
                "instance_state": observation.instance_state,
                "machine_id": observation.machine_id,
                "ready": observation.ready,
                "instances": list(observation.instances),
                "queued_job_count": queued_count,
                "active_job_count": active_count,
                "idle_since": idle_since,
            }
            changed = (
                existing is None
                or existing.provider_instance_id != observation.instance_id
                or existing.state != observation.state
                or existing.gpu_class != self.config.salad.gpu_class_name
                or existing.worker_image_digest != self.config.salad.worker_image
                or existing.deployment_metadata != metadata
            )
            await session.rollback()
        if not changed:
            return False
        async with self.sessions() as session:
            await record_i2v_worker_deployment(
                session,
                registration=I2VWorkerDeploymentRegistration(
                    provider="salad",
                    provider_group_id=observation.group_id,
                    provider_instance_id=observation.instance_id,
                    state=observation.state,
                    gpu_class=self.config.salad.gpu_class_name,
                    worker_image_digest=self.config.salad.worker_image,
                    last_heartbeat_at=now,
                    metadata=metadata,
                ),
                now=now,
            )
        return True

    async def _should_stop_warm_group(
        self,
        observation: I2VProviderObservation,
        *,
        now: datetime,
    ) -> bool:
        idle_seconds = self.config.salad.warm_idle_seconds
        if idle_seconds is None or observation.state != I2VWorkerDeploymentState.READY:
            return False
        async with self.sessions() as session:
            deployment = await session.scalar(
                select(I2VWorkerDeployment).where(
                    I2VWorkerDeployment.provider == "salad",
                    I2VWorkerDeployment.provider_group_id == observation.group_id,
                )
            )
            if deployment is None:
                return False
            idle_since = deployment.deployment_metadata.get("idle_since")
        if not isinstance(idle_since, str):
            return False
        try:
            idle_at = _as_utc(datetime.fromisoformat(idle_since))
        except ValueError:
            return False
        return now >= idle_at + timedelta(seconds=idle_seconds)


def _submission_key(job: I2VJobSnapshot, attempt: I2VAttemptSnapshot) -> str:
    stored = attempt.request_metadata.get("submission_key")
    calculated = canonical_sha256(
        {
            "schema": "i2v-salad-submission-key/v1",
            "job_id": str(job.job_id),
            "attempt_id": str(attempt.attempt_id),
            "attempt_no": attempt.attempt_no,
            "request_sha256": job.request_sha256,
        }
    )
    if stored is not None and stored != calculated:
        raise I2VRuntimeError("stored I2V submission identity is invalid")
    return calculated


def _lease_expired(job: I2VJobSnapshot, *, now: datetime) -> bool:
    return job.lease_expires_at is None or job.lease_expires_at <= now


def _validate_remote_identity(
    metadata: JSONObject,
    job: I2VJobSnapshot,
    attempt: I2VAttemptSnapshot,
    submission_key: str,
) -> None:
    expected = i2v_submission_metadata(
        job_id=job.job_id,
        attempt_id=attempt.attempt_id,
        submission_key=submission_key,
        request_sha256=job.request_sha256,
    )
    if any(metadata.get(key) != value for key, value in expected.items()):
        raise I2VRuntimeError("Salad I2V job identity does not match the durable attempt")


def _validate_worker_result_identity(
    value: JSONValue,
    *,
    job: I2VJobSnapshot,
    attempt: I2VAttemptSnapshot,
) -> None:
    if (
        not isinstance(value, dict)
        or value.get("schema") != "i2v-salad-result/v1"
        or value.get("job_id") != str(job.job_id)
        or value.get("attempt_id") != str(attempt.attempt_id)
        or value.get("request_sha256") != job.request_sha256
    ):
        raise I2VRuntimeError("I2V worker result identity does not match the durable attempt")


def _safe_attempt_metadata(
    attempt: I2VAttemptSnapshot,
    *,
    submission_key: str,
    provider_job_id: str,
    provider_status: SaladJobStatus,
    now: datetime,
) -> dict[str, JSONValue]:
    metadata = dict(attempt.request_metadata)
    metadata.update(
        {
            "schema": "i2v-runtime-attempt/v1",
            "submission_key": submission_key,
            "submission_state": "submitted",
            "provider_job_id": provider_job_id,
            "provider_status": provider_status.value,
            "provider_observed_at": now.isoformat(),
            "provider_absence_confirmations": 0,
        }
    )
    return cast(dict[str, JSONValue], metadata)


def _validate_fresh_grants(
    additions: Mapping[str, JSONValue],
    *,
    job: I2VJobSnapshot,
    attempt: I2VAttemptSnapshot,
    output_prefix: str,
    now: datetime,
) -> None:
    if set(additions) != {"input_grant", "output_grant"}:
        raise I2VRuntimeConfigurationError(
            "I2V input builder must return only fresh input_grant and output_grant"
        )
    input_grant = additions.get("input_grant")
    output_grant = additions.get("output_grant")
    if not isinstance(input_grant, dict) or input_grant.get("method") != "GET":
        raise I2VRuntimeConfigurationError("I2V input grant must be a GET grant")
    if not isinstance(output_grant, dict) or output_grant.get("method") != "PUT":
        raise I2VRuntimeConfigurationError("I2V output grant must be a PUT grant")
    for label, grant in (("input", input_grant), ("output", output_grant)):
        url = grant.get("url")
        expires_at = grant.get("expires_at")
        if not isinstance(url, str) or urlsplit(url).scheme != "https":
            raise I2VRuntimeConfigurationError(f"I2V {label} grant must use HTTPS")
        if not isinstance(expires_at, str):
            raise I2VRuntimeConfigurationError(f"I2V {label} grant expiry is missing")
        try:
            expiry = _as_utc(datetime.fromisoformat(expires_at.replace("Z", "+00:00")))
        except ValueError as error:
            raise I2VRuntimeConfigurationError(f"I2V {label} grant expiry is invalid") from error
        if expiry <= now:
            raise I2VRuntimeConfigurationError(f"I2V {label} grant is already expired")
    expected_key = f"{output_prefix}/{job.job_id}/{attempt.attempt_id}.mp4"
    if output_grant.get("object_key") != expected_key:
        raise I2VRuntimeConfigurationError("I2V output grant object key is not deterministic")
    headers = output_grant.get("headers")
    if not isinstance(headers, dict) or headers.get("Content-Type") != "video/mp4":
        raise I2VRuntimeConfigurationError("I2V output grant must bind video/mp4")
    for field_name in ("storage_backend", "storage_bucket"):
        if not isinstance(output_grant.get(field_name), str) or not output_grant[field_name]:
            raise I2VRuntimeConfigurationError(f"I2V output grant {field_name} is missing")


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
        raise I2VRuntimeConfigurationError("durable I2V input snapshot is invalid")
    return normalized


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


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _optional_utc(value: datetime | None) -> datetime | None:
    return _as_utc(value) if value is not None else None
