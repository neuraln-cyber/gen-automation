"""Build the fresh I2V runtime from validated settings and short-lived secrets."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from gen_automation.config import Settings
from gen_automation.domain.runtime_bindings import (
    WORKER_ARTIFACT_ACCESS_KEY_ID_BINDING,
    WORKER_ARTIFACT_ENDPOINT_URL_BINDING,
    WORKER_ARTIFACT_SECRET_ACCESS_KEY_BINDING,
    WORKER_ARTIFACT_SESSION_TOKEN_BINDING,
)
from gen_automation.services.i2v_runtime import I2VRuntimeConfig
from gen_automation.services.i2v_salad import I2VSaladConfig
from gen_automation.services.runtime_secrets import (
    RuntimeSecretResolver,
    configured_runtime_binding_references,
)

_REQUIRED_ROLES = (
    "diffusion_model_high",
    "diffusion_model_low",
    "text_encoder",
    "vae",
)


class I2VEnvironmentError(Exception):
    """A redacted deployment configuration failure."""


@dataclass(frozen=True, slots=True)
class I2VRuntimeEnvironment:
    settings: Settings
    resolver: RuntimeSecretResolver

    async def resolve(self) -> Mapping[str, str]:
        references = configured_runtime_binding_references(self.settings)
        resolved = await self.resolver.resolve_many(references)
        required_credentials = (
            WORKER_ARTIFACT_ACCESS_KEY_ID_BINDING,
            WORKER_ARTIFACT_SECRET_ACCESS_KEY_BINDING,
            WORKER_ARTIFACT_SESSION_TOKEN_BINDING,
        )
        if any(not resolved.get(name) for name in required_credentials):
            raise I2VEnvironmentError("I2V model credentials are unavailable")
        environment = {
            "GEN_I2V_WORKER_ENVIRONMENT": "production",
            "GEN_I2V_WORKER_AWS_REGION": _artifact_region(self.settings),
            "GEN_I2V_WORKER_MODEL_OBJECTS_JSON": _worker_model_objects(self.settings),
            "AWS_ACCESS_KEY_ID": resolved[WORKER_ARTIFACT_ACCESS_KEY_ID_BINDING],
            "AWS_SECRET_ACCESS_KEY": resolved[WORKER_ARTIFACT_SECRET_ACCESS_KEY_BINDING],
            "AWS_SESSION_TOKEN": resolved[WORKER_ARTIFACT_SESSION_TOKEN_BINDING],
        }
        endpoint = resolved.get(WORKER_ARTIFACT_ENDPOINT_URL_BINDING)
        if endpoint is not None:
            environment["GEN_I2V_WORKER_S3_ENDPOINT_URL"] = endpoint
        return environment


def i2v_runtime_config_from_settings(settings: Settings) -> I2VRuntimeConfig:
    if (
        not settings.i2v_enabled
        or settings.i2v_worker_image is None
        or settings.i2v_salad_gpu_class_id is None
    ):
        raise I2VEnvironmentError("validated I2V settings are incomplete")
    return I2VRuntimeConfig(
        salad=I2VSaladConfig(
            queue_name=settings.i2v_salad_queue_name,
            container_group_name=settings.i2v_salad_container_group_name,
            worker_image=settings.i2v_worker_image,
            gpu_class_id=settings.i2v_salad_gpu_class_id,
            gpu_class_name=settings.i2v_salad_gpu_class_name,
            prefetch=settings.i2v_salad_prefetch,
            worker_lease_seconds=settings.i2v_worker_lease_seconds,
            warm_idle_seconds=settings.i2v_warm_idle_seconds,
            cpu=settings.i2v_salad_cpu,
            memory_mb=settings.i2v_salad_memory_mb,
            storage_bytes=settings.i2v_salad_storage_bytes,
            priority=settings.i2v_salad_priority.value,
            max_replicas=settings.i2v_salad_max_replicas,
        ),
        output_prefix=settings.i2v_output_prefix,
    )


def _artifact_region(settings: Settings) -> str:
    value = settings.salad_worker_artifact_region
    if value is None or not value.get_secret_value().strip():
        raise I2VEnvironmentError("I2V model region is unavailable")
    return value.get_secret_value()


def _worker_model_objects(settings: Settings) -> str:
    raw_manifest = settings.i2v_model_manifest_json
    raw_digest = settings.i2v_model_manifest_sha256
    bucket_value = settings.salad_worker_artifact_bucket
    if raw_manifest is None or raw_digest is None or bucket_value is None:
        raise I2VEnvironmentError("I2V private model manifest is unavailable")
    raw = raw_manifest.get_secret_value()
    if hashlib.sha256(raw.encode("utf-8")).hexdigest() != raw_digest.get_secret_value():
        raise I2VEnvironmentError("I2V private model manifest digest changed")
    try:
        document = json.loads(raw)
        objects = document["objects"]
    except (KeyError, TypeError, ValueError):
        raise I2VEnvironmentError("I2V private model manifest is invalid") from None
    if not isinstance(objects, list):
        raise I2VEnvironmentError("I2V private model manifest is invalid")
    by_role: dict[str, dict[str, Any]] = {}
    for value in objects:
        if not isinstance(value, dict) or not isinstance(value.get("role"), str):
            raise I2VEnvironmentError("I2V private model manifest is invalid")
        role = value["role"]
        if role in by_role:
            raise I2VEnvironmentError("I2V private model manifest duplicates a role")
        by_role[role] = value
    if any(role not in by_role for role in _REQUIRED_ROLES):
        raise I2VEnvironmentError("I2V private model manifest is incomplete")
    install_directories = {
        "diffusion_model_high": "models/diffusion_models",
        "diffusion_model_low": "models/diffusion_models",
        "text_encoder": "models/text_encoders",
        "vae": "models/vae/Wan",
    }
    bucket = bucket_value.get_secret_value()
    result: list[dict[str, object]] = []
    for role in _REQUIRED_ROLES:
        value = by_role[role]
        try:
            result.append(
                {
                    "role": role,
                    "bucket": bucket,
                    "key": _text(value, "key"),
                    "version_id": _text(value, "version_id"),
                    "byte_size": _positive_int(value, "bytes"),
                    "sha256": _text(value, "sha256"),
                    "install_path": (
                        f"{install_directories[role]}/{_text(value, 'target_filename')}"
                    ),
                }
            )
        except ValueError:
            raise I2VEnvironmentError("I2V private model manifest is invalid") from None
    return json.dumps(result, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _text(value: Mapping[str, Any], key: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result:
        raise ValueError
    return result


def _positive_int(value: Mapping[str, Any], key: str) -> int:
    result = value.get(key)
    if not isinstance(result, int) or isinstance(result, bool) or result <= 0:
        raise ValueError
    return result
