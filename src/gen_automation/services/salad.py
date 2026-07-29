from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from re import fullmatch
from typing import Any, Protocol
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from gen_automation.db.models import (
    AuditEvent,
    GenerationAttempt,
    GenerationJob,
    OutboxEvent,
    ProviderBudgetGuard,
    SaladDeployment,
)
from gen_automation.domain.canonical import canonical_sha256
from gen_automation.domain.enums import (
    DesiredDeploymentState,
    GenerationAttemptState,
    GenerationState,
    SaladDeploymentState,
)
from gen_automation.domain.ids import uuid7
from gen_automation.integrations.salad.errors import (
    SaladAPIError,
    SaladProtocolError,
    SaladRateLimitError,
    SaladTransportError,
)
from gen_automation.integrations.salad.models import (
    JSONObject,
    JSONValue,
    SaladJobStatus,
    SaladQueueJob,
    SaladQueueJobPage,
)
from gen_automation.services.budgets import (
    release_attempt_reservation,
    reserve_attempt_budget,
)
from gen_automation.services.outbox import (
    GENERATION_ATTEMPT_AGGREGATE,
    SALAD_JOB_SUBMIT_TOPIC,
    enqueue_outbox_event,
)

_PROVIDER = "salad"
_IMAGE_DIGEST_PATTERN = r".+@sha256:[0-9a-f]{64}"
_DEFINITIVE_REJECTION_STATUS_CODES = frozenset({400, 401, 403, 404, 422})
_SECRET_KEY_MARKERS = frozenset(
    {
        "secret",
        "token",
        "password",
        "passwd",
        "apikey",
        "accesskey",
        "privatekey",
        "credential",
        "authorization",
    }
)
_TERMINAL_ATTEMPT_STATES = frozenset(
    {
        GenerationAttemptState.SUCCEEDED,
        GenerationAttemptState.FAILED,
        GenerationAttemptState.CANCELLED,
    }
)
_TERMINAL_JOB_STATES = frozenset(
    {
        GenerationState.SUCCEEDED,
        GenerationState.FAILED,
        GenerationState.DEAD_LETTER,
        GenerationState.CANCELLED,
    }
)
_PREPARABLE_JOB_STATES = frozenset(
    {
        GenerationState.QUEUED,
        GenerationState.RETRY_WAIT,
    }
)
_BLOCKING_ATTEMPT_STATES = frozenset(
    {
        GenerationAttemptState.CREATED,
        GenerationAttemptState.SUBMITTING,
        GenerationAttemptState.SUBMITTED,
        GenerationAttemptState.RUNNING,
        GenerationAttemptState.UNKNOWN,
        GenerationAttemptState.CANCEL_REQUESTED,
    }
)
_ATTEMPT_STATE_RANK = {
    GenerationAttemptState.CREATED: 0,
    GenerationAttemptState.SUBMITTING: 1,
    GenerationAttemptState.SUBMITTED: 2,
    GenerationAttemptState.RUNNING: 3,
    GenerationAttemptState.SUCCEEDED: 4,
    GenerationAttemptState.FAILED: 4,
    GenerationAttemptState.CANCELLED: 4,
}


class SaladServiceError(Exception):
    """Base error for durable Salad orchestration."""


class SaladServiceValidationError(SaladServiceError):
    pass


class SaladServiceNotFoundError(SaladServiceError):
    pass


class SaladServiceConflictError(SaladServiceError):
    pass


class MutationEffect(StrEnum):
    DEFINITELY_NOT_STARTED = "definitely_not_started"
    CONFIRMED = "confirmed"
    MAY_HAVE_STARTED = "may_have_started"


class SubmissionDisposition(StrEnum):
    SUBMITTED = "submitted"
    ALREADY_RECORDED = "already_recorded"
    BUDGET_BLOCKED = "budget_blocked"
    RETRY_WAIT = "retry_wait"
    FAILED = "failed"
    UNKNOWN = "unknown"


class ReconciliationSource(StrEnum):
    NONE = "none"
    GET = "get"
    LIST = "list"


@dataclass(frozen=True)
class SaladDeploymentConfig:
    organization_name: str
    project_name: str
    queue_name: str
    container_group_name: str
    worker_image_digest: str
    max_hourly_cost_microusd: int
    provider_configuration: Mapping[str, JSONValue]
    min_replicas: int = 0
    max_replicas: int = 1
    desired_queue_length: int = 1

    def __post_init__(self) -> None:
        for name, value, max_length in (
            ("organization_name", self.organization_name, 200),
            ("project_name", self.project_name, 200),
            ("queue_name", self.queue_name, 200),
            ("container_group_name", self.container_group_name, 200),
        ):
            _validate_name(value, name=name, max_length=max_length)
        if fullmatch(_IMAGE_DIGEST_PATTERN, self.worker_image_digest) is None:
            raise SaladServiceValidationError(
                "worker_image_digest must be an immutable sha256 image reference"
            )
        if self.min_replicas < 0 or self.max_replicas < self.min_replicas:
            raise SaladServiceValidationError("deployment replica range is invalid")
        if self.max_replicas > 1:
            raise SaladServiceValidationError("v1 supports at most one Salad replica")
        if self.desired_queue_length <= 0:
            raise SaladServiceValidationError("desired_queue_length must be positive")
        if self.max_hourly_cost_microusd <= 0:
            raise SaladServiceValidationError("max_hourly_cost_microusd must be positive")

        _validate_provider_configuration(self.provider_configuration)

    def canonical_value(self) -> dict[str, Any]:
        return {
            "organization_name": self.organization_name,
            "project_name": self.project_name,
            "queue_name": self.queue_name,
            "container_group_name": self.container_group_name,
            "worker_image_digest": self.worker_image_digest,
            "max_hourly_cost_microusd": self.max_hourly_cost_microusd,
            "provider_configuration": dict(self.provider_configuration),
            "min_replicas": self.min_replicas,
            "max_replicas": self.max_replicas,
            "desired_queue_length": self.desired_queue_length,
        }

    @property
    def config_sha256(self) -> str:
        provider_configuration = deepcopy(dict(self.provider_configuration))
        _validate_provider_configuration(provider_configuration)
        value = self.canonical_value()
        value["provider_configuration"] = provider_configuration
        return canonical_sha256(value)


