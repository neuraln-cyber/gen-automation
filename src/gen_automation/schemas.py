from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from gen_automation.domain.enums import ReleasePhase, ResourceHealth
from gen_automation.domain.release_spec import ProjectCreate, ReleaseCreate

__all__ = [
    "GenerationPlanRead",
    "ProjectCreate",
    "ProjectRead",
    "ReleaseCreate",
    "ReleaseRead",
]


class ProjectRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    slug: str
    name: str
    created_at: datetime


class ReleaseRead(BaseModel):
    id: UUID
    project_id: UUID
    slug: str
    title: str
    phase: ReleasePhase
    health: ResourceHealth
    current_version_no: int
    desired_accepted_count: int
    specification_sha256: str
    created_at: datetime
    updated_at: datetime


class GenerationPlanRead(BaseModel):
    release_id: UUID
    release_version_id: UUID
    release_phase: ReleasePhase
    specification_sha256: str
    jobs_created: int
    total_jobs: int
