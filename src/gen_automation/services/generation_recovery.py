from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from gen_automation.db.models import (
    AuditEvent,
    GenerationAttempt,
    GenerationJob,
    Release,
    ReleaseVersion,
    SaladDeployment,
)
from gen_automation.domain.canonical import canonical_sha256
from gen_automation.domain.enums import (
    DesiredDeploymentState,
    GenerationAttemptState,
    GenerationState,
    ReleasePhase,
    ResourceHealth,
    SaladDeploymentState,
)
from gen_automation.domain.ids import uuid7
from gen_automation.domain.near_black_recovery import (
    NEAR_BLACK_SEED_RECOVERY_METADATA_KEY,
    NearBlackRecoveryPlanError,
    NearBlackSeedRecoveryPlan,
    build_near_black_seed_recovery_plan,
)

INFRASTRUCTURE_RETRY_GRANT_ACTION = "generation_attempt.infrastructure_retry_granted"
MAX_INFRASTRUCTURE_RETRY_GRANTS = 2
RUNTIME_INSTANCE_TURNOVER_RETRY_GRANT_ACTION = (
    "generation_attempt.runtime_instance_turnover_retry_granted"
)
MAX_RUNTIME_INSTANCE_TURNOVER_RETRY_GRANTS = 2
NEAR_BLACK_OUTPUT_RETRY_GRANT_ACTION = "generation_attempt.near_black_retry_granted"
MAX_NEAR_BLACK_OUTPUT_RETRY_GRANTS = 1
DEFAULT_INFRASTRUCTURE_RECOVERY_SWEEP_LIMIT = 16

SALAD_JOB_FAILED_ERROR_CODE = "salad_job_failed"
SALAD_WEBHOOK_JOB_FAILED_ERROR_CODE = "provider_job_failed"
SALAD_WATCHDOG_EXPIRED_ERROR_CODE = "salad_attempt_watchdog_expired"
SALAD_DEPLOYMENT_SUPERSEDED_ERROR_CODE = "salad_deployment_superseded"
SALAD_DEPLOYMENT_PROVIDER_ABSENT_ERROR_CODE = "salad_deployment_rollover_provider_absent"
SALAD_RATE_LIMITED_ERROR_CODE = "salad_rate_limited"
SALAD_PROVIDER_CANCELLED_ERROR_CODE = "salad_provider_cancelled"
SALAD_PROVIDER_JOB_ABSENT_ERROR_CODE = "salad_provider_job_absent"
SALAD_WORKER_NEAR_BLACK_OUTPUT_ERROR_CODE = "salad_worker_near_black_output"

INFRASTRUCTURE_FAILURE_ERROR_CODES = frozenset(
    {
        SALAD_JOB_FAILED_ERROR_CODE,
        SALAD_WEBHOOK_JOB_FAILED_ERROR_CODE,
        SALAD_WATCHDOG_EXPIRED_ERROR_CODE,
        SALAD_DEPLOYMENT_SUPERSEDED_ERROR_CODE,
        SALAD_DEPLOYMENT_PROVIDER_ABSENT_ERROR_CODE,
        SALAD_RATE_LIMITED_ERROR_CODE,
        SALAD_PROVIDER_CANCELLED_ERROR_CODE,
        SALAD_PROVIDER_JOB_ABSENT_ERROR_CODE,
    }
)

# Historical controller versions could persist the definitive provider failure
# only on the job while leaving the exact terminal attempt's ``error_code``
# unset. Recovery may use that legacy fallback only when the attempt still
# carries provider evidence that independently agrees with the job-level code.
_LEGACY_FAILED_PROVIDER_ERROR_CODES = frozenset(
    {
        SALAD_JOB_FAILED_ERROR_CODE,
        SALAD_WEBHOOK_JOB_FAILED_ERROR_CODE,
    }
)
_LEGACY_CANCELLED_PROVIDER_ERROR_CODES = frozenset(
    {
        SALAD_WATCHDOG_EXPIRED_ERROR_CODE,
        SALAD_DEPLOYMENT_SUPERSEDED_ERROR_CODE,
        SALAD_PROVIDER_CANCELLED_ERROR_CODE,
    }
)
_LEGACY_ABSENT_PROVIDER_STATES_BY_ERROR_CODE = {
    SALAD_DEPLOYMENT_PROVIDER_ABSENT_ERROR_CODE: "absent_after_rollover",
    SALAD_PROVIDER_JOB_ABSENT_ERROR_CODE: "absent_after_reconciliation",
}

