from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import SecretStr

from gen_automation.config import I2VRunPodMode, Settings
from gen_automation.domain.i2v import (
    I2VAttemptSnapshot,
    I2VAttemptState,
    I2VJobSnapshot,
    I2VJobState,
    I2VOutputRegistration,
)
from gen_automation.i2v_worker.lora_catalog import LORA_ARTIFACTS_BY_ROLE
from gen_automation.i2v_worker.models import ModelObject
from gen_automation.services.i2v_environment import (
    _worker_model_objects,
    i2v_runtime_config_from_settings,
    i2v_worker_model_objects,
)
from gen_automation.services.i2v_media import I2VRunPodGrantBuilder
from gen_automation.storage.memory import MemoryObjectStore

_NOW = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)


def _manifest() -> str:
    objects = []
    roles = (
        "diffusion_model_high",
        "diffusion_model_low",
        "text_encoder",
        "vae",
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
    for role, artifact in LORA_ARTIFACTS_BY_ROLE.items():
        objects.append(
            {
                "bytes": artifact.byte_size,
                "key": f"worker/i2v/sha256/{artifact.sha256}",
                "name": role,
                "role": role,
                "sha256": artifact.sha256,
                "target_filename": artifact.filename,
                "version_id": f"version-{role}",
            }
        )
    return json.dumps(
        {"schema": "gen-automation/i2v-private-model-mirror/v1", "objects": objects},
        sort_keys=True,
    )


def _settings(
    *,
    lora_profile_enabled: bool = True,
    lora_worker_enabled: bool = True,
    manifest: str | None = None,
) -> Settings:
    manifest = manifest or _manifest()
    return Settings.model_construct(
        i2v_enabled=True,
        i2v_runpod_enabled=True,
        i2v_lora_profile_enabled=lora_profile_enabled,
        i2v_lora_worker_enabled=lora_worker_enabled,
        i2v_worker_image="ghcr.io/example/i2v@sha256:" + "a" * 64,
        i2v_worker_source_revision="b" * 40,
        i2v_private_manifest_source_sha256="c" * 64,
        i2v_runpod_endpoint_id="endpoint123",
        i2v_runpod_api_key=SecretStr("runpod-test-key-1234567890"),
        i2v_runpod_claim_url="https://staging.example/api/v1/i2v/runpod/claim",
        i2v_runpod_execution_timeout_seconds=21_600,
        i2v_runpod_job_ttl_seconds=604_800,
        i2v_worker_lease_seconds=86_400,
        i2v_output_prefix="i2v/outputs",
        i2v_model_manifest_json=SecretStr(manifest),
        i2v_model_manifest_sha256=SecretStr(hashlib.sha256(manifest.encode()).hexdigest()),
        salad_worker_artifact_bucket=SecretStr("private-models"),
        salad_worker_artifact_region=SecretStr("eu-central-1"),
    )


def _job(
    *,
    input_version: str = "input-v1",
    input_bucket: str = "private-models",
) -> I2VJobSnapshot:
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
            "storage_bucket": input_bucket,
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


def test_runtime_model_manifest_installs_baseline_and_all_reviewed_lora_objects() -> None:
    objects = json.loads(_worker_model_objects(_settings()))

    assert [item["role"] for item in objects[:4]] == [
        "diffusion_model_high",
        "diffusion_model_low",
        "text_encoder",
        "vae",
    ]
    assert [item["role"] for item in objects[4:]] == list(LORA_ARTIFACTS_BY_ROLE)
    assert all(item["bucket"] == "private-models" for item in objects)
    assert all(item["install_path"].endswith(".safetensors") for item in objects)


@pytest.mark.parametrize(
    "document",
    [
        {"objects": []},
        {"schema": "wrong", "objects": []},
        {
            "schema": "gen-automation/i2v-private-model-mirror/v1",
            "objects": [],
            "unexpected": True,
        },
    ],
)
def test_private_manifest_rejects_missing_wrong_or_extra_schema(
    document: dict[str, object],
) -> None:
    raw = json.dumps(document, sort_keys=True)
    settings = _settings(lora_worker_enabled=False, manifest=raw)

    with pytest.raises(Exception, match="manifest is invalid"):
        _worker_model_objects(settings)


def test_routine_deploy_keeps_legacy_four_role_worker_manifest_healthy() -> None:
    full = json.loads(_manifest())
    legacy_manifest = json.dumps(
        {**full, "objects": full["objects"][:4]},
        sort_keys=True,
    )
    settings = _settings(
        lora_profile_enabled=False,
        lora_worker_enabled=False,
        manifest=legacy_manifest,
    )
    objects = i2v_worker_model_objects(settings)

    assert [item.role for item in objects] == [
        "diffusion_model_high",
        "diffusion_model_low",
        "text_encoder",
        "vae",
    ]


@pytest.mark.asyncio
async def test_routine_deploy_ignores_live_legacy_lora_roles_when_worker_is_off() -> None:
    full = json.loads(_manifest())
    legacy_objects = list(full["objects"][:4])
    for index, role in enumerate(("lora_high", "lora_low"), start=20):
        digest = f"{index:064x}"
        legacy_objects.append(
            {
                "bytes": index,
                "key": f"worker/i2v/sha256/{digest}",
                "name": role,
                "role": role,
                "sha256": digest,
                "target_filename": f"legacy-{role}.safetensors",
                "version_id": f"legacy-version-{index}",
            }
        )
    live_manifest = json.dumps(
        {**full, "objects": legacy_objects},
        sort_keys=True,
    )
    settings = _settings(
        lora_profile_enabled=False,
        lora_worker_enabled=False,
        manifest=live_manifest,
    )

    objects = json.loads(_worker_model_objects(settings))

    assert [item["role"] for item in objects] == [
        "diffusion_model_high",
        "diffusion_model_low",
        "text_encoder",
        "vae",
    ]


def test_runtime_config_is_single_flight_and_exactly_bound_to_runpod() -> None:
    config = i2v_runtime_config_from_settings(_settings())

    assert config.provider_id == "endpoint123"
    assert config.claim_url == "https://staging.example/api/v1/i2v/runpod/claim"
    assert config.execution_timeout_seconds == 21_600
    assert config.job_ttl_seconds == 604_800
    assert config.worker_image.endswith("@sha256:" + "a" * 64)
    assert config.reviewed_loras_enabled is True


def test_runtime_config_binds_pod_mode_to_the_persistent_volume() -> None:
    settings = _settings().model_copy(
        update={
            "i2v_runpod_mode": I2VRunPodMode.POD,
            "i2v_runpod_endpoint_id": None,
            "i2v_runpod_network_volume_id": "volume123",
        }
    )

    config = i2v_runtime_config_from_settings(settings)

    assert config.provider_id == "pod-volume123"


def test_public_lora_gate_does_not_control_worker_capability() -> None:
    config = i2v_runtime_config_from_settings(
        _settings(lora_profile_enabled=False, lora_worker_enabled=True)
    )

    assert config.reviewed_loras_enabled is True


@pytest.mark.asyncio
async def test_attempt_grants_and_uploaded_object_are_exactly_bound() -> None:
    store = MemoryObjectStore(bucket="private-models")
    source = await store.write_bytes_if_absent(
        key="i2v/input.png",
        body=b"input",
        content_type="image/png",
        metadata={"sha256": hashlib.sha256(b"input").hexdigest()},
        max_bytes=10,
    )
    job = _job(input_version=source.version_id)
    attempt = _attempt(job)
    builder = I2VRunPodGrantBuilder(
        store=store,
        model_store=store,
        expires_in=3600,
        model_objects=(),
        output_prefix="i2v/outputs",
    )

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
            storage_bucket="private-models",
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


@pytest.mark.asyncio
async def test_runpod_grants_use_separate_asset_and_model_stores() -> None:
    asset_store = MemoryObjectStore(bucket="private-assets")
    model_store = MemoryObjectStore(bucket="private-models")
    source = await asset_store.write_bytes_if_absent(
        key="i2v/input.png",
        body=b"input",
        content_type="image/png",
        metadata={"sha256": hashlib.sha256(b"input").hexdigest()},
        max_bytes=10,
    )
    model_body = b"model"
    model_sha256 = hashlib.sha256(model_body).hexdigest()
    model_key = f"worker/i2v/sha256/{model_sha256}"
    model = await model_store.write_bytes_if_absent(
        key=model_key,
        body=model_body,
        content_type="application/x-safetensors",
        metadata={"sha256": model_sha256},
        max_bytes=10,
    )
    job = _job(input_version=source.version_id, input_bucket=asset_store.bucket)
    attempt = _attempt(job)
    builder = I2VRunPodGrantBuilder(
        store=asset_store,
        model_store=model_store,
        expires_in=3600,
        model_objects=(
            ModelObject(
                role="diffusion_model_high",
                bucket=model_store.bucket,
                key=model_key,
                version_id=model.version_id,
                byte_size=len(model_body),
                sha256=model_sha256,
                install_path="models/diffusion_models/model.safetensors",
            ),
        ),
        output_prefix="i2v/outputs",
    )

    grants = await builder.build(job=job, attempt=attempt)

    assert grants["input_grant"]["url"].startswith("memory://private-assets/")
    assert grants["output_grant"]["url"].startswith("memory://private-assets/")
    assert grants["model_grants"][0]["url"].startswith("memory://private-models/")
