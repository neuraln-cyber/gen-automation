from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr

from gen_automation.config import Settings
from gen_automation.domain.i2v import (
    I2VAttemptSnapshot,
    I2VAttemptState,
    I2VJobSnapshot,
    I2VJobState,
    I2VOutputRegistration,
)
from gen_automation.domain.runtime_bindings import (
    WORKER_ARTIFACT_ACCESS_KEY_ID_BINDING,
    WORKER_ARTIFACT_SECRET_ACCESS_KEY_BINDING,
    WORKER_ARTIFACT_SESSION_TOKEN_BINDING,
)
from gen_automation.services.i2v_environment import (
    I2VRuntimeEnvironment,
    _worker_model_objects,
    i2v_runtime_config_from_settings,
)
from gen_automation.services.i2v_media import I2VSignedGrantBuilder
from gen_automation.storage.memory import MemoryObjectStore

_NOW = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)


def _manifest() -> str:
    objects = []
    roles = (
        "diffusion_model_high",
        "diffusion_model_low",
        "text_encoder",
        "vae",
        "lora_high",
        "lora_low",
    )
    for index, role in enumerate(roles, start=1):
        sha256 = f"{index:064x}"
        objects.append(
            {
                "bytes": index,
                "key": f"worker/i2v/sha256/{sha256}",
                "name": role,
                "role": role,
                "sha256": sha256,
                "target_filename": f"{role}.safetensors",
                "version_id": f"version-{index}",
            }
        )
    return json.dumps(
        {"schema": "gen-automation/i2v-private-model-mirror/v1", "objects": objects},
        sort_keys=True,
    )


def _settings() -> Settings:
    manifest = _manifest()
    return Settings.model_construct(
        i2v_enabled=True,
        i2v_worker_image="ghcr.io/example/i2v@sha256:" + "a" * 64,
        i2v_salad_gpu_class_id=UUID("11111111-1111-4111-8111-111111111111"),
        i2v_salad_gpu_class_name="RTX 5090 (32 GB)",
        i2v_salad_queue_name="i2v-jobs-v1",
        i2v_salad_container_group_name="i2v-worker-v1",
        i2v_salad_prefetch=3,
        i2v_worker_lease_seconds=86_400,
        i2v_warm_idle_seconds=18_000,
        i2v_salad_cpu=8,
        i2v_salad_memory_mb=32_768,
        i2v_salad_storage_bytes=268_435_456_000,
        i2v_salad_priority=type("Priority", (), {"value": "high"})(),
        i2v_salad_max_replicas=1,
        i2v_output_prefix="i2v/outputs",
        i2v_model_manifest_json=SecretStr(manifest),
        i2v_model_manifest_sha256=SecretStr(hashlib.sha256(manifest.encode()).hexdigest()),
        salad_worker_artifact_bucket=SecretStr("private-models"),
        salad_worker_artifact_region=SecretStr("eu-central-1"),
    )


class _Resolver:
    async def resolve_many(self, _bindings: object) -> dict[str, str]:
        return {
            WORKER_ARTIFACT_ACCESS_KEY_ID_BINDING: "temporary-access",
            WORKER_ARTIFACT_SECRET_ACCESS_KEY_BINDING: "temporary-secret",
            WORKER_ARTIFACT_SESSION_TOKEN_BINDING: "temporary-session",
        }


