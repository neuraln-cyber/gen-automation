from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from gen_automation.db.models import AdminUser, I2VAttempt, I2VJob, I2VWorkerDeployment
from gen_automation.db.session import Database
from gen_automation.domain.enums import AdminRole
from gen_automation.domain.i2v import (
    I2VAttemptSnapshot,
    I2VAttemptState,
    I2VInputRegistration,
    I2VInputSource,
    I2VJobDraft,
    I2VJobSnapshot,
    I2VJobState,
)
from gen_automation.integrations.salad.errors import SaladAPIError, SaladTransportError
from gen_automation.integrations.salad.models import (
    JSONValue,
    SaladContainerGroup,
    SaladContainerGroupInstance,
    SaladContainerGroupInstancePage,
    SaladContainerGroupInstanceState,
    SaladContainerGroupPage,
    SaladContainerGroupState,
    SaladGpuClass,
    SaladJobStatus,
    SaladQueue,
    SaladQueueJob,
    SaladQueueJobPage,
)
from gen_automation.services.i2v import (
    create_i2v_job,
    register_i2v_input,
    request_i2v_job_cancellation,
)
from gen_automation.services.i2v_runtime import (
    I2V_SINGLETON_WORKER_ID,
    I2VJobInputBuilder,
    I2VRuntime,
    I2VRuntimeConfig,
    I2VRuntimeError,
)
from gen_automation.services.i2v_salad import (
    I2V_SALAD_GPU_CLASS_NAME,
    I2V_WORKER_OUTPUT_SCHEMA,
    I2VSaladConfig,
)

_NOW = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)
_GPU_ID = UUID("11111111-1111-4111-8111-111111111111")
_QUEUE_ID = UUID("22222222-2222-4222-8222-222222222222")
_GROUP_ID = UUID("33333333-3333-4333-8333-333333333333")
_IMAGE = "ghcr.io/example/i2v@sha256:" + "a" * 64


@pytest.fixture
async def runtime_database(tmp_path: Path) -> AsyncIterator[tuple[Database, UUID, UUID]]:
    database_path = tmp_path / "i2v-runtime.db"
    database = Database(f"sqlite+aiosqlite:///{database_path.as_posix()}")
    await database.create_schema()
    async with database.sessions() as session:
        owner = AdminUser(
            username_normalized="runtime-owner",
            display_name="Runtime Owner",
            password_hash="disabled-test-password-hash",  # noqa: S106
            role=AdminRole.OWNER,
            is_active=True,
            failed_login_count=0,
            password_changed_at=_NOW,
            lock_version=1,
        )
        session.add(owner)
        await session.commit()
        source = await register_i2v_input(
            session,
            actor_user_id=owner.id,
            registration=I2VInputRegistration(
                source=I2VInputSource.UPLOAD,
                display_name="runtime source",
                storage_backend="s3",
                storage_bucket="private-i2v",
                object_key="i2v/inputs/runtime.png",
                object_version_id="v1",
                sha256="b" * 64,
                content_type="image/png",
                width=1280,
                height=720,
                byte_size=1_000_000,
            ),
            now=_NOW,
        )
        yield database, owner.id, source.input_id
    await database.dispose()


class SignedGrantBuilder(I2VJobInputBuilder):
    async def build(
        self,
        *,
        job: I2VJobSnapshot,
        attempt: I2VAttemptSnapshot,
    ) -> Mapping[str, JSONValue]:
        expires_at = (_NOW + timedelta(days=6)).isoformat()
        return {
            "input_grant": {
                "method": "GET",
                "url": "https://private.example.test/input?signature=secret",
                "expires_at": expires_at,
            },
            "output_grant": {
                "method": "PUT",
                "url": "https://private.example.test/output?signature=secret",
                "headers": {"Content-Type": "video/mp4"},
                "storage_backend": "s3",
                "storage_bucket": "private-i2v",
                "object_key": f"i2v/outputs/{job.job_id}/{attempt.attempt_id}.mp4",
                "expires_at": expires_at,
            },
        }

    async def verify_output(self, **_kwargs: object) -> None:
        return None