_RETRYABLE_RELEASE_PHASES = frozenset(
    {
        ReleasePhase.READY,
        ReleasePhase.GENERATING,
    }
)
_NON_RETRYABLE_JOB_STATES = frozenset(
    {
        GenerationState.SUCCEEDED,
        GenerationState.FAILED,
        GenerationState.CANCELLED,
    }
)
_GENERATION_STOP_REQUESTED_ACTION = "release.generation_stop_requested"
_RUNTIME_ADMISSION_METADATA_VERSION = "v1"
_RUNTIME_ADMISSION_TRANSITIONAL_ERROR_CODES = frozenset(
    {
        "provider_autoscaler_repair_pending",
        "provider_image_preparation_pending",
        "provider_start_pending",
    }
)
_RUNTIME_ADMISSION_STALLED_ERROR_CODES = frozenset(
    {
        "provider_image_preparation_stalled",
        "provider_start_stalled",
    }
)


class InfrastructureRetrySource(StrEnum):
    SUBMISSION = "submission"
    RECONCILER = "reconciler"
    WEBHOOK = "webhook"
    SCHEDULER_RECOVERY = "scheduler_recovery"


@dataclass(frozen=True)
class InfrastructureRetryGrant:
    granted: bool
    grant_ordinal: int
    grant_limit: int


@dataclass(frozen=True)
class RuntimeInstanceTurnoverEvidence:
    provider_external_id: str
    provider_group_version: int
    artifact_manifest_sha256: str
    rollout_id: str
    previous_worker_instance_id: str
    current_worker_instance_id: str


@dataclass(frozen=True)
class NearBlackOutputRetryGrant:
    granted: bool
    grant_ordinal: int
    grant_limit: int
    grant_audit_event_id: UUID | None
    recovery_plan: NearBlackSeedRecoveryPlan | None

    def __post_init__(self) -> None:
        if self.granted != (self.grant_audit_event_id is not None) or self.granted != (
            self.recovery_plan is not None
        ):
            raise ValueError("near-black grant provenance is inconsistent")
        if self.recovery_plan is not None and self.recovery_plan.source_grant_audit_event_id != str(
            self.grant_audit_event_id
        ):
            raise ValueError("near-black grant plan belongs to another audit event")


@dataclass(frozen=True)
class InfrastructureRecoverySummary:
    scanned: int
    recovered_job_ids: tuple[UUID, ...]


def require_near_black_original_output_seeds(job: GenerationJob) -> tuple[int, ...]:
    """Return the immutable per-output seed map used to create the job."""

    try:
        parameters_match = canonical_sha256(job.parameters) == job.parameters_sha256
    except (TypeError, ValueError):
        parameters_match = False
    raw_outputs = job.parameters.get("output_generations")
    raw_generation = job.parameters.get("generation")
    if (
        not parameters_match
        or job.parameters.get("schema_version") != 2
        or not isinstance(raw_outputs, list)
        or len(raw_outputs) != job.expected_output_count
        or not isinstance(raw_generation, dict)
    ):
        raise NearBlackRecoveryPlanError("original job seeds are unavailable")
    seeds: list[int] = []
    for output in raw_outputs:
        if not isinstance(output, dict):
            raise NearBlackRecoveryPlanError("original job seeds are unavailable")
        seed = output.get("seed")
        if not isinstance(seed, int) or isinstance(seed, bool) or not 0 <= seed <= (2**63) - 1:
            raise NearBlackRecoveryPlanError("original job seeds are unavailable")
        seeds.append(seed)
    base_seed = raw_generation.get("seed")
    if (
        not seeds
        or not isinstance(base_seed, int)
        or isinstance(base_seed, bool)
        or base_seed != seeds[0]
    ):
        raise NearBlackRecoveryPlanError("original job seeds are unavailable")
    return tuple(seeds)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _provider_attempt_is_definitive(attempt: GenerationAttempt) -> bool:
    if attempt.provider_external_id is not None:
        return True
    return (
        (
            attempt.error_code == SALAD_DEPLOYMENT_PROVIDER_ABSENT_ERROR_CODE
            and attempt.provider_state == "absent_after_rollover"
        )
        or (
            attempt.error_code == SALAD_PROVIDER_JOB_ABSENT_ERROR_CODE
            and attempt.provider_state == "absent_after_reconciliation"
        )
        or (
            attempt.error_code == SALAD_RATE_LIMITED_ERROR_CODE
            and attempt.provider_external_id is None
        )
    )


