from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from uuid import UUID

import pytest
from PIL import Image
from sqlalchemy import select

from gen_automation.db.models import (
    Asset,
    AuditEvent,
    GenerationAttempt,
    GenerationJob,
    Project,
    Release,
    ReleaseVersion,
    SaladDeployment,
)
from gen_automation.db.session import Database
from gen_automation.domain.enums import (
    AssetState,
    GenerationAttemptState,
    GenerationState,
    ReleasePhase,
    ResourceHealth,
)
from gen_automation.services.assets import UploadIntent, create_raw_master_upload_intents
from gen_automation.services.collection import (
    claim_collection_jobs,
    collect_generation_job,
    collect_next_ready_running_asset,
    load_stop_salvage_status,
)
from gen_automation.storage.memory import MemoryObjectStore

NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)


@dataclass(frozen=True)
class CollectionContext:
    database: Database
    store: MemoryObjectStore
    release_id: UUID
    job_id: UUID
    intents: tuple[UploadIntent, ...]


def _png(color: str) -> bytes:
    output = BytesIO()
    Image.new("RGB", (32, 24), color).save(output, format="PNG")
    return output.getvalue()


def _stage(store: MemoryObjectStore, intent: UploadIntent, body: bytes) -> None:
    metadata = {
        key.removeprefix("x-amz-meta-"): value
        for key, value in intent.upload_fields.items()
        if key.startswith("x-amz-meta-")
    }
    store.put_for_test(
        intent.staging_key,
        body,
        content_type="image/png",
        metadata=metadata,
    )


@pytest.fixture
async def collection_context(tmp_path: Path) -> AsyncIterator[CollectionContext]:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'collection.db').as_posix()}")
    await database.create_schema()
    store = MemoryObjectStore()
    async with database.sessions() as session:
        project = Project(slug="collection", name="Collection")
        session.add(project)
        await session.flush()
        release = Release(
            project_id=project.id,
            slug="release",
            title="Release",
            phase=ReleasePhase.GENERATING,
            desired_accepted_count=2,
        )
        session.add(release)
        await session.flush()
        version = ReleaseVersion(
            release_id=release.id,
            version_no=1,
            specification={"schema_version": 1},
            specification_sha256="a" * 64,
            created_by="test",
            created_at=NOW,
        )
        session.add(version)
        await session.flush()
        job = GenerationJob(
            release_version_id=version.id,
            logical_key="b" * 64,
            parameters={"seed": 1},
            parameters_sha256="c" * 64,
            provider="salad",
            state=GenerationState.COLLECTING,
            expected_output_count=2,
        )
        session.add(job)
        await session.flush()
        deployment = SaladDeployment(
            version_no=1,
            config_sha256="d" * 64,
            provider_configuration={},
            worker_image_digest="registry.example.test/worker@sha256:" + "e" * 64,
            organization_name="test-org",
            project_name="test-project",
            queue_name="test-queue",
            container_group_name="test-group",
            max_hourly_cost_microusd=1_000_000,
        )
        session.add(deployment)
        await session.flush()
        session.add(
            GenerationAttempt(
                job_id=job.id,
                salad_deployment_id=deployment.id,
                attempt_no=1,
                provider="salad",
                provider_external_id="provider-job-1",
                submission_key="f" * 64,
                request_sha256="1" * 64,
                state=GenerationAttemptState.SUCCEEDED,
                worker_image_digest=deployment.worker_image_digest,
                request_metadata={},
                submit_started_at=NOW,
                submitted_at=NOW,
                completed_at=NOW,
                created_at=NOW,
            )
        )
        await session.commit()
        intents = await create_raw_master_upload_intents(
            session,
            store,
            generation_job_id=job.id,
            max_bytes=1_000_000,
        )
        result = CollectionContext(
            database=database,
            store=store,
            release_id=release.id,
            job_id=job.id,
            intents=tuple(intents),
        )
    try:
        yield result
    finally:
        await database.dispose()


async def _claim(context: CollectionContext, *, now: datetime = NOW) -> None:
    async with context.database.sessions() as session:
        claimed = await claim_collection_jobs(
            session,
            worker_id="collector-1",
            now=now,
        )
    assert [item.job_id for item in claimed] == [context.job_id]


