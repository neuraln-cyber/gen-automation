from collections.abc import AsyncIterator, Mapping
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
    SaladDeploymentState,
)
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
from gen_automation.services.outbox import SALAD_JOB_SUBMIT_TOPIC
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
    calls: list[SaladJobInputContext] = field(default_factory=list)

    async def build_job_input(self, context: SaladJobInputContext) -> JSONValue:
        self.calls.append(context)
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
        )

    async def get_job(self, queue_name: str, job_id: UUID | str) -> SaladQueueJob:
        self.get_calls.append((queue_name, str(job_id)))
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
        page_size: int = 50,
    ) -> SaladQueueJobPage:
        self.list_calls.append((queue_name, page, page_size))
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
) -> SaladQueueJob:
    return SaladQueueJob(
        id=job_id,
        input={"private": "provider input is not persisted"},
        status=status,
        events=(SaladJobEvent(action="updated", time=update_time),),
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
            list_page_size=10,
            now=NOW + timedelta(minutes=1),
        )

    assert result.source == ReconciliationSource.LIST
    assert result.matched is False
    assert result.error_code == "salad_provider_job_not_found"
    assert result.observation.attempt_state == GenerationAttemptState.UNKNOWN


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
            "salad_provider_job_not_found",
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
            list_page_size=10,
            now=NOW + timedelta(minutes=1),
        )
        second = await reconcile_generation_attempt(
            session,
            FakeSaladClient(list_pages={1: ()}),
            generation_attempt_id=attempt_id,
            list_page_size=10,
            now=NOW + timedelta(minutes=2),
        )
        third = await reconcile_generation_attempt(
            session,
            FakeSaladClient(list_pages={1: ()}),
            generation_attempt_id=attempt_id,
            list_page_size=10,
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
async def test_operator_stop_full_list_pages_are_inconclusive_and_do_not_count_absence(
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
        unrelated_one = remote_job(
            status=SaladJobStatus.PENDING,
            metadata={"generation_attempt_id": "another-attempt"},
            update_time=NOW + timedelta(minutes=1),
            job_id=uuid4(),
        )
        unrelated_two = remote_job(
            status=SaladJobStatus.PENDING,
            metadata={"generation_attempt_id": "another-attempt"},
            update_time=NOW + timedelta(minutes=1),
            job_id=uuid4(),
        )

        inconclusive = await reconcile_generation_attempt(
            session,
            FakeSaladClient(list_pages={1: (unrelated_one,), 2: (unrelated_two,)}),
            generation_attempt_id=attempt_id,
            max_list_pages=2,
            list_page_size=1,
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
            list_page_size=1,
            now=NOW + timedelta(minutes=2),
        )
        after_miss = await session.get(GenerationAttempt, attempt_id)
        assert after_miss is not None
        assert after_miss.response_metadata is not None
        tracker = after_miss.response_metadata["operator_stop_absence_confirmation"]
        assert isinstance(tracker, dict)

    assert inconclusive.error_code == "operator_generation_stop_provider_scan_inconclusive"
    assert inconclusive.observation.attempt_state == GenerationAttemptState.UNKNOWN
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
                list_page_size=10,
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
            list_page_size=10,
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
        running = remote_job(
            status=SaladJobStatus.RUNNING,
            metadata=metadata,
            update_time=NOW + timedelta(minutes=104),
        )
        cancel_client = FakeSaladClient(get_result=running)

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
async def test_attempt_watchdog_leaves_active_attempt_before_deadline(
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
                update_time=NOW + timedelta(minutes=104),
            )
        )

        result = await reconcile_generation_attempt(
            session,
            client,
            generation_attempt_id=attempt_id,
            attempt_watchdog_seconds=105 * 60,
            now=NOW + timedelta(minutes=104, seconds=59),
        )

    assert result.observation.attempt_state == GenerationAttemptState.RUNNING
    assert not client.cancel_calls


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
        assert (
            cancelling_attempt.error_code
            == DEPLOYMENT_ROLLOVER_CANCEL_REQUESTED_ERROR_CODE
        )
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
                list_page_size=10,
                now=NOW + timedelta(minutes=minute),
            )
            attempt = await session.get(GenerationAttempt, attempt_id)
            job = await session.get(GenerationJob, context.job_id)
            assert pending.error_code == (
                "salad_deployment_rollover_provider_absence_pending"
            )
            assert attempt is not None
            assert attempt.state == GenerationAttemptState.UNKNOWN
            assert attempt.reservation_released_at is None
            assert job is not None
            assert job.state == GenerationState.UNKNOWN

        unavailable = await reconcile_generation_attempt(
            session,
            FakeSaladClient(list_error=SaladTransportError("temporary outage")),
            generation_attempt_id=attempt_id,
            list_page_size=10,
            now=NOW + timedelta(minutes=3),
        )
        assert unavailable.error_code == "salad_reconciliation_unavailable"

        after_reset = await reconcile_generation_attempt(
            session,
            FakeSaladClient(list_pages={1: ()}),
            generation_attempt_id=attempt_id,
            list_page_size=10,
            now=NOW + timedelta(minutes=4),
        )
        after_reset_attempt = await session.get(GenerationAttempt, attempt_id)
        assert after_reset.error_code == (
            "salad_deployment_rollover_provider_absence_pending"
        )
        assert after_reset_attempt is not None
        assert after_reset_attempt.response_metadata is not None
        tracker = after_reset_attempt.response_metadata[
            "deployment_rollover_absence_confirmation"
        ]
        assert isinstance(tracker, dict)
        assert tracker["count"] == 1
        assert after_reset_attempt.reservation_released_at is None

        for minute in (5,):
            pending = await reconcile_generation_attempt(
                session,
                FakeSaladClient(list_pages={1: ()}),
                generation_attempt_id=attempt_id,
                list_page_size=10,
                now=NOW + timedelta(minutes=minute),
            )
            assert pending.error_code == (
                "salad_deployment_rollover_provider_absence_pending"
            )

        confirmed = await reconcile_generation_attempt(
            session,
            FakeSaladClient(list_pages={1: ()}),
            generation_attempt_id=attempt_id,
            list_page_size=10,
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
            list_page_size=10,
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
                    output={"detail": "invalid request"},
                )
            ),
            generation_attempt_id=attempt_id,
            now=NOW + timedelta(minutes=2),
        )
        await session.commit()
        failed_attempt = await session.get(GenerationAttempt, attempt_id)
        retrying_job = await session.get(GenerationJob, context.job_id)

    assert result.observation.attempt_state == GenerationAttemptState.FAILED
    assert result.observation.generation_job_state == GenerationState.RETRY_WAIT
    assert failed_attempt is not None
    assert failed_attempt.provider_state == SaladJobStatus.SUCCEEDED.value
    assert failed_attempt.error_code == "salad_worker_output_invalid"
    assert failed_attempt.reservation_released_at is not None
    assert failed_attempt.response_metadata["worker_output_valid"] is False
    assert "invalid request" not in repr(failed_attempt.response_metadata)
    assert retrying_job is not None
    assert retrying_job.state == GenerationState.RETRY_WAIT
    assert retrying_job.last_error_code == "salad_worker_output_invalid"