@dataclass(frozen=True)
class DeploymentVersionResult:
    deployment_id: UUID
    version_no: int
    config_sha256: str
    replayed: bool


@dataclass(frozen=True)
class PreparedAttempt:
    generation_attempt_id: UUID
    generation_job_id: UUID
    outbox_event_id: UUID
    attempt_no: int
    submission_key: str
    request_sha256: str
    replayed: bool


@dataclass(frozen=True)
class SaladJobInputContext:
    generation_attempt_id: UUID
    generation_job_id: UUID
    release_version_id: UUID
    salad_deployment_id: UUID
    deployment_version_no: int
    worker_image_digest: str
    expected_output_count: int
    parameters: Mapping[str, Any]
    parameters_sha256: str
    request_sha256: str


class SaladUploadIntentProvider(Protocol):
    async def build_job_input(self, context: SaladJobInputContext) -> JSONValue:
        """Issue short-lived upload grants and return the complete worker input."""


class SaladQueueClient(Protocol):
    async def create_job(
        self,
        queue_name: str,
        *,
        input: JSONValue,
        metadata: Mapping[str, JSONValue] | None = None,
        webhook: str | None = None,
    ) -> SaladQueueJob: ...

    async def get_job(self, queue_name: str, job_id: UUID | str) -> SaladQueueJob: ...

    async def list_jobs(
        self,
        queue_name: str,
        *,
        page: int = 1,
        page_size: int = 50,
    ) -> SaladQueueJobPage: ...


@dataclass(frozen=True)
class SubmissionResult:
    generation_attempt_id: UUID
    attempt_state: GenerationAttemptState
    generation_job_state: GenerationState
    disposition: SubmissionDisposition
    mutation_effect: MutationEffect
    provider_external_id: str | None
    retry_not_before: datetime | None = None


@dataclass(frozen=True)
class ObservationResult:
    generation_attempt_id: UUID
    attempt_state: GenerationAttemptState
    generation_job_state: GenerationState
    provider_external_id: str | None
    applied: bool
    stale: bool


@dataclass(frozen=True)
class ReconciliationResult:
    observation: ObservationResult
    source: ReconciliationSource
    matched: bool
    error_code: str | None = None


def deployment_config_sha256(config: SaladDeploymentConfig) -> str:
    return config.config_sha256


async def create_deployment_version(
    session: AsyncSession,
    config: SaladDeploymentConfig,
    *,
    actor: str = "controller",
    now: datetime | None = None,
) -> DeploymentVersionResult:
    """Create the next immutable deployment version in the caller's transaction."""

    created_at = _as_utc(now or datetime.now(UTC))
    provider_configuration = deepcopy(dict(config.provider_configuration))
    _validate_provider_configuration(provider_configuration)
    config_value = config.canonical_value()
    config_value["provider_configuration"] = provider_configuration
    config_hash = canonical_sha256(config_value)
    current = await session.scalar(
        select(SaladDeployment).where(SaladDeployment.is_current.is_(True)).with_for_update()
    )
    if current is not None and current.config_sha256 == config_hash:
        return DeploymentVersionResult(
            deployment_id=current.id,
            version_no=current.version_no,
            config_sha256=current.config_sha256,
            replayed=True,
        )

    latest_version = await session.scalar(select(func.max(SaladDeployment.version_no)))
    version_no = int(latest_version or 0) + 1
    if current is not None:
        current.is_current = False
        current.desired_state = DesiredDeploymentState.STOPPED
        current.reconcile_after = created_at
        current.last_error_code = "superseded_by_new_deployment"
        current.last_error_detail = "A newer immutable Salad deployment version became current."
        current.lock_version += 1
        session.add(
            AuditEvent(
                actor=_validate_name(actor, name="actor", max_length=200),
                action="salad.deployment_version_superseded",
                resource_type="salad_deployment",
                resource_id=current.id,
                correlation_id=f"salad-deployment:{current.id}",
                detail={"replacement_version_no": version_no},
                occurred_at=created_at,
            )
        )
        await session.flush()

    deployment = SaladDeployment(
        id=uuid7(),
        version_no=version_no,
        config_sha256=config_hash,
        worker_image_digest=config.worker_image_digest,
        organization_name=config.organization_name,
        project_name=config.project_name,
        queue_name=config.queue_name,
        provider_configuration=provider_configuration,
        container_group_name=config.container_group_name,
        state=SaladDeploymentState.PLANNED,
        desired_state=DesiredDeploymentState.ACTIVE,
        is_current=True,
        min_replicas=config.min_replicas,
        max_replicas=config.max_replicas,
        desired_queue_length=config.desired_queue_length,
        max_hourly_cost_microusd=config.max_hourly_cost_microusd,
        lock_version=1,
    )
    session.add(deployment)
    session.add(
        AuditEvent(
            actor=_validate_name(actor, name="actor", max_length=200),
            action="salad.deployment_version_created",
            resource_type="salad_deployment",
            resource_id=deployment.id,
            correlation_id=f"salad-deployment:{deployment.id}",
            detail={
                "version_no": version_no,
                "config_sha256": config_hash,
                "max_replicas": config.max_replicas,
            },
            occurred_at=created_at,
        )
    )
    await session.flush()
    return DeploymentVersionResult(
        deployment_id=deployment.id,
        version_no=deployment.version_no,
        config_sha256=deployment.config_sha256,
        replayed=False,
    )


