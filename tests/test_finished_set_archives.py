# ruff: noqa: F811

import asyncio
import hashlib
import inspect
import json
from collections.abc import Iterator
from dataclasses import replace
from datetime import timedelta
from io import BytesIO
from typing import Any
from uuid import uuid4
from zipfile import ZipFile

import pytest
from PIL import Image
from sqlalchemy import func, select

from gen_automation.db.models import (
    AdminUser,
    Asset,
    AuditEvent,
    DerivativeJob,
    DerivativeOutput,
    FinishedSetArchive,
    FinishedSetArchivePart,
    GenerationJob,
    MegaSetDelivery,
    PublicationIntent,
    PublicationPackage,
    Release,
    ReleaseSelection,
)
from gen_automation.domain.canonical import canonical_sha256
from gen_automation.domain.enums import (
    AdminRole,
    DerivativeJobState,
    FinishedSetArchiveState,
    GenerationState,
    ReleasePhase,
)
from gen_automation.services import finished_set_archives as archives
from gen_automation.services.derivatives import (
    DerivativeBundle,
    render_platform_derivatives,
)
from gen_automation.services.finished_set_archives import (
    FinishedSetArchiveConflictError,
    FinishedSetArchiveNotFoundError,
    load_finished_set_archive,
    presign_finished_set_archive_part,
    request_finished_set_archive,
    run_finished_set_archive_cycle,
)
from gen_automation.services.mega_set_delivery import ensure_next_mega_set_delivery
from gen_automation.storage.base import ObjectMetadata, ObjectStoreError
from gen_automation.storage.memory import MemoryObjectStore, StoredObject
from tests.image_privacy_assertions import (
    assert_delivery_metadata_absent,
    assert_private_master_metadata_present,
)
from tests.test_derivative_pipeline import ApprovedContext
from tests.test_derivative_pipeline import (
    approved_context as derivative_approved_context,  # noqa: F401
)
from tests.test_derivative_runtime import (
    RUN_AT,
    TrackingObjectStore,
    _cycle,
    _prepare,
)

ARCHIVE_AT = RUN_AT + timedelta(minutes=2)


