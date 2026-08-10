from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from uuid import UUID

import pytest
from PIL import Image
from sqlalchemy import func, select

from gen_automation.db.models import (
    Asset,
    AuditEvent,
    GenerationJob,
    OutboxEvent,
    Project,
    Release,
    ReleaseVersion,
)
from gen_automation.db.session import Database
from gen_automation.domain.enums import AssetState
from gen_automation.services.assets import (
    AssetBusyError,
    AssetConflictError,
    AssetQuarantinedError,
    UploadIntent,
    UploadNotReadyError,
    create_raw_master_upload_intents,
    finalize_raw_master,
    presign_asset_download,
)
from gen_automation.storage.base import ObjectNotFoundError, ObjectStoreError, PresignedUpload
from gen_automation.storage.images import verify_image_bytes
from gen_automation.storage.memory import MemoryObjectStore


@dataclass(frozen=True)
class AssetContext:
    database: Database
    release_id: UUID
    job_id: UUID


@pytest.fixture
async def asset_context(tmp_path: Path) -> AsyncIterator[AssetContext]:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'assets.db').as_posix()}")
    await database.create_schema()
    now = datetime.now(UTC)
    async with database.sessions() as session:
        project = Project(slug="asset-tests", name="Asset Tests")
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
            specification_sha256="a" * 64,
            created_by="test",
            created_at=now,
        )
        session.add(version)
        await session.flush()
        job = GenerationJob(
            release_version_id=version.id,
            logical_key="b" * 64,
            parameters={"seed": 1},
            parameters_sha256="c" * 64,
            provider="salad",
            expected_output_count=2,
        )
        session.add(job)
        await session.commit()
        context = AssetContext(
            database=database,
            release_id=release.id,
            job_id=job.id,
        )
    try:
        yield context
    finally:
        await database.dispose()


def png_bytes(color: str = "red") -> bytes:
    output = BytesIO()
    Image.new("RGB", (32, 24), color).save(output, format="PNG")
    return output.getvalue()


def stage_upload(
    store: MemoryObjectStore,
    intent: UploadIntent,
    body: bytes,
) -> None:
    metadata = {
        key.removeprefix("x-amz-meta-"): value
        for key, value in intent.upload_fields.items()
        if key.startswith("x-amz-meta-")
    }
    store.put_for_test(
        intent.staging_key,
        body,
        content_type=intent.upload_fields["Content-Type"],
        metadata=metadata,
    )


async def create_intents(
    context: AssetContext,
    store: MemoryObjectStore,
) -> list[UploadIntent]:
    async with context.database.sessions() as session:
        return await create_raw_master_upload_intents(
            session,
            store,
            generation_job_id=context.job_id,
            max_bytes=1_000_000,
        )


@pytest.mark.asyncio
async def test_upload_intents_are_scoped_and_not_reissued(
    asset_context: AssetContext,
) -> None:
    store = MemoryObjectStore()

    first = await create_intents(asset_context, store)
    second = await create_intents(asset_context, store)

    assert len(first) == 2
    assert [intent.asset_id for intent in first] == [intent.asset_id for intent in second]
    assert all(intent.upload_method == "POST" for intent in first)
    assert all(intent.upload_url is not None for intent in first)
    assert all(intent.upload_url is None for intent in second)
    assert all(
        intent.staging_key.startswith(f"staging/{asset_context.release_id}/{asset_context.job_id}/")
        for intent in first
    )
    assert all(intent.upload_fields["content-length-range"] == "1,1000000" for intent in first)