async def prepare_generation_attempt(
    session: AsyncSession,
    *,
    generation_job_id: UUID,
    salad_deployment_id: UUID,
    idempotency_key: str,
    actor: str = "controller",
    now: datetime | None = None,
) -> PreparedAttempt:
    """Prepare an attempt and its stable-ID-only outbox event atomically."""

    prepared_at = _as_utc(now or datetime.now(UTC))
    normalized_key = _validate_name(
        idempotency_key,
        name="idempotency_key",
        max_length=200,
    )
    normalized_actor = _validate_name(actor, name="actor", max_length=200)
    row = (
        await session.execute(
            select(GenerationJob, SaladDeployment)
            .join(
                SaladDeployment,
                SaladDeployment.id == salad_deployment_id,
            )
            .where(GenerationJob.id == generation_job_id)
            .with_for_update()
        )
    ).one_or_none()
    if row is None:
        raise SaladServiceNotFoundError("generation job or Salad deployment was not found")
    job, deployment = row
    if canonical_sha256(job.parameters) != job.parameters_sha256:
        raise SaladServiceConflictError("generation job parameters do not match their digest")

    submission_key = canonical_sha256(
        {
            "provider": _PROVIDER,
            "generation_job_id": str(job.id),
            "idempotency_key": normalized_key,
        }
    )
    request_sha256 = canonical_sha256(
        {
            "generation_job_id": str(job.id),
            "release_version_id": str(job.release_version_id),
            "parameters_sha256": job.parameters_sha256,
            "salad_deployment_id": str(deployment.id),
            "deployment_config_sha256": deployment.config_sha256,
            "worker_image_digest": deployment.worker_image_digest,
        }
    )
    existing = await session.scalar(
        select(GenerationAttempt).where(
            GenerationAttempt.provider == _PROVIDER,
            GenerationAttempt.submission_key == submission_key,
        )
    )
    if existing is not None:
        if (
            existing.job_id != job.id
            or existing.salad_deployment_id != deployment.id
            or existing.request_sha256 != request_sha256
        ):
            raise SaladServiceConflictError("attempt idempotency key conflicts with stored request")
        event = await session.scalar(
            select(OutboxEvent).where(
                OutboxEvent.topic == SALAD_JOB_SUBMIT_TOPIC,
                OutboxEvent.aggregate_id == existing.id,
            )
        )
        if event is None:
            raise SaladServiceConflictError("prepared attempt is missing its outbox event")
        return PreparedAttempt(
            generation_attempt_id=existing.id,
            generation_job_id=existing.job_id,
            outbox_event_id=event.id,
            attempt_no=existing.attempt_no,
            submission_key=existing.submission_key,
            request_sha256=existing.request_sha256,
            replayed=True,
        )

    _require_submittable_deployment(deployment)
    blocking_attempt_id = await session.scalar(
        select(GenerationAttempt.id).where(
            GenerationAttempt.job_id == job.id,
            GenerationAttempt.state.in_(_BLOCKING_ATTEMPT_STATES),
        )
    )
    if blocking_attempt_id is not None:
        raise SaladServiceConflictError("generation job already has a non-terminal Salad attempt")
    if job.state not in _PREPARABLE_JOB_STATES:
        raise SaladServiceConflictError(
            "generation job state does not permit another Salad attempt"
        )
    if job.attempt_count >= job.max_attempts:
        raise SaladServiceConflictError("generation job has exhausted its attempt limit")

    attempt = GenerationAttempt(
        id=uuid7(),
        job_id=job.id,
        salad_deployment_id=deployment.id,
        attempt_no=job.attempt_count + 1,
        provider=_PROVIDER,
        submission_key=submission_key,
        request_sha256=request_sha256,
        state=GenerationAttemptState.CREATED,
        worker_image_digest=deployment.worker_image_digest,
        request_metadata={
            "generation_job_id": str(job.id),
            "release_version_id": str(job.release_version_id),
            "salad_deployment_id": str(deployment.id),
            "deployment_version_no": deployment.version_no,
            "parameters_sha256": job.parameters_sha256,
            "deployment_config_sha256": deployment.config_sha256,
        },
        cost_reservation_microusd=0,
        lock_version=1,
        created_at=prepared_at,
    )
    session.add(attempt)
    await session.flush()

    stable_payload = {
        "generation_attempt_id": str(attempt.id),
        "generation_job_id": str(job.id),
        "salad_deployment_id": str(deployment.id),
    }
    enqueue_result = await enqueue_outbox_event(
        session,
        topic=SALAD_JOB_SUBMIT_TOPIC,
        dedupe_key=f"{SALAD_JOB_SUBMIT_TOPIC}:{attempt.id}",
        correlation_id=str(attempt.id),
        aggregate_type=GENERATION_ATTEMPT_AGGREGATE,
        aggregate_id=attempt.id,
        payload=stable_payload,
        actor=normalized_actor,
        now=prepared_at,
    )
    job.attempt_count = attempt.attempt_no
    job.state = GenerationState.CLAIMED
    job.retry_at = None
    job.last_error_code = None
    job.last_error_detail = None
    job.lock_version += 1
    session.add(
        AuditEvent(
            actor=normalized_actor,
            action="generation_attempt.prepared",
            resource_type="generation_attempt",
            resource_id=attempt.id,
            correlation_id=str(attempt.id),
            detail={
                "generation_job_id": str(job.id),
                "salad_deployment_id": str(deployment.id),
                "attempt_no": attempt.attempt_no,
                "request_sha256": request_sha256,
                "outbox_event_id": str(enqueue_result.event_id),
            },
            occurred_at=prepared_at,
        )
    )
    await session.flush()
    return PreparedAttempt(
        generation_attempt_id=attempt.id,
        generation_job_id=job.id,
        outbox_event_id=enqueue_result.event_id,
        attempt_no=attempt.attempt_no,
        submission_key=attempt.submission_key,
        request_sha256=attempt.request_sha256,
        replayed=False,
    )


