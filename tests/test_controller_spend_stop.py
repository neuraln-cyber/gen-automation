from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from uuid import UUID

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

import gen_automation.controller.runtime as controller_runtime
from gen_automation.config import Settings
from gen_automation.controller.runtime import ControllerWorkloads, _SubmissionProgress
from gen_automation.db.models import (
    AuditEvent,
    ExperimentWarmLease,
    GenerationAttempt,
    GenerationJob,
    OutboxEvent,
    Project,
    ProviderBudgetGuard,
    ProviderSpendEntry,
    Release,
    ReleaseVersion,
    SaladDeployment,
)
from gen_automation.db.session import Database
from gen_automation.domain.canonical import canonical_sha256
from gen_automation.domain.enums import (
    BudgetState,
    DesiredDeploymentState,
    GenerationAttemptState,
    GenerationState,
    OutboxStatus,
    ReleasePhase,
    SaladDeploymentPurpose,
    SaladDeploymentState,
    SpendEntryType,
)
from gen_automation.domain.runtime_bindings import WORKER_RUNTIME_ADMISSION_ID_BINDING
from gen_automation.domain.signing import encode_base64url
from gen_automation.gpu_worker.artifacts import ArtifactManifest
from gen_automation.integrations.salad.client import SaladClient
from gen_automation.integrations.salad.models import (
    JSONValue,
    SaladContainerGroup,
    SaladContainerGroupInstance,
    SaladContainerGroupInstancePage,
    SaladContainerGroupInstanceState,
    SaladContainerGroupState,
    SaladQueue,
)
from gen_automation.services.budgets import ensure_budget_guard, reserve_attempt_budget
from gen_automation.services.experiment_warm_leases import start_experiment_warm_lease
from gen_automation.services.managed_artifact_manifest import EffectiveArtifactManifest
from gen_automation.services.outbox import (
    GENERATION_ATTEMPT_AGGREGATE,
    SALAD_JOB_SUBMIT_TOPIC,
    ClaimedOutboxEvent,
    claim_outbox_events,
)
from gen_automation.services.salad import (
    MutationEffect,
    SubmissionDisposition,
    SubmissionResult,
    prepare_generation_attempt,
)
from gen_automation.services.salad_deployments import (
    deterministic_provider_name,
    effective_worker_min_replicas,
    reconcile_deployment,
)
from gen_automation.storage.base import ObjectStore

NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
CONFIG_SHA256 = "a" * 64
IMAGE_DIGEST = "registry.example.test/worker@sha256:" + "b" * 64
WORKER_SIGNING_PRIVATE_KEY = encode_base64url(bytes(range(1, 33)))
RUNTIME_MANIFEST = '{"artifacts":[],"manifest_sha256":"' + ("0" * 64) + '"}'
QUEUE_ID = UUID("3d59eff3-8f46-4743-ab42-c5bdd56a04ca")
GROUP_ID = UUID("e1f35986-d00a-44d0-a0c6-59dda919b07b")
TEST_CHECKPOINT_SHA256 = "9" * 64
RUNTIME_ADMISSION_ID = "f" * 32


def _runtime_job_parameters(seed: int, loras: list[dict[str, str]]) -> dict[str, object]:
    return {
        "seed": seed,
        "checkpoint": {"sha256": TEST_CHECKPOINT_SHA256},
        "loras": loras,
    }


async def _empty_effective_artifact_manifest(
    _workloads: ControllerWorkloads,
    _session: AsyncSession,
    *,
    required_checkpoint_sha256: str,
    required_lora_sha256s: tuple[str, ...] = (),
) -> EffectiveArtifactManifest:
    assert required_checkpoint_sha256 == TEST_CHECKPOINT_SHA256
    del required_lora_sha256s
    manifest = ArtifactManifest.model_construct(
        version="v1",
        artifacts=(),
        manifest_sha256="0" * 64,
    )
    return EffectiveArtifactManifest(
        manifest=manifest,
        manifest_json=RUNTIME_MANIFEST,
        managed_lora_sha256s=frozenset(),
    )


async def _selected_effective_artifact_manifest(
    _workloads: ControllerWorkloads,
    _session: AsyncSession,
    *,
    required_checkpoint_sha256: str,
    required_lora_sha256s: tuple[str, ...] = (),
) -> EffectiveArtifactManifest:
    assert required_checkpoint_sha256 == TEST_CHECKPOINT_SHA256
    selected = tuple(sorted(required_lora_sha256s))
    digest = canonical_sha256({"managed_loras": selected})
    manifest = ArtifactManifest.model_construct(
        version="v1",
        artifacts=(),
        manifest_sha256=digest,
    )
    return EffectiveArtifactManifest(
        manifest=manifest,
        manifest_json=('{"artifacts":[],"manifest_sha256":"' + digest + '"}'),
        managed_lora_sha256s=frozenset(selected),
    )


async def _runtime_admission_ready(*args: object, **kwargs: object) -> str:
    del args, kwargs
    return "instance-creator-1"


async def _runtime_refresh_preflight(
    deployment: SaladDeployment,
    client: object,
) -> SaladContainerGroup:
    del client
    return _group(deployment.container_group_name, deployment.queue_name)


@pytest.fixture(autouse=True)
def _stable_runtime_refresh_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        controller_runtime,
        "preflight_container_group_runtime_refresh",
        _runtime_refresh_preflight,
    )


