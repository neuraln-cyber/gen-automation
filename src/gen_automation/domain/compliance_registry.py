import re
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

from gen_automation.domain.controlled_duo import (
    WorkflowCapability,
    require_coherent_workflow_capabilities,
)
from gen_automation.domain.enums import (
    ApprovalStatus,
    GenerationModelFamily,
    ModelArtifactKind,
)

Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Slug = Annotated[
    str,
    StringConstraints(
        pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
        min_length=1,
        max_length=80,
    ),
]
_NODE_CLASS_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,200}$")


class StrictComplianceModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ApprovalEvidence(StrictComplianceModel):
    """Bounded evidence references; source documents themselves stay outside the API."""

    summary: str = Field(min_length=1, max_length=4_000)
    source_urls: list[AnyHttpUrl] = Field(default_factory=list, max_length=20)
    document_sha256s: list[Sha256] = Field(default_factory=list, max_length=20)
    internal_reference: str | None = Field(default=None, min_length=1, max_length=200)

    @field_validator("summary")
    @classmethod
    def validate_summary(cls, value: str) -> str:
        return _clean_text(value, "evidence summary")

    @field_validator("internal_reference")
    @classmethod
    def validate_internal_reference(cls, value: str | None) -> str | None:
        return _clean_text(value, "internal reference") if value is not None else None

    @field_validator("source_urls")
    @classmethod
    def validate_source_urls(cls, values: list[AnyHttpUrl]) -> list[AnyHttpUrl]:
        for value in values:
            _safe_evidence_url(value)
        return values

    @model_validator(mode="after")
    def require_and_deduplicate_evidence(self) -> "ApprovalEvidence":
        urls = [str(url) for url in self.source_urls]
        if not urls and not self.document_sha256s:
            raise ValueError("evidence requires a source URL or a document SHA-256")
        if len(urls) != len(set(urls)):
            raise ValueError("evidence source URLs must be unique")
        if len(self.document_sha256s) != len(set(self.document_sha256s)):
            raise ValueError("evidence document hashes must be unique")
        return self


