from __future__ import annotations

import asyncio
import hashlib
import inspect
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy import func, select

from gen_automation.db.models import (
    AdminUser,
    Asset,
    AssetLineage,
    AuditEvent,
    GenerationAttempt,
    GenerationJob,
    Project,
    ProviderBudgetGuard,
    Release,
    ReleaseVersion,
    SaladDeployment,
    VideoGenerationAttempt,
    VideoGenerationJob,
    VideoGenerationOutput,
)
from gen_automation.db.session import Database
from gen_automation.domain.enums import (
    AdminRole,
    AssetKind,
    AssetState,
    BudgetState,
    DesiredDeploymentState,
    GenerationAttemptState,
    GenerationState,
    SaladDeploymentPurpose,
    SaladDeploymentState,
)
from gen_automation.domain.signing import encode_base64url
from gen_automation.domain.video import (
    VideoContentRating,
    VideoGenerationAttemptState,
    VideoGenerationState,
)
from gen_automation.integrations.salad import (
    JSONValue,
    SaladAPIError,
    SaladJobStatus,
    SaladQueueJob,
    SaladQueueJobPage,
    SaladTransportError,
)
from gen_automation.services.budgets import reserve_attempt_budget
from gen_automation.services.video_runtime import (
    VideoRuntime,
    VideoRuntimeConfig,
    request_video_cancellation,
)
from gen_automation.storage.memory import MemoryObjectStore
from gen_automation.video_worker.models import AnimateEnvelope
from gen_automation.video_worker.profiles import (
    PINNED_VIDEO_PROFILE,
    PINNED_VIDEO_PROFILE_SHA256,
)

NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
WORKER_IMAGE = "registry.example.test/video@sha256:" + ("e" * 64)


def test_claim_lock_order_is_budget_then_deployment() -> None:
    source = inspect.getsource(VideoRuntime.claim_once)
    assert source.index("await self._lock_budget") < source.index("select(SaladDeployment)")


class FakeSalad:
    def __init__(self) -> None:
        self.jobs: dict[UUID, SaladQueueJob] = {}
        self.create_calls = 0
        self.cancel_calls = 0
        self.stop_names: list[str] = []
        self.raise_unknown_after_create = False
        self.create_queue_names: list[str] = []
        self.list_queue_names: list[str] = []
        self.get_queue_names: list[str] = []
        self.cancel_error: Exception | None = None
        self.get_error: Exception | None = None
        self.block_get = False
        self.get_started = asyncio.Event()
        self.release_get = asyncio.Event()
        self.fail_if_list_called = False
        self.tamper_create_response = False
        self.saturated_history_pages = 0

    async def create_job(
        self,
        queue_name: str,
        *,
        input: JSONValue,
        metadata: Mapping[str, JSONValue] | None = None,
        webhook: str | None = None,
    ) -> SaladQueueJob:
        del webhook
        self.create_calls += 1
        self.create_queue_names.append(queue_name)
        envelope = AnimateEnvelope.model_validate(input, strict=True)
        resolved_metadata = cast(dict[str, JSONValue], dict(metadata or {}))
        remote = _remote_job(
            remote_id=UUID(int=10_000 + self.create_calls),
            status=SaladJobStatus.PENDING,
            input=(
                {
                    **envelope.model_dump(mode="json"),
                    "payload": {
                        **envelope.payload.model_dump(mode="json"),
                        "prompt": "tampered provider input",
                    },
                }
                if self.tamper_create_response
                else input
            ),
            metadata=resolved_metadata,
        )
        self.jobs[remote.id] = remote
        if self.raise_unknown_after_create:
            self.raise_unknown_after_create = False
            raise SaladTransportError("connection ended after request transmission")
        assert envelope.payload.attempt_id == str(resolved_metadata["video_attempt_id"])
        assert envelope.payload.negative_prompt == "camera cut"
        return remote

    async def list_jobs(
        self,
        queue_name: str,
        *,
        page: int = 1,
        page_size: int = 50,
    ) -> SaladQueueJobPage:
        if self.fail_if_list_called:
            raise AssertionError("pristine attempt must not scan retained queue history")
        self.list_queue_names.append(queue_name)
        if page <= self.saturated_history_pages:
            return SaladQueueJobPage(
                items=tuple(
                    _remote_job(
                        remote_id=UUID(int=(page * 1000) + index + 100),
                        status=SaladJobStatus.PENDING,
                        input={},
                        metadata={"kind": "unrelated-history"},
                    )
                    for index in range(100)
                )
            )
        items = tuple(self.jobs.values())
        start = (page - 1) * page_size
        return SaladQueueJobPage(items=items[start : start + page_size])

    async def get_job(self, queue_name: str, job_id: UUID | str) -> SaladQueueJob:
        self.get_queue_names.append(queue_name)
        if self.block_get:
            self.get_started.set()
            await self.release_get.wait()
        if self.get_error is not None:
            raise self.get_error
        return self.jobs[UUID(str(job_id))]

    async def cancel_job(self, queue_name: str, job_id: UUID | str) -> None:
        del queue_name, job_id
        self.cancel_calls += 1
        if self.cancel_error is not None:
            raise self.cancel_error

    async def stop_container_group(self, container_group_name: str) -> None:
        self.stop_names.append(container_group_name)


@dataclass(frozen=True)
class RuntimeContext:
    database: Database
    runtime: VideoRuntime
    store: MemoryObjectStore
    salad: FakeSalad
    user_id: UUID
    source_id: UUID
    release_id: UUID
    deployment_id: UUID


@pytest.fixture
async def runtime_context(tmp_path: Path) -> AsyncIterator[RuntimeContext]:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'video-runtime.db').as_posix()}")
    await database.create_schema()
    store = MemoryObjectStore(bucket="video-private")
    source_bytes = b"source-image"
    source_key = "source/source.webp"
    store.put_for_test(
        source_key,
        source_bytes,
        content_type="image/webp",
        metadata={"sha256": hashlib.sha256(source_bytes).hexdigest()},
    )
    source_object = store.objects[source_key]

    async with database.sessions() as session:
        project = Project(slug="video-runtime", name="Video Runtime")
        user = AdminUser(
            username_normalized="video-runtime@example.test",
            display_name="Video Runtime",
            password_hash="disabled-test-password",  # noqa: S106
            role=AdminRole.OWNER,
            is_active=True,
            failed_login_count=0,
            password_changed_at=NOW,
            credential_version=1,
            lock_version=1,
        )
        session.add_all((project, user))
        await session.flush()
        release = Release(
            project_id=project.id,
            slug="animation",
            title="Animation",
            desired_accepted_count=1,
        )
        session.add(release)
        await session.flush()
        source = Asset(
            release_id=release.id,
            kind=AssetKind.DERIVATIVE,
            state=AssetState.AVAILABLE,
            storage_backend=store.backend,
            storage_bucket=store.bucket,
            object_key=source_key,
            object_version_id=source_object.version_id,
            sha256=hashlib.sha256(source_bytes).hexdigest(),
            content_type="image/webp",
            image_format="WEBP",
            width=768,
            height=1024,
            byte_size=len(source_bytes),
            asset_metadata={},
            available_at=NOW,
        )
        deployment = SaladDeployment(
            version_no=1,
            config_sha256="d" * 64,
            provider_configuration={},
            worker_image_digest=WORKER_IMAGE,
            organization_name="test-org",
            project_name="test-project",
            queue_name="animation-video-v1-a1b2c3d4",
            provider_queue_id="queue-1",
            container_group_name="animation-video-worker",
            provider_container_group_id="group-1",
            purpose=SaladDeploymentPurpose.VIDEO,
            state=SaladDeploymentState.ACTIVE,
            desired_state=DesiredDeploymentState.ACTIVE,
            is_current=True,
            min_replicas=0,
            max_replicas=1,
            desired_queue_length=1,
            max_hourly_cost_microusd=350_000,
            activated_at=NOW,
        )
        guard = ProviderBudgetGuard(
            provider="salad",
            currency="USD",
            daily_limit_microusd=25_000_000,
            monthly_limit_microusd=250_000_000,
            state=BudgetState.OPEN,
            lock_version=1,
            updated_at=NOW,
        )
        session.add_all((source, deployment, guard))
        await session.commit()
        user_id = user.id
        source_id = source.id
        release_id = release.id
        deployment_id = deployment.id

    salad = FakeSalad()
    config = VideoRuntimeConfig(
        enabled=True,
        queue_name="animation-video",
        worker_image_digest=WORKER_IMAGE,
        signing_key_id="video-test-key",
        signing_private_key=encode_base64url(bytes(range(32))),
        signature_ttl_seconds=720,
        grant_ttl_seconds=1800,
        max_output_bytes=1024 * 1024,
        max_queued_jobs=1,
        max_hourly_cost_microusd=350_000,
        attempt_watchdog_seconds=600,
        retry_delay_seconds=60,
        reconciliation_interval_seconds=30,
        unresolved_submission_seconds=300,
    )
    runtime = VideoRuntime(
        config=config,
        sessions=database.sessions,
        salad=cast(Any, salad),
        store=store,
        worker_id="controller-test:video",
    )
    context = RuntimeContext(
        database=database,
        runtime=runtime,
        store=store,
        salad=salad,
        user_id=user_id,
        source_id=source_id,
        release_id=release_id,
        deployment_id=deployment_id,
    )
    try:
        yield context
    finally:
        await database.dispose()


