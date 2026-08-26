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
)
from gen_automation.domain.enums import (
    GenerationAttemptState,
    GenerationState,
    ReleasePhase,
    ResourceHealth,
)

INFRASTRUCTURE_RETRY_GRANT_ACTION = "generation_attempt.infrastructure_retry_granted"
MAX_INFRASTRUCTURE_RETRY_GRANTS = 2
DEFAULT_INFRASTRUCTURE_RECOVERY_SWEEP_LIMIT = 16

SALAD_JOB_FAILED_ERROR_CODE = "salad_job_failed"
SALAD_WEBHOOK_JOB_FAILED_ERROR_CODE = "provider_job_failed"
SALAD_WATCHDOG_EXPIRED_ERROR_CODE = "salad_attempt_watchdog_expired"
SALAD_DEPLOYMENT_SUPERSEDED_ERROR_CODE = "salad_deployment_superseded"
SALAD_DEPLOYMENT_PROVIDER_ABSENT_ERROR_CODE = "salad_deployment_rollover_provider_absent"
SALAD_RATE_LIMITED_ERROR_CODE = "salad_rate_limited"
SALAD_PROVIDER_CANCELLED_ERROR_CODE = "salad_provider_cancelled"
SALAD_PROVIDER_JOB_ABSENT_ERROR_CODE = "salad_provider_job_absent"

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
class InfrastructureRecoverySummary:
    scanned: int
    recovered_job_ids: tuple[UUID, ...]


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

    existing_grant = await session.scalar(
        select(AuditEvent.id)
        .where(
            AuditEvent.resource_type == "generation_attempt",
            AuditEvent.resource_id == attempt.id,
            AuditEvent.action == INFRASTRUCTURE_RETRY_GRANT_ACTION,
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
                AuditEvent.action == INFRASTRUCTURE_RETRY_GRANT_ACTION,
                GenerationAttempt.job_id == job.id,
            )
        )
        or 0
    )
    if existing_grant is not None or grant_count >= MAX_INFRASTRUCTURE_RETRY_GRANTS:
        return InfrastructureRetryGrant(
            granted=False,
            grant_ordinal=grant_count,
            grant_limit=MAX_INFRASTRUCTURE_RETRY_GRANTS,
        )

    granted_at = _as_utc(occurred_at)
    retry_not_before = _as_utc(retry_at)
    previous_max_attempts = job.max_attempts
    previous_job_state = job.state
    grant_ordinal = grant_count + 1
    job.max_attempts = previous_max_attempts + 1
    job.state = GenerationState.RETRY_WAIT
    job.retry_at = retry_not_before
    job.lease_owner = None
    job.lease_expires_at = None
    job.last_error_code = failure_code
    job.last_error_detail = failure_detail
    job.lock_version += 1
    session.add(
        AuditEvent(
            actor=actor,
            action=INFRASTRUCTURE_RETRY_GRANT_ACTION,
            resource_type="generation_attempt",
            resource_id=attempt.id,
            correlation_id=str(attempt.id),
            detail={
                "generation_job_id": str(job.id),
                "attempt_no": attempt.attempt_no,
                "failure_code": failure_code,
                "failure_code_source": (
                    "generation_job_legacy_fallback"
                    if legacy_failure_code is not None
                    else "generation_attempt"
                ),
                "source": source.value,
                "grant_ordinal": grant_ordinal,
                "grant_limit": MAX_INFRASTRUCTURE_RETRY_GRANTS,
                "previous_max_attempts": previous_max_attempts,
                "new_max_attempts": job.max_attempts,
                "previous_job_state": previous_job_state.value,
                "new_job_state": job.state.value,
                "provider_external_id": attempt.provider_external_id,
                "salad_deployment_id": str(attempt.salad_deployment_id),
                "assets_retained": True,
            },
            occurred_at=granted_at,
        )
    )
    await session.flush()
    return InfrastructureRetryGrant(
        granted=True,
        grant_ordinal=grant_ordinal,
        grant_limit=MAX_INFRASTRUCTURE_RETRY_GRANTS,
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
