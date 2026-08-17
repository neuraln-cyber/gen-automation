"""Build the fresh I2V runtime from validated settings and short-lived secrets."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from gen_automation.config import I2VRunPodMode, Settings
from gen_automation.domain.runtime_bindings import (
    WORKER_ARTIFACT_ACCESS_KEY_ID_BINDING,
    WORKER_ARTIFACT_ENDPOINT_URL_BINDING,
    WORKER_ARTIFACT_SECRET_ACCESS_KEY_BINDING,
    WORKER_ARTIFACT_SESSION_TOKEN_BINDING,
)
from gen_automation.i2v_worker.lora_catalog import LORA_ARTIFACTS_BY_ROLE, REQUIRED_LORA_ROLES
from gen_automation.i2v_worker.manifest_contract import (
    required_i2v_model_roles,
    validated_i2v_manifest_objects,
)
from gen_automation.i2v_worker.models import ModelObject
from gen_automation.services.i2v_runpod_runtime import I2VRunPodRuntimeConfig
from gen_automation.services.i2v_runtime import I2VRuntimeConfig
from gen_automation.services.i2v_salad import I2VSaladConfig
from gen_automation.services.runtime_secrets import (
    RuntimeSecretResolver,
    configured_runtime_binding_references,
)


class I2VEnvironmentError(Exception):
    """A redacted deployment configuration failure."""


@dataclass(frozen=True, slots=True)
class I2VRuntimeEnvironment:
    """Legacy Salad bootstrap environment retained only for rollback."""

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
            "GEN_I2V_WORKER_LORA_WORKER_ENABLED": (
                "true" if self.settings.i2v_lora_worker_enabled else "false"
            ),
            "GEN_I2V_WORKER_AWS_REGION": _artifact_region(self.settings),
            "GEN_I2V_WORKER_MODEL_OBJECTS_JSON": _worker_model_objects(self.settings),
            "AWS_ACCESS_KEY_ID": resolved[WORKER_ARTIFACT_ACCESS_KEY_ID_BINDING],
            "AWS_SECRET_ACCESS_KEY": resolved[WORKER_ARTIFACT_SECRET_ACCESS_KEY_BINDING],
            "AWS_SESSION_TOKEN": resolved[WORKER_ARTIFACT_SESSION_TOKEN_BINDING],
        }
        if self.settings.i2v_private_manifest_source_sha256 is not None:
            environment["GEN_I2V_WORKER_PRIVATE_MANIFEST_SOURCE_SHA256"] = (
                self.settings.i2v_private_manifest_source_sha256
            )
        if self.settings.i2v_worker_source_revision is not None:
            environment["GEN_I2V_WORKER_SOURCE_REVISION"] = self.settings.i2v_worker_source_revision
        endpoint = resolved.get(WORKER_ARTIFACT_ENDPOINT_URL_BINDING)
        if endpoint is not None:
            environment["GEN_I2V_WORKER_S3_ENDPOINT_URL"] = endpoint
        return environment


def i2v_runtime_config_from_settings(settings: Settings) -> I2VRunPodRuntimeConfig:
    if (
        not settings.i2v_enabled
        or settings.i2v_worker_image is None
        or settings.i2v_runpod_api_key is None
        or settings.i2v_runpod_claim_url is None
    ):
        raise I2VEnvironmentError("validated I2V settings are incomplete")
    provider_id: str
    if settings.i2v_runpod_mode == I2VRunPodMode.POD:
        if settings.i2v_runpod_network_volume_id is None:
            raise I2VEnvironmentError("validated I2V Pod settings are incomplete")
        provider_id = f"pod-{settings.i2v_runpod_network_volume_id}"
    else:
        if settings.i2v_runpod_endpoint_id is None:
            raise I2VEnvironmentError("validated I2V Serverless settings are incomplete")
        provider_id = settings.i2v_runpod_endpoint_id
    return I2VRunPodRuntimeConfig(
        provider_id=provider_id,
        worker_image=settings.i2v_worker_image,
        claim_url=str(settings.i2v_runpod_claim_url),
        claim_secret=settings.i2v_runpod_api_key.get_secret_value(),
        worker_lease_seconds=settings.i2v_worker_lease_seconds,
        execution_timeout_seconds=settings.i2v_runpod_execution_timeout_seconds,
        job_ttl_seconds=settings.i2v_runpod_job_ttl_seconds,
        submission_claim_timeout_seconds=(settings.i2v_runpod_submission_claim_timeout_seconds),
        queue_timeout_seconds=settings.i2v_runpod_queue_timeout_seconds,
        terminal_grace_seconds=settings.i2v_runpod_terminal_grace_seconds,
        output_prefix=settings.i2v_output_prefix,
        reviewed_loras_enabled=settings.i2v_lora_worker_enabled,
    )


def i2v_salad_runtime_config_from_settings(settings: Settings) -> I2VRuntimeConfig:
    """Build the retired provider contract for an explicit rollback only."""

    if (
        not settings.i2v_enabled
        or settings.i2v_worker_image is None
        or settings.i2v_salad_gpu_class_id is None
    ):
        raise I2VEnvironmentError("validated I2V rollback settings are incomplete")
    readiness_probe_path = "/ready"
    if settings.i2v_lora_worker_enabled:
        source_manifest_sha256 = settings.i2v_private_manifest_source_sha256
        source_revision = settings.i2v_worker_source_revision
        if source_manifest_sha256 is None or source_revision is None:
            raise I2VEnvironmentError("validated I2V capability identity is incomplete")
        readiness_probe_path = (
            "/ready/capability/"
            f"{source_manifest_sha256}/{_worker_artifact_identity_sha256(settings)}/"
            f"{source_revision}"
        )
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
            readiness_probe_path=readiness_probe_path,
        ),
        output_prefix=settings.i2v_output_prefix,
        reviewed_loras_enabled=settings.i2v_lora_worker_enabled,
    )


def _worker_artifact_identity_sha256(settings: Settings) -> str:
    objects = json.loads(_worker_model_objects(settings))
    identity = [
        {
            "role": item["role"],
            "byte_size": item["byte_size"],
            "sha256": item["sha256"],
            "version_id": item["version_id"],
        }
        for item in objects
    ]
    encoded = json.dumps(
        identity,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def i2v_worker_identity(settings: Settings) -> tuple[str, str]:
    """Return derived model-object and artifact identities without secret values."""

    model_objects = _worker_model_objects(settings)
    return (
        hashlib.sha256(model_objects.encode("utf-8")).hexdigest(),
        _worker_artifact_identity_sha256(settings),
    )


def i2v_worker_model_objects(settings: Settings) -> tuple[ModelObject, ...]:
    try:
        raw = json.loads(_worker_model_objects(settings))
        return tuple(ModelObject.model_validate(item) for item in raw)
    except (TypeError, ValueError):
        raise I2VEnvironmentError("I2V private model manifest is invalid") from None


def i2v_worker_model_objects_json(settings: Settings) -> str:
    """Return the exact canonical worker manifest without exposing storage credentials."""

    return _worker_model_objects(settings)


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
        by_role = validated_i2v_manifest_objects(
            document,
            reviewed_loras_enabled=settings.i2v_lora_worker_enabled,
        )
    except (KeyError, TypeError, ValueError):
        raise I2VEnvironmentError("I2V private model manifest is invalid") from None
    required_roles = required_i2v_model_roles(
        reviewed_loras_enabled=settings.i2v_lora_worker_enabled
    )
    # Raw private manifests may retain obsolete roles during a coordinated
    # migration. Only selected required roles cross the worker boundary.
    install_directories: dict[str, str] = {
        "diffusion_model_high": "models/diffusion_models",
        "diffusion_model_low": "models/diffusion_models",
        "text_encoder": "models/text_encoders",
        "vae": "models/vae/Wan",
        **{role: "models/loras" for role in REQUIRED_LORA_ROLES},
    }
    bucket = bucket_value.get_secret_value()
    result: list[dict[str, object]] = []
    for role in required_roles:
        value = by_role[role]
        try:
            expected_lora = LORA_ARTIFACTS_BY_ROLE.get(role)
            if expected_lora is not None and (
                _text(value, "target_filename") != expected_lora.filename
                or _positive_int(value, "bytes") != expected_lora.byte_size
                or _text(value, "sha256") != expected_lora.sha256
            ):
                raise ValueError
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
