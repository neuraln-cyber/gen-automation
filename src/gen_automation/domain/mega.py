from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from gen_automation.domain.enums import MegaDeliveryState

Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class MegaDeliveryRead(BaseModel):
    """Credential-free delivery status returned to authenticated operators."""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    publication_package_id: UUID
    state: MegaDeliveryState
    remote_path: str = Field(min_length=1, max_length=1024)
    sha256: Sha256
    byte_size: int = Field(gt=0)
    attempts: int = Field(ge=0)
    available_at: datetime
    remote_node_handle: str | None = Field(default=None, max_length=80)
    verified_at: datetime | None = None
    completed_at: datetime | None = None
    last_error_code: str | None = Field(default=None, max_length=100)
    created_at: datetime
    updated_at: datetime


class MegaSetDeliveryItemRead(BaseModel):
    """Credential-free progress for one ordered image in an extracted set."""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    delivery_id: UUID
    ordinal: int = Field(gt=0)
    source_asset_id: UUID
    readiness_derivative_output_id: UUID
    source_sha256: Sha256
    source_byte_size: int = Field(gt=0)
    source_content_type: str = Field(min_length=1, max_length=100)
    remote_path: str = Field(min_length=1, max_length=1024)
    state: MegaDeliveryState
    attempts: int = Field(ge=0)
    available_at: datetime
    remote_node_handle: str | None = Field(default=None, max_length=80)
    uploaded_at: datetime | None = None
    verified_at: datetime | None = None
    completed_at: datetime | None = None
    last_error_code: str | None = Field(default=None, max_length=100)
    created_at: datetime
    updated_at: datetime


class MegaSetDeliveryRead(BaseModel):
    """Safe status for an extracted finished-set folder delivered to MEGA."""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    finished_set_archive_id: UUID
    state: MegaDeliveryState
    remote_root: str = Field(min_length=1, max_length=1024)
    remote_folder: str = Field(min_length=1, max_length=1024)
    manifest_sha256: Sha256
    total_item_count: int = Field(gt=0)
    uploaded_item_count: int = Field(ge=0)
    total_byte_size: int | None = Field(default=None, gt=0)
    uploaded_byte_size: int = Field(ge=0)
    attempts: int = Field(ge=0)
    available_at: datetime
    completion_marker_node_handle: str | None = Field(default=None, max_length=80)
    planned_at: datetime | None = None
    started_at: datetime | None = None
    verified_at: datetime | None = None
    completed_at: datetime | None = None
    last_error_code: str | None = Field(default=None, max_length=100)
    created_at: datetime
    updated_at: datetime
    items: tuple[MegaSetDeliveryItemRead, ...] = ()
