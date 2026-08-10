"""Strict HTTP contracts for the managed LoRA dashboard and API."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from gen_automation.domain.enums import LoraImportJobState, LoraImportSource

Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class StrictLoraApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class CivitaiResolveRequest(StrictLoraApiModel):
    url: str = Field(min_length=1, max_length=2_048)
    version_id: int | None = Field(default=None, ge=1)
    commercial_use_override_attested: bool = False

    @field_validator("url")
    @classmethod
    def require_trimmed_url(cls, value: str) -> str:
        if value != value.strip() or any(ord(character) < 32 for character in value):
            raise ValueError("Civitai URL must be trimmed visible text")
        return value


class CivitaiVersionRead(StrictLoraApiModel):
    version_id: int
    name: str
    base_model: str | None
    target_filename: str
    declared_size_bytes: int
    sha256: Sha256


class CivitaiFileRead(StrictLoraApiModel):
    file_id: int
    name: str
    target_filename: str
    size_bytes: int
    sha256: Sha256
    primary: bool = True


class CivitaiResolveRead(StrictLoraApiModel):
    model_id: int
    version_id: int | None = None
    model_name: str | None = None
    version_name: str | None = None
    base_model: str | None = None
    canonical_source_url: str | None = None
    license_url: str | None = None
    files: list[CivitaiFileRead] = Field(default_factory=list)
    trained_words: list[str] = Field(default_factory=list)
    commercial_image_allowed: bool
    provider_commercial_use: list[str] = Field(default_factory=list)
    commercial_use_override_applied: bool = False
    adult_use_requires_attestation: bool = True
    versions: list[CivitaiVersionRead] = Field(default_factory=list)


class LoraImportRead(StrictLoraApiModel):
    id: UUID
    name: str
    source_kind: LoraImportSource
    status: LoraImportJobState
    bytes_transferred: int
    total_bytes: int | None
    error: str | None
    error_code: str | None
    retryable: bool
    cancellable: bool
    lock_version: int
    created_at: datetime
    updated_at: datetime


class LoraEntryRead(StrictLoraApiModel):
    id: UUID | str
    name: str
    status: str
    readiness_status: str
    size_bytes: int
    sha256: Sha256
    source_url: str
    source_label: str
    version_name: str | None
    trigger_words: list[str]
    updated_at: datetime | None
    can_retire: bool
    can_restore: bool
    lock_version: int | None
    purge_requested: bool = False
    storage_retained_reason: str | None = None
    lifecycle_error_code: str | None = None
    lifecycle_error: str | None = None
    lifecycle_retry_at: datetime | None = None


class LoraLibraryRead(StrictLoraApiModel):
    entries: list[LoraEntryRead]
    imports: list[LoraImportRead]


class ManualImportUploadRead(StrictLoraApiModel):
    url: str
    method: Literal["POST"]
    fields: dict[str, str]
    headers: dict[str, str] = Field(default_factory=dict)


class ManualImportCreateRead(StrictLoraApiModel):
    import_: LoraImportRead = Field(serialization_alias="import")
    upload: ManualImportUploadRead


class LoraActionRequest(StrictLoraApiModel):
    expected_lock_version: int | None = Field(default=None, ge=1)


class LoraRetireRequest(LoraActionRequest):
    purge_requested: bool = True


class LoraMutationRead(StrictLoraApiModel):
    entry: LoraEntryRead | None = None
    import_: LoraImportRead | None = Field(default=None, serialization_alias="import")
    changed: bool
    replayed: bool
