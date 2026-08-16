"""Strict RunPod envelope models around the provider-neutral I2V job."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

from gen_automation.i2v_worker.models import SHA256_PATTERN, I2VJob


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RunPodModelGrant(_StrictModel):
    role: str = Field(min_length=1, max_length=100)
    method: Literal["GET"] = "GET"
    url: HttpUrl
    expires_at: datetime
    byte_size: int = Field(gt=0)
    sha256: str = Field(pattern=SHA256_PATTERN)


class RunPodClaimGrant(_StrictModel):
    method: Literal["POST"] = "POST"
    url: HttpUrl
    bearer_token: str = Field(min_length=40, max_length=4096)
    expires_at: datetime


class RunPodI2VInput(_StrictModel):
    schema_version: Literal["i2v-runpod-input/v1"] = Field(
        default="i2v-runpod-input/v1",
        alias="schema",
        serialization_alias="schema",
    )
    submission_key: str = Field(pattern=SHA256_PATTERN)
    job: I2VJob
    claim: RunPodClaimGrant
    model_grants: tuple[RunPodModelGrant, ...]

    @model_validator(mode="after")
    def validate_model_grants(self) -> RunPodI2VInput:
        roles = [grant.role for grant in self.model_grants]
        if len(roles) != len(set(roles)):
            raise ValueError("RunPod model grants contain duplicate roles")
        return self


class RunPodI2VEvent(_StrictModel):
    id: str = Field(min_length=5, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    input: RunPodI2VInput
