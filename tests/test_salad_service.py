from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from gen_automation.db.models import (
    Asset,
    AuditEvent,
    GenerationAttempt,
    GenerationJob,
    OutboxEvent,
    Project,
    Release,
    ReleaseVersion,
    SaladDeployment,
)
from gen_automation.db.session import Database
from gen_automation.domain.canonical import canonical_sha256
from gen_automation.domain.enums import (
    AssetKind,
    AssetState,
    DesiredDeploymentState,
    GenerationAttemptState,
    GenerationState,
    OutboxStatus,
    ReleasePhase,
    ResourceHealth,
    SaladDeploymentPurpose,
    SaladDeploymentState,
)
from gen_automation.integrations.salad.client import SALAD_QUEUE_JOB_PAGE_SIZE
from gen_automation.integrations.salad.errors import (
    SaladAPIError,
    SaladProtocolError,
    SaladRateLimitError,
    SaladTimeoutError,
    SaladTransportError,
)
from gen_automation.integrations.salad.models import (
    JSONObject,
    JSONValue,
    SaladJobEvent,
    SaladJobStatus,
    SaladQueueJob,
    SaladQueueJobPage,
)
from gen_automation.services.budgets import ensure_budget_guard, reserve_attempt_budget
from gen_automation.services.generation_recovery import (
    INFRASTRUCTURE_RETRY_GRANT_ACTION,
    NEAR_BLACK_OUTPUT_RETRY_GRANT_ACTION,
    SALAD_PROVIDER_CANCELLED_ERROR_CODE,
    SALAD_PROVIDER_JOB_ABSENT_ERROR_CODE,
    SALAD_WORKER_NEAR_BLACK_OUTPUT_ERROR_CODE,
)
from gen_automation.services.outbox import (
    SALAD_JOB_SUBMIT_TOPIC,
    claim_outbox_events,
    defer_unstarted_outbox_event,
    succeed_outbox_event,
)
from gen_automation.services.salad import (
    DEPLOYMENT_ROLLOVER_CANCEL_REQUESTED_ERROR_CODE,
    DEPLOYMENT_ROLLOVER_RETRY_ERROR_CODE,
    OPERATOR_STOP_CANCEL_REQUESTED_ERROR_CODE,
    SALAD_ATTEMPT_WATCHDOG_CANCEL_REQUESTED_ERROR_CODE,
    SALAD_ATTEMPT_WATCHDOG_EXPIRED_ERROR_CODE,
    MutationEffect,
    ReconciliationSource,
    SaladDeploymentConfig,
    SaladJobInputContext,
    SaladJobInputDeferredError,
    SaladServiceConflictError,
    SaladServiceValidationError,
    SubmissionDisposition,
    apply_salad_job_observation,
    create_deployment_version,
    deployment_config_sha256,
    prepare_generation_attempt,
    reconcile_generation_attempt,
    submit_prepared_attempt,
)

NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
REMOTE_JOB_ID = UUID("1c25eacc-29c9-4c84-a5b8-9e3617f6cd67")
IMAGE_DIGEST = f"registry.example.test/worker@sha256:{'a' * 64}"
SIGNED_UPLOAD_URL = "https://storage.example.test/upload?X-Amz-Signature=must-never-be-persisted"


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'salad.db').as_posix()}")
    await database.create_schema()
    try:
        yield database
    finally:
        await database.dispose()


@dataclass(frozen=True)
class SeededContext:
    job_id: UUID
    deployment_id: UUID


async def seed_context(session: AsyncSession) -> SeededContext:
    project = Project(slug="production", name="Production")
    session.add(project)
    await session.flush()
    release = Release(
        project_id=project.id,
        slug="release-one",
        title="Release One",
        desired_accepted_count=4,
        phase=ReleasePhase.READY,
    )
    session.add(release)
    await session.flush()
    version = ReleaseVersion(
        release_id=release.id,
        version_no=1,
        specification={"schema_version": 1},
        specification_sha256=canonical_sha256({"schema_version": 1}),
        created_by="test",
        created_at=NOW,
    )
    session.add(version)
    await session.flush()
    deployment = SaladDeployment(
        version_no=1,
        config_sha256="b" * 64,
        worker_image_digest=IMAGE_DIGEST,
        organization_name="creator-org",
        project_name="production",
        queue_name="generation-v1",
        provider_queue_id="provider-queue-v1",
        container_group_name="worker-v1",
        provider_container_group_id="provider-group-v1",
        state=SaladDeploymentState.ACTIVE,
        desired_state=DesiredDeploymentState.ACTIVE,
        is_current=True,
        min_replicas=0,
        max_replicas=1,
        desired_queue_length=1,
        max_hourly_cost_microusd=2_000_000,
        lock_version=1,
    )
    session.add(deployment)
    await session.flush()
    parameters = {
        "prompt": "private generation prompt",
        "negative_prompt": "private negative prompt",
        "seed": 123,
    }
    job = GenerationJob(
        release_version_id=version.id,
        logical_key="job-one",
        parameters=parameters,
        parameters_sha256=canonical_sha256(parameters),
        provider="salad",
        state=GenerationState.QUEUED,
        priority=100,
        expected_output_count=2,
        attempt_count=0,
        max_attempts=3,
        lock_version=1,
    )
    session.add(job)
    await session.flush()
    await ensure_budget_guard(
        session,
        provider="salad",
        daily_limit_usd=Decimal("20"),
        monthly_limit_usd=Decimal("200"),
        now=NOW,
    )
    await session.commit()
    return SeededContext(job_id=job.id, deployment_id=deployment.id)


@dataclass
class FakeUploadIntentProvider:
    fail: bool = False
    defer_retry_seconds: int | None = None
    calls: list[SaladJobInputContext] = field(default_factory=list)

    async def build_job_input(self, context: SaladJobInputContext) -> JSONValue:
        self.calls.append(context)
        if self.defer_retry_seconds is not None:
            raise SaladJobInputDeferredError(
                retry_after_seconds=self.defer_retry_seconds,
            )
        if self.fail:
            raise RuntimeError(f"do not persist {SIGNED_UPLOAD_URL}")
        return {
            "generation": dict(context.parameters),
            "uploads": [
                {
                    "output_index": 0,
                    "url": SIGNED_UPLOAD_URL,
                    "method": "POST",
                }
            ],
        }


@dataclass
class FakeSaladClient:
    create_error: Exception | None = None
    create_status: SaladJobStatus = SaladJobStatus.PENDING
    create_job_id: UUID = REMOTE_JOB_ID
    response_metadata_override: JSONObject | None = None
    create_calls: list[tuple[str, JSONValue, JSONObject, str | None]] = field(default_factory=list)
    get_result: SaladQueueJob | None = None
    get_error: Exception | None = None
    get_calls: list[tuple[str, str]] = field(default_factory=list)
    list_pages: dict[int, tuple[SaladQueueJob, ...]] = field(default_factory=dict)
    list_error: Exception | None = None
    list_calls: list[tuple[str, int, int]] = field(default_factory=list)
    cancel_error: Exception | None = None
    cancel_calls: list[tuple[str, str]] = field(default_factory=list)
    before_get: Callable[[], Awaitable[None]] | None = None
    before_list: Callable[[], Awaitable[None]] | None = None

    async def create_job(
        self,
        queue_name: str,
        *,
        input: JSONValue,
        metadata: Mapping[str, JSONValue] | None = None,
        webhook: str | None = None,
    ) -> SaladQueueJob:
        normalized_metadata = dict(metadata or {})
        self.create_calls.append((queue_name, input, normalized_metadata, webhook))
        if self.create_error is not None:
            raise self.create_error
        return remote_job(
            status=self.create_status,
            metadata=self.response_metadata_override or normalized_metadata,
            update_time=NOW + timedelta(seconds=1),
            job_id=self.create_job_id,
        )

    async def get_job(self, queue_name: str, job_id: UUID | str) -> SaladQueueJob:
        self.get_calls.append((queue_name, str(job_id)))
        if self.before_get is not None:
            before_get, self.before_get = self.before_get, None
            await before_get()
        if self.get_error is not None:
            raise self.get_error
        if self.get_result is None:
            raise AssertionError("unexpected get_job call")
        return self.get_result

    async def list_jobs(
        self,
        queue_name: str,
        *,
        page: int = 1,
        page_size: int = SALAD_QUEUE_JOB_PAGE_SIZE,
    ) -> SaladQueueJobPage:
        self.list_calls.append((queue_name, page, page_size))
        if self.before_list is not None:
            before_list, self.before_list = self.before_list, None
            await before_list()
        if self.list_error is not None:
            raise self.list_error
        return SaladQueueJobPage(items=self.list_pages.get(page, ()))

    async def cancel_job(self, queue_name: str, job_id: UUID | str) -> None:
        self.cancel_calls.append((queue_name, str(job_id)))
        if self.cancel_error is not None:
            raise self.cancel_error


def remote_job(
    *,
    status: SaladJobStatus,
    metadata: JSONObject,
    update_time: datetime,
    job_id: UUID = REMOTE_JOB_ID,
    output: JSONValue | None = None,
    events: tuple[SaladJobEvent, ...] | None = None,
) -> SaladQueueJob:
    return SaladQueueJob(
        id=job_id,
        input={"private": "provider input is not persisted"},
        status=status,
        events=events or (SaladJobEvent(action="updated", time=update_time),),
        create_time=NOW,
        update_time=update_time,
        metadata=metadata,
        webhook="https://controller.example.test/webhooks/salad",
        output=output or {"private": "provider output is not persisted"},
    )


async def prepared_attempt(
    session: AsyncSession,
    context: SeededContext,
) -> UUID:
    prepared = await prepare_generation_attempt(
        session,
        generation_job_id=context.job_id,
        salad_deployment_id=context.deployment_id,
        idempotency_key="scheduler-claim-1",
        now=NOW,
    )
    await session.commit()
    return prepared.generation_attempt_id


def enable_runtime_admission(attempt: GenerationAttempt) -> None:
    request_metadata = dict(attempt.request_metadata)
    request_metadata["runtime_admission"] = {
        "version": "v1",
        "provider_group_version": 7,
        "artifact_manifest_sha256": "7" * 64,
    }
    attempt.request_metadata = request_metadata
    attempt.lock_version += 1


async def add_progress_watchdog_assets(
    session: AsyncSession,
    context: SeededContext,
) -> tuple[UUID, UUID]:
    job = await session.get(GenerationJob, context.job_id)
    assert job is not None
    version = await session.get(ReleaseVersion, job.release_version_id)
    assert version is not None
    retained = Asset(
        release_id=version.release_id,
        generation_job_id=job.id,
        output_index=0,
        kind=AssetKind.RAW_MASTER,
        state=AssetState.AVAILABLE,
        storage_backend="memory",
        storage_bucket="test-assets",
        staging_object_key=f"staging/{job.id}/0/current",
        object_key=f"masters/{job.id}/0.png",
        object_version_id="retained-version",
        sha256="8" * 64,
        content_type="image/png",
        image_format="PNG",
        width=1024,
        height=1024,
        byte_size=4096,
        asset_metadata={
            "declared_content_type": "image/png",
            "upload_attempt_id": str(uuid4()),
            "staging_cleanup": "not_started",
        },
        available_at=NOW - timedelta(minutes=1),
    )
    staged = Asset(
        release_id=version.release_id,
        generation_job_id=job.id,
        output_index=1,
        kind=AssetKind.RAW_MASTER,
        state=AssetState.UPLOADING,
        storage_backend="memory",
        storage_bucket="test-assets",
        staging_object_key=f"staging/{job.id}/1/current",
        asset_metadata={
            "declared_content_type": "image/png",
            "upload_attempt_id": str(uuid4()),
            "staging_cleanup": "not_started",
        },
    )
    session.add_all((retained, staged))
    await session.flush()
    return retained.id, staged.id


async def superseded_running_attempt(
    session: AsyncSession,
) -> tuple[SeededContext, UUID, JSONObject, datetime]:
    context = await seed_context(session)
    attempt_id = await prepared_attempt(session, context)
    await submit_prepared_attempt(
        session,
        FakeSaladClient(),
        FakeUploadIntentProvider(),
        generation_attempt_id=attempt_id,
        webhook_url="https://controller.example.test/webhooks/salad",
        reservation_microusd=500_000,
        now=NOW,
    )
    attempt = await session.get(GenerationAttempt, attempt_id)
    job = await session.get(GenerationJob, context.job_id)
    deployment = await session.get(SaladDeployment, context.deployment_id)
    assert attempt is not None
    assert job is not None
    assert deployment is not None

    last_observed_at = NOW + timedelta(minutes=2)
    attempt.state = GenerationAttemptState.RUNNING
    attempt.provider_state = SaladJobStatus.RUNNING.value
    attempt.started_at = last_observed_at
    attempt.last_observed_at = last_observed_at
    attempt.lock_version += 1
    job.state = GenerationState.RUNNING
    job.lock_version += 1
    deployment.is_current = False
    deployment.desired_state = DesiredDeploymentState.STOPPED
    deployment.lock_version += 1
    await session.commit()

    metadata: JSONObject = {
        "generation_attempt_id": str(attempt.id),
        "generation_job_id": str(attempt.job_id),
        "submission_key": attempt.submission_key,
        "request_sha256": attempt.request_sha256,
    }
    return context, attempt_id, metadata, last_observed_at


async def mark_operator_stop(
    session: AsyncSession,
    context: SeededContext,
) -> None:
    job = await session.get(GenerationJob, context.job_id)
    assert job is not None
    version = await session.get(ReleaseVersion, job.release_version_id)
    assert version is not None
    session.add(
        AuditEvent(
            actor="test-owner",
            action="release.generation_stop_requested",
            resource_type="release",
            resource_id=version.release_id,
            correlation_id=f"generation-stop:{version.release_id}",
            detail={"assets_retained": True},
            occurred_at=NOW + timedelta(seconds=2),
        )
    )
    await session.commit()


async def expected_worker_outputs(
    session: AsyncSession,
    context: SeededContext,
) -> list[JSONObject]:
    job = await session.get(GenerationJob, context.job_id)
    assert job is not None
    version = await session.get(ReleaseVersion, job.release_version_id)
    assert version is not None
    outputs: list[JSONObject] = []
    for output_index in range(job.expected_output_count):
        upload_attempt_id = uuid4()
        asset = Asset(
            release_id=version.release_id,
            generation_job_id=job.id,
            output_index=output_index,
            kind=AssetKind.RAW_MASTER,
            state=AssetState.UPLOADING,
            storage_backend="memory",
            storage_bucket="test-assets",
            staging_object_key=f"staging/{job.id}/{output_index}",
            asset_metadata={
                "declared_content_type": "image/png",
                "upload_attempt_id": str(upload_attempt_id),
            },
        )
        session.add(asset)
        await session.flush()
        outputs.append(
            {
                "asset_id": str(asset.id),
                "upload_attempt_id": str(upload_attempt_id),
                "output_index": output_index,
                "status": "uploaded",
            }
        )
    return outputs


def test_deployment_config_hash_is_canonical_and_enforces_one_replica() -> None:
    first = SaladDeploymentConfig(
        organization_name="creator-org",
        project_name="production",
        queue_name="generation-v1",
        container_group_name="worker-v1",
        worker_image_digest=IMAGE_DIGEST,
        max_hourly_cost_microusd=1_500_000,
        provider_configuration={"replicas": 0, "resources": {"memory": 16384, "cpu": 4}},
    )
    second = SaladDeploymentConfig(
        organization_name="creator-org",
        project_name="production",
        queue_name="generation-v1",
        container_group_name="worker-v1",
        worker_image_digest=IMAGE_DIGEST,
        max_hourly_cost_microusd=1_500_000,
        provider_configuration={"resources": {"cpu": 4, "memory": 16384}, "replicas": 0},
    )

    assert deployment_config_sha256(first) == deployment_config_sha256(second)
    with pytest.raises(SaladServiceValidationError, match="at most one"):
        SaladDeploymentConfig(
            organization_name="creator-org",
            project_name="production",
            queue_name="generation-v1",
            container_group_name="worker-v1",
            worker_image_digest=IMAGE_DIGEST,
            max_hourly_cost_microusd=1_500_000,
            provider_configuration={"replicas": 2},
            max_replicas=2,
        )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"organization_name": " "}, "must not be empty"),
        ({"worker_image_digest": "registry.example.test/worker:latest"}, "immutable"),
        ({"min_replicas": -1}, "replica range"),
        ({"desired_queue_length": 0}, "desired_queue_length"),
        ({"max_hourly_cost_microusd": 0}, "max_hourly_cost"),
        ({"purpose": SaladDeploymentPurpose.VIDEO}, "only image deployments"),
        ({"provider_configuration": {"replicas": True}}, "must not request"),
        (
            {"provider_configuration": {"autoscaling": {"max_replicas": 2}}},
            "must not request",
        ),
        ({"provider_configuration": {"value": float("nan")}}, "finite JSON"),
        (
            {"provider_configuration": {"environment": {"api_key": "do-not-store"}}},
            "must not contain credentials",
        ),
        (
            {
                "provider_configuration": {
                    "callback": "https://example.test/?X-Amz-Signature=secret"
                }
            },
            "must not contain credentials",
        ),
    ],
)
def test_deployment_config_rejects_unsafe_values(
    overrides: dict[str, object],
    message: str,
) -> None:
    values: dict[str, object] = {
        "organization_name": "creator-org",
        "project_name": "production",
        "queue_name": "generation-v1",
        "container_group_name": "worker-v1",
        "worker_image_digest": IMAGE_DIGEST,
        "max_hourly_cost_microusd": 1_500_000,
        "provider_configuration": {"replicas": 0},
    }
    values.update(overrides)
    with pytest.raises(SaladServiceValidationError, match=message):
        SaladDeploymentConfig(**values)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_deployment_versions_are_idempotent_and_replace_current(
    database: Database,
) -> None:
    first_config = SaladDeploymentConfig(
        organization_name="creator-org",
        project_name="production",
        queue_name="generation-v1",
        container_group_name="worker-v1",
        worker_image_digest=IMAGE_DIGEST,
        max_hourly_cost_microusd=1_500_000,
        provider_configuration={"replicas": 0, "resources": {"memory": 16384}},
    )
    second_config = SaladDeploymentConfig(
        organization_name="creator-org",
        project_name="production",
        queue_name="generation-v2",
        container_group_name="worker-v2",
        worker_image_digest=IMAGE_DIGEST,
        max_hourly_cost_microusd=1_500_000,
        provider_configuration={"replicas": 0, "resources": {"memory": 16384}},
    )
    async with database.sessions() as session:
        first = await create_deployment_version(session, first_config, now=NOW)
        replay = await create_deployment_version(session, first_config, now=NOW)
        second = await create_deployment_version(session, second_config, now=NOW)
        await session.commit()

        deployments = list(
            (
                await session.scalars(select(SaladDeployment).order_by(SaladDeployment.version_no))
            ).all()
        )

    assert first.version_no == 1
    assert replay.replayed is True
    assert replay.deployment_id == first.deployment_id
    assert second.version_no == 2
    assert [deployment.is_current for deployment in deployments] == [False, True]
    assert deployments[0].desired_state == DesiredDeploymentState.STOPPED
    assert deployments[0].last_error_code == "superseded_by_new_deployment"
    assert deployments[0].provider_configuration == {
        "replicas": 0,
        "resources": {"memory": 16384},
    }