async def submit_prepared_attempt(
    session: AsyncSession,
    client: SaladQueueClient,
    upload_intent_provider: SaladUploadIntentProvider,
    *,
    generation_attempt_id: UUID,
    webhook_url: str,
    reservation_microusd: int,
    provider_post_started: Callable[[], None] | None = None,
    now: datetime | None = None,
) -> SubmissionResult:
    """Reserve budget, issue upload grants, and attempt exactly one Salad POST."""

    submitted_at = _as_utc(now or datetime.now(UTC))
    _validate_webhook_url(webhook_url)
    attempt, job, deployment = await _load_attempt_context(
        session,
        generation_attempt_id,
        lock=True,
    )
    _require_attempt_deployment(attempt, deployment)

    if attempt.state == GenerationAttemptState.SUBMITTING:
        await _mark_outcome_unknown(
            session,
            attempt=attempt,
            job=job,
            error_code="submit_resumed_after_durable_marker",
            occurred_at=submitted_at,
        )
        await session.commit()
        return _submission_result(
            attempt,
            job,
            SubmissionDisposition.UNKNOWN,
            MutationEffect.MAY_HAVE_STARTED,
        )
    if attempt.state == GenerationAttemptState.UNKNOWN:
        result = _submission_result(
            attempt,
            job,
            SubmissionDisposition.UNKNOWN,
            MutationEffect.MAY_HAVE_STARTED,
        )
        await session.rollback()
        return result
    if attempt.state != GenerationAttemptState.CREATED:
        result = _submission_result(
            attempt,
            job,
            SubmissionDisposition.ALREADY_RECORDED,
            MutationEffect.CONFIRMED,
        )
        await session.rollback()
        return result

    _require_submittable_deployment(deployment)
    decision = await reserve_attempt_budget(
        session,
        provider=_PROVIDER,
        attempt_id=attempt.id,
        amount_microusd=reservation_microusd,
        now=submitted_at,
    )
    if not decision.accepted:
        job.state = GenerationState.RETRY_WAIT
        job.last_error_code = "provider_budget_blocked"
        job.last_error_detail = "The Salad budget guard rejected this submission."
        job.lock_version += 1
        await session.commit()
        return _submission_result(
            attempt,
            job,
            SubmissionDisposition.BUDGET_BLOCKED,
            MutationEffect.DEFINITELY_NOT_STARTED,
        )

    job.state = GenerationState.SUBMITTING
    job.last_error_code = None
    job.last_error_detail = None
    job.lock_version += 1
    # This durable marker is deliberately committed before any signed grant is
    # issued or any mutation is sent to Salad.
    await session.commit()

    context = SaladJobInputContext(
        generation_attempt_id=attempt.id,
        generation_job_id=job.id,
        release_version_id=job.release_version_id,
        salad_deployment_id=deployment.id,
        deployment_version_no=deployment.version_no,
        worker_image_digest=attempt.worker_image_digest,
        expected_output_count=job.expected_output_count,
        parameters=dict(job.parameters),
        parameters_sha256=job.parameters_sha256,
        request_sha256=attempt.request_sha256,
    )
    try:
        job_input = await upload_intent_provider.build_job_input(context)
    except Exception:
        await session.rollback()
        attempt, job, _ = await _load_attempt_context(
            session,
            generation_attempt_id,
            lock=True,
        )
        await _mark_definite_failure(
            session,
            attempt=attempt,
            job=job,
            error_code="upload_intent_preparation_failed",
            safe_error_detail="Short-lived worker upload grants could not be prepared.",
            job_state=_retry_or_fail(job),
            occurred_at=submitted_at,
        )
        await session.commit()
        return _submission_result(
            attempt,
            job,
            SubmissionDisposition.RETRY_WAIT,
            MutationEffect.DEFINITELY_NOT_STARTED,
        )

    metadata: JSONObject = {
        "generation_attempt_id": str(attempt.id),
        "generation_job_id": str(job.id),
        "submission_key": attempt.submission_key,
        "request_sha256": attempt.request_sha256,
        "deployment_version_no": deployment.version_no,
    }
    if provider_post_started is not None:
        provider_post_started()
    try:
        remote_job = await client.create_job(
            deployment.queue_name,
            input=job_input,
            metadata=metadata,
            webhook=webhook_url,
        )
    except SaladRateLimitError as error:
        retry_delay = (
            max(error.retry_after_seconds, 1.0) if error.retry_after_seconds is not None else 60.0
        )
        retry_at = submitted_at + timedelta(seconds=retry_delay)
        attempt, job, _ = await _load_attempt_context(
            session,
            generation_attempt_id,
            lock=True,
        )
        await _mark_definite_failure(
            session,
            attempt=attempt,
            job=job,
            error_code="salad_rate_limited",
            safe_error_detail="Salad rejected the request before creating a job.",
            job_state=GenerationState.RETRY_WAIT,
            occurred_at=submitted_at,
            retry_at=retry_at,
        )
        await session.commit()
        return _submission_result(
            attempt,
            job,
            SubmissionDisposition.RETRY_WAIT,
            MutationEffect.DEFINITELY_NOT_STARTED,
            retry_not_before=retry_at,
        )
    except SaladAPIError as error:
        attempt, job, _ = await _load_attempt_context(
            session,
            generation_attempt_id,
            lock=True,
        )
        if error.status_code in _DEFINITIVE_REJECTION_STATUS_CODES:
            await _mark_definite_failure(
                session,
                attempt=attempt,
                job=job,
                error_code=f"salad_http_{error.status_code}",
                safe_error_detail="Salad rejected the job submission.",
                job_state=GenerationState.FAILED,
                occurred_at=submitted_at,
            )
            await session.commit()
            return _submission_result(
                attempt,
                job,
                SubmissionDisposition.FAILED,
                MutationEffect.DEFINITELY_NOT_STARTED,
            )
        await _mark_outcome_unknown(
            session,
            attempt=attempt,
            job=job,
            error_code="salad_submit_outcome_unknown",
            occurred_at=submitted_at,
        )
        await session.commit()
        return _submission_result(
            attempt,
            job,
            SubmissionDisposition.UNKNOWN,
            MutationEffect.MAY_HAVE_STARTED,
        )
    except (SaladTransportError, SaladProtocolError):
        attempt, job, _ = await _load_attempt_context(
            session,
            generation_attempt_id,
            lock=True,
        )
        await _mark_outcome_unknown(
            session,
            attempt=attempt,
            job=job,
            error_code="salad_submit_outcome_unknown",
            occurred_at=submitted_at,
        )
        await session.commit()
        return _submission_result(
            attempt,
            job,
            SubmissionDisposition.UNKNOWN,
            MutationEffect.MAY_HAVE_STARTED,
        )
    except Exception:
        # Once create_job has been invoked, even an unexpected adapter failure
        # cannot establish that Salad did not accept the mutation.
        attempt, job, _ = await _load_attempt_context(
            session,
            generation_attempt_id,
            lock=True,
        )
        await _mark_outcome_unknown(
            session,
            attempt=attempt,
            job=job,
            error_code="salad_submit_outcome_unknown",
            occurred_at=submitted_at,
        )
        await session.commit()
        return _submission_result(
            attempt,
            job,
            SubmissionDisposition.UNKNOWN,
            MutationEffect.MAY_HAVE_STARTED,
        )

    attempt, job, _ = await _load_attempt_context(
        session,
        generation_attempt_id,
        lock=True,
    )
    if not _remote_metadata_matches(attempt, remote_job):
        await _mark_outcome_unknown(
            session,
            attempt=attempt,
            job=job,
            error_code="salad_submit_response_identity_mismatch",
            occurred_at=submitted_at,
        )
        await session.commit()
        return _submission_result(
            attempt,
            job,
            SubmissionDisposition.UNKNOWN,
            MutationEffect.MAY_HAVE_STARTED,
        )

    observation = await apply_salad_job_observation(
        session,
        generation_attempt_id=attempt.id,
        remote_job=remote_job,
        observed_at=submitted_at,
    )
    await session.commit()
    return SubmissionResult(
        generation_attempt_id=attempt.id,
        attempt_state=observation.attempt_state,
        generation_job_state=observation.generation_job_state,
        disposition=SubmissionDisposition.SUBMITTED,
        mutation_effect=MutationEffect.CONFIRMED,
        provider_external_id=observation.provider_external_id,
    )


