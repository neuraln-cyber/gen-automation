from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from gen_automation.db.models import AdminUser, I2VAttempt, I2VJob
from gen_automation.db.session import Database
from gen_automation.domain.enums import AdminRole
from gen_automation.domain.i2v import (
    I2VAttemptSnapshot,
    I2VInputRegistration,
    I2VInputSource,
    I2VJobDraft,
    I2VJobSnapshot,
    I2VJobState,
)
from gen_automation.integrations.runpod.errors import RunPodAPIError, RunPodTransportError
from gen_automation.integrations.runpod.models import (
    JSONValue,
    RunPodEndpointHealth,
    RunPodJob,
    RunPodJobStatus,
)
from gen_automation.services.i2v import (
    I2V_RUNTIME_WORKER_ID,
    bind_i2v_runpod_execution,
    create_i2v_job,
    register_i2v_input,
    request_i2v_job_cancellation,
)
from gen_automation.services.i2v_runpod_runtime import (
    I2VRunPodJobInputBuilder,
    I2VRunPodRuntime,
    I2VRunPodRuntimeConfig,
)

NOW = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
IMAGE = "ghcr.io/example/i2v@sha256:" + ("a" * 64)
CLAIM_KEY = "not-a-real-runpod-api-key"


@pytest.fixture
async def runtime_database(tmp_path: Path) -> AsyncIterator[tuple[Database, UUID, UUID]]:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'runpod.db').as_posix()}")
    await database.create_schema()
    async with database.sessions() as session:
        owner = AdminUser(
            username_normalized="runpod-owner",
            display_name="RunPod Owner",
            password_hash="disabled-test-password-hash",  # noqa: S106
            role=AdminRole.OWNER,
            is_active=True,
            failed_login_count=0,
            password_changed_at=NOW,
            lock_version=1,
        )
        session.add(owner)
        await session.commit()
        source = await register_i2v_input(
            session,
            actor_user_id=owner.id,
            registration=I2VInputRegistration(
                source=I2VInputSource.UPLOAD,
                display_name="source",
                storage_backend="s3",
                storage_bucket="private-i2v",
                object_key="i2v/inputs/source.png",
                object_version_id="v1",
                sha256="b" * 64,
                content_type="image/png",
                width=768,
                height=992,
                byte_size=1000,
            ),
            now=NOW,
        )
    try:
        yield database, owner.id, source.input_id
    finally:
        await database.dispose()


class GrantBuilder(I2VRunPodJobInputBuilder):
    async def build(
        self,
        *,
        job: I2VJobSnapshot,
        attempt: I2VAttemptSnapshot,
    ) -> Mapping[str, JSONValue]:
        expires = (NOW + timedelta(days=6)).isoformat()
        return {
            "input_grant": {
                "method": "GET",
                "url": "https://private.example/input",
                "expires_at": expires,
            },
            "output_grant": {
                "method": "PUT",
                "url": "https://private.example/output",
                "headers": {
                    "Content-Type": "video/mp4",
                    "Cache-Control": "private, no-store, max-age=0",
                    "x-amz-server-side-encryption": "AES256",
                },
                "storage_backend": "s3",
                "storage_bucket": "private-i2v",
                "object_key": f"i2v/outputs/{job.job_id}/{attempt.attempt_id}.mp4",
                "expires_at": expires,
            },
            "model_grants": (
                {
                    "role": "diffusion_model_high",
                    "method": "GET",
                    "url": "https://private.example/model",
                    "expires_at": expires,
                    "byte_size": 10,
                    "sha256": "c" * 64,
                },
            ),
        }

    async def verify_output(self, **_kwargs: object) -> None:
        return None