def _legacy_job_failure_code(
    *,
    attempt: GenerationAttempt,
    job: GenerationJob,
) -> str | None:
    """Return a job-level legacy code only when provider evidence agrees exactly."""

    if (
        attempt.error_code is not None
        or job.last_error_code is None
        or job.state != GenerationState.DEAD_LETTER
        or job.provider != "salad"
        or attempt.provider != "salad"
    ):
        return None
    error_code = job.last_error_code
    if error_code in _LEGACY_FAILED_PROVIDER_ERROR_CODES:
        provider_evidence_matches = (
            attempt.provider_external_id is not None and attempt.provider_state == "failed"
        )
    elif error_code in _LEGACY_CANCELLED_PROVIDER_ERROR_CODES:
        provider_evidence_matches = (
            attempt.provider_external_id is not None and attempt.provider_state == "cancelled"
        )
    else:
        expected_provider_state = _LEGACY_ABSENT_PROVIDER_STATES_BY_ERROR_CODE.get(error_code)
        provider_evidence_matches = (
            expected_provider_state is not None
            and attempt.provider_state == expected_provider_state
        )
    return error_code if provider_evidence_matches else None


async def _release_allows_retry(
    session: AsyncSession,
    *,
    job: GenerationJob,
) -> bool:
    lifecycle = (
        await session.execute(
            select(ReleaseVersion, Release)
            .join(Release, Release.id == ReleaseVersion.release_id)
            .where(ReleaseVersion.id == job.release_version_id)
        )
    ).one_or_none()
    if lifecycle is None:
        return False
    version, release = lifecycle
    if (
        release.current_version_no != version.version_no
        or release.health != ResourceHealth.HEALTHY
        or release.phase not in _RETRYABLE_RELEASE_PHASES
    ):
        return False
    stop_requested = await session.scalar(
        select(
            exists().where(
                AuditEvent.resource_type == "release",
                AuditEvent.resource_id == release.id,
                AuditEvent.action == _GENERATION_STOP_REQUESTED_ACTION,
            )
        )
    )
    return not bool(stop_requested)