@pytest.mark.asyncio
async def test_deployment_creation_revalidates_mutable_configuration(
    database: Database,
) -> None:
    provider_configuration: dict[str, JSONValue] = {"replicas": 0}
    config = SaladDeploymentConfig(
        organization_name="creator-org",
        project_name="production",
        queue_name="generation-v1",
        container_group_name="worker-v1",
        worker_image_digest=IMAGE_DIGEST,
        max_hourly_cost_microusd=1_500_000,
        provider_configuration=provider_configuration,
    )
    provider_configuration["api_key"] = "added-after-validation"

    async with database.sessions() as session:
        with pytest.raises(SaladServiceValidationError, match="credentials"):
            await create_deployment_version(session, config, now=NOW)


@pytest.mark.asyncio
async def test_prepare_attempt_and_outbox_are_atomic_idempotent_and_content_free(
    database: Database,
) -> None:
    async with database.sessions() as session:
        context = await seed_context(session)
        rolled_back = await prepare_generation_attempt(
            session,
            generation_job_id=context.job_id,
            salad_deployment_id=context.deployment_id,
            idempotency_key="rolled-back-claim",
            now=NOW,
        )
        assert rolled_back.generation_attempt_id is not None
        await session.rollback()
        assert await session.scalar(select(func.count()).select_from(GenerationAttempt)) == 0
        assert await session.scalar(select(func.count()).select_from(OutboxEvent)) == 0

        first = await prepare_generation_attempt(
            session,
            generation_job_id=context.job_id,
            salad_deployment_id=context.deployment_id,
            idempotency_key="durable-claim",
            now=NOW,
        )
        await session.commit()
        replay = await prepare_generation_attempt(
            session,
            generation_job_id=context.job_id,
            salad_deployment_id=context.deployment_id,
            idempotency_key="durable-claim",
            now=NOW,
        )
        with pytest.raises(SaladServiceConflictError, match="non-terminal"):
            await prepare_generation_attempt(
                session,
                generation_job_id=context.job_id,
                salad_deployment_id=context.deployment_id,
                idempotency_key="different-claim",
                now=NOW,
            )
        event = await session.get(OutboxEvent, first.outbox_event_id)
        attempt = await session.get(GenerationAttempt, first.generation_attempt_id)

    assert replay.replayed is True
    assert replay.generation_attempt_id == first.generation_attempt_id
    assert event is not None
    assert attempt is not None
    assert event.topic == SALAD_JOB_SUBMIT_TOPIC
    assert event.payload == {
        "generation_attempt_id": str(attempt.id),
        "generation_job_id": str(context.job_id),
        "salad_deployment_id": str(context.deployment_id),
    }
    persisted = f"{event.payload!r}{attempt.request_metadata!r}".lower()
    assert "prompt" not in persisted
    assert "https://" not in persisted
    assert "signature" not in persisted


@pytest.mark.asyncio
async def test_submission_issues_grants_immediately_and_never_persists_them(
    database: Database,
) -> None:
    async with database.sessions() as session:
        context = await seed_context(session)
        attempt_id = await prepared_attempt(session, context)
        uploads = FakeUploadIntentProvider()
        client = FakeSaladClient()

        result = await submit_prepared_attempt(
            session,
            client,
            uploads,
            generation_attempt_id=attempt_id,
            webhook_url="https://controller.example.test/webhooks/salad",
            reservation_microusd=500_000,
            now=NOW,
        )
        replay = await submit_prepared_attempt(
            session,
            client,
            uploads,
            generation_attempt_id=attempt_id,
            webhook_url="https://controller.example.test/webhooks/salad",
            reservation_microusd=500_000,
            now=NOW + timedelta(seconds=2),
        )
        attempt = await session.get(GenerationAttempt, attempt_id)

    assert result.disposition == SubmissionDisposition.SUBMITTED
    assert result.mutation_effect == MutationEffect.CONFIRMED
    assert result.attempt_state == GenerationAttemptState.SUBMITTED
    assert result.provider_external_id == str(REMOTE_JOB_ID)
    assert replay.disposition == SubmissionDisposition.ALREADY_RECORDED
    assert len(uploads.calls) == 1
    assert len(client.create_calls) == 1
    assert client.create_calls[0][1] == {
        "generation": {
            "prompt": "private generation prompt",
            "negative_prompt": "private negative prompt",
            "seed": 123,
        },
        "uploads": [
            {
                "output_index": 0,
                "url": SIGNED_UPLOAD_URL,
                "method": "POST",
            }
        ],
    }
    assert attempt is not None
    persisted = f"{attempt.request_metadata!r}{attempt.response_metadata!r}"
    assert SIGNED_UPLOAD_URL not in persisted
    assert "private generation prompt" not in persisted
    assert "provider output" not in persisted


@pytest.mark.parametrize(
    "stale_lifecycle",
    [
        "terminal_job",
        "stop_requested",
        "superseded_version",
        "terminal_release",
        "blocked_release",
    ],
)
@pytest.mark.asyncio
async def test_submission_fences_stale_lifecycle_before_provider_contact(
    database: Database,
    stale_lifecycle: str,
) -> None:
    async with database.sessions() as session:
        context = await seed_context(session)
        attempt_id = await prepared_attempt(session, context)
        job = await session.get(GenerationJob, context.job_id)
        assert job is not None
        version = await session.get(ReleaseVersion, job.release_version_id)
        assert version is not None
        release = await session.get(Release, version.release_id)
        assert release is not None
        if stale_lifecycle == "terminal_job":
            job.state = GenerationState.CANCELLED
        elif stale_lifecycle == "stop_requested":
            session.add(
                AuditEvent(
                    actor="test-owner",
                    action="release.generation_stop_requested",
                    resource_type="release",
                    resource_id=release.id,
                    correlation_id=f"generation-stop:{release.id}",
                    detail={"assets_retained": True},
                    occurred_at=NOW + timedelta(seconds=1),
                )
            )
        elif stale_lifecycle == "superseded_version":
            release.current_version_no = 2
        elif stale_lifecycle == "terminal_release":
            release.phase = ReleasePhase.CANCELLED
        else:
            release.health = ResourceHealth.BLOCKED
        await session.commit()

        uploads = FakeUploadIntentProvider()
        client = FakeSaladClient()
        result = await submit_prepared_attempt(
            session,
            client,
            uploads,
            generation_attempt_id=attempt_id,
            webhook_url="https://controller.example.test/webhooks/salad",
            reservation_microusd=500_000,
            now=NOW + timedelta(seconds=2),
        )
        attempt = await session.get(GenerationAttempt, attempt_id)
        job = await session.get(GenerationJob, context.job_id)

    assert result.disposition == SubmissionDisposition.CANCELLED
    assert result.mutation_effect == MutationEffect.DEFINITELY_NOT_STARTED
    assert not uploads.calls
    assert not client.create_calls
    assert attempt is not None
    assert attempt.state == GenerationAttemptState.FAILED
    assert attempt.error_code == "stale_submission_lifecycle"
    assert job is not None
    assert job.state == GenerationState.CANCELLED


@pytest.mark.asyncio
async def test_timeout_is_unknown_and_is_never_reposted(database: Database) -> None:
    async with database.sessions() as session:
        context = await seed_context(session)
        attempt_id = await prepared_attempt(session, context)
        uploads = FakeUploadIntentProvider()
        client = FakeSaladClient(
            create_error=SaladTimeoutError(f"timeout included sensitive URL {SIGNED_UPLOAD_URL}")
        )

        first = await submit_prepared_attempt(
            session,
            client,
            uploads,
            generation_attempt_id=attempt_id,
            webhook_url="https://controller.example.test/webhooks/salad",
            reservation_microusd=500_000,
            now=NOW,
        )
        second = await submit_prepared_attempt(
            session,
            client,
            uploads,
            generation_attempt_id=attempt_id,
            webhook_url="https://controller.example.test/webhooks/salad",
            reservation_microusd=500_000,
            now=NOW + timedelta(minutes=1),
        )
        attempt = await session.get(GenerationAttempt, attempt_id)

    assert first.disposition == SubmissionDisposition.UNKNOWN
    assert first.mutation_effect == MutationEffect.MAY_HAVE_STARTED
    assert second.disposition == SubmissionDisposition.UNKNOWN
    assert len(uploads.calls) == 1
    assert len(client.create_calls) == 1
    assert attempt is not None
    assert attempt.state == GenerationAttemptState.UNKNOWN
    assert attempt.reservation_released_at is None
    assert SIGNED_UPLOAD_URL not in (attempt.error_detail or "")


@pytest.mark.asyncio
async def test_budget_block_prevents_upload_grants_and_provider_call(
    database: Database,
) -> None:
    async with database.sessions() as session:
        context = await seed_context(session)
        attempt_id = await prepared_attempt(session, context)
        uploads = FakeUploadIntentProvider()
        client = FakeSaladClient()

        result = await submit_prepared_attempt(
            session,
            client,
            uploads,
            generation_attempt_id=attempt_id,
            webhook_url="https://controller.example.test/webhooks/salad",
            reservation_microusd=21_000_000,
            now=NOW,
        )
        attempt = await session.get(GenerationAttempt, attempt_id)
        job = await session.get(GenerationJob, context.job_id)

    assert result.disposition == SubmissionDisposition.BUDGET_BLOCKED
    assert result.mutation_effect == MutationEffect.DEFINITELY_NOT_STARTED
    assert not uploads.calls
    assert not client.create_calls
    assert attempt is not None
    assert attempt.state == GenerationAttemptState.CREATED
    assert attempt.cost_reservation_microusd == 0
    assert job is not None
    assert job.state == GenerationState.RETRY_WAIT


@pytest.mark.asyncio
async def test_submission_rechecks_deployment_kill_switch(database: Database) -> None:
    async with database.sessions() as session:
        context = await seed_context(session)
        attempt_id = await prepared_attempt(session, context)
        deployment = await session.get(SaladDeployment, context.deployment_id)
        assert deployment is not None
        deployment.desired_state = DesiredDeploymentState.STOPPED
        await session.commit()
        replay = await prepare_generation_attempt(
            session,
            generation_job_id=context.job_id,
            salad_deployment_id=context.deployment_id,
            idempotency_key="scheduler-claim-1",
            now=NOW,
        )
        assert replay.replayed is True
        uploads = FakeUploadIntentProvider()
        client = FakeSaladClient()

        with pytest.raises(SaladServiceConflictError, match="stopped"):
            await submit_prepared_attempt(
                session,
                client,
                uploads,
                generation_attempt_id=attempt_id,
                webhook_url="https://controller.example.test/webhooks/salad",
                reservation_microusd=500_000,
                now=NOW,
            )
        await session.rollback()

    assert not uploads.calls
    assert not client.create_calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pending_error_code",
    [
        "provider_autoscaler_repair_pending",
        "provider_image_preparation_pending",
        "provider_start_pending",
    ],
)
async def test_submission_allows_exact_runtime_admission_transitional_state(
    database: Database,
    pending_error_code: str,
) -> None:
    async with database.sessions() as session:
        context = await seed_context(session)
        attempt_id = await prepared_attempt(session, context)
        deployment = await session.get(SaladDeployment, context.deployment_id)
        assert deployment is not None
        deployment.state = SaladDeploymentState.PROVISIONING
        deployment.last_error_code = pending_error_code
        await session.commit()
        uploads = FakeUploadIntentProvider()
        client = FakeSaladClient()

        result = await submit_prepared_attempt(
            session,
            client,
            uploads,
            generation_attempt_id=attempt_id,
            webhook_url="https://controller.example.test/webhooks/salad",
            reservation_microusd=500_000,
            now=NOW,
        )

    assert result.disposition == SubmissionDisposition.SUBMITTED
    assert len(client.create_calls) == 1


@pytest.mark.asyncio
async def test_upload_intent_failure_is_definite_and_releases_reservation(
    database: Database,
) -> None:
    async with database.sessions() as session:
        context = await seed_context(session)
        attempt_id = await prepared_attempt(session, context)
        uploads = FakeUploadIntentProvider(fail=True)
        client = FakeSaladClient()

        result = await submit_prepared_attempt(
            session,
            client,
            uploads,
            generation_attempt_id=attempt_id,
            webhook_url="https://controller.example.test/webhooks/salad",
            reservation_microusd=500_000,
            now=NOW,
        )
        attempt = await session.get(GenerationAttempt, attempt_id)

    assert result.disposition == SubmissionDisposition.RETRY_WAIT
    assert result.mutation_effect == MutationEffect.DEFINITELY_NOT_STARTED
    assert len(uploads.calls) == 1
    assert not client.create_calls
    assert attempt is not None
    assert attempt.state == GenerationAttemptState.FAILED
    assert attempt.reservation_released_at is not None
    assert SIGNED_UPLOAD_URL not in (attempt.error_detail or "")


@pytest.mark.asyncio
async def test_verifying_asset_defers_and_reuses_the_same_prepared_attempt(
    database: Database,
) -> None:
    retry_delay_seconds = 45
    worker_id = "salad-submit-test"
    async with database.sessions() as session:
        context = await seed_context(session)
        attempt_id = await prepared_attempt(session, context)
        await expected_worker_outputs(session, context)
        verifying_asset = await session.scalar(
            select(Asset)
            .where(
                Asset.generation_job_id == context.job_id,
                Asset.kind == AssetKind.RAW_MASTER,
                Asset.output_index == 0,
            )
            .with_for_update()
        )
        assert verifying_asset is not None
        verifying_asset.state = AssetState.VERIFYING
        verifying_asset.verification_lease_owner = "active-collector"
        verifying_asset.verification_lease_expires_at = NOW + timedelta(minutes=5)
        await session.commit()

        first_claim = await claim_outbox_events(
            session,
            worker_id=worker_id,
            limit=1,
            lease_seconds=60,
            topics=(SALAD_JOB_SUBMIT_TOPIC,),
            now=NOW,
        )
        assert len(first_claim) == 1
        event_id = first_claim[0].id
        assert first_claim[0].aggregate_id == attempt_id
        assert first_claim[0].attempt == 1

        deferred_uploads = FakeUploadIntentProvider(
            defer_retry_seconds=retry_delay_seconds,
        )
        client = FakeSaladClient()
        deferred = await submit_prepared_attempt(
            session,
            client,
            deferred_uploads,
            generation_attempt_id=attempt_id,
            webhook_url="https://controller.example.test/webhooks/salad",
            reservation_microusd=500_000,
            now=NOW,
        )
        attempt = await session.get(GenerationAttempt, attempt_id)
        job = await session.get(GenerationJob, context.job_id)
        assert attempt is not None
        assert job is not None
        assert attempt.state == GenerationAttemptState.CREATED
        assert attempt.cost_reservation_microusd == 0
        assert attempt.reservation_released_at is None
        assert attempt.submit_started_at is None
        assert attempt.error_code == "salad_job_input_deferred"
        assert job.state == GenerationState.CLAIMED
        assert job.attempt_count == 1
        assert job.max_attempts == 3
        assert job.retry_at is not None
        assert job.retry_at.replace(tzinfo=UTC) == NOW + timedelta(seconds=retry_delay_seconds)
        assert not client.create_calls

        deferred_event = await defer_unstarted_outbox_event(
            session,
            event_id=event_id,
            worker_id=worker_id,
            retry_not_before=NOW + timedelta(seconds=retry_delay_seconds),
            reason_code="salad_job_input_deferred",
            now=NOW + timedelta(seconds=1),
        )
        assert deferred_event.status == OutboxStatus.PENDING
        event = await session.get(OutboxEvent, event_id)
        assert event is not None
        assert event.status == OutboxStatus.PENDING
        assert event.attempts == 0

        early_claim = await claim_outbox_events(
            session,
            worker_id=worker_id,
            limit=1,
            lease_seconds=60,
            topics=(SALAD_JOB_SUBMIT_TOPIC,),
            now=NOW + timedelta(seconds=retry_delay_seconds - 1),
        )
        assert early_claim == []
        second_claim = await claim_outbox_events(
            session,
            worker_id=worker_id,
            limit=1,
            lease_seconds=60,
            topics=(SALAD_JOB_SUBMIT_TOPIC,),
            now=NOW + timedelta(seconds=retry_delay_seconds),
        )
        assert len(second_claim) == 1
        assert second_claim[0].id == event_id
        assert second_claim[0].aggregate_id == attempt_id
        assert second_claim[0].attempt == 1

        submitted = await submit_prepared_attempt(
            session,
            client,
            FakeUploadIntentProvider(),
            generation_attempt_id=attempt_id,
            webhook_url="https://controller.example.test/webhooks/salad",
            reservation_microusd=500_000,
            now=NOW + timedelta(seconds=retry_delay_seconds),
        )
        await succeed_outbox_event(
            session,
            event_id=event_id,
            worker_id=worker_id,
            now=NOW + timedelta(seconds=retry_delay_seconds + 1),
        )
        await session.refresh(attempt)
        await session.refresh(job)
        await session.refresh(event)
        attempt_total = int(
            await session.scalar(
                select(func.count(GenerationAttempt.id)).where(
                    GenerationAttempt.job_id == context.job_id
                )
            )
            or 0
        )
        actions = list(
            (
                await session.scalars(
                    select(AuditEvent.action).where(
                        AuditEvent.resource_type == "generation_attempt",
                        AuditEvent.resource_id == attempt_id,
                    )
                )
            ).all()
        )

    assert deferred.disposition == SubmissionDisposition.BUDGET_BLOCKED
    assert deferred.mutation_effect == MutationEffect.DEFINITELY_NOT_STARTED
    assert deferred.retry_not_before == NOW + timedelta(seconds=retry_delay_seconds)
    assert len(deferred_uploads.calls) == 1
    assert submitted.disposition == SubmissionDisposition.SUBMITTED
    assert submitted.generation_attempt_id == attempt_id
    assert len(client.create_calls) == 1
    assert attempt.state == GenerationAttemptState.SUBMITTED
    assert job.state == GenerationState.RUNNING
    assert job.attempt_count == 1
    assert job.max_attempts == 3
    assert attempt_total == 1
    assert event.status == OutboxStatus.SUCCEEDED
    assert event.attempts == 1
    assert actions.count("provider_budget.reserved") == 2
    assert actions.count("provider_budget.reservation_released") == 1
    assert actions.count("generation_attempt.job_input_deferred") == 1