class FakeRunPod:
    endpoint_id = "endpoint123"
    provider_id = endpoint_id

    def __init__(self) -> None:
        self.job: RunPodJob | None = None
        self.last_input: dict[str, JSONValue] | None = None
        self.cancel_calls = 0
        self.reap_calls = 0

    async def health(self) -> RunPodEndpointHealth:
        return RunPodEndpointHealth(0, 0, 0, 0, 0, 0, 0)

    async def reap_idle(self) -> None:
        self.reap_calls += 1

    async def submit(
        self,
        *,
        input_payload: dict[str, JSONValue],
        execution_timeout_ms: int,
        ttl_ms: int,
    ) -> RunPodJob:
        assert execution_timeout_ms == 21_600_000
        assert ttl_ms == 604_800_000
        self.last_input = input_payload
        self.job = RunPodJob(
            id="runpod_job_1",
            status=RunPodJobStatus.IN_QUEUE,
            output=None,
            error=None,
            delay_time_ms=None,
            execution_time_ms=None,
            worker_id=None,
        )
        return self.job

    async def get_job(self, job_id: str) -> RunPodJob:
        assert self.job is not None and job_id == self.job.id
        return self.job

    async def cancel(self, job_id: str) -> RunPodJob:
        assert self.job is not None and job_id == self.job.id
        self.cancel_calls += 1
        self.set_status(RunPodJobStatus.CANCELLED)
        assert self.job is not None
        return self.job

    def set_status(self, status: RunPodJobStatus) -> None:
        assert self.job is not None
        output: JSONValue = None
        if status == RunPodJobStatus.COMPLETED:
            assert self.last_input is not None
            job = self.last_input["job"]
            assert isinstance(job, dict)
            output = {
                "schema": "i2v-result/v2",
                "job_id": job["job_id"],
                "attempt_id": job["attempt_id"],
                "request_sha256": job["request_sha256"],
                "output": {
                    "storage_backend": "s3",
                    "storage_bucket": "private-i2v",
                    "object_key": (f"i2v/outputs/{job['job_id']}/{job['attempt_id']}.mp4"),
                    "object_version_id": "v-output",
                    "sha256": "d" * 64,
                    "content_type": "video/mp4",
                    "width": 768,
                    "height": 992,
                    "frame_count": 81,
                    "fps": 16.0,
                    "duration_ms": 5063,
                    "byte_size": 1000,
                    "metadata": {},
                },
            }
        self.job = RunPodJob(
            id=self.job.id,
            status=status,
            output=output,
            error=None,
            delay_time_ms=10,
            execution_time_ms=20,
            worker_id="worker-1",
        )


class AmbiguousRunPod(FakeRunPod):
    def __init__(self) -> None:
        super().__init__()
        self.submit_calls = 0

    async def submit(
        self,
        *,
        input_payload: dict[str, JSONValue],
        execution_timeout_ms: int,
        ttl_ms: int,
    ) -> RunPodJob:
        assert execution_timeout_ms == 21_600_000
        assert ttl_ms == 604_800_000
        self.submit_calls += 1
        self.last_input = input_payload
        self.job = RunPodJob(
            id="runpod_ambiguous_1",
            status=RunPodJobStatus.IN_QUEUE,
            output=None,
            error=None,
            delay_time_ms=None,
            execution_time_ms=None,
            worker_id=None,
        )
        raise RunPodTransportError("ambiguous test transport")


class MissingRunPod(FakeRunPod):
    async def get_job(self, job_id: str) -> RunPodJob:
        assert self.job is not None and job_id == self.job.id
        raise RunPodAPIError(status_code=404, message="missing")


class UnavailableStatusRunPod(FakeRunPod):
    def __init__(self) -> None:
        super().__init__()
        self.status_unavailable = False

    async def get_job(self, job_id: str) -> RunPodJob:
        assert self.job is not None and job_id == self.job.id
        if self.status_unavailable:
            raise RunPodTransportError("worker status is unavailable")
        return self.job


async def _job(database: Database, owner_id: UUID, input_id: UUID) -> UUID:
    async with database.sessions() as session:
        job = await create_i2v_job(
            session,
            actor_user_id=owner_id,
            draft=I2VJobDraft(
                input_id=input_id,
                positive_prompt="subtle movement",
                settings={"runpod_authorization": "sfw"},
            ),
            now=NOW,
        )
    return job.job_id


@pytest.mark.asyncio
async def test_runpod_runtime_preserves_queue_and_completes_exact_output(
    runtime_database: tuple[Database, UUID, UUID],
) -> None:
    database, owner_id, input_id = runtime_database
    job_id = await _job(database, owner_id, input_id)
    provider = FakeRunPod()
    runtime = I2VRunPodRuntime(
        config=I2VRunPodRuntimeConfig(
            provider_id=provider.provider_id,
            worker_image=IMAGE,
            claim_url="https://staging.example/api/v1/i2v/runpod/claim",
            claim_secret=CLAIM_KEY,
            worker_lease_seconds=86_400,
            execution_timeout_seconds=21_600,
            job_ttl_seconds=604_800,
            reviewed_loras_enabled=True,
        ),
        sessions=database.sessions,
        runpod_client=provider,  # type: ignore[arg-type]
        input_builder=GrantBuilder(),
    )

    assert (await runtime.run_cycle(now=NOW)).action == "deployment_observed"
    assert (await runtime.run_cycle(now=NOW + timedelta(seconds=1))).action == "job_claimed"
    assert (
        await runtime.run_cycle(now=NOW + timedelta(seconds=2))
    ).action == "provider_job_submitted"
    assert provider.last_input is not None
    assert provider.last_input["schema"] == "i2v-runpod-input/v1"
    nested = provider.last_input["job"]
    assert isinstance(nested, dict)
    settings_snapshot = nested["settings_snapshot"]
    assert isinstance(settings_snapshot, dict)
    assert settings_snapshot["runpod_authorization"] == "sfw"
    assert settings_snapshot["loras"] == []

    provider.set_status(RunPodJobStatus.IN_PROGRESS)
    assert (await runtime.run_cycle(now=NOW + timedelta(seconds=3))).action == "inference_started"
    provider.set_status(RunPodJobStatus.COMPLETED)
    assert (await runtime.run_cycle(now=NOW + timedelta(seconds=4))).action == "output_completed"

    async with database.sessions() as session:
        job = await session.get(I2VJob, job_id)
        assert job is not None and job.state == I2VJobState.SUCCEEDED


