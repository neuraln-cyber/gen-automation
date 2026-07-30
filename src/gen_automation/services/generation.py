from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from gen_automation.db.models import (
    AuditEvent,
    ComplianceCheck,
    GenerationJob,
    IdempotencyRecord,
    Release,
    ReleaseVersion,
)
from gen_automation.domain.canonical import canonical_sha256
from gen_automation.domain.deliverability import (
    MAX_ACCEPTED_IMAGES_PER_RELEASE,
    DeliverabilityError,
    require_generation_deliverability,
)
from gen_automation.domain.enums import (
    ComplianceResult,
    GenerationState,
    ReleasePhase,
    ResourceHealth,
)
from gen_automation.domain.release_spec import ReleaseSpecification
from gen_automation.schemas import GenerationPlanRead
from gen_automation.services.compliance import (
    ReleaseApprovalError,
    ReleaseApprovalSnapshot,
    validate_release_approvals,
)
from gen_automation.services.wildcards import (
    FrozenWildcardCatalog,
    WildcardError,
    load_frozen_wildcard_catalog,
    resolve_wildcard_prompts,
)


class GenerationPlanError(Exception):
    """Base error for generation-plan expansion."""


class GenerationPlanNotFoundError(GenerationPlanError):
    pass


class GenerationPlanConflictError(GenerationPlanError):
    pass


@dataclass(frozen=True)
class GenerationPlanResult:
    response: GenerationPlanRead
    replayed: bool


def _job_parameters(
    *,
    release_version: ReleaseVersion,
    specification: ReleaseSpecification,
    approval_snapshot: ReleaseApprovalSnapshot,
    wildcard_catalog: FrozenWildcardCatalog,
    ordinal: int,
) -> dict[str, object]:
    seed = (specification.generation.seed + ordinal) % (2**63)
    resolved_prompts = resolve_wildcard_prompts(
        specification,
        wildcard_catalog,
        seed=seed,
    )
    generation = specification.generation.model_copy(
        update={
            "seed": seed,
            "prompt": resolved_prompts.prompt,
            "negative_prompt": resolved_prompts.negative_prompt,
            "detailer_prompt": resolved_prompts.detailer_prompt,
            "detailer_negative_prompt": resolved_prompts.detailer_negative_prompt,
        }
    ).model_dump(mode="json")
    return {
        "schema_version": 1,
        "release_version_id": str(release_version.id),
        "release_specification_sha256": release_version.specification_sha256,
        "approval_snapshot_sha256": approval_snapshot.sha256,
        "ordinal": ordinal,
        "subjects": [
            {
                "name": subject.name,
                "canonical_age": subject.canonical_age,
                "canonical_source_url": str(subject.canonical_source_url),
            }
            for subject in specification.subjects
        ],
        "checkpoint": specification.checkpoint.model_dump(mode="json"),
        "loras": [lora.model_dump(mode="json") for lora in specification.loras],
        "workflow": specification.workflow.model_dump(mode="json"),
        "generation": generation,
        "prompt_resolution": resolved_prompts.evidence,
    }


async def _record_compliance_checks(
    session: AsyncSession,
    *,
    version: ReleaseVersion,
    approval_snapshot: ReleaseApprovalSnapshot,
    actor: str,
    checked_at: datetime,
) -> None:
    existing = set(
        (
            await session.scalars(
                select(ComplianceCheck.check_type).where(
                    ComplianceCheck.release_version_id == version.id
                )
            )
        ).all()
    )
    for check_type, evidence in approval_snapshot.checks.items():
        if check_type in existing:
            continue
        session.add(
            ComplianceCheck(
                release_version_id=version.id,
                check_type=check_type,
                result=ComplianceResult.PASSED,
                evidence=evidence,
                checked_by=actor,
                checked_at=checked_at,
            )
        )


