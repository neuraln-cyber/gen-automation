from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

from gen_automation.i2v_worker.h3_upscale import (
    H3_UPSCALER_BYTES,
    H3_UPSCALER_FILENAME,
    H3_UPSCALER_ROLE,
    H3_UPSCALER_SHA256,
)
from gen_automation.i2v_worker.lora_catalog import (
    LORA_ARTIFACTS_BY_ROLE,
    LORA_CATALOG,
    MAX_REVIEWED_LORA_SELECTIONS,
    MAX_REVIEWED_LORA_STRENGTH,
    MIN_REVIEWED_LORA_STRENGTH,
    LoraCatalogId,
)

SHA256_PATTERN = r"^[0-9a-f]{64}$"
I2V_JOB_SCHEMA: Final = "i2v-job/v2"
I2V_RESULT_SCHEMA: Final = "i2v-result/v2"
_SAFE_OBJECT_KEY = re.compile(r"^[^\x00-\x1f\\]{1,1024}$")
_MAX_LOOP_DURATION_SECONDS = 25


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class ModelObject(_StrictModel):
    role: Literal[
        "diffusion_model",
        "video_vae",
        "audio_vae",
        "h3_latent_upscaler",
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
            "diffusion_model": "models/diffusion_models/",
            "video_vae": "models/vae/",
            "audio_vae": "models/vae/",
            "h3_latent_upscaler": "models/latent_upscale_models/",
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
            or "\\" in self.install_path
            or any(ord(char) < 32 for char in self.install_path)
            or not self.install_path.endswith(".safetensors")
        ):
            raise ValueError("model object path is invalid")
        reviewed = LORA_ARTIFACTS_BY_ROLE.get(self.role)
        if self.role == H3_UPSCALER_ROLE and (
            self.install_path != f"models/latent_upscale_models/{H3_UPSCALER_FILENAME}"
            or self.byte_size != H3_UPSCALER_BYTES
            or self.sha256 != H3_UPSCALER_SHA256
        ):
            raise ValueError("H3 latent upscaler artifact identity is invalid")
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


class H3LoraSelection(_StrictModel):
    """Immutable library identity, never a user-supplied filename or URL."""

    artifact_id: UUID
    sha256: str = Field(pattern=SHA256_PATTERN)
    strength: float = Field(ge=-100, le=100, allow_inf_nan=False, strict=True)


class H3LoraGrant(_StrictModel):
    artifact_id: UUID
    sha256: str = Field(pattern=SHA256_PATTERN)
    byte_size: int = Field(gt=0)
    download: DownloadGrant

    @model_validator(mode="after")
    def validate_expiry(self) -> H3LoraGrant:
        if self.download.expires_at.tzinfo is None:
            raise ValueError("H3 LoRA grant expiry must include a timezone")
        return self


class GenerationSettings(_StrictModel):
    profile: Literal["wan22", "minimax_h3"] = "wan22"
    frame_count: int = Field(default=81, ge=9)
    fps: int = Field(default=16, gt=0)
    width: int = Field(default=576, ge=32)
    height: int = Field(default=1024, ge=32)
    match_source_aspect: bool = False
    match_source_resolution: bool = False
    h3_save_base_video: bool = False
    seed: int = -1
    steps: int = Field(default=4, ge=2)
    high_end_step: int = Field(default=2, ge=1)
    cfg: float = Field(default=1.0, gt=0)
    high_shift: float = 5.0
    low_shift: float = 5.0
    sampler: Literal["euler"] = "euler"
    scheduler: Literal["linear_quadratic", "simple"] = "linear_quadratic"
    video_shift: float = Field(default=8, ge=6, le=12)
    audio_shift: float = Field(default=4, ge=3, le=5)
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
    h3_loras: list[H3LoraSelection] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_wan_shape(self) -> GenerationSettings:
        if self.profile == "minimax_h3":
            if self.h3_save_base_video and not self.match_source_resolution:
                raise ValueError("base-video diagnostics require H3 source-size delivery")
            if len({item.artifact_id for item in self.h3_loras}) != len(self.h3_loras):
                raise ValueError("H3 LoRA selections must be unique")
            if (
                self.frame_count % 17 != 5
                or not 124 <= self.frame_count <= 362
                or self.fps != 24
                or self.steps not in {4, 8}
                or self.scheduler != "simple"
                or self.cfg != 1
                or self.width % 32
                or self.height % 32
                or (not self.match_source_resolution and self.width * self.height > 768 * 1344)
                or max(self.width, self.height) > 2048
            ):
                raise ValueError(
                    "MiniMax H3 requires 17n+5 frames (124-362), 24 fps, "
                    "4 or 8 steps, Euler/simple, CFG 1 and 32-aligned dimensions up to 2048; "
                    "standard mode uses a canvas up to 1.03 MP"
                )
            if self.loras or self.face_fidelity != "off" or self.loop or self.upscale != "none":
                raise ValueError(
                    "MiniMax H3 does not use WAN LoRAs, face locking, loops or upscaling"
                )
            return self
        if self.h3_save_base_video:
            raise ValueError("base-video diagnostics require MiniMax H3")
        if self.match_source_resolution:
            raise ValueError("original-resolution generation requires MiniMax H3")
        if self.h3_loras:
            raise ValueError("H3 LoRAs require the MiniMax H3 profile")
        if self.scheduler != "linear_quadratic":
            raise ValueError("WAN requires the linear_quadratic scheduler")
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


def source_resolution_canvas(width: int, height: int) -> tuple[int, int]:
    """Pad, never resample, an H3 source onto the model's 32-pixel grid."""
    if not (32 <= width <= 2048 and 32 <= height <= 2048):
        raise ValueError(
            "Original-resolution H3 images must be between 32 and 2048 pixels per side"
        )
    if width % 2 or height % 2:
        raise ValueError("Exact H.264 output requires an image with even width and height")
    return ((width + 31) // 32 * 32, (height + 31) // 32 * 32)


class I2VJob(_StrictModel):
    schema_version: Literal["i2v-job/v2"] = Field(
        default=I2V_JOB_SCHEMA,
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
    base_video_grant: UploadGrant | None = None
    h3_lora_grants: list[H3LoraGrant] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_h3_grants(self) -> I2VJob:
        if self.settings_snapshot.h3_save_base_video != (self.base_video_grant is not None):
            raise ValueError("base-video grant must match the diagnostic selection")
        if self.base_video_grant is not None and (
            self.base_video_grant.storage_bucket != self.output_grant.storage_bucket
            or not self.output_grant.object_key.endswith(".mp4")
            or self.base_video_grant.object_key != self.output_grant.object_key[:-4] + ".base.mp4"
        ):
            raise ValueError("base-video grant is outside this attempt")
        expected = [(item.artifact_id, item.sha256) for item in self.settings_snapshot.h3_loras]
        actual = [(item.artifact_id, item.sha256) for item in self.h3_lora_grants]
        if actual != expected:
            raise ValueError("H3 LoRA grants must exactly match the frozen selections")
        return self


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
        default=I2V_RESULT_SCHEMA,
        alias="schema",
        serialization_alias="schema",
    )
    job_id: UUID
    attempt_id: UUID
    request_sha256: str = Field(pattern=SHA256_PATTERN)
    output: OutputResult