async def fail_definitely_unstarted_submission(
    session: AsyncSession,
    *,
    generation_attempt_id: UUID,
    error_code: str,
    now: datetime | None = None,
) -> SubmissionResult:
    """Resolve a controller interruption that occurred before the provider POST.

    The caller must have independent proof that ``create_job`` was never entered.
    This function deliberately does not commit so attempt/job state, reservation
    release, and outbox acknowledgement can share one transaction.
    """

    occurred_at = _as_utc(now or datetime.now(UTC))
    attempt, job, _ = await _load_attempt_context(
        session,
        generation_attempt_id,
        lock=True,
    )
    if attempt.provider_external_id is not None or attempt.state not in {
        GenerationAttemptState.CREATED,
        GenerationAttemptState.SUBMITTING,
        GenerationAttemptState.FAILED,
    }:
        raise SaladServiceConflictError("generation attempt cannot be proven unstarted")
    if attempt.state != GenerationAttemptState.FAILED:
        await _mark_definite_failure(
            session,
            attempt=attempt,
            job=job,
            error_code=error_code,
            safe_error_detail="The controller stopped before sending the Salad job request.",
            job_state=_retry_or_fail(job),
            occurred_at=occurred_at,
            retry_at=occurred_at,
        )
    elif attempt.reservation_released_at is None and attempt.cost_reservation_microusd > 0:
        await release_attempt_reservation(
            session,
            provider=_PROVIDER,
            attempt_id=attempt.id,
            now=occurred_at,
        )
    return _submission_result(
        attempt,
        job,
        SubmissionDisposition.RETRY_WAIT,
        MutationEffect.DEFINITELY_NOT_STARTED,
        retry_not_before=occurred_at,
    )


async def apply_salad_job_observation(
    session: AsyncSession,
    *,
    generation_attempt_id: UUID,
    remote_job: SaladQueueJob,
    observed_at: datetime | None = None,
) -> ObservationResult:
    """Apply one trusted Salad observation without committing the caller's transaction."""

    controller_time = _as_utc(observed_at or datetime.now(UTC))
    attempt, job, _ = await _load_attempt_context(
        session,
        generation_attempt_id,
        lock=True,
    )
    remote_id = str(remote_job.id)
    if attempt.provider_external_id is not None and attempt.provider_external_id != remote_id:
        raise SaladServiceConflictError("Salad observation has another provider job ID")

    provider_update_time = _as_utc(remote_job.update_time)
    last_observed_at = (
        _stored_as_utc(attempt.last_observed_at) if attempt.last_observed_at is not None else None
    )
    target_state = _attempt_state(remote_job.status)
    if (
        attempt.state != GenerationAttemptState.UNKNOWN
        and last_observed_at is not None
        and provider_update_time <= last_observed_at
    ):
        return _observation_result(attempt, job, applied=False, stale=True)

    if attempt.state in _TERMINAL_ATTEMPT_STATES:
        if attempt.state != target_state:
            attempt.error_code = "salad_terminal_state_conflict"
            attempt.error_detail = "A later provider observation conflicted with a terminal state."
            attempt.lock_version += 1
            session.add(
                _audit(
                    action="generation_attempt.terminal_observation_ignored",
                    attempt=attempt,
                    detail={"provider_status": remote_job.status.value},
                    occurred_at=controller_time,
                )
            )
            await session.flush()
            return _observation_result(attempt, job, applied=False, stale=False)

    if not _is_forward_transition(attempt.state, target_state):
        return _observation_result(attempt, job, applied=False, stale=True)

    attempt.provider_external_id = remote_id
    attempt.provider_state = remote_job.status.value
    attempt.last_observed_at = provider_update_time
    attempt.response_metadata = {
        "provider_status": remote_job.status.value,
        "provider_create_time": _as_utc(remote_job.create_time).isoformat(),
        "provider_update_time": provider_update_time.isoformat(),
        "event_count": len(remote_job.events),
    }
    attempt.error_code = None
    attempt.error_detail = None
    attempt.unknown_since = None
    attempt.submitted_at = attempt.submitted_at or _as_utc(remote_job.create_time)
    attempt.state = target_state
    if target_state == GenerationAttemptState.RUNNING:
        attempt.started_at = attempt.started_at or provider_update_time
    if target_state in _TERMINAL_ATTEMPT_STATES:
        attempt.completed_at = attempt.completed_at or provider_update_time
    attempt.lock_version += 1

    if job.state not in _TERMINAL_JOB_STATES:
        job.state = _job_state_for_observation(job, target_state)
        job.retry_at = None
        job.last_error_code = (
            "salad_job_failed" if target_state == GenerationAttemptState.FAILED else None
        )
        job.last_error_detail = (
            "Salad reported a failed queue job."
            if target_state == GenerationAttemptState.FAILED
            else None
        )
        job.lock_version += 1

    session.add(
        _audit(
            action="generation_attempt.provider_observed",
            attempt=attempt,
            detail={
                "provider_external_id": remote_id,
                "provider_status": remote_job.status.value,
            },
            occurred_at=controller_time,
        )
    )
    if (
        target_state in _TERMINAL_ATTEMPT_STATES
        and attempt.cost_reservation_microusd > 0
        and attempt.reservation_released_at is None
    ):
        await release_attempt_reservation(
            session,
            provider=_PROVIDER,
            attempt_id=attempt.id,
            now=controller_time,
        )
    await session.flush()
    return _observation_result(attempt, job, applied=True, stale=False)