@pytest.mark.asyncio
async def test_running_job_publishes_each_staged_master_independently(
    collection_context: CollectionContext,
) -> None:
    _stage(collection_context.store, collection_context.intents[0], _png("red"))
    async with collection_context.database.sessions() as session:
        job = await session.get(GenerationJob, collection_context.job_id)
        assert job is not None
        job.state = GenerationState.RUNNING
        await session.commit()

        first = await collect_next_ready_running_asset(
            session,
            collection_context.store,
            worker_id="progressive-collector",
            max_image_bytes=1_000_000,
        )
        assets = list(
            (
                await session.scalars(
                    select(Asset)
                    .where(Asset.generation_job_id == collection_context.job_id)
                    .order_by(Asset.output_index)
                )
            ).all()
        )
        release = await session.get(Release, collection_context.release_id)
        job = await session.get(GenerationJob, collection_context.job_id)

    assert first.asset_id == collection_context.intents[0].asset_id
    assert first.finalized is True
    assert [asset.state for asset in assets] == [
        AssetState.AVAILABLE,
        AssetState.UPLOADING,
    ]
    assert job is not None and job.state == GenerationState.RUNNING
    assert release is not None and release.phase == ReleasePhase.GENERATING

    async with collection_context.database.sessions() as session:
        waiting = await collect_next_ready_running_asset(
            session,
            collection_context.store,
            worker_id="progressive-collector",
            max_image_bytes=1_000_000,
        )

    assert waiting.asset_id == collection_context.intents[1].asset_id
    assert waiting.finalized is False

    _stage(collection_context.store, collection_context.intents[1], _png("blue"))
    async with collection_context.database.sessions() as session:
        second = await collect_next_ready_running_asset(
            session,
            collection_context.store,
            worker_id="progressive-collector",
            max_image_bytes=1_000_000,
        )
        assets = list(
            (
                await session.scalars(
                    select(Asset)
                    .where(Asset.generation_job_id == collection_context.job_id)
                    .order_by(Asset.output_index)
                )
            ).all()
        )

    assert second.asset_id == collection_context.intents[1].asset_id
    assert second.finalized is True
    assert [asset.state for asset in assets] == [
        AssetState.AVAILABLE,
        AssetState.AVAILABLE,
    ]


@pytest.mark.parametrize(
    "job_state",
    (GenerationState.SUBMITTING, GenerationState.UNKNOWN),
)
@pytest.mark.asyncio
async def test_provider_active_job_publishes_first_master_before_batch_completion(
    collection_context: CollectionContext,
    job_state: GenerationState,
) -> None:
    """A first upload is visible while provider submission/recovery is unresolved."""

    _stage(collection_context.store, collection_context.intents[0], _png("yellow"))
    async with collection_context.database.sessions() as session:
        job = await session.get(GenerationJob, collection_context.job_id)
        assert job is not None
        job.state = job_state
        await session.commit()

        result = await collect_next_ready_running_asset(
            session,
            collection_context.store,
            worker_id="progressive-collector",
            max_image_bytes=1_000_000,
        )
        assets = list(
            (
                await session.scalars(
                    select(Asset)
                    .where(Asset.generation_job_id == collection_context.job_id)
                    .order_by(Asset.output_index)
                )
            ).all()
        )
        persisted_job = await session.get(GenerationJob, collection_context.job_id)

    assert result.asset_id == collection_context.intents[0].asset_id
    assert result.finalized is True
    assert [asset.state for asset in assets] == [
        AssetState.AVAILABLE,
        AssetState.UPLOADING,
    ]
    assert persisted_job is not None and persisted_job.state == job_state


@pytest.mark.asyncio
async def test_missing_older_job_does_not_hide_visible_later_job_master(
    collection_context: CollectionContext,
) -> None:
    async with collection_context.database.sessions() as session:
        first_job = await session.get(GenerationJob, collection_context.job_id)
        assert first_job is not None
        first_job.state = GenerationState.RUNNING
        later_job = GenerationJob(
            release_version_id=first_job.release_version_id,
            logical_key="later-visible-job",
            parameters={"seed": 2},
            parameters_sha256="2" * 64,
            provider="salad",
            state=GenerationState.RUNNING,
            priority=first_job.priority + 1,
            expected_output_count=1,
        )
        session.add(later_job)
        await session.commit()
        later_intent = (
            await create_raw_master_upload_intents(
                session,
                collection_context.store,
                generation_job_id=later_job.id,
                max_bytes=1_000_000,
            )
        )[0]

    _stage(collection_context.store, later_intent, _png("cyan"))
    async with collection_context.database.sessions() as session:
        result = await collect_next_ready_running_asset(
            session,
            collection_context.store,
            worker_id="progressive-collector",
            max_image_bytes=1_000_000,
        )
        first_assets = list(
            (
                await session.scalars(
                    select(Asset)
                    .where(Asset.generation_job_id == collection_context.job_id)
                    .order_by(Asset.output_index)
                )
            ).all()
        )
        later_asset = await session.get(Asset, later_intent.asset_id)

    assert result.asset_id == later_intent.asset_id
    assert result.finalized is True
    assert [asset.state for asset in first_assets] == [
        AssetState.UPLOADING,
        AssetState.UPLOADING,
    ]
    assert later_asset is not None and later_asset.state == AssetState.AVAILABLE


