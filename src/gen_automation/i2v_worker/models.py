from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

from gen_automation.i2v_worker.lora_catalog import (
    LORA_ARTIFACTS_BY_ROLE,
    LORA_CATALOG,
    MAX_REVIEWED_LORA_SELECTIONS,
    MAX_REVIEWED_LORA_STRENGTH,
    MIN_REVIEWED_LORA_STRENGTH,
    LoraCatalogId,
)

SHA256_PATTERN = r"^[0-9a-f]{64}$"
_SAFE_OBJECT_KEY = re.compile(r"^[^\x00-\x1f\\]{1,1024}$")
_MAX_LOOP_DURATION_SECONDS = 25


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class ModelObject(_StrictModel):
    role: Literal[
        "diffusion_model_high",
        "diffusion_model_low",
        "text_encoder",
        "vae",
        "lora_wan_general_nsfw_high",
        "lora_wan_general_nsfw_low",
        "lora_bouncing_boobs_high",
        "lora_bouncing_boobs_low",
        "lora_m4crom4sti4_high",
        "lora_m4crom4sti4_low",
        "lora_dr34ml4y_high",
        "lora_dr34ml4y_low",
        "lora_smoothmix_animations_high",
        "lora_smoothmix_animations_low",
    ]
    bucket: str = Field(min_length=2, max_length=255)
    key: str = Field(min_length=1, max_length=1024)
    version_id: str = Field(min_length=1, max_length=1024)
    byte_size: int = Field(gt=0)
    sha256: str = Field(pattern=SHA256_PATTERN)
    install_path: str = Field(min_length=1, max_length=1024)

    @model_validator(mode="after")
    def validate_paths(self) -> ModelObject:
        expected_directory = {
            "diffusion_model_high": "models/diffusion_models/",
            "diffusion_model_low": "models/diffusion_models/",
            "text_encoder": "models/text_encoders/",
            "vae": "models/vae/",
            "lora_wan_general_nsfw_high": "models/loras/",
            "lora_wan_general_nsfw_low": "models/loras/",
            "lora_bouncing_boobs_high": "models/loras/",
            "lora_bouncing_boobs_low": "models/loras/",
            "lora_m4crom4sti4_high": "models/loras/",
            "lora_m4crom4sti4_low": "models/loras/",
            "lora_dr34ml4y_high": "models/loras/",
            "lora_dr34ml4y_low": "models/loras/",
            "lora_smoothmix_animations_high": "models/loras/",
            "lora_smoothmix_animations_low": "models/loras/",
        }[self.role]
        if (
            _SAFE_OBJECT_KEY.fullmatch(self.key) is None
            or self.key != f"worker/i2v/sha256/{self.sha256}"
            or ".." in self.key.split("/")
            or not self.install_path.startswith(expected_directory)
            or self.install_path.startswith("/")
            or ".." in self.install_path.split("/")
            or "//" in self.install_path
            or not self.install_path.endswith(".safetensors")
        ):
            raise ValueError("model object path is invalid")
        reviewed = LORA_ARTIFACTS_BY_ROLE.get(self.role)
        if reviewed is not None and (
            self.install_path != reviewed.install_path
            or self.byte_size != reviewed.byte_size
            or self.sha256 != reviewed.sha256
        ):
            raise ValueError("reviewed LoRA artifact identity is invalid")
        return self


class InputSnapshot(_StrictModel):
    storage_backend: Literal["s3"]
    storage_bucket: str
    object_key: str
    object_version_id: str | None = None
    sha256: str = Field(pattern=SHA256_PATTERN)
    content_type: Literal["image/png", "image/jpeg", "image/webp"]
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    byte_size: int = Field(gt=0)


class DownloadGrant(_StrictModel):
    method: Literal["GET"]
    url: HttpUrl
    expires_at: datetime


class UploadGrant(_StrictModel):
    method: Literal["PUT"]
    url: HttpUrl
    headers: dict[str, str]
    storage_backend: Literal["s3"]
    storage_bucket: str
    object_key: str
    expires_at: datetime

    @model_validator(mode="after")
    def validate_headers(self) -> UploadGrant:
        if (
            self.headers.get("Content-Type") != "video/mp4"
            or self.headers.get("Cache-Control") != "private, no-store, max-age=0"
            or self.headers.get("x-amz-server-side-encryption") != "AES256"
            or any("authorization" in name.casefold() for name in self.headers)
        ):
            raise ValueError("output grant headers are invalid")
        return self