async def reconcile_generation_attempt(
    session: AsyncSession,
    client: SaladQueueClient,
    *,
    generation_attempt_id: UUID,
    max_list_pages: int = 5,
    list_page_size: int = 100,
    now: datetime | None = None,
) -> ReconciliationResult:
    """Resolve a provider job by ID, or by stable metadata after an unknown POST."""

    reconciled_at = _as_utc(now or datetime.now(UTC))
    if max_list_pages <= 0 or max_list_pages > 100:
        raise SaladServiceValidationError("max_list_pages must be between 1 and 100")
    if list_page_size <= 0 or list_page_size > 100:
        raise SaladServiceValidationError("list_page_size must be between 1 and 100")

    attempt, job, deployment = await _load_attempt_context(
        session,
        generation_attempt_id,
        lock=False,
    )
    if attempt.state in _TERMINAL_ATTEMPT_STATES:
        result = ReconciliationResult(
            observation=_observation_result(attempt, job, applied=False, stale=False),
            source=ReconciliationSource.NONE,
            matched=True,
        )
        await session.rollback()
        return result

    source = ReconciliationSource.GET
    try:
        if attempt.provider_external_id is not None:
            remote_job = await client.get_job(
                deployment.queue_name,
                attempt.provider_external_id,
            )
        else:
            source = ReconciliationSource.LIST
            matches: dict[UUID, SaladQueueJob] = {}
            for page_number in range(1, max_list_pages + 1):
                page = await client.list_jobs(
                    deployment.queue_name,
                    page=page_number,
                    page_size=list_page_size,
                )
                for candidate in page.items:
                    if _remote_metadata_matches(attempt, candidate):
                        matches[candidate.id] = candidate
                if len(page.items) < list_page_size:
                    break
            if len(matches) != 1:
                error_code = (
                    "salad_provider_job_not_found"
                    if not matches
                    else "salad_duplicate_provider_jobs"
                )
                attempt, job, _ = await _load_attempt_context(
                    session,
                    generation_attempt_id,
                    lock=True,
                )
                await _mark_outcome_unknown(
                    session,
                    attempt=attempt,
                    job=job,
                    error_code=error_code,
                    occurred_at=reconciled_at,
                )
                await session.commit()
                return ReconciliationResult(
                    observation=_observation_result(
                        attempt,
                        job,
                        applied=False,
                        stale=False,
                    ),
                    source=source,
                    matched=False,
                    error_code=error_code,
                )
            remote_job = next(iter(matches.values()))
    except SaladAPIError as error:
        error_code = (
            "salad_provider_job_not_found"
            if error.status_code == 404
            else "salad_reconciliation_unavailable"
        )
        attempt, job, _ = await _load_attempt_context(
            session,
            generation_attempt_id,
            lock=True,
        )
        if error.status_code == 404:
            await _mark_outcome_unknown(
                session,
                attempt=attempt,
                job=job,
                error_code=error_code,
                occurred_at=reconciled_at,
            )
        else:
            _note_reconciliation_error(
                session,
                attempt=attempt,
                error_code=error_code,
                occurred_at=reconciled_at,
            )
        await session.commit()
        return ReconciliationResult(
            observation=_observation_result(attempt, job, applied=False, stale=False),
            source=source,
            matched=False,
            error_code=error_code,
        )
    except (SaladTransportError, SaladProtocolError):
        attempt, job, _ = await _load_attempt_context(
            session,
            generation_attempt_id,
            lock=True,
        )
        _note_reconciliation_error(
            session,
            attempt=attempt,
            error_code="salad_reconciliation_unavailable",
            occurred_at=reconciled_at,
        )
        await session.commit()
        return ReconciliationResult(
            observation=_observation_result(attempt, job, applied=False, stale=False),
            source=source,
            matched=False,
            error_code="salad_reconciliation_unavailable",
        )

    observation = await apply_salad_job_observation(
        session,
        generation_attempt_id=generation_attempt_id,
        remote_job=remote_job,
        observed_at=reconciled_at,
    )
    await session.commit()
    return ReconciliationResult(
        observation=observation,
        source=source,
        matched=True,
    )