class FakeRuntimeSalad:
    def __init__(self) -> None:
        self.queue = _queue()
        self.group = _group()
        self.instances = (_instance("instance-1", "machine-1"),)
        self.jobs: dict[UUID, SaladQueueJob] = {}
        self.provider_mutations: list[str] = []
        self.stopped_group_names: list[str] = []
        self.submission_calls = 0
        self.raise_ambiguous_once = False
        self.last_input: JSONValue = None

    async def list_gpu_classes(self) -> tuple[SaladGpuClass, ...]:
        return (_gpu(),)

    async def get_queue(self, _name: str) -> SaladQueue:
        return self.queue

    async def create_queue(self, *_args: object, **_kwargs: object) -> SaladQueue:
        raise AssertionError("deterministic queue already exists")

    async def get_container_group(self, _name: str) -> SaladContainerGroup:
        return self.group

    async def list_container_groups(self) -> SaladContainerGroupPage:
        return SaladContainerGroupPage(items=(self.group,))

    async def create_container_group(self, _configuration: object) -> SaladContainerGroup:
        raise AssertionError("deterministic group already exists")

    async def list_container_group_instances(self, _name: str) -> SaladContainerGroupInstancePage:
        return SaladContainerGroupInstancePage(instances=self.instances)

    async def start_container_group(self, _name: str) -> None:
        self.provider_mutations.append("start_group")

    async def stop_container_group(self, name: str) -> None:
        self.provider_mutations.append("stop_group")
        self.stopped_group_names.append(name)

    async def create_job(
        self,
        _queue_name: str,
        *,
        input: JSONValue,
        metadata: Mapping[str, JSONValue] | None = None,
        webhook: str | None = None,
    ) -> SaladQueueJob:
        del webhook
        self.submission_calls += 1
        self.provider_mutations.append("create_job")
        self.last_input = input
        assert metadata is not None
        remote = SaladQueueJob(
            id=uuid4(),
            input=input,
            status=SaladJobStatus.PENDING,
            events=(),
            create_time=_NOW,
            update_time=_NOW,
            metadata=dict(metadata),
            webhook=None,
            output=None,
        )
        self.jobs[remote.id] = remote
        if self.raise_ambiguous_once:
            self.raise_ambiguous_once = False
            raise SaladTransportError("response lost after POST")
        return remote

    async def get_job(self, _queue_name: str, job_id: UUID | str) -> SaladQueueJob:
        try:
            return self.jobs[UUID(str(job_id))]
        except KeyError as error:
            raise SaladAPIError(
                status_code=404,
                message="job not found",
                response_body="{}",
                request_id=None,
            ) from error

    async def list_jobs(
        self,
        _queue_name: str,
        *,
        page: int = 1,
        page_size: int = 25,
    ) -> SaladQueueJobPage:
        jobs = tuple(self.jobs.values())
        start = (page - 1) * page_size
        return SaladQueueJobPage(items=jobs[start : start + page_size])

    async def cancel_job(self, _queue_name: str, _job_id: UUID | str) -> None:
        self.provider_mutations.append("cancel_job")

    def set_all_job_status(self, status: SaladJobStatus) -> None:
        self.jobs = {
            job_id: SaladQueueJob(
                id=job.id,
                input=job.input,
                status=status,
                events=job.events,
                create_time=job.create_time,
                update_time=_NOW,
                metadata=job.metadata,
                webhook=job.webhook,
                output=(
                    {
                        "schema": I2V_WORKER_OUTPUT_SCHEMA,
                        "job_id": job.input["job_id"],
                        "attempt_id": job.input["attempt_id"],
                        "request_sha256": job.input["request_sha256"],
                        "output": {
                            "storage_backend": "s3",
                            "storage_bucket": "private-i2v",
                            "object_key": (
                                f"i2v/outputs/{job.input['job_id']}/{job.input['attempt_id']}.mp4"
                            ),
                            "sha256": "c" * 64,
                            "content_type": "video/mp4",
                            "width": 1280,
                            "height": 720,
                            "frame_count": 81,
                            "fps": 24,
                            "duration_ms": 3375,
                            "byte_size": 8_000_000,
                        },
                    }
                    if status == SaladJobStatus.SUCCEEDED
                    else None
                ),
            )
            for job_id, job in self.jobs.items()
        }


def _runtime(
    database: Database,
    client: FakeRuntimeSalad,
    *,
    warm_idle_seconds: int | None = 1800,
    prefetch: int = 3,
    worker_id: str = I2V_SINGLETON_WORKER_ID,
    reviewed_loras_enabled: bool = False,
) -> I2VRuntime:
    return I2VRuntime(
        config=I2VRuntimeConfig(
            salad=I2VSaladConfig(
                queue_name="i2v-dasiwa-v1",
                container_group_name="i2v-dasiwa-5090-v1",
                worker_image=_IMAGE,
                gpu_class_id=_GPU_ID,
                prefetch=prefetch,
                worker_lease_seconds=86_400,
                warm_idle_seconds=warm_idle_seconds,
            ),
            reviewed_loras_enabled=reviewed_loras_enabled,
        ),
        sessions=database.sessions,
        salad_client=client,
        worker_id=worker_id,
        input_builder=SignedGrantBuilder(),
    )


async def _queue_job(
    database: Database,
    *,
    owner_id: UUID,
    input_id: UUID,
    index: int = 0,
    settings: dict[str, object] | None = None,
) -> UUID:
    async with database.sessions() as session:
        job = await create_i2v_job(
            session,
            actor_user_id=owner_id,
            draft=I2VJobDraft(
                input_id=input_id,
                positive_prompt=f"smooth motion {index}",
                settings=settings or {"seed": index},
            ),
            now=_NOW + timedelta(seconds=index),
        )
        return job.job_id


async def _durable_job(database: Database, job_id: UUID) -> tuple[I2VJob, I2VAttempt | None]:
    async with database.sessions() as session:
        job = await session.get(I2VJob, job_id)
        assert job is not None
        attempt = await session.scalar(
            select(I2VAttempt)
            .where(I2VAttempt.job_id == job_id)
            .order_by(I2VAttempt.attempt_no.desc())
        )
        session.expunge(job)
        if attempt is not None:
            session.expunge(attempt)
        return job, attempt


async def test_worker_capability_gates_reviewed_dispatch_without_provider_post(
    runtime_database: tuple[Database, UUID, UUID],
) -> None:
    database, owner_id, input_id = runtime_database
    lora_settings = {"loras": [{"catalog_id": "wan-general-nsfw-v0.08a", "strength": 0.3}]}
    job_id = await _queue_job(
        database,
        owner_id=owner_id,
        input_id=input_id,
        settings=lora_settings,
    )
    client = FakeRuntimeSalad()
    paused = _runtime(database, client, reviewed_loras_enabled=False)

    assert (await paused.run_cycle(now=_NOW)).action == "deployment_observed"
    assert (await paused.run_cycle(now=_NOW + timedelta(seconds=1))).action == "idle"
    job, attempt = await _durable_job(database, job_id)
    assert job.state == I2VJobState.QUEUED
    assert job.attempt_count == 0 and attempt is None
    assert client.submission_calls == 0

    capable = _runtime(database, client, reviewed_loras_enabled=True)
    assert (
        await capable.run_cycle(now=_NOW + timedelta(seconds=2))
    ).action == "deployment_observed"
    assert (await capable.run_cycle(now=_NOW + timedelta(seconds=3))).action == "job_claimed"
    assert (
        await capable.run_cycle(now=_NOW + timedelta(seconds=4))
    ).action == "provider_job_submitted"
    assert client.submission_calls == 1


