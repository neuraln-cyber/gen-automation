from collections.abc import Mapping
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from gen_automation.domain.controlled_duo import (
    DuoCompositionPreset,
    DuoIsolationMode,
    DuoQualityMode,
    TrioCompositionPreset,
    WorkflowCapability,
    require_coherent_workflow_capabilities,
    require_controlled_duo_capabilities,
    require_controlled_trio_capabilities,
)
from gen_automation.domain.deliverability import MAX_ACCEPTED_IMAGES_PER_RELEASE
from gen_automation.domain.generation_limits import (
    MAX_INLINE_OUTPUTS_PER_SIGNED_GENERATION_JOB,
    MAX_OUTPUTS_PER_GENERATION_JOB,
    MAX_PROMPT_TEXT_BYTES_PER_GENERATION_JOB,
    MAX_SAFE_OUTPUTS_PER_SIGNED_GENERATION_JOB,
    MAX_SIGNED_PROMPT_BUDGET_BYTES_PER_GENERATION_JOB,
    referenced_worker_prompt_budget_bytes,
    signed_worker_prompt_budget_bytes,
    utf8_prompt_bytes,
)

Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Slug = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", min_length=1, max_length=80),
]
WildcardName = Annotated[
    str,
    StringConstraints(
        pattern=r"^[a-z0-9]+(?:[._/-][a-z0-9]+)*$",
        min_length=1,
        max_length=80,
    ),
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ArtifactSpecification(StrictModel):
    name: str = Field(min_length=1, max_length=200)
    source_url: AnyHttpUrl
    storage_key: str = Field(min_length=1, max_length=1024)
    sha256: Sha256
    license_url: AnyHttpUrl
    commercial_use_approved: bool
    adult_use_approved: bool
    experiment_only: bool = False

    @model_validator(mode="after")
    def require_licence_approval(self) -> "ArtifactSpecification":
        if not self.adult_use_approved:
            raise ValueError("artifact requires adult-use approval")
        if not self.commercial_use_approved and not self.experiment_only:
            raise ValueError("non-commercial artifacts must be restricted to experiment-only use")
        if not self.storage_key.lower().endswith(".safetensors"):
            raise ValueError("production model artifacts must use Safetensors")
        return self


class LoraSpecification(ArtifactSpecification):
    weight: float = Field(ge=-2.0, le=2.0)


class SubjectSpecification(StrictModel):
    name: str = Field(min_length=1, max_length=200)
    canonical_source_url: AnyHttpUrl
    canonical_age: int = Field(ge=18, le=10000)
    clearly_adult_approved: bool
    adult_approval_evidence: str = Field(min_length=1, max_length=2000)
    is_real_person: bool = False
    is_aged_up_minor: bool = False

    @model_validator(mode="after")
    def enforce_subject_gate(self) -> "SubjectSpecification":
        if not self.clearly_adult_approved:
            raise ValueError("subject must be explicitly approved as clearly adult")
        if self.is_real_person:
            raise ValueError("real-person generation is disabled in the initial system")
        if self.is_aged_up_minor:
            raise ValueError("aged-up minor characters are not permitted")
        return self


class WorkflowSpecification(StrictModel):
    name: str = Field(min_length=1, max_length=200)
    version: str = Field(min_length=1, max_length=100)
    object_key: str = Field(min_length=1, max_length=1024)
    sha256: Sha256
    capabilities: tuple[WorkflowCapability, ...] = Field(default=(), max_length=16)

    @field_validator("capabilities")
    @classmethod
    def require_unique_capabilities(
        cls,
        values: tuple[WorkflowCapability, ...],
    ) -> tuple[WorkflowCapability, ...]:
        if len(values) != len(set(values)):
            raise ValueError("workflow capabilities must be unique")
        require_coherent_workflow_capabilities(values)
        return tuple(sorted(values, key=str))


class GenerationParameters(StrictModel):
    composition_mode: Literal["single", "duo", "trio"] = "single"
    duo_contract_version: Literal[1, 2, 3] = 1
    composition_preset_id: DuoCompositionPreset | TrioCompositionPreset | None = None
    prompt: str = Field(min_length=1, max_length=20000)
    character_a_prompt: str = Field(default="", max_length=20000)
    character_b_prompt: str = Field(default="", max_length=20000)
    character_a_pose_prompt: str = Field(default="", max_length=20000)
    character_b_pose_prompt: str = Field(default="", max_length=20000)
    character_c_prompt: str = Field(default="", max_length=20000)
    character_c_pose_prompt: str = Field(default="", max_length=20000)
    character_a_negative_prompt: str = Field(default="", max_length=20000)
    character_b_negative_prompt: str = Field(default="", max_length=20000)
    character_c_negative_prompt: str = Field(default="", max_length=20000)
    interaction_prompt: str = Field(default="", max_length=20000)
    camera_prompt: str = Field(default="", max_length=20000)
    duo_isolation_mode: DuoIsolationMode = DuoIsolationMode.BALANCED
    duo_quality_mode: DuoQualityMode = DuoQualityMode.STANDARD
    negative_prompt: str = Field(default="", max_length=20000)
    detailer_prompt: str = Field(default="", max_length=20000)
    detailer_negative_prompt: str = Field(default="", max_length=20000)
    # -1 mirrors the A1111/Forge sentinel. It is resolved into a concrete,
    # per-output seed before a job is sent to a worker.
    seed: int = Field(ge=-1, le=(2**63) - 1)
    width: int = Field(ge=512, le=4096, multiple_of=8)
    height: int = Field(ge=512, le=4096, multiple_of=8)
    steps: int = Field(ge=1, le=200)
    cfg: float = Field(default=5.0, ge=0.0, le=30.0)
    sampler: str = Field(min_length=1, max_length=100)
    scheduler: str = Field(min_length=1, max_length=100)
    clip_skip: int = Field(default=2, ge=1, le=12)
    outputs_per_job: int = Field(default=4, ge=1, le=MAX_OUTPUTS_PER_GENERATION_JOB)
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

    @model_validator(mode="after")
    def validate_detailer_sizes(self) -> "GenerationParameters":
        if self.detailer_max_size < self.detailer_guide_size:
            raise ValueError("detailer maximum size must cover its guide size")
        if self.composition_mode == "duo":
            if not self.character_a_prompt.strip() or not self.character_b_prompt.strip():
                raise ValueError("two-character composition requires both character prompts")
            if self.duo_contract_version == 1:
                if (
                    self.composition_preset_id is not None
                    or self.character_a_pose_prompt
                    or self.character_b_pose_prompt
                    or self.character_a_negative_prompt
                    or self.character_b_negative_prompt
                    or self.interaction_prompt
                    or self.camera_prompt
                    or self.duo_isolation_mode != DuoIsolationMode.BALANCED
                    or self.duo_quality_mode != DuoQualityMode.STANDARD
                ):
                    raise ValueError("Controlled Duo fields require duo contract version 2")
            elif self.duo_contract_version == 2 and self.composition_preset_id is None:
                raise ValueError("Controlled Duo v2 requires a composition preset")
            elif self.duo_contract_version != 2:
                raise ValueError("two-character composition requires duo contract version 1 or 2")
            elif not isinstance(self.composition_preset_id, DuoCompositionPreset):
                raise ValueError("two-character composition requires a two-character layout")
            if (
                self.character_c_prompt
                or self.character_c_pose_prompt
                or self.character_c_negative_prompt
            ):
                raise ValueError("two-character composition cannot include Character C fields")
        elif self.composition_mode == "trio":
            if self.duo_contract_version != 3:
                raise ValueError("three-character composition requires Controlled Trio contract v1")
            if not all(
                prompt.strip()
                for prompt in (
                    self.character_a_prompt,
                    self.character_b_prompt,
                    self.character_c_prompt,
                )
            ):
                raise ValueError("three-character composition requires all three character prompts")
            if not isinstance(self.composition_preset_id, TrioCompositionPreset):
                raise ValueError("three-character composition requires a three-character layout")
            if self.duo_isolation_mode != DuoIsolationMode.BALANCED:
                raise ValueError("Controlled Trio v1 currently supports balanced isolation only")
            if self.duo_quality_mode == DuoQualityMode.HIGH:
                raise ValueError("high trio quality is not implemented")
        elif (
            self.duo_contract_version != 1
            or self.composition_preset_id is not None
            or self.character_a_prompt
            or self.character_b_prompt
            or self.character_a_pose_prompt
            or self.character_b_pose_prompt
            or self.character_c_prompt
            or self.character_c_pose_prompt
            or self.character_a_negative_prompt
            or self.character_b_negative_prompt
            or self.character_c_negative_prompt
            or self.interaction_prompt
            or self.camera_prompt
            or self.duo_isolation_mode != DuoIsolationMode.BALANCED
            or self.duo_quality_mode != DuoQualityMode.STANDARD
        ):
            raise ValueError("Controlled Duo fields require two-character composition")
        prompt_bytes = utf8_prompt_bytes(
            (
                self.prompt,
                self.character_a_prompt,
                self.character_b_prompt,
                self.character_a_pose_prompt,
                self.character_b_pose_prompt,
                self.character_c_prompt,
                self.character_c_pose_prompt,
                self.character_a_negative_prompt,
                self.character_b_negative_prompt,
                self.character_c_negative_prompt,
                self.interaction_prompt,
                self.camera_prompt,
                self.negative_prompt,
                self.detailer_prompt,
                self.detailer_negative_prompt,
            )
        )
        if prompt_bytes * self.outputs_per_job > MAX_PROMPT_TEXT_BYTES_PER_GENERATION_JOB:
            raise ValueError("prompt text is too large for one multi-output generation job")
        return self


class GenerationBatchSpecification(StrictModel):
    """One ordered prompt segment within a release generation plan."""

    name: str = Field(min_length=1, max_length=100)
    image_count: int = Field(ge=1, le=80_000)
    generation: GenerationParameters

    @model_validator(mode="after")
    def require_trimmed_name(self) -> "GenerationBatchSpecification":
        if self.name != self.name.strip():
            raise ValueError("generation batch name must be trimmed")
        return self

    @property
    def planned_job_count(self) -> int:
        outputs_per_job = self.generation.outputs_per_job
        return (self.image_count + outputs_per_job - 1) // outputs_per_job


class WildcardVersionReference(StrictModel):
    """An immutable wildcard-library version frozen into a release."""

    name: WildcardName
    library_id: UUID
    version_id: UUID
    version_no: int = Field(ge=1)
    entries_sha256: Sha256
    entry_count: int = Field(ge=1, le=2000)


class ReleaseSpecification(StrictModel):
    schema_version: int = Field(default=1, ge=1, le=2)
    worker_request_budget_version: Literal[1, 2] = Field(
        default=1,
        exclude_if=lambda value: value == 1,
    )
    subjects: list[SubjectSpecification] = Field(min_length=1, max_length=20)
    checkpoint: ArtifactSpecification
    loras: list[LoraSpecification] = Field(default_factory=list, max_length=8)
    workflow: WorkflowSpecification
    generation: GenerationParameters
    planned_job_count: int = Field(ge=1, le=10000)
    generation_batches: list[GenerationBatchSpecification] = Field(
        default_factory=list,
        max_length=50,
        exclude_if=lambda value: not value,
    )
    wildcard_versions: list[WildcardVersionReference] = Field(
        default_factory=list,
        max_length=64,
    )

    @model_validator(mode="after")
    def validate_release_plan(self) -> "ReleaseSpecification":
        names = [reference.name for reference in self.wildcard_versions]
        if len(names) != len(set(names)):
            raise ValueError("wildcard version names must be unique")
        composition_mode = self.generation.composition_mode
        if composition_mode == "duo":
            subject_urls = {str(subject.canonical_source_url) for subject in self.subjects}
            if len(self.subjects) != 2 or len(subject_urls) != 2:
                raise ValueError("two-character composition requires exactly two distinct subjects")
            if self.generation.duo_contract_version == 2:
                require_controlled_duo_capabilities(
                    frozenset(self.workflow.capabilities),
                    isolation_mode=self.generation.duo_isolation_mode,
                    quality_mode=self.generation.duo_quality_mode,
                )
        elif composition_mode == "trio":
            subject_urls = {str(subject.canonical_source_url) for subject in self.subjects}
            if len(self.subjects) != 3 or len(subject_urls) != 3:
                raise ValueError(
                    "three-character composition requires exactly three distinct subjects"
                )
            require_controlled_trio_capabilities(
                frozenset(self.workflow.capabilities),
                isolation_mode=self.generation.duo_isolation_mode,
                quality_mode=self.generation.duo_quality_mode,
            )
        if any(
            batch.generation.composition_mode != composition_mode
            for batch in self.generation_batches
        ):
            raise ValueError("all generation batches must use the same composition mode")
        if any(
            batch.generation.duo_contract_version != self.generation.duo_contract_version
            for batch in self.generation_batches
        ):
            raise ValueError("all generation batches must use the same duo contract version")
        if self.generation.duo_contract_version == 2:
            capabilities = frozenset(self.workflow.capabilities)
            for batch in self.generation_batches:
                require_controlled_duo_capabilities(
                    capabilities,
                    isolation_mode=batch.generation.duo_isolation_mode,
                    quality_mode=batch.generation.duo_quality_mode,
                )
        elif self.generation.duo_contract_version == 3:
            capabilities = frozenset(self.workflow.capabilities)
            for batch in self.generation_batches:
                require_controlled_trio_capabilities(
                    capabilities,
                    isolation_mode=batch.generation.duo_isolation_mode,
                    quality_mode=batch.generation.duo_quality_mode,
                )
        if self.schema_version == 1:
            if self.generation_batches:
                raise ValueError(
                    "generation batches require release specification schema version 2"
                )
            return self
        if not self.generation_batches:
            raise ValueError("release specification schema version 2 requires generation batches")

        batch_names = [batch.name.casefold() for batch in self.generation_batches]
        if len(batch_names) != len(set(batch_names)):
            raise ValueError("generation batch names must be unique")
        if self.generation != self.generation_batches[0].generation:
            raise ValueError("top-level generation must match the first generation batch")
        planned_jobs = sum(batch.planned_job_count for batch in self.generation_batches)
        if planned_jobs != self.planned_job_count:
            raise ValueError("planned job count must match the generation batch plan")
        return self

    @property
    def ordered_generation_batches(self) -> tuple[GenerationBatchSpecification, ...]:
        if self.generation_batches:
            return tuple(self.generation_batches)
        return (
            GenerationBatchSpecification(
                name="Default batch",
                image_count=self.planned_job_count * self.generation.outputs_per_job,
                generation=self.generation,
            ),
        )

    @property
    def experiment_only(self) -> bool:
        return self.checkpoint.experiment_only or any(lora.experiment_only for lora in self.loras)


class ProjectCreate(StrictModel):
    slug: Slug
    name: str = Field(min_length=1, max_length=200)


class ReleaseCreate(StrictModel):
    slug: Slug
    title: str = Field(min_length=1, max_length=300)
    desired_accepted_count: int = Field(ge=1, le=MAX_ACCEPTED_IMAGES_PER_RELEASE)
    specification: ReleaseSpecification

    @model_validator(mode="before")
    @classmethod
    def mark_current_worker_request_budget(cls, value: object) -> object:
        """Mark API-created releases without changing legacy specification parsing."""

        if not isinstance(value, Mapping):
            return value
        raw_specification = value.get("specification")
        if not isinstance(raw_specification, Mapping):
            return value
        declared = raw_specification.get("worker_request_budget_version")
        if declared not in {None, 2}:
            raise ValueError("new releases require the current worker request budget")
        specification = dict(raw_specification)
        specification["worker_request_budget_version"] = 2
        updated = dict(value)
        updated["specification"] = specification
        return updated

    @model_validator(mode="after")
    def require_safe_new_provider_job_fanout(self) -> "ReleaseCreate":
        if self.specification.worker_request_budget_version != 2:
            raise ValueError("new releases require the current worker request budget")
        if any(
            batch.generation.outputs_per_job > MAX_SAFE_OUTPUTS_PER_SIGNED_GENERATION_JOB
            for batch in self.specification.ordered_generation_batches
        ):
            raise ValueError(
                "new signed provider jobs support at most "
                f"{MAX_SAFE_OUTPUTS_PER_SIGNED_GENERATION_JOB} outputs; "
                "larger batches must be split automatically"
            )
        for batch in self.specification.ordered_generation_batches:
            generation = batch.generation
            prompt_values = (
                generation.prompt,
                generation.character_a_prompt,
                generation.character_b_prompt,
                generation.character_a_pose_prompt,
                generation.character_b_pose_prompt,
                generation.character_c_prompt,
                generation.character_c_pose_prompt,
                generation.character_a_negative_prompt,
                generation.character_b_negative_prompt,
                generation.character_c_negative_prompt,
                generation.interaction_prompt,
                generation.camera_prompt,
                generation.negative_prompt,
                generation.detailer_prompt,
                generation.detailer_negative_prompt,
            )
            referenced = generation.outputs_per_job > MAX_INLINE_OUTPUTS_PER_SIGNED_GENERATION_JOB
            prompt_budget = (
                referenced_worker_prompt_budget_bytes(
                    prompt_values,
                    outputs_per_job=generation.outputs_per_job,
                )
                if referenced
                else signed_worker_prompt_budget_bytes(
                    prompt_values,
                    outputs_per_job=generation.outputs_per_job,
                )
            )
            prompt_budget_limit = (
                MAX_PROMPT_TEXT_BYTES_PER_GENERATION_JOB
                if referenced
                else MAX_SIGNED_PROMPT_BUDGET_BYTES_PER_GENERATION_JOB
            )
            if prompt_budget > prompt_budget_limit:
                raise ValueError("prompt text is too large for one signed provider job")

        capabilities = frozenset(self.specification.workflow.capabilities)
        generation = self.specification.generation
        if generation.composition_mode == "single":
            if len(self.specification.subjects) != 1:
                raise ValueError("single-character composition requires exactly one subject")
            unsupported = capabilities.intersection(
                {
                    WorkflowCapability.REGIONAL_PROMPTING_V1,
                    WorkflowCapability.CONTROLLED_DUO_V2,
                    WorkflowCapability.CONTROLLED_TRIO_V1,
                }
            )
            if unsupported:
                raise ValueError("single-character composition requires a non-regional workflow")
        elif generation.duo_contract_version == 1:
            if WorkflowCapability.REGIONAL_PROMPTING_V1 not in capabilities:
                raise ValueError("legacy two-character composition requires regional prompting")
        return self