async def _add_job(
    context: RuntimeContext,
    *,
    max_attempts: int = 3,
    request_suffix: str = "1",
) -> UUID:
    async with context.database.sessions() as session:
        source = await session.get(Asset, context.source_id)
        assert source is not None
        job = VideoGenerationJob(
            source_asset_id=source.id,
            created_by_user_id=context.user_id,
            source_storage_backend=source.storage_backend,
            source_storage_bucket=source.storage_bucket,
            source_object_key=cast(str, source.object_key),
            source_object_version_id=source.object_version_id,
            source_sha256=cast(str, source.sha256),
            source_content_type=cast(str, source.content_type),
            source_image_format=cast(str, source.image_format),
            source_width=cast(int, source.width),
            source_height=cast(int, source.height),
            source_byte_size=cast(int, source.byte_size),
            prompt="subtle breathing and hair movement",
            negative_prompt="camera cut",
            profile_key=PINNED_VIDEO_PROFILE.profile_id,
            profile_version=PINNED_VIDEO_PROFILE.adapter_revision,
            profile_sha256=PINNED_VIDEO_PROFILE_SHA256,
            seed=42,
            frame_count=73,
            fps=24,
            width=480,
            height=832,
            loop_mode="ping_pong",
            content_rating=VideoContentRating.SFW,
            source_rights_confirmed=True,
            lawful_use_confirmed=True,
            request_sha256=(request_suffix * 64)[:64],
            state=VideoGenerationState.QUEUED,
            max_attempts=max_attempts,
            estimated_cost_microusd=20_000,
            cost_metadata={},
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(job)
        await session.commit()
        return cast(UUID, job.id)


async def _add_image_attempt(context: RuntimeContext, *, suffix: str) -> UUID:
    async with context.database.sessions() as session:
        version = ReleaseVersion(
            release_id=context.release_id,
            version_no=int(suffix),
            specification={"schema_version": 1},
            specification_sha256=(suffix * 64)[:64],
            created_by="video-budget-test",
            created_at=NOW,
        )
        session.add(version)
        await session.flush()
        job = GenerationJob(
            release_version_id=version.id,
            logical_key=((str(int(suffix) + 5)) * 64)[:64],
            parameters={"seed": int(suffix)},
            parameters_sha256=((str(int(suffix) + 6)) * 64)[:64],
            provider="salad",
            state=GenerationState.QUEUED,
            expected_output_count=1,
        )
        session.add(job)
        await session.flush()
        attempt = GenerationAttempt(
            job_id=job.id,
            salad_deployment_id=context.deployment_id,
            attempt_no=1,
            provider="salad",
            submission_key=((str(int(suffix) + 7)) * 64)[:64],
            request_sha256=((str(int(suffix) + 8)) * 64)[:64],
            state=GenerationAttemptState.CREATED,
            worker_image_digest=WORKER_IMAGE,
            request_metadata={},
            created_at=NOW,
        )
        session.add(attempt)
        await session.commit()
        return cast(UUID, attempt.id)


@pytest.mark.asyncio
async def test_video_runtime_success_is_transactional(runtime_context: RuntimeContext) -> None:
    job_id = await _add_job(runtime_context)
    assert await runtime_context.runtime.claim_once(now=NOW) is True

    async with runtime_context.database.sessions() as session:
        attempt = await session.scalar(
            select(VideoGenerationAttempt).where(
                VideoGenerationAttempt.video_generation_job_id == job_id
            )
        )
        assert attempt is not None
        output_asset_id = UUID(str(attempt.request_metadata["output_asset_id"]))
        upload_attempt_id = UUID(str(attempt.request_metadata["upload_attempt_id"]))
        output_key = str(attempt.request_metadata["output_object_key"])
        placeholder = await session.get(Asset, output_asset_id)
        assert placeholder is not None
        assert placeholder.state == AssetState.UPLOADING

    assert await runtime_context.runtime.submit_once(now=NOW) is True
    assert runtime_context.salad.create_calls == 1
    assert runtime_context.salad.list_queue_names == []
    assert runtime_context.salad.create_queue_names == ["animation-video-v1-a1b2c3d4"]
    remote = next(iter(runtime_context.salad.jobs.values()))
    body = b"verified-mp4"
    digest = hashlib.sha256(body).hexdigest()
    runtime_context.store.put_for_test(
        output_key,
        body,
        content_type="video/mp4",
        metadata={
            "schema": "animation-video/v1",
            "video-job-id": str(job_id),
            "video-attempt-id": str(attempt.id),
            "output-asset-id": str(output_asset_id),
            "upload-attempt-id": str(upload_attempt_id),
        },
    )
    runtime_context.salad.jobs[remote.id] = _remote_job(
        remote_id=remote.id,
        status=SaladJobStatus.SUCCEEDED,
        input=remote.input,
        metadata=remote.metadata,
        output=_success_output(
            job_id=job_id,
            attempt_id=attempt.id,
            source_id=runtime_context.source_id,
            output_asset_id=output_asset_id,
            upload_attempt_id=upload_attempt_id,
            digest=digest,
            size=len(body),
        ),
    )

    assert await runtime_context.runtime.observe_once(now=NOW + timedelta(seconds=31)) is True
    assert runtime_context.salad.get_queue_names == ["animation-video-v1-a1b2c3d4"]
    async with runtime_context.database.sessions() as session:
        job = await session.get(VideoGenerationJob, job_id)
        output = await session.scalar(
            select(VideoGenerationOutput).where(
                VideoGenerationOutput.video_generation_job_id == job_id
            )
        )
        asset = await session.get(Asset, output_asset_id)
        lineage_count = await session.scalar(select(func.count(AssetLineage.id)))
        assert job is not None and job.state == VideoGenerationState.SUCCEEDED
        assert output is not None and output.sha256 == digest
        assert asset is not None and asset.state == AssetState.AVAILABLE
        assert asset.object_key == output_key
        assert lineage_count == 1


@pytest.mark.asyncio
async def test_unknown_submit_reconciles_without_duplicate(runtime_context: RuntimeContext) -> None:
    job_id = await _add_job(runtime_context)
    await runtime_context.runtime.claim_once(now=NOW)
    runtime_context.salad.raise_unknown_after_create = True

    assert await runtime_context.runtime.submit_once(now=NOW) is True
    async with runtime_context.database.sessions() as session:
        job = await session.get(VideoGenerationJob, job_id)
        assert job is not None and job.state == VideoGenerationState.UNKNOWN

    assert await runtime_context.runtime.submit_once(now=NOW + timedelta(seconds=31)) is True
    assert runtime_context.salad.create_calls == 1
    assert runtime_context.salad.list_queue_names == ["animation-video-v1-a1b2c3d4"]
    async with runtime_context.database.sessions() as session:
        attempt = await session.scalar(
            select(VideoGenerationAttempt).where(
                VideoGenerationAttempt.video_generation_job_id == job_id
            )
        )
        assert attempt is not None
        assert attempt.provider_external_id == str(next(iter(runtime_context.salad.jobs)))


@pytest.mark.asyncio
async def test_pristine_attempt_posts_without_scanning_saturated_history(
    runtime_context: RuntimeContext,
) -> None:
    await _add_job(runtime_context)
    await runtime_context.runtime.claim_once(now=NOW)
    runtime_context.salad.fail_if_list_called = True

    assert await runtime_context.runtime.submit_once(now=NOW) is True
    assert runtime_context.salad.create_calls == 1


@pytest.mark.asyncio
async def test_known_identity_mismatch_is_cancelled_before_reservation_release(
    runtime_context: RuntimeContext,
) -> None:
    job_id = await _add_job(runtime_context, max_attempts=1)
    await runtime_context.runtime.claim_once(now=NOW)
    runtime_context.salad.tamper_create_response = True
    assert await runtime_context.runtime.submit_once(now=NOW) is True
    async with runtime_context.database.sessions() as session:
        job = await session.get(VideoGenerationJob, job_id)
        attempt = await session.scalar(
            select(VideoGenerationAttempt).where(
                VideoGenerationAttempt.video_generation_job_id == job_id
            )
        )
        assert job is not None and job.state == VideoGenerationState.CANCEL_REQUESTED
        assert job.reserved_cost_microusd > 0
        assert attempt is not None and attempt.provider_external_id is not None
        assert attempt.state == VideoGenerationAttemptState.CANCEL_REQUESTED

    assert await runtime_context.runtime.observe_once(now=NOW + timedelta(seconds=1)) is True
    assert runtime_context.salad.cancel_calls == 1
    async with runtime_context.database.sessions() as session:
        job = await session.get(VideoGenerationJob, job_id)
        assert job is not None and job.state == VideoGenerationState.CANCEL_REQUESTED
        assert job.reserved_cost_microusd > 0

    remote = next(iter(runtime_context.salad.jobs.values()))
    runtime_context.salad.jobs[remote.id] = _remote_job(
        remote_id=remote.id,
        status=SaladJobStatus.CANCELLED,
        input=remote.input,
        metadata=remote.metadata,
    )
    assert await runtime_context.runtime.observe_once(now=NOW + timedelta(seconds=32)) is True
    async with runtime_context.database.sessions() as session:
        job = await session.get(VideoGenerationJob, job_id)
        output_count = await session.scalar(select(func.count(VideoGenerationOutput.id)))
        assert job is not None and job.state == VideoGenerationState.FAILED
        assert job.reserved_cost_microusd == 0
        assert output_count == 0


@pytest.mark.asyncio
async def test_saturated_unknown_history_advances_cursor_and_resolves(
    runtime_context: RuntimeContext,
) -> None:
    job_id = await _add_job(runtime_context, max_attempts=1)
    await runtime_context.runtime.claim_once(now=NOW)
    runtime_context.salad.raise_unknown_after_create = True
    await runtime_context.runtime.submit_once(now=NOW)
    runtime_context.salad.jobs.clear()
    runtime_context.salad.saturated_history_pages = 11

    assert await runtime_context.runtime.submit_once(now=NOW + timedelta(seconds=31)) is True
    async with runtime_context.database.sessions() as session:
        attempt = await session.scalar(
            select(VideoGenerationAttempt).where(
                VideoGenerationAttempt.video_generation_job_id == job_id
            )
        )
        assert attempt is not None
        assert attempt.request_metadata["reconciliation_next_page"] == 11

    assert await runtime_context.runtime.submit_once(now=NOW + timedelta(seconds=62)) is False
    assert await runtime_context.runtime.submit_once(now=NOW + timedelta(seconds=331)) is True
    async with runtime_context.database.sessions() as session:
        job = await session.get(VideoGenerationJob, job_id)
        assert job is not None and job.state == VideoGenerationState.FAILED
        assert job.last_error_code == "video_submission_outcome_unresolved"
        assert job.reserved_cost_microusd == 0


@pytest.mark.asyncio
async def test_duplicate_provider_matches_are_all_cancelled_before_release(
    runtime_context: RuntimeContext,
) -> None:
    job_id = await _add_job(runtime_context, max_attempts=1)
    await runtime_context.runtime.claim_once(now=NOW)
    runtime_context.salad.raise_unknown_after_create = True
    await runtime_context.runtime.submit_once(now=NOW)
    first = next(iter(runtime_context.salad.jobs.values()))
    duplicate = _remote_job(
        remote_id=UUID(int=20_001),
        status=SaladJobStatus.PENDING,
        input=first.input,
        metadata=first.metadata,
    )
    runtime_context.salad.jobs[duplicate.id] = duplicate

    assert await runtime_context.runtime.submit_once(now=NOW + timedelta(seconds=31)) is True
    async with runtime_context.database.sessions() as session:
        job = await session.get(VideoGenerationJob, job_id)
        attempt = await session.scalar(
            select(VideoGenerationAttempt).where(
                VideoGenerationAttempt.video_generation_job_id == job_id
            )
        )
        assert job is not None and job.state == VideoGenerationState.CANCEL_REQUESTED
        assert job.reserved_cost_microusd > 0
        assert attempt is not None
        assert set(attempt.request_metadata["quarantine_provider_ids"]) == {
            str(first.id),
            str(duplicate.id),
        }

    assert await runtime_context.runtime.observe_once(now=NOW + timedelta(seconds=62)) is True
    assert runtime_context.salad.cancel_calls == 2
    async with runtime_context.database.sessions() as session:
        job = await session.get(VideoGenerationJob, job_id)
        assert job is not None and job.reserved_cost_microusd > 0

    for remote in (first, duplicate):
        runtime_context.salad.jobs[remote.id] = _remote_job(
            remote_id=remote.id,
            status=SaladJobStatus.CANCELLED,
            input=remote.input,
            metadata=remote.metadata,
        )
    assert await runtime_context.runtime.observe_once(now=NOW + timedelta(seconds=93)) is True
    async with runtime_context.database.sessions() as session:
        job = await session.get(VideoGenerationJob, job_id)
        assert job is not None and job.state == VideoGenerationState.FAILED
        assert job.last_error_code == "video_duplicate_provider_jobs"
        assert job.reserved_cost_microusd == 0


@pytest.mark.asyncio
async def test_ambiguous_old_attempt_reconciles_on_frozen_queue_after_rollout(
    runtime_context: RuntimeContext,
) -> None:
    job_id = await _add_job(runtime_context, max_attempts=1)
    await runtime_context.runtime.claim_once(now=NOW)
    runtime_context.salad.raise_unknown_after_create = True
    await runtime_context.runtime.submit_once(now=NOW)
    old_remote = next(iter(runtime_context.salad.jobs.values()))
    new_digest = "registry.example/video-worker@sha256:" + ("b" * 64)
    async with runtime_context.database.sessions() as session:
        old = await session.get(SaladDeployment, runtime_context.deployment_id)
        assert old is not None
        old.is_current = False
        session.add(
            SaladDeployment(
                version_no=2,
                config_sha256="c" * 64,
                worker_image_digest=new_digest,
                organization_name="test-org",
                project_name="test-project",
                queue_name="animation-video-v2-c3d4e5f6",
                provider_queue_id="queue-2",
                container_group_name="animation-video-worker-v2-c3d4e5f6",
                provider_container_group_id="group-2",
                purpose=SaladDeploymentPurpose.VIDEO,
                state=SaladDeploymentState.ACTIVE,
                desired_state=DesiredDeploymentState.ACTIVE,
                is_current=True,
                min_replicas=0,
                max_replicas=1,
                desired_queue_length=1,
                max_hourly_cost_microusd=350_000,
                activated_at=NOW,
            )
        )
        await session.commit()
    rolled_runtime = VideoRuntime(
        config=replace(runtime_context.runtime.config, worker_image_digest=new_digest),
        sessions=runtime_context.database.sessions,
        salad=cast(Any, runtime_context.salad),
        store=runtime_context.store,
        worker_id="controller-test:video-rollout",
    )

    assert await rolled_runtime.submit_once(now=NOW + timedelta(seconds=31)) is True
    assert runtime_context.salad.list_queue_names == ["animation-video-v1-a1b2c3d4"]
    async with runtime_context.database.sessions() as session:
        job = await session.get(VideoGenerationJob, job_id)
        attempt = await session.scalar(
            select(VideoGenerationAttempt).where(
                VideoGenerationAttempt.video_generation_job_id == job_id
            )
        )
        assert job is not None and job.state == VideoGenerationState.CANCEL_REQUESTED
        assert job.reserved_cost_microusd > 0
        assert attempt is not None
        assert attempt.salad_deployment_id == runtime_context.deployment_id
        assert attempt.request_metadata["quarantine_error_code"] == ("video_deployment_superseded")

    assert await rolled_runtime.observe_once(now=NOW + timedelta(seconds=62)) is True
    assert runtime_context.salad.cancel_calls == 1
    assert runtime_context.salad.get_queue_names[-1] == "animation-video-v1-a1b2c3d4"
    runtime_context.salad.jobs[old_remote.id] = _remote_job(
        remote_id=old_remote.id,
        status=SaladJobStatus.CANCELLED,
        input=old_remote.input,
        metadata=old_remote.metadata,
    )
    assert await rolled_runtime.observe_once(now=NOW + timedelta(seconds=93)) is True
    async with runtime_context.database.sessions() as session:
        job = await session.get(VideoGenerationJob, job_id)
        assert job is not None and job.state == VideoGenerationState.FAILED
        assert job.last_error_code == "video_deployment_superseded"
        assert job.reserved_cost_microusd == 0


@pytest.mark.asyncio
async def test_provider_404_ambiguity_is_bounded_by_watchdog(
    runtime_context: RuntimeContext,
) -> None:
    job_id = await _add_job(runtime_context, max_attempts=1)
    await runtime_context.runtime.claim_once(now=NOW)
    await runtime_context.runtime.submit_once(now=NOW)
    runtime_context.salad.get_error = SaladAPIError(
        status_code=404,
        message="not found",
        response_body="",
        request_id=None,
    )

    assert await runtime_context.runtime.observe_once(now=NOW + timedelta(seconds=31)) is True
    async with runtime_context.database.sessions() as session:
        job = await session.get(VideoGenerationJob, job_id)
        assert job is not None and job.state == VideoGenerationState.UNKNOWN
        assert job.reserved_cost_microusd > 0

    assert await runtime_context.runtime.observe_once(now=NOW + timedelta(seconds=601)) is True
    async with runtime_context.database.sessions() as session:
        job = await session.get(VideoGenerationJob, job_id)
        assert job is not None and job.state == VideoGenerationState.FAILED
        assert job.last_error_code == "video_provider_job_not_found_watchdog"
        assert job.reserved_cost_microusd == 0


@pytest.mark.asyncio
async def test_cancel_request_survives_ambiguous_post_reconciliation(
    runtime_context: RuntimeContext,
) -> None:
    job_id = await _add_job(runtime_context)
    await runtime_context.runtime.claim_once(now=NOW)
    async with runtime_context.database.sessions() as session:
        attempt = await session.scalar(
            select(VideoGenerationAttempt).where(
                VideoGenerationAttempt.video_generation_job_id == job_id
            )
        )
        job = await session.get(VideoGenerationJob, job_id)
        assert attempt is not None and job is not None
        attempt.state = VideoGenerationAttemptState.SUBMITTING
        attempt.submit_started_at = NOW
        job.state = VideoGenerationState.SUBMITTING
        await session.commit()
        attempt_id = attempt.id

    async with runtime_context.database.sessions() as session:
        assert await request_video_cancellation(
            session,
            job_id=job_id,
            actor_user_id=runtime_context.user_id,
            now=NOW + timedelta(seconds=1),
        )
        await session.commit()
    await runtime_context.runtime._mark_unknown(
        attempt_id,
        code="video_submission_outcome_unknown",
        now=NOW + timedelta(seconds=2),
    )
    async with runtime_context.database.sessions() as session:
        job = await session.get(VideoGenerationJob, job_id)
        assert job is not None and job.state == VideoGenerationState.CANCEL_REQUESTED

    prepared = await runtime_context.runtime._prepare_submission(
        attempt_id,
        now=NOW + timedelta(seconds=3),
    )
    remote = _remote_job(
        status=SaladJobStatus.PENDING,
        input=cast(JSONValue, prepared.input),
        metadata=cast(dict[str, JSONValue], prepared.metadata),
    )
    await runtime_context.runtime._bind_provider_job(
        attempt_id,
        remote,
        now=NOW + timedelta(seconds=3),
    )
    async with runtime_context.database.sessions() as session:
        job = await session.get(VideoGenerationJob, job_id)
        attempt = await session.get(VideoGenerationAttempt, attempt_id)
        assert job is not None and job.state == VideoGenerationState.CANCEL_REQUESTED
        assert attempt is not None
        assert attempt.state == VideoGenerationAttemptState.CANCEL_REQUESTED


@pytest.mark.asyncio
async def test_identity_mismatch_never_publishes_partial_output(
    runtime_context: RuntimeContext,
) -> None:
    job_id = await _add_job(runtime_context, max_attempts=1)
    await runtime_context.runtime.claim_once(now=NOW)
    await runtime_context.runtime.submit_once(now=NOW)
    async with runtime_context.database.sessions() as session:
        attempt = await session.scalar(
            select(VideoGenerationAttempt).where(
                VideoGenerationAttempt.video_generation_job_id == job_id
            )
        )
        assert attempt is not None
        output_asset_id = UUID(str(attempt.request_metadata["output_asset_id"]))
        upload_attempt_id = UUID(str(attempt.request_metadata["upload_attempt_id"]))
    remote = next(iter(runtime_context.salad.jobs.values()))
    runtime_context.salad.jobs[remote.id] = _remote_job(
        remote_id=remote.id,
        status=SaladJobStatus.SUCCEEDED,
        input=remote.input,
        metadata=remote.metadata,
        output=_success_output(
            job_id=job_id,
            attempt_id=attempt.id,
            source_id=runtime_context.source_id,
            output_asset_id=UUID(int=0),
            upload_attempt_id=upload_attempt_id,
            digest="a" * 64,
            size=12,
        ),
    )

    await runtime_context.runtime.observe_once(now=NOW + timedelta(seconds=31))
    async with runtime_context.database.sessions() as session:
        job = await session.get(VideoGenerationJob, job_id)
        asset = await session.get(Asset, output_asset_id)
        output_count = await session.scalar(select(func.count(VideoGenerationOutput.id)))
        assert job is not None and job.state == VideoGenerationState.FAILED
        assert asset is not None and asset.state == AssetState.QUARANTINED
        assert output_count == 0


@pytest.mark.asyncio
async def test_remote_failure_retries_without_partial_output(
    runtime_context: RuntimeContext,
) -> None:
    job_id = await _add_job(runtime_context, max_attempts=2)
    await runtime_context.runtime.claim_once(now=NOW)
    await runtime_context.runtime.submit_once(now=NOW)
    remote = next(iter(runtime_context.salad.jobs.values()))
    runtime_context.salad.jobs[remote.id] = _remote_job(
        remote_id=remote.id,
        status=SaladJobStatus.FAILED,
        input=remote.input,
        metadata=remote.metadata,
    )

    await runtime_context.runtime.observe_once(now=NOW + timedelta(seconds=31))
    async with runtime_context.database.sessions() as session:
        job = await session.get(VideoGenerationJob, job_id)
        output_count = await session.scalar(select(func.count(VideoGenerationOutput.id)))
        assert job is not None and job.state == VideoGenerationState.RETRY_WAIT
        assert job.retry_at is not None
        assert output_count == 0


@pytest.mark.asyncio
async def test_definitely_unsubmitted_failure_has_zero_provider_usage(
    runtime_context: RuntimeContext,
) -> None:
    job_id = await _add_job(runtime_context, max_attempts=1)
    await runtime_context.runtime.claim_once(now=NOW)
    async with runtime_context.database.sessions() as session:
        attempt = await session.scalar(
            select(VideoGenerationAttempt).where(
                VideoGenerationAttempt.video_generation_job_id == job_id
            )
        )
        assert attempt is not None
        attempt_id = attempt.id

    await runtime_context.runtime._fail_attempt(
        attempt_id,
        code="video_submission_preparation_failed",
        retryable=False,
        now=NOW + timedelta(seconds=30),
    )
    async with runtime_context.database.sessions() as session:
        job = await session.get(VideoGenerationJob, job_id)
        attempt = await session.get(VideoGenerationAttempt, attempt_id)
        assert job is not None and attempt is not None
        assert job.actual_cost_microusd == 0
        assert job.billed_duration_ms == 0
        assert attempt.actual_cost_microusd == 0
        assert attempt.billed_duration_ms == 0


@pytest.mark.asyncio
async def test_observation_lease_excludes_a_second_controller(
    runtime_context: RuntimeContext,
) -> None:
    await _add_job(runtime_context)
    await runtime_context.runtime.claim_once(now=NOW)
    await runtime_context.runtime.submit_once(now=NOW)
    second = VideoRuntime(
        config=runtime_context.runtime.config,
        sessions=runtime_context.database.sessions,
        salad=cast(Any, runtime_context.salad),
        store=runtime_context.store,
        worker_id="controller-test:video-second",
    )
    runtime_context.salad.block_get = True
    first_observation = asyncio.create_task(
        runtime_context.runtime.observe_once(now=NOW + timedelta(seconds=31))
    )
    await asyncio.wait_for(runtime_context.salad.get_started.wait(), timeout=2)

    assert await second.observe_once(now=NOW + timedelta(seconds=31)) is False
    runtime_context.salad.release_get.set()
    assert await first_observation is True


@pytest.mark.asyncio
async def test_terminal_failure_replay_does_not_double_charge(
    runtime_context: RuntimeContext,
) -> None:
    job_id = await _add_job(runtime_context)
    await runtime_context.runtime.claim_once(now=NOW)
    await runtime_context.runtime.submit_once(now=NOW)
    remote = next(iter(runtime_context.salad.jobs.values()))
    runtime_context.salad.jobs[remote.id] = _remote_job(
        remote_id=remote.id,
        status=SaladJobStatus.FAILED,
        input=remote.input,
        metadata=remote.metadata,
    )
    await runtime_context.runtime.observe_once(now=NOW + timedelta(seconds=31))
    async with runtime_context.database.sessions() as session:
        job = await session.get(VideoGenerationJob, job_id)
        attempt = await session.scalar(
            select(VideoGenerationAttempt).where(
                VideoGenerationAttempt.video_generation_job_id == job_id
            )
        )
        assert job is not None and attempt is not None
        original_cost = job.actual_cost_microusd
        original_duration = job.billed_duration_ms
        attempt_id = attempt.id

    await runtime_context.runtime._fail_attempt(
        attempt_id,
        code="stale_observer_failure",
        retryable=True,
        now=NOW + timedelta(seconds=62),
    )
    async with runtime_context.database.sessions() as session:
        job = await session.get(VideoGenerationJob, job_id)
        assert job is not None and job.state == VideoGenerationState.RETRY_WAIT
        assert job.actual_cost_microusd == original_cost
        assert job.billed_duration_ms == original_duration


@pytest.mark.asyncio
async def test_remote_watchdog_keeps_reservation_until_cancel_is_confirmed(
    runtime_context: RuntimeContext,
) -> None:
    job_id = await _add_job(runtime_context)
    await runtime_context.runtime.claim_once(now=NOW)
    await runtime_context.runtime.submit_once(now=NOW)

    remote = next(iter(runtime_context.salad.jobs.values()))
    runtime_context.salad.jobs[remote.id] = _remote_job(
        remote_id=remote.id,
        status=SaladJobStatus.RUNNING,
        input=remote.input,
        metadata=remote.metadata,
    )
    assert await runtime_context.runtime.observe_once(now=NOW + timedelta(seconds=31)) is True

    assert await runtime_context.runtime.observe_once(now=NOW + timedelta(seconds=632)) is True
    async with runtime_context.database.sessions() as session:
        job = await session.get(VideoGenerationJob, job_id)
        output_count = await session.scalar(select(func.count(VideoGenerationOutput.id)))
        assert job is not None and job.state == VideoGenerationState.CANCEL_REQUESTED
        assert job.last_error_code == "video_attempt_watchdog_expired"
        assert job.reserved_cost_microusd > 0
        assert output_count == 0
    assert runtime_context.salad.cancel_calls == 1

    runtime_context.salad.jobs[remote.id] = _remote_job(
        remote_id=remote.id,
        status=SaladJobStatus.CANCELLED,
        input=remote.input,
        metadata=remote.metadata,
    )
    assert await runtime_context.runtime.observe_once(now=NOW + timedelta(seconds=663)) is True
    async with runtime_context.database.sessions() as session:
        job = await session.get(VideoGenerationJob, job_id)
        assert job is not None and job.state == VideoGenerationState.CANCELLED
        assert job.reserved_cost_microusd == 0


@pytest.mark.asyncio
async def test_cancel_grace_stops_only_video_lane_and_retains_reservation(
    runtime_context: RuntimeContext,
) -> None:
    runtime = VideoRuntime(
        config=replace(runtime_context.runtime.config, cancel_grace_seconds=60),
        sessions=runtime_context.database.sessions,
        salad=cast(Any, runtime_context.salad),
        store=runtime_context.store,
        worker_id="controller-test:video-cancel-grace",
    )
    job_id = await _add_job(runtime_context)
    assert await runtime.claim_once(now=NOW) is True
    assert await runtime.submit_once(now=NOW) is True
    async with runtime_context.database.sessions() as session:
        assert await request_video_cancellation(
            session,
            job_id=job_id,
            actor_user_id=runtime_context.user_id,
            now=NOW + timedelta(seconds=1),
        )
        await session.commit()

    assert await runtime.observe_once(now=NOW + timedelta(seconds=31)) is True
    assert runtime_context.salad.stop_names == []
    async with runtime_context.database.sessions() as session:
        attempt = await session.scalar(
            select(VideoGenerationAttempt).where(
                VideoGenerationAttempt.video_generation_job_id == job_id
            )
        )
        assert attempt is not None
        assert attempt.request_metadata["cancel_requested_at"] == int(
            (NOW + timedelta(seconds=1)).timestamp()
        )

    assert await runtime.observe_once(now=NOW + timedelta(seconds=62)) is True
    assert runtime_context.salad.stop_names == ["animation-video-worker"]
    async with runtime_context.database.sessions() as session:
        deployment = await session.get(SaladDeployment, runtime_context.deployment_id)
        job = await session.get(VideoGenerationJob, job_id)
        assert deployment is not None
        assert deployment.purpose == SaladDeploymentPurpose.VIDEO
        assert deployment.desired_state == DesiredDeploymentState.STOPPED
        assert deployment.state == SaladDeploymentState.DRAINING
        assert job is not None and job.state == VideoGenerationState.CANCEL_REQUESTED
        assert job.reserved_cost_microusd > 0

    assert await runtime.claim_once(now=NOW + timedelta(seconds=63)) is False
    assert await runtime.submit_once(now=NOW + timedelta(seconds=63)) is False
    remote = next(iter(runtime_context.salad.jobs.values()))
    runtime_context.salad.jobs[remote.id] = _remote_job(
        remote_id=remote.id,
        status=SaladJobStatus.CANCELLED,
        input=remote.input,
        metadata=remote.metadata,
    )
    assert await runtime.observe_once(now=NOW + timedelta(seconds=93)) is True
    assert runtime_context.salad.stop_names == ["animation-video-worker"]
    async with runtime_context.database.sessions() as session:
        job = await session.get(VideoGenerationJob, job_id)
        assert job is not None and job.state == VideoGenerationState.CANCELLED
        assert job.reserved_cost_microusd == 0


@pytest.mark.asyncio
async def test_cancel_grace_cancels_unsubmitted_siblings_without_provider_identity(
    runtime_context: RuntimeContext,
) -> None:
    runtime = VideoRuntime(
        config=replace(
            runtime_context.runtime.config,
            max_queued_jobs=3,
            cancel_grace_seconds=60,
        ),
        sessions=runtime_context.database.sessions,
        salad=cast(Any, runtime_context.salad),
        store=runtime_context.store,
        worker_id="controller-test:video-cancel-siblings",
    )
    job_ids = [await _add_job(runtime_context, request_suffix=str(index)) for index in range(1, 5)]
    for _index in range(3):
        assert await runtime.claim_once(now=NOW) is True
    assert await runtime.submit_once(now=NOW) is True
    async with runtime_context.database.sessions() as session:
        assert await request_video_cancellation(
            session,
            job_id=job_ids[0],
            now=NOW + timedelta(seconds=1),
        )
        await session.commit()

    assert await runtime.observe_once(now=NOW + timedelta(seconds=31)) is True
    assert await runtime.observe_once(now=NOW + timedelta(seconds=62)) is True
    assert runtime_context.salad.stop_names == ["animation-video-worker"]
    async with runtime_context.database.sessions() as session:
        jobs = {
            job.id: job
            for job in (
                await session.scalars(
                    select(VideoGenerationJob).where(VideoGenerationJob.id.in_(job_ids))
                )
            ).all()
        }
        attempts = {
            attempt.video_generation_job_id: attempt
            for attempt in (
                await session.scalars(
                    select(VideoGenerationAttempt).where(
                        VideoGenerationAttempt.video_generation_job_id.in_(job_ids)
                    )
                )
            ).all()
        }
        assert jobs[job_ids[0]].state == VideoGenerationState.CANCEL_REQUESTED
        assert jobs[job_ids[0]].reserved_cost_microusd > 0
        for job_id in job_ids[1:3]:
            assert jobs[job_id].state == VideoGenerationState.CANCELLED
            assert jobs[job_id].reserved_cost_microusd == 0
            assert attempts[job_id].state == VideoGenerationAttemptState.FAILED
            assert attempts[job_id].provider_external_id is None
            output_asset_id = UUID(str(attempts[job_id].request_metadata["output_asset_id"]))
            asset = await session.get(Asset, output_asset_id)
            assert asset is not None and asset.state == AssetState.QUARANTINED
        assert jobs[job_ids[3]].state == VideoGenerationState.CANCELLED
        assert jobs[job_ids[3]].reserved_cost_microusd == 0


@pytest.mark.asyncio
async def test_noncurrent_lane_stop_preserves_queue_for_current_video_deployment(
    runtime_context: RuntimeContext,
) -> None:
    old_runtime = VideoRuntime(
        config=replace(runtime_context.runtime.config, cancel_grace_seconds=60),
        sessions=runtime_context.database.sessions,
        salad=cast(Any, runtime_context.salad),
        store=runtime_context.store,
        worker_id="controller-test:video-old-lane-stop",
    )
    old_job_id = await _add_job(runtime_context, request_suffix="1")
    queued_job_id = await _add_job(runtime_context, request_suffix="2")
    assert await old_runtime.claim_once(now=NOW) is True
    assert await old_runtime.submit_once(now=NOW) is True
    new_digest = "registry.example/video-worker@sha256:" + ("d" * 64)
    async with runtime_context.database.sessions() as session:
        old = await session.get(SaladDeployment, runtime_context.deployment_id)
        assert old is not None
        old.is_current = False
        session.add(
            SaladDeployment(
                version_no=2,
                config_sha256="e" * 64,
                worker_image_digest=new_digest,
                organization_name="test-org",
                project_name="test-project",
                queue_name="animation-video-v2-e5f6a7b8",
                provider_queue_id="queue-2",
                container_group_name="animation-video-worker-v2-e5f6a7b8",
                provider_container_group_id="group-2",
                purpose=SaladDeploymentPurpose.VIDEO,
                state=SaladDeploymentState.ACTIVE,
                desired_state=DesiredDeploymentState.ACTIVE,
                is_current=True,
                min_replicas=0,
                max_replicas=1,
                desired_queue_length=1,
                max_hourly_cost_microusd=350_000,
                activated_at=NOW,
            )
        )
        assert await request_video_cancellation(
            session,
            job_id=old_job_id,
            now=NOW + timedelta(seconds=1),
        )
        await session.commit()

    assert await old_runtime.observe_once(now=NOW + timedelta(seconds=31)) is True
    assert await old_runtime.observe_once(now=NOW + timedelta(seconds=62)) is True
    assert runtime_context.salad.stop_names == ["animation-video-worker"]
    async with runtime_context.database.sessions() as session:
        queued = await session.get(VideoGenerationJob, queued_job_id)
        assert queued is not None and queued.state == VideoGenerationState.QUEUED

    current_runtime = VideoRuntime(
        config=replace(
            runtime_context.runtime.config,
            worker_image_digest=new_digest,
            max_queued_jobs=3,
        ),
        sessions=runtime_context.database.sessions,
        salad=cast(Any, runtime_context.salad),
        store=runtime_context.store,
        worker_id="controller-test:video-current-lane",
    )
    assert await current_runtime.claim_once(now=NOW + timedelta(seconds=63)) is True
    async with runtime_context.database.sessions() as session:
        queued = await session.get(VideoGenerationJob, queued_job_id)
        attempt = await session.scalar(
            select(VideoGenerationAttempt).where(
                VideoGenerationAttempt.video_generation_job_id == queued_job_id
            )
        )
        assert queued is not None and queued.state == VideoGenerationState.CLAIMED
        assert attempt is not None
        assert attempt.salad_deployment_id != runtime_context.deployment_id
        assert attempt.worker_image_digest == new_digest


@pytest.mark.asyncio
async def test_cancellation_before_post_releases_reservation_and_quarantines_placeholder(
    runtime_context: RuntimeContext,
) -> None:
    job_id = await _add_job(runtime_context)
    await runtime_context.runtime.claim_once(now=NOW)
    async with runtime_context.database.sessions() as session:
        assert await request_video_cancellation(
            session,
            job_id=job_id,
            actor_user_id=runtime_context.user_id,
            now=NOW,
        )
        await session.commit()

    async with runtime_context.database.sessions() as session:
        job = await session.get(VideoGenerationJob, job_id)
        attempt = await session.scalar(
            select(VideoGenerationAttempt).where(
                VideoGenerationAttempt.video_generation_job_id == job_id
            )
        )
        assert job is not None and job.state == VideoGenerationState.CANCELLED
        assert attempt is not None
        assert attempt.state == VideoGenerationAttemptState.FAILED
        output_asset_id = UUID(str(attempt.request_metadata["output_asset_id"]))
        asset = await session.get(Asset, output_asset_id)
        assert asset is not None and asset.state == AssetState.QUARANTINED
        audit = await session.scalar(
            select(AuditEvent).where(
                AuditEvent.resource_type == "video_generation_job",
                AuditEvent.resource_id == job_id,
                AuditEvent.action == "video_generation.cancellation_requested",
            )
        )
        assert audit is not None
        assert audit.actor == str(runtime_context.user_id)
        assert audit.detail == {
            "previous_state": VideoGenerationState.CLAIMED.value,
            "state": VideoGenerationState.CANCELLED.value,
            "attempt_id": str(attempt.id),
        }

    # A terminal replay is successful but cannot mutate the job or duplicate
    # the immutable cancellation audit marker.
    async with runtime_context.database.sessions() as session:
        assert await request_video_cancellation(
            session,
            job_id=job_id,
            actor_user_id=runtime_context.user_id,
            now=NOW + timedelta(seconds=1),
        )
        await session.commit()
    async with runtime_context.database.sessions() as session:
        audit_count = await session.scalar(
            select(func.count(AuditEvent.id)).where(
                AuditEvent.resource_type == "video_generation_job",
                AuditEvent.resource_id == job_id,
                AuditEvent.action == "video_generation.cancellation_requested",
            )
        )
        job = await session.get(VideoGenerationJob, job_id)
        assert audit_count == 1
        assert job is not None and job.updated_at.replace(tzinfo=UTC) == NOW


@pytest.mark.asyncio
async def test_cancelling_retry_wait_does_not_resurrect_or_recharge_attempt(
    runtime_context: RuntimeContext,
) -> None:
    job_id = await _add_job(runtime_context)
    await runtime_context.runtime.claim_once(now=NOW)
    await runtime_context.runtime.submit_once(now=NOW)
    remote = next(iter(runtime_context.salad.jobs.values()))
    runtime_context.salad.jobs[remote.id] = _remote_job(
        remote_id=remote.id,
        status=SaladJobStatus.FAILED,
        input=remote.input,
        metadata=remote.metadata,
    )
    await runtime_context.runtime.observe_once(now=NOW + timedelta(seconds=31))
    async with runtime_context.database.sessions() as session:
        before = await session.get(VideoGenerationJob, job_id)
        attempt = await session.scalar(
            select(VideoGenerationAttempt).where(
                VideoGenerationAttempt.video_generation_job_id == job_id
            )
        )
        assert before is not None and before.state == VideoGenerationState.RETRY_WAIT
        assert attempt is not None and attempt.state == VideoGenerationAttemptState.FAILED
        original_cost = before.actual_cost_microusd
        original_attempt_cost = attempt.actual_cost_microusd

    async with runtime_context.database.sessions() as session:
        assert await request_video_cancellation(
            session,
            job_id=job_id,
            actor_user_id=runtime_context.user_id,
            now=NOW + timedelta(seconds=32),
        )
        await session.commit()
    async with runtime_context.database.sessions() as session:
        job = await session.get(VideoGenerationJob, job_id)
        attempt = await session.scalar(
            select(VideoGenerationAttempt).where(
                VideoGenerationAttempt.video_generation_job_id == job_id
            )
        )
        assert job is not None and job.state == VideoGenerationState.CANCELLED
        assert job.actual_cost_microusd == original_cost
        assert job.retry_at is None
        assert attempt is not None and attempt.state == VideoGenerationAttemptState.FAILED
        assert attempt.actual_cost_microusd == original_attempt_cost


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cancel_error",
    [
        SaladTransportError("cancel outcome unknown"),
        SaladAPIError(
            status_code=409,
            message="already terminal",
            response_body="",
            request_id=None,
        ),
    ],
    ids=("transport", "conflict"),
)
async def test_cancel_error_still_gets_authoritative_terminal_state(
    runtime_context: RuntimeContext,
    cancel_error: Exception,
) -> None:
    job_id = await _add_job(runtime_context)
    await runtime_context.runtime.claim_once(now=NOW)
    await runtime_context.runtime.submit_once(now=NOW)
    async with runtime_context.database.sessions() as session:
        assert await request_video_cancellation(
            session,
            job_id=job_id,
            actor_user_id=runtime_context.user_id,
            now=NOW + timedelta(seconds=1),
        )
        await session.commit()
    remote = next(iter(runtime_context.salad.jobs.values()))
    runtime_context.salad.jobs[remote.id] = _remote_job(
        remote_id=remote.id,
        status=SaladJobStatus.CANCELLED,
        input=remote.input,
        metadata=remote.metadata,
    )
    runtime_context.salad.cancel_error = cancel_error

    assert await runtime_context.runtime.observe_once(now=NOW + timedelta(seconds=2)) is True
    async with runtime_context.database.sessions() as session:
        job = await session.get(VideoGenerationJob, job_id)
        assert job is not None and job.state == VideoGenerationState.CANCELLED
        assert job.reserved_cost_microusd == 0