async def test_pending_pull_never_starts_inference_and_success_completes(
    runtime_database: tuple[Database, UUID, UUID],
) -> None:
    database, owner_id, input_id = runtime_database
    job_id = await _queue_job(database, owner_id=owner_id, input_id=input_id)
    client = FakeRuntimeSalad()
    runtime = _runtime(database, client)

    assert (await runtime.run_cycle(now=_NOW)).action == "deployment_observed"
    assert (await runtime.run_cycle(now=_NOW + timedelta(seconds=1))).action == "job_claimed"
    assert (
        await runtime.run_cycle(now=_NOW + timedelta(seconds=2))
    ).action == "provider_job_submitted"
    assert client.submission_calls == 1
    assert isinstance(client.last_input, dict)
    assert client.last_input["schema"] == "i2v-salad-job/v1"
    input_grant = client.last_input["input_grant"]
    assert isinstance(input_grant, dict)
    assert "signature=secret" in str(input_grant["url"])

    # Deployment counters may update, but a provider-pending job stays claimed.
    await runtime.run_cycle(now=_NOW + timedelta(seconds=3))
    await runtime.run_cycle(now=_NOW + timedelta(seconds=4))
    job, attempt = await _durable_job(database, job_id)
    assert job.state == I2VJobState.CLAIMED
    assert attempt is not None and attempt.state == I2VAttemptState.CREATED
    assert attempt.started_at is None
    assert "signature=secret" not in str(attempt.request_metadata)

    client.set_all_job_status(SaladJobStatus.RUNNING)
    assert (await runtime.run_cycle(now=_NOW + timedelta(seconds=5))).action == "inference_started"
    job, attempt = await _durable_job(database, job_id)
    assert job.state == I2VJobState.RUNNING
    assert attempt is not None and _utc(attempt.started_at) == _NOW + timedelta(seconds=5)

    client.set_all_job_status(SaladJobStatus.SUCCEEDED)
    assert (await runtime.run_cycle(now=_NOW + timedelta(seconds=6))).action == "output_completed"
    job, _attempt = await _durable_job(database, job_id)
    assert job.state == I2VJobState.SUCCEEDED


async def test_ambiguous_post_is_recovered_before_any_resubmit(
    runtime_database: tuple[Database, UUID, UUID],
) -> None:
    database, owner_id, input_id = runtime_database
    job_id = await _queue_job(database, owner_id=owner_id, input_id=input_id)
    client = FakeRuntimeSalad()
    client.raise_ambiguous_once = True
    runtime = _runtime(database, client)

    await runtime.run_cycle(now=_NOW)
    await runtime.run_cycle(now=_NOW + timedelta(seconds=1))
    unknown = await runtime.run_cycle(now=_NOW + timedelta(seconds=2))
    assert unknown.action == "submission_unknown"
    assert client.submission_calls == 1

    recovered = await runtime.run_cycle(now=_NOW + timedelta(seconds=3))
    assert recovered.action == "provider_pending_observed"
    assert client.submission_calls == 1
    job, attempt = await _durable_job(database, job_id)
    assert job.state == I2VJobState.CLAIMED
    assert attempt is not None and attempt.provider_job_id == str(next(iter(client.jobs)))


async def test_current_owner_retries_only_after_unknown_submission_scan_is_empty(
    runtime_database: tuple[Database, UUID, UUID],
) -> None:
    database, owner_id, input_id = runtime_database
    job_id = await _queue_job(database, owner_id=owner_id, input_id=input_id)
    client = FakeRuntimeSalad()
    runtime = _runtime(database, client)
    await runtime.run_cycle(now=_NOW)
    await runtime.run_cycle(now=_NOW + timedelta(seconds=1))
    _job, attempt = await _durable_job(database, job_id)
    assert attempt is not None
    async with database.sessions() as session:
        durable_attempt = await session.get(I2VAttempt, attempt.id)
        assert durable_attempt is not None
        durable_attempt.request_metadata = {
            **durable_attempt.request_metadata,
            "submission_state": "unknown",
        }
        await session.commit()

    retried = await runtime.run_cycle(now=_NOW + timedelta(seconds=2))
    assert retried.action == "provider_job_submitted"
    assert client.submission_calls == 1


async def test_reallocation_updates_machine_without_replacing_attempt(
    runtime_database: tuple[Database, UUID, UUID],
) -> None:
    database, owner_id, input_id = runtime_database
    job_id = await _queue_job(database, owner_id=owner_id, input_id=input_id)
    client = FakeRuntimeSalad()
    runtime = _runtime(database, client)
    for offset in range(4):
        await runtime.run_cycle(now=_NOW + timedelta(seconds=offset))
    before_job, before_attempt = await _durable_job(database, job_id)
    assert before_attempt is not None

    client.instances = (
        SaladContainerGroupInstance(
            id="old-instance",
            machine_id="old-machine",
            state=SaladContainerGroupInstanceState.DOWNLOADING,
            update_time=_NOW,
            version=1,
            ready=False,
            started=False,
        ),
        _instance("replacement-instance", "replacement-machine"),
    )
    cycle = await runtime.run_cycle(now=_NOW + timedelta(seconds=10))
    assert cycle.action == "deployment_observed"
    after_job, after_attempt = await _durable_job(database, job_id)
    assert after_job.attempt_count == before_job.attempt_count == 1
    assert after_attempt is not None and after_attempt.id == before_attempt.id
    assert after_attempt.provider_job_id == before_attempt.provider_job_id


async def test_provider_evidence_refreshes_expired_lease_without_deadline(
    runtime_database: tuple[Database, UUID, UUID],
) -> None:
    database, owner_id, input_id = runtime_database
    job_id = await _queue_job(database, owner_id=owner_id, input_id=input_id)
    client = FakeRuntimeSalad()
    runtime = _runtime(database, client)
    for offset in range(4):
        await runtime.run_cycle(now=_NOW + timedelta(seconds=offset))
    _job, attempt = await _durable_job(database, job_id)
    assert attempt is not None

    far_future = _NOW + timedelta(days=30)
    refreshed = await runtime.run_cycle(now=far_future)
    assert refreshed.action == "provider_backed_lease_refreshed"
    durable, same_attempt = await _durable_job(database, job_id)
    assert durable.state == I2VJobState.CLAIMED
    assert _utc(durable.lease_expires_at) == far_future + timedelta(days=1)
    assert same_attempt is not None and same_attempt.id == attempt.id


