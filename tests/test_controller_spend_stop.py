from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest
from sqlalchemy import func, select

from gen_automation.config import Settings
from gen_automation.controller.runtime import ControllerWorkloads, _SubmissionProgress
from gen_automation.db.models import (
    AuditEvent,
    GenerationAttempt,
    GenerationJob,
    OutboxEvent,
    Project,
    ProviderSpendEntry,
    Release,
    ReleaseVersion,
    SaladDeployment,
)
from gen_automation.db.session import Database
from gen_automation.domain.canonical import canonical_sha256
from gen_automation.domain.enums import (
    DesiredDeploymentState,
    GenerationAttemptState,
    GenerationState,
    OutboxStatus,
    SaladDeploymentState,
)
from gen_automation.integrations.salad.client import SaladClient
from gen_automation.integrations.salad.models import (
    JSONValue,
    SaladContainerGroup,
    SaladContainerGroupState,
    SaladQueue,
)
from gen_automation.services.budgets import ensure_budget_guard, reserve_attempt_budget
from gen_automation.services.outbox import SALAD_JOB_SUBMIT_TOPIC
from gen_automation.services.salad import prepare_generation_attempt
from gen_automation.services.salad_deployments import deterministic_provider_name
from gen_automation.storage.base import ObjectStore

NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
CONFIG_SHA256 = "a" * 64
IMAGE_DIGEST = "registry.example.test/worker@sha256:" + "b" * 64
QUEUE_ID = UUID("3d59eff3-8f46-4743-ab42-c5bdd56a04ca")
GROUP_ID = UUID("e1f35986-d00a-44d0-a0c6-59dda919b07b")


class DeploymentOnlyClient:
    def __init__(self, *, queue: SaladQueue, group: SaladContainerGroup) -> None:
        self.queue = queue
        self.group = group
        self.stop_names: list[str] = []
        self.create_calls = 0

    async def create_queue(
        self,
        name: str,
        *,
        display_name: str | None = None,
        description: str | None = None,
    ) -> SaladQueue:
        del name, display_name, description
        self.create_calls += 1
        raise AssertionError("disabled allocation must not create a queue")

    async def get_queue(self, queue_name: str) -> SaladQueue:
        assert queue_name == self.queue.name
        return self.queue

    async def create_container_group(
        self,
        configuration: Mapping[str, JSONValue],
    ) -> SaladContainerGroup:
        del configuration
        self.create_calls += 1
        raise AssertionError("disabled allocation must not create a container group")

    async def get_container_group(
        self,
        container_group_name: str,
    ) -> SaladContainerGroup:
        assert container_group_name == self.group.name
        return self.group

    async def stop_container_group(self, container_group_name: str) -> None:
        self.stop_names.append(container_group_name)


@dataclass(frozen=True)
class SubmissionContext:
    database: Database
    attempt_id: UUID
    job_id: UUID


def _provider_configuration() -> dict[str, object]:
    return {
        "container": {
            "resources": {
                "cpu": 4,
                "memory": 16384,
                "gpu_classes": ["gpu-class"],
            },
        },
        "replicas": 0,
        "queue_connection": {},
    }


def _remote_names() -> tuple[str, str]:
    return (
        deterministic_provider_name(
            "generation",
            version_no=1,
            config_sha256=CONFIG_SHA256,
        ),
        deterministic_provider_name(
            "worker",
            version_no=1,
            config_sha256=CONFIG_SHA256,
        ),
    )


def _queue(name: str) -> SaladQueue:
    return SaladQueue(
        id=QUEUE_ID,
        name=name,
        display_name=name,
        description=None,
        current_queue_length=0,
        container_groups=(),
        create_time=NOW,
        update_time=NOW,
    )


