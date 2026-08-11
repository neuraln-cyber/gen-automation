from __future__ import annotations

from enum import StrEnum
from typing import Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from gen_automation.domain.canonical import canonical_sha256
from gen_automation.video_worker.profiles import (
    A14B_ADULT_VIDEO_PROFILE,
    A14B_MAX_OUTPUT_EDGE,
    A14B_MAX_OUTPUT_PIXELS,
    A14B_VIDEO_PROFILE,
    derive_a14b_output_dimensions,
    derive_a14b_render_dimensions,
    get_video_profile_registration,
)

VIDEO_REQUEST_SCHEMA: Final[Literal["video-generation-request/v1"]] = "video-generation-request/v1"
VIDEO_COMPLIANCE_SCHEMA: Final[Literal["video-compliance/v1"]] = "video-compliance/v1"


class VideoContentRating(StrEnum):
    SFW = "sfw"
    NSFW = "nsfw"
    EXPLICIT = "explicit"


class VideoGenerationState(StrEnum):
    QUEUED = "queued"
    CLAIMED = "claimed"
    SUBMITTING = "submitting"
    RUNNING = "running"
    COLLECTING = "collecting"
    VERIFYING = "verifying"
    SUCCEEDED = "succeeded"
    UNKNOWN = "unknown"
    RETRY_WAIT = "retry_wait"
    FAILED = "failed"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"


class VideoGenerationAttemptState(StrEnum):
    CREATED = "created"
    SUBMITTING = "submitting"
    SUBMITTED = "submitted"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class VideoComplianceAttestations(_StrictFrozenModel):
    policy_version: Literal["video-compliance/v1"] = VIDEO_COMPLIANCE_SCHEMA
    source_rights_confirmed: bool
    lawful_use_confirmed: bool
    all_depicted_people_are_adults: bool = False
    consensual_adult_content_confirmed: bool = False
    no_real_person_sexual_content: bool = False


class VideoSourceSnapshot(_StrictFrozenModel):
    asset_id: UUID
    storage_backend: str = Field(min_length=1, max_length=50)
    storage_bucket: str = Field(min_length=1, max_length=255)
    object_key: str = Field(min_length=1, max_length=1_024)
    object_version_id: str | None = Field(default=None, max_length=1_024)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_type: str = Field(pattern=r"^image/[a-z0-9.+-]+$", max_length=100)
    image_format: str = Field(min_length=1, max_length=20)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    byte_size: int = Field(gt=0)


class VideoGenerationParameters(_StrictFrozenModel):
    prompt: str = Field(default="", max_length=4_000)
    negative_prompt: str = Field(default="", max_length=4_000)
    profile_key: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[a-z0-9][a-z0-9._-]{0,99}$",
    )
    profile_version: str = Field(min_length=1, max_length=100)
    profile_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    seed: int = Field(ge=0, le=9_223_372_036_854_775_807)
    frame_count: Literal[73, 81, 121]
    fps: Literal[16, 24] = 24
    width: int = Field(ge=2, le=A14B_MAX_OUTPUT_EDGE, multiple_of=2)
    height: int = Field(ge=2, le=A14B_MAX_OUTPUT_EDGE, multiple_of=2)
    loop_mode: Literal["forward", "ping_pong"] = "ping_pong"

    @model_validator(mode="after")
    def validate_native_dimensions(self) -> VideoGenerationParameters:
        registration = get_video_profile_registration(self.profile_key)
        if registration is None:
            raise ValueError("video profile is not supported")
        profile = registration.profile
        is_a14b = profile in {A14B_VIDEO_PROFILE, A14B_ADULT_VIDEO_PROFILE}
        dimensions_valid = (
            self.width * self.height <= A14B_MAX_OUTPUT_PIXELS
            if is_a14b
            else (self.width, self.height)
            in {
                (profile.landscape_width, profile.landscape_height),
                (profile.portrait_width, profile.portrait_height),
            }
        )
        if (
            self.profile_version != profile.adapter_revision
            or self.profile_sha256 != registration.job_contract_sha256
            or self.frame_count not in profile.permitted_native_frame_counts
            or self.fps != profile.fps
            or not dimensions_valid
            or self.loop_mode != profile.loop_mode
        ):
            raise ValueError("video parameters do not match the selected profile")
        return self


class VideoGenerationRequest(_StrictFrozenModel):
    schema_version: Literal["video-generation-request/v1"] = VIDEO_REQUEST_SCHEMA
    source: VideoSourceSnapshot
    parameters: VideoGenerationParameters
    content_rating: VideoContentRating = VideoContentRating.SFW
    compliance: VideoComplianceAttestations

    @model_validator(mode="after")
    def validate_attestations(self) -> VideoGenerationRequest:
        if not self.compliance.source_rights_confirmed:
            raise ValueError("source-rights attestation is required")
        if not self.compliance.lawful_use_confirmed:
            raise ValueError("lawful-use attestation is required")
        if self.content_rating in {VideoContentRating.NSFW, VideoContentRating.EXPLICIT}:
            if not self.compliance.all_depicted_people_are_adults:
                raise ValueError("adult content requires an all-adults attestation")
            if not self.compliance.consensual_adult_content_confirmed:
                raise ValueError("adult content requires a consent attestation")
            if not self.compliance.no_real_person_sexual_content:
                raise ValueError("adult content requires a no-real-person attestation")
        if self.parameters.profile_key == A14B_VIDEO_PROFILE.profile_id:
            if self.content_rating != VideoContentRating.SFW:
                raise ValueError("SFW A14B profile requires SFW content")
            try:
                derive_a14b_render_dimensions(self.source.width, self.source.height)
                output_dimensions = derive_a14b_output_dimensions(
                    self.source.width,
                    self.source.height,
                )
            except ValueError:
                raise ValueError(
                    "source dimensions are not supported by the A14B profile"
                ) from None
            if (self.parameters.width, self.parameters.height) != output_dimensions:
                raise ValueError("A14B output dimensions must preserve the source image")
        elif self.parameters.profile_key == A14B_ADULT_VIDEO_PROFILE.profile_id:
            if self.content_rating == VideoContentRating.SFW:
                raise ValueError("adult A14B profile requires adult content")
            try:
                derive_a14b_render_dimensions(self.source.width, self.source.height)
                output_dimensions = derive_a14b_output_dimensions(
                    self.source.width,
                    self.source.height,
                )
            except ValueError:
                raise ValueError(
                    "source dimensions are not supported by the A14B profile"
                ) from None
            if (self.parameters.width, self.parameters.height) != output_dimensions:
                raise ValueError("A14B output dimensions must preserve the source image")
        return self

    @property
    def request_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json"))


class VideoCostSummary(_StrictFrozenModel):
    estimated_cost_microusd: int = Field(default=0, ge=0)
    reserved_cost_microusd: int = Field(default=0, ge=0)
    actual_cost_microusd: int = Field(default=0, ge=0)
    billed_duration_ms: int = Field(default=0, ge=0)