@pytest.mark.asyncio
async def test_exhausted_controller_attempts_dead_letter_the_job(
    database: Database,
) -> None:
    async with database.sessions() as session:
        context = await seed_context(session)
        job = await session.get(GenerationJob, context.job_id)
        assert job is not None
        job.max_attempts = 1
        await session.commit()
        attempt_id = await prepared_attempt(session, context)

        await submit_prepared_attempt(
            session,
            FakeSaladClient(),
            FakeUploadIntentProvider(fail=True),
            generation_attempt_id=attempt_id,
            webhook_url="https://controller.example.test/webhooks/salad",
            reservation_microusd=500_000,
            now=NOW,
        )
        await session.refresh(job)

    assert job.state == GenerationState.DEAD_LETTER


@pytest.mark.asyncio
async def test_durable_submitting_marker_is_reconciled_and_never_posted(
    database: Database,
) -> None:
    async with database.sessions() as session:
        context = await seed_context(session)
        attempt_id = await prepared_attempt(session, context)
        await reserve_attempt_budget(
            session,
            provider="salad",
            attempt_id=attempt_id,
            amount_microusd=500_000,
            now=NOW,
        )
        await session.commit()
        uploads = FakeUploadIntentProvider()
        client = FakeSaladClient()

        result = await submit_prepared_attempt(
            session,
            client,
            uploads,
            generation_attempt_id=attempt_id,
            webhook_url="https://controller.example.test/webhooks/salad",
            reservation_microusd=500_000,
            now=NOW + timedelta(seconds=1),
        )

    assert result.disposition == SubmissionDisposition.UNKNOWN
    assert result.mutation_effect == MutationEffect.MAY_HAVE_STARTED
    assert not uploads.calls
    assert not client.create_calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        SaladAPIError(
            status_code=408,
            message="request timeout",
            response_body="",
            request_id=None,
        ),
        SaladAPIError(
            status_code=503,
            message="unavailable",
            response_body="",
            request_id=None,
        ),
        SaladTransportError("connection reset"),
        SaladProtocolError("malformed successful response"),
        RuntimeError("unexpected adapter failure"),
    ],
)
async def test_ambiguous_provider_failures_are_unknown(
    database: Database,
    error: Exception,
) -> None:
    async with database.sessions() as session:
        context = await seed_context(session)
        attempt_id = await prepared_attempt(session, context)
        result = await submit_prepared_attempt(
            session,
            FakeSaladClient(create_error=error),
            FakeUploadIntentProvider(),
            generation_attempt_id=attempt_id,
            webhook_url="https://controller.example.test/webhooks/salad",
            reservation_microusd=500_000,
            now=NOW,
        )

    assert result.disposition == SubmissionDisposition.UNKNOWN
    assert result.mutation_effect == MutationEffect.MAY_HAVE_STARTED


@pytest.mark.asyncio
async def test_mismatched_create_response_is_unknown(database: Database) -> None:
    async with database.sessions() as session:
        context = await seed_context(session)
        attempt_id = await prepared_attempt(session, context)
        result = await submit_prepared_attempt(
            session,
            FakeSaladClient(response_metadata_override={"generation_attempt_id": "wrong"}),
            FakeUploadIntentProvider(),
            generation_attempt_id=attempt_id,
            webhook_url="https://controller.example.test/webhooks/salad",
            reservation_microusd=500_000,
            now=NOW,
        )

    assert result.disposition == SubmissionDisposition.UNKNOWN
    assert result.provider_external_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [400, 422])
async def test_definite_4xx_fails_without_reposting(
    database: Database,
    status_code: int,
) -> None:
    async with database.sessions() as session:
        context = await seed_context(session)
        attempt_id = await prepared_attempt(session, context)
        uploads = FakeUploadIntentProvider()
        client = FakeSaladClient(
            create_error=SaladAPIError(
                status_code=status_code,
                message=f"bad URL {SIGNED_UPLOAD_URL}",
                response_body=f"secret body {SIGNED_UPLOAD_URL}",
                request_id=None,
            )
        )

        result = await submit_prepared_attempt(
            session,
            client,
            uploads,
            generation_attempt_id=attempt_id,
            webhook_url="https://controller.example.test/webhooks/salad",
            reservation_microusd=500_000,
            now=NOW,
        )
        attempt = await session.get(GenerationAttempt, attempt_id)
        job = await session.get(GenerationJob, context.job_id)

    assert result.disposition == SubmissionDisposition.FAILED
    assert result.mutation_effect == MutationEffect.DEFINITELY_NOT_STARTED
    assert attempt is not None
    assert attempt.state == GenerationAttemptState.FAILED
    assert attempt.completed_at is not None
    assert attempt.reservation_released_at is not None
    assert SIGNED_UPLOAD_URL not in (attempt.error_detail or "")
    assert job is not None
    assert job.state == GenerationState.FAILED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("retry_after_seconds", "expected_delay"),
    [(30.0, 30), (None, 60)],
)
async def test_rate_limit_is_retry_wait_with_no_remote_effect(
    database: Database,
    retry_after_seconds: float | None,
    expected_delay: int,
) -> None:
    async with database.sessions() as session:
        context = await seed_context(session)
        attempt_id = await prepared_attempt(session, context)
        client = FakeSaladClient(
            create_error=SaladRateLimitError(
                message="rate limited",
                response_body="",
                request_id=None,
                retry_after_seconds=retry_after_seconds,
            )
        )

        result = await submit_prepared_attempt(
            session,
            client,
            FakeUploadIntentProvider(),
            generation_attempt_id=attempt_id,
            webhook_url="https://controller.example.test/webhooks/salad",
            reservation_microusd=500_000,
            now=NOW,
        )
        attempt = await session.get(GenerationAttempt, attempt_id)
        job = await session.get(GenerationJob, context.job_id)

    assert result.disposition == SubmissionDisposition.RETRY_WAIT
    assert result.mutation_effect == MutationEffect.DEFINITELY_NOT_STARTED
    assert result.retry_not_before == NOW + timedelta(seconds=expected_delay)
    assert attempt is not None
    assert attempt.state == GenerationAttemptState.FAILED
    assert attempt.reservation_released_at is not None
    assert job is not None
    assert job.state == GenerationState.RETRY_WAIT


@pytest.mark.asyncio
async def test_reconciliation_without_a_metadata_match_stays_unknown(
    database: Database,
) -> None:
    async with database.sessions() as session:
        context = await seed_context(session)
        attempt_id = await prepared_attempt(session, context)
        await submit_prepared_attempt(
            session,
            FakeSaladClient(create_error=SaladTimeoutError("timeout")),
            FakeUploadIntentProvider(),
            generation_attempt_id=attempt_id,
            webhook_url="https://controller.example.test/webhooks/salad",
            reservation_microusd=500_000,
            now=NOW,
        )

        result = await reconcile_generation_attempt(
            session,
            FakeSaladClient(list_pages={1: ()}),
            generation_attempt_id=attempt_id,
            list_page_size=1,
            now=NOW + timedelta(minutes=1),
        )

    assert result.source == ReconciliationSource.LIST
    assert result.matched is False
    assert result.error_code == "salad_provider_absence_pending"
    assert result.observation.attempt_state == GenerationAttemptState.UNKNOWN


@pytest.mark.asyncio
@pytest.mark.parametrize("target_page", [3, 8])
async def test_reconciliation_preserves_the_200_job_scan_window(
    database: Database,
    target_page: int,
) -> None:
    async with database.sessions() as session:
        context = await seed_context(session)
        attempt_id = await prepared_attempt(session, context)
        await submit_prepared_attempt(
            session,
            FakeSaladClient(create_error=SaladTimeoutError("timeout")),
            FakeUploadIntentProvider(),
            generation_attempt_id=attempt_id,
            webhook_url="https://controller.example.test/webhooks/salad",
            reservation_microusd=500_000,
            now=NOW,
        )
        attempt = await session.get(GenerationAttempt, attempt_id)
        assert attempt is not None
        metadata: JSONObject = {
            "generation_attempt_id": str(attempt.id),
            "generation_job_id": str(attempt.job_id),
            "submission_key": attempt.submission_key,
            "request_sha256": attempt.request_sha256,
        }
        pages = {
            page_number: tuple(
                remote_job(
                    status=SaladJobStatus.PENDING,
                    metadata={"generation_attempt_id": "another-attempt"},
                    update_time=NOW + timedelta(minutes=1),
                    job_id=uuid4(),
                )
                for _ in range(SALAD_QUEUE_JOB_PAGE_SIZE)
            )
            for page_number in range(1, target_page)
        }
        pages[target_page] = (
            remote_job(
                status=SaladJobStatus.RUNNING,
                metadata=metadata,
                update_time=NOW + timedelta(minutes=1),
            ),
        )
        client = FakeSaladClient(list_pages=pages)

        result = await reconcile_generation_attempt(
            session,
            client,
            generation_attempt_id=attempt_id,
            max_list_pages=8,
            list_page_size=SALAD_QUEUE_JOB_PAGE_SIZE,
            now=NOW + timedelta(minutes=1),
        )
        reconciled = await session.get(GenerationAttempt, attempt_id)

    assert result.matched is True
    assert result.observation.attempt_state == GenerationAttemptState.RUNNING
    assert reconciled is not None
    assert reconciled.provider_external_id == str(REMOTE_JOB_ID)
    assert client.list_calls == [
        ("generation-v1", page_number, SALAD_QUEUE_JOB_PAGE_SIZE)
        for page_number in range(1, target_page + 1)
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected_state", "expected_code"),
    [
        (
            SaladAPIError(
                status_code=503,
                message="unavailable",
                response_body="",
                request_id=None,
            ),
            GenerationAttemptState.SUBMITTED,
            "salad_reconciliation_unavailable",
        ),
        (
            SaladTransportError("connection reset"),
            GenerationAttemptState.SUBMITTED,
            "salad_reconciliation_unavailable",
        ),
        (
            SaladAPIError(
                status_code=404,
                message="not found",
                response_body="",
                request_id=None,
            ),
            GenerationAttemptState.UNKNOWN,
            "salad_provider_absence_pending",
        ),
    ],
)
async def test_reconciliation_errors_never_trigger_a_submission_retry(
    database: Database,
    error: Exception,
    expected_state: GenerationAttemptState,
    expected_code: str,
) -> None:
    async with database.sessions() as session:
        context = await seed_context(session)
        attempt_id = await prepared_attempt(session, context)
        create_client = FakeSaladClient()
        await submit_prepared_attempt(
            session,
            create_client,
            FakeUploadIntentProvider(),
            generation_attempt_id=attempt_id,
            webhook_url="https://controller.example.test/webhooks/salad",
            reservation_microusd=500_000,
            now=NOW,
        )
        reconcile_client = FakeSaladClient(get_error=error)

        result = await reconcile_generation_attempt(
            session,
            reconcile_client,
            generation_attempt_id=attempt_id,
            now=NOW + timedelta(minutes=1),
        )

    assert result.source == ReconciliationSource.GET
    assert result.matched is False
    assert result.error_code == expected_code
    assert result.observation.attempt_state == expected_state
    assert len(create_client.create_calls) == 1
    assert len(reconcile_client.get_calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("remote_status", [SaladJobStatus.PENDING, SaladJobStatus.RUNNING])
async def test_operator_stop_cancels_active_provider_work_and_waits_for_confirmation(
    database: Database,
    remote_status: SaladJobStatus,
) -> None:
    async with database.sessions() as session:
        context = await seed_context(session)
        attempt_id = await prepared_attempt(session, context)
        await submit_prepared_attempt(
            session,
            FakeSaladClient(),
            FakeUploadIntentProvider(),
            generation_attempt_id=attempt_id,
            webhook_url="https://controller.example.test/webhooks/salad",
            reservation_microusd=500_000,
            now=NOW,
        )
        await mark_operator_stop(session, context)
        attempt = await session.get(GenerationAttempt, attempt_id)
        assert attempt is not None
        metadata: JSONObject = {
            "generation_attempt_id": str(attempt.id),
            "generation_job_id": str(attempt.job_id),
            "submission_key": attempt.submission_key,
            "request_sha256": attempt.request_sha256,
        }
        active = remote_job(
            status=remote_status,
            metadata=metadata,
            update_time=NOW + timedelta(minutes=1),
        )
        cancel_client = FakeSaladClient(get_result=active)

        requested = await reconcile_generation_attempt(
            session,
            cancel_client,
            generation_attempt_id=attempt_id,
            now=NOW + timedelta(minutes=2),
        )
        pending_attempt = await session.get(GenerationAttempt, attempt_id)
        pending_job = await session.get(GenerationJob, context.job_id)
        request_audit_count = int(
            await session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(
                    AuditEvent.resource_id == attempt_id,
                    AuditEvent.action == "generation_attempt.operator_stop_cancel_requested",
                )
            )
            or 0
        )

        assert requested.observation.attempt_state == GenerationAttemptState.CANCEL_REQUESTED
        assert requested.observation.generation_job_state == GenerationState.CANCEL_REQUESTED
        assert requested.error_code == OPERATOR_STOP_CANCEL_REQUESTED_ERROR_CODE
        assert cancel_client.cancel_calls == [("generation-v1", str(REMOTE_JOB_ID))]
        assert pending_attempt is not None
        assert pending_attempt.error_code == OPERATOR_STOP_CANCEL_REQUESTED_ERROR_CODE
        assert pending_attempt.reservation_released_at is None
        assert pending_job is not None
        assert pending_job.state == GenerationState.CANCEL_REQUESTED
        assert request_audit_count == 1

        cancelled = remote_job(
            status=SaladJobStatus.CANCELLED,
            metadata=metadata,
            update_time=NOW + timedelta(minutes=3),
        )
        confirmed = await reconcile_generation_attempt(
            session,
            FakeSaladClient(get_result=cancelled),
            generation_attempt_id=attempt_id,
            now=NOW + timedelta(minutes=4),
        )
        final_attempt = await session.get(GenerationAttempt, attempt_id)
        final_job = await session.get(GenerationJob, context.job_id)

    assert confirmed.observation.attempt_state == GenerationAttemptState.CANCELLED
    assert confirmed.observation.generation_job_state == GenerationState.CANCELLED
    assert final_attempt is not None
    assert final_attempt.reservation_released_at is not None
    assert final_job is not None
    assert final_job.state == GenerationState.CANCELLED


@pytest.mark.asyncio
async def test_operator_stop_retries_transient_provider_cancellation(
    database: Database,
) -> None:
    async with database.sessions() as session:
        context = await seed_context(session)
        attempt_id = await prepared_attempt(session, context)
        await submit_prepared_attempt(
            session,
            FakeSaladClient(),
            FakeUploadIntentProvider(),
            generation_attempt_id=attempt_id,
            webhook_url="https://controller.example.test/webhooks/salad",
            reservation_microusd=500_000,
            now=NOW,
        )
        await mark_operator_stop(session, context)
        attempt = await session.get(GenerationAttempt, attempt_id)
        assert attempt is not None
        metadata: JSONObject = {
            "generation_attempt_id": str(attempt.id),
            "generation_job_id": str(attempt.job_id),
            "submission_key": attempt.submission_key,
            "request_sha256": attempt.request_sha256,
        }
        pending = remote_job(
            status=SaladJobStatus.PENDING,
            metadata=metadata,
            update_time=NOW + timedelta(minutes=1),
        )
        unavailable = FakeSaladClient(
            get_result=pending,
            cancel_error=SaladTransportError("connection reset"),
        )

        deferred = await reconcile_generation_attempt(
            session,
            unavailable,
            generation_attempt_id=attempt_id,
            now=NOW + timedelta(minutes=2),
        )
        deferred_attempt = await session.get(GenerationAttempt, attempt_id)
        deferred_job = await session.get(GenerationJob, context.job_id)

        assert deferred.error_code == "operator_generation_stop_cancel_unavailable"
        assert unavailable.cancel_calls == [("generation-v1", str(REMOTE_JOB_ID))]
        assert deferred_attempt is not None
        assert deferred_attempt.state == GenerationAttemptState.SUBMITTED
        assert deferred_attempt.reservation_released_at is None
        assert deferred_job is not None
        assert deferred_job.state == GenerationState.RUNNING

        retry = FakeSaladClient(get_result=pending)
        requested = await reconcile_generation_attempt(
            session,
            retry,
            generation_attempt_id=attempt_id,
            now=NOW + timedelta(minutes=3),
        )

    assert requested.observation.attempt_state == GenerationAttemptState.CANCEL_REQUESTED
    assert requested.observation.generation_job_state == GenerationState.CANCEL_REQUESTED
    assert retry.cancel_calls == [("generation-v1", str(REMOTE_JOB_ID))]


@pytest.mark.asyncio
async def test_operator_stop_delete_not_found_keeps_exact_provider_attempt_pending(
    database: Database,
) -> None:
    async with database.sessions() as session:
        context = await seed_context(session)
        attempt_id = await prepared_attempt(session, context)
        await submit_prepared_attempt(
            session,
            FakeSaladClient(),
            FakeUploadIntentProvider(),
            generation_attempt_id=attempt_id,
            webhook_url="https://controller.example.test/webhooks/salad",
            reservation_microusd=500_000,
            now=NOW,
        )
        await mark_operator_stop(session, context)
        attempt = await session.get(GenerationAttempt, attempt_id)
        assert attempt is not None
        metadata: JSONObject = {
            "generation_attempt_id": str(attempt.id),
            "generation_job_id": str(attempt.job_id),
            "submission_key": attempt.submission_key,
            "request_sha256": attempt.request_sha256,
        }
        pending = remote_job(
            status=SaladJobStatus.PENDING,
            metadata=metadata,
            update_time=NOW + timedelta(minutes=1),
        )
        client = FakeSaladClient(
            get_result=pending,
            cancel_error=SaladAPIError(
                status_code=404,
                message="not found",
                response_body="",
                request_id=None,
            ),
        )

        result = await reconcile_generation_attempt(
            session,
            client,
            generation_attempt_id=attempt_id,
            now=NOW + timedelta(minutes=2),
        )
        deferred_attempt = await session.get(GenerationAttempt, attempt_id)
        deferred_job = await session.get(GenerationJob, context.job_id)

    assert result.observation.attempt_state == GenerationAttemptState.SUBMITTED
    assert result.observation.generation_job_state == GenerationState.RUNNING
    assert result.error_code == "operator_generation_stop_cancel_unavailable"
    assert deferred_attempt is not None
    assert deferred_attempt.provider_external_id == str(REMOTE_JOB_ID)
    assert deferred_attempt.provider_state == SaladJobStatus.PENDING.value
    assert deferred_attempt.reservation_released_at is None
    assert deferred_job is not None
    assert deferred_job.state == GenerationState.RUNNING