@pytest.mark.asyncio
async def test_max_concurrency_and_budget_fail_closed(runtime_context: RuntimeContext) -> None:
    first = await _add_job(runtime_context, request_suffix="1")
    second = await _add_job(runtime_context, request_suffix="2")
    assert await runtime_context.runtime.claim_once(now=NOW) is True
    assert await runtime_context.runtime.claim_once(now=NOW) is False
    async with runtime_context.database.sessions() as session:
        attempts = await session.scalar(select(func.count(VideoGenerationAttempt.id)))
        first_job = await session.get(VideoGenerationJob, first)
        second_job = await session.get(VideoGenerationJob, second)
        assert attempts == 1
        assert first_job is not None and first_job.state == VideoGenerationState.CLAIMED
        assert second_job is not None and second_job.state == VideoGenerationState.QUEUED

        guard = await session.scalar(
            select(ProviderBudgetGuard).where(ProviderBudgetGuard.provider == "salad")
        )
        assert guard is not None
        guard.daily_limit_microusd = 1
        guard.monthly_limit_microusd = 1
        await session.commit()

    # The budget guard is acquired before the deployment lock on every claim;
    # this remains safe even when the concurrency ceiling rejects the job.
    assert await runtime_context.runtime.claim_once(now=NOW) is False


@pytest.mark.asyncio
async def test_configured_queue_admits_three_jobs_for_one_replica(
    runtime_context: RuntimeContext,
) -> None:
    runtime = VideoRuntime(
        config=replace(runtime_context.runtime.config, max_queued_jobs=3),
        sessions=runtime_context.database.sessions,
        salad=cast(Any, runtime_context.salad),
        store=runtime_context.store,
        worker_id="controller-test:video-three",
    )
    job_ids = [await _add_job(runtime_context, request_suffix=str(index)) for index in range(1, 5)]

    assert await runtime.claim_once(now=NOW) is True
    assert await runtime.claim_once(now=NOW) is True
    assert await runtime.claim_once(now=NOW) is True
    assert await runtime.claim_once(now=NOW) is False

    async with runtime_context.database.sessions() as session:
        active_count = await session.scalar(
            select(func.count(VideoGenerationJob.id)).where(
                VideoGenerationJob.id.in_(job_ids),
                VideoGenerationJob.state == VideoGenerationState.CLAIMED,
            )
        )
        queued_count = await session.scalar(
            select(func.count(VideoGenerationJob.id)).where(
                VideoGenerationJob.id.in_(job_ids),
                VideoGenerationJob.state == VideoGenerationState.QUEUED,
            )
        )
        deployment = await session.get(SaladDeployment, runtime_context.deployment_id)
        assert active_count == 3
        assert queued_count == 1
        assert deployment is not None
        assert deployment.min_replicas == 0
        assert deployment.max_replicas == 1


