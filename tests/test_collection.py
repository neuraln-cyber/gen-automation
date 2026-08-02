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
