from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from gen_automation.domain.private_delivery import PrivateDeliveryRoute
from gen_automation.i2v_worker.h3_upscale import H3_UPSCALER_ROLE
from gen_automation.i2v_worker.lora_catalog import REQUIRED_LORA_ROLES
from gen_automation.i2v_worker.manifest_contract import required_i2v_model_roles
from gen_automation.i2v_worker.models import ModelObject

I2V_CUSTOM_NODES = ("ComfyUI-NAG", "GenAutomationH3", "ComfyUI-H3-Latent-Upscaler")
H3_COMFY_MEMORY_ARGS = (
    "--reserve-vram",
    "8",
    "--disable-cuda-malloc",
    "--cache-none",
    "--vram-headroom",
    "4",
)


class I2VWorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GEN_I2V_WORKER_",
        case_sensitive=False,
        extra="ignore",
        env_ignore_empty=True,
    )

    model_objects_json: SecretStr
    profile: Literal["wan22", "minimax_h3"] = "wan22"
    environment: Literal["production", "test"] = "production"
    provider: Literal["runpod", "salad"] = "runpod"
    queue_worker_enabled: bool = False
    queue_worker_path: Path = Path("/usr/local/bin/salad-http-job-queue-worker")
    aws_region: str = "eu-central-1"
    s3_endpoint_url: str | None = None
    model_delivery_domain: str | None = None
    require_private_delivery: bool = False
    comfy_root: Path = Path("/opt/comfyui")
    runtime_root: Path = Path("/opt/i2v/runtime")
    volume_root: Path = Path("/runpod-volume")
    workflow_template: Path = Path("/opt/i2v/workflows/dasiwa-wan22-i2v-v1.api.json")
    comfy_python: Path = Path("/opt/i2v-venv/bin/python")
    comfy_main: Path = Path("/opt/comfyui/main.py")
    comfy_base_url: str = "http://127.0.0.1:8188"
    host: str = "127.0.0.1"
    port: int = Field(default=8000, gt=0, le=65535)
    log_level: str = "info"
    max_body_bytes: int = Field(default=1024 * 1024, gt=0)
    network_timeout_seconds: float = Field(default=120, gt=0)
    network_attempts: int = Field(default=5, gt=0)
    artifact_chunk_bytes: int = Field(default=64 * 1024 * 1024, ge=1024 * 1024)
    artifact_download_concurrency: int = Field(default=4, ge=1, le=4)
    startup_timeout_seconds: int | None = Field(default=None, ge=60)
    execution_timeout_seconds: int | None = Field(default=None, ge=60)
    comfy_poll_seconds: float = Field(default=1, gt=0)
    models_prepared: bool = False
    require_preseeded_volume: bool = False
    minimum_gpu_vram_bytes: int = Field(default=31 * 1024 * 1024 * 1024, gt=0)
    allowed_gpu_names_csv: str = "NVIDIA A40,NVIDIA RTX A6000,NVIDIA L40S,NVIDIA GeForce RTX 5090"
    lora_worker_enabled: bool = False
    source_revision: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    private_manifest_source_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )

    @model_validator(mode="after")
    def validate_configuration(self) -> I2VWorkerSettings:
        if self.provider == "salad" and not self.queue_worker_enabled:
            raise ValueError("Salad requires the queue consumer")
        if not _is_container_absolute(self.queue_worker_path):
            raise ValueError("queue worker path must be absolute")
        if self.comfy_base_url != "http://127.0.0.1:8188":
            raise ValueError("ComfyUI must be loopback only")
        if (
            not _is_container_absolute(self.runtime_root)
            or not _is_container_absolute(self.comfy_root)
            or not _is_container_absolute(self.volume_root)
        ):
            raise ValueError("runtime paths must be absolute")
        if not self.allowed_gpu_names:
            raise ValueError("at least one RunPod GPU name is required")
        objects = self.model_objects
        if self.require_private_delivery and self.model_delivery_domain is None:
            raise ValueError("private model delivery is required")
        if self.model_delivery_domain is not None:
            if self.s3_endpoint_url is not None or len({item.bucket for item in objects}) != 1:
                raise ValueError("private model delivery requires one regional S3 origin")
            PrivateDeliveryRoute(
                self.model_delivery_domain, objects[0].bucket, self.aws_region, "models"
            )
        roles = {item.role for item in objects}
        required = set(
            required_i2v_model_roles(
                reviewed_loras_enabled=self.lora_worker_enabled, profile=self.profile
            )
        )
        if self.lora_worker_enabled:
            if self.source_revision is None or self.private_manifest_source_sha256 is None:
                raise ValueError(
                    "LoRA worker capability requires immutable manifest and source identity"
                )
            required.update(REQUIRED_LORA_ROLES)
        if self.profile == "minimax_h3" and (
            self.source_revision is None or self.private_manifest_source_sha256 is None
        ):
            raise ValueError("MiniMax H3 requires immutable manifest and source identity")
        if self.profile == "minimax_h3" and H3_UPSCALER_ROLE in roles:
            required.add(H3_UPSCALER_ROLE)
        if roles != required or len(roles) != len(objects):
            raise ValueError("model manifest roles are incomplete or duplicated")
        return self

    @property
    def effective_workflow_template(self) -> Path:
        if self.profile == "minimax_h3":
            return self.workflow_template.parent / "dasiwa-minimax-h3-i2v-v1.api.json"
        return self.workflow_template

    @property
    def model_objects(self) -> tuple[ModelObject, ...]:
        try:
            raw = json.loads(self.model_objects_json.get_secret_value())
            if not isinstance(raw, list):
                raise ValueError
            return tuple(ModelObject.model_validate(item) for item in raw)
        except (TypeError, ValueError):
            raise ValueError("model object manifest is invalid") from None

    @property
    def model_objects_sha256(self) -> str:
        return hashlib.sha256(
            self.model_objects_json.get_secret_value().encode("utf-8")
        ).hexdigest()

    @property
    def artifact_identity_sha256(self) -> str:
        identity = [
            {
                "role": item.role,
                "byte_size": item.byte_size,
                "sha256": item.sha256,
                "version_id": item.version_id,
            }
            for item in self.model_objects
        ]
        encoded = json.dumps(
            identity,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @property
    def allowed_gpu_names(self) -> frozenset[str]:
        return frozenset(
            name.strip() for name in self.allowed_gpu_names_csv.split(",") if name.strip()
        )


def _is_container_absolute(path: Path) -> bool:
    """Recognize Linux container roots while unit tests run on Windows."""

    return path.is_absolute() or path.as_posix().startswith("/")
