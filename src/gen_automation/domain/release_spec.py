from typing import Annotated
from uuid import UUID

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
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
    prompt: str = Field(min_length=1, max_length=20000)
    negative_prompt: str = Field(default="", max_length=20000)
    seed: int = Field(ge=0, le=(2**63) - 1)
    width: int = Field(ge=512, le=4096, multiple_of=64)
    height: int = Field(ge=512, le=4096, multiple_of=64)
    steps: int = Field(ge=1, le=200)
    cfg: float = Field(default=5.0, ge=0.0, le=30.0)
    sampler: str = Field(min_length=1, max_length=100)
    scheduler: str = Field(min_length=1, max_length=100)
    outputs_per_job: int = Field(default=4, ge=1, le=8)


class WildcardVersionReference(StrictModel):
    """An immutable wildcard-library version frozen into a release."""

    name: WildcardName
    library_id: UUID
    version_id: UUID
    version_no: int = Field(ge=1)
    entries_sha256: Sha256
    entry_count: int = Field(ge=1, le=2000)


class ReleaseSpecification(StrictModel):
    schema_version: int = Field(default=1, ge=1, le=1)
    subjects: list[SubjectSpecification] = Field(min_length=1, max_length=20)
    checkpoint: ArtifactSpecification
    loras: list[LoraSpecification] = Field(default_factory=list, max_length=4)
    workflow: WorkflowSpecification
    generation: GenerationParameters
    planned_job_count: int = Field(ge=1, le=10000)
    wildcard_versions: list[WildcardVersionReference] = Field(
        default_factory=list,
        max_length=64,
    )

    @model_validator(mode="after")
    def require_unique_wildcard_names(self) -> "ReleaseSpecification":
        names = [reference.name for reference in self.wildcard_versions]
        if len(names) != len(set(names)):
            raise ValueError("wildcard version names must be unique")
        return self


class ProjectCreate(StrictModel):
    slug: Slug
    name: str = Field(min_length=1, max_length=200)


class ReleaseCreate(StrictModel):
    slug: Slug
    title: str = Field(min_length=1, max_length=300)
    desired_accepted_count: int = Field(ge=1, le=10000)
    specification: ReleaseSpecification