def test_video_runtime_config_rejects_ttls_shorter_than_provider_runway() -> None:
    config = VideoRuntimeConfig(
        enabled=True,
        queue_name="animation-video",
        worker_image_digest=WORKER_IMAGE,
        signing_key_id="video-test-key",
        signing_private_key=encode_base64url(bytes(range(32))),
        signature_ttl_seconds=720,
        grant_ttl_seconds=1800,
        attempt_watchdog_seconds=600,
        reconciliation_interval_seconds=30,
    )
    with pytest.raises(ValueError, match="signature TTL"):
        replace(config, signature_ttl_seconds=620)
    with pytest.raises(ValueError, match="grant TTL"):
        replace(config, grant_ttl_seconds=1200)
    with pytest.raises(ValueError, match="grant TTL"):
        replace(config, grant_ttl_seconds=14_401)


@pytest.mark.asyncio
async def test_three_variants_are_signed_just_in_time_for_one_replica(
    runtime_context: RuntimeContext,
) -> None:
    runtime = VideoRuntime(
        config=replace(runtime_context.runtime.config, max_queued_jobs=3),
        sessions=runtime_context.database.sessions,
        salad=cast(Any, runtime_context.salad),
        store=runtime_context.store,
        worker_id="controller-test:video-jit",
    )
    for index in range(1, 4):
        await _add_job(runtime_context, request_suffix=str(index))
        assert await runtime.claim_once(now=NOW) is True

    assert await runtime.submit_once(now=NOW) is True
    assert await runtime.submit_once(now=NOW + timedelta(seconds=1)) is False
    assert runtime_context.salad.create_calls == 1

    first = next(iter(runtime_context.salad.jobs.values()))
    runtime_context.salad.jobs[first.id] = _remote_job(
        remote_id=first.id,
        status=SaladJobStatus.RUNNING,
        input=first.input,
        metadata=first.metadata,
    )
    assert await runtime.observe_once(now=NOW + timedelta(seconds=31)) is True
    assert await runtime.submit_once(now=NOW + timedelta(seconds=32)) is True
    assert await runtime.submit_once(now=NOW + timedelta(seconds=33)) is False
    assert runtime_context.salad.create_calls == 2

    runtime_context.salad.jobs[first.id] = _remote_job(
        remote_id=first.id,
        status=SaladJobStatus.FAILED,
        input=first.input,
        metadata=first.metadata,
    )
    assert await runtime.observe_once(now=NOW + timedelta(seconds=62)) is True
    second = list(runtime_context.salad.jobs.values())[1]
    runtime_context.salad.jobs[second.id] = _remote_job(
        remote_id=second.id,
        status=SaladJobStatus.RUNNING,
        input=second.input,
        metadata=second.metadata,
    )
    assert await runtime.observe_once(now=NOW + timedelta(seconds=63)) is True
    assert await runtime.submit_once(now=NOW + timedelta(seconds=64)) is True
    assert runtime_context.salad.create_calls == 3

    issued_at = [
        AnimateEnvelope.model_validate(remote.input, strict=True).issued_at
        for remote in runtime_context.salad.jobs.values()
    ]
    assert issued_at == [
        int(NOW.timestamp()),
        int((NOW + timedelta(seconds=32)).timestamp()),
        int((NOW + timedelta(seconds=64)).timestamp()),
    ]


