"""Stable contracts for the fresh image-to-video pipeline.

The contracts in this module deliberately describe generation mechanics only.  They do
not encode content, character, or licensing policy; callers own those concerns.
"""

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class I2VInputSource(StrEnum):
    UPLOAD = "upload"
    GENERATION = "generation"


class I2VJobState(StrEnum):
    QUEUED = "queued"
    CLAIMED = "claimed"
    RUNNING = "running"
    CANCEL_REQUESTED = "cancel_requested"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class I2VAttemptState(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class I2VWorkerDeploymentState(StrEnum):
    STOPPED = "stopped"
    PROVISIONING = "provisioning"
    STARTING = "starting"
    READY = "ready"
    BUSY = "busy"
    DRAINING = "draining"
    FAILED = "failed"


class I2VInputSnapshot(_FrozenModel):
    input_id: UUID
    created_by_user_id: UUID
    source: I2VInputSource
    asset_id: UUID | None
    display_name: str
    storage_backend: str
    storage_bucket: str
    object_key: str
    object_version_id: str | None
    sha256: str
    content_type: str
    width: int
    height: int
    byte_size: int
    metadata: dict[str, Any]
    created_at: datetime


class I2VPresetSnapshot(_FrozenModel):
    preset_id: UUID
    created_by_user_id: UUID
    name: str
    description: str
    positive_prompt: str
    negative_prompt: str
    settings: dict[str, Any]
    lock_version: int
    created_at: datetime
    updated_at: datetime


class I2VJobSnapshot(_FrozenModel):
    job_id: UUID
    created_by_user_id: UUID
    input_id: UUID
    preset_id: UUID | None
    positive_prompt: str
    negative_prompt: str
    input_snapshot: dict[str, Any]
    preset_snapshot: dict[str, Any]
    settings_snapshot: dict[str, Any]
    request_sha256: str
    state: I2VJobState
    queue_position: int | None
    attempt_count: int
    lease_owner: str | None
    lease_expires_at: datetime | None
    cancel_requested_at: datetime | None
    completed_at: datetime | None
    last_error_code: str | None
    last_error_detail: str | None
    created_at: datetime
    updated_at: datetime


class I2VAttemptSnapshot(_FrozenModel):
    attempt_id: UUID
    job_id: UUID
    worker_deployment_id: UUID | None
    attempt_no: int
    state: I2VAttemptState
    worker_id: str | None
    worker_image_digest: str | None
    provider_job_id: str | None
    request_metadata: dict[str, Any]
    response_metadata: dict[str, Any]
    started_at: datetime | None
    completed_at: datetime | None
    error_code: str | None
    error_detail: str | None
    created_at: datetime
    updated_at: datetime


class I2VOutputSnapshot(_FrozenModel):
    output_id: UUID
    job_id: UUID
    attempt_id: UUID
    storage_backend: str
    storage_bucket: str
    object_key: str
    object_version_id: str | None
    sha256: str
    content_type: str
    width: int
    height: int
    frame_count: int
    fps: float
    duration_ms: int
    byte_size: int
    metadata: dict[str, Any]
    created_at: datetime


class I2VWorkerDeploymentSnapshot(_FrozenModel):
    deployment_id: UUID
    provider: str
    provider_group_id: str | None
    provider_instance_id: str | None
    state: I2VWorkerDeploymentState
    gpu_class: str
    worker_image_digest: str
    current_job_id: UUID | None
    started_at: datetime | None
    ready_at: datetime | None
    stopped_at: datetime | None
    last_heartbeat_at: datetime | None
    metadata: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class I2VInputRegistration(_FrozenModel):
    source: I2VInputSource
    asset_id: UUID | None = None
    display_name: str
    storage_backend: str
    storage_bucket: str
    object_key: str
    object_version_id: str | None = None
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_type: str = Field(pattern=r"^image/")
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    byte_size: int = Field(gt=0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class I2VPresetDraft(_FrozenModel):
    name: str
    description: str = ""
    positive_prompt: str = ""
    negative_prompt: str = ""
    settings: dict[str, Any] = Field(default_factory=dict)


class I2VJobDraft(_FrozenModel):
    input_id: UUID
    preset_id: UUID | None = None
    positive_prompt: str | None = None
    negative_prompt: str | None = None
    settings: dict[str, Any] | None = None


class I2VOutputRegistration(_FrozenModel):
    storage_backend: str
    storage_bucket: str
    object_key: str
    object_version_id: str | None = None
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_type: str = Field(pattern=r"^video/")
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    frame_count: int = Field(gt=0)
    fps: float = Field(gt=0)
    duration_ms: int = Field(gt=0)
    byte_size: int = Field(gt=0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class I2VClaim(_FrozenModel):
    job: I2VJobSnapshot
    attempt: I2VAttemptSnapshot


class I2VWorkerDeploymentRegistration(_FrozenModel):
    provider: str
    provider_group_id: str | None = None
    provider_instance_id: str | None = None
    state: I2VWorkerDeploymentState
    gpu_class: str
    worker_image_digest: str
    current_job_id: UUID | None = None
    started_at: datetime | None = None
    ready_at: datetime | None = None
    stopped_at: datetime | None = None
    last_heartbeat_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