@pytest.mark.asyncio
async def test_running_job_cost_bound_terminates_provider_and_fails_attempt(
    runtime_database: tuple[Database, UUID, UUID],
) -> None:
    database, owner_id, input_id = runtime_database
    job_id = await _job(database, owner_id, input_id)
    provider = FakeRunPod()
    runtime = I2VRunPodRuntime(
        config=I2VRunPodRuntimeConfig(
            provider_id=provider.provider_id,
            worker_image=IMAGE,
            claim_url="https://staging.example/api/v1/i2v/runpod/claim",
            claim_secret=CLAIM_KEY,
            worker_lease_seconds=86_400,
            execution_timeout_seconds=21_600,
            job_ttl_seconds=604_800,
        ),
        sessions=database.sessions,
        runpod_client=provider,  # type: ignore[arg-type]
        input_builder=GrantBuilder(),
    )

    assert (await runtime.run_cycle(now=NOW)).action == "deployment_observed"
    assert (await runtime.run_cycle(now=NOW + timedelta(seconds=1))).action == "job_claimed"
    assert (
        await runtime.run_cycle(now=NOW + timedelta(seconds=2))
    ).action == "provider_job_submitted"
    provider.set_status(RunPodJobStatus.IN_PROGRESS)
    assert (await runtime.run_cycle(now=NOW + timedelta(seconds=3))).action == ("inference_started")
    assert (await runtime.run_cycle(now=NOW + timedelta(seconds=21_604))).action == (
        "provider_execution_timeout_failed"
    )
    assert provider.cancel_calls == 1
    async with database.sessions() as session:
        job = await session.get(I2VJob, job_id)
        assert job is not None and job.state == I2VJobState.FAILED

    await runtime.run_cycle(now=NOW + timedelta(seconds=21_605))
    assert provider.reap_calls >= 2


@pytest.mark.asyncio
async def test_cost_bound_terminates_provider_even_when_worker_status_is_unavailable(
    runtime_database: tuple[Database, UUID, UUID],
) -> None:
    database, owner_id, input_id = runtime_database
    job_id = await _job(database, owner_id, input_id)
    provider = UnavailableStatusRunPod()
    runtime = I2VRunPodRuntime(
        config=I2VRunPodRuntimeConfig(
            provider_id=provider.provider_id,
            worker_image=IMAGE,
            claim_url="https://staging.example/api/v1/i2v/runpod/claim",
            claim_secret=CLAIM_KEY,
            worker_lease_seconds=86_400,
            execution_timeout_seconds=21_600,
            job_ttl_seconds=604_800,
        ),
        sessions=database.sessions,
        runpod_client=provider,  # type: ignore[arg-type]
        input_builder=GrantBuilder(),
    )

    assert (await runtime.run_cycle(now=NOW)).action == "deployment_observed"
    assert (await runtime.run_cycle(now=NOW + timedelta(seconds=1))).action == "job_claimed"
    assert (
        await runtime.run_cycle(now=NOW + timedelta(seconds=2))
    ).action == "provider_job_submitted"
    provider.set_status(RunPodJobStatus.IN_PROGRESS)
    assert (await runtime.run_cycle(now=NOW + timedelta(seconds=3))).action == ("inference_started")
    provider.status_unavailable = True

    assert (await runtime.run_cycle(now=NOW + timedelta(seconds=21_604))).action == (
        "provider_execution_timeout_failed"
    )
    assert provider.cancel_calls == 1
    async with database.sessions() as session:
        job = await session.get(I2VJob, job_id)
        assert job is not None and job.state == I2VJobState.FAILED