async def test_restart_adopts_exact_provider_attempt_before_applying_transition(
    runtime_database: tuple[Database, UUID, UUID],
) -> None:
    database, owner_id, input_id = runtime_database
    job_id = await _queue_job(database, owner_id=owner_id, input_id=input_id)
    client = FakeRuntimeSalad()
    before_restart = _runtime(
        database,
        client,
        worker_id="controller-before-restart:i2v",
    )
    await before_restart.run_cycle(now=_NOW)
    await before_restart.run_cycle(now=_NOW + timedelta(seconds=1))
    submitted = await before_restart.run_cycle(now=_NOW + timedelta(seconds=2))
    assert submitted.action == "provider_job_submitted"
    before_job, before_attempt = await _durable_job(database, job_id)
    assert before_attempt is not None and before_attempt.provider_job_id is not None

    after_restart = _runtime(
        database,
        client,
        worker_id=I2V_SINGLETON_WORKER_ID,
    )
    client.set_all_job_status(SaladJobStatus.RUNNING)
    transitioned = await after_restart.run_cycle(now=_NOW + timedelta(seconds=3))
    assert transitioned.action == "inference_started"
    adopted_job, adopted_attempt = await _durable_job(database, job_id)
    assert adopted_job.state == I2VJobState.RUNNING
    assert adopted_job.lease_owner == I2V_SINGLETON_WORKER_ID
    assert _utc(adopted_job.lease_expires_at) == _NOW + timedelta(days=1, seconds=3)
    assert adopted_job.attempt_count == before_job.attempt_count == 1
    assert adopted_attempt is not None
    assert adopted_attempt.id == before_attempt.id
    assert adopted_attempt.worker_id == I2V_SINGLETON_WORKER_ID
    assert adopted_attempt.provider_job_id == before_attempt.provider_job_id
    assert client.submission_calls == 1

    # A briefly overlapping retired controller may observe the same exact
    # provider job, but one-way migration prevents it from stealing ownership
    # back from the stable singleton.
    retired = await before_restart.run_cycle(now=_NOW + timedelta(milliseconds=3500))
    assert retired.action == "restart_reconciliation_waiting"
    assert retired.changed is False
    still_adopted_job, still_adopted_attempt = await _durable_job(database, job_id)
    assert still_adopted_job.lease_owner == I2V_SINGLETON_WORKER_ID
    assert still_adopted_attempt is not None
    assert still_adopted_attempt.worker_id == I2V_SINGLETON_WORKER_ID
    assert client.submission_calls == 1

    client.set_all_job_status(SaladJobStatus.SUCCEEDED)
    completed = await after_restart.run_cycle(now=_NOW + timedelta(seconds=4))
    assert completed.action == "output_completed"
    completed_job, completed_attempt = await _durable_job(database, job_id)
    assert completed_job.state == I2VJobState.SUCCEEDED
    assert completed_attempt is not None
    assert completed_attempt.state == I2VAttemptState.SUCCEEDED
    assert completed_attempt.worker_id == I2V_SINGLETON_WORKER_ID
    assert client.submission_calls == 1


async def test_restart_rejects_unvalidated_provider_identity_without_adoption(
    runtime_database: tuple[Database, UUID, UUID],
) -> None:
    database, owner_id, input_id = runtime_database
    job_id = await _queue_job(database, owner_id=owner_id, input_id=input_id)
    client = FakeRuntimeSalad()
    before_restart = _runtime(
        database,
        client,
        worker_id="controller-before-restart:i2v",
    )
    await before_restart.run_cycle(now=_NOW)
    await before_restart.run_cycle(now=_NOW + timedelta(seconds=1))
    await before_restart.run_cycle(now=_NOW + timedelta(seconds=2))
    before_job, before_attempt = await _durable_job(database, job_id)
    assert before_attempt is not None and before_attempt.provider_job_id is not None
    remote_id = UUID(before_attempt.provider_job_id)
    remote = client.jobs[remote_id]
    client.jobs[remote_id] = SaladQueueJob(
        id=remote.id,
        input=remote.input,
        status=remote.status,
        events=remote.events,
        create_time=remote.create_time,
        update_time=remote.update_time,
        metadata={**remote.metadata, "i2v_attempt_id": str(uuid4())},
        webhook=remote.webhook,
        output=remote.output,
    )

    after_restart = _runtime(
        database,
        client,
        worker_id=I2V_SINGLETON_WORKER_ID,
    )
    with pytest.raises(I2VRuntimeError, match="identity does not match"):
        await after_restart.run_cycle(now=_NOW + timedelta(seconds=3))
    unchanged_job, unchanged_attempt = await _durable_job(database, job_id)
    assert unchanged_job.state == before_job.state == I2VJobState.CLAIMED
    assert unchanged_job.lease_owner == "controller-before-restart:i2v"
    assert unchanged_job.attempt_count == 1
    assert unchanged_attempt is not None
    assert unchanged_attempt.id == before_attempt.id
    assert unchanged_attempt.worker_id == "controller-before-restart:i2v"
    assert unchanged_attempt.provider_job_id == before_attempt.provider_job_id
    assert client.submission_calls == 1