async def _grant_bounded_infrastructure_retry(
    session: AsyncSession,
    *,
    attempt: GenerationAttempt,
    job: GenerationJob,
    action: str,
    grant_limit: int,
    failure_code: str,
    failure_detail: str | None,
    source: InfrastructureRetrySource,
    actor: str,
    retry_at: datetime,
    occurred_at: datetime,
    audit_detail: dict[str, object],
) -> InfrastructureRetryGrant:
    existing_grant = await session.scalar(
        select(AuditEvent.id)
        .where(
            AuditEvent.resource_type == "generation_attempt",
            AuditEvent.resource_id == attempt.id,
            AuditEvent.action == action,
        )
        .limit(1)
    )
    grant_count = int(
        await session.scalar(
            select(func.count(AuditEvent.id))
            .join(GenerationAttempt, GenerationAttempt.id == AuditEvent.resource_id)
            .where(
                AuditEvent.resource_type == "generation_attempt",
                AuditEvent.action == action,
                GenerationAttempt.job_id == job.id,
            )
        )
        or 0
    )
    if existing_grant is not None or grant_count >= grant_limit:
        return InfrastructureRetryGrant(
            granted=False,
            grant_ordinal=grant_count,
            grant_limit=grant_limit,
        )

    previous_max_attempts = job.max_attempts
    previous_job_state = job.state
    grant_ordinal = grant_count + 1
    job.max_attempts = previous_max_attempts + 1
    job.state = GenerationState.RETRY_WAIT
    job.retry_at = _as_utc(retry_at)
    job.lease_owner = None
    job.lease_expires_at = None
    job.last_error_code = failure_code
    job.last_error_detail = failure_detail
    job.lock_version += 1
    session.add(
        AuditEvent(
            actor=actor,
            action=action,
            resource_type="generation_attempt",
            resource_id=attempt.id,
            correlation_id=str(attempt.id),
            detail={
                "generation_job_id": str(job.id),
                "attempt_no": attempt.attempt_no,
                "failure_code": failure_code,
                "source": source.value,
                "grant_ordinal": grant_ordinal,
                "grant_limit": grant_limit,
                "previous_max_attempts": previous_max_attempts,
                "new_max_attempts": job.max_attempts,
                "previous_job_state": previous_job_state.value,
                "new_job_state": job.state.value,
                **audit_detail,
            },
            occurred_at=_as_utc(occurred_at),
        )
    )
    await session.flush()
    return InfrastructureRetryGrant(
        granted=True,
        grant_ordinal=grant_ordinal,
        grant_limit=grant_limit,
    )


async def grant_infrastructure_retry(
    session: AsyncSession,
    *,
    attempt: GenerationAttempt,
    job: GenerationJob,
    source: InfrastructureRetrySource,
    actor: str,
    retry_at: datetime,
    occurred_at: datetime,
) -> InfrastructureRetryGrant:
    """Grant one bounded retry slot for an exact, definitive infrastructure failure.

    The caller must hold row locks for ``attempt`` and ``job``. The exact-attempt
    fence and the per-attempt audit marker make repeated provider observations
    and concurrent recovery sweeps idempotent under that lock.
    """

    legacy_failure_code = (
        _legacy_job_failure_code(attempt=attempt, job=job)
        if source == InfrastructureRetrySource.SCHEDULER_RECOVERY
        else None
    )
    failure_code = attempt.error_code or legacy_failure_code
    failure_detail = (
        job.last_error_detail if legacy_failure_code is not None else attempt.error_detail
    )
    if (
        attempt.job_id != job.id
        or attempt.attempt_no != job.attempt_count
        or attempt.state != GenerationAttemptState.FAILED
        or attempt.completed_at is None
        or failure_code not in INFRASTRUCTURE_FAILURE_ERROR_CODES
        or job.state in _NON_RETRYABLE_JOB_STATES
        or job.max_attempts >= 10
        or (legacy_failure_code is None and not _provider_attempt_is_definitive(attempt))
        or not await _release_allows_retry(session, job=job)
    ):
        return InfrastructureRetryGrant(
            granted=False,
            grant_ordinal=0,
            grant_limit=MAX_INFRASTRUCTURE_RETRY_GRANTS,
        )

    return await _grant_bounded_infrastructure_retry(
        session,
        attempt=attempt,
        job=job,
        action=INFRASTRUCTURE_RETRY_GRANT_ACTION,
        grant_limit=MAX_INFRASTRUCTURE_RETRY_GRANTS,
        failure_code=failure_code,
        failure_detail=failure_detail,
        source=source,
        actor=actor,
        retry_at=retry_at,
        occurred_at=occurred_at,
        audit_detail={
            "failure_code_source": (
                "generation_job_legacy_fallback"
                if legacy_failure_code is not None
                else "generation_attempt"
            ),
            "provider_external_id": attempt.provider_external_id,
            "salad_deployment_id": str(attempt.salad_deployment_id),
            "assets_retained": True,
        },
    )


def _runtime_instance_id_is_valid(value: object) -> bool:
    return bool(
        isinstance(value, str)
        and 1 <= len(value) <= 128
        and all(
            character in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:-"
            for character in value
        )
    )