@pytest.mark.asyncio
async def test_ambiguous_submit_is_never_retried_and_worker_claim_adopts_it(
    runtime_database: tuple[Database, UUID, UUID],
) -> None:
    database, owner_id, input_id = runtime_database
    await _job(database, owner_id, input_id)
    provider = AmbiguousRunPod()
    runtime = I2VRunPodRuntime(
        config=I2VRunPodRuntimeConfig(
            provider_id=provider.provider_id,
            worker_image=IMAGE,
            claim_url="https://staging.example/api/v1/i2v/runpod/claim",
            claim_secret=CLAIM_KEY,
            worker_lease_seconds=86_400,
            execution_timeout_seconds=21_600,
            job_ttl_seconds=604_800,
            reviewed_loras_enabled=True,
        ),
        sessions=database.sessions,
        runpod_client=provider,  # type: ignore[arg-type]
        input_builder=GrantBuilder(),
    )

    assert (await runtime.run_cycle(now=NOW)).action == "deployment_observed"
    assert (await runtime.run_cycle(now=NOW + timedelta(seconds=1))).action == "job_claimed"
    assert (await runtime.run_cycle(now=NOW + timedelta(seconds=2))).action == "submission_unknown"
    assert provider.submit_calls == 1
    assert (await runtime.run_cycle(now=NOW + timedelta(seconds=3))).action == (
        "provider_unchanged"
    )
    assert provider.submit_calls == 1
    assert provider.last_input is not None and provider.job is not None
    nested = provider.last_input["job"]
    assert isinstance(nested, dict)
    async with database.sessions() as session:
        await bind_i2v_runpod_execution(
            session,
            job_id=UUID(str(nested["job_id"])),
            attempt_id=UUID(str(nested["attempt_id"])),
            request_sha256=str(nested["request_sha256"]),
            submission_key=str(provider.last_input["submission_key"]),
            provider_job_id=provider.job.id,
            worker_id=I2V_RUNTIME_WORKER_ID,
            lease_duration=timedelta(seconds=86_400),
            now=NOW + timedelta(seconds=4),
        )
    assert (await runtime.run_cycle(now=NOW + timedelta(seconds=5))).action == (
        "provider_pending_observed"
    )
    assert provider.submit_calls == 1
    async with database.sessions() as session:
        attempt = await session.get(I2VAttempt, UUID(str(nested["attempt_id"])))
        assert attempt is not None
        assert attempt.provider_job_id == "runpod_ambiguous_1"


@pytest.mark.asyncio
async def test_ambiguous_submit_fails_bounded_without_a_worker_claim(
    runtime_database: tuple[Database, UUID, UUID],
) -> None:
    database, owner_id, input_id = runtime_database
    job_id = await _job(database, owner_id, input_id)
    provider = AmbiguousRunPod()
    runtime = I2VRunPodRuntime(
        config=I2VRunPodRuntimeConfig(
            provider_id=provider.provider_id,
            worker_image=IMAGE,
            claim_url="https://staging.example/api/v1/i2v/runpod/claim",
            claim_secret=CLAIM_KEY,
            worker_lease_seconds=86_400,
            execution_timeout_seconds=21_600,
            job_ttl_seconds=604_800,
            submission_claim_timeout_seconds=120,
        ),
        sessions=database.sessions,
        runpod_client=provider,  # type: ignore[arg-type]
        input_builder=GrantBuilder(),
    )

    assert (await runtime.run_cycle(now=NOW)).action == "deployment_observed"
    assert (await runtime.run_cycle(now=NOW + timedelta(seconds=1))).action == "job_claimed"
    assert (await runtime.run_cycle(now=NOW + timedelta(seconds=2))).action == "submission_unknown"
    assert (await runtime.run_cycle(now=NOW + timedelta(seconds=122))).action == (
        "submission_unresolved_failed"
    )
    assert provider.submit_calls == 1
    async with database.sessions() as session:
        job = await session.get(I2VJob, job_id)
        assert job is not None and job.state == I2VJobState.FAILED