async def test_restart_does_not_adopt_or_requeue_an_absent_provider_job(
    runtime_database: tuple[Database, UUID, UUID],
) -> None:
    database, owner_id, input_id = runtime_database
    job_id = await _queue_job(database, owner_id=owner_id, input_id=input_id)
    client = FakeRuntimeSalad()
    before_restart = _runtime(
        database,
        client,
        worker_id="controller-before-restart:i2v",
    )
    await before_restart.run_cycle(now=_NOW)
    await before_restart.run_cycle(now=_NOW + timedelta(seconds=1))
    await before_restart.run_cycle(now=_NOW + timedelta(seconds=2))
    before_job, before_attempt = await _durable_job(database, job_id)
    assert before_attempt is not None and before_attempt.provider_job_id is not None
    client.jobs.clear()

    after_restart = _runtime(
        database,
        client,
        worker_id=I2V_SINGLETON_WORKER_ID,
    )
    waiting = await after_restart.run_cycle(now=_NOW + timedelta(days=30))
    assert waiting.action == "restart_reconciliation_waiting"
    assert waiting.changed is False
    unchanged_job, unchanged_attempt = await _durable_job(database, job_id)
    assert unchanged_job.state == before_job.state == I2VJobState.CLAIMED
    assert unchanged_job.lease_owner == "controller-before-restart:i2v"
    assert unchanged_job.queue_position is None
    assert unchanged_job.attempt_count == 1
    assert unchanged_attempt is not None
    assert unchanged_attempt.id == before_attempt.id
    assert unchanged_attempt.worker_id == "controller-before-restart:i2v"
    assert unchanged_attempt.provider_job_id == before_attempt.provider_job_id
    assert client.submission_calls == 1


async def test_restart_adopts_and_binds_exact_discovered_provider_job_without_resubmit(
    runtime_database: tuple[Database, UUID, UUID],
) -> None:
    database, owner_id, input_id = runtime_database
    job_id = await _queue_job(database, owner_id=owner_id, input_id=input_id)
    client = FakeRuntimeSalad()
    legacy = _runtime(database, client, worker_id="controller-legacy:i2v")
    await legacy.run_cycle(now=_NOW)
    await legacy.run_cycle(now=_NOW + timedelta(seconds=1))
    await legacy.run_cycle(now=_NOW + timedelta(seconds=2))
    before_job, before_attempt = await _durable_job(database, job_id)
    assert before_attempt is not None and before_attempt.provider_job_id is not None
    exact_provider_job_id = before_attempt.provider_job_id

    # Model the crash window where Salad accepted the deterministic submission
    # but its ID was not durably bound to the attempt.
    async with database.sessions() as session:
        attempt = await session.get(I2VAttempt, before_attempt.id)
        assert attempt is not None
        attempt.provider_job_id = None
        attempt.request_metadata = {
            **attempt.request_metadata,
            "submission_state": "unknown",
        }
        await session.commit()

    singleton = _runtime(database, client, worker_id=I2V_SINGLETON_WORKER_ID)
    adopted = await singleton.run_cycle(now=_NOW + timedelta(seconds=3))
    assert adopted.action == "provider_attempt_adopted"
    durable_job, durable_attempt = await _durable_job(database, job_id)
    assert durable_job.lease_owner == I2V_SINGLETON_WORKER_ID
    assert durable_job.attempt_count == before_job.attempt_count == 1
    assert durable_attempt is not None and durable_attempt.id == before_attempt.id
    assert durable_attempt.worker_id == I2V_SINGLETON_WORKER_ID
    assert durable_attempt.provider_job_id == exact_provider_job_id
    assert client.submission_calls == 1


async def test_restart_with_unbound_absent_provider_waits_then_recovers_without_post(
    runtime_database: tuple[Database, UUID, UUID],
) -> None:
    database, owner_id, input_id = runtime_database
    job_id = await _queue_job(database, owner_id=owner_id, input_id=input_id)
    client = FakeRuntimeSalad()
    legacy = _runtime(database, client, worker_id="controller-legacy:i2v")
    await legacy.run_cycle(now=_NOW)
    assert (await legacy.run_cycle(now=_NOW + timedelta(seconds=1))).action == "job_claimed"
    claimed_job, claimed_attempt = await _durable_job(database, job_id)
    assert claimed_attempt is not None and claimed_attempt.provider_job_id is None
    assert claimed_attempt.request_metadata["submission_state"] == "prepared"

    singleton = _runtime(database, client, worker_id=I2V_SINGLETON_WORKER_ID)
    waiting = await singleton.run_cycle(now=_NOW + timedelta(seconds=2))
    assert waiting.action == "restart_reconciliation_waiting"
    assert waiting.changed is False
    unchanged_job, unchanged_attempt = await _durable_job(database, job_id)
    assert unchanged_job.lease_owner == claimed_job.lease_owner
    assert unchanged_attempt is not None and unchanged_attempt.id == claimed_attempt.id
    assert client.submission_calls == 0

    far_future = _NOW + timedelta(days=2)
    actions: list[str] = []
    for _ in range(3):
        cycle = await singleton.run_cycle(now=far_future)
        actions.append(cycle.action)
        if cycle.action == "expired_claims_recovered":
            break
    assert "expired_claims_recovered" in actions
    recovered_job, recovered_attempt = await _durable_job(database, job_id)
    assert recovered_job.state == I2VJobState.QUEUED
    assert recovered_job.attempt_count == 1
    assert recovered_attempt is not None and recovered_attempt.state == I2VAttemptState.FAILED
    assert client.submission_calls == 0