@pytest.mark.asyncio
async def test_operator_stop_get_not_found_requires_three_consecutive_confirmations(
    database: Database,
) -> None:
    async with database.sessions() as session:
        context = await seed_context(session)
        attempt_id = await prepared_attempt(session, context)
        await submit_prepared_attempt(
            session,
            FakeSaladClient(),
            FakeUploadIntentProvider(),
            generation_attempt_id=attempt_id,
            webhook_url="https://controller.example.test/webhooks/salad",
            reservation_microusd=500_000,
            now=NOW,
        )
        await mark_operator_stop(session, context)

        not_found = SaladAPIError(
            status_code=404,
            message="not found",
            response_body="",
            request_id=None,
        )
        first = await reconcile_generation_attempt(
            session,
            FakeSaladClient(get_error=not_found),
            generation_attempt_id=attempt_id,
            now=NOW + timedelta(minutes=2),
        )
        first_attempt = await session.get(GenerationAttempt, attempt_id)
        assert first_attempt is not None
        assert first_attempt.reservation_released_at is None
        assert first_attempt.response_metadata is not None
        first_tracker = first_attempt.response_metadata["operator_stop_absence_confirmation"]
        assert isinstance(first_tracker, dict)
        assert first_tracker["source"] == ReconciliationSource.GET.value
        assert first_tracker["count"] == 1

        second = await reconcile_generation_attempt(
            session,
            FakeSaladClient(get_error=not_found),
            generation_attempt_id=attempt_id,
            now=NOW + timedelta(minutes=3),
        )
        third = await reconcile_generation_attempt(
            session,
            FakeSaladClient(get_error=not_found),
            generation_attempt_id=attempt_id,
            now=NOW + timedelta(minutes=4),
        )
        final_attempt = await session.get(GenerationAttempt, attempt_id)
        final_job = await session.get(GenerationJob, context.job_id)

    assert first.source == ReconciliationSource.GET
    assert first.matched is False
    assert first.observation.attempt_state == GenerationAttemptState.UNKNOWN
    assert first.observation.generation_job_state == GenerationState.UNKNOWN
    assert first.error_code == "operator_generation_stop_provider_absence_pending"
    assert second.error_code == "operator_generation_stop_provider_absence_pending"
    assert second.observation.attempt_state == GenerationAttemptState.UNKNOWN
    assert third.observation.attempt_state == GenerationAttemptState.CANCELLED
    assert third.observation.generation_job_state == GenerationState.CANCELLED
    assert third.error_code == "operator_generation_stop_provider_absent"
    assert final_attempt is not None
    assert final_attempt.reservation_released_at is not None
    assert final_job is not None
    assert final_job.state == GenerationState.CANCELLED


@pytest.mark.asyncio
async def test_operator_stop_list_absence_retires_after_three_confirmations(
    database: Database,
) -> None:
    async with database.sessions() as session:
        context = await seed_context(session)
        attempt_id = await prepared_attempt(session, context)
        await submit_prepared_attempt(
            session,
            FakeSaladClient(create_error=SaladTimeoutError("timeout")),
            FakeUploadIntentProvider(),
            generation_attempt_id=attempt_id,
            webhook_url="https://controller.example.test/webhooks/salad",
            reservation_microusd=500_000,
            now=NOW,
        )
        await mark_operator_stop(session, context)

        first = await reconcile_generation_attempt(
            session,
            FakeSaladClient(list_pages={1: ()}),
            generation_attempt_id=attempt_id,
            list_page_size=SALAD_QUEUE_JOB_PAGE_SIZE,
            now=NOW + timedelta(minutes=1),
        )
        second = await reconcile_generation_attempt(
            session,
            FakeSaladClient(list_pages={1: ()}),
            generation_attempt_id=attempt_id,
            list_page_size=SALAD_QUEUE_JOB_PAGE_SIZE,
            now=NOW + timedelta(minutes=2),
        )
        third = await reconcile_generation_attempt(
            session,
            FakeSaladClient(list_pages={1: ()}),
            generation_attempt_id=attempt_id,
            list_page_size=SALAD_QUEUE_JOB_PAGE_SIZE,
            now=NOW + timedelta(minutes=3),
        )
        final_attempt = await session.get(GenerationAttempt, attempt_id)
        final_job = await session.get(GenerationJob, context.job_id)
        confirmation_count = int(
            await session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(
                    AuditEvent.resource_id == attempt_id,
                    AuditEvent.action
                    == "generation_attempt.operator_stop_provider_absence_observed",
                )
            )
            or 0
        )

    assert first.error_code == "operator_generation_stop_provider_absence_pending"
    assert second.error_code == "operator_generation_stop_provider_absence_pending"
    assert third.error_code == "operator_generation_stop_provider_absent"
    assert third.observation.attempt_state == GenerationAttemptState.FAILED
    assert third.observation.generation_job_state == GenerationState.CANCELLED
    assert final_attempt is not None
    assert final_attempt.reservation_released_at is not None
    assert final_job is not None
    assert final_job.state == GenerationState.CANCELLED
    assert confirmation_count == 3


@pytest.mark.asyncio
async def test_operator_stop_more_than_200_jobs_is_inconclusive_and_does_not_count_absence(
    database: Database,
) -> None:
    async with database.sessions() as session:
        context = await seed_context(session)
        attempt_id = await prepared_attempt(session, context)
        await submit_prepared_attempt(
            session,
            FakeSaladClient(create_error=SaladTimeoutError("timeout")),
            FakeUploadIntentProvider(),
            generation_attempt_id=attempt_id,
            webhook_url="https://controller.example.test/webhooks/salad",
            reservation_microusd=500_000,
            now=NOW,
        )
        await mark_operator_stop(session, context)
        full_pages = {
            page_number: tuple(
                remote_job(
                    status=SaladJobStatus.PENDING,
                    metadata={"generation_attempt_id": "another-attempt"},
                    update_time=NOW + timedelta(minutes=1),
                    job_id=uuid4(),
                )
                for _ in range(SALAD_QUEUE_JOB_PAGE_SIZE)
            )
            for page_number in range(1, 9)
        }
        bounded_client = FakeSaladClient(list_pages=full_pages)

        inconclusive = await reconcile_generation_attempt(
            session,
            bounded_client,
            generation_attempt_id=attempt_id,
            max_list_pages=8,
            list_page_size=SALAD_QUEUE_JOB_PAGE_SIZE,
            now=NOW + timedelta(minutes=1),
        )
        after_inconclusive = await session.get(GenerationAttempt, attempt_id)
        assert after_inconclusive is not None
        assert after_inconclusive.response_metadata is None or (
            "operator_stop_absence_confirmation" not in after_inconclusive.response_metadata
        )

        first_exhaustive_miss = await reconcile_generation_attempt(
            session,
            FakeSaladClient(list_pages={1: ()}),
            generation_attempt_id=attempt_id,
            list_page_size=SALAD_QUEUE_JOB_PAGE_SIZE,
            now=NOW + timedelta(minutes=2),
        )
        after_miss = await session.get(GenerationAttempt, attempt_id)
        assert after_miss is not None
        assert after_miss.response_metadata is not None
        tracker = after_miss.response_metadata["operator_stop_absence_confirmation"]
        assert isinstance(tracker, dict)

    assert inconclusive.error_code == "operator_generation_stop_provider_scan_inconclusive"
    assert inconclusive.observation.attempt_state == GenerationAttemptState.UNKNOWN
    assert bounded_client.list_calls == [
        ("generation-v1", page_number, SALAD_QUEUE_JOB_PAGE_SIZE) for page_number in range(1, 9)
    ]
    assert first_exhaustive_miss.error_code == ("operator_generation_stop_provider_absence_pending")
    assert tracker["source"] == ReconciliationSource.LIST.value
    assert tracker["count"] == 1


@pytest.mark.asyncio
async def test_operator_stop_positive_match_resets_prior_misses_before_get_absence(
    database: Database,
) -> None:
    async with database.sessions() as session:
        context = await seed_context(session)
        attempt_id = await prepared_attempt(session, context)
        await submit_prepared_attempt(
            session,
            FakeSaladClient(create_error=SaladTimeoutError("timeout")),
            FakeUploadIntentProvider(),
            generation_attempt_id=attempt_id,
            webhook_url="https://controller.example.test/webhooks/salad",
            reservation_microusd=500_000,
            now=NOW,
        )
        await mark_operator_stop(session, context)

        for minute in (1, 2):
            miss = await reconcile_generation_attempt(
                session,
                FakeSaladClient(list_pages={1: ()}),
                generation_attempt_id=attempt_id,
                list_page_size=SALAD_QUEUE_JOB_PAGE_SIZE,
                now=NOW + timedelta(minutes=minute),
            )
            assert miss.error_code == "operator_generation_stop_provider_absence_pending"

        attempt = await session.get(GenerationAttempt, attempt_id)
        assert attempt is not None
        metadata: JSONObject = {
            "generation_attempt_id": str(attempt.id),
            "generation_job_id": str(attempt.job_id),
            "submission_key": attempt.submission_key,
            "request_sha256": attempt.request_sha256,
        }
        pending = remote_job(
            status=SaladJobStatus.PENDING,
            metadata=metadata,
            update_time=NOW + timedelta(minutes=3),
        )
        match_then_transport_failure = await reconcile_generation_attempt(
            session,
            FakeSaladClient(
                list_pages={1: (pending,)},
                cancel_error=SaladTransportError("connection reset"),
            ),
            generation_attempt_id=attempt_id,
            list_page_size=SALAD_QUEUE_JOB_PAGE_SIZE,
            now=NOW + timedelta(minutes=3),
        )
        after_match = await session.get(GenerationAttempt, attempt_id)
        assert after_match is not None
        assert after_match.provider_external_id == str(REMOTE_JOB_ID)
        assert after_match.response_metadata is not None
        assert "operator_stop_absence_confirmation" not in after_match.response_metadata
        assert after_match.reservation_released_at is None

        one_exact_get_miss = await reconcile_generation_attempt(
            session,
            FakeSaladClient(
                get_error=SaladAPIError(
                    status_code=404,
                    message="not found",
                    response_body="",
                    request_id=None,
                )
            ),
            generation_attempt_id=attempt_id,
            now=NOW + timedelta(minutes=4),
        )
        after_get_miss = await session.get(GenerationAttempt, attempt_id)
        final_job = await session.get(GenerationJob, context.job_id)

    assert match_then_transport_failure.error_code == (
        "operator_generation_stop_cancel_unavailable"
    )
    assert one_exact_get_miss.source == ReconciliationSource.GET
    assert one_exact_get_miss.error_code == "operator_generation_stop_provider_absence_pending"
    assert one_exact_get_miss.observation.attempt_state == GenerationAttemptState.UNKNOWN
    assert after_get_miss is not None
    assert after_get_miss.response_metadata is not None
    get_tracker = after_get_miss.response_metadata["operator_stop_absence_confirmation"]
    assert isinstance(get_tracker, dict)
    assert get_tracker["source"] == ReconciliationSource.GET.value
    assert get_tracker["count"] == 1
    assert after_get_miss.reservation_released_at is None
    assert final_job is not None
    assert final_job.state == GenerationState.UNKNOWN


@pytest.mark.asyncio
async def test_attempt_watchdog_cancels_then_retries_only_after_provider_confirmation(
    database: Database,
) -> None:
    async with database.sessions() as session:
        context = await seed_context(session)
        attempt_id = await prepared_attempt(session, context)
        await submit_prepared_attempt(
            session,
            FakeSaladClient(),
            FakeUploadIntentProvider(),
            generation_attempt_id=attempt_id,
            webhook_url="https://controller.example.test/webhooks/salad",
            reservation_microusd=500_000,
            now=NOW,
        )
        attempt = await session.get(GenerationAttempt, attempt_id)
        assert attempt is not None
        metadata: JSONObject = {
            "generation_attempt_id": str(attempt.id),
            "generation_job_id": str(attempt.job_id),
            "submission_key": attempt.submission_key,
            "request_sha256": attempt.request_sha256,
        }
        started = remote_job(
            status=SaladJobStatus.RUNNING,
            metadata=metadata,
            update_time=NOW + timedelta(minutes=1),
        )
        cancel_client = FakeSaladClient(get_result=started)

        started_result = await reconcile_generation_attempt(
            session,
            cancel_client,
            generation_attempt_id=attempt_id,
            attempt_watchdog_seconds=105 * 60,
            now=NOW + timedelta(minutes=1),
        )
        assert started_result.observation.attempt_state == GenerationAttemptState.RUNNING
        assert not cancel_client.cancel_calls

        cancel_client.get_result = remote_job(
            status=SaladJobStatus.RUNNING,
            metadata=metadata,
            update_time=NOW + timedelta(minutes=106),
        )

        cancel_requested = await reconcile_generation_attempt(
            session,
            cancel_client,
            generation_attempt_id=attempt_id,
            attempt_watchdog_seconds=105 * 60,
            now=NOW + timedelta(minutes=106),
        )
        pending_attempt = await session.get(GenerationAttempt, attempt_id)
        pending_job = await session.get(GenerationJob, context.job_id)

        assert cancel_requested.observation.attempt_state == (
            GenerationAttemptState.CANCEL_REQUESTED
        )
        assert cancel_requested.error_code == (SALAD_ATTEMPT_WATCHDOG_CANCEL_REQUESTED_ERROR_CODE)
        assert cancel_client.cancel_calls == [("generation-v1", str(REMOTE_JOB_ID))]
        assert pending_attempt is not None
        assert pending_attempt.error_code == (SALAD_ATTEMPT_WATCHDOG_CANCEL_REQUESTED_ERROR_CODE)
        assert pending_attempt.response_metadata is not None
        assert pending_attempt.response_metadata["watchdog_reason"] == ("runtime_envelope_expired")
        assert pending_attempt.reservation_released_at is None
        assert pending_job is not None
        assert pending_job.state == GenerationState.RUNNING

        cancelled = remote_job(
            status=SaladJobStatus.CANCELLED,
            metadata=metadata,
            update_time=NOW + timedelta(minutes=107),
        )
        confirmed = await reconcile_generation_attempt(
            session,
            FakeSaladClient(get_result=cancelled),
            generation_attempt_id=attempt_id,
            attempt_watchdog_seconds=105 * 60,
            now=NOW + timedelta(minutes=108),
        )
        final_attempt = await session.get(GenerationAttempt, attempt_id)
        retrying_job = await session.get(GenerationJob, context.job_id)

    assert confirmed.observation.attempt_state == GenerationAttemptState.FAILED
    assert confirmed.observation.generation_job_state == GenerationState.RETRY_WAIT
    assert final_attempt is not None
    assert final_attempt.error_code == SALAD_ATTEMPT_WATCHDOG_EXPIRED_ERROR_CODE
    assert final_attempt.reservation_released_at is not None
    assert retrying_job is not None
    assert retrying_job.state == GenerationState.RETRY_WAIT
    assert retrying_job.last_error_code == SALAD_ATTEMPT_WATCHDOG_EXPIRED_ERROR_CODE


@pytest.mark.asyncio
async def test_attempt_watchdog_ignores_queue_wait_and_uses_running_deadline(
    database: Database,
) -> None:
    async with database.sessions() as session:
        context = await seed_context(session)
        attempt_id = await prepared_attempt(session, context)
        await submit_prepared_attempt(
            session,
            FakeSaladClient(),
            FakeUploadIntentProvider(),
            generation_attempt_id=attempt_id,
            webhook_url="https://controller.example.test/webhooks/salad",
            reservation_microusd=500_000,
            now=NOW,
        )
        attempt = await session.get(GenerationAttempt, attempt_id)
        assert attempt is not None
        metadata: JSONObject = {
            "generation_attempt_id": str(attempt.id),
            "generation_job_id": str(attempt.job_id),
            "submission_key": attempt.submission_key,
            "request_sha256": attempt.request_sha256,
        }
        client = FakeSaladClient(
            get_result=remote_job(
                status=SaladJobStatus.RUNNING,
                metadata=metadata,
                update_time=NOW + timedelta(minutes=100),
            )
        )

        started = await reconcile_generation_attempt(
            session,
            client,
            generation_attempt_id=attempt_id,
            attempt_watchdog_seconds=105 * 60,
            now=NOW + timedelta(minutes=100),
        )
        assert started.observation.attempt_state == GenerationAttemptState.RUNNING
        assert not client.cancel_calls

        client.get_result = remote_job(
            status=SaladJobStatus.RUNNING,
            metadata=metadata,
            update_time=NOW + timedelta(minutes=100),
        )
        result = await reconcile_generation_attempt(
            session,
            client,
            generation_attempt_id=attempt_id,
            attempt_watchdog_seconds=105 * 60,
            now=NOW + timedelta(minutes=204, seconds=59),
        )

    assert result.observation.attempt_state == GenerationAttemptState.RUNNING
    assert not client.cancel_calls


@pytest.mark.asyncio
async def test_attempt_watchdog_still_bounds_pending_queue_wait(
    database: Database,
) -> None:
    async with database.sessions() as session:
        context = await seed_context(session)
        attempt_id = await prepared_attempt(session, context)
        await submit_prepared_attempt(
            session,
            FakeSaladClient(),
            FakeUploadIntentProvider(),
            generation_attempt_id=attempt_id,
            webhook_url="https://controller.example.test/webhooks/salad",
            reservation_microusd=500_000,
            now=NOW,
        )
        attempt = await session.get(GenerationAttempt, attempt_id)
        assert attempt is not None
        enable_runtime_admission(attempt)
        await session.commit()
        metadata: JSONObject = {
            "generation_attempt_id": str(attempt.id),
            "generation_job_id": str(attempt.job_id),
            "submission_key": attempt.submission_key,
            "request_sha256": attempt.request_sha256,
        }
        client = FakeSaladClient(
            get_result=remote_job(
                status=SaladJobStatus.PENDING,
                metadata=metadata,
                update_time=NOW + timedelta(minutes=1),
            )
        )

        still_pending = await reconcile_generation_attempt(
            session,
            client,
            generation_attempt_id=attempt_id,
            attempt_watchdog_seconds=105 * 60,
            output_progress_watchdog_seconds=10 * 60,
            now=NOW + timedelta(minutes=15),
        )
        assert still_pending.observation.attempt_state == GenerationAttemptState.SUBMITTED
        assert client.cancel_calls == []

        result = await reconcile_generation_attempt(
            session,
            client,
            generation_attempt_id=attempt_id,
            attempt_watchdog_seconds=105 * 60,
            output_progress_watchdog_seconds=10 * 60,
            now=NOW + timedelta(minutes=106),
        )

    assert result.observation.attempt_state == GenerationAttemptState.CANCEL_REQUESTED
    assert client.cancel_calls == [("generation-v1", str(REMOTE_JOB_ID))]


