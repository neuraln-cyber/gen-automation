from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
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
from gen_automation.services.salad_deployments import deterministic_provider_name
from gen_automation.storage.base import ObjectStore

NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
CONFIG_SHA256 = "a" * 64
IMAGE_DIGEST = "registry.example.test/worker@sha256:" + "b" * 64
WORKER_SIGNING_PRIVATE_KEY = encode_base64url(bytes(range(1, 33)))
RUNTIME_MANIFEST = '{"artifacts":[],"manifest_sha256":"' + ("0" * 64) + '"}'
QUEUE_ID = UUID("3d59eff3-8f46-4743-ab42-c5bdd56a04ca")
GROUP_ID = UUID("e1f35986-d00a-44d0-a0c6-59dda919b07b")


async def _empty_effective_artifact_manifest(
    _workloads: ControllerWorkloads,
    _session: AsyncSession,
    *,
    required_lora_sha256s: tuple[str, ...] = (),
) -> EffectiveArtifactManifest:
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
    required_lora_sha256s: tuple[str, ...] = (),
) -> EffectiveArtifactManifest:
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
        job = GenerationJob(
            release_version_id=version.id,
            logical_key="d" * 64,
            parameters={"seed": 1, "loras": []},
            parameters_sha256=canonical_sha256({"seed": 1, "loras": []}),
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
        ) -> SaladContainerGroup:
            del client, resolver, environment_overrides
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
        ) -> SaladContainerGroup:
            del client
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

        resumed = await workloads._submit_event(reclaimed[0], progress=_SubmissionProgress())
        assert resumed.disposition == SubmissionDisposition.SUBMITTED
        assert admissions and admissions[0][1] == 1
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
async def test_submit_reuses_active_runtime_then_refreshes_same_manifest_at_idle_boundary(
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

            second_job = GenerationJob(
                release_version_id=first_job.release_version_id,
                logical_key="e" * 64,
                parameters={"seed": 2, "loras": []},
                parameters_sha256=canonical_sha256({"seed": 2, "loras": []}),
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
        ) -> SaladContainerGroup:
            del client, resolver, environment_overrides
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

        monkeypatch.setattr(
            controller_runtime,
            "refresh_container_group_runtime",
            fake_refresh,
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

        # With no other active provider attempt, this is the idle-to-active
        # boundary where refreshing short-lived bootstrap credentials is safe.
        await workloads._submit_event(event, progress=_SubmissionProgress())
        assert refreshes == [deployment_id]
        assert submissions == [
            second.generation_attempt_id,
            second.generation_attempt_id,
        ]
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

        admissions: list[tuple[UUID, int]] = []
        submissions: list[UUID] = []

        async def fake_refresh(
            deployment: SaladDeployment,
            client: object,
            resolver: object,
            *,
            environment_overrides: Mapping[str, str] | None = None,
        ) -> SaladContainerGroup:
            del client, resolver, environment_overrides
            group = _group(deployment.container_group_name, deployment.queue_name)
            return replace(
                group,
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
        ) -> SaladContainerGroup:
            del client
            admissions.append((deployment.id, effective_min_replicas))
            group = _group(deployment.container_group_name, deployment.queue_name)
            # A provider-accepted start can still read back as stopped until
            # the cold allocation appears. The durable queue POST must not
            # wait for image download or worker attachment.
            return replace(
                group,
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

        await workloads._submit_event(claimed, progress=_SubmissionProgress())

        assert len(admissions) == 1
        assert admissions[0][1] == 1
        assert submissions == [context.attempt_id]
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
            job.parameters = {"seed": 1, "loras": [{"sha256": lora_a}]}
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
        ) -> SaladContainerGroup:
            del client, resolver
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

        await workloads._submit_event(event, progress=_SubmissionProgress())
        assert refreshes == [
            (
                deployment_id,
                {
                    "GEN_WORKER_MODEL_MANIFEST_JSON": (
                        '{"artifacts":[],"manifest_sha256":"' + resident_digest + '"}'
                    ),
                    "GEN_WORKER_MODEL_MANIFEST_SHA256": resident_digest,
                },
            )
        ]
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
            first_job.parameters = {"seed": 1, "loras": [{"sha256": lora_a}]}
            first_job.parameters_sha256 = canonical_sha256(first_job.parameters)
            first_job.state = active_job_state
            first_attempt.state = active_attempt_state
            first_attempt.provider_external_id = provider_external_id
            first_attempt.unknown_since = (
                NOW if active_attempt_state == GenerationAttemptState.UNKNOWN else None
            )
            deployment.runtime_managed_lora_sha256s = [lora_a]
            deployment.runtime_artifact_manifest_sha256 = canonical_sha256(
                {"managed_loras": (lora_a,)}
            )
            second_job = GenerationJob(
                release_version_id=first_job.release_version_id,
                logical_key="f" * 64,
                parameters={"seed": 2, "loras": [{"sha256": lora_b}]},
                parameters_sha256=canonical_sha256({"seed": 2, "loras": [{"sha256": lora_b}]}),
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
        ) -> SaladContainerGroup:
            del client, resolver, environment_overrides
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

        monkeypatch.setattr(
            controller_runtime,
            "refresh_container_group_runtime",
            fake_refresh,
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