@pytest.mark.asyncio
async def test_queue_allocation_timeout_cancels_once_then_fails_terminally(
    runtime_database: tuple[Database, UUID, UUID],
) -> None:
    database, owner_id, input_id = runtime_database
    job_id = await _job(database, owner_id, input_id)
    provider = FakeRunPod()
    runtime = I2VRunPodRuntime(
        config=I2VRunPodRuntimeConfig(
            provider_id=provider.provider_id,
            worker_image=IMAGE,
            claim_url="https://staging.example/api/v1/i2v/runpod/claim",
            claim_secret=CLAIM_KEY,
            worker_lease_seconds=86_400,
            execution_timeout_seconds=21_600,
            job_ttl_seconds=604_800,
            queue_timeout_seconds=120,
            terminal_grace_seconds=30,
        ),
        sessions=database.sessions,
        runpod_client=provider,  # type: ignore[arg-type]
        input_builder=GrantBuilder(),
    )

    assert (await runtime.run_cycle(now=NOW)).action == "deployment_observed"
    assert (await runtime.run_cycle(now=NOW + timedelta(seconds=1))).action == "job_claimed"
    assert (
        await runtime.run_cycle(now=NOW + timedelta(seconds=2))
    ).action == "provider_job_submitted"
    assert (await runtime.run_cycle(now=NOW + timedelta(seconds=122))).action == (
        "provider_queue_timeout_cancel_requested"
    )
    assert provider.cancel_calls == 1
    assert (await runtime.run_cycle(now=NOW + timedelta(seconds=123))).action == (
        "provider_terminal_failure"
    )
    async with database.sessions() as session:
        job = await session.get(I2VJob, job_id)
        assert job is not None and job.state == I2VJobState.FAILED


@pytest.mark.asyncio
async def test_unbound_cancel_never_waits_for_an_ambiguous_provider_job(
    runtime_database: tuple[Database, UUID, UUID],
) -> None:
    database, owner_id, input_id = runtime_database
    job_id = await _job(database, owner_id, input_id)
    provider = AmbiguousRunPod()
    runtime = I2VRunPodRuntime(
        config=I2VRunPodRuntimeConfig(
            provider_id=provider.provider_id,
            worker_image=IMAGE,
            claim_url="https://staging.example/api/v1/i2v/runpod/claim",
            claim_secret=CLAIM_KEY,
            worker_lease_seconds=86_400,
            execution_timeout_seconds=21_600,
            job_ttl_seconds=604_800,
        ),
        sessions=database.sessions,
        runpod_client=provider,  # type: ignore[arg-type]
        input_builder=GrantBuilder(),
    )

    assert (await runtime.run_cycle(now=NOW)).action == "deployment_observed"
    assert (await runtime.run_cycle(now=NOW + timedelta(seconds=1))).action == "job_claimed"
    assert (await runtime.run_cycle(now=NOW + timedelta(seconds=2))).action == "submission_unknown"
    async with database.sessions() as session:
        await request_i2v_job_cancellation(
            session,
            actor_user_id=owner_id,
            job_id=job_id,
            now=NOW + timedelta(seconds=3),
        )
    assert (await runtime.run_cycle(now=NOW + timedelta(seconds=4))).action == (
        "unbound_cancellation_acknowledged"
    )
    assert provider.cancel_calls == 0
    async with database.sessions() as session:
        job = await session.get(I2VJob, job_id)
        assert job is not None and job.state == I2VJobState.CANCELLED


@pytest.mark.asyncio
async def test_missing_bound_provider_job_fails_after_a_short_grace(
    runtime_database: tuple[Database, UUID, UUID],
) -> None:
    database, owner_id, input_id = runtime_database
    job_id = await _job(database, owner_id, input_id)
    provider = MissingRunPod()
    runtime = I2VRunPodRuntime(
        config=I2VRunPodRuntimeConfig(
            provider_id=provider.provider_id,
            worker_image=IMAGE,
            claim_url="https://staging.example/api/v1/i2v/runpod/claim",
            claim_secret=CLAIM_KEY,
            worker_lease_seconds=86_400,
            execution_timeout_seconds=21_600,
            job_ttl_seconds=604_800,
            terminal_grace_seconds=30,
        ),
        sessions=database.sessions,
        runpod_client=provider,  # type: ignore[arg-type]
        input_builder=GrantBuilder(),
    )

    assert (await runtime.run_cycle(now=NOW)).action == "deployment_observed"
    assert (await runtime.run_cycle(now=NOW + timedelta(seconds=1))).action == "job_claimed"
    assert (
        await runtime.run_cycle(now=NOW + timedelta(seconds=2))
    ).action == "provider_job_submitted"
    assert (await runtime.run_cycle(now=NOW + timedelta(seconds=3))).action == (
        "provider_absence_observed"
    )
    assert (await runtime.run_cycle(now=NOW + timedelta(seconds=33))).action == (
        "provider_absence_failed"
    )
    async with database.sessions() as session:
        job = await session.get(I2VJob, job_id)
        assert job is not None and job.state == I2VJobState.FAILED
