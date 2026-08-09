from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from gen_automation.config import Settings
from gen_automation.db.models import (
    Asset,
    AssetRanking,
    AssetScore,
    AuditEvent,
    GenerationAttempt,
    GenerationJob,
    ManagedLoraArtifact,
    ModelArtifactApproval,
    Project,
    Release,
    ReleaseVersion,
    SaladDeployment,
    ScoringRun,
    SubjectApproval,
    WorkflowApproval,
)
from gen_automation.domain.controlled_duo import (
    DuoCompositionPreset,
    DuoIsolationMode,
    DuoQualityMode,
    WorkflowCapability,
    effective_workflow_capabilities,
    require_controlled_duo_capabilities,
)
from gen_automation.domain.deliverability import (
    MAX_ACCEPTED_IMAGES_PER_RELEASE,
    DeliverabilityError,
    require_generation_deliverability,
)
from gen_automation.domain.enums import (
    ApprovalStatus,
    AssetKind,
    AssetState,
    GenerationAttemptState,
    GenerationState,
    ManagedLoraLifecycle,
    ModelArtifactKind,
    ReleasePhase,
    ResourceHealth,
    SaladDeploymentState,
    ScoringRunState,
)
from gen_automation.domain.generation_limits import MAX_OUTPUTS_PER_GENERATION_JOB
from gen_automation.domain.release_spec import (
    ArtifactSpecification,
    GenerationBatchSpecification,
    GenerationParameters,
    LoraSpecification,
    ProjectCreate,
    ReleaseCreate,
    ReleaseSpecification,
    Slug,
    SubjectSpecification,
    WorkflowSpecification,
)
from gen_automation.schemas import GenerationPlanRead, ProjectRead, ReleaseRead
from gen_automation.services.generation import approve_and_expand_generation_plan
from gen_automation.services.generation_control import (
    GENERATION_STOP_REQUESTED_ACTION,
    GENERATION_STOPPED_ACTION,
)
from gen_automation.services.gpu_billing import (
    GpuBillingSnapshot,
    gpu_billing_payload,
    gpu_billing_poll_after_ms,
    load_shared_gpu_billing_snapshot,
)
from gen_automation.services.releases import create_project, create_release
from gen_automation.services.wildcards import list_wildcard_libraries

_PROVIDER_STATUS_STALE_AFTER = timedelta(minutes=5)
_PROVIDER_PREPARATION_ERROR_CODES = frozenset(
    {
        "provider_image_preparation_pending",
        "provider_image_preparation_stalled",
    }
)
_PROVIDER_PREPARATION_PHASES = frozenset({"allocation", "image_pull", "preparing", "unknown"})
_PROVIDER_GROUP_STATUSES = frozenset(
    {
        "allocating",
        "creating",
        "deploying",
        "failed",
        "pending",
        "preparing",
        "running",
        "stopped",
        "stopping",
        "unknown",
    }
)


class NewSetInputError(ValueError):
    """The operator's selection no longer maps to an approved release."""


class NewSetNotFoundError(LookupError):
    pass


class GenerationProgressStage(StrEnum):
    QUEUED = "queued"
    GPU_STARTING = "gpu_starting"
    GENERATING = "generating"
    SCORING = "scoring"
    REVIEW = "review"
    PAUSED = "paused"
    CANCELLED = "cancelled"
    ERROR = "error"


class NewSetLoraSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    approval_id: UUID
    weight: float = Field(ge=-2.0, le=2.0)


class NewSetBatchSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=100)
    image_count: int = Field(ge=1, le=80_000)
    prompt: str = Field(min_length=1, max_length=20_000)
    negative_prompt: str | None = Field(default=None, max_length=20_000)
    detailer_prompt: str | None = Field(default=None, max_length=20_000)
    detailer_negative_prompt: str | None = Field(default=None, max_length=20_000)
    seed: int | None = Field(default=None, ge=-1, le=(2**63) - 1)

    @field_validator("name")
    @classmethod
    def require_trimmed_name(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("batch name must be trimmed")
        return value

    @field_validator("prompt")
    @classmethod
    def require_visible_prompt(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("batch prompt must not be blank")
        return value


class NewSetSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    slug: Slug
    title: str = Field(min_length=1, max_length=300)
    subject_approval_id: UUID
    secondary_subject_approval_id: UUID | None = None
    composition_mode: Literal["single", "duo"] = "single"
    duo_contract_version: Literal[1, 2] = 1
    composition_preset_id: DuoCompositionPreset | None = None
    character_a_prompt: str = Field(default="", max_length=20_000)
    character_b_prompt: str = Field(default="", max_length=20_000)
    character_a_negative_prompt: str = Field(default="", max_length=20_000)
    character_b_negative_prompt: str = Field(default="", max_length=20_000)
    interaction_prompt: str = Field(default="", max_length=20_000)
    camera_prompt: str = Field(default="", max_length=20_000)
    duo_isolation_mode: DuoIsolationMode = DuoIsolationMode.BALANCED
    duo_quality_mode: DuoQualityMode = DuoQualityMode.STANDARD
    checkpoint_approval_id: UUID
    loras: tuple[NewSetLoraSelection, ...] = Field(default=(), max_length=8)
    workflow_approval_id: UUID
    prompt: str = Field(default="", max_length=20_000)
    negative_prompt: str = Field(default="", max_length=20_000)
    detailer_prompt: str = Field(default="", max_length=20_000)
    detailer_negative_prompt: str = Field(default="", max_length=20_000)
    batches: tuple[NewSetBatchSubmission, ...] = Field(default=(), max_length=50)
    seed: int = Field(ge=-1, le=(2**63) - 1)
    width: int = Field(ge=512, le=4096, multiple_of=8)
    height: int = Field(ge=512, le=4096, multiple_of=8)
    cfg: float = Field(default=5.0, ge=0.0, le=30.0)
    steps: int = Field(ge=1, le=200)
    sampler: str = Field(min_length=1, max_length=100)
    scheduler: str = Field(min_length=1, max_length=100)
    clip_skip: int = Field(default=2, ge=1, le=12)
    outputs_per_job: int = Field(ge=1, le=MAX_OUTPUTS_PER_GENERATION_JOB)
    hires_scale: float = Field(default=1.5, ge=1.0, le=3.0)
    hires_denoise: float = Field(default=0.35, ge=0.05, le=1.0)
    hires_upscale_method: Literal[
        "nearest-exact",
        "bilinear",
        "area",
        "bicubic",
        "bislerp",
    ] = "bislerp"
    detailer_guide_size: int = Field(default=768, ge=512, le=2048, multiple_of=64)
    detailer_max_size: int = Field(default=1024, ge=512, le=4096, multiple_of=64)
    detailer_denoise: float = Field(default=0.35, ge=0.05, le=1.0)
    detailer_bbox_threshold: float = Field(default=0.5, ge=0.1, le=0.95)
    detailer_bbox_dilation: int = Field(default=10, ge=-64, le=128)
    detailer_bbox_crop_factor: float = Field(default=3.0, ge=1.0, le=5.0)
    detailer_feather: int = Field(default=4, ge=0, le=128)
    planned_job_count: int = Field(ge=1, le=10_000)
    desired_accepted_count: int = Field(ge=1, le=MAX_ACCEPTED_IMAGES_PER_RELEASE)

    @field_validator("title", "sampler", "scheduler")
    @classmethod
    def require_trimmed_text(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("value must be trimmed")
        return value

    @model_validator(mode="after")
    def validate_plan(self) -> "NewSetSubmission":
        if self.composition_mode == "single":
            if self.secondary_subject_approval_id is not None:
                raise ValueError("single-character composition cannot include a second subject")
            if (
                self.duo_contract_version != 1
                or self.composition_preset_id is not None
                or self.character_a_prompt.strip()
                or self.character_b_prompt.strip()
                or self.character_a_negative_prompt.strip()
                or self.character_b_negative_prompt.strip()
                or self.interaction_prompt.strip()
                or self.camera_prompt.strip()
                or self.duo_isolation_mode != DuoIsolationMode.BALANCED
                or self.duo_quality_mode != DuoQualityMode.STANDARD
            ):
                raise ValueError(
                    "single-character composition cannot include Controlled Duo fields"
                )
        else:
            if self.secondary_subject_approval_id is None:
                raise ValueError("two-character composition requires a second subject")
            if self.secondary_subject_approval_id == self.subject_approval_id:
                raise ValueError("two-character composition requires two different subjects")
            if not self.character_a_prompt.strip() or not self.character_b_prompt.strip():
                raise ValueError("two-character composition requires both character prompts")
            if self.duo_contract_version == 1:
                if (
                    self.composition_preset_id is not None
                    or self.character_a_negative_prompt
                    or self.character_b_negative_prompt
                    or self.interaction_prompt
                    or self.camera_prompt
                    or self.duo_isolation_mode != DuoIsolationMode.BALANCED
                    or self.duo_quality_mode != DuoQualityMode.STANDARD
                ):
                    raise ValueError("Controlled Duo fields require duo contract version 2")
            elif self.composition_preset_id is None:
                raise ValueError("Controlled Duo v2 requires a composition preset")
        lora_ids = [selection.approval_id for selection in self.loras]
        if len(lora_ids) != len(set(lora_ids)):
            raise ValueError("a LoRA can be selected only once")
        batch_names = [batch.name.casefold() for batch in self.batches]
        if len(batch_names) != len(set(batch_names)):
            raise ValueError("batch names must be unique")
        if not self.batches and not self.prompt.strip():
            raise ValueError("prompt must not be blank")
        if self.effective_planned_job_count > 10_000:
            raise ValueError("generation plan supports at most 10000 provider jobs")
        planned_outputs = (
            sum(batch.image_count for batch in self.batches)
            if self.batches
            else self.planned_job_count * self.outputs_per_job
        )
        if self.desired_accepted_count > planned_outputs:
            raise ValueError("desired accepted count exceeds the planned output count")
        if self.detailer_max_size < self.detailer_guide_size:
            raise ValueError("detailer maximum size must cover its guide size")
        return self

    @property
    def effective_planned_job_count(self) -> int:
        if not self.batches:
            return self.planned_job_count
        return sum(
            (batch.image_count + self.outputs_per_job - 1) // self.outputs_per_job
            for batch in self.batches
        )


@dataclass(frozen=True, slots=True)
class SubjectOption:
    approval_id: UUID
    slug: str
    name: str
    canonical_age: int


@dataclass(frozen=True, slots=True)
class ArtifactOption:
    approval_id: UUID
    name: str
    sha256: str


@dataclass(frozen=True, slots=True)
class WorkflowOption:
    approval_id: UUID
    name: str
    version: str
    sha256: str
    has_hires_pass: bool
    has_face_detailer: bool
    has_regional_prompting: bool
    capabilities: tuple[WorkflowCapability, ...]
    supports_controlled_duo_v2: bool
    supports_duo_strict_isolation: bool
    supports_duo_high_quality: bool


@dataclass(frozen=True, slots=True)
class WildcardOption:
    name: str
    version_no: int
    entry_count: int


@dataclass(frozen=True, slots=True)
class NewSetOptions:
    subjects: tuple[SubjectOption, ...]
    checkpoints: tuple[ArtifactOption, ...]
    loras: tuple[ArtifactOption, ...]
    workflows: tuple[WorkflowOption, ...]
    wildcards: tuple[WildcardOption, ...]

    @property
    def ready(self) -> bool:
        return bool(self.subjects and self.checkpoints and self.workflows)


def _workflow_option(row: WorkflowApproval) -> WorkflowOption:
    capabilities = effective_workflow_capabilities(
        row.capabilities or (),
        reviewed_node_classes=row.reviewed_node_classes,
    )
    return WorkflowOption(
        approval_id=row.id,
        name=row.name,
        version=row.version,
        sha256=row.workflow_sha256,
        has_hires_pass="LatentUpscaleBy" in row.reviewed_node_classes,
        has_face_detailer="FaceDetailer" in row.reviewed_node_classes,
        has_regional_prompting=(WorkflowCapability.REGIONAL_PROMPTING_V1 in capabilities),
        capabilities=tuple(sorted(capabilities, key=str)),
        supports_controlled_duo_v2=(WorkflowCapability.CONTROLLED_DUO_V2 in capabilities),
        supports_duo_strict_isolation=(WorkflowCapability.DUO_STRICT_ISOLATION in capabilities),
        supports_duo_high_quality=(WorkflowCapability.DUO_HIGH_QUALITY in capabilities),
    )


@dataclass(frozen=True, slots=True)
class NewSetResult:
    project: ProjectRead
    release: ReleaseRead
    generation_plan: GenerationPlanRead
    release_replayed: bool
    generation_plan_replayed: bool


@dataclass(frozen=True, slots=True)
class GenerationStateCount:
    state: GenerationState
    count: int


@dataclass(frozen=True, slots=True)
class GenerationProgressStageView:
    key: GenerationProgressStage
    step: int
    step_count: int
    label: str
    detail: str


@dataclass(frozen=True, slots=True)
class GenerationImageProgress:
    generated: int
    expected: int
    percent: float


@dataclass(frozen=True, slots=True)
class GenerationJobProgress:
    completed: int
    total: int
    active: int
    failed: int
    states: dict[str, int]


@dataclass(frozen=True, slots=True)
class GenerationScoringProgress:
    completed: int
    total: int
    percent: float


@dataclass(frozen=True, slots=True)
class GenerationProgressError:
    code: str
    message: str
    retryable: bool


@dataclass(frozen=True, slots=True)
class _ProviderPreparationSnapshot:
    phase: str
    progress: int | None
    stalled: bool


@dataclass(frozen=True, slots=True)
class NewSetStatus:
    release_id: UUID
    project_slug: str
    release_slug: str
    title: str
    phase: ReleasePhase
    health: ResourceHealth
    desired_accepted_count: int
    specification_sha256: str
    total_jobs: int
    expected_outputs: int
    jobs_by_state: tuple[GenerationStateCount, ...]
    stage: GenerationProgressStageView
    images: GenerationImageProgress
    jobs: GenerationJobProgress
    scoring: GenerationScoringProgress | None
    error: GenerationProgressError | None
    ready_for_review: bool
    next_url: str | None
    poll_after_ms: int
    gpu_billing: GpuBillingSnapshot
    stop_requested: bool = False
    stop_settled: bool = False
    can_stop: bool = False
    planned_outputs: int = 0


async def list_new_set_options(session: AsyncSession) -> NewSetOptions:
    subjects = tuple(
        SubjectOption(
            approval_id=row.id,
            slug=row.slug,
            name=row.display_name,
            canonical_age=row.canonical_age,
        )
        for row in (
            await session.scalars(
                select(SubjectApproval)
                .where(
                    SubjectApproval.status == ApprovalStatus.APPROVED,
                    SubjectApproval.is_current.is_(True),
                    SubjectApproval.clearly_adult.is_(True),
                    SubjectApproval.is_fictional.is_(True),
                    SubjectApproval.is_aged_up_minor.is_(False),
                    SubjectApproval.distribution_rights_approved.is_(True),
                    SubjectApproval.adult_derivative_rights_approved.is_(True),
                )
                .order_by(SubjectApproval.display_name, SubjectApproval.id)
            )
        ).all()
    )
    artifacts = list(
        (
            await session.scalars(
                select(ModelArtifactApproval)
                .where(
                    ModelArtifactApproval.status == ApprovalStatus.APPROVED,
                    ModelArtifactApproval.is_current.is_(True),
                    ModelArtifactApproval.commercial_use_approved.is_(True),
                    ModelArtifactApproval.adult_use_approved.is_(True),
                    ModelArtifactApproval.safetensors_verified.is_(True),
                )
                .order_by(ModelArtifactApproval.kind, ModelArtifactApproval.name)
            )
        ).all()
    )
    managed_lora_lifecycles: dict[UUID, ManagedLoraLifecycle] = {
        approval_id: lifecycle
        for approval_id, lifecycle in (
            await session.execute(
                select(
                    ManagedLoraArtifact.approval_id,
                    ManagedLoraArtifact.lifecycle,
                )
            )
        ).all()
    }
    checkpoints = tuple(
        ArtifactOption(
            approval_id=row.id,
            name=row.name,
            sha256=row.artifact_sha256,
        )
        for row in artifacts
        if row.kind == ModelArtifactKind.CHECKPOINT
    )
    loras = tuple(
        ArtifactOption(
            approval_id=row.id,
            name=row.name,
            sha256=row.artifact_sha256,
        )
        for row in artifacts
        if row.kind == ModelArtifactKind.LORA
        and (
            (
                row.id not in managed_lora_lifecycles
                and not row.storage_key.startswith("worker/managed-loras/sha256/")
            )
            or managed_lora_lifecycles.get(row.id) == ManagedLoraLifecycle.ACTIVE
        )
    )
    workflows = tuple(
        _workflow_option(row)
        for row in (
            await session.scalars(
                select(WorkflowApproval)
                .where(
                    WorkflowApproval.status == ApprovalStatus.APPROVED,
                    WorkflowApproval.is_current.is_(True),
                )
                .order_by(WorkflowApproval.name, WorkflowApproval.version)
            )
        ).all()
    )
    wildcard_libraries = await list_wildcard_libraries(session)
    wildcards = tuple(
        WildcardOption(
            name=library.name,
            version_no=library.current_version_no,
            entry_count=len(library.entries),
        )
        for library in wildcard_libraries
    )
    return NewSetOptions(
        subjects=subjects,
        checkpoints=checkpoints,
        loras=loras,
        workflows=workflows,
        wildcards=wildcards,
    )


async def create_and_approve_new_set(
    session: AsyncSession,
    *,
    command: NewSetSubmission,
    idempotency_key: str,
    settings: Settings,
    actor: str,
) -> NewSetResult:
    if not 8 <= len(idempotency_key) <= 200 or any(
        character.isspace() or ord(character) < 32 for character in idempotency_key
    ):
        raise NewSetInputError("the submission idempotency key is invalid")

    subject = await _approved_subject(session, command.subject_approval_id)
    secondary_subject = (
        await _approved_subject(session, command.secondary_subject_approval_id)
        if command.secondary_subject_approval_id is not None
        else None
    )
    checkpoint = await _approved_artifact(
        session,
        command.checkpoint_approval_id,
        expected_kind=ModelArtifactKind.CHECKPOINT,
    )
    lora_rows = [
        await _approved_artifact(
            session,
            selection.approval_id,
            expected_kind=ModelArtifactKind.LORA,
        )
        for selection in command.loras
    ]
    workflow = await _approved_workflow(session, command.workflow_approval_id)
    workflow_capabilities = effective_workflow_capabilities(
        workflow.capabilities or (),
        reviewed_node_classes=workflow.reviewed_node_classes,
    )
    if command.composition_mode == "duo" and command.duo_contract_version == 1:
        if WorkflowCapability.REGIONAL_PROMPTING_V1 not in workflow_capabilities:
            raise NewSetInputError(
                "two-character composition requires a legacy regional workflow profile"
            )
    if command.composition_mode == "duo" and command.duo_contract_version == 2:
        try:
            require_controlled_duo_capabilities(
                workflow_capabilities,
                isolation_mode=command.duo_isolation_mode,
                quality_mode=command.duo_quality_mode,
            )
        except ValueError as error:
            raise NewSetInputError(str(error)) from error
    if command.composition_mode == "single" and workflow_capabilities.intersection(
        {
            WorkflowCapability.REGIONAL_PROMPTING_V1,
            WorkflowCapability.CONTROLLED_DUO_V2,
        }
    ):
        raise NewSetInputError("single-character composition requires a standard workflow profile")
    try:
        require_generation_deliverability(
            width=command.width,
            height=command.height,
            hires_scale=command.hires_scale,
            workflow_node_classes=workflow.reviewed_node_classes,
        )
    except DeliverabilityError as error:
        raise NewSetInputError(str(error)) from error

    base_generation = GenerationParameters(
        prompt=(command.prompt if command.prompt.strip() else command.batches[0].prompt),
        negative_prompt=command.negative_prompt,
        detailer_prompt=command.detailer_prompt,
        detailer_negative_prompt=command.detailer_negative_prompt,
        seed=command.seed,
        width=command.width,
        height=command.height,
        steps=command.steps,
        cfg=command.cfg,
        sampler=command.sampler,
        scheduler=command.scheduler,
        clip_skip=command.clip_skip,
        outputs_per_job=command.outputs_per_job,
        hires_scale=command.hires_scale,
        hires_denoise=command.hires_denoise,
        hires_upscale_method=command.hires_upscale_method,
        detailer_guide_size=command.detailer_guide_size,
        detailer_max_size=command.detailer_max_size,
        detailer_denoise=command.detailer_denoise,
        detailer_bbox_threshold=command.detailer_bbox_threshold,
        detailer_bbox_dilation=command.detailer_bbox_dilation,
        detailer_bbox_crop_factor=command.detailer_bbox_crop_factor,
        detailer_feather=command.detailer_feather,
        composition_mode=command.composition_mode,
        duo_contract_version=command.duo_contract_version,
        composition_preset_id=command.composition_preset_id,
        character_a_prompt=command.character_a_prompt,
        character_b_prompt=command.character_b_prompt,
        character_a_negative_prompt=command.character_a_negative_prompt,
        character_b_negative_prompt=command.character_b_negative_prompt,
        interaction_prompt=command.interaction_prompt,
        camera_prompt=command.camera_prompt,
        duo_isolation_mode=command.duo_isolation_mode,
        duo_quality_mode=command.duo_quality_mode,
    )
    generation_batches: list[GenerationBatchSpecification] = []
    implicit_seed_offset = 0
    for batch in command.batches:
        batch_seed = (
            batch.seed
            if batch.seed is not None
            else (-1 if command.seed == -1 else (command.seed + implicit_seed_offset) % (2**63))
        )
        generation_batches.append(
            GenerationBatchSpecification(
                name=batch.name,
                image_count=batch.image_count,
                generation=base_generation.model_copy(
                    update={
                        "prompt": batch.prompt,
                        "negative_prompt": (
                            command.negative_prompt
                            if batch.negative_prompt is None
                            else batch.negative_prompt
                        ),
                        "detailer_prompt": (
                            command.detailer_prompt
                            if batch.detailer_prompt is None
                            else batch.detailer_prompt
                        ),
                        "detailer_negative_prompt": (
                            command.detailer_negative_prompt
                            if batch.detailer_negative_prompt is None
                            else batch.detailer_negative_prompt
                        ),
                        "seed": batch_seed,
                    }
                ),
            )
        )
        implicit_seed_offset += batch.image_count
    selected_generation = (
        generation_batches[0].generation if generation_batches else base_generation
    )

    specification = ReleaseSpecification(
        schema_version=2 if generation_batches else 1,
        subjects=[
            SubjectSpecification(
                name=row.display_name,
                canonical_source_url=row.canonical_source_url,
                canonical_age=row.canonical_age,
                clearly_adult_approved=True,
                adult_approval_evidence=(
                    f"Server approval {row.id}, version {row.approval_version}."
                ),
                is_real_person=False,
                is_aged_up_minor=False,
            )
            for row in (subject, secondary_subject)
            if row is not None
        ],
        checkpoint=ArtifactSpecification(
            name=checkpoint.name,
            source_url=checkpoint.source_url,
            storage_key=checkpoint.storage_key,
            sha256=checkpoint.artifact_sha256,
            license_url=checkpoint.license_url,
            commercial_use_approved=True,
            adult_use_approved=True,
        ),
        loras=[
            LoraSpecification(
                name=row.name,
                source_url=row.source_url,
                storage_key=row.storage_key,
                sha256=row.artifact_sha256,
                license_url=row.license_url,
                commercial_use_approved=True,
                adult_use_approved=True,
                weight=selection.weight,
            )
            for row, selection in zip(lora_rows, command.loras, strict=True)
        ],
        workflow=WorkflowSpecification(
            name=workflow.name,
            version=workflow.version,
            object_key=workflow.object_key,
            sha256=workflow.workflow_sha256,
            capabilities=tuple(sorted(workflow_capabilities, key=str)),
        ),
        generation=selected_generation,
        planned_job_count=command.effective_planned_job_count,
        generation_batches=generation_batches,
    )
    project = await _default_project(
        session,
        actor=actor,
        correlation_id=idempotency_key,
    )
    release_result = await create_release(
        session,
        project_id=project.id,
        command=ReleaseCreate(
            slug=command.slug,
            title=command.title,
            desired_accepted_count=command.desired_accepted_count,
            specification=specification,
        ),
        idempotency_key=f"{idempotency_key}:release",
        actor=actor,
    )
    plan_result = await approve_and_expand_generation_plan(
        session,
        release_id=release_result.response.id,
        idempotency_key=f"{idempotency_key}:generation-plan",
        settings=settings,
        actor=actor,
    )
    return NewSetResult(
        project=project,
        release=release_result.response,
        generation_plan=plan_result.response,
        release_replayed=release_result.replayed,
        generation_plan_replayed=plan_result.replayed,
    )


async def load_new_set_status(
    session: AsyncSession,
    *,
    release_id: UUID,
    gpu_billing_snapshot: GpuBillingSnapshot | None = None,
) -> NewSetStatus:
    row = (
        await session.execute(
            select(Release, Project, ReleaseVersion)
            .join(Project, Project.id == Release.project_id)
            .join(
                ReleaseVersion,
                (ReleaseVersion.release_id == Release.id)
                & (ReleaseVersion.version_no == Release.current_version_no),
            )
            .where(Release.id == release_id)
        )
    ).one_or_none()
    if row is None:
        raise NewSetNotFoundError("release was not found")
    release, project, version = row
    state_rows = (
        await session.execute(
            select(
                GenerationJob.state,
                func.count(GenerationJob.id),
                func.sum(GenerationJob.expected_output_count),
            )
            .where(GenerationJob.release_version_id == version.id)
            .group_by(GenerationJob.state)
            .order_by(GenerationJob.state)
        )
    ).all()
    state_counts = {state: int(count) for state, count, _outputs in state_rows}
    total_jobs = sum(state_counts.values())
    planned_outputs = sum(int(outputs or 0) for _state, _count, outputs in state_rows)
    stop_actions = frozenset(
        (
            await session.scalars(
                select(AuditEvent.action).where(
                    AuditEvent.resource_type == "release",
                    AuditEvent.resource_id == release.id,
                    AuditEvent.action.in_(
                        (GENERATION_STOP_REQUESTED_ACTION, GENERATION_STOPPED_ACTION)
                    ),
                )
            )
        ).all()
    )
    stop_requested = GENERATION_STOP_REQUESTED_ACTION in stop_actions
    stop_settled = GENERATION_STOPPED_ACTION in stop_actions
    intentionally_unfinished_states = {
        GenerationState.CANCELLED,
        GenerationState.FAILED,
        GenerationState.DEAD_LETTER,
    }
    expected_outputs = (
        sum(
            int(outputs or 0)
            for state, _count, outputs in state_rows
            if state not in intentionally_unfinished_states
        )
        if stop_requested
        else planned_outputs
    )
    generated_outputs = int(
        await session.scalar(
            select(func.count(Asset.id))
            .join(GenerationJob, GenerationJob.id == Asset.generation_job_id)
            .where(
                GenerationJob.release_version_id == version.id,
                Asset.release_id == release.id,
                Asset.kind == AssetKind.RAW_MASTER,
                Asset.state == AssetState.AVAILABLE,
            )
        )
        or 0
    )
    if stop_requested:
        # A failed provider job can still have uploaded valid masters before its
        # terminal error. Those retained images are part of the partial set, so
        # never show a denominator smaller than the number already preserved.
        expected_outputs = max(expected_outputs, generated_outputs)
    active_attempt_rows = (
        await session.execute(
            select(
                GenerationAttempt.state,
                func.count(GenerationAttempt.id),
            )
            .join(GenerationJob, GenerationJob.id == GenerationAttempt.job_id)
            .where(
                GenerationJob.release_version_id == version.id,
                GenerationAttempt.state.in_(
                    (
                        GenerationAttemptState.CREATED,
                        GenerationAttemptState.SUBMITTING,
                        GenerationAttemptState.SUBMITTED,
                        GenerationAttemptState.RUNNING,
                        GenerationAttemptState.UNKNOWN,
                        GenerationAttemptState.CANCEL_REQUESTED,
                    )
                ),
            )
            .group_by(GenerationAttempt.state)
        )
    ).all()
    attempt_counts = {state: int(count) for state, count in active_attempt_rows}

    scoring_run = await session.scalar(
        select(ScoringRun)
        .where(ScoringRun.release_version_id == version.id)
        .order_by(ScoringRun.created_at.desc(), ScoringRun.id.desc())
        .limit(1)
    )
    scoring_progress: GenerationScoringProgress | None = None
    ranking_count = 0
    if scoring_run is not None:
        if scoring_run.state == ScoringRunState.COMPLETED:
            scored_outputs = scoring_run.asset_count
        else:
            scored_outputs = int(
                await session.scalar(
                    select(func.count(AssetScore.id)).where(
                        AssetScore.scoring_run_id == scoring_run.id,
                        AssetScore.completed_at.is_not(None),
                    )
                )
                or 0
            )
        scoring_progress = GenerationScoringProgress(
            completed=scored_outputs,
            total=scoring_run.asset_count,
            percent=_progress_percent(scored_outputs, scoring_run.asset_count),
        )
        if scoring_run.state == ScoringRunState.COMPLETED:
            ranking_count = int(
                await session.scalar(
                    select(func.count(AssetRanking.id)).where(
                        AssetRanking.scoring_run_id == scoring_run.id
                    )
                )
                or 0
            )

    completed_jobs = state_counts.get(GenerationState.SUCCEEDED, 0)
    if stop_requested:
        completed_jobs += sum(
            state_counts.get(state, 0) for state in intentionally_unfinished_states
        )
    failed_states = (
        (GenerationState.FAILED, GenerationState.DEAD_LETTER)
        if stop_requested
        else (
            GenerationState.FAILED,
            GenerationState.DEAD_LETTER,
            GenerationState.CANCELLED,
        )
    )
    failed_jobs = sum(state_counts.get(state, 0) for state in failed_states)
    active_jobs = sum(
        state_counts.get(state, 0)
        for state in (
            GenerationState.CLAIMED,
            GenerationState.SUBMITTING,
            GenerationState.RUNNING,
            GenerationState.COLLECTING,
            GenerationState.VERIFYING,
            GenerationState.UNKNOWN,
            GenerationState.CANCEL_REQUESTED,
        )
    )
    ready_for_review = bool(
        scoring_run is not None
        and scoring_run.state == ScoringRunState.COMPLETED
        and ranking_count == scoring_run.asset_count
    )
    terminal_job_count = sum(
        state_counts.get(state, 0)
        for state in (
            GenerationState.SUCCEEDED,
            GenerationState.FAILED,
            GenerationState.DEAD_LETTER,
            GenerationState.CANCELLED,
        )
    )
    can_stop = bool(
        not stop_requested
        and release.phase in {ReleasePhase.READY, ReleasePhase.GENERATING}
        and terminal_job_count < total_jobs
    )
    stage, progress_error = _generation_progress_stage(
        release=release,
        state_counts=state_counts,
        attempt_counts=attempt_counts,
        generated_outputs=generated_outputs,
        expected_outputs=expected_outputs,
        scoring_run=scoring_run,
        scoring_progress=scoring_progress,
        ranking_count=ranking_count,
        ready_for_review=ready_for_review,
        failed_jobs=failed_jobs,
        stop_requested=stop_requested,
    )
    status_now = datetime.now(UTC)
    if stage.key in {GenerationProgressStage.QUEUED, GenerationProgressStage.GPU_STARTING}:
        current_deployment = await session.scalar(
            select(SaladDeployment).where(SaladDeployment.is_current.is_(True)).limit(1)
        )
        stage, progress_error = _overlay_provider_preparation(
            stage,
            progress_error,
            deployment=current_deployment,
            now=status_now,
        )
    if stage.key == GenerationProgressStage.SCORING and scoring_progress is None:
        scoring_progress = GenerationScoringProgress(
            completed=0,
            total=generated_outputs,
            percent=0.0,
        )
    gpu_billing = gpu_billing_snapshot or await load_shared_gpu_billing_snapshot(
        session,
        now=status_now,
    )
    poll_after_ms = (
        0
        if stop_settled and release.phase == ReleasePhase.CANCELLED
        else 3_000
        if stop_requested and not stop_settled and release.phase == ReleasePhase.PAUSED
        else _poll_after_ms(stage.key)
    )
    poll_after_ms = gpu_billing_poll_after_ms(
        gpu_billing,
        settled_poll_after_ms=poll_after_ms,
    )
    return NewSetStatus(
        release_id=release.id,
        project_slug=project.slug,
        release_slug=release.slug,
        title=release.title,
        phase=release.phase,
        health=release.health,
        desired_accepted_count=release.desired_accepted_count,
        specification_sha256=version.specification_sha256,
        total_jobs=total_jobs,
        expected_outputs=expected_outputs,
        jobs_by_state=tuple(
            GenerationStateCount(state=state, count=int(count))
            for state, count, _outputs in state_rows
        ),
        stage=stage,
        images=GenerationImageProgress(
            generated=generated_outputs,
            expected=expected_outputs,
            percent=_progress_percent(generated_outputs, expected_outputs),
        ),
        jobs=GenerationJobProgress(
            completed=completed_jobs,
            total=total_jobs,
            active=active_jobs,
            failed=failed_jobs,
            states={state.value: count for state, count in state_counts.items()},
        ),
        scoring=scoring_progress,
        error=progress_error,
        ready_for_review=ready_for_review,
        next_url=(f"/dashboard/releases/{release.id}" if ready_for_review else None),
        poll_after_ms=poll_after_ms,
        gpu_billing=gpu_billing,
        stop_requested=stop_requested,
        stop_settled=stop_settled,
        can_stop=can_stop,
        planned_outputs=planned_outputs,
    )


def new_set_progress_payload(
    progress: NewSetStatus,
    *,
    include_gpu_billing: bool = True,
) -> dict[str, object]:
    scoring: dict[str, object] | None = None
    if progress.scoring is not None:
        scoring = {
            "completed": progress.scoring.completed,
            "total": progress.scoring.total,
            "percent": progress.scoring.percent,
        }
    error: dict[str, object] | None = None
    if progress.error is not None:
        error = {
            "code": progress.error.code,
            "message": progress.error.message,
            "retryable": progress.error.retryable,
        }
    payload: dict[str, object] = {
        "schema_version": 2,
        "release_id": str(progress.release_id),
        "phase": progress.phase.value,
        "health": progress.health.value,
        "stage": {
            "key": progress.stage.key.value,
            "step": progress.stage.step,
            "step_count": progress.stage.step_count,
            "label": progress.stage.label,
            "detail": progress.stage.detail,
        },
        "images": {
            "generated": progress.images.generated,
            "expected": progress.images.expected,
            "percent": progress.images.percent,
        },
        "jobs": {
            "completed": progress.jobs.completed,
            "total": progress.jobs.total,
            "active": progress.jobs.active,
            "failed": progress.jobs.failed,
            "states": progress.jobs.states,
        },
        "scoring": scoring,
        "error": error,
        "stop": {
            "available": progress.can_stop,
            "requested": progress.stop_requested,
            "settled": progress.stop_settled,
        },
        "ready_for_review": progress.ready_for_review,
        "next_url": progress.next_url,
        "poll_after_ms": progress.poll_after_ms,
    }
    if include_gpu_billing:
        payload["gpu_billing"] = gpu_billing_payload(progress.gpu_billing)
    return payload


def _generation_progress_stage(
    *,
    release: Release,
    state_counts: dict[GenerationState, int],
    attempt_counts: dict[GenerationAttemptState, int],
    generated_outputs: int,
    expected_outputs: int,
    scoring_run: ScoringRun | None,
    scoring_progress: GenerationScoringProgress | None,
    ranking_count: int,
    ready_for_review: bool,
    failed_jobs: int,
    stop_requested: bool = False,
) -> tuple[GenerationProgressStageView, GenerationProgressError | None]:
    release_unhealthy = release.health == ResourceHealth.BLOCKED or (
        stop_requested and release.health != ResourceHealth.HEALTHY
    )
    # An operator stop deliberately turns provider/job failures into a reviewable
    # partial set. Integrity failures still mark the release unhealthy and must
    # remain fail-closed, but a terminal GPU job by itself must not hide the
    # masters that were successfully preserved before it failed.
    if release_unhealthy or (failed_jobs and not stop_requested):
        error_code = (
            "generation_failed"
            if failed_jobs
            else (
                "release_blocked"
                if release.health == ResourceHealth.BLOCKED
                else "release_unhealthy"
            )
        )
        return (
            _stage(
                GenerationProgressStage.ERROR,
                step=_generation_step(state_counts, scoring_run),
                label="Generation needs attention",
                detail=(
                    f"{generated_outputs} of {expected_outputs} verified raw masters are safe."
                ),
            ),
            GenerationProgressError(
                code=error_code,
                message=(
                    "One or more generation jobs need operator attention."
                    if failed_jobs
                    else "The release is unhealthy and its retained images need operator attention."
                ),
                retryable=False,
            ),
        )
    if release.phase == ReleasePhase.CANCELLED:
        return (
            _stage(
                GenerationProgressStage.CANCELLED,
                step=_generation_step(state_counts, scoring_run),
                label=("Generation stopped" if stop_requested else "Run cancelled"),
                detail=(
                    f"{generated_outputs} of {expected_outputs} verified raw masters "
                    "were completed."
                ),
            ),
            (
                None
                if stop_requested
                else GenerationProgressError(
                    code="release_cancelled",
                    message="This generation run was cancelled.",
                    retryable=False,
                )
            ),
        )
    if ready_for_review:
        return (
            _stage(
                GenerationProgressStage.REVIEW,
                step=5,
                label="Ready for review",
                detail=f"All {ranking_count} images are ranked and ready to inspect.",
            ),
            None,
        )
    if release.phase == ReleasePhase.PAUSED:
        return (
            _stage(
                GenerationProgressStage.PAUSED,
                step=_generation_step(state_counts, scoring_run),
                label=("Stopping generation" if stop_requested else "Run paused"),
                detail=(
                    (
                        f"{generated_outputs} verified images are saved. Active GPU jobs "
                        "are being cancelled; no new jobs from this set will start."
                    )
                    if stop_requested
                    else (
                        f"Progress is saved at {generated_outputs} of {expected_outputs} "
                        "verified images."
                    )
                ),
            ),
            None,
        )
    if scoring_run is not None and scoring_run.state == ScoringRunState.COMPLETED:
        return (
            _stage(
                GenerationProgressStage.ERROR,
                step=4,
                label="Ranking needs attention",
                detail="Quality scoring finished, but the ranked snapshot is incomplete.",
            ),
            GenerationProgressError(
                code="ranking_incomplete",
                message="The completed ranking needs operator attention.",
                retryable=False,
            ),
        )
    if release.phase == ReleasePhase.REVIEWING or scoring_run is not None:
        scored = scoring_progress.completed if scoring_progress is not None else 0
        total = scoring_progress.total if scoring_progress is not None else generated_outputs
        return (
            _stage(
                GenerationProgressStage.SCORING,
                step=4,
                label="Scoring image quality",
                detail=f"{scored} of {total} images have completed quality scoring.",
            ),
            None,
        )

    generating_states = (
        GenerationState.COLLECTING,
        GenerationState.VERIFYING,
        GenerationState.SUCCEEDED,
    )
    if (
        attempt_counts.get(GenerationAttemptState.RUNNING, 0)
        or generated_outputs
        or any(state_counts.get(state, 0) for state in generating_states)
    ):
        return (
            _stage(
                GenerationProgressStage.GENERATING,
                step=3,
                label="Generating images",
                detail=(
                    f"{generated_outputs} of {expected_outputs} verified raw masters are ready."
                ),
            ),
            None,
        )

    starting_attempt_states = (
        GenerationAttemptState.CREATED,
        GenerationAttemptState.SUBMITTING,
        GenerationAttemptState.SUBMITTED,
        GenerationAttemptState.UNKNOWN,
        GenerationAttemptState.CANCEL_REQUESTED,
    )
    starting_job_states = (
        GenerationState.CLAIMED,
        GenerationState.SUBMITTING,
        GenerationState.RUNNING,
        GenerationState.UNKNOWN,
        GenerationState.CANCEL_REQUESTED,
    )
    if any(attempt_counts.get(state, 0) for state in starting_attempt_states) or any(
        state_counts.get(state, 0) for state in starting_job_states
    ):
        return (
            _stage(
                GenerationProgressStage.GPU_STARTING,
                step=2,
                label="GPU worker starting",
                detail="The cloud GPU job is accepted and its worker is starting.",
            ),
            None,
        )

    retrying = state_counts.get(GenerationState.RETRY_WAIT, 0)
    detail = (
        f"Waiting to retry {retrying} GPU job{'s' if retrying != 1 else ''}."
        if retrying
        else "Generation jobs are queued for cloud GPU capacity."
    )
    return (
        _stage(
            GenerationProgressStage.QUEUED,
            step=1,
            label="Queued",
            detail=detail,
        ),
        None,
    )


def _stage(
    key: GenerationProgressStage,
    *,
    step: int,
    label: str,
    detail: str,
) -> GenerationProgressStageView:
    return GenerationProgressStageView(
        key=key,
        step=step,
        step_count=5,
        label=label,
        detail=detail,
    )


def _overlay_provider_preparation(
    stage: GenerationProgressStageView,
    error: GenerationProgressError | None,
    *,
    deployment: SaladDeployment | None,
    now: datetime,
) -> tuple[GenerationProgressStageView, GenerationProgressError | None]:
    if stage.key not in {GenerationProgressStage.QUEUED, GenerationProgressStage.GPU_STARTING}:
        return stage, error
    snapshot = _provider_preparation_snapshot(deployment, now=now)
    if snapshot is None:
        return stage, error
    if snapshot.stalled:
        return (
            _stage(
                GenerationProgressStage.ERROR,
                step=stage.step,
                label="Worker image preparation stalled",
                detail=(
                    "The worker image has reported no preparation progress for at least "
                    "30 minutes. The system will keep checking automatically."
                ),
            ),
            GenerationProgressError(
                code="provider_image_preparation_stalled",
                message=(
                    "Worker image preparation has not advanced for at least 30 minutes. "
                    "You can stop this run safely; no generated images will be discarded."
                ),
                retryable=True,
            ),
        )
    if snapshot.phase != "image_pull":
        return stage, error
    progress_suffix = f" ({snapshot.progress}%)" if snapshot.progress is not None else ""
    return (
        _stage(
            stage.key,
            step=stage.step,
            label=f"Preparing worker image{progress_suffix}",
            detail=(
                "The reusable cloud worker image is being prepared. Generation will start "
                "automatically when it is ready."
            ),
        ),
        error,
    )


def _provider_preparation_snapshot(
    deployment: SaladDeployment | None,
    *,
    now: datetime,
) -> _ProviderPreparationSnapshot | None:
    if deployment is None or not deployment.is_current:
        return None
    if deployment.last_error_code not in _PROVIDER_PREPARATION_ERROR_CODES:
        return None
    expected_state = (
        SaladDeploymentState.DEGRADED
        if deployment.last_error_code == "provider_image_preparation_stalled"
        else SaladDeploymentState.PROVISIONING
    )
    if deployment.state != expected_state or deployment.last_observed_at is None:
        return None
    observed_at = _stored_as_utc(deployment.last_observed_at)
    current_time = _stored_as_utc(now)
    age = current_time - observed_at
    if age < timedelta(0) or age > _PROVIDER_STATUS_STALE_AFTER:
        return None
    fields = _strict_provider_status_fields(deployment.provider_status)
    if fields is None or fields["pending"] != "1":
        return None
    phase = fields["phase"]
    if phase not in _PROVIDER_PREPARATION_PHASES:
        return None
    progress_value = fields["progress"]
    if progress_value == "unknown":
        progress = None
    elif progress_value.isascii() and progress_value.isdecimal():
        progress = int(progress_value)
        if not 0 <= progress <= 100:
            return None
    else:
        return None
    return _ProviderPreparationSnapshot(
        phase=phase,
        progress=progress,
        stalled=deployment.last_error_code == "provider_image_preparation_stalled",
    )


def _strict_provider_status_fields(status: str | None) -> dict[str, str] | None:
    if status is None or not status.isascii():
        return None
    fields: dict[str, str] = {}
    for item in status.split(";"):
        key, separator, value = item.partition("=")
        if not separator or not key or not value or key in fields:
            return None
        fields[key] = value
    if set(fields) != {"queue", "group", "pending", "phase", "progress"}:
        return None
    queue_value = fields["queue"]
    queue_digits = queue_value[:-1] if queue_value.endswith("+") else queue_value
    if not queue_digits.isdecimal() or not queue_digits.isascii():
        return None
    if not 0 <= int(queue_digits) <= 999_999_999:
        return None
    if fields["group"] not in _PROVIDER_GROUP_STATUSES:
        return None
    return fields


def _stored_as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _generation_step(
    state_counts: dict[GenerationState, int],
    scoring_run: ScoringRun | None,
) -> int:
    if scoring_run is not None:
        return 4
    if any(
        state_counts.get(state, 0)
        for state in (
            GenerationState.RUNNING,
            GenerationState.COLLECTING,
            GenerationState.VERIFYING,
            GenerationState.SUCCEEDED,
        )
    ):
        return 3
    if any(
        state_counts.get(state, 0)
        for state in (
            GenerationState.CLAIMED,
            GenerationState.SUBMITTING,
            GenerationState.UNKNOWN,
        )
    ):
        return 2
    return 1


def _progress_percent(completed: int, total: int) -> float:
    if total <= 0:
        return 0.0
    return round(min(max(completed, 0), total) * 100 / total, 1)


def _poll_after_ms(stage: GenerationProgressStage) -> int:
    if stage in {GenerationProgressStage.REVIEW, GenerationProgressStage.CANCELLED}:
        return 0
    if stage in {GenerationProgressStage.ERROR, GenerationProgressStage.PAUSED}:
        return 10_000
    return 3_000


async def _default_project(
    session: AsyncSession,
    *,
    actor: str,
    correlation_id: str,
) -> ProjectRead:
    project = await session.scalar(select(Project).where(Project.slug == "main"))
    if project is None:
        project = await session.scalar(select(Project).order_by(Project.created_at, Project.id))
    if project is not None:
        return ProjectRead.model_validate(project)
    try:
        return await create_project(
            session,
            ProjectCreate(slug="main", name="Main"),
            actor=actor,
            correlation_id=correlation_id,
        )
    except IntegrityError:
        await session.rollback()
        concurrent = await session.scalar(select(Project).where(Project.slug == "main"))
        if concurrent is None:
            raise
        return ProjectRead.model_validate(concurrent)


async def _approved_subject(
    session: AsyncSession,
    approval_id: UUID,
) -> SubjectApproval:
    subject = await session.scalar(
        select(SubjectApproval).where(
            SubjectApproval.id == approval_id,
            SubjectApproval.status == ApprovalStatus.APPROVED,
            SubjectApproval.is_current.is_(True),
            SubjectApproval.clearly_adult.is_(True),
            SubjectApproval.is_fictional.is_(True),
            SubjectApproval.is_aged_up_minor.is_(False),
            SubjectApproval.distribution_rights_approved.is_(True),
            SubjectApproval.adult_derivative_rights_approved.is_(True),
        )
    )
    if subject is None:
        raise NewSetInputError("the selected subject is no longer approved")
    return subject


async def _approved_artifact(
    session: AsyncSession,
    approval_id: UUID,
    *,
    expected_kind: ModelArtifactKind,
) -> ModelArtifactApproval:
    artifact = await session.scalar(
        select(ModelArtifactApproval).where(
            ModelArtifactApproval.id == approval_id,
            ModelArtifactApproval.kind == expected_kind,
            ModelArtifactApproval.status == ApprovalStatus.APPROVED,
            ModelArtifactApproval.is_current.is_(True),
            ModelArtifactApproval.commercial_use_approved.is_(True),
            ModelArtifactApproval.adult_use_approved.is_(True),
            ModelArtifactApproval.safetensors_verified.is_(True),
        )
    )
    if artifact is None:
        raise NewSetInputError(f"the selected {expected_kind.value} is no longer approved")
    if expected_kind == ModelArtifactKind.LORA:
        lifecycle = await session.scalar(
            select(ManagedLoraArtifact.lifecycle)
            .where(ManagedLoraArtifact.approval_id == artifact.id)
            .with_for_update(read=True)
        )
        if (
            lifecycle is None and artifact.storage_key.startswith("worker/managed-loras/sha256/")
        ) or (lifecycle is not None and lifecycle != ManagedLoraLifecycle.ACTIVE):
            raise NewSetInputError("the selected lora is not active on the worker")
    return artifact


async def _approved_workflow(
    session: AsyncSession,
    approval_id: UUID,
) -> WorkflowApproval:
    workflow = await session.scalar(
        select(WorkflowApproval).where(
            WorkflowApproval.id == approval_id,
            WorkflowApproval.status == ApprovalStatus.APPROVED,
            WorkflowApproval.is_current.is_(True),
        )
    )
    if workflow is None:
        raise NewSetInputError("the selected workflow profile is no longer approved")
    return workflow