@pytest.mark.asyncio
async def test_stopped_terminal_job_salvages_uploaded_master_then_retires_absent_intent(
    collection_context: CollectionContext,
) -> None:
    _stage(collection_context.store, collection_context.intents[0], _png("purple"))
    async with collection_context.database.sessions() as session:
        job = await session.get(GenerationJob, collection_context.job_id)
        release = await session.get(Release, collection_context.release_id)
        assert job is not None and release is not None
        release_version_id = job.release_version_id
        job.state = GenerationState.CANCELLED
        release.phase = ReleasePhase.PAUSED
        session.add(
            AuditEvent(
                actor="owner",
                action="release.generation_stop_requested",
                resource_type="release",
                resource_id=release.id,
                correlation_id=f"generation-stop:{release.id}",
                detail={"safe_drain": True},
                occurred_at=NOW,
            )
        )
        await session.commit()

        before = await load_stop_salvage_status(
            session,
            release_version_id=release_version_id,
        )
        salvaged = await collect_next_ready_running_asset(
            session,
            collection_context.store,
            worker_id="stop-salvage-collector",
            max_image_bytes=1_000_000,
            now=NOW,
        )
        middle = await load_stop_salvage_status(
            session,
            release_version_id=release_version_id,
        )
        retired = await collect_next_ready_running_asset(
            session,
            collection_context.store,
            worker_id="stop-salvage-collector",
            max_image_bytes=1_000_000,
            now=NOW,
        )
        after = await load_stop_salvage_status(
            session,
            release_version_id=release_version_id,
        )
        assets = list(
            (
                await session.scalars(
                    select(Asset)
                    .where(Asset.generation_job_id == collection_context.job_id)
                    .order_by(Asset.output_index)
                )
            ).all()
        )
        retired_events = list(
            (
                await session.scalars(
                    select(AuditEvent).where(
                        AuditEvent.action == "asset.stop_salvage_upload_absent"
                    )
                )
            ).all()
        )

    assert before.stop_requested is True
    assert before.uploading_assets == 2
    assert before.pending_assets == 2
    assert before.pending is True
    assert salvaged.asset_id == collection_context.intents[0].asset_id
    assert salvaged.finalized is True
    assert middle.pending_assets == 1
    assert retired.asset_id == collection_context.intents[1].asset_id
    assert retired.finalized is False
    assert after.pending is False
    assert [asset.state for asset in assets] == [
        AssetState.AVAILABLE,
        AssetState.QUARANTINED,
    ]
    assert assets[1].verification_error_code == "stopped_generation_upload_absent"
    assert len(retired_events) == 1


@pytest.mark.asyncio
async def test_stopped_terminal_job_reclaims_expired_verification_lease(
    collection_context: CollectionContext,
) -> None:
    _stage(collection_context.store, collection_context.intents[0], _png("orange"))
    async with collection_context.database.sessions() as session:
        job = await session.get(GenerationJob, collection_context.job_id)
        release = await session.get(Release, collection_context.release_id)
        asset = await session.get(Asset, collection_context.intents[0].asset_id)
        assert job is not None and release is not None and asset is not None
        release_version_id = job.release_version_id
        asset_id = asset.id
        job.state = GenerationState.FAILED
        release.phase = ReleasePhase.PAUSED
        asset.state = AssetState.VERIFYING
        asset.verification_lease_owner = "dead-collector"
        asset.verification_lease_expires_at = NOW - timedelta(seconds=1)
        session.add(
            AuditEvent(
                actor="owner",
                action="release.generation_stop_requested",
                resource_type="release",
                resource_id=release.id,
                correlation_id=f"generation-stop:{release.id}",
                detail={"safe_drain": True},
                occurred_at=NOW,
            )
        )
        await session.commit()

        status = await load_stop_salvage_status(
            session,
            release_version_id=release_version_id,
        )
        result = await collect_next_ready_running_asset(
            session,
            collection_context.store,
            worker_id="replacement-stop-salvage-collector",
            max_image_bytes=1_000_000,
            now=NOW,
        )
        reclaimed = await session.get(Asset, asset_id)

    assert status.verifying_assets == 1
    assert status.uploading_assets == 1
    assert result.asset_id == asset_id
    assert result.finalized is True
    assert reclaimed is not None
    assert reclaimed.state == AssetState.AVAILABLE
    assert reclaimed.verification_lease_owner is None
    assert reclaimed.verification_lease_expires_at is None


