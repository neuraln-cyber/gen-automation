from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest
from sqlalchemy import func, select
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
from gen_automation.domain.signing import encode_base64url
from gen_automation.gpu_worker.artifacts import ArtifactManifest
from gen_automation.integrations.salad.client import SaladClient
from gen_automation.integrations.salad.models import (
    JSONValue,
    SaladContainerGroup,
    SaladContainerGroupState,
    SaladQueue,
)
from gen_automation.services.budgets import ensure_budget_guard, reserve_attempt_budget
from gen_automation.services.experiment_warm_leases import start_experiment_warm_lease
from gen_automation.services.outbox import (
    GENERATION_ATTEMPT_AGGREGATE,
    SALAD_JOB_SUBMIT_TOPIC,
    ClaimedOutboxEvent,
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
            assert deployment.state == SaladDeploymentState.DRAINING
            assert deployment.provider_queue_id == str(QUEUE_ID)
            assert deployment.provider_container_group_id == str(GROUP_ID)
            assert "salad_deployment.gpu_allocation_disabled" in actions
            assert "salad_deployment.stop_container_group_recovered" in actions
        assert client.stop_names == [group_name]
        assert client.create_calls == 0
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
            assert deployment.state == SaladDeploymentState.DRAINING
            assert deployment.provider_queue_id == str(QUEUE_ID)
            assert deployment.provider_container_group_id == str(GROUP_ID)
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
async def test_submit_refreshes_runtime_only_at_same_deployment_idle_boundary(
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
                parameters={"seed": 2},
                parameters_sha256=canonical_sha256({"seed": 2}),
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
        ) -> SaladContainerGroup:
            del client, resolver
            refreshes.append(deployment.id)
            return cast(SaladContainerGroup, object())

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
        artifact_manifest = ArtifactManifest.model_construct(
            version="v1",
            artifacts=(),
            manifest_sha256="0" * 64,
        )
        monkeypatch.setattr(
            controller_runtime,
            "load_artifact_manifest",
            lambda _raw: artifact_manifest,
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
            first_attempt = await session.get(GenerationAttempt, first.attempt_id)
            assert first_attempt is not None
            first_attempt.state = GenerationAttemptState.FAILED
            first_attempt.completed_at = NOW
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
        ) -> SaladContainerGroup:
            del client, resolver
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
        artifact_manifest = ArtifactManifest.model_construct(
            version="v1",
            artifacts=(),
            manifest_sha256="0" * 64,
        )
        monkeypatch.setattr(
            controller_runtime,
            "load_artifact_manifest",
            lambda _raw: artifact_manifest,
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
