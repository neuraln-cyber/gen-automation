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
    RetryUploadSalvageDeferredError,
    claim_collection_jobs,
    collect_generation_job,
    collect_next_ready_running_asset,
    create_retry_safe_raw_master_upload_intents,
    load_stop_salvage_status,
)
from gen_automation.storage.base import ObjectStoreError
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
async def test_eight_missing_candidates_rotate_so_ninth_visible_master_is_collected(
    collection_context: CollectionContext,
) -> None:
    checked_at = NOW + timedelta(days=2)
    async with collection_context.database.sessions() as session:
        ready_job = await session.get(GenerationJob, collection_context.job_id)
        ready_asset = await session.get(Asset, collection_context.intents[0].asset_id)
        assert ready_job is not None and ready_asset is not None
        ready_job.state = GenerationState.RUNNING
        ready_asset.updated_at = NOW
        release_version_id = ready_job.release_version_id
        await session.commit()

        missing_job = GenerationJob(
            release_version_id=release_version_id,
            logical_key="missing-progressive-batch",
            parameters={"seed": 100},
            parameters_sha256="a" * 64,
            provider="salad",
            state=GenerationState.RUNNING,
            expected_output_count=8,
        )
        session.add(missing_job)
        await session.commit()
        missing_intents = await create_raw_master_upload_intents(
            session,
            collection_context.store,
            generation_job_id=missing_job.id,
            max_bytes=1_000_000,
        )
        for index, missing_intent in enumerate(missing_intents):
            missing_asset = await session.get(Asset, missing_intent.asset_id)
            assert missing_asset is not None
            missing_asset.updated_at = NOW - timedelta(minutes=10 + index)
        await session.commit()

    _stage(collection_context.store, collection_context.intents[0], _png("cyan"))
    async with collection_context.database.sessions() as session:
        rotated = await collect_next_ready_running_asset(
            session,
            collection_context.store,
            worker_id="progressive-collector",
            max_image_bytes=1_000_000,
            now=checked_at,
        )
        collected = await collect_next_ready_running_asset(
            session,
            collection_context.store,
            worker_id="progressive-collector",
            max_image_bytes=1_000_000,
            now=checked_at + timedelta(microseconds=1),
        )

    assert rotated.finalized is False
    assert rotated.asset_id != collection_context.intents[0].asset_id
    assert collected.asset_id == collection_context.intents[0].asset_id
    assert collected.finalized is True


