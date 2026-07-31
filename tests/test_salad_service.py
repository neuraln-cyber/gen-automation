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