@pytest.mark.asyncio
async def test_output_progress_watchdog_uses_latest_provider_run_epoch_after_requeue(
    database: Database,
) -> None:
    async with database.sessions() as session:
        context = await seed_context(session)
        attempt_id = await prepared_attempt(session, context)
        await submit_prepared_attempt(
            session,
            FakeSaladClient(),
            FakeUploadIntentProvider(),
            generation_attempt_id=attempt_id,
            webhook_url="https://controller.example.test/webhooks/salad",
            reservation_microusd=500_000,
            now=NOW,
        )
        attempt = await session.get(GenerationAttempt, attempt_id)
        assert attempt is not None
        enable_runtime_admission(attempt)
        await session.commit()
        provider_metadata: JSONObject = {
            "generation_attempt_id": str(attempt.id),
            "generation_job_id": str(attempt.job_id),
            "submission_key": attempt.submission_key,
            "request_sha256": attempt.request_sha256,
        }
        first_started_at = NOW + timedelta(minutes=1)
        first_events = (
            SaladJobEvent(action="created", time=NOW),
            SaladJobEvent(action="started", time=first_started_at),
        )
        client = FakeSaladClient(
            get_result=remote_job(
                status=SaladJobStatus.RUNNING,
                metadata=provider_metadata,
                update_time=first_started_at,
                events=first_events,
            )
        )
        await reconcile_generation_attempt(
            session,
            client,
            generation_attempt_id=attempt_id,
            attempt_watchdog_seconds=105 * 60,
            output_progress_watchdog_seconds=10 * 60,
            now=first_started_at,
        )

        failed_at = NOW + timedelta(minutes=2)
        failed_events = (*first_events, SaladJobEvent(action="failed", time=failed_at))
        client.get_result = remote_job(
            status=SaladJobStatus.PENDING,
            metadata=provider_metadata,
            update_time=failed_at,
            events=failed_events,
        )
        requeued = await reconcile_generation_attempt(
            session,
            client,
            generation_attempt_id=attempt_id,
            attempt_watchdog_seconds=105 * 60,
            output_progress_watchdog_seconds=10 * 60,
            now=NOW + timedelta(minutes=20),
        )
        assert requeued.observation.attempt_state == GenerationAttemptState.RUNNING
        assert client.cancel_calls == []

        restarted_at = NOW + timedelta(minutes=21)
        restarted_events = (*failed_events, SaladJobEvent(action="started", time=restarted_at))
        client.get_result = remote_job(
            status=SaladJobStatus.RUNNING,
            metadata=provider_metadata,
            update_time=restarted_at,
            events=restarted_events,
        )
        restarted = await reconcile_generation_attempt(
            session,
            client,
            generation_attempt_id=attempt_id,
            attempt_watchdog_seconds=105 * 60,
            output_progress_watchdog_seconds=10 * 60,
            now=restarted_at,
        )
        restarted_attempt = await session.get(GenerationAttempt, attempt_id)
        assert restarted.observation.attempt_state == GenerationAttemptState.RUNNING
        assert restarted_attempt is not None
        assert restarted_attempt.started_at is not None
        assert restarted_attempt.started_at.replace(tzinfo=UTC) == first_started_at
        assert (
            restarted_attempt.response_metadata["provider_run_epoch_started_at"]
            == restarted_at.isoformat()
        )
        assert client.cancel_calls == []

        # An event outside the provider create/update interval cannot keep resetting
        # the short timer. The original started_at still bounds the hard envelope.
        client.get_result = remote_job(
            status=SaladJobStatus.RUNNING,
            metadata=provider_metadata,
            update_time=restarted_at,
            events=(
                *restarted_events,
                SaladJobEvent(action="started", time=NOW + timedelta(minutes=40)),
            ),
        )
        still_running = await reconcile_generation_attempt(
            session,
            client,
            generation_attempt_id=attempt_id,
            attempt_watchdog_seconds=105 * 60,
            output_progress_watchdog_seconds=10 * 60,
            now=NOW + timedelta(minutes=30, seconds=59),
        )
        assert still_running.observation.attempt_state == GenerationAttemptState.RUNNING
        assert client.cancel_calls == []

        stalled = await reconcile_generation_attempt(
            session,
            client,
            generation_attempt_id=attempt_id,
            attempt_watchdog_seconds=105 * 60,
            output_progress_watchdog_seconds=10 * 60,
            now=NOW + timedelta(minutes=31),
        )
        cancelled_attempt = await session.get(GenerationAttempt, attempt_id)

    assert stalled.observation.attempt_state == GenerationAttemptState.CANCEL_REQUESTED
    assert client.cancel_calls == [("generation-v1", str(REMOTE_JOB_ID))]
    assert cancelled_attempt is not None
    assert cancelled_attempt.response_metadata["watchdog_reason"] == (
        "accepted_output_progress_stalled"
    )
    assert cancelled_attempt.response_metadata["watchdog_anchor_at"] == restarted_at.isoformat()


@pytest.mark.asyncio
async def test_hard_watchdog_bounds_requeued_pending_job_with_older_provider_update(
    database: Database,
) -> None:
    async with database.sessions() as session:
        context = await seed_context(session)
        attempt_id = await prepared_attempt(session, context)
        await submit_prepared_attempt(
            session,
            FakeSaladClient(),
            FakeUploadIntentProvider(),
            generation_attempt_id=attempt_id,
            webhook_url="https://controller.example.test/webhooks/salad",
            reservation_microusd=500_000,
            now=NOW,
        )
        attempt = await session.get(GenerationAttempt, attempt_id)
        assert attempt is not None
        enable_runtime_admission(attempt)
        await session.commit()
        provider_metadata: JSONObject = {
            "generation_attempt_id": str(attempt.id),
            "generation_job_id": str(attempt.job_id),
            "submission_key": attempt.submission_key,
            "request_sha256": attempt.request_sha256,
        }
        first_started_at = NOW + timedelta(minutes=1)
        first_events = (
            SaladJobEvent(action="created", time=NOW),
            SaladJobEvent(action="started", time=first_started_at),
        )
        client = FakeSaladClient(
            get_result=remote_job(
                status=SaladJobStatus.RUNNING,
                metadata=provider_metadata,
                update_time=first_started_at,
                events=first_events,
            )
        )
        await reconcile_generation_attempt(
            session,
            client,
            generation_attempt_id=attempt_id,
            attempt_watchdog_seconds=105 * 60,
            output_progress_watchdog_seconds=10 * 60,
            now=first_started_at,
        )

        # Salad's requeued PENDING snapshot can regress both state and its
        # provider update clock. Exact ID+metadata and the locked hard deadline,
        # not that older clock, fence the eventual DELETE.
        failed_at = NOW + timedelta(seconds=30)
        client.get_result = remote_job(
            status=SaladJobStatus.PENDING,
            metadata=provider_metadata,
            update_time=failed_at,
            events=(*first_events, SaladJobEvent(action="failed", time=failed_at)),
        )
        still_requeued = await reconcile_generation_attempt(
            session,
            client,
            generation_attempt_id=attempt_id,
            attempt_watchdog_seconds=105 * 60,
            output_progress_watchdog_seconds=10 * 60,
            now=NOW + timedelta(minutes=15),
        )
        assert still_requeued.observation.attempt_state == GenerationAttemptState.RUNNING
        assert client.cancel_calls == []

        bounded = await reconcile_generation_attempt(
            session,
            client,
            generation_attempt_id=attempt_id,
            attempt_watchdog_seconds=105 * 60,
            output_progress_watchdog_seconds=10 * 60,
            now=NOW + timedelta(minutes=106),
        )
        cancelled_attempt = await session.get(GenerationAttempt, attempt_id)

    assert bounded.observation.attempt_state == GenerationAttemptState.CANCEL_REQUESTED
    assert client.cancel_calls == [("generation-v1", str(REMOTE_JOB_ID))]
    assert cancelled_attempt is not None
    assert cancelled_attempt.response_metadata["watchdog_reason"] == "runtime_envelope_expired"


@pytest.mark.asyncio
async def test_output_watchdog_bounds_future_provider_stored_and_progress_timestamps(
    database: Database,
) -> None:
    async with database.sessions() as session:
        context = await seed_context(session)
        attempt_id = await prepared_attempt(session, context)
        await submit_prepared_attempt(
            session,
            FakeSaladClient(),
            FakeUploadIntentProvider(),
            generation_attempt_id=attempt_id,
            webhook_url="https://controller.example.test/webhooks/salad",
            reservation_microusd=500_000,
            now=NOW,
        )
        attempt = await session.get(GenerationAttempt, attempt_id)
        assert attempt is not None
        enable_runtime_admission(attempt)
        retained_asset_id, _ = await add_progress_watchdog_assets(session, context)
        await session.commit()
        provider_metadata: JSONObject = {
            "generation_attempt_id": str(attempt.id),
            "generation_job_id": str(attempt.job_id),
            "submission_key": attempt.submission_key,
            "request_sha256": attempt.request_sha256,
        }
        far_future = datetime.max.replace(tzinfo=UTC)
        observed_start = NOW + timedelta(minutes=1)
        client = FakeSaladClient(
            get_result=remote_job(
                status=SaladJobStatus.RUNNING,
                metadata=provider_metadata,
                update_time=far_future,
                events=(
                    SaladJobEvent(action="created", time=NOW),
                    SaladJobEvent(action="started", time=far_future),
                ),
            )
        )
        await reconcile_generation_attempt(
            session,
            client,
            generation_attempt_id=attempt_id,
            attempt_watchdog_seconds=30 * 60,
            output_progress_watchdog_seconds=10 * 60,
            now=observed_start,
        )
        attempt = await session.get(GenerationAttempt, attempt_id)
        retained = await session.get(Asset, retained_asset_id)
        assert attempt is not None
        assert retained is not None
        assert attempt.last_observed_at is not None
        assert attempt.last_observed_at.replace(tzinfo=UTC) == observed_start
        assert attempt.started_at is not None
        assert attempt.started_at.replace(tzinfo=UTC) == observed_start
        assert attempt.response_metadata["provider_update_time"] == observed_start.isoformat()

        poisoned_metadata = dict(attempt.response_metadata)
        poisoned_metadata["provider_run_epoch_started_at"] = far_future.isoformat()
        attempt.response_metadata = poisoned_metadata
        attempt.started_at = far_future
        attempt.last_observed_at = far_future
        retained.available_at = far_future
        retained_metadata = dict(retained.asset_metadata)
        retained_metadata.update(
            {
                "staging_cleanup": "completed",
                "staging_cleaned_at": far_future.isoformat(),
            }
        )
        retained.asset_metadata = retained_metadata
        await session.commit()

        bounded = await reconcile_generation_attempt(
            session,
            client,
            generation_attempt_id=attempt_id,
            attempt_watchdog_seconds=30 * 60,
            output_progress_watchdog_seconds=10 * 60,
            now=NOW + timedelta(minutes=14),
        )
        cancelled_attempt = await session.get(GenerationAttempt, attempt_id)

    assert bounded.observation.attempt_state == GenerationAttemptState.CANCEL_REQUESTED
    assert client.cancel_calls == [("generation-v1", str(REMOTE_JOB_ID))]
    assert cancelled_attempt is not None
    assert cancelled_attempt.response_metadata["watchdog_reason"] == (
        "accepted_output_progress_stalled"
    )
    assert cancelled_attempt.response_metadata["watchdog_anchor_at"] != far_future.isoformat()
    assert (
        cancelled_attempt.response_metadata["provider_update_time"]
        == (NOW + timedelta(minutes=14)).isoformat()
    )


@pytest.mark.asyncio
async def test_hard_watchdog_deadline_overflow_fails_closed(
    database: Database,
) -> None:
    async with database.sessions() as session:
        context = await seed_context(session)
        attempt_id = await prepared_attempt(session, context)
        await submit_prepared_attempt(
            session,
            FakeSaladClient(),
            FakeUploadIntentProvider(),
            generation_attempt_id=attempt_id,
            webhook_url="https://controller.example.test/webhooks/salad",
            reservation_microusd=500_000,
            now=NOW,
        )
        attempt = await session.get(GenerationAttempt, attempt_id)
        assert attempt is not None
        provider_metadata: JSONObject = {
            "generation_attempt_id": str(attempt.id),
            "generation_job_id": str(attempt.job_id),
            "submission_key": attempt.submission_key,
            "request_sha256": attempt.request_sha256,
        }
        latest_time = datetime.max.replace(tzinfo=UTC)
        client = FakeSaladClient(
            get_result=remote_job(
                status=SaladJobStatus.RUNNING,
                metadata=provider_metadata,
                update_time=latest_time,
            )
        )
        await reconcile_generation_attempt(
            session,
            client,
            generation_attempt_id=attempt_id,
            attempt_watchdog_seconds=105 * 60,
            now=latest_time,
        )
        bounded = await reconcile_generation_attempt(
            session,
            client,
            generation_attempt_id=attempt_id,
            attempt_watchdog_seconds=105 * 60,
            now=latest_time,
        )
        cancelled_attempt = await session.get(GenerationAttempt, attempt_id)

    assert bounded.observation.attempt_state == GenerationAttemptState.CANCEL_REQUESTED
    assert client.cancel_calls == [("generation-v1", str(REMOTE_JOB_ID))]
    assert cancelled_attempt is not None
    assert cancelled_attempt.response_metadata["watchdog_anchor_at"] == latest_time.isoformat()
    assert cancelled_attempt.response_metadata["watchdog_deadline_at"] == latest_time.isoformat()


@pytest.mark.asyncio
async def test_output_progress_watchdog_recovers_orphaned_running_provider_job_without_asset_loss(
    database: Database,
) -> None:
    async with database.sessions() as session:
        context = await seed_context(session)
        attempt_id = await prepared_attempt(session, context)
        await submit_prepared_attempt(
            session,
            FakeSaladClient(),
            FakeUploadIntentProvider(),
            generation_attempt_id=attempt_id,
            webhook_url="https://controller.example.test/webhooks/salad",
            reservation_microusd=500_000,
            now=NOW,
        )
        attempt = await session.get(GenerationAttempt, attempt_id)
        assert attempt is not None
        enable_runtime_admission(attempt)
        retained_asset_id, staged_asset_id = await add_progress_watchdog_assets(
            session,
            context,
        )
        await session.commit()
        provider_metadata: JSONObject = {
            "generation_attempt_id": str(attempt.id),
            "generation_job_id": str(attempt.job_id),
            "submission_key": attempt.submission_key,
            "request_sha256": attempt.request_sha256,
        }
        frozen_running = remote_job(
            status=SaladJobStatus.RUNNING,
            metadata=provider_metadata,
            update_time=NOW + timedelta(minutes=1),
        )
        client = FakeSaladClient(
            get_result=frozen_running,
            cancel_error=SaladTransportError("connection reset after DELETE"),
        )

        started = await reconcile_generation_attempt(
            session,
            client,
            generation_attempt_id=attempt_id,
            attempt_watchdog_seconds=30 * 60,
            output_progress_watchdog_seconds=10 * 60,
            now=NOW + timedelta(minutes=1),
        )
        assert started.observation.attempt_state == GenerationAttemptState.RUNNING
        assert client.cancel_calls == []

        # One pre-existing accepted prefix output receives a bounded two-minute
        # grace. With no cleanup or newly accepted output, this is now stalled.
        cancel_requested = await reconcile_generation_attempt(
            session,
            client,
            generation_attempt_id=attempt_id,
            attempt_watchdog_seconds=30 * 60,
            output_progress_watchdog_seconds=10 * 60,
            now=NOW + timedelta(minutes=14),
        )
        pending_attempt = await session.get(GenerationAttempt, attempt_id)
        assert pending_attempt is not None
        assert cancel_requested.observation.attempt_state == (
            GenerationAttemptState.CANCEL_REQUESTED
        )
        assert cancel_requested.error_code == "salad_attempt_watchdog_cancel_unavailable"
        assert pending_attempt.provider_external_id == str(REMOTE_JOB_ID)
        assert pending_attempt.last_observed_at is not None
        assert pending_attempt.last_observed_at.replace(tzinfo=UTC) == NOW + timedelta(minutes=1)
        assert pending_attempt.response_metadata is not None
        assert pending_attempt.response_metadata["watchdog_reason"] == (
            "accepted_output_progress_stalled"
        )
        assert pending_attempt.response_metadata["accepted_output_count"] == 1
        assert pending_attempt.response_metadata["preexisting_output_grace_count"] == 1
        assert pending_attempt.error_detail == (
            "The manifest-bound Salad attempt stopped producing accepted output progress; "
            "cancellation was requested."
        )
        deferred_audit_detail = await session.scalar(
            select(AuditEvent.detail).where(
                AuditEvent.resource_type == "generation_attempt",
                AuditEvent.resource_id == attempt_id,
                AuditEvent.action == "generation_attempt.watchdog_cancel_deferred",
            )
        )
        assert deferred_audit_detail is not None
        assert deferred_audit_detail["reason"] == "accepted_output_progress_stalled"
        assert client.cancel_calls == [("generation-v1", str(REMOTE_JOB_ID))]
        assert client.create_calls == []

        # A transport failure after DELETE cannot prove whether Salad accepted
        # it. Reconciliation retries only the same exact cancellation; it never
        # posts another job or creates another attempt while that identity is active.
        # A provider PENDING-after-RUNNING regression with an update clock older
        # than retained last_observed_at does not erase the durable exact intent.
        client.cancel_error = None
        client.get_result = remote_job(
            status=SaladJobStatus.PENDING,
            metadata=provider_metadata,
            update_time=NOW + timedelta(seconds=30),
        )
        repeated = await reconcile_generation_attempt(
            session,
            client,
            generation_attempt_id=attempt_id,
            attempt_watchdog_seconds=30 * 60,
            output_progress_watchdog_seconds=10 * 60,
            now=NOW + timedelta(minutes=15),
        )
        attempts_before_terminal = int(
            await session.scalar(
                select(func.count(GenerationAttempt.id)).where(
                    GenerationAttempt.job_id == context.job_id
                )
            )
            or 0
        )
        watchdog_audits = int(
            await session.scalar(
                select(func.count(AuditEvent.id)).where(
                    AuditEvent.resource_type == "generation_attempt",
                    AuditEvent.resource_id == attempt_id,
                    AuditEvent.action == "generation_attempt.watchdog_cancel_requested",
                )
            )
            or 0
        )
        assert repeated.observation.attempt_state == GenerationAttemptState.CANCEL_REQUESTED
        assert client.cancel_calls == [
            ("generation-v1", str(REMOTE_JOB_ID)),
            ("generation-v1", str(REMOTE_JOB_ID)),
        ]
        assert client.create_calls == []
        assert attempts_before_terminal == 1
        assert watchdog_audits == 1

        confirmed = await reconcile_generation_attempt(
            session,
            FakeSaladClient(
                get_result=remote_job(
                    status=SaladJobStatus.CANCELLED,
                    metadata=provider_metadata,
                    update_time=NOW + timedelta(minutes=16),
                )
            ),
            generation_attempt_id=attempt_id,
            attempt_watchdog_seconds=30 * 60,
            output_progress_watchdog_seconds=10 * 60,
            now=NOW + timedelta(minutes=16),
        )
        final_attempt = await session.get(GenerationAttempt, attempt_id)
        retrying_job = await session.get(GenerationJob, context.job_id)
        retained_asset = await session.get(Asset, retained_asset_id)
        staged_asset = await session.get(Asset, staged_asset_id)

    assert confirmed.observation.attempt_state == GenerationAttemptState.FAILED
    assert confirmed.observation.generation_job_state == GenerationState.RETRY_WAIT
    assert final_attempt is not None
    assert final_attempt.provider_external_id == str(REMOTE_JOB_ID)
    assert final_attempt.error_code == SALAD_ATTEMPT_WATCHDOG_EXPIRED_ERROR_CODE
    assert retrying_job is not None
    assert retrying_job.state == GenerationState.RETRY_WAIT
    assert retrying_job.max_attempts == 4
    assert retained_asset is not None
    assert retained_asset.state == AssetState.AVAILABLE
    assert retained_asset.object_key is not None
    assert staged_asset is not None
    assert staged_asset.state == AssetState.UPLOADING
    assert staged_asset.staging_object_key is not None