class DeploymentOnlyClient:
    def __init__(self, *, queue: SaladQueue, group: SaladContainerGroup) -> None:
        self.queue = queue
        self.group = group
        self.stop_names: list[str] = []
        self.start_names: list[str] = []
        self.updated_group_patches: list[Mapping[str, JSONValue]] = []
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

    async def list_container_group_instances(
        self,
        container_group_name: str,
    ) -> SaladContainerGroupInstancePage:
        assert container_group_name == self.group.name
        if self.group.current_state.running_count == 0:
            return SaladContainerGroupInstancePage(instances=())
        return SaladContainerGroupInstancePage(
            instances=(
                SaladContainerGroupInstance(
                    id="instance-creator-1",
                    machine_id="machine-creator-1",
                    state=SaladContainerGroupInstanceState.RUNNING,
                    update_time=self.group.current_state.start_time or NOW,
                    version=self.group.version,
                ),
            )
        )

    async def stop_container_group(self, container_group_name: str) -> None:
        self.stop_names.append(container_group_name)

    async def start_container_group(self, container_group_name: str) -> None:
        self.start_names.append(container_group_name)

    async def update_container_group(
        self,
        container_group_name: str,
        patch: Mapping[str, JSONValue],
    ) -> SaladContainerGroup:
        assert container_group_name == self.group.name
        self.updated_group_patches.append(patch)
        return self.group


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
            status="running",
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
async def test_dispatch_loop_reaches_scheduler_after_provider_start_stalls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'stalled-dispatch.db').as_posix()}")
    await database.create_schema()
    called: list[UUID] = []
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
                provider_queue_id=str(QUEUE_ID),
                container_group_name="worker",
                provider_container_group_id=str(GROUP_ID),
                purpose=SaladDeploymentPurpose.IMAGE,
                state=SaladDeploymentState.DEGRADED,
                desired_state=DesiredDeploymentState.ACTIVE,
                is_current=True,
                last_error_code="provider_start_stalled",
                max_hourly_cost_microusd=3_600_000,
                created_at=NOW,
                updated_at=NOW,
            )
            session.add(deployment)
            await session.commit()
            deployment_id = deployment.id

        async def fake_dispatch(
            session: AsyncSession,
            *,
            salad_deployment_id: UUID,
            **kwargs: object,
        ) -> object:
            del session, kwargs
            called.append(salad_deployment_id)
            return SimpleNamespace(dispatched=())

        monkeypatch.setattr(controller_runtime, "dispatch_generation_jobs", fake_dispatch)
        workloads = ControllerWorkloads(
            settings=Settings().model_copy(update={"gpu_allocation_enabled": True}),
            sessions=database.sessions,
            instance_id="controller-stalled-dispatch-test",
            salad_client=cast(SaladClient, object()),
            object_store=None,
        )

        assert await workloads.dispatch_once() is False
        assert called == [deployment_id]

        async with database.sessions() as session:
            deployment = await session.get(SaladDeployment, deployment_id)
            assert deployment is not None
            deployment.last_error_code = "provider_group_unhealthy"
            await session.commit()

        assert await workloads.dispatch_once() is False
        assert called == [deployment_id]
    finally:
        await database.dispose()


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
            assert deployment.state == SaladDeploymentState.UNKNOWN
            assert deployment.provider_queue_id is None
            assert deployment.provider_container_group_id == str(GROUP_ID)
            assert "salad_deployment.gpu_allocation_disabled" in actions
            assert "salad_deployment.stop_container_group_recovered" in actions
        assert client.stop_names == [group_name]
        assert client.create_calls == 0
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_retired_video_deployment_is_stopped_even_when_gpu_allocation_is_enabled(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'retired-video.db').as_posix()}")
    await database.create_schema()
    try:
        async with database.sessions() as session:
            deployment = SaladDeployment(
                version_no=1,
                config_sha256=CONFIG_SHA256,
                provider_configuration=_provider_configuration(),
                worker_image_digest=IMAGE_DIGEST,
                organization_name="organization",
                project_name="project",
                queue_name="retired-video",
                provider_queue_id=str(QUEUE_ID),
                container_group_name="retired-video-worker",
                provider_container_group_id=str(GROUP_ID),
                purpose=SaladDeploymentPurpose.VIDEO,
                state=SaladDeploymentState.ACTIVE,
                desired_state=DesiredDeploymentState.ACTIVE,
                is_current=True,
                max_hourly_cost_microusd=3_600_000,
                created_at=NOW,
                updated_at=NOW,
            )
            session.add(deployment)
            await session.commit()
            deployment_id = deployment.id

        workloads = ControllerWorkloads(
            settings=Settings().model_copy(update={"gpu_allocation_enabled": True}),
            sessions=database.sessions,
            instance_id="controller-retired-video-test",
            salad_client=None,
            object_store=None,
        )
        async with database.sessions() as session:
            changed = await workloads._retire_legacy_video_allocations(session, now=NOW)
            await session.commit()

        assert changed == 1
        async with database.sessions() as session:
            deployment = await session.get(SaladDeployment, deployment_id)
            action = await session.scalar(
                select(AuditEvent.action).where(AuditEvent.resource_id == deployment_id)
            )
            assert deployment is not None
            assert deployment.desired_state == DesiredDeploymentState.STOPPED
            assert deployment.last_error_code == "legacy_video_retired"
            assert action == "salad_deployment.legacy_video_retired"
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_deployment_loop_reconciles_failed_stop_with_partial_identity(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'failed-stop.db').as_posix()}")
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
                provider_container_group_id=str(GROUP_ID),
                state=SaladDeploymentState.FAILED,
                desired_state=DesiredDeploymentState.STOPPED,
                is_current=False,
                max_hourly_cost_microusd=3_600_000,
                created_at=NOW,
                updated_at=NOW,
            )
            session.add(deployment)
            await ensure_budget_guard(
                session,
                provider="salad",
                daily_limit_usd=Decimal("100"),
                monthly_limit_usd=Decimal("1000"),
                now=NOW,
            )
            await session.commit()
            deployment_id = deployment.id

        workloads = ControllerWorkloads(
            settings=Settings().model_copy(
                update={
                    "gpu_allocation_enabled": True,
                    "salad_daily_budget_usd": Decimal("100"),
                    "salad_monthly_budget_usd": Decimal("1000"),
                }
            ),
            sessions=database.sessions,
            instance_id="controller-failed-stop-test",
            salad_client=cast(SaladClient, client),
            object_store=None,
        )

        assert await workloads.deployment_once() is True

        async with database.sessions() as session:
            deployment = await session.get(SaladDeployment, deployment_id)
            assert deployment is not None
            assert deployment.state == SaladDeploymentState.UNKNOWN
            assert deployment.provider_queue_id is None
            assert deployment.provider_container_group_id == str(GROUP_ID)
        assert client.stop_names == [group_name]
        assert client.create_calls == 0
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_outer_controller_failure_marks_billing_observation_stale(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'unknown-stale.db').as_posix()}")
    await database.create_schema()
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
                state=SaladDeploymentState.PROVISIONING,
                desired_state=DesiredDeploymentState.ACTIVE,
                is_current=True,
                max_hourly_cost_microusd=3_600_000,
                created_at=NOW,
                updated_at=NOW,
            )
            session.add(deployment)
            await session.commit()
            deployment_id = deployment.id

        workloads = ControllerWorkloads(
            settings=Settings(),
            sessions=database.sessions,
            instance_id="controller-unknown-stale-test",
            salad_client=None,
            object_store=None,
        )
        await workloads._mark_deployment_unknown(
            deployment_id,
            error_code="controller_provider_operation_timed_out",
        )

        async with database.sessions() as session:
            deployment = await session.get(SaladDeployment, deployment_id)
            assert deployment is not None
            assert deployment.state == SaladDeploymentState.UNKNOWN
            assert deployment.billing_observation_stale is True
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
            phase=ReleasePhase.READY,
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
            runtime_artifact_manifest_sha256="0" * 64,
            runtime_managed_lora_sha256s=[],
            max_hourly_cost_microusd=2_000_000,
            created_at=NOW,
            updated_at=NOW,
        )
        session.add_all([version, deployment])
        await session.flush()
        parameters = _runtime_job_parameters(1, [])
        job = GenerationJob(
            release_version_id=version.id,
            logical_key="d" * 64,
            parameters=parameters,
            parameters_sha256=canonical_sha256(parameters),
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
async def test_staged_runtime_target_keeps_durable_worker_demand_until_superseded(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'target-demand.db').as_posix()}")
    await database.create_schema()
    try:
        context = await _seed_submission_database(database)
        async with database.sessions() as session:
            attempt = await session.get(GenerationAttempt, context.attempt_id)
            event = await session.scalar(
                select(OutboxEvent).where(OutboxEvent.aggregate_id == context.attempt_id)
            )
            deployment = await session.scalar(select(SaladDeployment))
            release = await session.scalar(select(Release))
            assert attempt is not None
            assert event is not None
            assert deployment is not None
            assert release is not None
            attempt.request_metadata = {
                **attempt.request_metadata,
                "runtime_admission": {
                    "version": "v1",
                    "provider_group_version": 2,
                    "artifact_manifest_sha256": "0" * 64,
                    "rollout_id": RUNTIME_ADMISSION_ID,
                    "worker_instance_id": None,
                },
            }
            await session.flush()

            assert event.status == OutboxStatus.PENDING
            assert (
                await effective_worker_min_replicas(
                    session,
                    salad_deployment_id=deployment.id,
                    now=NOW,
                )
                == 1
            )

            event.status = OutboxStatus.PROCESSING
            event.lease_owner = "controller-target-demand-test"
            event.lease_expires_at = NOW - timedelta(seconds=1)
            await session.flush()
            assert (
                await effective_worker_min_replicas(
                    session,
                    salad_deployment_id=deployment.id,
                    now=NOW,
                )
                == 1
            )

            release.phase = ReleasePhase.CANCELLED
            await session.flush()
            assert (
                await effective_worker_min_replicas(
                    session,
                    salad_deployment_id=deployment.id,
                    now=NOW,
                )
                == 0
            )
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_budget_block_defers_without_consuming_outbox_attempt(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'budget-defer.db').as_posix()}")
    await database.create_schema()
    try:
        context = await _seed_submission_database(database)
        transition_at = datetime.now(UTC)
        async with database.sessions() as session:
            claimed = await claim_outbox_events(
                session,
                worker_id="controller-submit",
                limit=1,
                lease_seconds=60,
                topics={SALAD_JOB_SUBMIT_TOPIC},
                now=transition_at,
            )
            assert len(claimed) == 1
            job = await session.get(GenerationJob, context.job_id)
            assert job is not None
            job.state = GenerationState.RETRY_WAIT
            job.last_error_code = "provider_budget_blocked"
            await session.commit()

        workloads = ControllerWorkloads(
            settings=Settings(),
            sessions=database.sessions,
            instance_id="controller-budget-defer-test",
            salad_client=None,
            object_store=None,
        )
        retry_at = transition_at + timedelta(minutes=1)
        await workloads._transition_submit_result(
            claimed[0],
            result=SubmissionResult(
                generation_attempt_id=context.attempt_id,
                attempt_state=GenerationAttemptState.CREATED,
                generation_job_state=GenerationState.RETRY_WAIT,
                disposition=SubmissionDisposition.BUDGET_BLOCKED,
                mutation_effect=MutationEffect.DEFINITELY_NOT_STARTED,
                provider_external_id=None,
                retry_not_before=retry_at,
            ),
            worker_id="controller-submit",
        )

        async with database.sessions() as session:
            event = await session.scalar(
                select(OutboxEvent).where(OutboxEvent.aggregate_id == context.attempt_id)
            )
            attempt = await session.get(GenerationAttempt, context.attempt_id)
            assert event is not None
            assert attempt is not None
            assert event.status == OutboxStatus.PENDING
            assert event.attempts == 0
            assert event.available_at.replace(tzinfo=UTC) == retry_at
            assert event.last_error_code == "provider_budget_blocked"
            assert attempt.state == GenerationAttemptState.CREATED
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_budget_preflight_blocks_runtime_then_reopens_the_cold_queue_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'budget-runtime-fence.db').as_posix()}")
    await database.create_schema()
    try:
        context = await _seed_submission_database(database)
        transition_at = datetime.now(UTC)
        async with database.sessions() as session:
            guard = await session.scalar(select(ProviderBudgetGuard))
            assert guard is not None
            session.add(
                ProviderSpendEntry(
                    budget_guard_id=guard.id,
                    dedupe_key="blocked-before-runtime-refresh",
                    entry_type=SpendEntryType.USAGE,
                    # The guard is OPEN at $99 committed, but this deployment's
                    # $2 reservation would cross the $100 daily ceiling.
                    amount_microusd=99_000_000,
                    effective_at=transition_at,
                    source_reference="test",
                    detail={},
                    created_at=transition_at,
                )
            )
            await session.commit()

        async with database.sessions() as session:
            claimed = await claim_outbox_events(
                session,
                worker_id="controller-submit",
                limit=1,
                lease_seconds=60,
                topics={SALAD_JOB_SUBMIT_TOPIC},
                now=transition_at,
            )
            assert len(claimed) == 1

        async def forbidden(*args: object, **kwargs: object) -> object:
            del args, kwargs
            raise AssertionError("a blocked budget must prevent every provider runtime mutation")

        monkeypatch.setattr(ControllerWorkloads, "_effective_artifact_manifest", forbidden)
        monkeypatch.setattr(controller_runtime, "refresh_container_group_runtime", forbidden)
        monkeypatch.setattr(
            controller_runtime,
            "ensure_container_group_queue_admission",
            forbidden,
        )
        monkeypatch.setattr(controller_runtime, "submit_prepared_attempt", forbidden)
        workloads = ControllerWorkloads(
            settings=Settings(
                worker_signing_key_id="worker-key-1",
                worker_signing_private_key=WORKER_SIGNING_PRIVATE_KEY,
            ),
            sessions=database.sessions,
            instance_id="controller-budget-runtime-fence-test",
            salad_client=cast(SaladClient, object()),
            object_store=cast(ObjectStore, object()),
        )

        result = await workloads._submit_event(claimed[0], progress=_SubmissionProgress())
        assert result.disposition == SubmissionDisposition.BUDGET_BLOCKED
        assert result.mutation_effect == MutationEffect.DEFINITELY_NOT_STARTED
        await workloads._transition_submit_result(
            claimed[0],
            result=result,
            worker_id="controller-submit",
        )

        async with database.sessions() as session:
            event = await session.get(OutboxEvent, claimed[0].id)
            attempt = await session.get(GenerationAttempt, context.attempt_id)
            job = await session.get(GenerationJob, context.job_id)
            assert event is not None
            assert attempt is not None
            assert job is not None
            assert event.status == OutboxStatus.PENDING
            assert event.attempts == 0
            assert event.last_error_code == "provider_budget_blocked"
            assert attempt.state == GenerationAttemptState.CREATED
            assert attempt.submit_started_at is None
            assert attempt.provider_external_id is None
            assert job.state == GenerationState.CLAIMED
            guard = await session.scalar(select(ProviderBudgetGuard))
            assert guard is not None
            assert guard.state == BudgetState.BLOCKED
            retry_at = event.available_at.replace(tzinfo=UTC)
            await session.execute(delete(ProviderSpendEntry))
            await session.commit()

        admissions: list[tuple[UUID, int]] = []
        submissions: list[UUID] = []

        async def cold_refresh(
            deployment: SaladDeployment,
            client: object,
            resolver: object,
            *,
            environment_overrides: Mapping[str, str] | None = None,
            **runtime_refresh: object,
        ) -> SaladContainerGroup:
            del client, resolver, environment_overrides, runtime_refresh
            group = _group(deployment.container_group_name, deployment.queue_name)
            return replace(
                group,
                current_state=replace(
                    group.current_state,
                    status="stopped",
                    description="stopped",
                    running_count=0,
                    start_time=None,
                    finish_time=transition_at,
                ),
            )

        async def cold_admission(
            deployment: SaladDeployment,
            client: object,
            *,
            effective_min_replicas: int,
            **runtime_identity: object,
        ) -> SaladContainerGroup:
            del client, runtime_identity
            admissions.append((deployment.id, effective_min_replicas))
            return _group(deployment.container_group_name, deployment.queue_name)

        async def successful_submit(
            *args: object,
            generation_attempt_id: UUID,
            **kwargs: object,
        ) -> SubmissionResult:
            del args, kwargs
            submissions.append(generation_attempt_id)
            return SubmissionResult(
                generation_attempt_id=generation_attempt_id,
                attempt_state=GenerationAttemptState.CREATED,
                generation_job_state=GenerationState.CLAIMED,
                disposition=SubmissionDisposition.SUBMITTED,
                mutation_effect=MutationEffect.CONFIRMED,
                provider_external_id="provider-job",
            )

        monkeypatch.setattr(
            ControllerWorkloads,
            "_effective_artifact_manifest",
            _empty_effective_artifact_manifest,
        )
        monkeypatch.setattr(controller_runtime, "refresh_container_group_runtime", cold_refresh)
        monkeypatch.setattr(
            controller_runtime,
            "ensure_container_group_queue_admission",
            cold_admission,
        )
        monkeypatch.setattr(controller_runtime, "submit_prepared_attempt", successful_submit)
        async with database.sessions() as session:
            reclaimed = await claim_outbox_events(
                session,
                worker_id="controller-submit",
                limit=1,
                lease_seconds=60,
                topics={SALAD_JOB_SUBMIT_TOPIC},
                now=retry_at + timedelta(seconds=1),
            )
            assert len(reclaimed) == 1

        with pytest.raises(controller_runtime._RuntimeAdmissionPendingError):
            await workloads._submit_event(reclaimed[0], progress=_SubmissionProgress())
        with pytest.raises(controller_runtime._RuntimeAdmissionPendingError):
            await workloads._submit_event(reclaimed[0], progress=_SubmissionProgress())
        assert admissions == []
        assert submissions == []
        monkeypatch.setattr(
            controller_runtime,
            "container_group_runtime_admission_ready",
            _runtime_admission_ready,
        )
        with pytest.raises(controller_runtime._RuntimeAdmissionPendingError):
            await workloads._submit_event(reclaimed[0], progress=_SubmissionProgress())
        resumed = await workloads._submit_event(reclaimed[0], progress=_SubmissionProgress())
        assert resumed.disposition == SubmissionDisposition.SUBMITTED
        assert admissions and all(effective_min == 1 for _, effective_min in admissions)
        assert submissions == [context.attempt_id]
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_stale_created_attempt_is_fenced_before_runtime_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'stale-submit.db').as_posix()}")
    await database.create_schema()
    try:
        context = await _seed_submission_database(database)
        async with database.sessions() as session:
            job = await session.get(GenerationJob, context.job_id)
            assert job is not None
            job.state = GenerationState.CANCELLED
            await session.commit()

        async def forbidden_manifest(*args: object, **kwargs: object) -> EffectiveArtifactManifest:
            del args, kwargs
            raise AssertionError("stale work must be fenced before a runtime refresh")

        monkeypatch.setattr(
            ControllerWorkloads,
            "_effective_artifact_manifest",
            forbidden_manifest,
        )
        workloads = ControllerWorkloads(
            settings=Settings(
                worker_signing_key_id="worker-key-1",
                worker_signing_private_key=WORKER_SIGNING_PRIVATE_KEY,
            ),
            sessions=database.sessions,
            instance_id="controller-stale-submit-test",
            salad_client=cast(SaladClient, object()),
            object_store=cast(ObjectStore, object()),
        )
        event = ClaimedOutboxEvent(
            id=UUID("a26536aa-b97e-4ad6-b019-d712e15a860b"),
            topic=SALAD_JOB_SUBMIT_TOPIC,
            dedupe_key=f"{SALAD_JOB_SUBMIT_TOPIC}:{context.attempt_id}",
            correlation_id=str(context.attempt_id),
            aggregate_type=GENERATION_ATTEMPT_AGGREGATE,
            aggregate_id=context.attempt_id,
            payload={"generation_attempt_id": str(context.attempt_id)},
            attempt=1,
            max_attempts=3,
            lease_expires_at=NOW + timedelta(minutes=5),
        )

        result = await workloads._submit_event(event, progress=_SubmissionProgress())

        assert result.disposition == SubmissionDisposition.CANCELLED
        assert result.mutation_effect == MutationEffect.DEFINITELY_NOT_STARTED
        async with database.sessions() as session:
            attempt = await session.get(GenerationAttempt, context.attempt_id)
            job = await session.get(GenerationJob, context.job_id)
            assert attempt is not None
            assert job is not None
            assert attempt.state == GenerationAttemptState.FAILED
            assert job.state == GenerationState.CANCELLED
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_terminal_markerless_submitting_orphan_is_fenced_before_runtime_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'stale-submitting.db').as_posix()}")
    await database.create_schema()
    try:
        context = await _seed_submission_database(database)
        async with database.sessions() as session:
            job = await session.get(GenerationJob, context.job_id)
            attempt = await session.get(GenerationAttempt, context.attempt_id)
            assert job is not None
            assert attempt is not None
            job.state = GenerationState.CANCELLED
            attempt.state = GenerationAttemptState.SUBMITTING
            await session.commit()

        async def forbidden_manifest(*args: object, **kwargs: object) -> EffectiveArtifactManifest:
            del args, kwargs
            raise AssertionError("a local orphan must be fenced before runtime work")

        monkeypatch.setattr(
            ControllerWorkloads,
            "_effective_artifact_manifest",
            forbidden_manifest,
        )
        workloads = ControllerWorkloads(
            settings=Settings(
                worker_signing_key_id="worker-key-1",
                worker_signing_private_key=WORKER_SIGNING_PRIVATE_KEY,
            ),
            sessions=database.sessions,
            instance_id="controller-stale-submitting-test",
            salad_client=cast(SaladClient, object()),
            object_store=cast(ObjectStore, object()),
        )
        event = ClaimedOutboxEvent(
            id=UUID("c72554ea-399e-4795-bf96-c2991e009a9a"),
            topic=SALAD_JOB_SUBMIT_TOPIC,
            dedupe_key=f"{SALAD_JOB_SUBMIT_TOPIC}:{context.attempt_id}",
            correlation_id=str(context.attempt_id),
            aggregate_type=GENERATION_ATTEMPT_AGGREGATE,
            aggregate_id=context.attempt_id,
            payload={"generation_attempt_id": str(context.attempt_id)},
            attempt=1,
            max_attempts=3,
            lease_expires_at=NOW + timedelta(minutes=5),
        )

        result = await workloads._submit_event(event, progress=_SubmissionProgress())

        assert result.disposition == SubmissionDisposition.CANCELLED
        assert result.mutation_effect == MutationEffect.DEFINITELY_NOT_STARTED
        async with database.sessions() as session:
            attempt = await session.get(GenerationAttempt, context.attempt_id)
            job = await session.get(GenerationJob, context.job_id)
            assert attempt is not None
            assert job is not None
            assert attempt.state == GenerationAttemptState.FAILED
            assert attempt.unknown_since is None
            assert job.state == GenerationState.CANCELLED
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_submit_borrows_exact_active_runtime_without_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'runtime-refresh.db').as_posix()}")
    await database.create_schema()
    try:
        first = await _seed_submission_database(database)
        async with database.sessions() as session:
            deployment = await session.scalar(select(SaladDeployment))
            first_job = await session.get(GenerationJob, first.job_id)
            first_attempt = await session.get(GenerationAttempt, first.attempt_id)
            assert deployment is not None
            assert first_job is not None
            assert first_attempt is not None

            second_parameters = _runtime_job_parameters(2, [])
            second_job = GenerationJob(
                release_version_id=first_job.release_version_id,
                logical_key="e" * 64,
                parameters=second_parameters,
                parameters_sha256=canonical_sha256(second_parameters),
                provider="salad",
                state=GenerationState.QUEUED,
                expected_output_count=1,
            )
            session.add(second_job)
            await session.flush()
            second = await prepare_generation_attempt(
                session,
                generation_job_id=second_job.id,
                salad_deployment_id=deployment.id,
                idempotency_key="runtime-refresh-second-attempt",
                now=NOW,
            )
            first_job.state = GenerationState.SUBMITTING
            first_attempt.state = GenerationAttemptState.SUBMITTING
            first_attempt.request_metadata = {
                **first_attempt.request_metadata,
                "runtime_admission": {
                    "version": "v1",
                    "provider_group_version": 1,
                    "artifact_manifest_sha256": "0" * 64,
                    "rollout_id": RUNTIME_ADMISSION_ID,
                    "worker_instance_id": "instance-creator-1",
                },
            }
            await session.commit()

        refreshes: list[UUID] = []
        submissions: list[UUID] = []

        async def fake_refresh(
            deployment: SaladDeployment,
            client: object,
            resolver: object,
            *,
            environment_overrides: Mapping[str, str] | None = None,
            **runtime_refresh: object,
        ) -> SaladContainerGroup:
            del client, resolver, environment_overrides, runtime_refresh
            refreshes.append(deployment.id)
            return _group(deployment.container_group_name, deployment.queue_name)

        async def fake_submit(
            *args: object,
            generation_attempt_id: UUID,
            **kwargs: object,
        ) -> SubmissionResult:
            del args, kwargs
            submissions.append(generation_attempt_id)
            return SubmissionResult(
                generation_attempt_id=generation_attempt_id,
                attempt_state=GenerationAttemptState.CREATED,
                generation_job_state=GenerationState.CLAIMED,
                disposition=SubmissionDisposition.SUBMITTED,
                mutation_effect=MutationEffect.CONFIRMED,
                provider_external_id="provider-job",
            )

        async def fake_admission(
            deployment: SaladDeployment,
            client: object,
            *,
            effective_min_replicas: int,
            **runtime_identity: object,
        ) -> SaladContainerGroup:
            del client, runtime_identity
            assert effective_min_replicas == 1
            return _group(deployment.container_group_name, deployment.queue_name)

        monkeypatch.setattr(
            controller_runtime,
            "refresh_container_group_runtime",
            fake_refresh,
        )
        monkeypatch.setattr(
            controller_runtime,
            "ensure_container_group_queue_admission",
            fake_admission,
        )
        monkeypatch.setattr(
            ControllerWorkloads,
            "_effective_artifact_manifest",
            _empty_effective_artifact_manifest,
        )
        monkeypatch.setattr(
            controller_runtime,
            "container_group_runtime_admission_ready",
            _runtime_admission_ready,
        )
        monkeypatch.setattr(controller_runtime, "submit_prepared_attempt", fake_submit)
        workloads = ControllerWorkloads(
            settings=Settings(
                worker_signing_key_id="worker-key-1",
                worker_signing_private_key=WORKER_SIGNING_PRIVATE_KEY,
                salad_worker_model_manifest_json=RUNTIME_MANIFEST,
                salad_worker_model_manifest_sha256="0" * 64,
            ),
            sessions=database.sessions,
            instance_id="controller-runtime-refresh-test",
            salad_client=cast(SaladClient, object()),
            object_store=cast(ObjectStore, object()),
        )
        event = ClaimedOutboxEvent(
            id=second.outbox_event_id,
            topic=SALAD_JOB_SUBMIT_TOPIC,
            dedupe_key=f"{SALAD_JOB_SUBMIT_TOPIC}:{second.generation_attempt_id}",
            correlation_id=str(second.generation_attempt_id),
            aggregate_type=GENERATION_ATTEMPT_AGGREGATE,
            aggregate_id=second.generation_attempt_id,
            payload={"generation_attempt_id": str(second.generation_attempt_id)},
            attempt=1,
            max_attempts=3,
            lease_expires_at=NOW + timedelta(minutes=5),
        )

        # A durable provider attempt on this deployment means the model-bearing
        # worker is already warming/running; changing its environment here would
        # create a new Salad version and restart it between ordered batches.
        with pytest.raises(controller_runtime._RuntimeAdmissionPendingError):
            await workloads._submit_event(event, progress=_SubmissionProgress())
        await workloads._submit_event(event, progress=_SubmissionProgress())
        assert refreshes == []
        assert submissions == [second.generation_attempt_id]

        async with database.sessions() as session:
            first_job = await session.get(GenerationJob, first.job_id)
            first_attempt = await session.get(GenerationAttempt, first.attempt_id)
            assert first_job is not None
            assert first_attempt is not None
            # Simulate the legacy orphan that caused a completed/cancelled set to
            # pin runtime refreshes: SUBMITTING was recorded without any durable
            # reservation/submission marker, then the parent became terminal.
            first_job.state = GenerationState.CANCELLED
            await session.commit()

        # This exact attempt retains its signed rollout/instance provenance even
        # after the sibling becomes terminal; it must not repatch on replay.
        await workloads._submit_event(event, progress=_SubmissionProgress())
        assert refreshes == []
        assert submissions == [
            second.generation_attempt_id,
            second.generation_attempt_id,
        ]
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_three_inflight_attempts_share_one_exact_runtime_admission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(
        f"sqlite+aiosqlite:///{(tmp_path / 'three-inflight-runtime.db').as_posix()}"
    )
    await database.create_schema()
    try:
        first = await _seed_submission_database(database)
        async with database.sessions() as session:
            deployment = await session.scalar(select(SaladDeployment))
            first_job = await session.get(GenerationJob, first.job_id)
            assert deployment is not None
            assert first_job is not None
            prepared = []
            for index in (2, 3):
                parameters = _runtime_job_parameters(index, [])
                job = GenerationJob(
                    release_version_id=first_job.release_version_id,
                    logical_key=str(index) * 64,
                    parameters=parameters,
                    parameters_sha256=canonical_sha256(parameters),
                    provider="salad",
                    state=GenerationState.QUEUED,
                    expected_output_count=1,
                )
                session.add(job)
                await session.flush()
                prepared.append(
                    await prepare_generation_attempt(
                        session,
                        generation_job_id=job.id,
                        salad_deployment_id=deployment.id,
                        idempotency_key=f"three-inflight-{index}",
                        now=NOW,
                    )
                )
            await session.commit()
            deployment_id = deployment.id

        refreshes: list[UUID] = []
        submissions: list[UUID] = []
        readiness_checks: list[tuple[int, str]] = []
        admissions: list[tuple[str, str]] = []

        async def fake_refresh(
            deployment: SaladDeployment,
            client: object,
            resolver: object,
            *,
            environment_overrides: Mapping[str, str] | None = None,
            **runtime_refresh: object,
        ) -> SaladContainerGroup:
            del client, resolver, environment_overrides, runtime_refresh
            refreshes.append(deployment.id)
            return replace(
                _group(deployment.container_group_name, deployment.queue_name),
                version=6,
            )

        async def fake_admission(
            deployment: SaladDeployment,
            client: object,
            *,
            effective_min_replicas: int,
            artifact_manifest_sha256: str | None = None,
            runtime_admission_id: str | None = None,
        ) -> SaladContainerGroup:
            del client
            assert effective_min_replicas == 1
            assert artifact_manifest_sha256 == "0" * 64
            assert runtime_admission_id is not None
            admissions.append((artifact_manifest_sha256, runtime_admission_id))
            return replace(
                _group(deployment.container_group_name, deployment.queue_name),
                version=6,
            )

        async def runtime_ready(
            deployment: SaladDeployment,
            client: object,
            *,
            provider_version: int,
            artifact_manifest_sha256: str,
            runtime_admission_id: str,
        ) -> str:
            del deployment, client
            assert len(runtime_admission_id) == 32
            readiness_checks.append((provider_version, artifact_manifest_sha256))
            return "instance-creator-6"

        async def fake_submit(
            session: AsyncSession,
            *args: object,
            generation_attempt_id: UUID,
            **kwargs: object,
        ) -> SubmissionResult:
            del args, kwargs
            # Production commits the adopted target with the durable
            # SUBMITTING marker before its provider POST.
            await session.commit()
            submissions.append(generation_attempt_id)
            return SubmissionResult(
                generation_attempt_id=generation_attempt_id,
                attempt_state=GenerationAttemptState.CREATED,
                generation_job_state=GenerationState.CLAIMED,
                disposition=SubmissionDisposition.SUBMITTED,
                mutation_effect=MutationEffect.CONFIRMED,
                provider_external_id="provider-job",
            )

        monkeypatch.setattr(
            ControllerWorkloads,
            "_effective_artifact_manifest",
            _empty_effective_artifact_manifest,
        )
        monkeypatch.setattr(controller_runtime, "refresh_container_group_runtime", fake_refresh)
        monkeypatch.setattr(
            controller_runtime,
            "ensure_container_group_queue_admission",
            fake_admission,
        )
        monkeypatch.setattr(
            controller_runtime,
            "container_group_runtime_admission_ready",
            runtime_ready,
        )
        monkeypatch.setattr(controller_runtime, "submit_prepared_attempt", fake_submit)
        workloads = ControllerWorkloads(
            settings=Settings(
                worker_signing_key_id="worker-key-1",
                worker_signing_private_key=WORKER_SIGNING_PRIVATE_KEY,
            ),
            sessions=database.sessions,
            instance_id="controller-three-inflight-test",
            salad_client=cast(SaladClient, object()),
            object_store=cast(ObjectStore, object()),
        )

        attempt_ids = [
            first.attempt_id,
            prepared[0].generation_attempt_id,
            prepared[1].generation_attempt_id,
        ]
        outbox_ids = []
        async with database.sessions() as session:
            for attempt_id in attempt_ids:
                outbox = await session.scalar(
                    select(OutboxEvent).where(OutboxEvent.aggregate_id == attempt_id)
                )
                assert outbox is not None
                outbox_ids.append(outbox.id)

        def claimed_event(index: int) -> ClaimedOutboxEvent:
            attempt_id = attempt_ids[index]
            return ClaimedOutboxEvent(
                id=outbox_ids[index],
                topic=SALAD_JOB_SUBMIT_TOPIC,
                dedupe_key=f"{SALAD_JOB_SUBMIT_TOPIC}:{attempt_id}",
                correlation_id=str(attempt_id),
                aggregate_type=GENERATION_ATTEMPT_AGGREGATE,
                aggregate_id=attempt_id,
                payload={"generation_attempt_id": str(attempt_id)},
                attempt=1,
                max_attempts=10,
                lease_expires_at=NOW + timedelta(minutes=5),
            )

        # The first attempt creates exactly one runtime version and stages v6.
        with pytest.raises(controller_runtime._RuntimeAdmissionPendingError):
            await workloads._submit_event(claimed_event(0), progress=_SubmissionProgress())
        with pytest.raises(controller_runtime._RuntimeAdmissionPendingError):
            await workloads._submit_event(claimed_event(0), progress=_SubmissionProgress())
        with pytest.raises(controller_runtime._RuntimeAdmissionPendingError):
            await workloads._submit_event(claimed_event(0), progress=_SubmissionProgress())
        await workloads._submit_event(claimed_event(0), progress=_SubmissionProgress())
        assert refreshes == [deployment_id]

        # Keep each submitted attempt active while the next one is admitted.
        for index in (0, 1):
            async with database.sessions() as session:
                attempt = await session.get(GenerationAttempt, attempt_ids[index])
                job = await session.get(
                    GenerationJob,
                    first.job_id if index == 0 else prepared[0].generation_job_id,
                )
                assert attempt is not None
                assert job is not None
                attempt.state = GenerationAttemptState.RUNNING
                attempt.provider_external_id = f"provider-job-{index}"
                job.state = GenerationState.RUNNING
                await session.commit()
            with pytest.raises(controller_runtime._RuntimeAdmissionPendingError):
                await workloads._submit_event(
                    claimed_event(index + 1),
                    progress=_SubmissionProgress(),
                )
            await workloads._submit_event(claimed_event(index + 1), progress=_SubmissionProgress())

        assert refreshes == [deployment_id]
        assert submissions == attempt_ids
        assert readiness_checks == [(6, "0" * 64)] * 4
        assert len(admissions) == 4
        assert len({rollout_id for _, rollout_id in admissions}) == 1
        async with database.sessions() as session:
            attempts = tuple(
                (
                    await session.scalars(
                        select(GenerationAttempt).where(GenerationAttempt.id.in_(attempt_ids))
                    )
                ).all()
            )
            assert len(attempts) == 3
            assert {
                (
                    attempt.request_metadata["runtime_admission"]["provider_group_version"],
                    attempt.request_metadata["runtime_admission"]["artifact_manifest_sha256"],
                    attempt.request_metadata["runtime_admission"]["worker_instance_id"],
                )
                for attempt in attempts
            } == {(6, "0" * 64, "instance-creator-6")}
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_cold_submit_uses_claimed_outbox_demand_before_queue_admission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'cold-admission.db').as_posix()}")
    await database.create_schema()
    try:
        context = await _seed_submission_database(database)
        async with database.sessions() as session:
            attempt = await session.get(GenerationAttempt, context.attempt_id)
            event = await session.scalar(
                select(OutboxEvent).where(OutboxEvent.aggregate_id == context.attempt_id)
            )
            release = await session.scalar(select(Release))
            assert attempt is not None
            assert event is not None
            assert release is not None
            release.phase = ReleasePhase.GENERATING
            event.status = OutboxStatus.PROCESSING
            event.attempts = 1
            event.lease_owner = "controller-submit"
            event.lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)
            await session.commit()
            outbox_id = event.id

        refreshes: list[UUID] = []
        admissions: list[tuple[UUID, int]] = []
        readiness_checks: list[tuple[int, str]] = []
        submissions: list[UUID] = []

        async def fake_refresh(
            deployment: SaladDeployment,
            client: object,
            resolver: object,
            *,
            environment_overrides: Mapping[str, str] | None = None,
            **runtime_refresh: object,
        ) -> SaladContainerGroup:
            del client, resolver, environment_overrides, runtime_refresh
            refreshes.append(deployment.id)
            group = _group(deployment.container_group_name, deployment.queue_name)
            return replace(
                group,
                version=7,
                current_state=replace(
                    group.current_state,
                    status="stopped",
                    description="stopped",
                    running_count=0,
                    start_time=None,
                    finish_time=NOW,
                ),
            )

        async def fake_admission(
            deployment: SaladDeployment,
            client: object,
            *,
            effective_min_replicas: int,
            **runtime_identity: object,
        ) -> SaladContainerGroup:
            del client, runtime_identity
            admissions.append((deployment.id, effective_min_replicas))
            group = _group(deployment.container_group_name, deployment.queue_name)
            # A provider-accepted start can still read back as stopped until
            # the cold allocation appears. The durable queue POST must not
            # wait for image download or worker attachment.
            return replace(
                group,
                # Queue-admission autoscaler convergence may advance the
                # provider version before start is accepted. Persist this
                # final exact readback, not the preceding runtime-PATCH version.
                version=8,
                current_state=replace(
                    group.current_state,
                    status="stopped",
                    description="stopped",
                    running_count=0,
                    start_time=None,
                    finish_time=NOW,
                ),
            )

        async def fake_submit(
            *args: object,
            generation_attempt_id: UUID,
            **kwargs: object,
        ) -> SubmissionResult:
            del args, kwargs
            submissions.append(generation_attempt_id)
            return SubmissionResult(
                generation_attempt_id=generation_attempt_id,
                attempt_state=GenerationAttemptState.CREATED,
                generation_job_state=GenerationState.CLAIMED,
                disposition=SubmissionDisposition.SUBMITTED,
                mutation_effect=MutationEffect.CONFIRMED,
                provider_external_id="provider-job",
            )

        async def exact_runtime_ready(
            deployment: SaladDeployment,
            client: object,
            *,
            provider_version: int,
            artifact_manifest_sha256: str,
            runtime_admission_id: str,
        ) -> str:
            del deployment, client
            assert len(runtime_admission_id) == 32
            readiness_checks.append((provider_version, artifact_manifest_sha256))
            return "instance-creator-8"

        monkeypatch.setattr(
            controller_runtime,
            "refresh_container_group_runtime",
            fake_refresh,
        )
        monkeypatch.setattr(
            controller_runtime,
            "ensure_container_group_queue_admission",
            fake_admission,
        )
        monkeypatch.setattr(
            ControllerWorkloads,
            "_effective_artifact_manifest",
            _empty_effective_artifact_manifest,
        )
        monkeypatch.setattr(controller_runtime, "submit_prepared_attempt", fake_submit)
        workloads = ControllerWorkloads(
            settings=Settings(
                worker_signing_key_id="worker-key-1",
                worker_signing_private_key=WORKER_SIGNING_PRIVATE_KEY,
            ),
            sessions=database.sessions,
            instance_id="controller-cold-admission-test",
            salad_client=cast(SaladClient, object()),
            object_store=cast(ObjectStore, object()),
        )
        claimed = ClaimedOutboxEvent(
            id=outbox_id,
            topic=SALAD_JOB_SUBMIT_TOPIC,
            dedupe_key=f"{SALAD_JOB_SUBMIT_TOPIC}:{context.attempt_id}",
            correlation_id=str(context.attempt_id),
            aggregate_type=GENERATION_ATTEMPT_AGGREGATE,
            aggregate_id=context.attempt_id,
            payload={"generation_attempt_id": str(context.attempt_id)},
            attempt=1,
            max_attempts=10,
            lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )

        with pytest.raises(controller_runtime._RuntimeAdmissionPendingError):
            await workloads._submit_event(claimed, progress=_SubmissionProgress())
        with pytest.raises(controller_runtime._RuntimeAdmissionPendingError):
            await workloads._submit_event(claimed, progress=_SubmissionProgress())
        with pytest.raises(controller_runtime._RuntimeAdmissionPendingError):
            await workloads._submit_event(claimed, progress=_SubmissionProgress())

        assert len(admissions) == 1
        assert admissions[0][1] == 1
        assert refreshes
        assert submissions == []
        async with database.sessions() as session:
            attempt = await session.get(GenerationAttempt, context.attempt_id)
            assert attempt is not None
            assert attempt.request_metadata["runtime_admission"] == {
                "version": "v1",
                "provider_group_version": 8,
                "artifact_manifest_sha256": "0" * 64,
                "rollout_id": attempt.request_metadata["runtime_admission"]["rollout_id"],
                "worker_instance_id": None,
            }
        monkeypatch.setattr(
            controller_runtime,
            "container_group_runtime_admission_ready",
            exact_runtime_ready,
        )
        with pytest.raises(controller_runtime._RuntimeAdmissionPendingError):
            await workloads._submit_event(claimed, progress=_SubmissionProgress())
        await workloads._submit_event(claimed, progress=_SubmissionProgress())
        assert refreshes == [admissions[0][0]]
        assert admissions == [(admissions[0][0], 1)] * 3
        assert readiness_checks == [(8, "0" * 64)] * 2
        assert submissions == [context.attempt_id]
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_runtime_rollout_is_staged_until_the_old_worker_is_gone_and_shared(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'staged-runtime.db').as_posix()}")
    await database.create_schema()
    try:
        first = await _seed_submission_database(database)
        lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)
        async with database.sessions() as session:
            deployment = await session.scalar(select(SaladDeployment))
            first_job = await session.get(GenerationJob, first.job_id)
            first_outbox = await session.scalar(
                select(OutboxEvent).where(OutboxEvent.aggregate_id == first.attempt_id)
            )
            release = await session.scalar(select(Release))
            assert deployment is not None
            assert first_job is not None
            assert first_outbox is not None
            assert release is not None
            release.phase = ReleasePhase.GENERATING
            first_outbox.status = OutboxStatus.PROCESSING
            first_outbox.attempts = 1
            first_outbox.lease_owner = "controller-submit-first"
            first_outbox.lease_expires_at = lease_expires_at

            second_parameters = _runtime_job_parameters(2, [])
            second_job = GenerationJob(
                release_version_id=first_job.release_version_id,
                logical_key="f" * 64,
                parameters=second_parameters,
                parameters_sha256=canonical_sha256(second_parameters),
                provider="salad",
                state=GenerationState.QUEUED,
                expected_output_count=1,
            )
            session.add(second_job)
            await session.flush()
            second = await prepare_generation_attempt(
                session,
                generation_job_id=second_job.id,
                salad_deployment_id=deployment.id,
                idempotency_key="staged-runtime-second",
                now=NOW,
            )
            second_outbox = await session.get(OutboxEvent, second.outbox_event_id)
            assert second_outbox is not None
            second_outbox.status = OutboxStatus.PROCESSING
            second_outbox.attempts = 1
            second_outbox.lease_owner = "controller-submit-second"
            second_outbox.lease_expires_at = lease_expires_at
            await session.commit()
            deployment_id = deployment.id

        refreshes: list[UUID] = []
        readiness_checks: list[tuple[int, str]] = []
        submissions: list[UUID] = []
        exact_worker_ready = False

        async def fake_refresh(
            deployment: SaladDeployment,
            client: object,
            resolver: object,
            *,
            environment_overrides: Mapping[str, str] | None = None,
            **runtime_refresh: object,
        ) -> SaladContainerGroup:
            del client, resolver, environment_overrides, runtime_refresh
            refreshes.append(deployment.id)
            return replace(
                _group(deployment.container_group_name, deployment.queue_name),
                version=2,
            )

        async def runtime_ready(
            deployment: SaladDeployment,
            client: object,
            *,
            provider_version: int,
            artifact_manifest_sha256: str,
            runtime_admission_id: str,
        ) -> str | None:
            del deployment, client
            assert len(runtime_admission_id) == 32
            readiness_checks.append((provider_version, artifact_manifest_sha256))
            return "instance-creator-2" if exact_worker_ready else None

        async def fake_admission(
            deployment: SaladDeployment,
            client: object,
            *,
            effective_min_replicas: int,
            **runtime_identity: object,
        ) -> SaladContainerGroup:
            del client, runtime_identity
            assert effective_min_replicas == 1
            return replace(
                _group(deployment.container_group_name, deployment.queue_name),
                version=2,
            )

        async def fake_submit(
            *args: object,
            generation_attempt_id: UUID,
            **kwargs: object,
        ) -> SubmissionResult:
            del args, kwargs
            submissions.append(generation_attempt_id)
            return SubmissionResult(
                generation_attempt_id=generation_attempt_id,
                attempt_state=GenerationAttemptState.CREATED,
                generation_job_state=GenerationState.CLAIMED,
                disposition=SubmissionDisposition.SUBMITTED,
                mutation_effect=MutationEffect.CONFIRMED,
                provider_external_id="provider-job",
            )

        monkeypatch.setattr(
            ControllerWorkloads,
            "_effective_artifact_manifest",
            _empty_effective_artifact_manifest,
        )
        monkeypatch.setattr(controller_runtime, "refresh_container_group_runtime", fake_refresh)
        monkeypatch.setattr(
            controller_runtime,
            "container_group_runtime_admission_ready",
            runtime_ready,
        )
        monkeypatch.setattr(
            controller_runtime,
            "ensure_container_group_queue_admission",
            fake_admission,
        )
        monkeypatch.setattr(controller_runtime, "submit_prepared_attempt", fake_submit)
        workloads = ControllerWorkloads(
            settings=Settings(
                worker_signing_key_id="worker-key-1",
                worker_signing_private_key=WORKER_SIGNING_PRIVATE_KEY,
            ),
            sessions=database.sessions,
            instance_id="controller-staged-runtime-test",
            salad_client=cast(SaladClient, object()),
            object_store=cast(ObjectStore, object()),
        )
        first_event = ClaimedOutboxEvent(
            id=first_outbox.id,
            topic=SALAD_JOB_SUBMIT_TOPIC,
            dedupe_key=first_outbox.dedupe_key,
            correlation_id=first_outbox.correlation_id,
            aggregate_type=GENERATION_ATTEMPT_AGGREGATE,
            aggregate_id=first.attempt_id,
            payload=dict(first_outbox.payload),
            attempt=1,
            max_attempts=first_outbox.max_attempts,
            lease_expires_at=lease_expires_at,
        )
        second_event = ClaimedOutboxEvent(
            id=second.outbox_event_id,
            topic=SALAD_JOB_SUBMIT_TOPIC,
            dedupe_key=second_outbox.dedupe_key,
            correlation_id=second_outbox.correlation_id,
            aggregate_type=GENERATION_ATTEMPT_AGGREGATE,
            aggregate_id=second.generation_attempt_id,
            payload=dict(second_outbox.payload),
            attempt=1,
            max_attempts=second_outbox.max_attempts,
            lease_expires_at=lease_expires_at,
        )

        # Claim one persists the PATCH plan; claim two applies it and durably
        # stages v2 before any queue admission or provider job POST.
        with pytest.raises(controller_runtime._RuntimeAdmissionPendingError):
            await workloads._submit_event(first_event, progress=_SubmissionProgress())
        assert refreshes == []
        with pytest.raises(controller_runtime._RuntimeAdmissionPendingError):
            await workloads._submit_event(first_event, progress=_SubmissionProgress())
        assert refreshes == [deployment_id]
        assert readiness_checks == []
        assert submissions == []

        # A controller can die after staging but before releasing its outbox
        # lease. Until recovery settles that still-live aggregate, an expired
        # PROCESSING row must continue fencing the exact target.
        async with database.sessions() as session:
            staged_outbox = await session.get(OutboxEvent, first_outbox.id)
            assert staged_outbox is not None
            staged_outbox.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()

        # A concurrent CREATED attempt first commits its own copy of the exact
        # target. While an old consumer remains observable, it neither PATCHes
        # again nor posts.
        with pytest.raises(controller_runtime._RuntimeAdmissionPendingError):
            await workloads._submit_event(second_event, progress=_SubmissionProgress())
        assert refreshes == [deployment_id]
        assert readiness_checks == []
        with pytest.raises(controller_runtime._RuntimeAdmissionPendingError):
            await workloads._submit_event(second_event, progress=_SubmissionProgress())
        assert readiness_checks == [(2, "0" * 64)]
        assert submissions == []
        async with database.sessions() as session:
            attempts = tuple(
                (
                    await session.scalars(
                        select(GenerationAttempt).where(
                            GenerationAttempt.id.in_(
                                (first.attempt_id, second.generation_attempt_id)
                            )
                        )
                    )
                ).all()
            )
            assert len(attempts) == 2
            assert {
                attempt.request_metadata["runtime_admission"]["provider_group_version"]
                for attempt in attempts
            } == {2}

        # Once the exact target is the sole ready consumer, only this claimed
        # event is submitted and the staged rollout is never repeated.
        exact_worker_ready = True
        with pytest.raises(controller_runtime._RuntimeAdmissionPendingError):
            await workloads._submit_event(second_event, progress=_SubmissionProgress())
        result = await workloads._submit_event(second_event, progress=_SubmissionProgress())
        assert result.disposition == SubmissionDisposition.SUBMITTED
        assert refreshes == [deployment_id]
        assert readiness_checks == [(2, "0" * 64)] * 3
        assert submissions == [second.generation_attempt_id]
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_pending_runtime_target_ignores_stale_created_demand(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'stale-target.db').as_posix()}")
    await database.create_schema()
    try:
        context = await _seed_submission_database(database)
        excluded_attempt_id = UUID("991ae61a-6ead-42e9-be67-58a3d28f6b52")
        async with database.sessions() as session:
            deployment = await session.scalar(select(SaladDeployment))
            attempt = await session.get(GenerationAttempt, context.attempt_id)
            job = await session.get(GenerationJob, context.job_id)
            outbox = await session.scalar(
                select(OutboxEvent).where(OutboxEvent.aggregate_id == context.attempt_id)
            )
            assert deployment is not None
            assert attempt is not None
            assert job is not None
            assert outbox is not None
            attempt.request_metadata = {
                **attempt.request_metadata,
                "runtime_admission": {
                    "version": "v1",
                    "provider_group_version": 4,
                    "artifact_manifest_sha256": "0" * 64,
                    "rollout_id": RUNTIME_ADMISSION_ID,
                    "worker_instance_id": None,
                },
            }
            await session.commit()

            target = await controller_runtime._pending_runtime_admission_target(
                session,
                salad_deployment_id=deployment.id,
                excluding_attempt_id=excluded_attempt_id,
            )
            assert target == controller_runtime._RuntimeAdmissionTarget(
                4,
                "0" * 64,
                RUNTIME_ADMISSION_ID,
            )

            outbox.status = OutboxStatus.DEAD_LETTER
            await session.commit()
            assert (
                await controller_runtime._pending_runtime_admission_target(
                    session,
                    salad_deployment_id=deployment.id,
                    excluding_attempt_id=excluded_attempt_id,
                )
                is None
            )

            outbox.status = OutboxStatus.PENDING
            job.state = GenerationState.CANCELLED
            await session.commit()
            assert (
                await controller_runtime._pending_runtime_admission_target(
                    session,
                    salad_deployment_id=deployment.id,
                    excluding_attempt_id=excluded_attempt_id,
                )
                is None
            )
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_transient_runtime_observation_defers_without_consuming_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(
        f"sqlite+aiosqlite:///{(tmp_path / 'runtime-observation-defer.db').as_posix()}"
    )
    await database.create_schema()
    try:
        context = await _seed_submission_database(database)
        refreshes: list[UUID] = []

        async def fake_preflight(
            deployment: SaladDeployment,
            client: object,
        ) -> SaladContainerGroup:
            del client
            return _group(deployment.container_group_name, deployment.queue_name)

        async def fake_refresh(
            deployment: SaladDeployment,
            client: object,
            resolver: object,
            *,
            environment_overrides: Mapping[str, str] | None = None,
            expected_provider_version: int | None = None,
            runtime_admission_id: str | None = None,
            effective_min_replicas: int | None = None,
        ) -> SaladContainerGroup:
            del client, resolver
            assert expected_provider_version == 1
            assert runtime_admission_id is not None
            assert effective_min_replicas == 1
            assert environment_overrides is not None
            assert (
                environment_overrides[WORKER_RUNTIME_ADMISSION_ID_BINDING] == runtime_admission_id
            )
            refreshes.append(deployment.id)
            return replace(
                _group(deployment.container_group_name, deployment.queue_name),
                version=5,
            )

        async def unavailable(*args: object, **kwargs: object) -> bool:
            del args, kwargs
            raise controller_runtime.SaladRuntimeAdmissionUnavailableError(
                "provider readback unavailable"
            )

        async def fake_admission(
            deployment: SaladDeployment,
            client: object,
            *,
            effective_min_replicas: int,
            **runtime_identity: object,
        ) -> SaladContainerGroup:
            del client, runtime_identity
            assert effective_min_replicas == 1
            return replace(
                _group(deployment.container_group_name, deployment.queue_name),
                version=5,
            )

        async def forbidden_submit(*args: object, **kwargs: object) -> SubmissionResult:
            del args, kwargs
            raise AssertionError("an inconclusive runtime observation must prevent POST")

        monkeypatch.setattr(
            ControllerWorkloads,
            "_effective_artifact_manifest",
            _empty_effective_artifact_manifest,
        )
        monkeypatch.setattr(
            controller_runtime,
            "preflight_container_group_runtime_refresh",
            fake_preflight,
        )
        monkeypatch.setattr(controller_runtime, "refresh_container_group_runtime", fake_refresh)
        monkeypatch.setattr(
            controller_runtime,
            "ensure_container_group_queue_admission",
            fake_admission,
        )
        monkeypatch.setattr(controller_runtime, "submit_prepared_attempt", forbidden_submit)
        workloads = ControllerWorkloads(
            settings=Settings(
                worker_signing_key_id="worker-key-1",
                worker_signing_private_key=WORKER_SIGNING_PRIVATE_KEY,
                background_retry_delay_seconds=1,
            ).model_copy(update={"gpu_allocation_enabled": True}),
            sessions=database.sessions,
            instance_id="controller-runtime-observation-test",
            salad_client=cast(SaladClient, object()),
            object_store=cast(ObjectStore, object()),
        )

        # First claim commits the provider-version-fenced plan before any PATCH.
        assert await workloads.submit_once() is True
        async with database.sessions() as session:
            event = await session.scalar(
                select(OutboxEvent).where(OutboxEvent.aggregate_id == context.attempt_id)
            )
            attempt = await session.get(GenerationAttempt, context.attempt_id)
            assert event is not None
            assert attempt is not None
            assert event.status == OutboxStatus.PENDING
            assert event.attempts == 0
            assert attempt.state == GenerationAttemptState.CREATED
            assert "runtime_admission_refresh_plan" in attempt.request_metadata
            event.available_at = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()

        # The second claim applies/adopts that one rollout and commits its exact
        # target, again without consuming an outbox attempt or issuing a job POST.
        assert await workloads.submit_once() is True
        async with database.sessions() as session:
            event = await session.scalar(
                select(OutboxEvent).where(OutboxEvent.aggregate_id == context.attempt_id)
            )
            attempt = await session.get(GenerationAttempt, context.attempt_id)
            assert event is not None
            assert attempt is not None
            assert event.status == OutboxStatus.PENDING
            assert event.attempts == 0
            assert attempt.request_metadata["runtime_admission"] == {
                "version": "v1",
                "provider_group_version": 5,
                "artifact_manifest_sha256": "0" * 64,
                "rollout_id": attempt.request_metadata["runtime_admission"]["rollout_id"],
                "worker_instance_id": None,
            }
            assert "runtime_admission_refresh_plan" not in attempt.request_metadata
            event.available_at = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()

        monkeypatch.setattr(
            controller_runtime,
            "container_group_runtime_admission_ready",
            unavailable,
        )
        assert await workloads.submit_once() is True

        assert len(refreshes) == 1
        async with database.sessions() as session:
            event = await session.scalar(
                select(OutboxEvent).where(OutboxEvent.aggregate_id == context.attempt_id)
            )
            attempt = await session.get(GenerationAttempt, context.attempt_id)
            job = await session.get(GenerationJob, context.job_id)
            assert event is not None
            assert attempt is not None
            assert job is not None
            assert event.status == OutboxStatus.PENDING
            assert event.attempts == 0
            assert event.last_error_code == "worker_runtime_admission_pending"
            assert attempt.state == GenerationAttemptState.CREATED
            assert attempt.submit_started_at is None
            assert attempt.provider_external_id is None
            assert job.state == GenerationState.CLAIMED
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_runtime_refresh_crash_replays_the_marked_rollout_without_repatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SimulatedControllerCrash(BaseException):
        pass

    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'runtime-refresh-crash.db').as_posix()}")
    await database.create_schema()
    try:
        context = await _seed_submission_database(database)
        remote_version = 1
        remote_rollout_id: str | None = None
        patch_count = 0
        adoption_count = 0

        async def fake_preflight(
            deployment: SaladDeployment,
            client: object,
        ) -> SaladContainerGroup:
            del client
            return replace(
                _group(deployment.container_group_name, deployment.queue_name),
                version=remote_version,
            )

        async def crash_then_adopt_refresh(
            deployment: SaladDeployment,
            client: object,
            resolver: object,
            *,
            environment_overrides: Mapping[str, str] | None = None,
            expected_provider_version: int | None = None,
            runtime_admission_id: str | None = None,
            effective_min_replicas: int | None = None,
        ) -> SaladContainerGroup:
            nonlocal remote_version, remote_rollout_id, patch_count, adoption_count
            del client, resolver
            assert expected_provider_version == 1
            assert runtime_admission_id is not None
            assert effective_min_replicas == 1
            assert environment_overrides is not None
            assert (
                environment_overrides[WORKER_RUNTIME_ADMISSION_ID_BINDING] == runtime_admission_id
            )
            if remote_rollout_id is None:
                patch_count += 1
                remote_rollout_id = runtime_admission_id
                remote_version = 2
                # The provider accepted the PATCH, but the SQL transaction that
                # would replace the plan with the target never committed.
                raise SimulatedControllerCrash
            assert remote_rollout_id == runtime_admission_id
            adoption_count += 1
            return replace(
                _group(deployment.container_group_name, deployment.queue_name),
                version=remote_version,
            )

        async def forbidden_submit(*args: object, **kwargs: object) -> SubmissionResult:
            del args, kwargs
            raise AssertionError("runtime recovery must still precede the provider job POST")

        monkeypatch.setattr(
            ControllerWorkloads,
            "_effective_artifact_manifest",
            _empty_effective_artifact_manifest,
        )
        monkeypatch.setattr(
            controller_runtime,
            "preflight_container_group_runtime_refresh",
            fake_preflight,
        )
        monkeypatch.setattr(
            controller_runtime,
            "refresh_container_group_runtime",
            crash_then_adopt_refresh,
        )
        monkeypatch.setattr(controller_runtime, "submit_prepared_attempt", forbidden_submit)
        workloads = ControllerWorkloads(
            settings=Settings(
                worker_signing_key_id="worker-key-1",
                worker_signing_private_key=WORKER_SIGNING_PRIVATE_KEY,
                background_retry_delay_seconds=1,
            ).model_copy(update={"gpu_allocation_enabled": True}),
            sessions=database.sessions,
            instance_id="controller-runtime-crash-test",
            salad_client=cast(SaladClient, object()),
            object_store=cast(ObjectStore, object()),
        )

        # Claim one stores the immutable plan and releases its lease before PATCH.
        assert await workloads.submit_once() is True
        async with database.sessions() as session:
            event = await session.scalar(
                select(OutboxEvent).where(OutboxEvent.aggregate_id == context.attempt_id)
            )
            assert event is not None
            event.available_at = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()

        # Claim two dies after the external PATCH. Its SQL work rolls back, so
        # only the precommitted plan remains and the outbox lease expires.
        with pytest.raises(SimulatedControllerCrash):
            await workloads.submit_once()
        async with database.sessions() as session:
            event = await session.scalar(
                select(OutboxEvent).where(OutboxEvent.aggregate_id == context.attempt_id)
            )
            attempt = await session.get(GenerationAttempt, context.attempt_id)
            assert event is not None
            assert attempt is not None
            assert event.status == OutboxStatus.PROCESSING
            assert "runtime_admission_refresh_plan" in attempt.request_metadata
            assert "runtime_admission" not in attempt.request_metadata
            event.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()

        # Deployment reconciliation can race expired-lease recovery. It must
        # leave the already-applied marker+min=1 rollout untouched until this
        # durable plan is adopted as an exact target.
        async with database.sessions() as session:
            deployment = await session.scalar(select(SaladDeployment))
            assert deployment is not None
            queue_name, group_name = _remote_names()
            reconcile_client = DeploymentOnlyClient(
                queue=_queue(queue_name),
                group=replace(
                    _group(group_name, queue_name),
                    version=remote_version,
                ),
            )
            reconciled = await reconcile_deployment(
                session,
                deployment_id=deployment.id,
                client=cast(SaladClient, reconcile_client),
                now=NOW + timedelta(minutes=1),
            )
            await session.commit()
        assert reconciled.state == SaladDeploymentState.ACTIVE
        assert reconcile_client.updated_group_patches == []
        assert reconcile_client.start_names == []

        # Expired-lease recovery can prove this exact plan never reached a job
        # POST, reclaims it, and adopts the provider marker instead of PATCHing.
        assert await workloads.submit_once() is True
        assert patch_count == 1
        assert adoption_count == 1
        async with database.sessions() as session:
            event = await session.scalar(
                select(OutboxEvent).where(OutboxEvent.aggregate_id == context.attempt_id)
            )
            attempt = await session.get(GenerationAttempt, context.attempt_id)
            assert event is not None
            assert attempt is not None
            assert event.status == OutboxStatus.PENDING
            assert event.attempts == 1
            assert attempt.state == GenerationAttemptState.CREATED
            assert attempt.request_metadata["runtime_admission"] == {
                "version": "v1",
                "provider_group_version": 2,
                "artifact_manifest_sha256": "0" * 64,
                "rollout_id": remote_rollout_id,
                "worker_instance_id": None,
            }
            assert "runtime_admission_refresh_plan" not in attempt.request_metadata
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_submit_refreshes_idle_resident_lora_superset_without_evicting_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'resident-superset.db').as_posix()}")
    await database.create_schema()
    try:
        context = await _seed_submission_database(database)
        lora_a = "1" * 64
        lora_b = "2" * 64
        resident_digest = canonical_sha256({"managed_loras": (lora_a, lora_b)})
        async with database.sessions() as session:
            job = await session.get(GenerationJob, context.job_id)
            deployment = await session.scalar(select(SaladDeployment))
            outbox = await session.scalar(
                select(OutboxEvent).where(OutboxEvent.aggregate_id == context.attempt_id)
            )
            assert job is not None
            assert deployment is not None
            assert outbox is not None
            job.parameters = _runtime_job_parameters(1, [{"sha256": lora_a}])
            job.parameters_sha256 = canonical_sha256(job.parameters)
            deployment.runtime_managed_lora_sha256s = [lora_a, lora_b]
            deployment.runtime_artifact_manifest_sha256 = resident_digest
            await session.commit()
            deployment_id = deployment.id
            outbox_id = outbox.id

        refreshes: list[tuple[UUID, Mapping[str, str] | None]] = []
        submissions: list[UUID] = []

        async def fake_refresh(
            deployment: SaladDeployment,
            client: object,
            resolver: object,
            *,
            environment_overrides: Mapping[str, str] | None = None,
            **runtime_refresh: object,
        ) -> SaladContainerGroup:
            del client, resolver, runtime_refresh
            refreshes.append((deployment.id, environment_overrides))
            return _group(deployment.container_group_name, deployment.queue_name)

        async def fake_submit(
            *args: object,
            generation_attempt_id: UUID,
            **kwargs: object,
        ) -> SubmissionResult:
            del args, kwargs
            submissions.append(generation_attempt_id)
            return SubmissionResult(
                generation_attempt_id=generation_attempt_id,
                attempt_state=GenerationAttemptState.CREATED,
                generation_job_state=GenerationState.CLAIMED,
                disposition=SubmissionDisposition.SUBMITTED,
                mutation_effect=MutationEffect.CONFIRMED,
                provider_external_id="provider-job",
            )

        async def fake_admission(
            deployment: SaladDeployment,
            client: object,
            *,
            effective_min_replicas: int,
            **runtime_identity: object,
        ) -> SaladContainerGroup:
            del client, runtime_identity
            assert effective_min_replicas == 1
            return _group(deployment.container_group_name, deployment.queue_name)

        monkeypatch.setattr(
            ControllerWorkloads,
            "_effective_artifact_manifest",
            _selected_effective_artifact_manifest,
        )
        monkeypatch.setattr(
            controller_runtime,
            "refresh_container_group_runtime",
            fake_refresh,
        )
        monkeypatch.setattr(
            controller_runtime,
            "ensure_container_group_queue_admission",
            fake_admission,
        )
        monkeypatch.setattr(controller_runtime, "submit_prepared_attempt", fake_submit)
        workloads = ControllerWorkloads(
            settings=Settings(
                worker_signing_key_id="worker-key-1",
                worker_signing_private_key=WORKER_SIGNING_PRIVATE_KEY,
            ),
            sessions=database.sessions,
            instance_id="controller-resident-superset-test",
            salad_client=cast(SaladClient, object()),
            object_store=cast(ObjectStore, object()),
        )
        event = ClaimedOutboxEvent(
            id=outbox_id,
            topic=SALAD_JOB_SUBMIT_TOPIC,
            dedupe_key=f"{SALAD_JOB_SUBMIT_TOPIC}:{context.attempt_id}",
            correlation_id=str(context.attempt_id),
            aggregate_type=GENERATION_ATTEMPT_AGGREGATE,
            aggregate_id=context.attempt_id,
            payload={"generation_attempt_id": str(context.attempt_id)},
            attempt=1,
            max_attempts=3,
            lease_expires_at=NOW + timedelta(minutes=5),
        )

        with pytest.raises(controller_runtime._RuntimeAdmissionPendingError):
            await workloads._submit_event(event, progress=_SubmissionProgress())
        assert refreshes == []
        with pytest.raises(controller_runtime._RuntimeAdmissionPendingError):
            await workloads._submit_event(event, progress=_SubmissionProgress())
        assert len(refreshes) == 1
        refresh_deployment_id, refresh_environment = refreshes[0]
        assert refresh_deployment_id == deployment_id
        assert refresh_environment is not None
        assert refresh_environment["GEN_WORKER_MODEL_MANIFEST_JSON"] == (
            '{"artifacts":[],"manifest_sha256":"' + resident_digest + '"}'
        )
        assert refresh_environment["GEN_WORKER_MODEL_MANIFEST_SHA256"] == resident_digest
        assert len(refresh_environment[WORKER_RUNTIME_ADMISSION_ID_BINDING]) == 32
        assert submissions == []
        monkeypatch.setattr(
            controller_runtime,
            "container_group_runtime_admission_ready",
            _runtime_admission_ready,
        )
        with pytest.raises(controller_runtime._RuntimeAdmissionPendingError):
            await workloads._submit_event(event, progress=_SubmissionProgress())
        await workloads._submit_event(event, progress=_SubmissionProgress())
        assert submissions == [context.attempt_id]
    finally:
        await database.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("active_attempt_state", "active_job_state", "provider_external_id"),
    [
        (
            GenerationAttemptState.RUNNING,
            GenerationState.RUNNING,
            "provider-active-manifest-attempt",
        ),
        (GenerationAttemptState.UNKNOWN, GenerationState.CANCELLED, None),
    ],
)
async def test_submit_defers_an_incompatible_lora_rollout_while_work_is_active(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    active_attempt_state: GenerationAttemptState,
    active_job_state: GenerationState,
    provider_external_id: str | None,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'active-manifest-defer.db').as_posix()}")
    await database.create_schema()
    try:
        first = await _seed_submission_database(database)
        lora_a = "3" * 64
        lora_b = "4" * 64
        async with database.sessions() as session:
            deployment = await session.scalar(select(SaladDeployment))
            first_job = await session.get(GenerationJob, first.job_id)
            first_attempt = await session.get(GenerationAttempt, first.attempt_id)
            assert deployment is not None
            assert first_job is not None
            assert first_attempt is not None
            first_job.parameters = _runtime_job_parameters(1, [{"sha256": lora_a}])
            first_job.parameters_sha256 = canonical_sha256(first_job.parameters)
            first_job.state = active_job_state
            first_attempt.state = active_attempt_state
            first_attempt.provider_external_id = provider_external_id
            first_attempt.unknown_since = (
                NOW if active_attempt_state == GenerationAttemptState.UNKNOWN else None
            )
            deployment.runtime_managed_lora_sha256s = [lora_a]
            active_manifest_sha256 = canonical_sha256({"managed_loras": (lora_a,)})
            deployment.runtime_artifact_manifest_sha256 = active_manifest_sha256
            first_attempt.request_metadata = {
                **first_attempt.request_metadata,
                "runtime_admission": {
                    "version": "v1",
                    "provider_group_version": 1,
                    "artifact_manifest_sha256": active_manifest_sha256,
                    "rollout_id": RUNTIME_ADMISSION_ID,
                    "worker_instance_id": "instance-active-1",
                },
            }
            second_parameters = _runtime_job_parameters(2, [{"sha256": lora_b}])
            second_job = GenerationJob(
                release_version_id=first_job.release_version_id,
                logical_key="f" * 64,
                parameters=second_parameters,
                parameters_sha256=canonical_sha256(second_parameters),
                provider="salad",
                state=GenerationState.QUEUED,
                expected_output_count=1,
            )
            session.add(second_job)
            await session.flush()
            second = await prepare_generation_attempt(
                session,
                generation_job_id=second_job.id,
                salad_deployment_id=deployment.id,
                idempotency_key="active-manifest-second-attempt",
                now=NOW,
            )
            await session.commit()

        monkeypatch.setattr(
            ControllerWorkloads,
            "_effective_artifact_manifest",
            _selected_effective_artifact_manifest,
        )
        workloads = ControllerWorkloads(
            settings=Settings(
                worker_signing_key_id="worker-key-1",
                worker_signing_private_key=WORKER_SIGNING_PRIVATE_KEY,
            ),
            sessions=database.sessions,
            instance_id="controller-active-manifest-test",
            salad_client=cast(SaladClient, object()),
            object_store=cast(ObjectStore, object()),
        )
        event = ClaimedOutboxEvent(
            id=second.outbox_event_id,
            topic=SALAD_JOB_SUBMIT_TOPIC,
            dedupe_key=f"{SALAD_JOB_SUBMIT_TOPIC}:{second.generation_attempt_id}",
            correlation_id=str(second.generation_attempt_id),
            aggregate_type=GENERATION_ATTEMPT_AGGREGATE,
            aggregate_id=second.generation_attempt_id,
            payload={"generation_attempt_id": str(second.generation_attempt_id)},
            attempt=1,
            max_attempts=3,
            lease_expires_at=NOW + timedelta(minutes=5),
        )

        with pytest.raises(controller_runtime._RuntimeArtifactManifestBusyError):
            await workloads._submit_event(event, progress=_SubmissionProgress())
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_experiment_warm_lease_refreshes_first_submit_once_then_reuses_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'warm-submit.db').as_posix()}")
    await database.create_schema()
    try:
        context = await _seed_submission_database(database)
        warm_now = datetime.now(UTC)
        async with database.sessions() as session:
            deployment = await session.scalar(select(SaladDeployment))
            assert deployment is not None
            started = await start_experiment_warm_lease(
                session,
                salad_deployment_id=deployment.id,
                actor="lab-post",
                now=warm_now,
            )
            await session.commit()
            deployment_id = deployment.id

        refreshes: list[UUID] = []
        submissions: list[UUID] = []

        async def fake_refresh(
            deployment: SaladDeployment,
            client: object,
            resolver: object,
            *,
            environment_overrides: Mapping[str, str] | None = None,
            **runtime_refresh: object,
        ) -> SaladContainerGroup:
            del client, resolver, environment_overrides, runtime_refresh
            refreshes.append(deployment.id)
            return _group(deployment.container_group_name, deployment.queue_name)

        async def fake_submit(
            *args: object,
            generation_attempt_id: UUID,
            **kwargs: object,
        ) -> SubmissionResult:
            del kwargs
            session = cast(AsyncSession, args[0])
            await session.commit()
            submissions.append(generation_attempt_id)
            return SubmissionResult(
                generation_attempt_id=generation_attempt_id,
                attempt_state=GenerationAttemptState.CREATED,
                generation_job_state=GenerationState.CLAIMED,
                disposition=SubmissionDisposition.SUBMITTED,
                mutation_effect=MutationEffect.CONFIRMED,
                provider_external_id="provider-job",
            )

        async def fake_admission(
            deployment: SaladDeployment,
            client: object,
            *,
            effective_min_replicas: int,
            **runtime_identity: object,
        ) -> SaladContainerGroup:
            del client, runtime_identity
            assert effective_min_replicas == 1
            return _group(deployment.container_group_name, deployment.queue_name)

        monkeypatch.setattr(
            controller_runtime,
            "refresh_container_group_runtime",
            fake_refresh,
        )
        monkeypatch.setattr(
            controller_runtime,
            "ensure_container_group_queue_admission",
            fake_admission,
        )
        monkeypatch.setattr(
            ControllerWorkloads,
            "_effective_artifact_manifest",
            _empty_effective_artifact_manifest,
        )
        monkeypatch.setattr(controller_runtime, "submit_prepared_attempt", fake_submit)
        workloads = ControllerWorkloads(
            settings=Settings(
                worker_signing_key_id="worker-key-1",
                worker_signing_private_key=WORKER_SIGNING_PRIVATE_KEY,
                salad_worker_model_manifest_json=RUNTIME_MANIFEST,
                salad_worker_model_manifest_sha256="0" * 64,
            ),
            sessions=database.sessions,
            instance_id="controller-warm-submit-test",
            salad_client=cast(SaladClient, object()),
            object_store=cast(ObjectStore, object()),
        )
        event = ClaimedOutboxEvent(
            id=UUID("0af360ac-9a91-4684-8e48-09c0f4ee788d"),
            topic=SALAD_JOB_SUBMIT_TOPIC,
            dedupe_key=f"{SALAD_JOB_SUBMIT_TOPIC}:{context.attempt_id}",
            correlation_id=str(context.attempt_id),
            aggregate_type=GENERATION_ATTEMPT_AGGREGATE,
            aggregate_id=context.attempt_id,
            payload={"generation_attempt_id": str(context.attempt_id)},
            attempt=1,
            max_attempts=3,
            lease_expires_at=NOW + timedelta(minutes=5),
        )

        with pytest.raises(controller_runtime._RuntimeAdmissionPendingError):
            await workloads._submit_event(event, progress=_SubmissionProgress())
        with pytest.raises(controller_runtime._RuntimeAdmissionPendingError):
            await workloads._submit_event(event, progress=_SubmissionProgress())
        monkeypatch.setattr(
            controller_runtime,
            "container_group_runtime_admission_ready",
            _runtime_admission_ready,
        )
        with pytest.raises(controller_runtime._RuntimeAdmissionPendingError):
            await workloads._submit_event(event, progress=_SubmissionProgress())
        await workloads._submit_event(event, progress=_SubmissionProgress())
        await workloads._submit_event(event, progress=_SubmissionProgress())

        assert refreshes == [deployment_id]
        assert submissions == [context.attempt_id, context.attempt_id]
        async with database.sessions() as session:
            lease = await session.get(ExperimentWarmLease, started.lease_id)
            assert lease is not None
            assert lease.provider_version == 1
    finally:
        await database.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("pending_job", [False, True])