@pytest.mark.asyncio
async def test_second_video_reservation_is_not_double_counted(
    runtime_context: RuntimeContext,
) -> None:
    runtime = VideoRuntime(
        config=replace(runtime_context.runtime.config, max_queued_jobs=3),
        sessions=runtime_context.database.sessions,
        salad=cast(Any, runtime_context.salad),
        store=runtime_context.store,
        worker_id="controller-test:video-budget-two",
    )
    await _add_job(runtime_context, request_suffix="1")
    await _add_job(runtime_context, request_suffix="2")
    async with runtime_context.database.sessions() as session:
        guard = await session.scalar(
            select(ProviderBudgetGuard).where(ProviderBudgetGuard.provider == "salad")
        )
        assert guard is not None
        guard.daily_limit_microusd = 120_000
        guard.monthly_limit_microusd = 120_000
        await session.commit()

    assert await runtime.claim_once(now=NOW) is True
    assert await runtime.claim_once(now=NOW) is True
    async with runtime_context.database.sessions() as session:
        attempts = await session.scalar(select(func.count(VideoGenerationAttempt.id)))
        assert attempts == 2


@pytest.mark.asyncio
async def test_disabled_runtime_drains_without_admitting_or_posting(
    runtime_context: RuntimeContext,
) -> None:
    active_job_id = await _add_job(runtime_context, request_suffix="1")
    queued_job_id = await _add_job(runtime_context, request_suffix="2")
    await runtime_context.runtime.claim_once(now=NOW)
    await runtime_context.runtime.submit_once(now=NOW)
    disabled = VideoRuntime(
        config=replace(
            runtime_context.runtime.config,
            enabled=False,
            queue_name="",
            worker_image_digest="",
            signing_key_id="",
            signing_private_key="",
        ),
        sessions=runtime_context.database.sessions,
        salad=cast(Any, runtime_context.salad),
        store=runtime_context.store,
        worker_id="controller-test:video-disabled",
    )

    assert await disabled.claim_once(now=NOW + timedelta(seconds=1)) is False
    assert await disabled.disable_once(now=NOW + timedelta(seconds=1)) is True
    assert await disabled.disable_once(now=NOW + timedelta(seconds=2)) is True
    async with runtime_context.database.sessions() as session:
        active = await session.get(VideoGenerationJob, active_job_id)
        queued = await session.get(VideoGenerationJob, queued_job_id)
        assert active is not None and active.state == VideoGenerationState.CANCEL_REQUESTED
        assert active.reserved_cost_microusd > 0
        assert queued is not None and queued.state == VideoGenerationState.CANCELLED
    assert runtime_context.salad.create_calls == 1

    remote = next(iter(runtime_context.salad.jobs.values()))
    runtime_context.salad.jobs[remote.id] = _remote_job(
        remote_id=remote.id,
        status=SaladJobStatus.CANCELLED,
        input=remote.input,
        metadata=remote.metadata,
    )
    assert await disabled.disable_once(now=NOW + timedelta(seconds=3)) is True
    async with runtime_context.database.sessions() as session:
        active = await session.get(VideoGenerationJob, active_job_id)
        assert active is not None and active.state == VideoGenerationState.CANCELLED
        assert active.reserved_cost_microusd == 0
    assert runtime_context.salad.create_calls == 1