async def test_expired_blank_metadata_reviewed_claim_recovers_without_post(
    runtime_database: tuple[Database, UUID, UUID],
) -> None:
    database, owner_id, input_id = runtime_database
    job_id = await _queue_job(
        database,
        owner_id=owner_id,
        input_id=input_id,
        settings={"loras": [{"catalog_id": "wan-general-nsfw-v0.08a", "strength": 0.3}]},
    )
    client = FakeRuntimeSalad()
    capable = _runtime(database, client, reviewed_loras_enabled=True)
    await capable.run_cycle(now=_NOW)
    assert (await capable.run_cycle(now=_NOW + timedelta(seconds=1))).action == "job_claimed"
    _job, attempt = await _durable_job(database, job_id)
    assert attempt is not None
    async with database.sessions() as session:
        row = await session.get(I2VAttempt, attempt.id)
        assert row is not None
        row.request_metadata = {}
        await session.commit()

    paused = _runtime(database, client, reviewed_loras_enabled=False)
    far_future = _NOW + timedelta(days=2)
    actions = [(await paused.run_cycle(now=far_future)).action]
    actions.append((await paused.run_cycle(now=far_future)).action)
    assert "expired_claims_recovered" in actions
    recovered, recovered_attempt = await _durable_job(database, job_id)
    assert recovered.state == I2VJobState.QUEUED
    assert recovered_attempt is not None and recovered_attempt.state == I2VAttemptState.FAILED
    assert client.submission_calls == 0


async def test_restart_rejects_different_remote_id_for_durably_bound_provider_job(
    runtime_database: tuple[Database, UUID, UUID],
) -> None:
    database, owner_id, input_id = runtime_database
    job_id = await _queue_job(database, owner_id=owner_id, input_id=input_id)
    client = FakeRuntimeSalad()
    runtime = _runtime(database, client)
    await runtime.run_cycle(now=_NOW)
    await runtime.run_cycle(now=_NOW + timedelta(seconds=1))
    await runtime.run_cycle(now=_NOW + timedelta(seconds=2))
    before_job, before_attempt = await _durable_job(database, job_id)
    assert before_attempt is not None and before_attempt.provider_job_id is not None
    original = client.jobs.pop(UUID(before_attempt.provider_job_id))
    replacement = SaladQueueJob(
        id=uuid4(),
        input=original.input,
        status=original.status,
        events=original.events,
        create_time=original.create_time,
        update_time=original.update_time,
        metadata=original.metadata,
        webhook=original.webhook,
        output=original.output,
    )
    client.jobs[replacement.id] = replacement

    with pytest.raises(I2VRuntimeError, match="durable provider job"):
        await runtime.run_cycle(now=_NOW + timedelta(seconds=3))
    unchanged_job, unchanged_attempt = await _durable_job(database, job_id)
    assert unchanged_job.lease_owner == before_job.lease_owner
    assert unchanged_attempt is not None
    assert unchanged_attempt.provider_job_id == before_attempt.provider_job_id
    assert client.submission_calls == 1


async def test_cancel_requested_provider_success_settles_as_cancelled(
    runtime_database: tuple[Database, UUID, UUID],
) -> None:
    database, owner_id, input_id = runtime_database
    job_id = await _queue_job(database, owner_id=owner_id, input_id=input_id)
    client = FakeRuntimeSalad()
    runtime = _runtime(database, client)
    await runtime.run_cycle(now=_NOW)
    await runtime.run_cycle(now=_NOW + timedelta(seconds=1))
    await runtime.run_cycle(now=_NOW + timedelta(seconds=2))
    client.set_all_job_status(SaladJobStatus.RUNNING)
    await runtime.run_cycle(now=_NOW + timedelta(seconds=3))
    async with database.sessions() as session:
        requested = await request_i2v_job_cancellation(
            session,
            actor_user_id=owner_id,
            job_id=job_id,
            now=_NOW + timedelta(seconds=4),
        )
    assert requested.state == I2VJobState.CANCEL_REQUESTED

    client.set_all_job_status(SaladJobStatus.SUCCEEDED)
    settled = await runtime.run_cycle(now=_NOW + timedelta(seconds=5))
    assert settled.action == "cancellation_acknowledged"
    durable_job, durable_attempt = await _durable_job(database, job_id)
    assert durable_job.state == I2VJobState.CANCELLED
    assert durable_attempt is not None
    assert durable_attempt.state == I2VAttemptState.CANCELLED
    assert durable_attempt.response_metadata["provider_status"] == "succeeded"
    assert durable_attempt.response_metadata["provider_job_id"] == str(next(iter(client.jobs)))
    assert "cancel_job" not in client.provider_mutations


@pytest.mark.parametrize("warm_idle_seconds", (900, 1800))
async def test_warm_idle_can_stop_or_remain_manual_unbounded(
    runtime_database: tuple[Database, UUID, UUID],
    warm_idle_seconds: int,
) -> None:
    database, _owner_id, _input_id = runtime_database
    client = FakeRuntimeSalad()
    runtime = _runtime(database, client, warm_idle_seconds=warm_idle_seconds)
    assert (await runtime.run_cycle(now=_NOW)).action == "deployment_observed"
    stopped = await runtime.run_cycle(now=_NOW + timedelta(seconds=warm_idle_seconds))
    assert stopped.action == "group_stop_requested"
    assert client.provider_mutations == ["stop_group"]
    assert client.stopped_group_names == ["i2v-dasiwa-5090-v1"]

    client.provider_mutations.clear()
    manual = _runtime(database, client, warm_idle_seconds=None)
    idle = await manual.run_cycle(now=_NOW + timedelta(days=365))
    assert idle.action in {"idle", "deployment_observed"}
    assert client.provider_mutations == []


@pytest.mark.parametrize(
    ("instance_id", "machine_id", "version"),
    (
        ("instance-2", "machine-1", 1),
        ("instance-1", "machine-2", 1),
        ("instance-1", "machine-1", 2),
    ),
)
async def test_new_ready_worker_identity_earns_a_fresh_warm_idle_window(
    runtime_database: tuple[Database, UUID, UUID],
    instance_id: str,
    machine_id: str,
    version: int,
) -> None:
    database, _owner_id, _input_id = runtime_database
    client = FakeRuntimeSalad()
    runtime = _runtime(database, client, warm_idle_seconds=1800)
    assert (await runtime.run_cycle(now=_NOW)).action == "deployment_observed"

    replacement_ready_at = _NOW + timedelta(seconds=1801)
    client.instances = (_instance(instance_id, machine_id, version=version),)
    observed = await runtime.run_cycle(now=replacement_ready_at)
    assert observed.action == "deployment_observed"
    assert client.provider_mutations == []
    async with database.sessions() as session:
        deployment = await session.scalar(select(I2VWorkerDeployment))
        assert deployment is not None
        assert deployment.deployment_metadata["idle_since"] == replacement_ready_at.isoformat()

    before_deadline = await runtime.run_cycle(now=replacement_ready_at + timedelta(seconds=1799))
    assert before_deadline.action == "idle"
    assert client.provider_mutations == []
    at_deadline = await runtime.run_cycle(now=replacement_ready_at + timedelta(seconds=1800))
    assert at_deadline.action == "group_stop_requested"
    assert client.provider_mutations == ["stop_group"]
    assert client.stopped_group_names == ["i2v-dasiwa-5090-v1"]