def _group(name: str, queue_name: str) -> SaladContainerGroup:
    return SaladContainerGroup(
        id=GROUP_ID,
        name=name,
        display_name=name,
        replicas=1,
        pending_change=False,
        version=1,
        current_state=SaladContainerGroupState(
            description="running",
            allocating_count=0,
            creating_count=0,
            running_count=1,
            stopping_count=0,
            start_time=NOW,
            finish_time=None,
        ),
        create_time=NOW,
        update_time=NOW,
        raw={
            "id": str(GROUP_ID),
            "name": name,
            "queue_connection": {"queue_name": queue_name},
        },
    )


@pytest.mark.asyncio
async def test_disabled_allocation_durably_stops_unknown_resource_with_missing_ids(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'disabled-allocation.db').as_posix()}")
    await database.create_schema()
    queue_name, group_name = _remote_names()
    client = DeploymentOnlyClient(
        queue=_queue(queue_name),
        group=_group(group_name, queue_name),
    )
    try:
        async with database.sessions() as session:
            deployment = SaladDeployment(
                version_no=1,
                config_sha256=CONFIG_SHA256,
                provider_configuration=_provider_configuration(),
                worker_image_digest=IMAGE_DIGEST,
                organization_name="organization",
                project_name="project",
                queue_name="generation",
                container_group_name="worker",
                state=SaladDeploymentState.UNKNOWN,
                desired_state=DesiredDeploymentState.ACTIVE,
                is_current=True,
                max_hourly_cost_microusd=3_600_000,
                unknown_since=NOW,
                created_at=NOW,
                updated_at=NOW,
            )
            session.add(deployment)
            await session.commit()
            deployment_id = deployment.id

        workloads = ControllerWorkloads(
            settings=Settings().model_copy(
                update={
                    "gpu_allocation_enabled": False,
                    "salad_daily_budget_usd": Decimal("100"),
                    "salad_monthly_budget_usd": Decimal("1000"),
                }
            ),
            sessions=database.sessions,
            instance_id="controller-disabled-test",
            salad_client=cast(SaladClient, client),
            object_store=None,
        )
        await workloads.initialize()
        async with database.sessions() as session:
            deployment = await session.get(SaladDeployment, deployment_id)
            assert deployment is not None
            assert deployment.desired_state == DesiredDeploymentState.STOPPED
            assert deployment.last_error_code == "gpu_allocation_disabled"

        assert await workloads.deployment_once() is True

        async with database.sessions() as session:
            deployment = await session.get(SaladDeployment, deployment_id)
            actions = set(
                (
                    await session.scalars(
                        select(AuditEvent.action).where(AuditEvent.resource_id == deployment_id)
                    )
                ).all()
            )
            assert deployment is not None
            assert deployment.state == SaladDeploymentState.DRAINING
            assert deployment.provider_queue_id == str(QUEUE_ID)
            assert deployment.provider_container_group_id == str(GROUP_ID)
            assert "salad_deployment.gpu_allocation_disabled" in actions
            assert "salad_deployment.stop_container_group_recovered" in actions
        assert client.stop_names == [group_name]
        assert client.create_calls == 0
    finally:
        await database.dispose()


