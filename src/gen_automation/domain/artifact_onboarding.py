from pathlib import Path, PurePosixPath
from typing import Annotated, Literal

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from gen_automation.domain.compliance_registry import ApprovalEvidence
from gen_automation.domain.controlled_duo import (
    WorkflowCapability,
    require_coherent_workflow_capabilities,
)
from gen_automation.gpu_worker.artifacts import (
    MAX_ARTIFACT_BYTES,
    MAX_ARTIFACTS,
    ArtifactKind,
    Sha256,
)

SafeName = Annotated[
    str,
    StringConstraints(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$"),
]
LocalPath = Annotated[str, StringConstraints(min_length=1, max_length=1_024)]


class StrictOnboardingModel(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class ModelApprovalPlan(StrictOnboardingModel):
    """The explicit rights and format assertions required by the existing registry."""

    name: str = Field(min_length=1, max_length=200)
    source_url: AnyHttpUrl
    license_url: AnyHttpUrl
    commercial_use_approved: Literal[True]
    adult_use_approved: Literal[True]
    safetensors_verified: Literal[True]
    evidence: ApprovalEvidence

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if value != value.strip() or any(ord(character) < 32 for character in value):
            raise ValueError("approval name must be trimmed visible text")
        return value

    @field_validator("source_url", "license_url")
    @classmethod
    def validate_external_url(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        if (
            value.scheme != "https"
            or value.username is not None
            or value.password is not None
            or value.query is not None
            or value.fragment is not None
        ):
            raise ValueError(
                "model source and license URLs must be canonical credential-free HTTPS"
            )
        return value


class ArtifactOnboardingEntry(StrictOnboardingModel):
    logical_name: SafeName
    kind: ArtifactKind
    object_key: str = Field(min_length=1, max_length=512)
    target_filename: str | None = Field(default=None, min_length=1, max_length=236)
    local_path: LocalPath | None = None
    sha256: Sha256 | None = None
    exact_size_bytes: int | None = Field(default=None, ge=10, le=MAX_ARTIFACT_BYTES)
    max_size_bytes: int | None = Field(default=None, ge=10, le=MAX_ARTIFACT_BYTES)
    approval: ModelApprovalPlan | None = None

    @field_validator("object_key")
    @classmethod
    def validate_object_key(cls, value: str) -> str:
        _validate_object_key(value)
        return value

    @field_validator("local_path")
    @classmethod
    def validate_local_path(cls, value: str | None) -> str | None:
        if value is not None and (
            value != value.strip() or any(ord(character) < 32 for character in value)
        ):
            raise ValueError("local artifact path must be trimmed text")
        return value

    @model_validator(mode="after")
    def validate_source_and_approval(self) -> "ArtifactOnboardingEntry":
        suffix = ".pt" if self.kind == ArtifactKind.DETECTOR else ".safetensors"
        if not self.object_key.casefold().endswith(suffix):
            raise ValueError(f"{self.kind.value} object key must end in {suffix}")
        if self.target_filename is not None:
            target = Path(self.target_filename)
            if (
                target.name != self.target_filename
                or target.is_absolute()
                or not self.target_filename.casefold().endswith(suffix)
            ):
                raise ValueError(f"{self.kind.value} target filename is invalid")
        if self.local_path is None and (self.sha256 is None or self.exact_size_bytes is None):
            raise ValueError(
                "an artifact without a local path requires sha256 and exact_size_bytes"
            )
        if (self.sha256 is None) != (self.exact_size_bytes is None):
            raise ValueError("sha256 and exact_size_bytes must be supplied together")
        if (
            self.max_size_bytes is not None
            and self.exact_size_bytes is not None
            and self.max_size_bytes < self.exact_size_bytes
        ):
            raise ValueError("artifact maximum size cannot be lower than its exact size")
        if self.kind == ArtifactKind.DETECTOR:
            if self.approval is not None:
                raise ValueError(
                    "detectors are protected by the worker manifest, not model approval"
                )
        elif self.approval is None:
            raise ValueError("checkpoint and LoRA entries require an approval block")
        return self


class WorkflowOnboardingEntry(StrictOnboardingModel):
    name: str = Field(min_length=1, max_length=200)
    version: str = Field(min_length=1, max_length=100)
    object_key: str = Field(min_length=1, max_length=1_024)
    local_path: LocalPath
    capabilities: list[WorkflowCapability] = Field(default_factory=list, max_length=16)
    evidence: ApprovalEvidence

    @field_validator("capabilities")
    @classmethod
    def validate_capabilities(
        cls,
        values: list[WorkflowCapability],
    ) -> list[WorkflowCapability]:
        if len(values) != len(set(values)):
            raise ValueError("workflow capabilities must be unique")
        require_coherent_workflow_capabilities(values)
        return sorted(values, key=str)

    @field_validator("name", "version")
    @classmethod
    def validate_text(cls, value: str) -> str:
        if value != value.strip() or any(ord(character) < 32 for character in value):
            raise ValueError("workflow name and version must be trimmed visible text")
        return value

    @field_validator("object_key")
    @classmethod
    def validate_object_key(cls, value: str) -> str:
        _validate_object_key(value)
        if not value.casefold().endswith(".json"):
            raise ValueError("workflow object key must end in .json")
        return value

    @field_validator("local_path")
    @classmethod
    def validate_local_path(cls, value: str) -> str:
        if value != value.strip() or any(ord(character) < 32 for character in value):
            raise ValueError("local workflow path must be trimmed text")
        return value


class ArtifactOnboardingPlan(StrictOnboardingModel):
    version: Literal["v1"]
    owner_username: str | None = Field(default=None, min_length=1, max_length=200)
    artifacts: list[ArtifactOnboardingEntry] = Field(
        min_length=1,
        max_length=MAX_ARTIFACTS,
    )
    workflows: list[WorkflowOnboardingEntry] = Field(default_factory=list, max_length=16)

    @field_validator("owner_username")
    @classmethod
    def validate_owner_username(cls, value: str | None) -> str | None:
        if value is not None and (
            value != value.strip() or any(ord(character) < 32 for character in value)
        ):
            raise ValueError("owner username must be trimmed visible text")
        return value

    @model_validator(mode="after")
    def require_unique_entries(self) -> "ArtifactOnboardingPlan":
        logical_names = [entry.logical_name.casefold() for entry in self.artifacts]
        object_keys = [entry.object_key for entry in self.artifacts]
        targets = [
            (
                entry.kind,
                (entry.target_filename or PurePosixPath(entry.object_key).name).casefold(),
            )
            for entry in self.artifacts
        ]
        workflow_keys = [entry.object_key for entry in self.workflows]
        workflow_versions = [
            (entry.name.casefold(), entry.version.casefold()) for entry in self.workflows
        ]
        for values, message in (
            (logical_names, "artifact logical names must be unique"),
            (object_keys, "artifact object keys must be unique"),
            (targets, "artifact target filenames must be unique per kind"),
            (workflow_keys, "workflow object keys must be unique"),
            (workflow_versions, "workflow name/version pairs must be unique"),
        ):
            if len(values) != len(set(values)):
                raise ValueError(message)
        if not any(entry.kind == ArtifactKind.CHECKPOINT for entry in self.artifacts):
            raise ValueError("artifact inventory requires at least one checkpoint")
        if sum(entry.kind == ArtifactKind.DETECTOR for entry in self.artifacts) > 1:
            raise ValueError("artifact inventory supports at most one detector")
        return self


def _validate_object_key(value: str) -> None:
    if (
        value != value.strip()
        or value.startswith("/")
        or value.endswith("/")
        or "\\" in value
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError("object key is invalid")
    path = PurePosixPath(value)
    if str(path) != value or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("object key is invalid")