@pytest.mark.parametrize("warm_idle_seconds", (900, 1800))
async def test_nonready_to_ready_transition_earns_a_fresh_warm_idle_window(
    runtime_database: tuple[Database, UUID, UUID],
    warm_idle_seconds: int,
) -> None:
    database, _owner_id, _input_id = runtime_database
    client = FakeRuntimeSalad()
    runtime = _runtime(database, client, warm_idle_seconds=warm_idle_seconds)
    assert (await runtime.run_cycle(now=_NOW)).action == "deployment_observed"

    client.instances = (
        _instance(
            "instance-1",
            "machine-1",
            state=SaladContainerGroupInstanceState.DOWNLOADING,
            ready=False,
            started=False,
        ),
    )
    provisioning_at = _NOW + timedelta(seconds=1801)
    assert (await runtime.run_cycle(now=provisioning_at)).action == "deployment_observed"
    async with database.sessions() as session:
        deployment = await session.scalar(select(I2VWorkerDeployment))
        assert deployment is not None
        assert deployment.deployment_metadata["idle_since"] is None

    ready_at = provisioning_at + timedelta(hours=2)
    client.instances = (_instance("instance-1", "machine-1"),)
    assert (await runtime.run_cycle(now=ready_at)).action == "deployment_observed"
    assert client.provider_mutations == []
    async with database.sessions() as session:
        deployment = await session.scalar(select(I2VWorkerDeployment))
        assert deployment is not None
        assert deployment.deployment_metadata["idle_since"] == ready_at.isoformat()

    assert (
        await runtime.run_cycle(now=ready_at + timedelta(seconds=warm_idle_seconds - 1))
    ).action == "idle"
    assert client.provider_mutations == []
    assert (
        await runtime.run_cycle(now=ready_at + timedelta(seconds=warm_idle_seconds))
    ).action == "group_stop_requested"
    assert client.provider_mutations == ["stop_group"]
    assert client.stopped_group_names == ["i2v-dasiwa-5090-v1"]


async def test_promoted_worker_image_earns_a_fresh_warm_idle_window(
    runtime_database: tuple[Database, UUID, UUID],
) -> None:
    database, _owner_id, _input_id = runtime_database
    client = FakeRuntimeSalad()
    runtime = _runtime(database, client, warm_idle_seconds=1800)
    assert (await runtime.run_cycle(now=_NOW)).action == "deployment_observed"
    async with database.sessions() as session:
        deployment = await session.scalar(select(I2VWorkerDeployment).with_for_update())
        assert deployment is not None
        deployment.worker_image_digest = "ghcr.io/example/i2v@sha256:" + "f" * 64
        await session.commit()

    promoted_ready_at = _NOW + timedelta(seconds=1801)
    assert (await runtime.run_cycle(now=promoted_ready_at)).action == "deployment_observed"
    assert client.provider_mutations == []
    async with database.sessions() as session:
        deployment = await session.scalar(select(I2VWorkerDeployment))
        assert deployment is not None
        assert deployment.worker_image_digest == _IMAGE
        assert deployment.deployment_metadata["idle_since"] == promoted_ready_at.isoformat()

    assert (await runtime.run_cycle(now=promoted_ready_at + timedelta(seconds=1))).action == "idle"
    assert client.provider_mutations == []


async def test_same_ready_worker_preserves_warm_idle_epoch_across_runtime_restart(
    runtime_database: tuple[Database, UUID, UUID],
) -> None:
    database, _owner_id, _input_id = runtime_database
    client = FakeRuntimeSalad()
    assert (await _runtime(database, client).run_cycle(now=_NOW)).action == "deployment_observed"

    restarted = _runtime(database, client, warm_idle_seconds=1800)
    assert (await restarted.run_cycle(now=_NOW + timedelta(seconds=1799))).action == "idle"
    assert client.provider_mutations == []
    assert (
        await restarted.run_cycle(now=_NOW + timedelta(seconds=1800))
    ).action == "group_stop_requested"
    assert client.provider_mutations == ["stop_group"]
    assert client.stopped_group_names == ["i2v-dasiwa-5090-v1"]


@pytest.mark.parametrize(
    "invalid_idle_since",
    (
        "not-a-timestamp",
        "0001-01-01T00:00:00+14:00",
        _NOW.replace(tzinfo=None).isoformat(),
        (_NOW + timedelta(days=1)).isoformat(),
    ),
)
async def test_invalid_idle_timestamp_cannot_end_or_extend_a_ready_epoch(
    runtime_database: tuple[Database, UUID, UUID],
    invalid_idle_since: str,
) -> None:
    database, _owner_id, _input_id = runtime_database
    client = FakeRuntimeSalad()
    runtime = _runtime(database, client, warm_idle_seconds=1800)
    assert (await runtime.run_cycle(now=_NOW)).action == "deployment_observed"
    async with database.sessions() as session:
        deployment = await session.scalar(select(I2VWorkerDeployment).with_for_update())
        assert deployment is not None
        metadata = dict(deployment.deployment_metadata)
        metadata["idle_since"] = invalid_idle_since
        deployment.deployment_metadata = metadata
        await session.commit()

    reset_at = _NOW + timedelta(seconds=1)
    assert (await runtime.run_cycle(now=reset_at)).action == "deployment_observed"
    assert client.provider_mutations == []
    async with database.sessions() as session:
        deployment = await session.scalar(select(I2VWorkerDeployment))
        assert deployment is not None
        assert deployment.deployment_metadata["idle_since"] == reset_at.isoformat()


