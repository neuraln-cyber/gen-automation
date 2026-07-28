from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import func, or_, select
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
from gen_automation.services.salad import PreparedAttempt, prepare_generation_attempt

_DISPATCHABLE_RELEASE_PHASES = frozenset(
    {
        ReleasePhase.READY,
        ReleasePhase.GENERATING,
    }
)
_INFLIGHT_ATTEMPT_STATES = frozenset(
    {
        GenerationAttemptState.CREATED,
        GenerationAttemptState.SUBMITTING,
        GenerationAttemptState.SUBMITTED,
        GenerationAttemptState.RUNNING,
        GenerationAttemptState.UNKNOWN,
        GenerationAttemptState.CANCEL_REQUESTED,
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

    deployment = await session.scalar(
        select(SaladDeployment).where(SaladDeployment.id == salad_deployment_id).with_for_update()
    )
    if deployment is None:
        raise GenerationSchedulingConflictError("Salad deployment was not found")
    if (
        not deployment.is_current
        or deployment.state != SaladDeploymentState.ACTIVE
        or deployment.desired_state != DesiredDeploymentState.ACTIVE
        or deployment.provider_queue_id is None
        or deployment.provider_container_group_id is None
    ):
        raise GenerationSchedulingConflictError("Salad deployment is not dispatchable")

    inflight = int(
        await session.scalar(
            select(func.count())
            .select_from(GenerationAttempt)
            .where(
                GenerationAttempt.salad_deployment_id == deployment.id,
                GenerationAttempt.state.in_(_INFLIGHT_ATTEMPT_STATES),
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