@pytest.mark.asyncio
async def test_output_progress_watchdog_allows_slow_first_output(
    database: Database,
) -> None:
    async with database.sessions() as session:
        context = await seed_context(session)
        attempt_id = await prepared_attempt(session, context)
        await submit_prepared_attempt(
            session,
            FakeSaladClient(),
            FakeUploadIntentProvider(),
            generation_attempt_id=attempt_id,
            webhook_url="https://controller.example.test/webhooks/salad",
            reservation_microusd=500_000,
            now=NOW,
        )
        attempt = await session.get(GenerationAttempt, attempt_id)
        assert attempt is not None
        enable_runtime_admission(attempt)
        await session.commit()
        provider_metadata: JSONObject = {
            "generation_attempt_id": str(attempt.id),
            "generation_job_id": str(attempt.job_id),
            "submission_key": attempt.submission_key,
            "request_sha256": attempt.request_sha256,
        }
        client = FakeSaladClient(
            get_result=remote_job(
                status=SaladJobStatus.RUNNING,
                metadata=provider_metadata,
                update_time=NOW + timedelta(minutes=1),
            )
        )
        await reconcile_generation_attempt(
            session,
            client,
            generation_attempt_id=attempt_id,
            attempt_watchdog_seconds=30 * 60,
            output_progress_watchdog_seconds=10 * 60,
            now=NOW + timedelta(minutes=1),
        )
        # Runtime-bound first-image model loading gets the complete base window.
        slow_first_output = await reconcile_generation_attempt(
            session,
            client,
            generation_attempt_id=attempt_id,
            attempt_watchdog_seconds=30 * 60,
            output_progress_watchdog_seconds=10 * 60,
            now=NOW + timedelta(minutes=10, seconds=59),
        )

    assert slow_first_output.observation.attempt_state == GenerationAttemptState.RUNNING
    assert client.cancel_calls == []


@pytest.mark.asyncio
async def test_output_progress_watchdog_ignores_legacy_job_without_runtime_admission(
    database: Database,
) -> None:
    async with database.sessions() as session:
        context = await seed_context(session)
        attempt_id = await prepared_attempt(session, context)
        await submit_prepared_attempt(
            session,
            FakeSaladClient(),
            FakeUploadIntentProvider(),
            generation_attempt_id=attempt_id,
            webhook_url="https://controller.example.test/webhooks/salad",
            reservation_microusd=500_000,
            now=NOW,
        )
        attempt = await session.get(GenerationAttempt, attempt_id)
        assert attempt is not None
        provider_metadata: JSONObject = {
            "generation_attempt_id": str(attempt.id),
            "generation_job_id": str(attempt.job_id),
            "submission_key": attempt.submission_key,
            "request_sha256": attempt.request_sha256,
        }
        client = FakeSaladClient(
            get_result=remote_job(
                status=SaladJobStatus.RUNNING,
                metadata=provider_metadata,
                update_time=NOW + timedelta(minutes=1),
            )
        )
        await reconcile_generation_attempt(
            session,
            client,
            generation_attempt_id=attempt_id,
            attempt_watchdog_seconds=30 * 60,
            output_progress_watchdog_seconds=10 * 60,
            now=NOW + timedelta(minutes=1),
        )

        # A legacy job may still be in an old boot/runtime handoff. Only the
        # existing full runtime envelope applies without the durable v1 marker.
        legacy_running = await reconcile_generation_attempt(
            session,
            client,
            generation_attempt_id=attempt_id,
            attempt_watchdog_seconds=30 * 60,
            output_progress_watchdog_seconds=10 * 60,
            now=NOW + timedelta(minutes=15),
        )

    assert legacy_running.observation.attempt_state == GenerationAttemptState.RUNNING
    assert client.cancel_calls == []


@pytest.mark.asyncio
async def test_exact_current_intent_cleanup_resets_partial_retry_progress_deadline(
    database: Database,
) -> None:
    async with database.sessions() as session:
        context = await seed_context(session)
        attempt_id = await prepared_attempt(session, context)
        await submit_prepared_attempt(
            session,
            FakeSaladClient(),
            FakeUploadIntentProvider(),
            generation_attempt_id=attempt_id,
            webhook_url="https://controller.example.test/webhooks/salad",
            reservation_microusd=500_000,
            now=NOW,
        )
        attempt = await session.get(GenerationAttempt, attempt_id)
        assert attempt is not None
        enable_runtime_admission(attempt)
        retained_asset_id, _ = await add_progress_watchdog_assets(session, context)
        await session.commit()
        provider_metadata: JSONObject = {
            "generation_attempt_id": str(attempt.id),
            "generation_job_id": str(attempt.job_id),
            "submission_key": attempt.submission_key,
            "request_sha256": attempt.request_sha256,
        }
        client = FakeSaladClient(
            get_result=remote_job(
                status=SaladJobStatus.RUNNING,
                metadata=provider_metadata,
                update_time=NOW + timedelta(minutes=1),
            )
        )
        await reconcile_generation_attempt(
            session,
            client,
            generation_attempt_id=attempt_id,
            attempt_watchdog_seconds=30 * 60,
            output_progress_watchdog_seconds=10 * 60,
            now=NOW + timedelta(minutes=1),
        )

        retained = await session.get(Asset, retained_asset_id)
        assert retained is not None
        exact_intent_metadata = dict(retained.asset_metadata)
        exact_intent_metadata.update(
            {
                "staging_cleanup": "completed",
                "staging_cleaned_at": (NOW + timedelta(minutes=12)).isoformat(),
            }
        )
        retained.asset_metadata = exact_intent_metadata
        await session.commit()

        still_progressing = await reconcile_generation_attempt(
            session,
            client,
            generation_attempt_id=attempt_id,
            attempt_watchdog_seconds=30 * 60,
            output_progress_watchdog_seconds=10 * 60,
            now=NOW + timedelta(minutes=21, seconds=59),
        )
        assert still_progressing.observation.attempt_state == GenerationAttemptState.RUNNING
        assert client.cancel_calls == []

        stalled_after_cleanup = await reconcile_generation_attempt(
            session,
            client,
            generation_attempt_id=attempt_id,
            attempt_watchdog_seconds=30 * 60,
            output_progress_watchdog_seconds=10 * 60,
            now=NOW + timedelta(minutes=22),
        )
        pending_attempt = await session.get(GenerationAttempt, attempt_id)

    assert stalled_after_cleanup.observation.attempt_state == (
        GenerationAttemptState.CANCEL_REQUESTED
    )
    assert client.cancel_calls == [("generation-v1", str(REMOTE_JOB_ID))]
    assert pending_attempt is not None
    assert pending_attempt.response_metadata is not None
    assert pending_attempt.response_metadata["latest_progress_output_index"] == 0
    assert pending_attempt.response_metadata["preexisting_output_grace_count"] == 0
    assert (
        pending_attempt.response_metadata["watchdog_anchor_at"]
        == (NOW + timedelta(minutes=12)).isoformat()
    )


@pytest.mark.asyncio
async def test_operator_stop_precedes_due_output_progress_watchdog(
    database: Database,
) -> None:
    async with database.sessions() as session:
        context = await seed_context(session)
        attempt_id = await prepared_attempt(session, context)
        await submit_prepared_attempt(
            session,
            FakeSaladClient(),
            FakeUploadIntentProvider(),
            generation_attempt_id=attempt_id,
            webhook_url="https://controller.example.test/webhooks/salad",
            reservation_microusd=500_000,
            now=NOW,
        )
        attempt = await session.get(GenerationAttempt, attempt_id)
        assert attempt is not None
        enable_runtime_admission(attempt)
        await session.commit()
        provider_metadata: JSONObject = {
            "generation_attempt_id": str(attempt.id),
            "generation_job_id": str(attempt.job_id),
            "submission_key": attempt.submission_key,
            "request_sha256": attempt.request_sha256,
        }
        client = FakeSaladClient(
            get_result=remote_job(
                status=SaladJobStatus.RUNNING,
                metadata=provider_metadata,
                update_time=NOW + timedelta(minutes=1),
            )
        )
        await reconcile_generation_attempt(
            session,
            client,
            generation_attempt_id=attempt_id,
            attempt_watchdog_seconds=30 * 60,
            output_progress_watchdog_seconds=10 * 60,
            now=NOW + timedelta(minutes=1),
        )
        await mark_operator_stop(session, context)

        stopped = await reconcile_generation_attempt(
            session,
            client,
            generation_attempt_id=attempt_id,
            attempt_watchdog_seconds=30 * 60,
            output_progress_watchdog_seconds=10 * 60,
            now=NOW + timedelta(minutes=14),
        )
        final_attempt = await session.get(GenerationAttempt, attempt_id)
        watchdog_audits = int(
            await session.scalar(
                select(func.count(AuditEvent.id)).where(
                    AuditEvent.resource_type == "generation_attempt",
                    AuditEvent.resource_id == attempt_id,
                    AuditEvent.action == "generation_attempt.watchdog_cancel_requested",
                )
            )
            or 0
        )

    assert stopped.error_code == OPERATOR_STOP_CANCEL_REQUESTED_ERROR_CODE
    assert final_attempt is not None
    assert final_attempt.error_code == OPERATOR_STOP_CANCEL_REQUESTED_ERROR_CODE
    assert client.cancel_calls == [("generation-v1", str(REMOTE_JOB_ID))]
    assert watchdog_audits == 0


@pytest.mark.asyncio
async def test_superseded_deployment_cancels_then_retries_on_current_deployment(
    database: Database,
) -> None:
    async with database.sessions() as session:
        context = await seed_context(session)
        attempt_id = await prepared_attempt(session, context)
        await submit_prepared_attempt(
            session,
            FakeSaladClient(),
            FakeUploadIntentProvider(),
            generation_attempt_id=attempt_id,
            webhook_url="https://controller.example.test/webhooks/salad",
            reservation_microusd=500_000,
            now=NOW,
        )
        old_deployment = await session.get(SaladDeployment, context.deployment_id)
        job = await session.get(GenerationJob, context.job_id)
        assert old_deployment is not None
        assert job is not None
        version = await session.get(ReleaseVersion, job.release_version_id)
        assert version is not None
        retained_asset = Asset(
            release_id=version.release_id,
            generation_job_id=job.id,
            output_index=0,
            kind=AssetKind.RAW_MASTER,
            state=AssetState.AVAILABLE,
            storage_backend="memory",
            storage_bucket="test-assets",
            object_key=f"masters/{job.id}/0.png",
            object_version_id="retained-version",
            sha256="9" * 64,
            content_type="image/png",
            image_format="png",
            width=1024,
            height=1024,
            byte_size=4096,
            asset_metadata={"source": "progressive-upload"},
            available_at=NOW + timedelta(seconds=1),
        )
        session.add(retained_asset)
        old_deployment.is_current = False
        old_deployment.desired_state = DesiredDeploymentState.STOPPED
        old_deployment.state = SaladDeploymentState.STOPPED
        old_deployment.stopped_at = NOW + timedelta(seconds=2)
        current_deployment = SaladDeployment(
            version_no=2,
            config_sha256="c" * 64,
            worker_image_digest=IMAGE_DIGEST,
            organization_name="creator-org",
            project_name="production",
            queue_name="generation-v2",
            provider_queue_id="provider-queue-v2",
            container_group_name="worker-v2",
            provider_container_group_id="provider-group-v2",
            state=SaladDeploymentState.ACTIVE,
            desired_state=DesiredDeploymentState.ACTIVE,
            is_current=True,
            min_replicas=0,
            max_replicas=1,
            desired_queue_length=1,
            max_hourly_cost_microusd=2_000_000,
            lock_version=1,
        )
        session.add(current_deployment)
        await session.commit()
        retained_asset_id = retained_asset.id
        retained_object_key = retained_asset.object_key
        current_deployment_id = current_deployment.id

        attempt = await session.get(GenerationAttempt, attempt_id)
        assert attempt is not None
        metadata: JSONObject = {
            "generation_attempt_id": str(attempt.id),
            "generation_job_id": str(attempt.job_id),
            "submission_key": attempt.submission_key,
            "request_sha256": attempt.request_sha256,
        }
        pending = remote_job(
            status=SaladJobStatus.PENDING,
            metadata=metadata,
            update_time=NOW + timedelta(minutes=1),
        )
        deferred_client = FakeSaladClient(
            get_result=pending,
            cancel_error=SaladTransportError("connection reset"),
        )
        deferred = await reconcile_generation_attempt(
            session,
            deferred_client,
            generation_attempt_id=attempt_id,
            now=NOW + timedelta(minutes=2),
        )
        cancelling_attempt = await session.get(GenerationAttempt, attempt_id)
        blocked_job = await session.get(GenerationJob, context.job_id)

        assert deferred_client.cancel_calls == [("generation-v1", str(REMOTE_JOB_ID))]
        assert deferred.error_code == "salad_deployment_rollover_cancel_unavailable"
        assert cancelling_attempt is not None
        assert cancelling_attempt.state == GenerationAttemptState.CANCEL_REQUESTED
        assert cancelling_attempt.error_code == DEPLOYMENT_ROLLOVER_CANCEL_REQUESTED_ERROR_CODE
        assert cancelling_attempt.response_metadata is not None
        assert cancelling_attempt.response_metadata["deployment_rollover_cancel_requested"] is True
        assert cancelling_attempt.reservation_released_at is None
        assert blocked_job is not None
        assert blocked_job.state == GenerationState.RUNNING

        cancel_client = FakeSaladClient(get_result=pending)
        cancel_requested = await reconcile_generation_attempt(
            session,
            cancel_client,
            generation_attempt_id=attempt_id,
            now=NOW + timedelta(minutes=3),
        )
        assert cancel_client.cancel_calls == [("generation-v1", str(REMOTE_JOB_ID))]
        assert cancel_requested.error_code == DEPLOYMENT_ROLLOVER_CANCEL_REQUESTED_ERROR_CODE

        confirmed = await reconcile_generation_attempt(
            session,
            FakeSaladClient(
                get_result=remote_job(
                    status=SaladJobStatus.CANCELLED,
                    metadata=metadata,
                    update_time=NOW + timedelta(minutes=4),
                )
            ),
            generation_attempt_id=attempt_id,
            now=NOW + timedelta(minutes=5),
        )
        retrying_job = await session.get(GenerationJob, context.job_id)
        final_old_attempt = await session.get(GenerationAttempt, attempt_id)
        unchanged_asset = await session.get(Asset, retained_asset_id)

        assert confirmed.observation.attempt_state == GenerationAttemptState.FAILED
        assert confirmed.observation.generation_job_state == GenerationState.RETRY_WAIT
        assert final_old_attempt is not None
        assert final_old_attempt.error_code == DEPLOYMENT_ROLLOVER_RETRY_ERROR_CODE
        assert final_old_attempt.reservation_released_at is not None
        assert retrying_job is not None
        assert retrying_job.state == GenerationState.RETRY_WAIT
        assert retrying_job.last_error_code == DEPLOYMENT_ROLLOVER_RETRY_ERROR_CODE
        assert unchanged_asset is not None
        assert unchanged_asset.state == AssetState.AVAILABLE
        assert unchanged_asset.object_key == retained_object_key
        assert unchanged_asset.object_version_id == "retained-version"

        replacement = await prepare_generation_attempt(
            session,
            generation_job_id=context.job_id,
            salad_deployment_id=current_deployment_id,
            idempotency_key="scheduler-claim-2",
            now=NOW + timedelta(minutes=6),
        )
        replacement_attempt = await session.get(
            GenerationAttempt,
            replacement.generation_attempt_id,
        )

    assert replacement_attempt is not None
    assert replacement_attempt.salad_deployment_id == current_deployment_id
    assert replacement_attempt.attempt_no == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_time_offset", "supersede_release"),
    [
        (timedelta(0), False),
        (-timedelta(seconds=30), False),
        (-timedelta(seconds=30), True),
    ],
    ids=[
        "equal-provider-timestamp",
        "older-provider-timestamp",
        "older-provider-timestamp-release-superseded",
    ],
)
async def test_superseded_running_attempt_cancels_exact_pending_provider_regression(
    database: Database,
    provider_time_offset: timedelta,
    supersede_release: bool,
) -> None:
    async with database.sessions() as session:
        context, attempt_id, metadata, last_observed_at = await superseded_running_attempt(session)

        async def supersede_release_lifecycle() -> None:
            async with database.sessions() as concurrent:
                job = await concurrent.get(GenerationJob, context.job_id)
                assert job is not None
                version = await concurrent.get(ReleaseVersion, job.release_version_id)
                assert version is not None
                release = await concurrent.get(Release, version.release_id)
                assert release is not None
                release.current_version_no = version.version_no + 1
                release.lock_version += 1
                await concurrent.commit()

        client = FakeSaladClient(
            get_result=remote_job(
                status=SaladJobStatus.PENDING,
                metadata=metadata,
                update_time=last_observed_at + provider_time_offset,
            ),
            before_get=supersede_release_lifecycle if supersede_release else None,
        )

        result = await reconcile_generation_attempt(
            session,
            client,
            generation_attempt_id=attempt_id,
            now=NOW + timedelta(minutes=3),
        )
        attempt = await session.get(GenerationAttempt, attempt_id)
        job = await session.get(GenerationJob, context.job_id)
        cancel_audits = int(
            await session.scalar(
                select(func.count(AuditEvent.id)).where(
                    AuditEvent.resource_type == "generation_attempt",
                    AuditEvent.resource_id == attempt_id,
                    AuditEvent.action == "generation_attempt.deployment_rollover_cancel_requested",
                )
            )
            or 0
        )

    assert client.cancel_calls == [("generation-v1", str(REMOTE_JOB_ID))]
    assert result.observation.stale is False
    assert result.observation.attempt_state == GenerationAttemptState.CANCEL_REQUESTED
    assert result.error_code == DEPLOYMENT_ROLLOVER_CANCEL_REQUESTED_ERROR_CODE
    assert attempt is not None
    assert attempt.state == GenerationAttemptState.CANCEL_REQUESTED
    assert attempt.error_code == DEPLOYMENT_ROLLOVER_CANCEL_REQUESTED_ERROR_CODE
    assert attempt.response_metadata is not None
    assert attempt.response_metadata["deployment_rollover_cancel_requested"] is True
    assert attempt.last_observed_at is not None
    assert attempt.last_observed_at.replace(tzinfo=UTC) == last_observed_at
    assert job is not None
    assert job.state == GenerationState.RUNNING
    assert cancel_audits == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "concurrent_change",
    ["terminal-attempt", "provider-id-drift", "deployment-reactivated"],
)
async def test_rollover_pending_cancel_rejects_concurrent_local_fence_changes(
    database: Database,
    concurrent_change: str,
) -> None:
    async with database.sessions() as session_a:
        context, attempt_id, metadata, last_observed_at = await superseded_running_attempt(
            session_a
        )
        drifted_provider_id = str(uuid4())

        async def change_local_fence() -> None:
            async with database.sessions() as session_b:
                attempt_b = await session_b.get(GenerationAttempt, attempt_id)
                job_b = await session_b.get(GenerationJob, context.job_id)
                assert attempt_b is not None
                assert job_b is not None
                if concurrent_change == "terminal-attempt":
                    attempt_b.state = GenerationAttemptState.FAILED
                    attempt_b.completed_at = NOW + timedelta(minutes=2, seconds=10)
                    attempt_b.error_code = "concurrent_terminal_failure"
                    attempt_b.error_detail = "The terminal state remains authoritative."
                    job_b.state = GenerationState.DEAD_LETTER
                    job_b.retry_at = None
                    job_b.last_error_code = attempt_b.error_code
                    job_b.last_error_detail = attempt_b.error_detail
                    job_b.lock_version += 1
                    attempt_b.lock_version += 1
                elif concurrent_change == "provider-id-drift":
                    attempt_b.provider_external_id = drifted_provider_id
                    attempt_b.lock_version += 1
                else:
                    deployment_b = await session_b.get(
                        SaladDeployment,
                        context.deployment_id,
                    )
                    assert deployment_b is not None
                    deployment_b.is_current = True
                    deployment_b.desired_state = DesiredDeploymentState.ACTIVE
                    deployment_b.lock_version += 1
                await session_b.commit()

        client = FakeSaladClient(
            get_result=remote_job(
                status=SaladJobStatus.PENDING,
                metadata=metadata,
                update_time=last_observed_at - timedelta(seconds=30),
            ),
            before_get=change_local_fence,
        )
        result = await reconcile_generation_attempt(
            session_a,
            client,
            generation_attempt_id=attempt_id,
            now=NOW + timedelta(minutes=3),
        )

    async with database.sessions() as verification:
        attempt = await verification.get(GenerationAttempt, attempt_id)
        job = await verification.get(GenerationJob, context.job_id)
        deployment = await verification.get(SaladDeployment, context.deployment_id)
        cancel_audits = int(
            await verification.scalar(
                select(func.count(AuditEvent.id)).where(
                    AuditEvent.resource_type == "generation_attempt",
                    AuditEvent.resource_id == attempt_id,
                    AuditEvent.action == "generation_attempt.deployment_rollover_cancel_requested",
                )
            )
            or 0
        )

    assert client.cancel_calls == []
    assert result.observation.stale is True
    assert attempt is not None
    assert attempt.state != GenerationAttemptState.CANCEL_REQUESTED
    assert attempt.error_code != DEPLOYMENT_ROLLOVER_CANCEL_REQUESTED_ERROR_CODE
    assert job is not None
    assert cancel_audits == 0
    if concurrent_change == "terminal-attempt":
        assert attempt.state == GenerationAttemptState.FAILED
        assert attempt.error_code == "concurrent_terminal_failure"
        assert job.state == GenerationState.DEAD_LETTER
    elif concurrent_change == "provider-id-drift":
        assert attempt.state == GenerationAttemptState.RUNNING
        assert attempt.provider_external_id == drifted_provider_id
        assert job.state == GenerationState.RUNNING
    else:
        assert deployment is not None
        assert deployment.is_current is True
        assert deployment.desired_state == DesiredDeploymentState.ACTIVE
        assert attempt.state == GenerationAttemptState.RUNNING
        assert job.state == GenerationState.RUNNING