@pytest.mark.parametrize("warm_idle_seconds", (900, 1800))
async def test_queued_and_active_work_clear_a_stale_warm_idle_epoch(
    runtime_database: tuple[Database, UUID, UUID],
    warm_idle_seconds: int,
) -> None:
    database, owner_id, input_id = runtime_database
    client = FakeRuntimeSalad()
    runtime = _runtime(database, client, warm_idle_seconds=warm_idle_seconds)
    assert (await runtime.run_cycle(now=_NOW)).action == "deployment_observed"
    await _queue_job(database, owner_id=owner_id, input_id=input_id)

    queued_at = _NOW + timedelta(seconds=1801)
    assert (await runtime.run_cycle(now=queued_at)).action == "deployment_observed"
    async with database.sessions() as session:
        deployment = await session.scalar(select(I2VWorkerDeployment))
        assert deployment is not None
        assert deployment.deployment_metadata["idle_since"] is None
    assert (await runtime.run_cycle(now=queued_at + timedelta(seconds=1))).action == "job_claimed"
    assert (
        await runtime.run_cycle(now=queued_at + timedelta(seconds=2))
    ).action == "provider_job_submitted"

    async with database.sessions() as session:
        deployment = await session.scalar(select(I2VWorkerDeployment).with_for_update())
        assert deployment is not None
        metadata = dict(deployment.deployment_metadata)
        metadata["idle_since"] = _NOW.isoformat()
        deployment.deployment_metadata = metadata
        await session.commit()
    active_observed = await runtime.run_cycle(now=queued_at + timedelta(seconds=3))
    assert active_observed.action == "deployment_observed"
    assert client.provider_mutations == ["create_job"]
    assert client.stopped_group_names == []
    async with database.sessions() as session:
        deployment = await session.scalar(select(I2VWorkerDeployment))
        assert deployment is not None
        assert deployment.deployment_metadata["idle_since"] is None


async def test_prefetch_three_leaves_rest_editable_in_postgresql_fifo(
    runtime_database: tuple[Database, UUID, UUID],
) -> None:
    database, owner_id, input_id = runtime_database
    job_ids = [
        await _queue_job(database, owner_id=owner_id, input_id=input_id, index=index)
        for index in range(5)
    ]
    client = FakeRuntimeSalad()
    runtime = _runtime(database, client, prefetch=3)
    previous_mutations = 0
    for offset in range(30):
        await runtime.run_cycle(now=_NOW + timedelta(seconds=offset))
        current_mutations = len(client.provider_mutations)
        assert current_mutations - previous_mutations <= 1
        previous_mutations = current_mutations
        async with database.sessions() as session:
            active = int(
                await session.scalar(
                    select(func.count(I2VJob.id)).where(I2VJob.state == I2VJobState.CLAIMED)
                )
                or 0
            )
        if active == 3:
            break
    async with database.sessions() as session:
        jobs = (
            await session.scalars(
                select(I2VJob).where(I2VJob.id.in_(job_ids)).order_by(I2VJob.created_at)
            )
        ).all()
    assert sum(job.state == I2VJobState.CLAIMED for job in jobs) == 3
    queued = [job for job in jobs if job.state == I2VJobState.QUEUED]
    assert len(queued) == 2
    assert [job.queue_position for job in queued] == [1, 2]


def _gpu() -> SaladGpuClass:
    return SaladGpuClass(
        id=_GPU_ID,
        name=I2V_SALAD_GPU_CLASS_NAME,
        prices=("0.5",),
        gpu_count=1,
        is_high_demand=True,
        max_ram=0,
        max_storage=0,
        max_vcpu=0,
        min_ram=0,
        min_storage=0,
        min_vcpu=0,
        raw={},
    )


def _queue() -> SaladQueue:
    return SaladQueue(
        id=_QUEUE_ID,
        name="i2v-dasiwa-v1",
        display_name="I2V",
        description=None,
        current_queue_length=0,
        container_groups=(),
        create_time=_NOW,
        update_time=_NOW,
    )


def _group() -> SaladContainerGroup:
    return SaladContainerGroup(
        id=_GROUP_ID,
        name="i2v-dasiwa-5090-v1",
        display_name="I2V",
        replicas=1,
        pending_change=False,
        version=1,
        current_state=SaladContainerGroupState(
            status="running",
            description="",
            allocating_count=0,
            creating_count=0,
            running_count=1,
            stopping_count=0,
            start_time=_NOW,
            finish_time=None,
        ),
        create_time=_NOW,
        update_time=_NOW,
        raw={
            "container": {
                "image": _IMAGE,
                "resources": {"gpu_classes": [str(_GPU_ID)]},
            },
            "priority": "high",
            "queue_connection": {"queue_name": "i2v-dasiwa-v1"},
            "queue_autoscaler": {
                "min_replicas": 1,
                "max_replicas": 1,
                "desired_queue_length": 1,
                "polling_period": 15,
                "max_upscale_per_minute": 1,
                "max_downscale_per_minute": 1,
            },
        },
    )


def _instance(
    instance_id: str,
    machine_id: str,
    *,
    state: SaladContainerGroupInstanceState = SaladContainerGroupInstanceState.RUNNING,
    ready: bool | None = True,
    started: bool | None = True,
    version: int = 1,
) -> SaladContainerGroupInstance:
    return SaladContainerGroupInstance(
        id=instance_id,
        machine_id=machine_id,
        state=state,
        update_time=_NOW,
        version=version,
        ready=ready,
        started=started,
    )


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