@pytest.mark.asyncio
async def test_budget_capacity_blocks_before_attempt(runtime_context: RuntimeContext) -> None:
    job_id = await _add_job(runtime_context)
    async with runtime_context.database.sessions() as session:
        guard = await session.scalar(
            select(ProviderBudgetGuard).where(ProviderBudgetGuard.provider == "salad")
        )
        assert guard is not None
        guard.daily_limit_microusd = 10_000
        guard.monthly_limit_microusd = 10_000
        await session.commit()

    assert await runtime_context.runtime.claim_once(now=NOW) is False
    async with runtime_context.database.sessions() as session:
        job = await session.get(VideoGenerationJob, job_id)
        attempts = await session.scalar(select(func.count(VideoGenerationAttempt.id)))
        assert job is not None and job.state == VideoGenerationState.QUEUED
        assert attempts == 0


@pytest.mark.asyncio
async def test_image_budget_decision_sees_active_video_reservation(
    runtime_context: RuntimeContext,
) -> None:
    await _add_job(runtime_context)
    assert await runtime_context.runtime.claim_once(now=NOW) is True
    image_attempt_id = await _add_image_attempt(runtime_context, suffix="1")
    async with runtime_context.database.sessions() as session:
        guard = await session.scalar(
            select(ProviderBudgetGuard).where(ProviderBudgetGuard.provider == "salad")
        )
        assert guard is not None
        guard.daily_limit_microusd = 100_000
        guard.monthly_limit_microusd = 100_000
        decision = await reserve_attempt_budget(
            session,
            provider="salad",
            attempt_id=image_attempt_id,
            amount_microusd=50_000,
            now=NOW,
        )
        await session.commit()

    assert decision.accepted is False
    assert decision.snapshot.active_reservations_microusd == (
        runtime_context.runtime.config.conservative_reservation_microusd
    )


