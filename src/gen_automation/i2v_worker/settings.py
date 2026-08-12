from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from gen_automation.i2v_worker.models import ModelObject


class I2VWorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GEN_I2V_WORKER_",
        case_sensitive=False,
        extra="ignore",
        env_ignore_empty=True,
    )

    model_objects_json: SecretStr
    environment: Literal["production", "test"] = "production"
    aws_region: str = "eu-central-1"
    s3_endpoint_url: str | None = None
    comfy_root: Path = Path("/opt/comfyui")
    runtime_root: Path = Path("/opt/i2v/runtime")
    workflow_template: Path = Path("/opt/i2v/workflows/dasiwa-wan22-i2v-v1.api.json")
    comfy_python: Path = Path("/opt/i2v-venv/bin/python")
    comfy_main: Path = Path("/opt/comfyui/main.py")
    comfy_base_url: str = "http://127.0.0.1:8188"
    host: str = "0.0.0.0"  # noqa: S104 - container ingress is required for Salad.
    port: int = Field(default=8000, gt=0, le=65535)
    log_level: str = "info"
    max_body_bytes: int = Field(default=1024 * 1024, gt=0)
    network_timeout_seconds: float = Field(default=120, gt=0)
    network_attempts: int = Field(default=5, gt=0)
    artifact_chunk_bytes: int = Field(default=64 * 1024 * 1024, ge=1024 * 1024)
    comfy_poll_seconds: float = Field(default=1, gt=0)
    queue_worker_enabled: bool = True
    queue_worker_path: Path = Path("/usr/local/bin/salad-http-job-queue-worker")

    @model_validator(mode="after")
    def validate_configuration(self) -> I2VWorkerSettings:
        if self.comfy_base_url != "http://127.0.0.1:8188":
            raise ValueError("ComfyUI must be loopback only")
        if not self.runtime_root.is_absolute() or not self.comfy_root.is_absolute():
            raise ValueError("runtime paths must be absolute")
        objects = self.model_objects
        roles = {item.role for item in objects}
        required = {"diffusion_model_high", "diffusion_model_low", "text_encoder", "vae"}
        if not required.issubset(roles) or len(roles) != len(objects):
            raise ValueError("model manifest roles are incomplete or duplicated")
        return self

    @property
    def model_objects(self) -> tuple[ModelObject, ...]:
        try:
            raw = json.loads(self.model_objects_json.get_secret_value())
            if not isinstance(raw, list):
                raise ValueError
            return tuple(ModelObject.model_validate(item) for item in raw)
        except (TypeError, ValueError):
            raise ValueError("model object manifest is invalid") from None