class SubjectApprovalCreate(StrictComplianceModel):
    slug: Slug
    display_name: str = Field(min_length=1, max_length=200)
    canonical_source_url: AnyHttpUrl
    canonical_age: int = Field(ge=18, le=10_000)
    clearly_adult: Literal[True]
    is_fictional: Literal[True]
    is_aged_up_minor: Literal[False]
    distribution_rights_approved: Literal[True]
    adult_derivative_rights_approved: Literal[True]
    evidence: ApprovalEvidence

    @field_validator("display_name")
    @classmethod
    def validate_display_name(cls, value: str) -> str:
        return _clean_text(value, "display name")

    @field_validator("canonical_source_url")
    @classmethod
    def validate_canonical_source_url(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        return _safe_evidence_url(value)


class ModelArtifactApprovalCreate(StrictComplianceModel):
    artifact_sha256: Sha256
    name: str = Field(min_length=1, max_length=200)
    kind: ModelArtifactKind
    model_family: GenerationModelFamily = GenerationModelFamily.ILLUSTRIOUS
    source_url: AnyHttpUrl
    storage_key: str = Field(min_length=1, max_length=1_024)
    license_url: AnyHttpUrl
    commercial_use_approved: bool
    adult_use_approved: Literal[True]
    safetensors_verified: Literal[True]
    experiment_only: bool = False
    evidence: ApprovalEvidence

    @model_validator(mode="after")
    def require_approved_usage_scope(self) -> "ModelArtifactApprovalCreate":
        if not self.commercial_use_approved and not self.experiment_only:
            raise ValueError("non-commercial artifacts must be restricted to experiment-only use")
        return self

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return _clean_text(value, "artifact name")

    @field_validator("source_url", "license_url")
    @classmethod
    def validate_external_url(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        return _safe_evidence_url(value)

    @field_validator("storage_key")
    @classmethod
    def validate_storage_key(cls, value: str) -> str:
        return _safe_object_key(value, required_suffix=".safetensors")


class WorkflowApprovalCreate(StrictComplianceModel):
    workflow_sha256: Sha256
    name: str = Field(min_length=1, max_length=200)
    version: str = Field(min_length=1, max_length=100)
    model_family: GenerationModelFamily = GenerationModelFamily.ILLUSTRIOUS
    object_key: str = Field(min_length=1, max_length=1_024)
    reviewed_node_classes: list[str] = Field(min_length=1, max_length=128)
    capabilities: list[WorkflowCapability] = Field(default_factory=list, max_length=16)
    evidence: ApprovalEvidence

    @field_validator("name", "version")
    @classmethod
    def validate_text_fields(cls, value: str) -> str:
        return _clean_text(value, "workflow text")

    @field_validator("object_key")
    @classmethod
    def validate_object_key(cls, value: str) -> str:
        return _safe_object_key(value, required_suffix=".json")

    @field_validator("reviewed_node_classes")
    @classmethod
    def validate_node_classes(cls, values: list[str]) -> list[str]:
        if any(_NODE_CLASS_PATTERN.fullmatch(value) is None for value in values):
            raise ValueError("reviewed node classes contain an invalid identifier")
        if len(values) != len(set(values)):
            raise ValueError("reviewed node classes must be unique")
        return sorted(values)

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

    @model_validator(mode="after")
    def validate_capability_topology(self) -> "WorkflowApprovalCreate":
        require_coherent_workflow_capabilities(
            self.capabilities,
            reviewed_node_classes=self.reviewed_node_classes,
        )
        return self


class ApprovalRevoke(StrictComplianceModel):
    expected_approval_version: int = Field(ge=1)
    reason_code: str = Field(pattern=r"^[a-z][a-z0-9_]{0,99}$")
    note: str | None = Field(default=None, min_length=1, max_length=2_000)

    @field_validator("note")
    @classmethod
    def validate_note(cls, value: str | None) -> str | None:
        return _clean_text(value, "revocation note") if value is not None else None


class ApprovalRead(StrictComplianceModel):
    registry: Literal["subject", "model_artifact", "workflow"]
    approval_id: str
    identity_sha256: Sha256
    name: str
    approval_version: int
    status: ApprovalStatus
    is_current: bool
    evidence_sha256: Sha256
    approved_by_user_id: str
    approved_at: str
    revoked_by_user_id: str | None
    revoked_at: str | None
    replayed: bool


def _safe_object_key(value: str, *, required_suffix: str) -> str:
    if (
        value != value.strip()
        or value.startswith("/")
        or value.endswith("/")
        or "\\" in value
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError("object key is invalid")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("object key is invalid")
    if not value.casefold().endswith(required_suffix):
        raise ValueError(f"object key must end in {required_suffix}")
    return value


def _safe_evidence_url(value: AnyHttpUrl) -> AnyHttpUrl:
    path = value.path or ""
    query = value.query
    exact_civitai_version = bool(
        (value.host or "").casefold() == "civitai.com"
        and re.fullmatch(r"/models/[1-9][0-9]*", path)
        and query is not None
        and re.fullmatch(r"modelVersionId=[1-9][0-9]*", query)
    )
    if (
        value.scheme != "https"
        or value.username is not None
        or value.password is not None
        or (query is not None and not exact_civitai_version)
        or value.fragment is not None
    ):
        raise ValueError("evidence URLs must be credential-free canonical HTTPS URLs")
    return value


def _clean_text(value: str, label: str) -> str:
    if value != value.strip() or not value.strip():
        raise ValueError(f"{label} must be nonempty and trimmed")
    if any(ord(character) < 32 and character not in {"\n", "\r", "\t"} for character in value):
        raise ValueError(f"{label} contains a prohibited control character")
    return value
