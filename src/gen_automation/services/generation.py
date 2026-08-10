import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from gen_automation.config import Settings
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
from gen_automation.domain.generation_limits import (
    MAX_PROMPT_TEXT_BYTES_PER_GENERATION_JOB,
    MAX_SIGNED_PROMPT_BUDGET_BYTES_PER_GENERATION_JOB,
    signed_worker_prompt_budget_bytes,
    utf8_prompt_bytes,
)
from gen_automation.domain.release_spec import GenerationParameters, ReleaseSpecification
from gen_automation.gpu_worker.artifacts import ArtifactKind
from gen_automation.schemas import GenerationPlanRead
from gen_automation.services.compliance import (
    ReleaseApprovalError,
    ReleaseApprovalSnapshot,
    validate_release_approvals,
)
from gen_automation.services.managed_artifact_manifest import (
    ManagedArtifactManifestError,
    effective_artifact_manifest_from_settings,
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


_SEED_MODULUS = 2**63
_RANDOM_SEED_SENTINEL = -1
_RANDOM_SEED_PERSON = b"gen-seed-v1"


@dataclass(frozen=True)
class GenerationPlanResult:
    response: GenerationPlanRead
    replayed: bool


@dataclass(frozen=True, slots=True)
class _PlannedGenerationJob:
    ordinal: int
    batch_index: int
    batch_name: str
    batch_image_offset: int
    batch_image_count: int
    generation: GenerationParameters
    expected_output_count: int
    output_seeds: tuple[int, ...]
    include_batch_metadata: bool


def _planned_generation_jobs(
    specification: ReleaseSpecification,
    *,
    random_seed_key: bytes,
) -> tuple[_PlannedGenerationJob, ...]:
    if not specification.generation_batches:
        generation = specification.generation
        randomized_seeds = (
            _random_output_seeds(
                count=specification.planned_job_count * generation.outputs_per_job,
                key=random_seed_key,
                namespace="default",
                used=set(),
            )
            if generation.seed == _RANDOM_SEED_SENTINEL
            else ()
        )
        return tuple(
            _PlannedGenerationJob(
                ordinal=ordinal,
                batch_index=0,
                batch_name="Default batch",
                batch_image_offset=ordinal * generation.outputs_per_job,
                batch_image_count=(specification.planned_job_count * generation.outputs_per_job),
                generation=generation,
                expected_output_count=generation.outputs_per_job,
                output_seeds=tuple(
                    randomized_seeds[ordinal + (output_index * specification.planned_job_count)]
                    if randomized_seeds
                    else (
                        generation.seed + ordinal + (output_index * specification.planned_job_count)
                    )
                    % _SEED_MODULUS
                    for output_index in range(generation.outputs_per_job)
                ),
                include_batch_metadata=False,
            )
            for ordinal in range(specification.planned_job_count)
        )

    plans: list[_PlannedGenerationJob] = []
    ordinal = 0
    used_random_seeds: set[int] = set()
    for batch_index, batch in enumerate(specification.generation_batches):
        outputs_per_job = batch.generation.outputs_per_job
        randomized_seeds = (
            _random_output_seeds(
                count=batch.image_count,
                key=random_seed_key,
                namespace=f"batch:{batch_index}",
                used=used_random_seeds,
            )
            if batch.generation.seed == _RANDOM_SEED_SENTINEL
            else ()
        )
        for image_offset in range(0, batch.image_count, outputs_per_job):
            output_count = min(outputs_per_job, batch.image_count - image_offset)
            plans.append(
                _PlannedGenerationJob(
                    ordinal=ordinal,
                    batch_index=batch_index,
                    batch_name=batch.name,
                    batch_image_offset=image_offset,
                    batch_image_count=batch.image_count,
                    generation=batch.generation,
                    expected_output_count=output_count,
                    output_seeds=tuple(
                        randomized_seeds[image_offset + output_index]
                        if randomized_seeds
                        else (batch.generation.seed + image_offset + output_index) % _SEED_MODULUS
                        for output_index in range(output_count)
                    ),
                    include_batch_metadata=True,
                )
            )
            ordinal += 1
    return tuple(plans)


def _random_output_seeds(
    *,
    count: int,
    key: bytes,
    namespace: str,
    used: set[int],
) -> tuple[int, ...]:
    """Derive unique random-looking seeds while keeping retries deterministic."""

    if not key:
        raise GenerationPlanConflictError("random seed key is unavailable")
    seeds: list[int] = []
    for output_index in range(count):
        collision_index = 0
        while True:
            payload = f"{namespace}:{output_index}:{collision_index}".encode()
            digest = hashlib.blake2b(
                payload,
                digest_size=8,
                key=key,
                person=_RANDOM_SEED_PERSON,
            ).digest()
            seed = int.from_bytes(digest, "big") & (_SEED_MODULUS - 1)
            if seed not in used:
                seeds.append(seed)
                used.add(seed)
                break
            collision_index += 1
    return tuple(seeds)


def _job_parameters(
    *,
    release_version: ReleaseVersion,
    specification: ReleaseSpecification,
    approval_snapshot: ReleaseApprovalSnapshot,
    wildcard_catalog: FrozenWildcardCatalog,
    plan: _PlannedGenerationJob,
) -> dict[str, object]:
    output_generations: list[dict[str, object]] = []
    output_prompt_resolutions: list[dict[str, object]] = []
    for seed in plan.output_seeds:
        resolved_prompts = resolve_wildcard_prompts(
            specification,
            wildcard_catalog,
            seed=seed,
            generation=plan.generation,
        )
        output_generations.append(
            plan.generation.model_copy(
                update={
                    "seed": seed,
                    "prompt": resolved_prompts.prompt,
                    "character_a_prompt": resolved_prompts.character_a_prompt,
                    "character_b_prompt": resolved_prompts.character_b_prompt,
                    "character_a_pose_prompt": resolved_prompts.character_a_pose_prompt,
                    "character_b_pose_prompt": resolved_prompts.character_b_pose_prompt,
                    "character_c_prompt": resolved_prompts.character_c_prompt,
                    "character_c_pose_prompt": resolved_prompts.character_c_pose_prompt,
                    "character_a_negative_prompt": (resolved_prompts.character_a_negative_prompt),
                    "character_b_negative_prompt": (resolved_prompts.character_b_negative_prompt),
                    "character_c_negative_prompt": (resolved_prompts.character_c_negative_prompt),
                    "interaction_prompt": resolved_prompts.interaction_prompt,
                    "camera_prompt": resolved_prompts.camera_prompt,
                    "negative_prompt": resolved_prompts.negative_prompt,
                    "detailer_prompt": resolved_prompts.detailer_prompt,
                    "detailer_negative_prompt": resolved_prompts.detailer_negative_prompt,
                    "outputs_per_job": 1,
                }
            ).model_dump(mode="json")
        )
        output_prompt_resolutions.append(resolved_prompts.evidence)

    output_prompt_values = tuple(
        str(generation[field])
        for generation in output_generations
        for field in (
            "prompt",
            "character_a_prompt",
            "character_b_prompt",
            "character_a_pose_prompt",
            "character_b_pose_prompt",
            "character_c_prompt",
            "character_c_pose_prompt",
            "character_a_negative_prompt",
            "character_b_negative_prompt",
            "character_c_negative_prompt",
            "interaction_prompt",
            "camera_prompt",
            "negative_prompt",
            "detailer_prompt",
            "detailer_negative_prompt",
        )
    )
    prompt_bytes = utf8_prompt_bytes(output_prompt_values)
    budgeted_prompt_bytes = (
        signed_worker_prompt_budget_bytes(output_prompt_values)
        if specification.worker_request_budget_version >= 2
        else prompt_bytes
    )
    prompt_budget_limit = (
        MAX_SIGNED_PROMPT_BUDGET_BYTES_PER_GENERATION_JOB
        if specification.worker_request_budget_version >= 2
        else MAX_PROMPT_TEXT_BYTES_PER_GENERATION_JOB
    )
    if budgeted_prompt_bytes > prompt_budget_limit:
        raise GenerationPlanConflictError(
            "expanded prompt text is too large for one multi-output generation job"
        )

    generation = dict(output_generations[0])
    generation["outputs_per_job"] = plan.expected_output_count
    parameters: dict[str, object] = {
        "schema_version": 2,
        "worker_request_budget_version": specification.worker_request_budget_version,
        "release_version_id": str(release_version.id),
        "release_specification_sha256": release_version.specification_sha256,
        "approval_snapshot_sha256": approval_snapshot.sha256,
        "ordinal": plan.ordinal,
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
        "prompt_resolution": output_prompt_resolutions[0],
        "output_generations": output_generations,
        "output_prompt_resolutions": output_prompt_resolutions,
    }
    if plan.include_batch_metadata:
        parameters["batch"] = {
            "index": plan.batch_index,
            "name": plan.batch_name,
            "image_offset": plan.batch_image_offset,
            "image_count": plan.batch_image_count,
        }
    return parameters


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
    settings: Settings,
    actor: str = "owner",
) -> GenerationPlanResult:
    """Revalidate a frozen release and expand it into deterministic jobs."""
    scope = f"release:{release_id}:approve-generation"
    current_version_no = await session.scalar(
        select(Release.current_version_no).where(Release.id == release_id)
    )
    if current_version_no is None:
        raise GenerationPlanNotFoundError("release not found")
    version = await session.scalar(
        select(ReleaseVersion)
        .where(
            ReleaseVersion.release_id == release_id,
            ReleaseVersion.version_no == current_version_no,
        )
        .with_for_update()
    )
    if version is None:
        raise GenerationPlanConflictError("current release version is unavailable")
    release = await session.scalar(
        select(Release)
        .where(Release.id == release_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if release is None:
        raise GenerationPlanNotFoundError("release not found")
    if release.current_version_no != version.version_no:
        raise GenerationPlanConflictError("current release version changed concurrently")

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
    if settings.lora_manager_enabled:
        try:
            effective_manifest = await effective_artifact_manifest_from_settings(
                session,
                settings=settings,
                required_lora_sha256s=tuple(lora.sha256 for lora in specification.loras),
            )
        except (ManagedArtifactManifestError, ValueError) as error:
            raise GenerationPlanConflictError(
                "selected model stack does not fit the safe worker runtime"
            ) from error
        if not any(
            artifact.kind == ArtifactKind.CHECKPOINT
            and artifact.sha256 == specification.checkpoint.sha256
            for artifact in effective_manifest.manifest.artifacts
        ):
            raise GenerationPlanConflictError(
                "selected checkpoint is not present in the pinned worker runtime"
            )
    workflow_check = approval_snapshot.checks["workflow_integrity_gate"]["workflow"]
    workflow_node_classes = workflow_check["reviewed_node_classes"]
    try:
        if release.desired_accepted_count > MAX_ACCEPTED_IMAGES_PER_RELEASE:
            raise DeliverabilityError(
                "desired accepted count exceeds the final-set limit of "
                f"{MAX_ACCEPTED_IMAGES_PER_RELEASE}"
            )
        for batch in specification.ordered_generation_batches:
            require_generation_deliverability(
                width=batch.generation.width,
                height=batch.generation.height,
                hires_scale=batch.generation.hires_scale,
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
    planned_jobs = _planned_generation_jobs(
        specification,
        random_seed_key=version.id.bytes,
    )
    if len(planned_jobs) != specification.planned_job_count:
        raise GenerationPlanConflictError(
            "generation batch expansion conflicts with the frozen release plan"
        )
    prepared_jobs: list[tuple[_PlannedGenerationJob, str, dict[str, object], str]] = []
    for plan in planned_jobs:
        logical_identity: dict[str, object] = {
            "release_version_id": str(version.id),
            "ordinal": plan.ordinal,
        }
        if plan.include_batch_metadata:
            logical_identity.update(
                {
                    "batch_index": plan.batch_index,
                    "batch_image_offset": plan.batch_image_offset,
                }
            )
        logical_key = canonical_sha256(logical_identity)
        parameters = _job_parameters(
            release_version=version,
            specification=specification,
            approval_snapshot=approval_snapshot,
            wildcard_catalog=wildcard_catalog,
            plan=plan,
        )
        parameters_sha256 = canonical_sha256(parameters)
        prepared_jobs.append((plan, logical_key, parameters, parameters_sha256))

    for plan, logical_key, parameters, parameters_sha256 in prepared_jobs:
        existing_job = jobs_by_key.get(logical_key)
        if existing_job is not None:
            if (
                existing_job.parameters_sha256 != parameters_sha256
                or existing_job.expected_output_count != plan.expected_output_count
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
                expected_output_count=plan.expected_output_count,
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