@pytest.mark.asyncio
async def test_superseded_unknown_attempt_retries_only_after_confirmed_provider_absence(
    database: Database,
) -> None:
    async with database.sessions() as session:
        context = await seed_context(session)
        attempt_id = await prepared_attempt(session, context)
        await submit_prepared_attempt(
            session,
            FakeSaladClient(create_error=SaladTimeoutError("ambiguous provider post")),
            FakeUploadIntentProvider(),
            generation_attempt_id=attempt_id,
            webhook_url="https://controller.example.test/webhooks/salad",
            reservation_microusd=500_000,
            now=NOW,
        )
        deployment = await session.get(SaladDeployment, context.deployment_id)
        assert deployment is not None
        deployment.is_current = False
        deployment.desired_state = DesiredDeploymentState.STOPPED
        deployment.state = SaladDeploymentState.STOPPED
        deployment.stopped_at = NOW + timedelta(seconds=1)
        await session.commit()

        for minute in (1, 2):
            pending = await reconcile_generation_attempt(
                session,
                FakeSaladClient(list_pages={1: ()}),
                generation_attempt_id=attempt_id,
                list_page_size=SALAD_QUEUE_JOB_PAGE_SIZE,
                now=NOW + timedelta(minutes=minute),
            )
            attempt = await session.get(GenerationAttempt, attempt_id)
            job = await session.get(GenerationJob, context.job_id)
            assert pending.error_code == ("salad_deployment_rollover_provider_absence_pending")
            assert attempt is not None
            assert attempt.state == GenerationAttemptState.UNKNOWN
            assert attempt.reservation_released_at is None
            assert job is not None
            assert job.state == GenerationState.UNKNOWN

        unavailable = await reconcile_generation_attempt(
            session,
            FakeSaladClient(list_error=SaladTransportError("temporary outage")),
            generation_attempt_id=attempt_id,
            list_page_size=SALAD_QUEUE_JOB_PAGE_SIZE,
            now=NOW + timedelta(minutes=3),
        )
        assert unavailable.error_code == "salad_reconciliation_unavailable"

        after_reset = await reconcile_generation_attempt(
            session,
            FakeSaladClient(list_pages={1: ()}),
            generation_attempt_id=attempt_id,
            list_page_size=SALAD_QUEUE_JOB_PAGE_SIZE,
            now=NOW + timedelta(minutes=4),
        )
        after_reset_attempt = await session.get(GenerationAttempt, attempt_id)
        assert after_reset.error_code == ("salad_deployment_rollover_provider_absence_pending")
        assert after_reset_attempt is not None
        assert after_reset_attempt.response_metadata is not None
        tracker = after_reset_attempt.response_metadata["deployment_rollover_absence_confirmation"]
        assert isinstance(tracker, dict)
        assert tracker["count"] == 1
        assert after_reset_attempt.reservation_released_at is None

        for minute in (5,):
            pending = await reconcile_generation_attempt(
                session,
                FakeSaladClient(list_pages={1: ()}),
                generation_attempt_id=attempt_id,
                list_page_size=SALAD_QUEUE_JOB_PAGE_SIZE,
                now=NOW + timedelta(minutes=minute),
            )
            assert pending.error_code == ("salad_deployment_rollover_provider_absence_pending")

        confirmed = await reconcile_generation_attempt(
            session,
            FakeSaladClient(list_pages={1: ()}),
            generation_attempt_id=attempt_id,
            list_page_size=SALAD_QUEUE_JOB_PAGE_SIZE,
            now=NOW + timedelta(minutes=6),
        )
        final_attempt = await session.get(GenerationAttempt, attempt_id)
        retrying_job = await session.get(GenerationJob, context.job_id)

    assert confirmed.error_code == "salad_deployment_rollover_provider_absent"
    assert confirmed.observation.attempt_state == GenerationAttemptState.FAILED
    assert confirmed.observation.generation_job_state == GenerationState.RETRY_WAIT
    assert final_attempt is not None
    assert final_attempt.reservation_released_at is not None
    assert retrying_job is not None
    assert retrying_job.state == GenerationState.RETRY_WAIT


@pytest.mark.asyncio
async def test_terminal_attempt_reconciliation_is_a_noop(database: Database) -> None:
    async with database.sessions() as session:
        context = await seed_context(session)
        attempt_id = await prepared_attempt(session, context)
        await submit_prepared_attempt(
            session,
            FakeSaladClient(
                create_error=SaladAPIError(
                    status_code=400,
                    message="invalid",
                    response_body="",
                    request_id=None,
                )
            ),
            FakeUploadIntentProvider(),
            generation_attempt_id=attempt_id,
            webhook_url="https://controller.example.test/webhooks/salad",
            reservation_microusd=500_000,
            now=NOW,
        )
        client = FakeSaladClient()

        result = await reconcile_generation_attempt(
            session,
            client,
            generation_attempt_id=attempt_id,
            now=NOW + timedelta(minutes=1),
        )

    assert result.source == ReconciliationSource.NONE
    assert result.matched is True
    assert not client.get_calls
    assert not client.list_calls


@pytest.mark.asyncio
async def test_unknown_submission_reconciles_by_metadata_then_get_and_ignores_regression(
    database: Database,
) -> None:
    async with database.sessions() as session:
        context = await seed_context(session)
        attempt_id = await prepared_attempt(session, context)
        timeout_client = FakeSaladClient(create_error=SaladTimeoutError("timeout"))
        await submit_prepared_attempt(
            session,
            timeout_client,
            FakeUploadIntentProvider(),
            generation_attempt_id=attempt_id,
            webhook_url="https://controller.example.test/webhooks/salad",
            reservation_microusd=500_000,
            now=NOW,
        )
        attempt = await session.get(GenerationAttempt, attempt_id)
        assert attempt is not None
        # Lease recovery timestamps are controller observations, not provider
        # update timestamps, so they must not suppress metadata reconciliation.
        attempt.last_observed_at = NOW + timedelta(minutes=10)
        await session.commit()
        metadata: JSONObject = {
            "generation_attempt_id": str(attempt.id),
            "generation_job_id": str(attempt.job_id),
            "submission_key": attempt.submission_key,
            "request_sha256": attempt.request_sha256,
        }
        running = remote_job(
            status=SaladJobStatus.RUNNING,
            metadata=metadata,
            update_time=NOW + timedelta(minutes=1),
        )
        list_client = FakeSaladClient(list_pages={1: (running,)})
        listed = await reconcile_generation_attempt(
            session,
            list_client,
            generation_attempt_id=attempt_id,
            list_page_size=SALAD_QUEUE_JOB_PAGE_SIZE,
            now=NOW + timedelta(minutes=2),
        )

        worker_outputs = await expected_worker_outputs(session, context)
        succeeded = remote_job(
            status=SaladJobStatus.SUCCEEDED,
            metadata=metadata,
            update_time=NOW + timedelta(minutes=3),
            output={
                "version": "v1",
                "job_id": str(context.job_id),
                "attempt_id": str(attempt_id),
                "status": "succeeded",
                "outputs": worker_outputs,
            },
        )
        get_client = FakeSaladClient(get_result=succeeded)
        fetched = await reconcile_generation_attempt(
            session,
            get_client,
            generation_attempt_id=attempt_id,
            now=NOW + timedelta(minutes=4),
        )

        stale_running = remote_job(
            status=SaladJobStatus.RUNNING,
            metadata=metadata,
            update_time=NOW + timedelta(minutes=2),
        )
        stale = await apply_salad_job_observation(
            session,
            generation_attempt_id=attempt_id,
            remote_job=stale_running,
            observed_at=NOW + timedelta(minutes=5),
        )
        conflicting_failed = remote_job(
            status=SaladJobStatus.FAILED,
            metadata=metadata,
            update_time=NOW + timedelta(minutes=6),
        )
        conflict = await apply_salad_job_observation(
            session,
            generation_attempt_id=attempt_id,
            remote_job=conflicting_failed,
            observed_at=NOW + timedelta(minutes=7),
        )
        await session.commit()
        final_attempt = await session.get(GenerationAttempt, attempt_id)
        final_job = await session.get(GenerationJob, context.job_id)

    assert listed.source == ReconciliationSource.LIST
    assert listed.matched is True
    assert listed.observation.attempt_state == GenerationAttemptState.RUNNING
    assert len(list_client.list_calls) == 1
    assert not list_client.get_calls
    assert fetched.source == ReconciliationSource.GET
    assert fetched.observation.attempt_state == GenerationAttemptState.SUCCEEDED
    assert len(get_client.get_calls) == 1
    assert stale.stale is True
    assert stale.applied is False
    assert conflict.applied is False
    assert final_attempt is not None
    assert final_attempt.state == GenerationAttemptState.SUCCEEDED
    assert final_attempt.reservation_released_at is not None
    assert final_job is not None
    assert final_job.state == GenerationState.COLLECTING


@pytest.mark.asyncio
async def test_succeeded_provider_job_with_invalid_worker_output_retries(
    database: Database,
) -> None:
    async with database.sessions() as session:
        context = await seed_context(session)
        attempt_id = await prepared_attempt(session, context)
        await submit_prepared_attempt(
            session,
            FakeSaladClient(),
            FakeUploadIntentProvider(),
            generation_attempt_id=attempt_id,
            webhook_url="https://controller.example.test/webhooks/salad",
            reservation_microusd=500_000,
            now=NOW,
        )
        attempt = await session.get(GenerationAttempt, attempt_id)
        assert attempt is not None
        metadata: JSONObject = {
            "generation_attempt_id": str(attempt.id),
            "generation_job_id": str(attempt.job_id),
            "submission_key": attempt.submission_key,
            "request_sha256": attempt.request_sha256,
        }

        result = await reconcile_generation_attempt(
            session,
            FakeSaladClient(
                get_result=remote_job(
                    status=SaladJobStatus.SUCCEEDED,
                    metadata=metadata,
                    update_time=NOW + timedelta(minutes=1),
                    output={
                        "version": "v1",
                        "job_id": str(context.job_id),
                        "attempt_id": str(attempt_id),
                        "status": "failed",
                        "code": "near_black_output",
                        "failed_output_index": 1,
                        "outputs": [
                            {
                                "asset_id": "malformed-private-asset",
                                "upload_attempt_id": "malformed-private-upload",
                                "output_index": 1,
                                "status": "uploaded",
                            }
                        ],
                    },
                )
            ),
            generation_attempt_id=attempt_id,
            now=NOW + timedelta(minutes=2),
        )
        await session.commit()
        failed_attempt = await session.get(GenerationAttempt, attempt_id)
        retrying_job = await session.get(GenerationJob, context.job_id)
        assert retrying_job is not None
        near_black_grants = int(
            await session.scalar(
                select(func.count(AuditEvent.id)).where(
                    AuditEvent.action == NEAR_BLACK_OUTPUT_RETRY_GRANT_ACTION,
                    AuditEvent.resource_id == attempt_id,
                )
            )
            or 0
        )

    assert result.observation.attempt_state == GenerationAttemptState.FAILED
    assert result.observation.generation_job_state == GenerationState.RETRY_WAIT
    assert failed_attempt is not None
    assert failed_attempt.provider_state == SaladJobStatus.SUCCEEDED.value
    assert failed_attempt.error_code == "salad_worker_output_invalid"
    assert failed_attempt.reservation_released_at is not None
    assert failed_attempt.response_metadata["worker_output_valid"] is False
    assert "malformed-private" not in repr(failed_attempt.response_metadata)
    assert near_black_grants == 0
    assert retrying_job is not None
    assert retrying_job.state == GenerationState.RETRY_WAIT
    assert retrying_job.last_error_code == "salad_worker_output_invalid"