async def _seed_submission_database(database: Database) -> SubmissionContext:
    async with database.sessions() as session:
        await ensure_budget_guard(
            session,
            provider="salad",
            daily_limit_usd=Decimal("100"),
            monthly_limit_usd=Decimal("1000"),
            now=NOW,
        )
        project = Project(slug="submission-timeout", name="Submission Timeout")
        session.add(project)
        await session.flush()
        release = Release(
            project_id=project.id,
            slug="release",
            title="Release",
            desired_accepted_count=1,
        )
        session.add(release)
        await session.flush()
        version = ReleaseVersion(
            release_id=release.id,
            version_no=1,
            specification={"schema_version": 1},
            specification_sha256="c" * 64,
            created_by="test",
            created_at=NOW,
        )
        deployment = SaladDeployment(
            version_no=1,
            config_sha256=CONFIG_SHA256,
            provider_configuration=_provider_configuration(),
            worker_image_digest=IMAGE_DIGEST,
            organization_name="organization",
            project_name="project",
            queue_name="generation",
            provider_queue_id=str(QUEUE_ID),
            container_group_name="worker",
            provider_container_group_id=str(GROUP_ID),
            state=SaladDeploymentState.ACTIVE,
            desired_state=DesiredDeploymentState.ACTIVE,
            is_current=True,
            max_hourly_cost_microusd=2_000_000,
            created_at=NOW,
            updated_at=NOW,
        )
        session.add_all([version, deployment])
        await session.flush()
        job = GenerationJob(
            release_version_id=version.id,
            logical_key="d" * 64,
            parameters={"seed": 1},
            parameters_sha256=canonical_sha256({"seed": 1}),
            provider="salad",
            state=GenerationState.QUEUED,
            expected_output_count=1,
        )
        session.add(job)
        await session.flush()
        prepared = await prepare_generation_attempt(
            session,
            generation_job_id=job.id,
            salad_deployment_id=deployment.id,
            idempotency_key="submission-timeout",
            now=NOW,
        )
        await session.commit()
        return SubmissionContext(
            database=database,
            attempt_id=prepared.generation_attempt_id,
            job_id=job.id,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_post_started", [False, True])
async def test_submission_timeout_preserves_exact_pre_post_effect_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider_post_started: bool,
) -> None:
    suffix = "post" if provider_post_started else "pre"
    database = Database(
        f"sqlite+aiosqlite:///{(tmp_path / f'submit-timeout-{suffix}.db').as_posix()}"
    )
    await database.create_schema()
    try:
        context = await _seed_submission_database(database)
        workloads = ControllerWorkloads(
            settings=Settings().model_copy(
                update={
                    "gpu_allocation_enabled": True,
                    "background_submit_timeout_seconds": 0.2,
                }
            ),
            sessions=database.sessions,
            instance_id=f"controller-{suffix}-timeout",
            salad_client=cast(SaladClient, object()),
            object_store=cast(ObjectStore, object()),
        )

        async def hang_submission(
            event: object,
            *,
            progress: _SubmissionProgress,
        ) -> object:
            del event
            async with database.sessions() as session:
                await reserve_attempt_budget(
                    session,
                    provider="salad",
                    attempt_id=context.attempt_id,
                    amount_microusd=2_000_000,
                    now=NOW,
                )
                job = await session.get(GenerationJob, context.job_id)
                assert job is not None
                job.state = GenerationState.SUBMITTING
                job.lock_version += 1
                await session.commit()
            if provider_post_started:
                progress.mark_provider_post_started()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        monkeypatch.setattr(workloads, "_submit_event", hang_submission)
        with pytest.raises(TimeoutError):
            await workloads.submit_once()

        async with database.sessions() as session:
            attempt = await session.get(GenerationAttempt, context.attempt_id)
            job = await session.get(GenerationJob, context.job_id)
            outbox = await session.scalar(
                select(OutboxEvent).where(
                    OutboxEvent.topic == SALAD_JOB_SUBMIT_TOPIC,
                    OutboxEvent.aggregate_id == context.attempt_id,
                )
            )
            spend_count = await session.scalar(select(func.count()).select_from(ProviderSpendEntry))

        assert attempt is not None
        assert job is not None
        assert outbox is not None
        assert spend_count == 0
        if provider_post_started:
            assert attempt.state == GenerationAttemptState.UNKNOWN
            assert attempt.reservation_released_at is None
            assert job.state == GenerationState.UNKNOWN
            assert outbox.status == OutboxStatus.DEAD_LETTER
        else:
            assert attempt.state == GenerationAttemptState.FAILED
            assert attempt.reservation_released_at is not None
            assert job.state == GenerationState.RETRY_WAIT
            assert outbox.status == OutboxStatus.SUCCEEDED
    finally:
        await database.dispose()
