import re
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import SplitResult, urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from gen_automation.domain.signing import SigningMaterialError, validate_public_key
from gen_automation.video_worker.profiles import PINNED_VIDEO_PROFILE

MAX_HARD_BODY_BYTES = 512 * 1024
MAX_HARD_SOURCE_BYTES = 100 * 1024 * 1024
MAX_HARD_OUTPUT_BYTES = 1024 * 1024 * 1024
DEFAULT_MAX_SOURCE_BYTES = 50 * 1024 * 1024
DEFAULT_MAX_IMAGE_DIMENSION = 16_384
DEFAULT_MAX_IMAGE_PIXELS = 64_000_000
MAX_UPLOAD_FIELDS = 64
MAX_UPLOAD_FIELD_NAME_LENGTH = 256
MAX_UPLOAD_FIELD_VALUE_LENGTH = 16 * 1024
KEY_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")

BoundedId = Annotated[
    str,
    StringConstraints(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$"),
]


class WorkerEnvironment(StrEnum):
    TEST = "test"
    PRODUCTION = "production"


def parse_https_origin(value: str, *, origin_only: bool) -> tuple[str, int]:
    if not value or any(character.isspace() or ord(character) < 32 for character in value):
        raise ValueError("invalid HTTPS origin")
    if "\\" in value:
        raise ValueError("invalid HTTPS origin")
    try:
        parsed: SplitResult = urlsplit(value)
        port = parsed.port or 443
    except ValueError:
        raise ValueError("invalid HTTPS origin") from None
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError("invalid HTTPS origin")
    if origin_only and (parsed.path not in {"", "/"} or parsed.query):
        raise ValueError("configured origin must not include a path or query")
    if not 1 <= port <= 65535:
        raise ValueError("invalid HTTPS origin")
    return parsed.hostname.lower(), port


class WorkerSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    environment: WorkerEnvironment = WorkerEnvironment.PRODUCTION
    verification_keys: dict[str, str]
    allowed_source_origins: frozenset[str]
    allowed_upload_origin: str
    staging_root: Path
    max_body_bytes: int = Field(default=128 * 1024, ge=1024, le=MAX_HARD_BODY_BYTES)
    max_source_bytes: int = Field(
        default=DEFAULT_MAX_SOURCE_BYTES,
        ge=1024,
        le=MAX_HARD_SOURCE_BYTES,
    )
    max_output_bytes: int = Field(
        default=256 * 1024 * 1024,
        ge=1024,
        le=MAX_HARD_OUTPUT_BYTES,
    )
    max_image_dimension: int = Field(
        default=DEFAULT_MAX_IMAGE_DIMENSION,
        ge=64,
        le=65_536,
    )
    max_image_pixels: int = Field(
        default=DEFAULT_MAX_IMAGE_PIXELS,
        ge=4_096,
        le=256_000_000,
    )
    max_signature_ttl_seconds: int = Field(default=7200, ge=5, le=7200)
    clock_skew_seconds: int = Field(default=15, ge=0, le=60)
    max_replay_entries: int = Field(default=128, ge=1, le=1024)
    download_timeout_seconds: float = Field(default=60.0, ge=1.0, le=300.0)
    upload_timeout_seconds: float = Field(default=120.0, ge=1.0, le=600.0)
    readiness_timeout_seconds: float = Field(default=1.0, ge=0.05, le=5.0)
    ffprobe_timeout_seconds: float = Field(default=15.0, ge=1.0, le=60.0)
    ffmpeg_timeout_seconds: float = Field(default=300.0, ge=10.0, le=1800.0)

    @model_validator(mode="after")
    def validate_security_boundary(self) -> "WorkerSettings":
        if not self.verification_keys or len(self.verification_keys) > 8:
            raise ValueError("invalid verification keys")
        for key_id, public_key in self.verification_keys.items():
            if KEY_ID_PATTERN.fullmatch(key_id) is None:
                raise ValueError("invalid verification key identifier")
            try:
                validate_public_key(public_key)
            except SigningMaterialError:
                raise ValueError("invalid Ed25519 verification key") from None

        if not self.allowed_source_origins or len(self.allowed_source_origins) > 8:
            raise ValueError("invalid source origins")
        for origin in self.allowed_source_origins:
            parse_https_origin(origin, origin_only=True)
        parse_https_origin(self.allowed_upload_origin, origin_only=True)
        if not self.staging_root.is_absolute():
            raise ValueError("staging root must be absolute")
        return self

    @property
    def source_origins(self) -> frozenset[tuple[str, int]]:
        return frozenset(
            parse_https_origin(origin, origin_only=True) for origin in self.allowed_source_origins
        )

    @property
    def upload_origin(self) -> tuple[str, int]:
        return parse_https_origin(self.allowed_upload_origin, origin_only=True)


class SourceDownloadGrant(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    asset_id: BoundedId
    url: Annotated[str, StringConstraints(min_length=9, max_length=4096)] = Field(repr=False)
    content_type: Literal["image/png", "image/jpeg", "image/webp"]
    size_bytes: int = Field(ge=1, le=MAX_HARD_SOURCE_BYTES)
    sha256: Annotated[str, StringConstraints(min_length=64, max_length=64)]

    @field_validator("sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        if SHA256_PATTERN.fullmatch(value) is None:
            raise ValueError("invalid SHA-256")
        return value


class VideoUploadGrant(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    asset_id: BoundedId
    upload_attempt_id: BoundedId
    content_type: Literal["video/mp4"]
    url: Annotated[str, StringConstraints(min_length=9, max_length=4096)] = Field(repr=False)
    fields: dict[str, str] = Field(repr=False)

    @field_validator("fields")
    @classmethod
    def validate_fields(cls, value: dict[str, str]) -> dict[str, str]:
        if not 1 <= len(value) <= MAX_UPLOAD_FIELDS:
            raise ValueError("invalid upload form fields")
        for name, field_value in value.items():
            if (
                not name
                or len(name) > MAX_UPLOAD_FIELD_NAME_LENGTH
                or len(field_value) > MAX_UPLOAD_FIELD_VALUE_LENGTH
                or any(ord(character) < 32 for character in name)
                or name.lower() == "file"
            ):
                raise ValueError("invalid upload form field")
        return value


class AnimatePayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    job_id: BoundedId
    attempt_id: BoundedId
    profile_id: Literal["wan2.2-ti2v-5b-comfy-v1"]
    source: SourceDownloadGrant
    upload: VideoUploadGrant
    prompt: Annotated[str, StringConstraints(max_length=4000)] = Field(default="", repr=False)
    negative_prompt: Annotated[str, StringConstraints(max_length=4000)] = Field(
        default="",
        repr=False,
    )
    seed: int = Field(ge=0, le=9_223_372_036_854_775_807)
    native_frame_count: Literal[73, 121] = 73
    fps: Literal[24]
    width: Literal[480, 832]
    height: Literal[480, 832]
    loop_mode: Literal["ping_pong"]

    @model_validator(mode="after")
    def validate_pinned_profile(self) -> "AnimatePayload":
        if self.profile_id != PINNED_VIDEO_PROFILE.profile_id:
            raise ValueError("invalid profile")
        if (self.width, self.height) not in {
            (
                PINNED_VIDEO_PROFILE.landscape_width,
                PINNED_VIDEO_PROFILE.landscape_height,
            ),
            (
                PINNED_VIDEO_PROFILE.portrait_width,
                PINNED_VIDEO_PROFILE.portrait_height,
            ),
        }:
            raise ValueError("invalid video dimensions")
        if self.source.asset_id == self.upload.asset_id:
            raise ValueError("source and output assets must be distinct")
        return self


class AnimateEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    version: Literal["video-worker.v1"]
    key_id: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    issued_at: int = Field(ge=0)
    expires_at: int = Field(ge=0)
    payload: AnimatePayload
    signature: Annotated[
        str,
        StringConstraints(min_length=86, max_length=86, pattern=r"^[A-Za-z0-9_-]{86}$"),
    ] = Field(repr=False)


class AnimateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal["video-worker.v1"] = "video-worker.v1"
    job_id: str
    attempt_id: str
    status: Literal["succeeded"] = "succeeded"
    profile_id: str
    source_asset_id: str
    output_asset_id: str
    upload_attempt_id: str
    output_sha256: str
    output_size_bytes: int
    loop_mode: Literal["ping_pong"] = "ping_pong"
    fps: int
    width: int
    height: int
    native_frame_count: int
    native_duration_seconds: float
    output_frame_count: int
    output_duration_seconds: float


def validate_grant_url(
    url: str,
    allowed_origins: frozenset[tuple[str, int]],
) -> None:
    if parse_https_origin(url, origin_only=False) not in allowed_origins:
        raise ValueError("grant URL origin is not allowed")