@pytest.mark.asyncio
async def test_missing_candidate_touch_does_not_overwrite_concurrent_intent_change(
    collection_context: CollectionContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checked_at = NOW + timedelta(days=2)
    replacement_updated_at = NOW + timedelta(days=1)
    intent = collection_context.intents[0]
    replacement_key = f"{intent.staging_key}/replacement"
    async with collection_context.database.sessions() as session:
        job = await session.get(GenerationJob, collection_context.job_id)
        asset = await session.get(Asset, intent.asset_id)
        other_asset = await session.get(Asset, collection_context.intents[1].asset_id)
        assert job is not None and asset is not None and other_asset is not None
        job.state = GenerationState.RUNNING
        asset.updated_at = NOW
        other_asset.state = AssetState.QUARANTINED
        await session.commit()

    async def race_head(key: str):  # type: ignore[no-untyped-def]
        assert key == intent.staging_key
        async with collection_context.database.sessions() as racing_session:
            racing_asset = await racing_session.get(Asset, intent.asset_id)
            assert racing_asset is not None
            racing_asset.staging_object_key = replacement_key
            racing_asset.state = AssetState.VERIFYING
            racing_asset.verification_lease_owner = "concurrent-collector"
            racing_asset.verification_lease_expires_at = checked_at + timedelta(minutes=5)
            racing_asset.updated_at = replacement_updated_at
            await racing_session.commit()
        return None

    monkeypatch.setattr(collection_context.store, "head", race_head)
    async with collection_context.database.sessions() as session:
        result = await collect_next_ready_running_asset(
            session,
            collection_context.store,
            worker_id="progressive-collector",
            max_image_bytes=1_000_000,
            now=checked_at,
        )
        asset = await session.get(Asset, intent.asset_id)

    assert result.asset_id == intent.asset_id
    assert result.finalized is False
    assert asset is not None
    assert asset.state == AssetState.VERIFYING
    assert asset.staging_object_key == replacement_key
    assert asset.updated_at != checked_at


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
async def test_terminal_job_without_stop_marker_is_not_progressively_scanned(
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
async def test_retry_wait_salvages_later_staged_master_across_missing_index_gap(
    collection_context: CollectionContext,
) -> None:
    missing_intent, staged_intent = collection_context.intents
    staged_bytes = _png("blue")
    _stage(collection_context.store, staged_intent, staged_bytes)
    async with collection_context.database.sessions() as session:
        job = await session.get(GenerationJob, collection_context.job_id)
        assert job is not None
        job.state = GenerationState.RETRY_WAIT
        await session.commit()

        result = await collect_next_ready_running_asset(
            session,
            collection_context.store,
            worker_id="progressive-collector",
            max_image_bytes=1_000_000,
            now=NOW,
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

    assert result.asset_id == staged_intent.asset_id
    assert result.finalized is True
    assert assets[0].id == missing_intent.asset_id
    assert assets[0].state == AssetState.UPLOADING
    assert assets[0].staging_object_key == missing_intent.staging_key
    assert assets[1].id == staged_intent.asset_id
    assert assets[1].state == AssetState.AVAILABLE
    assert assets[1].object_key is not None
    assert collection_context.store.objects[assets[1].object_key].body == staged_bytes


@pytest.mark.asyncio
async def test_retry_grant_rotation_salvages_exact_visible_staging_intent_first(
    collection_context: CollectionContext,
) -> None:
    original_intent = collection_context.intents[0]
    original_bytes = _png("green")
    _stage(collection_context.store, original_intent, original_bytes)
    async with collection_context.database.sessions() as session:
        job = await session.get(GenerationJob, collection_context.job_id)
        assert job is not None
        job.state = GenerationState.RETRY_WAIT
        await session.commit()

        rotated = await create_retry_safe_raw_master_upload_intents(
            session,
            collection_context.store,
            generation_job_id=job.id,
            expected_output_count=job.expected_output_count,
            max_bytes=1_000_000,
            actor="retry-input-builder",
        )
        asset = await session.get(Asset, original_intent.asset_id)

    assert asset is not None
    assert asset.state == AssetState.AVAILABLE
    assert asset.object_key is not None
    immutable_master_key = asset.object_key
    assert collection_context.store.objects[immutable_master_key].body == original_bytes
    assert rotated[0].asset_id == original_intent.asset_id
    assert rotated[0].staging_key != original_intent.staging_key
    assert rotated[0].upload_url is not None
    assert asset.object_key == immutable_master_key


@pytest.mark.asyncio
async def test_retry_grant_reuses_exact_intent_when_upload_finishes_after_head_miss(
    collection_context: CollectionContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_intent = collection_context.intents[0]
    late_bytes = _png("purple")
    original_head = collection_context.store.head
    completed_after_miss = False

    async def miss_then_complete(key: str, *, version_id: str | None = None):  # type: ignore[no-untyped-def]
        nonlocal completed_after_miss
        if key == original_intent.staging_key and not completed_after_miss:
            completed_after_miss = True
            _stage(collection_context.store, original_intent, late_bytes)
            return None
        return await original_head(key, version_id=version_id)

    monkeypatch.setattr(collection_context.store, "head", miss_then_complete)
    async with collection_context.database.sessions() as session:
        job = await session.get(GenerationJob, collection_context.job_id)
        assert job is not None
        job.state = GenerationState.RETRY_WAIT
        await session.commit()

        reissued = await create_retry_safe_raw_master_upload_intents(
            session,
            collection_context.store,
            generation_job_id=job.id,
            expected_output_count=job.expected_output_count,
            max_bytes=1_000_000,
            actor="retry-input-builder",
        )
        collected = await collect_next_ready_running_asset(
            session,
            collection_context.store,
            worker_id="progressive-collector",
            max_image_bytes=1_000_000,
            now=NOW,
        )
        asset = await session.get(Asset, original_intent.asset_id)

    assert completed_after_miss is True
    assert reissued[0].staging_key == original_intent.staging_key
    assert reissued[0].upload_attempt_id == original_intent.upload_attempt_id
    assert collected.asset_id == original_intent.asset_id
    assert collected.finalized is True
    assert asset is not None
    assert asset.state == AssetState.AVAILABLE
    assert asset.object_key is not None
    assert collection_context.store.objects[asset.object_key].body == late_bytes


@pytest.mark.asyncio
async def test_progressive_finalize_is_fenced_to_candidate_staging_key(
    collection_context: CollectionContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_intent = collection_context.intents[0]
    _stage(collection_context.store, original_intent, _png("green"))
    original_head = collection_context.store.head
    replacement: UploadIntent | None = None

    async with collection_context.database.sessions() as session:
        job = await session.get(GenerationJob, collection_context.job_id)
        other_asset = await session.get(Asset, collection_context.intents[1].asset_id)
        assert job is not None and other_asset is not None
        job.state = GenerationState.RETRY_WAIT
        other_asset.state = AssetState.QUARANTINED
        await session.commit()

    async def rotate_after_candidate_head(
        key: str,
        *,
        version_id: str | None = None,
    ):  # type: ignore[no-untyped-def]
        nonlocal replacement
        observed = await original_head(key, version_id=version_id)
        if key == original_intent.staging_key and replacement is None:
            async with collection_context.database.sessions() as racing_session:
                replacement = (
                    await create_raw_master_upload_intents(
                        racing_session,
                        collection_context.store,
                        generation_job_id=collection_context.job_id,
                        max_bytes=1_000_000,
                        rotate_incomplete_uploads=True,
                    )
                )[0]
            _stage(collection_context.store, replacement, _png("blue"))
        return observed

    monkeypatch.setattr(collection_context.store, "head", rotate_after_candidate_head)
    async with collection_context.database.sessions() as session:
        result = await collect_next_ready_running_asset(
            session,
            collection_context.store,
            worker_id="progressive-collector",
            max_image_bytes=1_000_000,
            now=NOW,
        )
        asset = await session.get(Asset, original_intent.asset_id)

    assert replacement is not None
    assert result.asset_id == original_intent.asset_id
    assert result.finalized is False
    assert asset is not None
    assert asset.state == AssetState.UPLOADING
    assert asset.staging_object_key == replacement.staging_key
    assert asset.object_key is None
    assert original_intent.staging_key in collection_context.store.objects
    assert replacement.staging_key in collection_context.store.objects


@pytest.mark.asyncio
async def test_retry_grant_defers_while_any_raw_master_is_verifying(
    collection_context: CollectionContext,
) -> None:
    lease_expires_at = datetime.now(UTC) + timedelta(minutes=10)
    async with collection_context.database.sessions() as session:
        assets = list(
            (
                await session.scalars(
                    select(Asset)
                    .where(Asset.generation_job_id == collection_context.job_id)
                    .order_by(Asset.output_index)
                )
            ).all()
        )
        for asset in assets:
            asset.state = AssetState.VERIFYING
            asset.verification_lease_owner = "active-collector"
            asset.verification_lease_expires_at = lease_expires_at
        await session.commit()

        with pytest.raises(RetryUploadSalvageDeferredError, match="still in progress"):
            await create_retry_safe_raw_master_upload_intents(
                session,
                collection_context.store,
                generation_job_id=collection_context.job_id,
                expected_output_count=2,
                max_bytes=1_000_000,
            )
        unchanged = list(
            (
                await session.scalars(
                    select(Asset)
                    .where(Asset.generation_job_id == collection_context.job_id)
                    .order_by(Asset.output_index)
                )
            ).all()
        )

    assert [asset.state for asset in unchanged] == [
        AssetState.VERIFYING,
        AssetState.VERIFYING,
    ]
    assert [asset.staging_object_key for asset in unchanged] == [
        intent.staging_key for intent in collection_context.intents
    ]


@pytest.mark.asyncio
async def test_retry_grant_reclaims_expired_verification_without_rotating_intent(
    collection_context: CollectionContext,
) -> None:
    original_intent = collection_context.intents[0]
    async with collection_context.database.sessions() as session:
        asset = await session.get(Asset, original_intent.asset_id)
        assert asset is not None
        asset.state = AssetState.VERIFYING
        asset.verification_lease_owner = "expired-collector"
        asset.verification_lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()

        reissued = await create_retry_safe_raw_master_upload_intents(
            session,
            collection_context.store,
            generation_job_id=collection_context.job_id,
            expected_output_count=2,
            max_bytes=1_000_000,
            actor="retry-input-builder",
        )
        reclaimed = await session.get(Asset, original_intent.asset_id, populate_existing=True)

    assert reissued[0].staging_key == original_intent.staging_key
    assert reissued[0].upload_attempt_id == original_intent.upload_attempt_id
    assert reclaimed is not None
    assert reclaimed.state == AssetState.UPLOADING
    assert reclaimed.staging_object_key == original_intent.staging_key
    assert reclaimed.verification_lease_owner is None
    assert reclaimed.verification_lease_expires_at is None


@pytest.mark.asyncio
async def test_progressive_miss_then_retry_reclaims_expired_verification(
    collection_context: CollectionContext,
) -> None:
    original_intent = collection_context.intents[0]
    checked_at = datetime.now(UTC)
    async with collection_context.database.sessions() as session:
        job = await session.get(GenerationJob, collection_context.job_id)
        expired_asset = await session.get(Asset, original_intent.asset_id)
        other_asset = await session.get(Asset, collection_context.intents[1].asset_id)
        assert job is not None and expired_asset is not None and other_asset is not None
        job.state = GenerationState.RETRY_WAIT
        expired_asset.state = AssetState.VERIFYING
        expired_asset.verification_lease_owner = "expired-progressive-collector"
        expired_asset.verification_lease_expires_at = checked_at - timedelta(seconds=1)
        other_asset.state = AssetState.QUARANTINED
        await session.commit()

        scanned = await collect_next_ready_running_asset(
            session,
            collection_context.store,
            worker_id="progressive-collector",
            max_image_bytes=1_000_000,
            now=checked_at,
        )
        after_scan = await session.get(Asset, original_intent.asset_id, populate_existing=True)
        assert after_scan is not None
        assert after_scan.state == AssetState.VERIFYING

        reissued = await create_retry_safe_raw_master_upload_intents(
            session,
            collection_context.store,
            generation_job_id=collection_context.job_id,
            expected_output_count=2,
            max_bytes=1_000_000,
            actor="retry-input-builder",
        )
        reclaimed = await session.get(Asset, original_intent.asset_id, populate_existing=True)

    assert scanned.asset_id == original_intent.asset_id
    assert scanned.finalized is False
    assert reissued[0].staging_key == original_intent.staging_key
    assert reissued[0].upload_attempt_id == original_intent.upload_attempt_id
    assert reclaimed is not None
    assert reclaimed.state == AssetState.UPLOADING
    assert reclaimed.verification_lease_owner is None
    assert reclaimed.verification_lease_expires_at is None


@pytest.mark.asyncio
async def test_retry_grant_salvages_visible_upload_from_expired_verification(
    collection_context: CollectionContext,
) -> None:
    original_intent = collection_context.intents[0]
    original_bytes = _png("orange")
    _stage(collection_context.store, original_intent, original_bytes)
    async with collection_context.database.sessions() as session:
        asset = await session.get(Asset, original_intent.asset_id)
        other_asset = await session.get(Asset, collection_context.intents[1].asset_id)
        assert asset is not None and other_asset is not None
        asset.state = AssetState.VERIFYING
        asset.verification_lease_owner = "expired-collector"
        asset.verification_lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        other_asset.state = AssetState.QUARANTINED
        await session.commit()

        reissued = await create_retry_safe_raw_master_upload_intents(
            session,
            collection_context.store,
            generation_job_id=collection_context.job_id,
            expected_output_count=2,
            max_bytes=1_000_000,
            actor="retry-input-builder",
        )
        salvaged = await session.get(Asset, original_intent.asset_id, populate_existing=True)

    assert salvaged is not None
    assert salvaged.state == AssetState.AVAILABLE
    assert salvaged.object_key is not None
    assert collection_context.store.objects[salvaged.object_key].body == original_bytes
    assert reissued[0].staging_key != original_intent.staging_key
    assert reissued[0].upload_attempt_id != original_intent.upload_attempt_id


@pytest.mark.asyncio
async def test_retry_rotation_storage_head_failure_rolls_back_every_exact_key(
    collection_context: CollectionContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def inconclusive_head(_key: str):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        if calls == 1:
            return None
        raise ObjectStoreError("storage unavailable")

    monkeypatch.setattr(collection_context.store, "head", inconclusive_head)
    async with collection_context.database.sessions() as session:
        with pytest.raises(RetryUploadSalvageDeferredError, match="could not prove"):
            await create_retry_safe_raw_master_upload_intents(
                session,
                collection_context.store,
                generation_job_id=collection_context.job_id,
                expected_output_count=2,
                max_bytes=1_000_000,
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

    assert calls == 2
    assert [asset.staging_object_key for asset in assets] == [
        intent.staging_key for intent in collection_context.intents
    ]
    assert [asset.state for asset in assets] == [
        AssetState.UPLOADING,
        AssetState.UPLOADING,
    ]


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