def _runtime_turnover_deployment_allows_retry(
    *,
    attempt: GenerationAttempt,
    deployment: SaladDeployment,
    evidence: RuntimeInstanceTurnoverEvidence,
) -> bool:
    admission = attempt.request_metadata.get("runtime_admission")
    deployment_state_allows_admission = (
        deployment.state == SaladDeploymentState.ACTIVE
        or (
            deployment.state == SaladDeploymentState.PROVISIONING
            and deployment.last_error_code in _RUNTIME_ADMISSION_TRANSITIONAL_ERROR_CODES
        )
        or (
            deployment.state == SaladDeploymentState.DEGRADED
            and deployment.last_error_code in _RUNTIME_ADMISSION_STALLED_ERROR_CODES
        )
    )
    expected_admission = {
        "version": _RUNTIME_ADMISSION_METADATA_VERSION,
        "provider_group_version": evidence.provider_group_version,
        "artifact_manifest_sha256": evidence.artifact_manifest_sha256,
        "rollout_id": evidence.rollout_id,
        "worker_instance_id": evidence.previous_worker_instance_id,
    }
    return bool(
        attempt.salad_deployment_id == deployment.id
        and attempt.worker_image_digest == deployment.worker_image_digest
        and deployment.provider_queue_id is not None
        and deployment.provider_container_group_id is not None
        and deployment.is_current
        and deployment.desired_state == DesiredDeploymentState.ACTIVE
        and deployment.administrative_stop_reason is None
        and deployment.max_replicas == 1
        and deployment_state_allows_admission
        and deployment.runtime_artifact_manifest_sha256 == evidence.artifact_manifest_sha256
        and admission == expected_admission
        and evidence.provider_group_version > 0
        and len(evidence.artifact_manifest_sha256) == 64
        and evidence.artifact_manifest_sha256 == evidence.artifact_manifest_sha256.lower()
        and all(character in "0123456789abcdef" for character in evidence.artifact_manifest_sha256)
        and len(evidence.rollout_id) == 32
        and evidence.rollout_id == evidence.rollout_id.lower()
        and all(character in "0123456789abcdef" for character in evidence.rollout_id)
        and _runtime_instance_id_is_valid(evidence.previous_worker_instance_id)
        and _runtime_instance_id_is_valid(evidence.current_worker_instance_id)
        and evidence.previous_worker_instance_id != evidence.current_worker_instance_id
    )


async def grant_runtime_instance_turnover_retry(
    session: AsyncSession,
    *,
    attempt: GenerationAttempt,
    job: GenerationJob,
    deployment: SaladDeployment,
    evidence: RuntimeInstanceTurnoverEvidence,
    source: InfrastructureRetrySource,
    actor: str,
    retry_at: datetime,
    occurred_at: datetime,
) -> InfrastructureRetryGrant:
    """Grant a bounded retry after an exact admitted worker instance rotated.

    The caller must hold the budget guard plus attempt, job, and deployment row
    locks. Provider-side evidence is accepted only when it still matches the
    attempt's immutable admission tuple and the current deployment remains
    eligible for queue admission.
    """

    if (
        attempt.job_id != job.id
        or attempt.attempt_no != job.attempt_count
        or attempt.state != GenerationAttemptState.FAILED
        or attempt.completed_at is None
        or attempt.error_code
        not in {SALAD_JOB_FAILED_ERROR_CODE, SALAD_WEBHOOK_JOB_FAILED_ERROR_CODE}
        or attempt.provider_external_id != evidence.provider_external_id
        or attempt.provider_state != "failed"
        or job.state
        in {
            GenerationState.SUCCEEDED,
            GenerationState.FAILED,
            GenerationState.DEAD_LETTER,
            GenerationState.CANCELLED,
        }
        or job.max_attempts >= 10
        or not _runtime_turnover_deployment_allows_retry(
            attempt=attempt,
            deployment=deployment,
            evidence=evidence,
        )
        or not await _release_allows_retry(session, job=job)
    ):
        return InfrastructureRetryGrant(
            granted=False,
            grant_ordinal=0,
            grant_limit=MAX_RUNTIME_INSTANCE_TURNOVER_RETRY_GRANTS,
        )

    assert attempt.error_code is not None
    return await _grant_bounded_infrastructure_retry(
        session,
        attempt=attempt,
        job=job,
        action=RUNTIME_INSTANCE_TURNOVER_RETRY_GRANT_ACTION,
        grant_limit=MAX_RUNTIME_INSTANCE_TURNOVER_RETRY_GRANTS,
        failure_code=attempt.error_code,
        failure_detail=attempt.error_detail,
        source=source,
        actor=actor,
        retry_at=retry_at,
        occurred_at=occurred_at,
        audit_detail={
            "provider_external_id": evidence.provider_external_id,
            "salad_deployment_id": str(deployment.id),
            "provider_group_version": evidence.provider_group_version,
            "artifact_manifest_sha256": evidence.artifact_manifest_sha256,
            "rollout_id": evidence.rollout_id,
            "previous_worker_instance_id": evidence.previous_worker_instance_id,
            "current_worker_instance_id": evidence.current_worker_instance_id,
            "retry_class": "runtime_instance_turnover",
            "assets_retained": True,
        },
    )


