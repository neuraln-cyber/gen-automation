from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from gen_automation.db.models import (
    AuditEvent,
    ComplianceCheck,
    GenerationAttempt,
    GenerationJob,
    Release,
    ReleaseVersion,
    SaladDeployment,
)
from gen_automation.domain.enums import (
    ComplianceResult,
    DesiredDeploymentState,
    GenerationAttemptState,
    GenerationState,
    ReleasePhase,
    ResourceHealth,
    SaladDeploymentState,
)
from gen_automation.domain.release_spec import ReleaseSpecification
from gen_automation.services.compliance import (
    ReleaseApprovalError,
    validate_release_approvals,
)
from gen_automation.services.generation_control import GENERATION_STOP_REQUESTED_ACTION
from gen_automation.services.generation_recovery import (
    DEFAULT_INFRASTRUCTURE_RECOVERY_SWEEP_LIMIT,
    recover_dead_lettered_infrastructure_jobs,
)
from gen_automation.services.salad import PreparedAttempt, prepare_generation_attempt

_DISPATCHABLE_RELEASE_PHASES = frozenset(
    {
        ReleasePhase.READY,
        ReleasePhase.GENERATING,
    }
)
_REMOTE_OR_AMBIGUOUS_INFLIGHT_ATTEMPT_STATES = frozenset(
    {
        GenerationAttemptState.SUBMITTED,
        GenerationAttemptState.RUNNING,
        GenerationAttemptState.UNKNOWN,
        GenerationAttemptState.CANCEL_REQUESTED,
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
_REQUIRED_COMPLIANCE_CHECKS = frozenset(
    {
        "adult_subject_gate",
        "artifact_license_gate",
        "workflow_integrity_gate",
    }
)


class GenerationSchedulingError(Exception):
    """Base error for local generation dispatch."""


class GenerationSchedulingConflictError(GenerationSchedulingError):
    pass


@dataclass(frozen=True)
class DispatchResult:
    deployment_id: UUID
    available_slots: int
    dispatched: tuple[PreparedAttempt, ...]


async def has_dispatchable_generation_job(
    session: AsyncSession,
    *,
    scheduled_at: datetime | None = None,
) -> bool:
    """Return whether a Salad job can enter dispatch now.

    The warm-loop uses this exact static eligibility boundary to avoid starting
    a baseline-only worker immediately before dispatch creates an attempt with
    a managed-LoRA manifest. Full approval currency is still revalidated by
    ``dispatch_generation_jobs`` before any attempt is created.
    """

    due_at = scheduled_at or datetime.now(UTC)
    job_id = await session.scalar(
        select(GenerationJob.id)
        .join(ReleaseVersion, ReleaseVersion.id == GenerationJob.release_version_id)
        .join(Release, Release.id == ReleaseVersion.release_id)
        .where(
            GenerationJob.provider == "salad",
            GenerationJob.attempt_count < GenerationJob.max_attempts,
            or_(
                GenerationJob.state == GenerationState.QUEUED,
                (
                    (GenerationJob.state == GenerationState.RETRY_WAIT)
                    & or_(
                        GenerationJob.retry_at.is_(None),
                        GenerationJob.retry_at <= due_at,
                    )
                ),
            ),
            Release.phase.in_(_DISPATCHABLE_RELEASE_PHASES),
            Release.health == ResourceHealth.HEALTHY,
            Release.current_version_no == ReleaseVersion.version_no,
            ~exists(
                select(AuditEvent.id).where(
                    AuditEvent.resource_type == "release",
                    AuditEvent.resource_id == Release.id,
                    AuditEvent.action == GENERATION_STOP_REQUESTED_ACTION,
                )
            ),
            (
                select(func.count(func.distinct(ComplianceCheck.check_type)))
                .where(
                    ComplianceCheck.release_version_id == ReleaseVersion.id,
                    ComplianceCheck.check_type.in_(_REQUIRED_COMPLIANCE_CHECKS),
                    ComplianceCheck.result == ComplianceResult.PASSED,
                )
                .correlate(ReleaseVersion)
                .scalar_subquery()
                == len(_REQUIRED_COMPLIANCE_CHECKS)
            ),
        )
        .limit(1)
    )
    return job_id is not None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


async def _dispatch_compliance_is_current(
    session: AsyncSession,
    *,
    job: GenerationJob,
    release: Release,
    version: ReleaseVersion,
    actor: str,
    now: datetime,
) -> bool:
    reason_code = "approval_registry_changed"
    try:
        specification = ReleaseSpecification.model_validate(version.specification)
        snapshot = await validate_release_approvals(session, specification)
        latest_checks: dict[str, ComplianceCheck] = {}
        checks = list(
            (
                await session.scalars(
                    select(ComplianceCheck)
                    .where(
                        ComplianceCheck.release_version_id == version.id,
                        ComplianceCheck.check_type.in_(_REQUIRED_COMPLIANCE_CHECKS),
                    )
                    .order_by(
                        ComplianceCheck.checked_at.desc(),
                        ComplianceCheck.id.desc(),
                    )
                )
            ).all()
        )
        for check in checks:
            latest_checks.setdefault(check.check_type, check)
        stored_snapshot_sha256: Any = job.parameters.get("approval_snapshot_sha256")
        current = (
            stored_snapshot_sha256 == snapshot.sha256
            and set(latest_checks) == _REQUIRED_COMPLIANCE_CHECKS
            and all(
                latest_checks[check_type].result == ComplianceResult.PASSED
                and latest_checks[check_type].evidence == snapshot.checks[check_type]
                for check_type in _REQUIRED_COMPLIANCE_CHECKS
            )
        )
        if current:
            return True
        reason_code = "compliance_snapshot_stale"
    except (ReleaseApprovalError, ValidationError):
        pass

    release.health = ResourceHealth.BLOCKED
    release.lock_version += 1
    session.add(
        ComplianceCheck(
            release_version_id=version.id,
            check_type="dispatch_registry_gate",
            result=ComplianceResult.FAILED,
            evidence={
                "gate_version": 1,
                "reason_code": reason_code,
                "generation_job_id": str(job.id),
            },
            checked_by=actor,
            checked_at=now,
        )
    )
    session.add(
        AuditEvent(
            actor=actor,
            action="release.dispatch_blocked_by_compliance",
            resource_type="release",
            resource_id=release.id,
            correlation_id=f"dispatch-compliance:{job.id}",
            detail={
                "reason_code": reason_code,
                "release_version_id": str(version.id),
                "generation_job_id": str(job.id),
            },
            occurred_at=now,
        )
    )
    return False


async def dispatch_generation_jobs(
    session: AsyncSession,
    *,
    salad_deployment_id: UUID,
    gpu_allocation_enabled: bool,
    max_inflight: int,
    limit: int = 25,
    actor: str = "generation-scheduler",
    now: datetime | None = None,
) -> DispatchResult:
    """Atomically prepare eligible jobs up to the local provider backlog limit."""

    if not gpu_allocation_enabled:
        raise GenerationSchedulingConflictError("GPU allocation is disabled")
    if max_inflight <= 0 or max_inflight > 10_000:
        raise ValueError("max_inflight must be between 1 and 10000")
    if limit <= 0 or limit > 1_000:
        raise ValueError("limit must be between 1 and 1000")
    scheduled_at = _as_utc(now or datetime.now(UTC))

    recovery = await recover_dead_lettered_infrastructure_jobs(
        session,
        actor=actor,
        limit=min(limit, DEFAULT_INFRASTRUCTURE_RECOVERY_SWEEP_LIMIT),
        now=scheduled_at,
    )
    if recovery.recovered_job_ids:
        # Recovery is durable independently of current queue capacity or the
        # deployment lock below. A full provider queue must not roll a grant
        # back and leave the job permanently dead-lettered.
        await session.commit()
    else:
        await session.rollback()

    deployment = await session.scalar(
        select(SaladDeployment).where(SaladDeployment.id == salad_deployment_id).with_for_update()
    )
    if deployment is None:
        raise GenerationSchedulingConflictError("Salad deployment was not found")
    if (
        not deployment.is_current
        or (
            deployment.state != SaladDeploymentState.ACTIVE
            and not (
                deployment.state == SaladDeploymentState.PROVISIONING
                and deployment.last_error_code == "provider_start_pending"
            )
        )
        or deployment.desired_state != DesiredDeploymentState.ACTIVE
        or deployment.provider_queue_id is None
        or deployment.provider_container_group_id is None
    ):
        raise GenerationSchedulingConflictError("Salad deployment is not dispatchable")

    inflight = int(
        await session.scalar(
            select(func.count())
            .select_from(GenerationAttempt)
            .join(GenerationJob, GenerationJob.id == GenerationAttempt.job_id)
            .join(
                ReleaseVersion,
                ReleaseVersion.id == GenerationJob.release_version_id,
            )
            .join(Release, Release.id == ReleaseVersion.release_id)
            .where(
                GenerationAttempt.salad_deployment_id == deployment.id,
                GenerationAttempt.provider == "salad",
                or_(
                    # Once a provider request may exist, the attempt remains a
                    # capacity blocker until its own state becomes terminal. A
                    # stopped/failed parent is not proof that remote work ended.
                    GenerationAttempt.state.in_(_REMOTE_OR_AMBIGUOUS_INFLIGHT_ATTEMPT_STATES),
                    # Modern SUBMITTING rows always carry the durable marker
                    # written before provider contact. Ignore only a legacy
                    # markerless orphan whose parent is already terminal.
                    and_(
                        GenerationAttempt.state == GenerationAttemptState.SUBMITTING,
                        or_(
                            GenerationJob.state.not_in(_TERMINAL_JOB_STATES),
                            GenerationAttempt.provider_external_id.is_not(None),
                            GenerationAttempt.submit_started_at.is_not(None),
                            GenerationAttempt.submitted_at.is_not(None),
                            GenerationAttempt.started_at.is_not(None),
                            GenerationAttempt.last_observed_at.is_not(None),
                            GenerationAttempt.unknown_since.is_not(None),
                            GenerationAttempt.provider_state.is_not(None),
                            GenerationAttempt.response_metadata.is_not(None),
                            GenerationAttempt.cost_reservation_microusd > 0,
                            GenerationAttempt.reservation_released_at.is_not(None),
                        ),
                    ),
                    # CREATED is the sole definitely-unstarted state. Count a
                    # clean row only while its owning lifecycle can still submit
                    # it; stale rows from cancelled/terminal sets must not pin
                    # every local backlog slot forever.
                    and_(
                        GenerationAttempt.state == GenerationAttemptState.CREATED,
                        or_(
                            and_(
                                GenerationJob.state.not_in(_TERMINAL_JOB_STATES),
                                Release.phase.in_(_DISPATCHABLE_RELEASE_PHASES),
                                Release.current_version_no == ReleaseVersion.version_no,
                                ~exists(
                                    select(AuditEvent.id).where(
                                        AuditEvent.resource_type == "release",
                                        AuditEvent.resource_id == Release.id,
                                        AuditEvent.action == GENERATION_STOP_REQUESTED_ACTION,
                                    )
                                ),
                            ),
                            # Corrupt/legacy CREATED rows carrying evidence of a
                            # started submission remain fail-closed.
                            GenerationAttempt.provider_external_id.is_not(None),
                            GenerationAttempt.submit_started_at.is_not(None),
                            GenerationAttempt.submitted_at.is_not(None),
                            GenerationAttempt.started_at.is_not(None),
                            GenerationAttempt.last_observed_at.is_not(None),
                            GenerationAttempt.unknown_since.is_not(None),
                            GenerationAttempt.provider_state.is_not(None),
                            GenerationAttempt.response_metadata.is_not(None),
                            GenerationAttempt.cost_reservation_microusd > 0,
                            GenerationAttempt.reservation_released_at.is_not(None),
                        ),
                    ),
                ),
            )
        )
        or 0
    )
    available_slots = max(max_inflight - inflight, 0)
    dispatch_count = min(available_slots, limit)
    if dispatch_count == 0:
        deployment_id = deployment.id
        await session.rollback()
        return DispatchResult(
            deployment_id=deployment_id,
            available_slots=0,
            dispatched=(),
        )

    rows = list(
        (
            await session.execute(
                select(GenerationJob, Release, ReleaseVersion)
                .join(
                    ReleaseVersion,
                    ReleaseVersion.id == GenerationJob.release_version_id,
                )
                .join(Release, Release.id == ReleaseVersion.release_id)
                .where(
                    GenerationJob.provider == "salad",
                    GenerationJob.attempt_count < GenerationJob.max_attempts,
                    or_(
                        GenerationJob.state == GenerationState.QUEUED,
                        (
                            (GenerationJob.state == GenerationState.RETRY_WAIT)
                            & or_(
                                GenerationJob.retry_at.is_(None),
                                GenerationJob.retry_at <= scheduled_at,
                            )
                        ),
                    ),
                    Release.phase.in_(_DISPATCHABLE_RELEASE_PHASES),
                    Release.health == ResourceHealth.HEALTHY,
                    Release.current_version_no == ReleaseVersion.version_no,
                    ~exists(
                        select(AuditEvent.id).where(
                            AuditEvent.resource_type == "release",
                            AuditEvent.resource_id == Release.id,
                            AuditEvent.action == GENERATION_STOP_REQUESTED_ACTION,
                        )
                    ),
                    (
                        select(func.count(func.distinct(ComplianceCheck.check_type)))
                        .where(
                            ComplianceCheck.release_version_id == ReleaseVersion.id,
                            ComplianceCheck.check_type.in_(_REQUIRED_COMPLIANCE_CHECKS),
                            ComplianceCheck.result == ComplianceResult.PASSED,
                        )
                        .correlate(ReleaseVersion)
                        .scalar_subquery()
                        == len(_REQUIRED_COMPLIANCE_CHECKS)
                    ),
                )
                .order_by(
                    GenerationJob.priority,
                    GenerationJob.created_at,
                    GenerationJob.parameters["ordinal"].as_integer(),
                    GenerationJob.id,
                )
                .limit(dispatch_count)
                .with_for_update(skip_locked=True)
            )
        ).all()
    )

    prepared: list[PreparedAttempt] = []
    touched_releases: set[UUID] = set()
    for job, release, version in rows:
        if release.health != ResourceHealth.HEALTHY:
            continue
        if not await _dispatch_compliance_is_current(
            session,
            job=job,
            release=release,
            version=version,
            actor=actor,
            now=scheduled_at,
        ):
            continue
        next_attempt = job.attempt_count + 1
        item = await prepare_generation_attempt(
            session,
            generation_job_id=job.id,
            salad_deployment_id=deployment.id,
            idempotency_key=f"dispatch:{job.id}:attempt:{next_attempt}",
            actor=actor,
            now=scheduled_at,
        )
        prepared.append(item)
        if release.id not in touched_releases:
            release.phase = ReleasePhase.GENERATING
            release.lock_version += 1
            touched_releases.add(release.id)

    await session.commit()
    return DispatchResult(
        deployment_id=deployment.id,
        available_slots=available_slots - len(prepared),
        dispatched=tuple(prepared),
    )