def _job(*, input_version: str = "input-v1") -> I2VJobSnapshot:
    job_id = uuid4()
    return I2VJobSnapshot(
        job_id=job_id,
        created_by_user_id=uuid4(),
        input_id=uuid4(),
        preset_id=None,
        positive_prompt="motion",
        negative_prompt="jitter",
        input_snapshot={
            "storage_backend": "memory",
            "storage_bucket": "private-i2v",
            "object_key": "i2v/input.png",
            "object_version_id": input_version,
            "sha256": hashlib.sha256(b"input").hexdigest(),
            "content_type": "image/png",
            "width": 576,
            "height": 1024,
            "byte_size": 5,
        },
        preset_snapshot={},
        settings_snapshot={},
        request_sha256="c" * 64,
        state=I2VJobState.CLAIMED,
        queue_position=None,
        attempt_count=1,
        lease_owner="worker",
        lease_expires_at=_NOW,
        cancel_requested_at=None,
        completed_at=None,
        last_error_code=None,
        last_error_detail=None,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _attempt(job: I2VJobSnapshot) -> I2VAttemptSnapshot:
    return I2VAttemptSnapshot(
        attempt_id=uuid4(),
        job_id=job.job_id,
        worker_deployment_id=None,
        attempt_no=1,
        state=I2VAttemptState.CREATED,
        worker_id="worker",
        worker_image_digest=None,
        provider_job_id=None,
        request_metadata={},
        response_metadata={},
        started_at=None,
        completed_at=None,
        error_code=None,
        error_detail=None,
        created_at=_NOW,
        updated_at=_NOW,
    )


def test_runtime_model_manifest_installs_only_the_four_baseline_objects() -> None:
    objects = json.loads(_worker_model_objects(_settings()))

    assert [item["role"] for item in objects] == [
        "diffusion_model_high",
        "diffusion_model_low",
        "text_encoder",
        "vae",
    ]
    assert all(item["bucket"] == "private-models" for item in objects)
    assert all(item["install_path"].endswith(".safetensors") for item in objects)


@pytest.mark.asyncio
async def test_runtime_environment_contains_only_required_bootstrap_values() -> None:
    provider = I2VRuntimeEnvironment(settings=_settings(), resolver=_Resolver())  # type: ignore[arg-type]

    environment = await provider.resolve()

    assert environment["GEN_I2V_WORKER_ENVIRONMENT"] == "production"
    assert environment["AWS_SESSION_TOKEN"].startswith("temporary-")
    assert len(json.loads(environment["GEN_I2V_WORKER_MODEL_OBJECTS_JSON"])) == 4
    assert "CIVITAI" not in " ".join(environment)


def test_runtime_config_keeps_five_hour_warm_session_and_exact_5090() -> None:
    config = i2v_runtime_config_from_settings(_settings())

    assert config.salad.warm_idle_seconds == 18_000
    assert config.salad.gpu_class_name == "RTX 5090 (32 GB)"
    assert config.salad.storage_bytes == 268_435_456_000
    assert config.salad.max_replicas == 1


@pytest.mark.asyncio
async def test_attempt_grants_and_uploaded_object_are_exactly_bound() -> None:
    store = MemoryObjectStore(bucket="private-i2v")
    source = await store.write_bytes_if_absent(
        key="i2v/input.png",
        body=b"input",
        content_type="image/png",
        metadata={"sha256": hashlib.sha256(b"input").hexdigest()},
        max_bytes=10,
    )
    job = _job(input_version=source.version_id)
    attempt = _attempt(job)
    builder = I2VSignedGrantBuilder(store=store, expires_in=3600)

    grants = await builder.build(job=job, attempt=attempt)
    assert grants["output_grant"]["object_key"] == (
        f"i2v/outputs/{job.job_id}/{attempt.attempt_id}.mp4"
    )
    output_key = f"i2v/outputs/{job.job_id}/{attempt.attempt_id}.mp4"
    body = b"video"
    stored = await store.write_bytes_if_absent(
        key=output_key,
        body=body,
        content_type="video/mp4",
        metadata={
            "i2v-job-id": str(job.job_id),
            "i2v-attempt-id": str(attempt.attempt_id),
            "request-sha256": job.request_sha256,
        },
        max_bytes=10,
    )
    await builder.verify_output(
        job=job,
        attempt=attempt,
        output=I2VOutputRegistration(
            storage_backend="memory",
            storage_bucket="private-i2v",
            object_key=output_key,
            object_version_id=stored.version_id,
            sha256=hashlib.sha256(body).hexdigest(),
            content_type="video/mp4",
            width=576,
            height=1024,
            frame_count=81,
            fps=16,
            duration_ms=5063,
            byte_size=len(body),
        ),
    )
