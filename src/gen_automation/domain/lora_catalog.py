"""Strict commands for durable, content-addressed LoRA onboarding.

This module deliberately contains no storage or provider clients.  It defines
only the bounded data which may cross into the persistence/service layer.
Credentials, presigned URLs, and provider download redirects must never be
placed in these commands because their durable JSON fields are operator-visible.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal
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

from gen_automation.domain.canonical import canonical_json_bytes

MAX_MANAGED_LORA_BYTES = 4 * 1024 * 1024 * 1024
MAX_LORA_METADATA_BYTES = 16 * 1024
MAX_LORA_TRIGGER_WORDS = 100
CIVITAI_COMMERCIAL_USE_OVERRIDE_METADATA_KEY = "civitai_commercial_use_override"
CIVITAI_COMMERCIAL_USE_OVERRIDE_SCHEMA = "civitai-commercial-use-override/v1"

Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
VisibleName = Annotated[str, StringConstraints(min_length=1, max_length=200)]
S3Bucket = Annotated[
    str,
    StringConstraints(min_length=3, max_length=63, pattern=r"^[a-z0-9][a-z0-9.-]+[a-z0-9]$"),
]
S3ObjectKey = Annotated[str, StringConstraints(min_length=1, max_length=1024)]
S3VersionId = Annotated[str, StringConstraints(min_length=1, max_length=1024)]
S3ETag = Annotated[
    str,
    StringConstraints(min_length=32, max_length=80, pattern=r"^[0-9a-f]{32}(?:-[1-9][0-9]*)?$"),
]

_SAFE_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]*\.safetensors$", re.IGNORECASE)
_FORBIDDEN_METADATA_KEY_PARTS = (
    "api_key",
    "apikey",
    "authorization",
    "bearer",
    "credential",
    "password",
    "presigned",
    "secret",
    "signature",
    "token",
)


class StrictLoraCatalogModel(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class _ImportCreateBase(StrictLoraCatalogModel):
    display_name: VisibleName
    canonical_source_url: AnyHttpUrl
    license_url: AnyHttpUrl
    commercial_use_attested: Literal[True]
    adult_use_attested: Literal[True]
    target_filename: str = Field(min_length=13, max_length=236)
    expected_sha256: Sha256 | None = None
    expected_byte_size: int | None = Field(
        default=None,
        ge=10,
        le=MAX_MANAGED_LORA_BYTES,
    )
    expected_metadata: dict[str, Any] = Field(default_factory=dict)
    trigger_words: list[str] = Field(
        default_factory=list,
        max_length=MAX_LORA_TRIGGER_WORDS,
    )

    @field_validator("display_name")
    @classmethod
    def validate_display_name(cls, value: str) -> str:
        return _visible_text(value, label="LoRA display name")

    @field_validator("canonical_source_url", "license_url")
    @classmethod
    def validate_source_url(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        return _safe_https_url(value)

    @field_validator("target_filename")
    @classmethod
    def validate_target_filename(cls, value: str) -> str:
        if (
            value != value.strip()
            or PurePosixPath(value).name != value
            or not _SAFE_FILENAME.fullmatch(value)
            or any(ord(character) < 32 for character in value)
        ):
            raise ValueError("LoRA target filename must be a safe .safetensors basename")
        return value

    @field_validator("expected_metadata")
    @classmethod
    def validate_expected_metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        validate_lora_durable_metadata(value)
        return value

    @field_validator("trigger_words")
    @classmethod
    def validate_trigger_words(cls, values: list[str]) -> list[str]:
        normalized = [
            _visible_text(value, label="LoRA trigger word", maximum=200) for value in values
        ]
        if len({value.casefold() for value in normalized}) != len(normalized):
            raise ValueError("LoRA trigger words must be unique")
        return normalized


class ManualLoraImportCreate(_ImportCreateBase):
    """Create a job before a browser uploads to the server-chosen staging key."""

    @field_validator("canonical_source_url", "license_url")
    @classmethod
    def validate_manual_source_url(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        return _canonical_https_url(value)


class CivitaiLoraImportCreate(_ImportCreateBase):
    """Create an import request for one canonical Civitai model or version URL."""

    civitai_model_id: int | None = Field(default=None, ge=1)
    civitai_version_id: int | None = Field(default=None, ge=1)
    civitai_file_id: int | None = Field(default=None, ge=1)
    commercial_use_override_attested: bool = False

    @model_validator(mode="before")
    @classmethod
    def derive_canonical_provider_urls(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        model_id = value.get("civitai_model_id")
        version_id = value.get("civitai_version_id")
        if (
            isinstance(model_id, int)
            and not isinstance(model_id, bool)
            and model_id > 0
            and isinstance(version_id, int)
            and not isinstance(version_id, bool)
            and version_id > 0
        ):
            canonical = f"https://civitai.com/models/{model_id}?modelVersionId={version_id}"
            value = {
                **value,
                "canonical_source_url": canonical,
                "license_url": canonical,
            }
        return value

    @field_validator("canonical_source_url")
    @classmethod
    def validate_civitai_url(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        value = _safe_https_url(value)
        if (value.host or "").casefold() not in {"civitai.com", "www.civitai.com"}:
            raise ValueError("Civitai imports require a canonical civitai.com URL")
        if (
            value.query is None
            or re.fullmatch(
                r"modelVersionId=[1-9][0-9]*",
                value.query,
            )
            is None
        ):
            raise ValueError("Civitai imports require one exact model version URL")
        return value

    @model_validator(mode="after")
    def validate_provider_ids(self) -> CivitaiLoraImportCreate:
        if CIVITAI_COMMERCIAL_USE_OVERRIDE_METADATA_KEY in self.expected_metadata:
            raise ValueError("Civitai commercial-use override metadata is server-managed")
        if (
            self.civitai_model_id is None
            or self.civitai_version_id is None
            or self.civitai_file_id is None
            or self.expected_sha256 is None
            or self.expected_byte_size is None
        ):
            raise ValueError(
                "Civitai imports require the resolved model, version, file, SHA-256, and size"
            )
        return self


class ManualUploadCompletion(StrictLoraCatalogModel):
    """The exact immutable S3 version produced by one direct manual upload."""

    object_version_id: S3VersionId
    object_etag: S3ETag
    byte_size: int = Field(ge=10, le=MAX_MANAGED_LORA_BYTES)

    @field_validator("object_etag", mode="before")
    @classmethod
    def normalize_etag(cls, value: object) -> object:
        return _normalize_etag(value)

    @field_validator("object_version_id")
    @classmethod
    def validate_version_id(cls, value: str) -> str:
        return _visible_text(value, label="S3 object version", maximum=1024)


class VerifiedLoraArtifact(StrictLoraCatalogModel):
    """Verified immutable artifact facts supplied after bounded byte inspection."""

    artifact_sha256: Sha256
    storage_bucket: S3Bucket
    object_key: S3ObjectKey
    object_version_id: S3VersionId
    object_etag: S3ETag
    byte_size: int = Field(ge=10, le=MAX_MANAGED_LORA_BYTES)
    approval_id: UUID
    provenance: dict[str, Any] = Field(default_factory=dict)

    @field_validator("object_etag", mode="before")
    @classmethod
    def normalize_final_etag(cls, value: object) -> object:
        return _normalize_etag(value)

    @field_validator("object_key")
    @classmethod
    def validate_final_object_key(cls, value: str) -> str:
        _validate_object_key(value)
        if not value.casefold().endswith(".safetensors"):
            raise ValueError("managed LoRA object key must end in .safetensors")
        return value

    @field_validator("object_version_id")
    @classmethod
    def validate_final_version_id(cls, value: str) -> str:
        return _visible_text(value, label="S3 object version", maximum=1024)

    @field_validator("provenance")
    @classmethod
    def validate_provenance(cls, value: dict[str, Any]) -> dict[str, Any]:
        validate_lora_durable_metadata(value)
        return value

    @model_validator(mode="after")
    def validate_content_addressed_key(self) -> VerifiedLoraArtifact:
        expected_key = f"worker/managed-loras/sha256/{self.artifact_sha256}.safetensors"
        if self.object_key != expected_key:
            raise ValueError("managed LoRA key must be content-addressed by its SHA-256")
        return self


class LoraDependencySummary(StrictLoraCatalogModel):
    """A runtime-owned snapshot of references which must drain before purge."""

    queued_generation_jobs: int = Field(default=0, ge=0)
    active_generation_attempts: int = Field(default=0, ge=0)
    warm_experiment_leases: int = Field(default=0, ge=0)

    @property
    def has_dependencies(self) -> bool:
        return any(
            (
                self.queued_generation_jobs,
                self.active_generation_attempts,
                self.warm_experiment_leases,
            )
        )


def _safe_https_url(value: AnyHttpUrl) -> AnyHttpUrl:
    if (
        value.scheme != "https"
        or value.username is not None
        or value.password is not None
        or value.fragment is not None
    ):
        raise ValueError("LoRA source URLs must be canonical credential-free HTTPS URLs")
    return value


def _canonical_https_url(value: AnyHttpUrl) -> AnyHttpUrl:
    value = _safe_https_url(value)
    if value.query is not None:
        raise ValueError("LoRA source URLs must not contain query parameters")
    return value


def _visible_text(value: str, *, label: str, maximum: int = 200) -> str:
    if (
        value != value.strip()
        or not value
        or len(value) > maximum
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError(f"{label} must be trimmed visible text")
    return value


def _validate_object_key(value: str) -> None:
    path = PurePosixPath(value)
    if (
        value != value.strip()
        or value.startswith("/")
        or value.endswith("/")
        or "\\" in value
        or str(path) != value
        or any(part in {"", ".", ".."} for part in path.parts)
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError("S3 object key is invalid")


def validate_lora_durable_metadata(value: dict[str, Any]) -> None:
    def inspect(item: Any) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str) or not key or len(key) > 200:
                    raise ValueError("LoRA metadata keys must be short non-empty strings")
                folded = key.casefold().replace("-", "_")
                if any(part in folded for part in _FORBIDDEN_METADATA_KEY_PARTS):
                    raise ValueError("LoRA metadata must not contain credentials or signed URLs")
                inspect(child)
        elif isinstance(item, list):
            for child in item:
                inspect(child)
        elif isinstance(item, str):
            folded = item.casefold()
            if any(
                marker in folded
                for marker in (
                    "x-amz-credential=",
                    "x-amz-security-token=",
                    "x-amz-signature=",
                )
            ):
                raise ValueError("LoRA metadata must not contain credentials or signed URLs")

    inspect(value)
    try:
        encoded = canonical_json_bytes(value)
    except (TypeError, ValueError) as error:
        raise ValueError("LoRA metadata must be canonical JSON") from error
    if len(encoded) > MAX_LORA_METADATA_BYTES:
        raise ValueError("LoRA metadata exceeds the durable size limit")


def _normalize_etag(value: object) -> object:
    if isinstance(value, str) and len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1].casefold()
    return value.casefold() if isinstance(value, str) else value
