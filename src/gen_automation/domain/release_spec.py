from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from gen_automation.domain.deliverability import MAX_ACCEPTED_IMAGES_PER_RELEASE
from gen_automation.domain.generation_limits import (
    MAX_OUTPUTS_PER_GENERATION_JOB,
    MAX_PROMPT_TEXT_BYTES_PER_GENERATION_JOB,
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

    @model_validator(mode="after")
    def require_licence_approval(self) -> "ArtifactSpecification":
        if not self.commercial_use_approved or not self.adult_use_approved:
            raise ValueError("artifact requires commercial-use and adult-use approval")
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


class GenerationParameters(StrictModel):
    composition_mode: Literal["single", "duo"] = "single"
    prompt: str = Field(min_length=1, max_length=20000)
    character_a_prompt: str = Field(default="", max_length=20000)
    character_b_prompt: str = Field(default="", max_length=20000)
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
        elif self.character_a_prompt or self.character_b_prompt:
            raise ValueError("character prompts require two-character composition")
        prompt_bytes = sum(
            len(value.encode("utf-8"))
            for value in (
                self.prompt,
                self.character_a_prompt,
                self.character_b_prompt,
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
        if any(
            batch.generation.composition_mode != composition_mode
            for batch in self.generation_batches
        ):
            raise ValueError("all generation batches must use the same composition mode")
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


class ProjectCreate(StrictModel):
    slug: Slug
    name: str = Field(min_length=1, max_length=200)


class ReleaseCreate(StrictModel):
    slug: Slug
    title: str = Field(min_length=1, max_length=300)
    desired_accepted_count: int = Field(ge=1, le=MAX_ACCEPTED_IMAGES_PER_RELEASE)
    specification: ReleaseSpecification
