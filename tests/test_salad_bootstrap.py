from __future__ import annotations

from pathlib import Path
from typing import cast
from uuid import UUID

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from gen_automation.config import Environment, Settings
from gen_automation.controller.runtime import (
    ControllerWorkloads,
    salad_deployment_config_from_settings,
    salad_video_deployment_config_from_settings,
)
from gen_automation.db.models import SaladDeployment
from gen_automation.db.session import Database
from gen_automation.domain.enums import (
    DesiredDeploymentState,
    SaladDeploymentPurpose,
    SaladDeploymentState,
)
from gen_automation.domain.signing import encode_base64url
from gen_automation.integrations.salad.client import SaladClient

GPU_CLASS_ID = UUID("3c90c3cc-0d44-4b50-8888-8dd25736052a")
WORKER_IMAGE = "registry.example.test/worker@sha256:" + "b" * 64
WORKER_SIGNING_PRIVATE_KEY = encode_base64url(bytes(range(1, 33)))


def _settings(**updates: object) -> Settings:
    values: dict[str, object] = {
        "environment": Environment.TEST,
        "gpu_allocation_enabled": True,
        "storage_enabled": True,
        "storage_bucket": "private-assets",
        "salad_enabled": True,
        "salad_api_key": "test-key",
        "salad_organization": "creator-org",
        "salad_project": "production",
        "salad_queue_name": "generation",
        "salad_container_group_name": "worker",
        "salad_gpu_class_ids": (GPU_CLASS_ID,),
        "salad_webhook_secret": "whsec_test",
        "salad_worker_image": WORKER_IMAGE,
        "worker_signing_key_id": "worker-key-1",
        "worker_signing_private_key": WORKER_SIGNING_PRIVATE_KEY,
        "salad_max_hourly_cost_usd": "0.35",
    }
    values.update(updates)
    return Settings(**values)  # type: ignore[arg-type]


def _workloads(database: Database, settings: Settings) -> ControllerWorkloads:
    return ControllerWorkloads(
        settings=settings,
        sessions=database.sessions,
        instance_id="controller-bootstrap-test",
        salad_client=cast(SaladClient, object()),
        object_store=None,
    )


def test_gpu_bootstrap_settings_require_unique_gpu_classes_and_budget_bound() -> None:
    with pytest.raises(ValidationError, match="at least one Salad GPU class ID"):
        _settings(salad_gpu_class_ids=())
    with pytest.raises(ValidationError, match="GPU class IDs must be unique"):
        _settings(salad_gpu_class_ids=(GPU_CLASS_ID, GPU_CLASS_ID))
    with pytest.raises(ValidationError, match="cannot exceed the daily budget"):
        _settings(salad_max_hourly_cost_usd="26.00")
    with pytest.raises(ValidationError, match="video and image Salad queues"):
        _settings(video_generation_enabled=False, salad_video_queue_name="generation")
    with pytest.raises(ValidationError, match="video and image Salad container groups"):
        _settings(video_generation_enabled=False, salad_video_container_group_name="worker")