async def grant_near_black_output_retry(
    session: AsyncSession,
    *,
    attempt: GenerationAttempt,
    job: GenerationJob,
    source: InfrastructureRetrySource,
    actor: str,
    retry_at: datetime,
    occurred_at: datetime,
    failed_output_index: int,
    uploaded_output_indices: tuple[int, ...],
) -> NearBlackOutputRetryGrant:
    """Grant one retry slot independent of the generic infrastructure budget."""

    if (
        attempt.job_id != job.id
        or attempt.attempt_no != job.attempt_count
        or attempt.state != GenerationAttemptState.FAILED
        or attempt.completed_at is None
        or attempt.error_code != SALAD_WORKER_NEAR_BLACK_OUTPUT_ERROR_CODE
        or job.state in _NON_RETRYABLE_JOB_STATES
        or job.max_attempts >= 10
        or not _provider_attempt_is_definitive(attempt)
        or not 0 <= failed_output_index < job.expected_output_count
        or list(uploaded_output_indices) not in ([], list(range(failed_output_index)))
        or not await _release_allows_retry(session, job=job)
    ):
        return NearBlackOutputRetryGrant(
            granted=False,
            grant_ordinal=0,
            grant_limit=MAX_NEAR_BLACK_OUTPUT_RETRY_GRANTS,
            grant_audit_event_id=None,
            recovery_plan=None,
        )

    existing_grant = await session.scalar(
        select(AuditEvent.id)
        .where(
            AuditEvent.resource_type == "generation_attempt",
            AuditEvent.resource_id == attempt.id,
            AuditEvent.action == NEAR_BLACK_OUTPUT_RETRY_GRANT_ACTION,
        )
        .limit(1)
    )
    grant_count = int(
        await session.scalar(
            select(func.count(AuditEvent.id))
            .join(
                GenerationAttempt,
                GenerationAttempt.id == AuditEvent.resource_id,
            )
            .where(
                AuditEvent.resource_type == "generation_attempt",
                AuditEvent.action == NEAR_BLACK_OUTPUT_RETRY_GRANT_ACTION,
                GenerationAttempt.job_id == job.id,
            )
        )
        or 0
    )
    if existing_grant is not None or grant_count >= MAX_NEAR_BLACK_OUTPUT_RETRY_GRANTS:
        return NearBlackOutputRetryGrant(
            granted=False,
            grant_ordinal=grant_count,
            grant_limit=MAX_NEAR_BLACK_OUTPUT_RETRY_GRANTS,
            grant_audit_event_id=None,
            recovery_plan=None,
        )

    granted_at = _as_utc(occurred_at)
    retry_not_before = _as_utc(retry_at)
    previous_max_attempts = job.max_attempts
    previous_job_state = job.state
    grant_ordinal = grant_count + 1
    grant_audit_event_id = uuid7()
    try:
        recovery_plan = build_near_black_seed_recovery_plan(
            generation_job_id=job.id,
            source_grant_audit_event_id=grant_audit_event_id,
            source_generation_attempt_id=attempt.id,
            source_attempt_no=attempt.attempt_no,
            grant_ordinal=grant_ordinal,
            failed_output_index=failed_output_index,
            expected_output_count=job.expected_output_count,
            uploaded_output_indices=uploaded_output_indices,
            original_seeds=require_near_black_original_output_seeds(job),
        )
    except NearBlackRecoveryPlanError:
        return NearBlackOutputRetryGrant(
            granted=False,
            grant_ordinal=grant_count,
            grant_limit=MAX_NEAR_BLACK_OUTPUT_RETRY_GRANTS,
            grant_audit_event_id=None,
            recovery_plan=None,
        )
    job.max_attempts = previous_max_attempts + 1
    job.state = GenerationState.RETRY_WAIT
    job.retry_at = retry_not_before
    job.lease_owner = None
    job.lease_expires_at = None
    job.last_error_code = attempt.error_code
    job.last_error_detail = attempt.error_detail
    job.lock_version += 1
    session.add(
        AuditEvent(
            id=grant_audit_event_id,
            actor=actor,
            action=NEAR_BLACK_OUTPUT_RETRY_GRANT_ACTION,
            resource_type="generation_attempt",
            resource_id=attempt.id,
            correlation_id=str(attempt.id),
            detail={
                "generation_job_id": str(job.id),
                "attempt_no": attempt.attempt_no,
                "failure_code": attempt.error_code,
                "source": source.value,
                "grant_ordinal": grant_ordinal,
                "grant_limit": MAX_NEAR_BLACK_OUTPUT_RETRY_GRANTS,
                "previous_max_attempts": previous_max_attempts,
                "new_max_attempts": job.max_attempts,
                "previous_job_state": previous_job_state.value,
                "new_job_state": job.state.value,
                "provider_external_id": attempt.provider_external_id,
                "salad_deployment_id": str(attempt.salad_deployment_id),
                "failed_output_index": failed_output_index,
                "uploaded_output_count": len(uploaded_output_indices),
                "assets_retained": True,
                NEAR_BLACK_SEED_RECOVERY_METADATA_KEY: recovery_plan.model_dump(mode="json"),
            },
            occurred_at=granted_at,
        )
    )
    await session.flush()
    return NearBlackOutputRetryGrant(
        granted=True,
        grant_ordinal=grant_ordinal,
        grant_limit=MAX_NEAR_BLACK_OUTPUT_RETRY_GRANTS,
        grant_audit_event_id=grant_audit_event_id,
        recovery_plan=recovery_plan,
    )


