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
)
from gen_automation.db.models import SaladDeployment
from gen_automation.db.session import Database
from gen_automation.domain.enums import DesiredDeploymentState, SaladDeploymentState
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


def test_deployment_config_is_deterministic_secret_free_and_scale_to_zero() -> None:
    settings = _settings(
        salad_container_cpu=6,
        salad_container_memory_mb=24 * 1024,
        salad_container_storage_bytes=60 * 1024 * 1024 * 1024,
    )

    first = salad_deployment_config_from_settings(settings)
    second = salad_deployment_config_from_settings(settings)

    assert first == second
    assert first.config_sha256 == second.config_sha256
    assert first.min_replicas == 0
    assert first.max_replicas == 1
    assert first.desired_queue_length == 1
    assert first.max_hourly_cost_microusd == 350_000
    assert first.provider_configuration["replicas"] == 0
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