async def _load_attempt_context(
    session: AsyncSession,
    attempt_id: UUID,
    *,
    lock: bool,
) -> tuple[GenerationAttempt, GenerationJob, SaladDeployment]:
    if lock:
        # Budget operations use guard -> attempt lock ordering. Acquire the same
        # singleton first for every mutating attempt transaction so submission,
        # reconciliation, callbacks, and metering cannot deadlock each other.
        await session.scalar(
            select(ProviderBudgetGuard.id)
            .where(ProviderBudgetGuard.provider == _PROVIDER)
            .with_for_update()
        )
    query = (
        select(GenerationAttempt, GenerationJob, SaladDeployment)
        .join(GenerationJob, GenerationJob.id == GenerationAttempt.job_id)
        .join(
            SaladDeployment,
            SaladDeployment.id == GenerationAttempt.salad_deployment_id,
        )
        .where(GenerationAttempt.id == attempt_id)
    )
    if lock:
        query = query.with_for_update()
    row = (await session.execute(query)).one_or_none()
    if row is None:
        raise SaladServiceNotFoundError("generation attempt was not found")
    attempt, job, deployment = row
    return attempt, job, deployment


def _require_submittable_deployment(deployment: SaladDeployment) -> None:
    if deployment.max_replicas > 1:
        raise SaladServiceConflictError("v1 Salad deployment exceeds one replica")
    if not deployment.is_current:
        raise SaladServiceConflictError("Salad deployment is not current")
    if deployment.state != SaladDeploymentState.ACTIVE:
        raise SaladServiceConflictError("Salad deployment is not active")
    if deployment.desired_state != DesiredDeploymentState.ACTIVE:
        raise SaladServiceConflictError("Salad deployment is stopped")


def _require_attempt_deployment(
    attempt: GenerationAttempt,
    deployment: SaladDeployment,
) -> None:
    if attempt.provider != _PROVIDER:
        raise SaladServiceConflictError("generation attempt belongs to another provider")
    if attempt.worker_image_digest != deployment.worker_image_digest:
        raise SaladServiceConflictError("attempt worker digest differs from its deployment")
    if deployment.max_replicas > 1:
        raise SaladServiceConflictError("v1 Salad deployment exceeds one replica")


def _validate_name(value: str, *, name: str, max_length: int) -> str:
    normalized = value.strip()
    if not normalized:
        raise SaladServiceValidationError(f"{name} must not be empty")
    if len(normalized) > max_length:
        raise SaladServiceValidationError(f"{name} is too long")
    return normalized