async def test_manual_warm_selection_is_used_only_without_a_pending_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pending_job: bool,
) -> None:
    suffix = "pending" if pending_job else "manual"
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / f'warm-{suffix}.db').as_posix()}")
    await database.create_schema()
    try:
        context = await _seed_submission_database(database)
        manual_checkpoint = "8" * 64
        manual_lora = "7" * 64
        pending_lora = "6" * 64
        async with database.sessions() as session:
            deployment = await session.scalar(select(SaladDeployment))
            job = await session.get(GenerationJob, context.job_id)
            assert deployment is not None and job is not None
            started = await start_experiment_warm_lease(
                session,
                salad_deployment_id=deployment.id,
                actor="test",
                now=datetime.now(UTC),
            )
            lease = await session.get(ExperimentWarmLease, started.lease_id)
            assert lease is not None
            lease.requested_checkpoint_sha256 = manual_checkpoint
            lease.requested_lora_sha256s = [manual_lora]
            if pending_job:
                job.parameters = _runtime_job_parameters(
                    1,
                    [{"sha256": pending_lora}],
                )
                job.parameters_sha256 = canonical_sha256(job.parameters)
            else:
                await session.execute(
                    delete(OutboxEvent).where(OutboxEvent.aggregate_id == context.attempt_id)
                )
                await session.execute(
                    delete(GenerationAttempt).where(GenerationAttempt.id == context.attempt_id)
                )
                await session.execute(
                    delete(GenerationJob).where(GenerationJob.id == context.job_id)
                )
            await session.commit()

        selections: list[tuple[str, tuple[str, ...]]] = []

        async def selected_manifest(
            _workloads: ControllerWorkloads,
            _session: AsyncSession,
            *,
            required_checkpoint_sha256: str,
            required_lora_sha256s: tuple[str, ...] = (),
        ) -> EffectiveArtifactManifest:
            selection = (required_checkpoint_sha256, tuple(required_lora_sha256s))
            selections.append(selection)
            digest = canonical_sha256(selection)
            manifest = ArtifactManifest.model_construct(
                version="v1",
                artifacts=(),
                manifest_sha256=digest,
            )
            return EffectiveArtifactManifest(
                manifest=manifest,
                manifest_json=('{"artifacts":[],"manifest_sha256":"' + digest + '"}'),
                managed_lora_sha256s=frozenset(required_lora_sha256s),
            )

        async def fake_refresh(
            deployment: SaladDeployment,
            client: object,
            resolver: object,
            *,
            environment_overrides: Mapping[str, str] | None = None,
            **runtime_refresh: object,
        ) -> SaladContainerGroup:
            del client, resolver, environment_overrides, runtime_refresh
            return _group(deployment.container_group_name, deployment.queue_name)

        monkeypatch.setattr(
            ControllerWorkloads,
            "_effective_artifact_manifest",
            selected_manifest,
        )
        monkeypatch.setattr(
            controller_runtime,
            "refresh_container_group_runtime",
            fake_refresh,
        )
        workloads = ControllerWorkloads(
            settings=Settings().model_copy(update={"gpu_allocation_enabled": True}),
            sessions=database.sessions,
            instance_id=f"controller-warm-{suffix}-selection-test",
            salad_client=cast(SaladClient, object()),
            object_store=None,
        )

        assert await workloads.experiment_warm_once() is True
        assert selections == [
            (
                TEST_CHECKPOINT_SHA256 if pending_job else manual_checkpoint,
                (pending_lora,) if pending_job else (manual_lora,),
            )
        ]
    finally:
        await database.dispose()


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
                    "background_submit_timeout_seconds": 1.0,
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
            durable_runtime_admission_defer: bool = False,
        ) -> object:
            del event, durable_runtime_admission_defer
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