@pytest.mark.asyncio
async def test_terminal_job_without_stop_marker_is_not_progressively_salvaged(
    collection_context: CollectionContext,
) -> None:
    _stage(collection_context.store, collection_context.intents[0], _png("green"))
    async with collection_context.database.sessions() as session:
        job = await session.get(GenerationJob, collection_context.job_id)
        assert job is not None
        job.state = GenerationState.FAILED
        await session.commit()

        status = await load_stop_salvage_status(
            session,
            release_version_id=job.release_version_id,
        )
        result = await collect_next_ready_running_asset(
            session,
            collection_context.store,
            worker_id="progressive-collector",
            max_image_bytes=1_000_000,
            now=NOW,
        )
        asset = await session.get(Asset, collection_context.intents[0].asset_id)

    assert status.stop_requested is False
    assert status.pending is False
    assert result.asset_id is None
    assert result.finalized is False
    assert asset is not None and asset.state == AssetState.UPLOADING


@pytest.mark.asyncio
async def test_collection_verifies_all_masters_and_advances_release(
    collection_context: CollectionContext,
) -> None:
    for intent, color in zip(collection_context.intents, ("red", "blue"), strict=True):
        _stage(collection_context.store, intent, _png(color))
    await _claim(collection_context)

    async with collection_context.database.sessions() as session:
        result = await collect_generation_job(
            session,
            collection_context.store,
            job_id=collection_context.job_id,
            worker_id="collector-1",
            max_image_bytes=1_000_000,
            now=NOW,
        )
        release = await session.get(Release, collection_context.release_id)

    assert result.state == GenerationState.SUCCEEDED
    assert result.finalized_assets == 2
    assert release is not None
    assert release.phase == ReleasePhase.REVIEWING
    assert all(key.startswith("masters/") for key in collection_context.store.objects)


@pytest.mark.asyncio
async def test_collection_defers_when_an_upload_is_not_visible(
    collection_context: CollectionContext,
) -> None:
    _stage(collection_context.store, collection_context.intents[0], _png("red"))
    await _claim(collection_context)

    async with collection_context.database.sessions() as session:
        result = await collect_generation_job(
            session,
            collection_context.store,
            job_id=collection_context.job_id,
            worker_id="collector-1",
            max_image_bytes=1_000_000,
            now=NOW,
        )

    assert result.state == GenerationState.COLLECTING
    assert result.retry_at == NOW + timedelta(seconds=30)
    assert result.error_code == "master_not_ready"


@pytest.mark.asyncio
async def test_collection_blocks_after_missing_upload_grant_expires(
    collection_context: CollectionContext,
) -> None:
    _stage(collection_context.store, collection_context.intents[0], _png("red"))
    expired_at = NOW + timedelta(seconds=3630)
    await _claim(collection_context, now=expired_at)

    async with collection_context.database.sessions() as session:
        result = await collect_generation_job(
            session,
            collection_context.store,
            job_id=collection_context.job_id,
            worker_id="collector-1",
            max_image_bytes=1_000_000,
            upload_grant_ttl_seconds=3600,
            now=expired_at,
        )
        release = await session.get(Release, collection_context.release_id)
        assets = list(
            (
                await session.scalars(
                    select(Asset)
                    .where(Asset.generation_job_id == collection_context.job_id)
                    .order_by(Asset.output_index)
                )
            ).all()
        )

    assert result.state == GenerationState.DEAD_LETTER
    assert result.retry_at is None
    assert result.error_code == "master_upload_expired"
    assert release is not None
    assert release.health == ResourceHealth.BLOCKED
    assert [asset.state for asset in assets] == [
        AssetState.AVAILABLE,
        AssetState.UPLOADING,
    ]


@pytest.mark.asyncio
async def test_collection_quarantines_invalid_output_and_blocks_release(
    collection_context: CollectionContext,
) -> None:
    _stage(collection_context.store, collection_context.intents[0], b"not-an-image")
    _stage(collection_context.store, collection_context.intents[1], _png("blue"))
    await _claim(collection_context)

    async with collection_context.database.sessions() as session:
        result = await collect_generation_job(
            session,
            collection_context.store,
            job_id=collection_context.job_id,
            worker_id="collector-1",
            max_image_bytes=1_000_000,
            now=NOW,
        )
        release = await session.get(Release, collection_context.release_id)

    assert result.state == GenerationState.DEAD_LETTER
    assert result.error_code == "master_quarantined"
    assert release is not None
    assert release.health == ResourceHealth.BLOCKED


@pytest.mark.asyncio
async def test_expired_collection_lease_is_reclaimed(
    collection_context: CollectionContext,
) -> None:
    await _claim(collection_context)
    async with collection_context.database.sessions() as session:
        reclaimed = await claim_collection_jobs(
            session,
            worker_id="collector-2",
            now=NOW + timedelta(minutes=16),
        )

    assert [item.job_id for item in reclaimed] == [collection_context.job_id]
