from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from gen_automation.db.models import (
    GenerationJob,
    ModelArtifactApproval,
    Project,
    Release,
    ReleaseVersion,
    SubjectApproval,
    WorkflowApproval,
)
from gen_automation.domain.deliverability import (
    MAX_ACCEPTED_IMAGES_PER_RELEASE,
    DeliverabilityError,
    require_generation_deliverability,
)
from gen_automation.domain.enums import (
    ApprovalStatus,
    GenerationState,
    ModelArtifactKind,
    ReleasePhase,
    ResourceHealth,
)
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
from gen_automation.services.releases import create_project, create_release
from gen_automation.services.wildcards import list_wildcard_libraries


class NewSetInputError(ValueError):
    """The operator's selection no longer maps to an approved release."""


class NewSetNotFoundError(LookupError):
    pass


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
    seed: int | None = Field(default=None, ge=0, le=(2**63) - 1)

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
    checkpoint_approval_id: UUID
    loras: tuple[NewSetLoraSelection, ...] = Field(default=(), max_length=8)
    workflow_approval_id: UUID
    prompt: str = Field(default="", max_length=20_000)
    negative_prompt: str = Field(default="", max_length=20_000)
    detailer_prompt: str = Field(default="", max_length=20_000)
    detailer_negative_prompt: str = Field(default="", max_length=20_000)
    batches: tuple[NewSetBatchSubmission, ...] = Field(default=(), max_length=50)
    seed: int = Field(ge=0, le=(2**63) - 1)
    width: int = Field(ge=512, le=4096, multiple_of=8)
    height: int = Field(ge=512, le=4096, multiple_of=8)
    cfg: float = Field(default=5.0, ge=0.0, le=30.0)
    steps: int = Field(ge=1, le=200)
    sampler: str = Field(min_length=1, max_length=100)
    scheduler: str = Field(min_length=1, max_length=100)
    clip_skip: int = Field(default=2, ge=1, le=12)
    outputs_per_job: int = Field(ge=1, le=8)
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
    )
    workflows = tuple(
        WorkflowOption(
            approval_id=row.id,
            name=row.name,
            version=row.version,
        )
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
    actor: str,
) -> NewSetResult:
    if not 8 <= len(idempotency_key) <= 200 or any(
        character.isspace() or ord(character) < 32 for character in idempotency_key
    ):
        raise NewSetInputError("the submission idempotency key is invalid")

    subject = await _approved_subject(session, command.subject_approval_id)
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
    )
    generation_batches: list[GenerationBatchSpecification] = []
    implicit_seed_offset = 0
    for batch in command.batches:
        batch_seed = (
            batch.seed
            if batch.seed is not None
            else (command.seed + implicit_seed_offset) % (2**63)
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
                name=subject.display_name,
                canonical_source_url=subject.canonical_source_url,
                canonical_age=subject.canonical_age,
                clearly_adult_approved=True,
                adult_approval_evidence=(
                    f"Server approval {subject.id}, version {subject.approval_version}."
                ),
                is_real_person=False,
                is_aged_up_minor=False,
            )
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
    return NewSetStatus(
        release_id=release.id,
        project_slug=project.slug,
        release_slug=release.slug,
        title=release.title,
        phase=release.phase,
        health=release.health,
        desired_accepted_count=release.desired_accepted_count,
        specification_sha256=version.specification_sha256,
        total_jobs=sum(int(count) for _state, count, _outputs in state_rows),
        expected_outputs=sum(int(outputs or 0) for _state, _count, outputs in state_rows),
        jobs_by_state=tuple(
            GenerationStateCount(state=state, count=int(count))
            for state, count, _outputs in state_rows
        ),
    )


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