async def recover_dead_lettered_infrastructure_jobs(
    session: AsyncSession,
    *,
    actor: str,
    limit: int = DEFAULT_INFRASTRUCTURE_RECOVERY_SWEEP_LIMIT,
    now: datetime | None = None,
) -> InfrastructureRecoverySummary:
    """Rearm a bounded batch of historical infrastructure dead letters."""

    if limit <= 0 or limit > 100:
        raise ValueError("infrastructure recovery limit must be between 1 and 100")
    recovered_at = _as_utc(now or datetime.now(UTC))
    granted_attempt = aliased(GenerationAttempt)
    exact_attempt_already_granted = exists(
        select(AuditEvent.id).where(
            AuditEvent.resource_type == "generation_attempt",
            AuditEvent.resource_id == GenerationAttempt.id,
            AuditEvent.action == INFRASTRUCTURE_RETRY_GRANT_ACTION,
        )
    )
    job_grant_count = (
        select(func.count(AuditEvent.id))
        .select_from(AuditEvent)
        .join(granted_attempt, granted_attempt.id == AuditEvent.resource_id)
        .where(
            AuditEvent.resource_type == "generation_attempt",
            AuditEvent.action == INFRASTRUCTURE_RETRY_GRANT_ACTION,
            granted_attempt.job_id == GenerationJob.id,
        )
        .correlate(GenerationJob)
        .scalar_subquery()
    )
    stop_requested = exists(
        select(AuditEvent.id).where(
            AuditEvent.resource_type == "release",
            AuditEvent.resource_id == Release.id,
            AuditEvent.action == _GENERATION_STOP_REQUESTED_ACTION,
        )
    )
    provider_attempt_is_definitive = or_(
        GenerationAttempt.provider_external_id.is_not(None),
        and_(
            GenerationAttempt.error_code == SALAD_DEPLOYMENT_PROVIDER_ABSENT_ERROR_CODE,
            GenerationAttempt.provider_state == "absent_after_rollover",
        ),
        and_(
            GenerationAttempt.error_code == SALAD_PROVIDER_JOB_ABSENT_ERROR_CODE,
            GenerationAttempt.provider_state == "absent_after_reconciliation",
        ),
        and_(
            GenerationAttempt.error_code == SALAD_RATE_LIMITED_ERROR_CODE,
            GenerationAttempt.provider_external_id.is_(None),
        ),
    )
    legacy_job_failure_is_definitive = or_(
        and_(
            GenerationJob.last_error_code.in_(_LEGACY_FAILED_PROVIDER_ERROR_CODES),
            GenerationAttempt.provider == "salad",
            GenerationAttempt.provider_external_id.is_not(None),
            GenerationAttempt.provider_state == "failed",
        ),
        and_(
            GenerationJob.last_error_code.in_(_LEGACY_CANCELLED_PROVIDER_ERROR_CODES),
            GenerationAttempt.provider == "salad",
            GenerationAttempt.provider_external_id.is_not(None),
            GenerationAttempt.provider_state == "cancelled",
        ),
        and_(
            GenerationJob.last_error_code == SALAD_DEPLOYMENT_PROVIDER_ABSENT_ERROR_CODE,
            GenerationAttempt.provider == "salad",
            GenerationAttempt.provider_state == "absent_after_rollover",
        ),
        and_(
            GenerationJob.last_error_code == SALAD_PROVIDER_JOB_ABSENT_ERROR_CODE,
            GenerationAttempt.provider == "salad",
            GenerationAttempt.provider_state == "absent_after_reconciliation",
        ),
    )
    recoverable_infrastructure_failure = or_(
        and_(
            GenerationAttempt.error_code.in_(INFRASTRUCTURE_FAILURE_ERROR_CODES),
            provider_attempt_is_definitive,
        ),
        and_(
            GenerationAttempt.error_code.is_(None),
            legacy_job_failure_is_definitive,
        ),
    )
    rows = list(
        (
            await session.execute(
                select(GenerationJob, GenerationAttempt)
                .join(
                    GenerationAttempt,
                    GenerationAttempt.job_id == GenerationJob.id,
                )
                .join(
                    ReleaseVersion,
                    ReleaseVersion.id == GenerationJob.release_version_id,
                )
                .join(Release, Release.id == ReleaseVersion.release_id)
                .where(
                    GenerationJob.provider == "salad",
                    GenerationJob.state == GenerationState.DEAD_LETTER,
                    GenerationJob.max_attempts < 10,
                    GenerationAttempt.attempt_no == GenerationJob.attempt_count,
                    GenerationAttempt.state == GenerationAttemptState.FAILED,
                    GenerationAttempt.completed_at.is_not(None),
                    recoverable_infrastructure_failure,
                    Release.current_version_no == ReleaseVersion.version_no,
                    Release.health == ResourceHealth.HEALTHY,
                    Release.phase.in_(_RETRYABLE_RELEASE_PHASES),
                    ~stop_requested,
                    ~exact_attempt_already_granted,
                    job_grant_count < MAX_INFRASTRUCTURE_RETRY_GRANTS,
                )
                .order_by(GenerationJob.created_at, GenerationJob.id)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        ).all()
    )
    recovered_job_ids: list[UUID] = []
    for job, attempt in rows:
        result = await grant_infrastructure_retry(
            session,
            attempt=attempt,
            job=job,
            source=InfrastructureRetrySource.SCHEDULER_RECOVERY,
            actor=actor,
            retry_at=recovered_at,
            occurred_at=recovered_at,
        )
        if result.granted:
            recovered_job_ids.append(job.id)
    return InfrastructureRecoverySummary(
        scanned=len(rows),
        recovered_job_ids=tuple(recovered_job_ids),
    )