def test_deployment_config_is_deterministic_secret_free_and_scale_to_zero() -> None:
    settings = _settings(
        salad_container_cpu=6,
        salad_container_memory_mb=24 * 1024,
        salad_container_storage_bytes=60 * 1024 * 1024 * 1024,
    )

    first = salad_deployment_config_from_settings(settings)
    second = salad_deployment_config_from_settings(settings)
    prefetched = salad_deployment_config_from_settings(
        settings.model_copy(update={"salad_max_queued_jobs": 5})
    )
    high_priority = salad_deployment_config_from_settings(
        _settings(
            salad_container_cpu=6,
            salad_container_memory_mb=24 * 1024,
            salad_container_storage_bytes=60 * 1024 * 1024 * 1024,
            salad_container_priority="high",
        )
    )

    assert first == second
    assert first.config_sha256 == second.config_sha256
    assert prefetched == first
    assert prefetched.config_sha256 == first.config_sha256
    assert prefetched.desired_queue_length == 1
    assert high_priority != first
    assert high_priority.config_sha256 != first.config_sha256
    assert high_priority.provider_configuration["container"]["priority"] == "high"
    assert first.min_replicas == 0
    assert first.max_replicas == 1
    assert settings.salad_max_queued_jobs == 3
    assert first.desired_queue_length == 1
    assert first.max_hourly_cost_microusd == 350_000
    assert first.provider_configuration["replicas"] == 0
    assert first.provider_configuration["queue_autoscaler"] == {"polling_period": 15}
    assert first.provider_configuration["container"] == {
        "resources": {
            "cpu": 6,
            "memory": 24 * 1024,
            "storage_amount": 60 * 1024 * 1024 * 1024,
            "gpu_classes": [str(GPU_CLASS_ID)],
        },
        "image_caching": True,
        "priority": "low",
    }
    assert "priority" not in first.provider_configuration
    assert "test-key" not in str(first.provider_configuration)
    assert "whsec_test" not in str(first.provider_configuration)
    assert "purpose" not in first.canonical_value()


def test_video_deployment_is_isolated_cached_and_scale_to_zero() -> None:
    video_gpu_class_id = UUID("f2e8a738-2fb5-4e42-8c1a-944509496506")
    settings = _settings(
        video_generation_enabled=True,
        salad_worker_allowed_upload_origin="https://private-assets.example.test",
        salad_video_queue_name="video-generation",
        salad_video_container_group_name="video-worker",
        salad_video_worker_image=("registry.example.test/video-worker@sha256:" + "c" * 64),
        salad_video_gpu_class_ids=(video_gpu_class_id,),
    )

    config = salad_video_deployment_config_from_settings(settings)

    assert config.purpose == SaladDeploymentPurpose.VIDEO
    assert config.canonical_value()["purpose"] == "video"
    assert config.queue_name == "video-generation"
    assert config.container_group_name == "video-worker"
    assert config.min_replicas == 0
    assert config.max_replicas == 1
    assert config.desired_queue_length == 1
    assert config.provider_configuration["replicas"] == 0
    assert config.provider_configuration["container"]["image_caching"] is True
    assert config.provider_configuration["container"]["priority"] == "low"
    assert config.provider_configuration["container"]["resources"] == {
        "cpu": 4,
        "memory": 32 * 1024,
        "storage_amount": 50 * 1024 * 1024 * 1024,
        "gpu_classes": [str(video_gpu_class_id)],
    }
    binding_names = {item["name"] for item in config.provider_configuration["runtime_bindings"]}
    assert binding_names == {
        "VIDEO_WORKER_ALLOWED_SOURCE_ORIGINS_JSON",
        "VIDEO_WORKER_ALLOWED_UPLOAD_ORIGIN",
        "VIDEO_WORKER_ENVIRONMENT",
        "VIDEO_WORKER_VERIFICATION_KEYS_JSON",
    }
    fingerprint = config.provider_configuration["runtime_binding_contract_sha256"]
    assert isinstance(fingerprint, str) and len(fingerprint) == 64

    rotated = salad_video_deployment_config_from_settings(
        _settings(
            video_generation_enabled=True,
            salad_worker_allowed_upload_origin="https://private-assets.example.test",
            salad_video_queue_name="video-generation",
            salad_video_container_group_name="video-worker",
            salad_video_worker_image=("registry.example.test/video-worker@sha256:" + "c" * 64),
            salad_video_gpu_class_ids=(video_gpu_class_id,),
            worker_signing_key_id="worker-key-2",
            worker_signing_private_key=encode_base64url(bytes(range(33, 65))),
        )
    )
    assert rotated.config_sha256 != config.config_sha256
    assert rotated.provider_configuration["runtime_binding_contract_sha256"] != fingerprint

    batch_settings = _settings(
        video_generation_enabled=True,
        salad_worker_allowed_upload_origin="https://private-assets.example.test",
        salad_video_queue_name="video-generation",
        salad_video_container_group_name="video-worker",
        salad_video_worker_image=("registry.example.test/video-worker@sha256:" + "c" * 64),
        salad_video_gpu_class_ids=(video_gpu_class_id,),
        salad_video_container_priority="batch",
    )
    batch = salad_video_deployment_config_from_settings(batch_settings)
    assert batch.provider_configuration["container"]["priority"] == "batch"
    assert (
        salad_deployment_config_from_settings(batch_settings).config_sha256
        == salad_deployment_config_from_settings(settings).config_sha256
    )


