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