@pytest.mark.asyncio
async def test_typed_near_black_output_gets_one_separate_bounded_retry(
    database: Database,
) -> None:
    async with database.sessions() as session:
        context = await seed_context(session)
        job = await session.get(GenerationJob, context.job_id)
        assert job is not None
        # Keep ordinary attempts available so a second deterministic near-black
        # result can prove that only the dedicated recovery grant permits retry.
        job.max_attempts = 4
        await session.commit()
        attempt_id = await prepared_attempt(session, context)
        await submit_prepared_attempt(
            session,
            FakeSaladClient(),
            FakeUploadIntentProvider(),
            generation_attempt_id=attempt_id,
            webhook_url="https://controller.example.test/webhooks/salad",
            reservation_microusd=500_000,
            now=NOW,
        )
        worker_outputs = await expected_worker_outputs(session, context)
        attempt = await session.get(GenerationAttempt, attempt_id)
        assert attempt is not None
        metadata: JSONObject = {
            "generation_attempt_id": str(attempt.id),
            "generation_job_id": str(attempt.job_id),
            "submission_key": attempt.submission_key,
            "request_sha256": attempt.request_sha256,
        }
        near_black = remote_job(
            status=SaladJobStatus.SUCCEEDED,
            metadata=metadata,
            update_time=NOW + timedelta(minutes=1),
            output={
                "version": "v1",
                "job_id": str(context.job_id),
                "attempt_id": str(attempt_id),
                "status": "failed",
                "code": "near_black_output",
                "failed_output_index": 1,
                "outputs": worker_outputs[:1],
            },
        )

        result = await reconcile_generation_attempt(
            session,
            FakeSaladClient(get_result=near_black),
            generation_attempt_id=attempt_id,
            now=NOW + timedelta(minutes=2),
        )
        await session.commit()
        failed_attempt = await session.get(GenerationAttempt, attempt_id)
        retrying_job = await session.get(GenerationJob, context.job_id)
        assert retrying_job is not None
        grant_count = int(
            await session.scalar(
                select(func.count(AuditEvent.id)).where(
                    AuditEvent.action == NEAR_BLACK_OUTPUT_RETRY_GRANT_ACTION,
                    AuditEvent.resource_id == attempt_id,
                )
            )
            or 0
        )

        repeated = await apply_salad_job_observation(
            session,
            generation_attempt_id=attempt_id,
            remote_job=near_black,
            observed_at=NOW + timedelta(minutes=3),
        )
        await session.commit()
        await session.refresh(retrying_job)
        first_retry_max_attempts = retrying_job.max_attempts
        first_retry_state = retrying_job.state
        repeated_grant_count = int(
            await session.scalar(
                select(func.count(AuditEvent.id)).where(
                    AuditEvent.action == NEAR_BLACK_OUTPUT_RETRY_GRANT_ACTION,
                    AuditEvent.resource_id == attempt_id,
                )
            )
            or 0
        )

        second_prepared = await prepare_generation_attempt(
            session,
            generation_job_id=context.job_id,
            salad_deployment_id=context.deployment_id,
            idempotency_key="near-black-retry-2",
            now=NOW + timedelta(minutes=4),
        )
        await session.commit()
        second_attempt_id = second_prepared.generation_attempt_id
        second_provider_job_id = uuid4()
        await submit_prepared_attempt(
            session,
            FakeSaladClient(create_job_id=second_provider_job_id),
            FakeUploadIntentProvider(),
            generation_attempt_id=second_attempt_id,
            webhook_url="https://controller.example.test/webhooks/salad",
            reservation_microusd=500_000,
            now=NOW + timedelta(minutes=4),
        )
        second_attempt = await session.get(GenerationAttempt, second_attempt_id)
        assert second_attempt is not None
        second_attempt_no = second_attempt.attempt_no
        second_metadata: JSONObject = {
            "generation_attempt_id": str(second_attempt.id),
            "generation_job_id": str(second_attempt.job_id),
            "submission_key": second_attempt.submission_key,
            "request_sha256": second_attempt.request_sha256,
        }
        second_near_black = remote_job(
            status=SaladJobStatus.SUCCEEDED,
            metadata=second_metadata,
            update_time=NOW + timedelta(minutes=5),
            output={
                "version": "v1",
                "job_id": str(context.job_id),
                "attempt_id": str(second_attempt_id),
                "status": "failed",
                "code": "near_black_output",
                "failed_output_index": 1,
                "outputs": worker_outputs[:1],
            },
            job_id=second_provider_job_id,
        )
        second_result = await reconcile_generation_attempt(
            session,
            FakeSaladClient(get_result=second_near_black),
            generation_attempt_id=second_attempt_id,
            now=NOW + timedelta(minutes=6),
        )
        await session.commit()
        final_job = await session.get(GenerationJob, context.job_id)
        assert final_job is not None
        all_black_grants = int(
            await session.scalar(
                select(func.count(AuditEvent.id)).where(
                    AuditEvent.action == NEAR_BLACK_OUTPUT_RETRY_GRANT_ACTION,
                )
            )
            or 0
        )

    assert result.observation.attempt_state == GenerationAttemptState.FAILED
    assert result.observation.generation_job_state == GenerationState.RETRY_WAIT
    assert failed_attempt is not None
    assert failed_attempt.provider_state == SaladJobStatus.SUCCEEDED.value
    assert failed_attempt.error_code == SALAD_WORKER_NEAR_BLACK_OUTPUT_ERROR_CODE
    assert failed_attempt.response_metadata["worker_failure_code"] == "near_black_output"
    assert failed_attempt.response_metadata["failed_output_index"] == 1
    assert failed_attempt.response_metadata["uploaded_output_count"] == 1
    assert first_retry_max_attempts == 5
    assert first_retry_state == GenerationState.RETRY_WAIT
    assert grant_count == 1
    assert repeated.applied is False
    assert repeated_grant_count == 1
    assert second_result.observation.attempt_state == GenerationAttemptState.FAILED
    assert second_result.observation.generation_job_state == GenerationState.DEAD_LETTER
    assert second_attempt_no < final_job.max_attempts
    assert final_job.max_attempts == 5
    assert final_job.state == GenerationState.DEAD_LETTER
    assert all_black_grants == 1


@pytest.mark.asyncio
async def test_infrastructure_failures_receive_only_two_idempotent_retry_slots(
    database: Database,
) -> None:
    async with database.sessions() as session:
        context = await seed_context(session)
        job = await session.get(GenerationJob, context.job_id)
        assert job is not None
        job.max_attempts = 1
        await session.commit()

        attempt_ids: list[UUID] = []
        for attempt_no in range(1, 4):
            prepared = await prepare_generation_attempt(
                session,
                generation_job_id=context.job_id,
                salad_deployment_id=context.deployment_id,
                idempotency_key=f"infra-failure-{attempt_no}",
                now=NOW + timedelta(minutes=attempt_no),
            )
            await session.commit()
            attempt_ids.append(prepared.generation_attempt_id)
            failed = remote_job(
                status=SaladJobStatus.FAILED,
                metadata={},
                update_time=NOW + timedelta(minutes=attempt_no, seconds=1),
                job_id=uuid4(),
            )
            result = await apply_salad_job_observation(
                session,
                generation_attempt_id=prepared.generation_attempt_id,
                remote_job=failed,
                observed_at=NOW + timedelta(minutes=attempt_no, seconds=2),
            )
            await session.commit()
            await session.refresh(job)
            if attempt_no < 3:
                assert result.generation_job_state == GenerationState.RETRY_WAIT
                assert job.max_attempts == attempt_no + 1
            else:
                assert result.generation_job_state == GenerationState.DEAD_LETTER
                assert job.max_attempts == 3

        replay = await apply_salad_job_observation(
            session,
            generation_attempt_id=attempt_ids[-1],
            remote_job=remote_job(
                status=SaladJobStatus.FAILED,
                metadata={},
                update_time=NOW + timedelta(minutes=4),
                job_id=UUID(
                    str(
                        (await session.get(GenerationAttempt, attempt_ids[-1])).provider_external_id
                    )
                ),
            ),
            observed_at=NOW + timedelta(minutes=4),
        )
        grants = int(
            await session.scalar(
                select(func.count(AuditEvent.id)).where(
                    AuditEvent.action == INFRASTRUCTURE_RETRY_GRANT_ACTION
                )
            )
            or 0
        )

    assert replay.generation_job_state == GenerationState.DEAD_LETTER
    assert grants == 2


@pytest.mark.asyncio
async def test_final_invalid_worker_output_does_not_extend_attempt_budget(
    database: Database,
) -> None:
    async with database.sessions() as session:
        context = await seed_context(session)
        job = await session.get(GenerationJob, context.job_id)
        assert job is not None
        job.max_attempts = 1
        await session.commit()
        attempt_id = await prepared_attempt(session, context)
        result = await apply_salad_job_observation(
            session,
            generation_attempt_id=attempt_id,
            remote_job=remote_job(
                status=SaladJobStatus.SUCCEEDED,
                metadata={},
                update_time=NOW + timedelta(minutes=1),
                output={"invalid": True},
            ),
            observed_at=NOW + timedelta(minutes=1),
        )
        await session.commit()
        await session.refresh(job)
        grants = int(
            await session.scalar(
                select(func.count(AuditEvent.id)).where(
                    AuditEvent.action == INFRASTRUCTURE_RETRY_GRANT_ACTION
                )
            )
            or 0
        )

    assert result.generation_job_state == GenerationState.DEAD_LETTER
    assert job.max_attempts == 1
    assert grants == 0


@pytest.mark.asyncio
async def test_final_rate_limit_is_bounded_infrastructure_retry(database: Database) -> None:
    async with database.sessions() as session:
        context = await seed_context(session)
        job = await session.get(GenerationJob, context.job_id)
        assert job is not None
        job.max_attempts = 1
        await session.commit()
        attempt_id = await prepared_attempt(session, context)
        result = await submit_prepared_attempt(
            session,
            FakeSaladClient(
                create_error=SaladRateLimitError(
                    message="rate limited",
                    response_body="",
                    request_id=None,
                    retry_after_seconds=30,
                )
            ),
            FakeUploadIntentProvider(),
            generation_attempt_id=attempt_id,
            webhook_url="https://controller.example.test/webhooks/salad",
            reservation_microusd=500_000,
            now=NOW,
        )
        await session.refresh(job)

    assert result.disposition == SubmissionDisposition.RETRY_WAIT
    assert job.state == GenerationState.RETRY_WAIT
    assert job.attempt_count == 1
    assert job.max_attempts == 2


@pytest.mark.asyncio
async def test_spontaneous_provider_cancel_retries_and_equal_time_terminal_wins(
    database: Database,
) -> None:
    async with database.sessions() as session:
        context = await seed_context(session)
        attempt_id = await prepared_attempt(session, context)
        remote_id = uuid4()
        running = remote_job(
            status=SaladJobStatus.RUNNING,
            metadata={},
            update_time=NOW + timedelta(minutes=1),
            job_id=remote_id,
        )
        await apply_salad_job_observation(
            session,
            generation_attempt_id=attempt_id,
            remote_job=running,
            observed_at=NOW + timedelta(minutes=1),
        )
        cancelled = remote_job(
            status=SaladJobStatus.CANCELLED,
            metadata={},
            update_time=running.update_time,
            job_id=remote_id,
        )
        result = await apply_salad_job_observation(
            session,
            generation_attempt_id=attempt_id,
            remote_job=cancelled,
            observed_at=NOW + timedelta(minutes=2),
        )
        await session.commit()
        attempt = await session.get(GenerationAttempt, attempt_id)

    assert result.applied is True
    assert result.stale is False
    assert result.attempt_state == GenerationAttemptState.FAILED
    assert result.generation_job_state == GenerationState.RETRY_WAIT
    assert attempt is not None
    assert attempt.error_code == SALAD_PROVIDER_CANCELLED_ERROR_CODE


@pytest.mark.asyncio
async def test_unknown_provider_absence_requires_three_exact_confirmations(
    database: Database,
) -> None:
    async with database.sessions() as session:
        context = await seed_context(session)
        attempt_id = await prepared_attempt(session, context)
        await submit_prepared_attempt(
            session,
            FakeSaladClient(create_error=SaladTimeoutError("timeout")),
            FakeUploadIntentProvider(),
            generation_attempt_id=attempt_id,
            webhook_url="https://controller.example.test/webhooks/salad",
            reservation_microusd=500_000,
            now=NOW,
        )
        results = []
        for minute in range(1, 4):
            results.append(
                await reconcile_generation_attempt(
                    session,
                    FakeSaladClient(list_pages={1: ()}),
                    generation_attempt_id=attempt_id,
                    list_page_size=1,
                    now=NOW + timedelta(minutes=minute),
                )
            )
        attempt = await session.get(GenerationAttempt, attempt_id)
        job = await session.get(GenerationJob, context.job_id)

    assert [item.error_code for item in results[:2]] == [
        "salad_provider_absence_pending",
        "salad_provider_absence_pending",
    ]
    assert results[2].error_code == SALAD_PROVIDER_JOB_ABSENT_ERROR_CODE
    assert attempt is not None
    assert attempt.state == GenerationAttemptState.FAILED
    assert job is not None
    assert job.state == GenerationState.RETRY_WAIT


@pytest.mark.asyncio
@pytest.mark.parametrize("source", [ReconciliationSource.LIST, ReconciliationSource.GET])
async def test_absence_does_not_overwrite_a_concurrent_terminal_commit(
    database: Database,
    source: ReconciliationSource,
) -> None:
    async with database.sessions() as session_a:
        context = await seed_context(session_a)
        attempt_id = await prepared_attempt(session_a, context)
        submit_client = (
            FakeSaladClient(create_error=SaladTimeoutError("timeout"))
            if source == ReconciliationSource.LIST
            else FakeSaladClient()
        )
        await submit_prepared_attempt(
            session_a,
            submit_client,
            FakeUploadIntentProvider(),
            generation_attempt_id=attempt_id,
            webhook_url="https://controller.example.test/webhooks/salad",
            reservation_microusd=500_000,
            now=NOW,
        )
        stale_attempt = await session_a.get(GenerationAttempt, attempt_id)
        assert stale_attempt is not None

        async def commit_terminal_state() -> None:
            async with database.sessions() as session_b:
                attempt_b = await session_b.get(GenerationAttempt, attempt_id)
                job_b = await session_b.get(GenerationJob, context.job_id)
                assert attempt_b is not None
                assert job_b is not None
                attempt_b.state = GenerationAttemptState.FAILED
                attempt_b.completed_at = NOW + timedelta(minutes=1)
                attempt_b.error_code = "concurrent_terminal_failure"
                attempt_b.error_detail = "A concurrent terminal result must remain authoritative."
                attempt_b.lock_version += 1
                job_b.state = GenerationState.DEAD_LETTER
                job_b.retry_at = None
                job_b.last_error_code = "concurrent_terminal_failure"
                job_b.last_error_detail = attempt_b.error_detail
                job_b.lock_version += 1
                await session_b.commit()

        not_found = SaladAPIError(
            status_code=404,
            message="not found",
            response_body="",
            request_id=None,
        )
        client = (
            FakeSaladClient(list_pages={1: ()}, before_list=commit_terminal_state)
            if source == ReconciliationSource.LIST
            else FakeSaladClient(get_error=not_found, before_get=commit_terminal_state)
        )
        result = await reconcile_generation_attempt(
            session_a,
            client,
            generation_attempt_id=attempt_id,
            list_page_size=1,
            now=NOW + timedelta(minutes=2),
        )

    async with database.sessions() as verification:
        attempt = await verification.get(GenerationAttempt, attempt_id)
        job = await verification.get(GenerationJob, context.job_id)
        actions = set(
            (
                await verification.scalars(
                    select(AuditEvent.action).where(
                        AuditEvent.resource_type == "generation_attempt",
                        AuditEvent.resource_id == attempt_id,
                    )
                )
            ).all()
        )

    assert result.source == source
    assert result.matched is False
    assert result.observation.stale is True
    assert result.observation.attempt_state == GenerationAttemptState.FAILED
    assert attempt is not None
    assert attempt.state == GenerationAttemptState.FAILED
    assert attempt.error_code == "concurrent_terminal_failure"
    assert "provider_absence_confirmation" not in (attempt.response_metadata or {})
    assert job is not None
    assert job.state == GenerationState.DEAD_LETTER
    assert job.max_attempts == 3
    assert job.last_error_code == "concurrent_terminal_failure"
    assert "generation_attempt.provider_absence_observed" not in actions
    assert INFRASTRUCTURE_RETRY_GRANT_ACTION not in actions


@pytest.mark.asyncio
async def test_watchdog_does_not_delete_after_a_concurrent_fresher_running_commit(
    database: Database,
) -> None:
    async with database.sessions() as session_a:
        context = await seed_context(session_a)
        attempt_id = await prepared_attempt(session_a, context)
        await submit_prepared_attempt(
            session_a,
            FakeSaladClient(),
            FakeUploadIntentProvider(),
            generation_attempt_id=attempt_id,
            webhook_url="https://controller.example.test/webhooks/salad",
            reservation_microusd=500_000,
            now=NOW,
        )
        stale_attempt = await session_a.get(GenerationAttempt, attempt_id)
        assert stale_attempt is not None
        metadata: JSONObject = {
            "generation_attempt_id": str(stale_attempt.id),
            "generation_job_id": str(stale_attempt.job_id),
            "submission_key": stale_attempt.submission_key,
            "request_sha256": stale_attempt.request_sha256,
        }

        async def commit_fresher_running_state() -> None:
            async with database.sessions() as session_b:
                observation = await apply_salad_job_observation(
                    session_b,
                    generation_attempt_id=attempt_id,
                    remote_job=remote_job(
                        status=SaladJobStatus.RUNNING,
                        metadata=metadata,
                        update_time=NOW + timedelta(minutes=2),
                    ),
                    observed_at=NOW + timedelta(minutes=2),
                )
                assert observation.applied is True
                await session_b.commit()

        client = FakeSaladClient(
            get_result=remote_job(
                status=SaladJobStatus.PENDING,
                metadata=metadata,
                update_time=NOW + timedelta(minutes=1, seconds=30),
            ),
            before_get=commit_fresher_running_state,
        )
        result = await reconcile_generation_attempt(
            session_a,
            client,
            generation_attempt_id=attempt_id,
            attempt_watchdog_seconds=60,
            now=NOW + timedelta(minutes=3),
        )

    async with database.sessions() as verification:
        attempt = await verification.get(GenerationAttempt, attempt_id)
        job = await verification.get(GenerationJob, context.job_id)
        watchdog_audits = int(
            await verification.scalar(
                select(func.count(AuditEvent.id)).where(
                    AuditEvent.resource_type == "generation_attempt",
                    AuditEvent.resource_id == attempt_id,
                    AuditEvent.action == "generation_attempt.watchdog_cancel_requested",
                )
            )
            or 0
        )

    assert client.cancel_calls == []
    assert result.observation.stale is True
    assert result.observation.attempt_state == GenerationAttemptState.RUNNING
    assert attempt is not None
    assert attempt.state == GenerationAttemptState.RUNNING
    assert attempt.started_at is not None
    assert attempt.started_at.replace(tzinfo=UTC) == NOW + timedelta(minutes=2)
    assert attempt.error_code is None
    assert job is not None
    assert job.state == GenerationState.RUNNING
    assert watchdog_audits == 0


@pytest.mark.asyncio
async def test_exact_stale_provider_match_resets_absence_confirmations(
    database: Database,
) -> None:
    not_found = SaladAPIError(
        status_code=404,
        message="not found",
        response_body="",
        request_id=None,
    )
    async with database.sessions() as session:
        context = await seed_context(session)
        attempt_id = await prepared_attempt(session, context)
        await submit_prepared_attempt(
            session,
            FakeSaladClient(),
            FakeUploadIntentProvider(),
            generation_attempt_id=attempt_id,
            webhook_url="https://controller.example.test/webhooks/salad",
            reservation_microusd=500_000,
            now=NOW,
        )
        first_miss = await reconcile_generation_attempt(
            session,
            FakeSaladClient(get_error=not_found),
            generation_attempt_id=attempt_id,
            now=NOW + timedelta(minutes=1),
        )
        attempt = await session.get(GenerationAttempt, attempt_id)
        assert attempt is not None
        metadata: JSONObject = {
            "generation_attempt_id": str(attempt.id),
            "generation_job_id": str(attempt.job_id),
            "submission_key": attempt.submission_key,
            "request_sha256": attempt.request_sha256,
        }
        stale_match = await reconcile_generation_attempt(
            session,
            FakeSaladClient(
                get_result=remote_job(
                    status=SaladJobStatus.RUNNING,
                    metadata=metadata,
                    update_time=NOW,
                )
            ),
            generation_attempt_id=attempt_id,
            now=NOW + timedelta(minutes=2),
        )
        second_miss = await reconcile_generation_attempt(
            session,
            FakeSaladClient(get_error=not_found),
            generation_attempt_id=attempt_id,
            now=NOW + timedelta(minutes=3),
        )
        attempt = await session.get(GenerationAttempt, attempt_id)

    assert first_miss.error_code == "salad_provider_absence_pending"
    assert stale_match.observation.stale is True
    assert second_miss.error_code == "salad_provider_absence_pending"
    assert attempt is not None
    tracker = (attempt.response_metadata or {})["provider_absence_confirmation"]
    assert isinstance(tracker, dict)
    assert tracker["count"] == 1