def _validate_webhook_url(value: str) -> None:
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
    except ValueError:
        raise SaladServiceValidationError(
            "webhook_url must be a credential-free HTTPS URL"
        ) from None
    if (
        parsed.scheme != "https"
        or hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise SaladServiceValidationError("webhook_url must be a credential-free HTTPS URL")


def _contains_likely_secret(value: JSONValue | Mapping[str, JSONValue]) -> bool:
    if isinstance(value, Mapping):
        for key, item in value.items():
            compact_key = "".join(character for character in key.lower() if character.isalnum())
            if any(marker in compact_key for marker in _SECRET_KEY_MARKERS):
                return True
            if _contains_likely_secret(item):
                return True
        return False
    if isinstance(value, list):
        return any(_contains_likely_secret(item) for item in value)
    if not isinstance(value, str):
        return False

    normalized = value.strip()
    lowered = normalized.lower()
    if lowered.startswith(
        (
            "bearer ",
            "basic ",
            "sk-",
            "ghp_",
            "github_pat_",
            "xoxb-",
            "xoxp-",
        )
    ):
        return True
    if "-----begin private key-----" in lowered:
        return True
    if fullmatch(r"AKIA[0-9A-Z]{16}", normalized) is not None:
        return True
    try:
        parsed = urlsplit(normalized)
        if parsed.scheme in {"http", "https"} and (
            parsed.username is not None or parsed.password is not None
        ):
            return True
    except ValueError:
        pass
    return (
        fullmatch(
            r".*(?:x-amz-signature|api[_-]?key|access[_-]?token|token|secret|"
            r"password|credential|authorization)=[^&\s]+.*",
            lowered,
        )
        is not None
    )


def _contains_invalid_replica_setting(
    value: JSONValue | Mapping[str, JSONValue],
) -> bool:
    if isinstance(value, Mapping):
        for key, item in value.items():
            compact_key = "".join(character for character in key.lower() if character.isalnum())
            if compact_key in {"replicas", "minreplicas", "maxreplicas"} and (
                not isinstance(item, int) or isinstance(item, bool) or not 0 <= item <= 1
            ):
                return True
            if _contains_invalid_replica_setting(item):
                return True
        return False
    if isinstance(value, list):
        return any(_contains_invalid_replica_setting(item) for item in value)
    return False


def _validate_provider_configuration(
    value: Mapping[str, JSONValue],
) -> None:
    if _contains_invalid_replica_setting(value):
        raise SaladServiceValidationError(
            "provider configuration must not request more than one replica"
        )
    if _contains_likely_secret(value):
        raise SaladServiceValidationError(
            "provider_configuration must not contain credentials or secret material"
        )
    try:
        canonical_sha256(value)
    except (TypeError, ValueError):
        raise SaladServiceValidationError(
            "provider_configuration must contain finite JSON values"
        ) from None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise SaladServiceValidationError("timestamps must include a timezone")
    return value.astimezone(UTC)


def _stored_as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _submission_result(
    attempt: GenerationAttempt,
    job: GenerationJob,
    disposition: SubmissionDisposition,
    mutation_effect: MutationEffect,
    *,
    retry_not_before: datetime | None = None,
) -> SubmissionResult:
    return SubmissionResult(
        generation_attempt_id=attempt.id,
        attempt_state=attempt.state,
        generation_job_state=job.state,
        disposition=disposition,
        mutation_effect=mutation_effect,
        provider_external_id=attempt.provider_external_id,
        retry_not_before=retry_not_before,
    )


def _observation_result(
    attempt: GenerationAttempt,
    job: GenerationJob,
    *,
    applied: bool,
    stale: bool,
) -> ObservationResult:
    return ObservationResult(
        generation_attempt_id=attempt.id,
        attempt_state=attempt.state,
        generation_job_state=job.state,
        provider_external_id=attempt.provider_external_id,
        applied=applied,
        stale=stale,
    )


async def _mark_definite_failure(
    session: AsyncSession,
    *,
    attempt: GenerationAttempt,
    job: GenerationJob,
    error_code: str,
    safe_error_detail: str,
    job_state: GenerationState,
    occurred_at: datetime,
    retry_at: datetime | None = None,
) -> None:
    attempt.state = GenerationAttemptState.FAILED
    attempt.completed_at = occurred_at
    attempt.error_code = error_code
    attempt.error_detail = safe_error_detail
    attempt.lock_version += 1
    job.state = job_state
    job.retry_at = retry_at
    job.last_error_code = error_code
    job.last_error_detail = safe_error_detail
    job.lock_version += 1
    session.add(
        _audit(
            action="generation_attempt.submit_failed",
            attempt=attempt,
            detail={"error_code": error_code},
            occurred_at=occurred_at,
        )
    )
    if attempt.cost_reservation_microusd > 0 and attempt.reservation_released_at is None:
        await release_attempt_reservation(
            session,
            provider=_PROVIDER,
            attempt_id=attempt.id,
            now=occurred_at,
        )
    await session.flush()


async def _mark_outcome_unknown(
    session: AsyncSession,
    *,
    attempt: GenerationAttempt,
    job: GenerationJob,
    error_code: str,
    occurred_at: datetime,
) -> None:
    if attempt.state not in _TERMINAL_ATTEMPT_STATES:
        attempt.state = GenerationAttemptState.UNKNOWN
        attempt.unknown_since = attempt.unknown_since or occurred_at
        attempt.error_code = error_code
        attempt.error_detail = "The submission outcome is ambiguous; reconciliation is required."
        attempt.lock_version += 1
    if job.state not in _TERMINAL_JOB_STATES:
        job.state = GenerationState.UNKNOWN
        job.last_error_code = error_code
        job.last_error_detail = "The Salad submission outcome is ambiguous; no retry was attempted."
        job.lock_version += 1
    session.add(
        _audit(
            action="generation_attempt.submit_outcome_unknown",
            attempt=attempt,
            detail={"error_code": error_code},
            occurred_at=occurred_at,
        )
    )
    await session.flush()


def _note_reconciliation_error(
    session: AsyncSession,
    *,
    attempt: GenerationAttempt,
    error_code: str,
    occurred_at: datetime,
) -> None:
    attempt.error_code = error_code
    attempt.error_detail = "Salad job reconciliation is temporarily unavailable."
    attempt.lock_version += 1
    session.add(
        _audit(
            action="generation_attempt.reconciliation_deferred",
            attempt=attempt,
            detail={"error_code": error_code},
            occurred_at=occurred_at,
        )
    )


def _retry_or_fail(job: GenerationJob) -> GenerationState:
    return (
        GenerationState.RETRY_WAIT
        if job.attempt_count < job.max_attempts
        else GenerationState.DEAD_LETTER
    )


def _attempt_state(status: SaladJobStatus) -> GenerationAttemptState:
    return {
        SaladJobStatus.PENDING: GenerationAttemptState.SUBMITTED,
        SaladJobStatus.RUNNING: GenerationAttemptState.RUNNING,
        SaladJobStatus.SUCCEEDED: GenerationAttemptState.SUCCEEDED,
        SaladJobStatus.FAILED: GenerationAttemptState.FAILED,
        SaladJobStatus.CANCELLED: GenerationAttemptState.CANCELLED,
    }[status]


def _is_forward_transition(
    current: GenerationAttemptState,
    target: GenerationAttemptState,
) -> bool:
    if current == GenerationAttemptState.UNKNOWN:
        return True
    if current == GenerationAttemptState.CANCEL_REQUESTED:
        return target in _TERMINAL_ATTEMPT_STATES
    if current in _TERMINAL_ATTEMPT_STATES:
        return current == target
    return _ATTEMPT_STATE_RANK[target] >= _ATTEMPT_STATE_RANK[current]


def _job_state_for_observation(
    job: GenerationJob,
    attempt_state: GenerationAttemptState,
) -> GenerationState:
    if attempt_state in {
        GenerationAttemptState.SUBMITTED,
        GenerationAttemptState.RUNNING,
    }:
        return GenerationState.RUNNING
    if attempt_state == GenerationAttemptState.SUCCEEDED:
        return GenerationState.COLLECTING
    if attempt_state == GenerationAttemptState.FAILED:
        return _retry_or_fail(job)
    if attempt_state == GenerationAttemptState.CANCELLED:
        return GenerationState.CANCELLED
    raise SaladServiceConflictError("provider observation cannot map to a generation job")


def _remote_metadata_matches(
    attempt: GenerationAttempt,
    remote_job: SaladQueueJob,
) -> bool:
    return (
        remote_job.metadata.get("generation_attempt_id") == str(attempt.id)
        and remote_job.metadata.get("generation_job_id") == str(attempt.job_id)
        and remote_job.metadata.get("submission_key") == attempt.submission_key
        and remote_job.metadata.get("request_sha256") == attempt.request_sha256
    )


def _audit(
    *,
    action: str,
    attempt: GenerationAttempt,
    detail: dict[str, Any],
    occurred_at: datetime,
) -> AuditEvent:
    return AuditEvent(
        actor="salad-controller",
        action=action,
        resource_type="generation_attempt",
        resource_id=attempt.id,
        correlation_id=str(attempt.id),
        detail=detail,
        occurred_at=occurred_at,
    )