class LoraSelection(_StrictModel):
    catalog_id: LoraCatalogId
    strength: float = Field(
        ge=MIN_REVIEWED_LORA_STRENGTH,
        le=MAX_REVIEWED_LORA_STRENGTH,
    )


class GenerationSettings(_StrictModel):
    frame_count: int = Field(default=81, ge=9)
    fps: int = Field(default=16, gt=0)
    width: int = Field(default=576, ge=32)
    height: int = Field(default=1024, ge=32)
    match_source_aspect: bool = False
    seed: int = -1
    steps: int = Field(default=4, ge=2)
    high_end_step: int = Field(default=2, ge=1)
    cfg: float = Field(default=1.0, gt=0)
    high_shift: float = 5.0
    low_shift: float = 5.0
    sampler: Literal["euler"] = "euler"
    scheduler: Literal["linear_quadratic"] = "linear_quadratic"
    interpolation: Literal["none"] = "none"
    upscale: Literal["none", "source"] = "none"
    loop: bool = False
    # The field ceiling is a secondary resource guard; the cross-field
    # validator below enforces the actual 25-second delivery contract.
    loop_count: int = Field(default=2, ge=1, le=20)
    color_transfer: Literal[False] = False
    tiled_vae: Literal[False] = False
    face_fidelity: Literal["off", "stable_expression"] = "off"
    # Operator attestation only. The application deliberately does not infer or
    # classify content; the owner chooses the applicable RunPod authorization.
    runpod_authorization: Literal["sfw", "written_permission"] = "sfw"
    loras: list[LoraSelection] = Field(
        default_factory=list,
        max_length=MAX_REVIEWED_LORA_SELECTIONS,
    )

    @model_validator(mode="after")
    def validate_wan_shape(self) -> GenerationSettings:
        if self.frame_count % 8 != 1:
            raise ValueError("frame count must be 8n+1")
        if self.width % 32 or self.height % 32:
            raise ValueError("dimensions must be divisible by 32")
        if self.high_end_step >= self.steps:
            raise ValueError("high stage must end before total steps")
        if self.loop:
            if self.face_fidelity == "stable_expression":
                raise ValueError("stable expression does not support looped delivery")
            output_frames = ((2 * self.frame_count) - 2) * self.loop_count
            if output_frames > self.fps * _MAX_LOOP_DURATION_SECONDS:
                raise ValueError("looped output duration must not exceed 25 seconds")
        if len({selection.catalog_id for selection in self.loras}) != len(self.loras):
            raise ValueError("reviewed LoRA selections must be unique")
        catalog_order = {catalog_id: index for index, catalog_id in enumerate(LORA_CATALOG)}
        self.loras.sort(key=lambda selection: catalog_order[selection.catalog_id])
        return self


class I2VJob(_StrictModel):
    schema_version: Literal["i2v-job/v2"] = Field(
        default="i2v-job/v2",
        alias="schema",
        serialization_alias="schema",
    )
    job_id: UUID
    attempt_id: UUID
    request_sha256: str = Field(pattern=SHA256_PATTERN)
    input_snapshot: InputSnapshot
    positive_prompt: str
    negative_prompt: str = ""
    settings_snapshot: GenerationSettings
    input_grant: DownloadGrant
    output_grant: UploadGrant


class OutputResult(_StrictModel):
    storage_backend: Literal["s3"]
    storage_bucket: str
    object_key: str
    object_version_id: str | None
    sha256: str = Field(pattern=SHA256_PATTERN)
    content_type: Literal["video/mp4"] = "video/mp4"
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    frame_count: int = Field(gt=0)
    fps: float = Field(gt=0)
    duration_ms: int = Field(gt=0)
    byte_size: int = Field(gt=0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class I2VResult(_StrictModel):
    schema_version: Literal["i2v-result/v2"] = Field(
        default="i2v-result/v2",
        alias="schema",
        serialization_alias="schema",
    )
    job_id: UUID
    attempt_id: UUID
    request_sha256: str = Field(pattern=SHA256_PATTERN)
    output: OutputResult