@pytest.mark.asyncio
async def test_video_budget_decision_sees_active_image_reservation(
    runtime_context: RuntimeContext,
) -> None:
    image_attempt_id = await _add_image_attempt(runtime_context, suffix="1")
    async with runtime_context.database.sessions() as session:
        guard = await session.scalar(
            select(ProviderBudgetGuard).where(ProviderBudgetGuard.provider == "salad")
        )
        assert guard is not None
        guard.daily_limit_microusd = 100_000
        guard.monthly_limit_microusd = 100_000
        decision = await reserve_attempt_budget(
            session,
            provider="salad",
            attempt_id=image_attempt_id,
            amount_microusd=70_000,
            now=NOW,
        )
        await session.commit()
    assert decision.accepted is True

    job_id = await _add_job(runtime_context)
    assert await runtime_context.runtime.claim_once(now=NOW) is False
    async with runtime_context.database.sessions() as session:
        job = await session.get(VideoGenerationJob, job_id)
        attempts = await session.scalar(select(func.count(VideoGenerationAttempt.id)))
        assert job is not None and job.state == VideoGenerationState.QUEUED
        assert attempts == 0


def _remote_job(
    *,
    status: SaladJobStatus,
    input: JSONValue,
    metadata: dict[str, JSONValue],
    remote_id: UUID | None = None,
    output: Any = None,
) -> SaladQueueJob:
    return SaladQueueJob(
        id=remote_id or UUID(int=len(metadata) + 1),
        input=input,
        status=status,
        events=(),
        create_time=NOW,
        update_time=NOW,
        metadata=metadata,
        webhook=None,
        output=output,
    )


def _success_output(
    *,
    job_id: UUID,
    attempt_id: UUID,
    source_id: UUID,
    output_asset_id: UUID,
    upload_attempt_id: UUID,
    digest: str,
    size: int,
) -> dict[str, object]:
    return {
        "version": "video-worker.v1",
        "job_id": str(job_id),
        "attempt_id": str(attempt_id),
        "status": "succeeded",
        "profile_id": PINNED_VIDEO_PROFILE.profile_id,
        "source_asset_id": str(source_id),
        "output_asset_id": str(output_asset_id),
        "upload_attempt_id": str(upload_attempt_id),
        "output_sha256": digest,
        "output_size_bytes": size,
        "loop_mode": "ping_pong",
        "fps": 24,
        "width": 480,
        "height": 832,
        "native_frame_count": 73,
        "native_duration_seconds": round(73 / 24, 6),
        "output_frame_count": 144,
        "output_duration_seconds": 6.0,
    }