@pytest.fixture(autouse=True)
def _trusted_test_isolated_renderer(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[list[tuple[bytes, dict[str, Any]]]]:
    calls: list[tuple[bytes, dict[str, Any]]] = []

    async def render(raw_master: bytes, **kwargs: Any) -> DerivativeBundle:
        kwargs.pop("policy", None)
        calls.append((raw_master, dict(kwargs)))
        return render_platform_derivatives(raw_master, **kwargs)

    monkeypatch.setattr(archives, "render_platform_derivatives_isolated", render)
    yield calls


class BlockingSecondArchiveWriteStore(TrackingObjectStore):
    def __init__(self) -> None:
        super().__init__()
        self.archive_write_count = 0
        self.second_archive_write_started = asyncio.Event()

    async def write_bytes_if_absent(
        self,
        *,
        key: str,
        body: bytes,
        content_type: str,
        metadata: dict[str, str],
        max_bytes: int,
    ) -> ObjectMetadata:
        if content_type == "application/zip":
            self.archive_write_count += 1
            if self.archive_write_count == 2:
                self.second_archive_write_started.set()
                await asyncio.Event().wait()
        return await super().write_bytes_if_absent(
            key=key,
            body=body,
            content_type=content_type,
            metadata=metadata,
            max_bytes=max_bytes,
        )


class LostArchiveWriteResponseStore(TrackingObjectStore):
    def __init__(self) -> None:
        super().__init__()
        self.lose_next_archive_write_response = False

    async def write_bytes_if_absent(
        self,
        *,
        key: str,
        body: bytes,
        content_type: str,
        metadata: dict[str, str],
        max_bytes: int,
    ) -> ObjectMetadata:
        result = await super().write_bytes_if_absent(
            key=key,
            body=body,
            content_type=content_type,
            metadata=metadata,
            max_bytes=max_bytes,
        )
        if content_type == "application/zip" and self.lose_next_archive_write_response:
            self.lose_next_archive_write_response = False
            raise ObjectStoreError("simulated archive response loss")
        return result


def test_archive_controller_never_decodes_raw_masters_in_process() -> None:
    source = inspect.getsource(archives)
    assert "Image.open" not in source
    assert "_prepare_public_png" not in source
    assert "render_platform_derivatives_isolated" in source


@pytest.mark.asyncio
async def test_archive_is_available_without_preparing_any_destination(
    derivative_approved_context: ApprovedContext,
    _trusted_test_isolated_renderer: list[tuple[bytes, dict[str, Any]]],
) -> None:
    approved = derivative_approved_context
    prepared = await _prepare(approved)
    await _cycle(prepared, worker_id="archive-derivative")
    await _cycle(prepared, worker_id="archive-derivative")
    for asset_id, source in zip(approved.raw_asset_ids, approved.raw_payloads, strict=True):
        assert_private_master_metadata_present(source)
        assert prepared.store.objects[f"raw/{asset_id}.png"].body == source
    async with approved.database.sessions() as session:
        full_rows = tuple(
            (
                await session.execute(
                    select(DerivativeOutput, ReleaseSelection)
                    .join(
                        ReleaseSelection,
                        ReleaseSelection.id == DerivativeOutput.release_selection_id,
                    )
                    .where(
                        ReleaseSelection.review_task_id == approved.review_task_id,
                        DerivativeOutput.target == "full",
                    )
                    .order_by(ReleaseSelection.source_generation_queue_position)
                )
            ).all()
        )
    assert len(full_rows) == 2
    full_jpegs = tuple(
        prepared.store.objects[output.asset_object_key].body for output, _ in full_rows
    )
    assert all(output.asset_content_type == "image/jpeg" for output, _ in full_rows)

    async with approved.database.sessions() as session:
        assert (
            await load_finished_set_archive(
                session,
                review_task_id=approved.review_task_id,
            )
            is None
        )
        requested = await request_finished_set_archive(
            session,
            review_task_id=approved.review_task_id,
            requested_by_user_id=approved.owner_id,
            now=ARCHIVE_AT,
        )
        replay = await request_finished_set_archive(
            session,
            review_task_id=approved.review_task_id,
            requested_by_user_id=approved.owner_id,
            now=ARCHIVE_AT,
        )
    assert requested.state == FinishedSetArchiveState.PENDING
    assert replay.archive_id == requested.archive_id

    result = await run_finished_set_archive_cycle(
        approved.database.sessions,
        prepared.store,
        worker_id="finished-set-archive",
        lease_seconds=300,
        retry_base_seconds=5,
        retry_max_seconds=60,
        max_archive_bytes=8 * 1024 * 1024,
        now=ARCHIVE_AT + timedelta(seconds=1),
    )
    assert result.completed_archive
    assert result.archive_id == requested.archive_id
    assert len(_trusted_test_isolated_renderer) == 2
    assert all(
        kwargs["targets"] == ("full",)
        and kwargs["watermark_png"] is None
        and kwargs["recipe"].full.encoding.image_format.value == "PNG"
        for _source, kwargs in _trusted_test_isolated_renderer
    )
    public_png_reads = [
        key
        for key, _max_bytes, _version_id, _etag in prepared.store.read_requests
        if key.startswith("public-media/public-png-v1/")
    ]
    assert len(public_png_reads) == 2
    assert len(set(public_png_reads)) == 2

    async with approved.database.sessions() as session:
        snapshot = await load_finished_set_archive(
            session,
            review_task_id=approved.review_task_id,
        )
        publication_intents = await session.scalar(select(func.count(PublicationIntent.id)))
        publication_packages = await session.scalar(select(func.count(PublicationPackage.id)))
        assert snapshot is not None
        assert snapshot.state == FinishedSetArchiveState.READY
        assert snapshot.media_profile == "public-png-v1"
        assert snapshot.selection_count == 2
        assert snapshot.part_count == 1
        assert len(snapshot.parts) == 1
        assert publication_intents == 0
        assert publication_packages == 0

        download = await presign_finished_set_archive_part(
            session,
            prepared.store,
            review_task_id=approved.review_task_id,
            archive_id=snapshot.archive_id,
            actor_user_id=approved.owner_id,
            actor_role=AdminRole.OWNER,
            now=ARCHIVE_AT + timedelta(seconds=2),
        )
        audit_count = await session.scalar(
            select(func.count(AuditEvent.id)).where(
                AuditEvent.action == "review.finished_set_archive_download_authorized"
            )
        )
    assert download.filename == f"finished-ranked-set-{approved.review_task_id}.zip"
    assert download.part_number == 1
    assert download.part_count == 1
    assert "finished-ranked-set" in download.url
    assert audit_count == 1

    async with approved.database.sessions() as session:
        mega_created = await ensure_next_mega_set_delivery(
            session,
            remote_root="/sets",
            now=ARCHIVE_AT + timedelta(seconds=3),
        )
        mega_count = await session.scalar(select(func.count(MegaSetDelivery.id)))
    assert not mega_created
    assert mega_count == 0

    part_row = next(
        part for part in prepared.store.objects.values() if part.content_type == "application/zip"
    )
    with ZipFile(BytesIO(part_row.body)) as archive:
        assert archive.namelist() == [
            "set-manifest.json",
            "part-manifest.json",
            "content/001.png",
            "content/002.png",
        ]
        manifest = json.loads(archive.read("set-manifest.json"))
        assert manifest["schema"] == "finished-set-manifest/v2"
        assert manifest["media_profile"] == "public-png-v1"
        assert manifest["ordering"] == "frozen_generation_queue"
        assert [item["ordinal"] for item in manifest["outputs"]] == [1, 2]
        assert [item["readiness_derivative_output_id"] for item in manifest["outputs"]] == [
            str(output.id) for output, _selection in full_rows
        ]
        assert [item["source_asset_id"] for item in manifest["outputs"]] == [
            str(selection.asset_id) for _output, selection in full_rows
        ]
        assert [item["content_type"] for item in manifest["outputs"]] == [
            "image/png",
            "image/png",
        ]
        assert [(item["width"], item["height"]) for item in manifest["outputs"]] == [
            (64, 64),
            (64, 64),
        ]
        packaged = tuple(archive.read(path) for path in ("content/001.png", "content/002.png"))
        assert packaged != approved.raw_payloads
        assert all(payload not in full_jpegs for payload in packaged)
        cached_by_sha = {
            hashlib.sha256(stored.body).hexdigest(): stored.body
            for key, stored in prepared.store.objects.items()
            if key.startswith("public-media/public-png-v1/")
        }
        assert len(cached_by_sha) == 2
        for row, payload, source in zip(
            manifest["outputs"], packaged, approved.raw_payloads, strict=True
        ):
            assert cached_by_sha[row["sha256"]] == payload
            assert_delivery_metadata_absent(payload)
            with (
                Image.open(BytesIO(payload)) as public_image,
                Image.open(BytesIO(source)) as source_image,
            ):
                assert public_image.size == source_image.size == (64, 64)
                assert (
                    public_image.convert("RGB").tobytes() == source_image.convert("RGB").tobytes()
                )
    for asset_id, source in zip(approved.raw_asset_ids, approved.raw_payloads, strict=True):
        assert prepared.store.objects[f"raw/{asset_id}.png"].body == source
        assert_private_master_metadata_present(source)


@pytest.mark.asyncio
async def test_archive_adopts_a_completed_write_after_response_loss(
    derivative_approved_context: ApprovedContext,
    _trusted_test_isolated_renderer: list[tuple[bytes, dict[str, Any]]],
) -> None:
    approved = derivative_approved_context
    store = LostArchiveWriteResponseStore()
    prepared = await _prepare(approved, store=store)
    await _cycle(prepared, worker_id="archive-retry-derivative")
    await _cycle(prepared, worker_id="archive-retry-derivative")
    async with approved.database.sessions() as session:
        await request_finished_set_archive(
            session,
            review_task_id=approved.review_task_id,
            requested_by_user_id=approved.owner_id,
            now=ARCHIVE_AT,
        )
    store.lose_next_archive_write_response = True

    first = await run_finished_set_archive_cycle(
        approved.database.sessions,
        store,
        worker_id="archive-retry",
        lease_seconds=300,
        retry_base_seconds=5,
        retry_max_seconds=60,
        max_archive_bytes=8 * 1024 * 1024,
        now=ARCHIVE_AT,
    )
    assert first.processed_archive
    assert not first.completed_archive
    assert first.state == FinishedSetArchiveState.RETRY_WAIT
    assert first.error_code == "archive_storage_retryable"
    assert len(_trusted_test_isolated_renderer) == 2
    assert sum(key.startswith("public-media/public-png-v1/") for key in store.objects) == 2
    store.read_requests.clear()

    second = await run_finished_set_archive_cycle(
        approved.database.sessions,
        store,
        worker_id="archive-retry",
        lease_seconds=300,
        retry_base_seconds=5,
        retry_max_seconds=60,
        max_archive_bytes=8 * 1024 * 1024,
        now=ARCHIVE_AT + timedelta(seconds=6),
    )
    assert second.completed_archive
    assert second.archive_id == first.archive_id
    assert len(_trusted_test_isolated_renderer) == 2
    assert not any(key.startswith("raw/") for key, *_rest in store.read_requests)
    public_png_reads = [
        key
        for key, _max_bytes, _version_id, _etag in store.read_requests
        if key.startswith("public-media/public-png-v1/")
    ]
    assert len(public_png_reads) == 2
    assert len(set(public_png_reads)) == 2

    async with approved.database.sessions() as session:
        snapshot = await load_finished_set_archive(
            session,
            review_task_id=approved.review_task_id,
        )
        part_count = await session.scalar(select(func.count(FinishedSetArchivePart.id)))
    assert snapshot is not None
    assert snapshot.state == FinishedSetArchiveState.READY
    assert snapshot.attempts == 2
    assert part_count == 1
    assert sum(item.content_type == "application/zip" for item in store.objects.values()) == 1


@pytest.mark.asyncio
async def test_presign_fails_closed_for_actor_binding_parts_and_storage_tamper(
    derivative_approved_context: ApprovedContext,
) -> None:
    approved = derivative_approved_context
    prepared = await _prepare(approved)
    await _cycle(prepared, worker_id="archive-hardening-derivative")
    await _cycle(prepared, worker_id="archive-hardening-derivative")
    async with approved.database.sessions() as session:
        await request_finished_set_archive(
            session,
            review_task_id=approved.review_task_id,
            requested_by_user_id=approved.owner_id,
            now=ARCHIVE_AT,
        )
    result = await run_finished_set_archive_cycle(
        approved.database.sessions,
        prepared.store,
        worker_id="archive-hardening",
        lease_seconds=300,
        retry_base_seconds=5,
        retry_max_seconds=60,
        max_archive_bytes=8 * 1024 * 1024,
        now=ARCHIVE_AT,
    )
    assert result.archive_id is not None

    kwargs = {
        "review_task_id": approved.review_task_id,
        "archive_id": result.archive_id,
        "actor_user_id": approved.owner_id,
        "actor_role": AdminRole.OWNER,
        "now": ARCHIVE_AT + timedelta(seconds=1),
    }
    async with approved.database.sessions() as session:
        release = await session.get(Release, approved.release_id)
        assert release is not None
        release.phase = ReleasePhase.PUBLISHING
        await session.commit()
        publishing_download = await presign_finished_set_archive_part(
            session,
            prepared.store,
            **kwargs,
        )
        assert publishing_download.archive_id == result.archive_id
        release.phase = ReleasePhase.PUBLISHED
        await session.commit()
        published_download = await presign_finished_set_archive_part(
            session,
            prepared.store,
            **kwargs,
        )
        assert published_download.archive_id == result.archive_id

        with pytest.raises(FinishedSetArchiveNotFoundError):
            await presign_finished_set_archive_part(
                session,
                prepared.store,
                **{**kwargs, "archive_id": uuid4()},
            )
        with pytest.raises(FinishedSetArchiveNotFoundError):
            await presign_finished_set_archive_part(
                session,
                prepared.store,
                **{**kwargs, "review_task_id": uuid4()},
            )
        with pytest.raises(FinishedSetArchiveConflictError, match="active owner"):
            await presign_finished_set_archive_part(
                session,
                prepared.store,
                **{**kwargs, "actor_role": AdminRole.REVIEWER},
            )

        owner = await session.get(AdminUser, approved.owner_id)
        assert owner is not None
        owner.is_active = False
        await session.commit()
        with pytest.raises(FinishedSetArchiveConflictError, match="active owner"):
            await presign_finished_set_archive_part(
                session,
                prepared.store,
                **kwargs,
            )
        owner.is_active = True
        await session.commit()

        archive_row = await session.get(FinishedSetArchive, result.archive_id)
        assert archive_row is not None
        archive_row.part_count = 2
        await session.commit()
        with pytest.raises(FinishedSetArchiveConflictError, match="parts are incomplete"):
            await presign_finished_set_archive_part(
                session,
                prepared.store,
                **kwargs,
            )
        archive_row.part_count = 1
        await session.commit()

        part = await session.scalar(
            select(FinishedSetArchivePart).where(
                FinishedSetArchivePart.archive_id == result.archive_id
            )
        )
        assert part is not None

    stored = prepared.store.objects[part.object_key]
    prepared.store.objects[part.object_key] = replace(
        stored,
        version_id="tampered-version",
    )
    async with approved.database.sessions() as session:
        with pytest.raises(FinishedSetArchiveConflictError, match="storage snapshot"):
            await presign_finished_set_archive_part(
                session,
                prepared.store,
                **kwargs,
            )
    prepared.store.objects[part.object_key] = replace(
        stored,
        metadata={**stored.metadata, "manifest-sha256": "0" * 64},
    )
    async with approved.database.sessions() as session:
        with pytest.raises(FinishedSetArchiveConflictError, match="storage snapshot"):
            await presign_finished_set_archive_part(
                session,
                prepared.store,
                **kwargs,
            )


@pytest.mark.asyncio
async def test_cancelled_cycle_resumes_from_durable_part_checkpoint(
    derivative_approved_context: ApprovedContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    approved = derivative_approved_context
    store = BlockingSecondArchiveWriteStore()
    prepared = await _prepare(approved, store=store)
    await _cycle(prepared, worker_id="archive-checkpoint-derivative")
    await _cycle(prepared, worker_id="archive-checkpoint-derivative")
    async with approved.database.sessions() as session:
        await request_finished_set_archive(
            session,
            review_task_id=approved.review_task_id,
            requested_by_user_id=approved.owner_id,
            now=ARCHIVE_AT,
        )
    monkeypatch.setattr(archives, "_MAX_IMAGES_PER_PART", 1)

    task = asyncio.create_task(
        run_finished_set_archive_cycle(
            approved.database.sessions,
            store,
            worker_id="archive-checkpoint",
            lease_seconds=300,
            retry_base_seconds=5,
            retry_max_seconds=60,
            max_archive_bytes=8 * 1024 * 1024,
            now=ARCHIVE_AT,
        )
    )
    await asyncio.wait_for(store.second_archive_write_started.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    async with approved.database.sessions() as session:
        interrupted = await load_finished_set_archive(
            session,
            review_task_id=approved.review_task_id,
        )
    assert interrupted is not None
    assert interrupted.state == FinishedSetArchiveState.RETRY_WAIT
    assert interrupted.part_count == 2
    assert [part.part_number for part in interrupted.parts] == [1]
    first_part_sha256 = interrupted.parts[0].sha256

    resumed = await run_finished_set_archive_cycle(
        approved.database.sessions,
        store,
        worker_id="archive-checkpoint",
        lease_seconds=300,
        retry_base_seconds=5,
        retry_max_seconds=60,
        max_archive_bytes=8 * 1024 * 1024,
        now=ARCHIVE_AT + timedelta(seconds=6),
    )
    assert resumed.completed_archive
    async with approved.database.sessions() as session:
        completed = await load_finished_set_archive(
            session,
            review_task_id=approved.review_task_id,
        )
    assert completed is not None
    assert completed.state == FinishedSetArchiveState.READY
    assert completed.attempts == 2
    assert [part.part_number for part in completed.parts] == [1, 2]
    assert completed.parts[0].sha256 == first_part_sha256


@pytest.mark.asyncio
async def test_planner_full_outputs_archive_while_x_teaser_job_failed(
    derivative_approved_context: ApprovedContext,
) -> None:
    approved = derivative_approved_context
    selected_asset_id = approved.raw_asset_ids[0]
    prepared = await _prepare(
        approved,
        with_watermark=True,
        x_selected_asset_ids=(selected_asset_id,),
        max_attempts=1,
    )
    assert prepared.plan.total_jobs == 2
    assert prepared.watermark_key is not None
    watermark = prepared.store.objects[prepared.watermark_key]
    corrupted_watermark = bytearray(watermark.body)
    corrupted_watermark[-12] ^= 1
    prepared.store.objects[prepared.watermark_key] = StoredObject(
        body=bytes(corrupted_watermark),
        content_type=watermark.content_type,
        metadata=watermark.metadata,
        version_id=watermark.version_id,
    )
    first_full = await _cycle(prepared, worker_id="archive-full-ready-derivative")
    second_full = await _cycle(prepared, worker_id="archive-full-ready-derivative")
    assert first_full.execution is not None
    assert second_full.execution is not None
    assert first_full.execution.state == DerivativeJobState.SUCCEEDED
    assert second_full.execution.state == DerivativeJobState.SUCCEEDED

    failed_x = await _cycle(prepared, worker_id="archive-failed-x-derivative")
    assert failed_x.execution is not None
    assert failed_x.execution.state == DerivativeJobState.FAILED

    async with approved.database.sessions() as session:
        release = await session.get(Release, approved.release_id)
        jobs = tuple((await session.scalars(select(DerivativeJob))).all())
        assert release is not None
        assert release.phase == ReleasePhase.READY_TO_PUBLISH
        assert len(jobs) == 3
        assert {(tuple(job.request_payload["output_targets"]), job.state) for job in jobs} == {
            (("full",), DerivativeJobState.SUCCEEDED),
            (("x_teaser",), DerivativeJobState.FAILED),
        }
        await request_finished_set_archive(
            session,
            review_task_id=approved.review_task_id,
            requested_by_user_id=approved.owner_id,
            now=ARCHIVE_AT,
        )

    result = await run_finished_set_archive_cycle(
        approved.database.sessions,
        prepared.store,
        worker_id="archive-full-ready",
        lease_seconds=300,
        retry_base_seconds=5,
        retry_max_seconds=60,
        max_archive_bytes=8 * 1024 * 1024,
        now=ARCHIVE_AT + timedelta(seconds=1),
    )
    assert result.completed_archive


@pytest.mark.asyncio
async def test_archive_uses_frozen_generation_order_after_live_lineage_changes(
    derivative_approved_context: ApprovedContext,
) -> None:
    approved = derivative_approved_context
    prepared = await _prepare(approved)
    await _cycle(prepared, worker_id="archive-generation-order-derivative")
    await _cycle(prepared, worker_id="archive-generation-order-derivative")

    async with approved.database.sessions() as session:
        source_assets = tuple(
            (
                await session.scalars(
                    select(Asset)
                    .where(Asset.id.in_(approved.raw_asset_ids))
                    .order_by(Asset.output_index)
                )
            ).all()
        )
        original_job = await session.get(GenerationJob, source_assets[0].generation_job_id)
        assert len(source_assets) == 2 and original_job is not None
        original_job.parameters = {"ordinal": 1, "batch": {"index": 1}}
        original_job.parameters_sha256 = canonical_sha256(original_job.parameters)
        earlier_job = GenerationJob(
            release_version_id=approved.release_version_id,
            logical_key="c" * 64,
            parameters={"ordinal": 0, "batch": {"index": 0}},
            parameters_sha256=canonical_sha256({"ordinal": 0, "batch": {"index": 0}}),
            provider="salad",
            state=GenerationState.SUCCEEDED,
            priority=100,
            expected_output_count=1,
            attempt_count=1,
            max_attempts=3,
            lock_version=1,
            lease_owner=None,
            lease_expires_at=None,
            retry_at=None,
        )
        session.add(earlier_job)
        await session.flush()
        source_assets[1].generation_job_id = earlier_job.id
        source_assets[1].output_index = 0
        await session.commit()
        await request_finished_set_archive(
            session,
            review_task_id=approved.review_task_id,
            requested_by_user_id=approved.owner_id,
            now=ARCHIVE_AT,
        )

    result = await run_finished_set_archive_cycle(
        approved.database.sessions,
        prepared.store,
        worker_id="archive-generation-order",
        lease_seconds=300,
        retry_base_seconds=5,
        retry_max_seconds=60,
        max_archive_bytes=8 * 1024 * 1024,
        now=ARCHIVE_AT,
    )
    assert result.completed_archive
    zip_object = next(
        item for item in prepared.store.objects.values() if item.content_type == "application/zip"
    )
    with ZipFile(BytesIO(zip_object.body)) as archive:
        manifest = json.loads(archive.read("set-manifest.json"))
    assert manifest["ordering"] == "frozen_generation_queue"
    assert [item["review_display_order"] for item in manifest["outputs"]] == [1, 2]
    assert [item["generation_queue_position"] for item in manifest["outputs"]] == [1, 2]
    assert {item["generation_job_id"] for item in manifest["outputs"]} == {str(original_job.id)}
    assert [item["source_output_index"] for item in manifest["outputs"]] == [0, 1]
    assert [item["ordinal"] for item in manifest["outputs"]] == [1, 2]


@pytest.mark.asyncio
async def test_multipart_archive_preserves_order_and_one_shared_manifest() -> None:
    store = MemoryObjectStore(bucket="multipart")
    archive_id = uuid4()
    review_task_id = uuid4()
    release_version_id = uuid4()
    outputs: list[archives._OutputRecord] = []
    image_buffer = BytesIO()
    with Image.new("RGB", (1, 1), color=(12, 34, 56)) as image:
        image.save(image_buffer, format="PNG")
    body = image_buffer.getvalue()
    for ordinal in range(1, 102):
        key = f"full/{ordinal:03d}.png"
        version_id = f"version-{ordinal:03d}"
        source_asset_id = uuid4()
        source_object_version_id = f"source-version-{ordinal:03d}"
        source_sha256 = hashlib.sha256(body).hexdigest()
        lineage_sha256 = f"{ordinal:064x}"
        store.objects[key] = StoredObject(
            body=body,
            content_type="image/png",
            metadata={
                **archives._public_png_static_metadata(
                    source_asset_id=source_asset_id,
                    source_object_version_id=source_object_version_id,
                    source_sha256=source_sha256,
                    source_byte_size=len(body),
                ),
                "sha256": source_sha256,
                "byte-size": str(len(body)),
                "width": "1",
                "height": "1",
                "lineage-sha256": lineage_sha256,
            },
            version_id=version_id,
        )
        outputs.append(
            archives._OutputRecord(
                ordinal=ordinal,
                generation_ordinal=0,
                generation_job_id=uuid4(),
                generation_queue_position=ordinal,
                source_output_index=ordinal - 1,
                review_display_order=ordinal,
                ranking_rank=ordinal,
                selection_id=uuid4(),
                source_asset_id=source_asset_id,
                source_object_version_id=source_object_version_id,
                source_sha256=source_sha256,
                source_byte_size=len(body),
                output_id=uuid4(),
                object_key=key,
                object_version_id=version_id,
                sha256=hashlib.sha256(body).hexdigest(),
                content_type="image/png",
                image_format="PNG",
                width=1,
                height=1,
                byte_size=len(body),
                path=f"content/{ordinal:03d}.png",
                public_recipe_sha256=archives._PUBLIC_PNG_RECIPE_SHA256,
                public_lineage_sha256=lineage_sha256,
                public_renderer_version=archives.DERIVATIVE_RENDERER_VERSION,
                public_pillow_version=archives.PILLOW_VERSION,
            )
        )
    shared_manifest = json.dumps(
        {"schema": "finished-set-manifest/v1", "count": len(outputs)},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    plan = archives._ArchivePlan(
        archive_id=archive_id,
        review_task_id=review_task_id,
        release_version_id=release_version_id,
        selection_count=len(outputs),
        outputs=tuple(outputs),
        manifest=shared_manifest,
        manifest_sha256=hashlib.sha256(shared_manifest).hexdigest(),
    )
    chunks = archives._partition_outputs(plan, max_archive_bytes=8 * 1024 * 1024)
    assert [len(chunk) for chunk in chunks] == [100, 1]

    built = [
        await archives._build_part(
            store,
            plan=plan,
            outputs=chunk,
            part_number=part_number,
            part_count=len(chunks),
            max_archive_bytes=8 * 1024 * 1024,
        )
        for part_number, chunk in enumerate(chunks, start=1)
    ]
    observed_ordinals: list[int] = []
    for expected_number, part in enumerate(built, start=1):
        with ZipFile(BytesIO(part.body)) as archive:
            assert archive.read("set-manifest.json") == shared_manifest
            part_manifest = json.loads(archive.read("part-manifest.json"))
            assert part_manifest["set_manifest_sha256"] == plan.manifest_sha256
            assert part_manifest["part_number"] == expected_number
            assert part_manifest["part_count"] == 2
            observed_ordinals.extend(item["ordinal"] for item in part_manifest["outputs"])
    assert observed_ordinals == list(range(1, 102))