def test_video_settings_require_grants_to_outlive_bounded_queue_reconciliation() -> None:
    with pytest.raises(
        ValidationError,
        match="upload grant TTL must cover two attempt watchdog windows",
    ):
        _settings(
            video_generation_enabled=True,
            salad_worker_allowed_upload_origin="https://private-assets.example.test",
            salad_video_queue_name="video-generation",
            salad_video_container_group_name="video-worker",
            salad_video_worker_image=("registry.example.test/video-worker@sha256:" + "c" * 64),
            salad_video_gpu_class_ids=(UUID("f2e8a738-2fb5-4e42-8c1a-944509496506"),),
            worker_upload_grant_ttl_seconds=12_629,
        )


@pytest.mark.asyncio
async def test_initialize_reuses_identical_bootstrap_and_versions_changed_config(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'bootstrap.db').as_posix()}")
    await database.create_schema()
    try:
        settings = _settings()
        await _workloads(database, settings).initialize()
        await _workloads(database, settings).initialize()

        changed = _settings(salad_container_memory_mb=20 * 1024)
        await _workloads(database, changed).initialize()

        async with database.sessions() as session:
            deployments = list(
                (
                    await session.scalars(
                        select(SaladDeployment).order_by(SaladDeployment.version_no)
                    )
                ).all()
            )

        assert len(deployments) == 2
        previous, current = deployments
        assert previous.version_no == 1
        assert previous.is_current is False
        assert previous.desired_state == DesiredDeploymentState.STOPPED
        assert previous.last_error_code == "superseded_by_new_deployment"
        assert current.version_no == 2
        assert current.is_current is True
        assert current.state == SaladDeploymentState.PLANNED
        assert current.desired_state == DesiredDeploymentState.ACTIVE
        assert previous.config_sha256 != current.config_sha256
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_first_post_migration_bootstrap_replays_legacy_image_hash(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'legacy-image.db').as_posix()}")
    await database.create_schema()
    try:
        settings = _settings()
        config = salad_deployment_config_from_settings(settings)
        legacy = SaladDeployment(
            purpose=SaladDeploymentPurpose.IMAGE,
            version_no=1,
            config_sha256=config.config_sha256,
            worker_image_digest=config.worker_image_digest,
            organization_name=config.organization_name,
            project_name=config.project_name,
            queue_name=config.queue_name,
            provider_configuration=dict(config.provider_configuration),
            container_group_name=config.container_group_name,
            state=SaladDeploymentState.PLANNED,
            desired_state=DesiredDeploymentState.ACTIVE,
            is_current=True,
            min_replicas=config.min_replicas,
            max_replicas=config.max_replicas,
            desired_queue_length=config.desired_queue_length,
            max_hourly_cost_microusd=config.max_hourly_cost_microusd,
        )
        async with database.sessions() as session:
            session.add(legacy)
            await session.commit()
            legacy_id = legacy.id

        await _workloads(database, settings).initialize()

        async with database.sessions() as session:
            deployments = list((await session.scalars(select(SaladDeployment))).all())

        assert len(deployments) == 1
        assert deployments[0].id == legacy_id
        assert deployments[0].version_no == 1
        assert deployments[0].is_current is True
        assert deployments[0].desired_state == DesiredDeploymentState.ACTIVE
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_initialize_creates_independent_current_image_and_video_lanes(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'video-lanes.db').as_posix()}")
    await database.create_schema()
    try:
        settings = _settings(
            video_generation_enabled=True,
            salad_worker_allowed_upload_origin="https://private-assets.example.test",
            salad_video_queue_name="video-generation",
            salad_video_container_group_name="video-worker",
            salad_video_worker_image=("registry.example.test/video-worker@sha256:" + "c" * 64),
            salad_video_gpu_class_ids=(UUID("f2e8a738-2fb5-4e42-8c1a-944509496506"),),
        )
        await _workloads(database, settings).initialize()

        async with database.sessions() as session:
            deployments = list(
                (
                    await session.scalars(
                        select(SaladDeployment).order_by(SaladDeployment.version_no)
                    )
                ).all()
            )

        assert len(deployments) == 2
        assert {deployment.purpose for deployment in deployments} == {
            SaladDeploymentPurpose.IMAGE,
            SaladDeploymentPurpose.VIDEO,
        }
        assert all(deployment.is_current for deployment in deployments)
        assert all(
            deployment.desired_state == DesiredDeploymentState.ACTIVE for deployment in deployments
        )
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_disabling_and_reenabling_video_replaces_only_the_stopped_video_lane(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'disable-video.db').as_posix()}")
    await database.create_schema()
    try:
        enabled = _settings(
            video_generation_enabled=True,
            salad_worker_allowed_upload_origin="https://private-assets.example.test",
            salad_video_queue_name="video-generation",
            salad_video_container_group_name="video-worker",
            salad_video_worker_image=("registry.example.test/video-worker@sha256:" + "c" * 64),
            salad_video_gpu_class_ids=(UUID("f2e8a738-2fb5-4e42-8c1a-944509496506"),),
        )
        await _workloads(database, enabled).initialize()
        disabled = enabled.model_copy(update={"video_generation_enabled": False})
        await _workloads(database, disabled).initialize()

        async with database.sessions() as session:
            deployments = list(
                (
                    await session.scalars(select(SaladDeployment).order_by(SaladDeployment.purpose))
                ).all()
            )

        assert len(deployments) == 2
        by_purpose = {deployment.purpose: deployment for deployment in deployments}
        assert (
            by_purpose[SaladDeploymentPurpose.IMAGE].desired_state == DesiredDeploymentState.ACTIVE
        )
        video = by_purpose[SaladDeploymentPurpose.VIDEO]
        assert video.desired_state == DesiredDeploymentState.STOPPED
        assert video.last_error_code == "video_generation_disabled"
        assert video.reconcile_after is not None

        await _workloads(database, enabled).initialize()
        async with database.sessions() as session:
            deployments = list(
                (
                    await session.scalars(
                        select(SaladDeployment).order_by(SaladDeployment.version_no)
                    )
                ).all()
            )

        image_deployments = [
            deployment
            for deployment in deployments
            if deployment.purpose == SaladDeploymentPurpose.IMAGE
        ]
        video_deployments = [
            deployment
            for deployment in deployments
            if deployment.purpose == SaladDeploymentPurpose.VIDEO
        ]
        assert len(image_deployments) == 1
        assert image_deployments[0].is_current is True
        assert image_deployments[0].desired_state == DesiredDeploymentState.ACTIVE
        assert len(video_deployments) == 2
        old_video, current_video = video_deployments
        assert old_video.is_current is False
        assert old_video.desired_state == DesiredDeploymentState.STOPPED
        assert old_video.last_error_code == "superseded_by_new_deployment"
        assert old_video.administrative_stop_reason == "video_generation_disabled"
        assert current_video.is_current is True
        assert current_video.desired_state == DesiredDeploymentState.ACTIVE
        assert current_video.administrative_stop_reason is None
        assert current_video.state == SaladDeploymentState.PLANNED
        assert old_video.config_sha256 == current_video.config_sha256
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_initialize_does_not_create_deployment_when_gpu_allocation_is_disabled(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'disabled.db').as_posix()}")
    await database.create_schema()
    try:
        settings = _settings(gpu_allocation_enabled=False)
        await _workloads(database, settings).initialize()
        async with database.sessions() as session:
            count = await session.scalar(select(func.count()).select_from(SaladDeployment))
        assert count == 0
    finally:
        await database.dispose()