@pytest.mark.asyncio
async def test_oversized_upload_grant_rolls_back_before_intents_are_committed(
    asset_context: AssetContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryObjectStore()
    original_presign = store.presign_upload

    async def oversized_presign(
        *,
        key: str,
        content_type: str,
        metadata: dict[str, str],
        expires_in: int,
        max_bytes: int,
    ) -> PresignedUpload:
        grant = await original_presign(
            key=key,
            content_type=content_type,
            metadata=metadata,
            expires_in=expires_in,
            max_bytes=max_bytes,
        )
        return PresignedUpload(
            url=grant.url,
            method=grant.method,
            fields={**grant.fields, "x-amz-security-token": "x" * (13 * 1024)},
            headers=grant.headers,
        )

    monkeypatch.setattr(store, "presign_upload", oversized_presign)
    async with asset_context.database.sessions() as session:
        with pytest.raises(AssetConflictError, match="upload grant exceeds"):
            await create_raw_master_upload_intents(
                session,
                store,
                generation_job_id=asset_context.job_id,
                max_bytes=1_000_000,
                max_serialized_grant_bytes=12 * 1024,
            )

    async with asset_context.database.sessions() as session:
        asset_count = int(await session.scalar(select(func.count(Asset.id))) or 0)
    assert asset_count == 0


@pytest.mark.asyncio
async def test_incomplete_upload_grants_can_be_rotated_for_a_new_provider_attempt(
    asset_context: AssetContext,
) -> None:
    store = MemoryObjectStore()
    first = await create_intents(asset_context, store)

    async with asset_context.database.sessions() as session:
        rotated = await create_raw_master_upload_intents(
            session,
            store,
            generation_job_id=asset_context.job_id,
            max_bytes=1_000_000,
            rotate_incomplete_uploads=True,
        )

    assert [intent.asset_id for intent in rotated] == [intent.asset_id for intent in first]
    assert [intent.staging_key for intent in rotated] != [intent.staging_key for intent in first]
    assert all(intent.upload_url is not None for intent in rotated)
    assert all(intent.upload_method == "POST" for intent in rotated)


@pytest.mark.asyncio
async def test_provider_retry_reissues_and_cleans_grant_for_progressively_available_asset(
    asset_context: AssetContext,
) -> None:
    store = MemoryObjectStore()
    first = await create_intents(asset_context, store)
    original = png_bytes("red")
    stage_upload(store, first[0], original)
    async with asset_context.database.sessions() as session:
        finalized = await finalize_raw_master(
            session,
            store,
            asset_id=first[0].asset_id,
            max_bytes=1_000_000,
        )
        rotated = await create_raw_master_upload_intents(
            session,
            store,
            generation_job_id=asset_context.job_id,
            max_bytes=1_000_000,
            rotate_incomplete_uploads=True,
        )

    assert rotated[0].asset_id == first[0].asset_id
    assert rotated[0].state == AssetState.AVAILABLE
    assert rotated[0].staging_key != first[0].staging_key
    assert rotated[0].upload_url is not None

    stage_upload(store, rotated[0], original)
    async with asset_context.database.sessions() as session:
        replay = await finalize_raw_master(
            session,
            store,
            asset_id=rotated[0].asset_id,
            max_bytes=1_000_000,
        )
        asset = await session.get(Asset, rotated[0].asset_id)

    assert replay.replayed is True
    assert replay.object_key == finalized.object_key
    assert rotated[0].staging_key not in store.objects
    assert asset is not None
    assert asset.state == AssetState.AVAILABLE
    assert asset.asset_metadata["staging_cleanup"] == "completed"


@pytest.mark.asyncio
async def test_raw_master_is_promoted_without_changing_bytes(
    asset_context: AssetContext,
) -> None:
    store = MemoryObjectStore()
    intent = (await create_intents(asset_context, store))[0]
    original = png_bytes()
    stage_upload(store, intent, original)

    async with asset_context.database.sessions() as session:
        finalized = await finalize_raw_master(
            session,
            store,
            asset_id=intent.asset_id,
            max_bytes=1_000_000,
        )

        asset = await session.get(Asset, intent.asset_id)
        outbox_count = await session.scalar(select(func.count()).select_from(OutboxEvent))
        audit_count = await session.scalar(select(func.count()).select_from(AuditEvent))

    assert finalized.replayed is False
    assert finalized.sha256 == verify_image_bytes(original).sha256
    assert store.objects[finalized.object_key].body == original
    assert intent.staging_key not in store.objects
    assert asset is not None
    assert asset.state == AssetState.AVAILABLE
    assert asset.asset_metadata["staging_cleanup"] == "completed"
    assert outbox_count == 1
    assert audit_count == 3

    async with asset_context.database.sessions() as session:
        replay = await finalize_raw_master(
            session,
            store,
            asset_id=intent.asset_id,
            max_bytes=1_000_000,
        )
        download = await presign_asset_download(
            session,
            store,
            asset_id=intent.asset_id,
            expires_in=600,
            download_name="master.png",
        )
    assert replay.replayed is True
    assert "version=" in download

    async with asset_context.database.sessions() as session:
        with pytest.raises(AssetConflictError, match="different checksum"):
            await finalize_raw_master(
                session,
                store,
                asset_id=intent.asset_id,
                max_bytes=1_000_000,
                reported_sha256="0" * 64,
            )
        unchanged = await session.get(Asset, intent.asset_id)
    assert unchanged is not None
    assert unchanged.state == AssetState.AVAILABLE
    assert unchanged.sha256 == finalized.sha256


@pytest.mark.asyncio
async def test_missing_upload_is_retryable(asset_context: AssetContext) -> None:
    store = MemoryObjectStore()
    intent = (await create_intents(asset_context, store))[0]

    async with asset_context.database.sessions() as session:
        with pytest.raises(UploadNotReadyError):
            await finalize_raw_master(
                session,
                store,
                asset_id=intent.asset_id,
                max_bytes=1_000_000,
            )
        asset = await session.get(Asset, intent.asset_id)

    assert asset is not None
    assert asset.state == AssetState.UPLOADING
    assert asset.verification_error_code == "staging_not_found"


@pytest.mark.asyncio
async def test_invalid_image_is_quarantined_and_retained(
    asset_context: AssetContext,
) -> None:
    store = MemoryObjectStore()
    intent = (await create_intents(asset_context, store))[0]
    stage_upload(store, intent, b"not an image")

    async with asset_context.database.sessions() as session:
        with pytest.raises(AssetQuarantinedError):
            await finalize_raw_master(
                session,
                store,
                asset_id=intent.asset_id,
                max_bytes=1_000_000,
            )
        asset = await session.get(Asset, intent.asset_id)

    assert asset is not None
    assert asset.state == AssetState.QUARANTINED
    assert intent.staging_key in store.objects


class SwappingObjectStore(MemoryObjectStore):
    replacement: bytes

    async def read_bytes(
        self,
        key: str,
        *,
        max_bytes: int,
        version_id: str | None = None,
        etag: str | None = None,
    ) -> bytes:
        stored = self.objects[key]
        self.put_for_test(
            key,
            self.replacement,
            content_type=stored.content_type,
            metadata=stored.metadata,
        )
        return await super().read_bytes(
            key,
            max_bytes=max_bytes,
            version_id=version_id,
            etag=etag,
        )


@pytest.mark.asyncio
async def test_staging_overwrite_cannot_change_the_verified_version(
    asset_context: AssetContext,
) -> None:
    store = SwappingObjectStore()
    store.replacement = png_bytes("blue")
    intent = (await create_intents(asset_context, store))[0]
    stage_upload(store, intent, png_bytes("red"))

    async with asset_context.database.sessions() as session:
        with pytest.raises(UploadNotReadyError):
            await finalize_raw_master(
                session,
                store,
                asset_id=intent.asset_id,
                max_bytes=1_000_000,
            )

    assert not any(key.startswith("masters/") for key in store.objects)


@pytest.mark.asyncio
async def test_existing_identical_master_is_adopted_after_controller_retry(
    asset_context: AssetContext,
) -> None:
    store = MemoryObjectStore()
    intent = (await create_intents(asset_context, store))[0]
    original = png_bytes()
    stage_upload(store, intent, original)
    verified = verify_image_bytes(original)
    master_key = f"masters/{asset_context.release_id}/{intent.asset_id}/{verified.sha256}.png"
    store.put_for_test(
        master_key,
        original,
        content_type="image/png",
        metadata={
            "asset-id": str(intent.asset_id),
            "generation-job-id": str(asset_context.job_id),
            "output-index": "0",
            "sha256": verified.sha256,
        },
    )

    async with asset_context.database.sessions() as session:
        result = await finalize_raw_master(
            session,
            store,
            asset_id=intent.asset_id,
            max_bytes=1_000_000,
        )

    assert result.object_key == master_key
    assert result.sha256 == verified.sha256


@pytest.mark.asyncio
async def test_conflicting_existing_master_is_never_overwritten(
    asset_context: AssetContext,
) -> None:
    store = MemoryObjectStore()
    intent = (await create_intents(asset_context, store))[0]
    original = png_bytes("blue")
    conflict = png_bytes("white")
    assert len(original) == len(conflict)
    stage_upload(store, intent, original)
    verified = verify_image_bytes(original)
    master_key = f"masters/{asset_context.release_id}/{intent.asset_id}/{verified.sha256}.png"
    store.put_for_test(
        master_key,
        conflict,
        content_type="image/png",
        metadata={
            "asset-id": str(intent.asset_id),
            "generation-job-id": str(asset_context.job_id),
            "output-index": "0",
            "sha256": verified.sha256,
        },
    )

    async with asset_context.database.sessions() as session:
        with pytest.raises(AssetQuarantinedError):
            await finalize_raw_master(
                session,
                store,
                asset_id=intent.asset_id,
                max_bytes=1_000_000,
            )

    assert store.objects[master_key].body == conflict


class CleanupFailingStore(MemoryObjectStore):
    async def delete(self, key: str, *, version_id: str | None = None) -> None:
        del key, version_id
        raise ObjectStoreError("simulated cleanup failure")


@pytest.mark.asyncio
async def test_cleanup_failure_does_not_roll_back_available_master(
    asset_context: AssetContext,
) -> None:
    store = CleanupFailingStore()
    intent = (await create_intents(asset_context, store))[0]
    stage_upload(store, intent, png_bytes())

    async with asset_context.database.sessions() as session:
        result = await finalize_raw_master(
            session,
            store,
            asset_id=intent.asset_id,
            max_bytes=1_000_000,
        )
        asset = await session.get(Asset, intent.asset_id)

    assert result.object_key in store.objects
    assert asset is not None
    assert asset.state == AssetState.AVAILABLE
    assert asset.asset_metadata["staging_cleanup"] == "pending"
    assert intent.staging_key in store.objects


@pytest.mark.asyncio
async def test_active_verification_lease_prevents_duplicate_promotion(
    asset_context: AssetContext,
) -> None:
    store = MemoryObjectStore()
    intent = (await create_intents(asset_context, store))[0]
    stage_upload(store, intent, png_bytes())
    async with asset_context.database.sessions() as session:
        asset = await session.get(Asset, intent.asset_id)
        assert asset is not None
        asset.state = AssetState.VERIFYING
        asset.verification_lease_owner = "another-controller"
        asset.verification_lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)
        await session.commit()

        with pytest.raises(AssetBusyError):
            await finalize_raw_master(
                session,
                store,
                asset_id=intent.asset_id,
                max_bytes=1_000_000,
            )


@pytest.mark.asyncio
async def test_delete_is_version_specific() -> None:
    store = MemoryObjectStore()
    store.put_for_test("staging/object", b"first")
    old_version = store.objects["staging/object"].version_id
    store.put_for_test("staging/object", b"second")

    with pytest.raises(ObjectNotFoundError):
        await store.delete("staging/object", version_id=old_version)

    assert store.objects["staging/object"].body == b"second"