async def approve_and_expand_generation_plan(
    session: AsyncSession,
    *,
    release_id: UUID,
    idempotency_key: str,
    actor: str = "owner",
) -> GenerationPlanResult:
    """Revalidate a frozen release and expand it into deterministic jobs."""
    scope = f"release:{release_id}:approve-generation"
    row = (
        await session.execute(
            select(Release, ReleaseVersion)
            .join(
                ReleaseVersion,
                (ReleaseVersion.release_id == Release.id)
                & (ReleaseVersion.version_no == Release.current_version_no),
            )
            .where(Release.id == release_id)
            .with_for_update()
        )
    ).one_or_none()
    if row is None:
        raise GenerationPlanNotFoundError("release not found")
    release, version = row

    request_sha256 = canonical_sha256(
        {
            "release_id": str(release.id),
            "release_version_id": str(version.id),
            "specification_sha256": version.specification_sha256,
        }
    )
    existing_command = await session.scalar(
        select(IdempotencyRecord).where(
            IdempotencyRecord.scope == scope,
            IdempotencyRecord.idempotency_key == idempotency_key,
        )
    )
    if existing_command is not None:
        if existing_command.request_sha256 != request_sha256:
            raise GenerationPlanConflictError(
                "idempotency key was already used for another generation plan"
            )
        return GenerationPlanResult(
            response=GenerationPlanRead.model_validate(existing_command.response_body),
            replayed=True,
        )

    if release.phase not in {
        ReleasePhase.DRAFT,
        ReleasePhase.VALIDATING,
        ReleasePhase.READY,
    }:
        raise GenerationPlanConflictError("release phase does not allow generation-plan approval")
    if canonical_sha256(version.specification) != version.specification_sha256:
        raise GenerationPlanConflictError("frozen release specification digest mismatch")
    try:
        specification = ReleaseSpecification.model_validate(version.specification)
    except ValidationError:
        raise GenerationPlanConflictError(
            "frozen release specification no longer passes validation"
        ) from None

    try:
        approval_snapshot = await validate_release_approvals(session, specification)
    except ReleaseApprovalError as error:
        raise GenerationPlanConflictError(str(error)) from error
    workflow_check = approval_snapshot.checks["workflow_integrity_gate"]["workflow"]
    workflow_node_classes = workflow_check["reviewed_node_classes"]
    try:
        if release.desired_accepted_count > MAX_ACCEPTED_IMAGES_PER_RELEASE:
            raise DeliverabilityError(
                "desired accepted count exceeds the Patreon package limit of "
                f"{MAX_ACCEPTED_IMAGES_PER_RELEASE}"
            )
        require_generation_deliverability(
            width=specification.generation.width,
            height=specification.generation.height,
            hires_scale=specification.generation.hires_scale,
            workflow_node_classes=workflow_node_classes,
        )
    except DeliverabilityError as error:
        raise GenerationPlanConflictError(str(error)) from error
    try:
        wildcard_catalog = await load_frozen_wildcard_catalog(session, specification)
    except WildcardError as error:
        raise GenerationPlanConflictError(str(error)) from error

    now = datetime.now(UTC)
    release.phase = ReleasePhase.VALIDATING
    await _record_compliance_checks(
        session,
        version=version,
        approval_snapshot=approval_snapshot,
        actor=actor,
        checked_at=now,
    )

    existing_jobs = list(
        (
            await session.scalars(
                select(GenerationJob)
                .where(GenerationJob.release_version_id == version.id)
                .order_by(GenerationJob.logical_key)
            )
        ).all()
    )
    jobs_by_key = {job.logical_key: job for job in existing_jobs}
    created_count = 0
    for ordinal in range(specification.planned_job_count):
        logical_key = canonical_sha256(
            {
                "release_version_id": str(version.id),
                "ordinal": ordinal,
            }
        )
        parameters = _job_parameters(
            release_version=version,
            specification=specification,
            approval_snapshot=approval_snapshot,
            wildcard_catalog=wildcard_catalog,
            ordinal=ordinal,
        )
        parameters_sha256 = canonical_sha256(parameters)
        existing_job = jobs_by_key.get(logical_key)
        if existing_job is not None:
            if (
                existing_job.parameters_sha256 != parameters_sha256
                or existing_job.expected_output_count != specification.generation.outputs_per_job
            ):
                raise GenerationPlanConflictError(
                    "existing generation job conflicts with the frozen plan"
                )
            continue
        session.add(
            GenerationJob(
                release_version_id=version.id,
                logical_key=logical_key,
                parameters=parameters,
                parameters_sha256=parameters_sha256,
                provider="salad",
                state=GenerationState.QUEUED,
                priority=100,
                expected_output_count=specification.generation.outputs_per_job,
                attempt_count=0,
                max_attempts=3,
                lock_version=1,
            )
        )
        created_count += 1

    release.phase = ReleasePhase.READY
    release.health = ResourceHealth.HEALTHY
    release.lock_version += 1
    await session.flush()
    total_jobs = int(
        await session.scalar(
            select(func.count())
            .select_from(GenerationJob)
            .where(GenerationJob.release_version_id == version.id)
        )
        or 0
    )
    if total_jobs != specification.planned_job_count:
        raise GenerationPlanConflictError(
            "generation job count conflicts with the frozen release plan"
        )

    response = GenerationPlanRead(
        release_id=release.id,
        release_version_id=version.id,
        release_phase=release.phase,
        specification_sha256=version.specification_sha256,
        jobs_created=created_count,
        total_jobs=total_jobs,
    )
    session.add(
        AuditEvent(
            actor=actor,
            action="release.generation_plan_approved",
            resource_type="release",
            resource_id=release.id,
            correlation_id=idempotency_key,
            detail={
                "release_version_id": str(version.id),
                "specification_sha256": version.specification_sha256,
                "jobs_created": created_count,
                "total_jobs": total_jobs,
                "approval_snapshot_sha256": approval_snapshot.sha256,
            },
            occurred_at=now,
        )
    )
    session.add(
        IdempotencyRecord(
            scope=scope,
            idempotency_key=idempotency_key,
            request_sha256=request_sha256,
            response_status=200,
            response_body=response.model_dump(mode="json"),
            created_at=now,
            expires_at=now + timedelta(days=30),
        )
    )
    await session.commit()
    return GenerationPlanResult(response=response, replayed=False)
