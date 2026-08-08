# ruff: noqa: F811

import asyncio
import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path, PurePosixPath
from uuid import UUID, uuid4
from zipfile import ZIP_STORED, ZipFile

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from gen_automation.api.routes.mega_deliveries import (
    get_mega_set_delivery,
    get_release_mega_set_deliveries,
)
from gen_automation.db.models import (
    DerivativeOutput,
    FinishedSetArchive,
    FinishedSetArchivePart,
    MegaSetDelivery,
    MegaSetDeliveryItem,
    PublicationIntent,
    PublicationPackage,
    ReleaseSelection,
)
from gen_automation.domain.canonical import canonical_json_bytes
from gen_automation.domain.enums import FinishedSetArchiveState, MegaDeliveryState
from gen_automation.integrations.mega import (
    MegaAmbiguousError,
    MegaCmdClient,
    MegaRemoteNode,
)
from gen_automation.integrations.mega.client import MegaCommandResult
from gen_automation.services.mega_set_delivery import (
    MegaSetDeliveryCycleResult,
    run_mega_set_delivery_cycle,
)
from gen_automation.storage.memory import StoredObject
from tests.image_privacy_assertions import (
    assert_delivery_metadata_absent,
    assert_private_master_metadata_present,
)
from tests.test_derivative_pipeline import ApprovedContext
from tests.test_derivative_pipeline import (
    approved_context as derivative_approved_context,  # noqa: F401
)
from tests.test_derivative_runtime import RUN_AT, TrackingObjectStore, _cycle, _prepare

_MAX_PART_BYTES = 8 * 1024 * 1024
_REMOTE_ROOT = "/sets"
_WORKER_ID = "test:mega-set"


@dataclass(frozen=True, slots=True)
class _ArchiveFixture:
    archive_id: UUID
    manifest_sha256: str
    image_payloads: tuple[bytes, ...]
    derivative_output_ids: tuple[UUID, ...]


class _RecordingMegaClient:
    def __init__(self, *, lose_first_batch_after_one: bool = False) -> None:
        self.remote: dict[str, bytes] = {}
        self.write_counts: Counter[str] = Counter()
        self.upload_files_calls: list[tuple[str, ...]] = []
        self._lose_first_batch_after_one = lose_first_batch_after_one

    async def ensure_folder(self, remote_folder: str) -> None:
        assert remote_folder.startswith(f"{_REMOTE_ROOT}/")

    async def list_files(self, remote_folder: str) -> tuple[str, ...]:
        return tuple(
            sorted(
                remote_path
                for remote_path in self.remote
                if str(PurePosixPath(remote_path).parent) == remote_folder
            )
        )

    async def find_file(self, remote_path: str) -> tuple[MegaRemoteNode, ...]:
        if remote_path not in self.remote:
            return ()
        return (self._node(remote_path),)

    async def upload_file(self, local_file: Path, remote_folder: str) -> None:
        await self._store(local_file, remote_folder)

    async def upload_files(
        self,
        local_files: tuple[Path, ...],
        remote_folder: str,
    ) -> None:
        self.upload_files_calls.append(tuple(path.name for path in local_files))
        for index, local_file in enumerate(local_files):
            await self._store(local_file, remote_folder)
            if self._lose_first_batch_after_one and index == 0:
                self._lose_first_batch_after_one = False
                raise MegaAmbiguousError("simulated partial batch response loss")

    async def download_node(self, node: MegaRemoteNode, local_folder: Path) -> Path:
        destination = local_folder / PurePosixPath(node.remote_path).name
        destination.write_bytes(self.remote[node.remote_path])
        return destination

    async def _store(self, local_file: Path, remote_folder: str) -> None:
        remote_path = f"{remote_folder}/{local_file.name}"
        if remote_path in self.remote:
            raise AssertionError(f"duplicate remote upload: {remote_path}")
        self.remote[remote_path] = await asyncio.to_thread(local_file.read_bytes)
        self.write_counts[remote_path] += 1

    @staticmethod
    def _node(remote_path: str) -> MegaRemoteNode:
        handle = f"H:{hashlib.sha256(remote_path.encode()).hexdigest()[:12]}"
        return MegaRemoteNode(handle=handle, remote_path=remote_path)


@pytest.mark.asyncio
async def test_megacmd_batches_files_in_one_ordered_put_command(tmp_path: Path) -> None:
    profile = _mega_profile(tmp_path)
    first = tmp_path / "001.jpg"
    second = tmp_path / "002.jpg"
    await asyncio.to_thread(first.write_bytes, b"first")
    await asyncio.to_thread(second.write_bytes, b"second")
    commands: list[tuple[str, ...]] = []

    async def runner(
        command: tuple[str, ...],
        profile_home: Path,
        timeout_seconds: float,
    ) -> MegaCommandResult:
        commands.append(command)
        assert profile_home == profile.resolve()
        assert timeout_seconds == 30
        return MegaCommandResult(return_code=0, stdout=b"")

    client = MegaCmdClient(
        profile_home=profile,
        command_timeout_seconds=30,
        runner=runner,
    )
    await client.upload_files((first, second), "/sets/ordered")

    assert commands == [
        (
            "mega-put",
            str(first.resolve()),
            str(second.resolve()),
            "/sets/ordered",
        )
    ]


@pytest.mark.asyncio
async def test_megacmd_list_files_preserves_duplicates_and_filters_descendants(
    tmp_path: Path,
) -> None:
    profile = _mega_profile(tmp_path)
    commands: list[tuple[str, ...]] = []

    async def runner(
        command: tuple[str, ...],
        profile_home: Path,
        timeout_seconds: float,
    ) -> MegaCommandResult:
        commands.append(command)
        assert profile_home == profile.resolve()
        assert timeout_seconds == 30
        return MegaCommandResult(
            return_code=0,
            stdout=(
                b"/sets/ordered/001.jpg\n"
                b"/sets/ordered/001.jpg\n"
                b"/sets/ordered/nested/ignored.jpg\n"
                b"/sets/ordered/002.jpg\n"
            ),
        )

    client = MegaCmdClient(
        profile_home=profile,
        command_timeout_seconds=30,
        runner=runner,
    )
    paths = await client.list_files("/sets/ordered")

    assert commands == [("mega-find", "/sets/ordered", "--type=f")]
    assert paths == (
        "/sets/ordered/001.jpg",
        "/sets/ordered/001.jpg",
        "/sets/ordered/002.jpg",
    )


@pytest.mark.asyncio
async def test_provider_independent_multipart_upload_preserves_bytes_order_and_status_api(
    derivative_approved_context: ApprovedContext,
) -> None:
    approved = derivative_approved_context
    store, archive = await _prepare_archive(approved, part_sizes=(1, 1))
    mega = _RecordingMegaClient()
    read_cursor = len(store.read_requests)
    for asset_id, source in zip(approved.raw_asset_ids, approved.raw_payloads, strict=True):
        assert_private_master_metadata_present(source)
        assert store.objects[f"raw/{asset_id}.png"].body == source
    for payload in archive.image_payloads:
        assert_delivery_metadata_absent(payload)

    created = await _cycle_mega(approved, store, mega)
    first_part = await _cycle_mega(approved, store, mega)
    second_part = await _cycle_mega(approved, store, mega)

    assert created.created_delivery
    assert first_part.uploaded_items == 1
    assert not first_part.completed_delivery
    assert second_part.uploaded_items == 1
    assert second_part.completed_delivery
    assert mega.upload_files_calls == [("001.jpg",), ("002.jpg",)]
    archive_reads = Counter(
        key
        for key, _max_bytes, _version_id, _etag in store.read_requests[read_cursor:]
        if key.startswith("finished-sets/")
    )
    # The first part is read once to freeze/cache the shared manifest and once
    # to stage its image. Later cycles reuse the cached manifest instead of
    # downloading that large part again.
    assert sorted(archive_reads.values()) == [1, 2]

    async with approved.database.sessions() as session:
        delivery = await session.scalar(select(MegaSetDelivery))
        assert delivery is not None
        items = tuple(
            (
                await session.scalars(
                    select(MegaSetDeliveryItem).order_by(MegaSetDeliveryItem.ordinal)
                )
            ).all()
        )
        publication_intents = await session.scalar(select(func.count(PublicationIntent.id)))
        publication_packages = await session.scalar(select(func.count(PublicationPackage.id)))

        direct = await get_mega_set_delivery(
            delivery.id,
            session,
            None,  # type: ignore[arg-type]
        )
        listed = await get_release_mega_set_deliveries(
            approved.release_id,
            session,
            None,  # type: ignore[arg-type]
        )
        with pytest.raises(HTTPException, match="MEGA set delivery was not found"):
            await get_mega_set_delivery(
                uuid4(),
                session,
                None,  # type: ignore[arg-type]
            )

    assert publication_intents == 0
    assert publication_packages == 0
    assert delivery.finished_set_archive_id == archive.archive_id
    assert delivery.remote_folder == "/sets/Derivative release"
    assert delivery.manifest_sha256 == archive.manifest_sha256
    assert delivery.state == MegaDeliveryState.SUCCEEDED
    assert delivery.total_item_count == 2
    assert delivery.uploaded_item_count == 2
    assert delivery.total_byte_size == sum(map(len, archive.image_payloads))
    assert delivery.uploaded_byte_size == delivery.total_byte_size
    assert delivery.completion_marker_node_handle is not None
    assert delivery.verified_at is not None
    assert delivery.completed_at is not None
    assert [item.ordinal for item in items] == [1, 2]
    assert [item.source_derivative_output_id for item in items] == list(
        archive.derivative_output_ids
    )
    assert all(item.state == MegaDeliveryState.SUCCEEDED for item in items)
    assert all(item.remote_node_handle is None for item in items)

    image_paths = [item.remote_path for item in items]
    assert [mega.remote[path] for path in image_paths] == list(archive.image_payloads)
    for path in image_paths:
        assert_delivery_metadata_absent(mega.remote[path])
    assert all(mega.write_counts[path] == 1 for path in image_paths)
    remote_manifest_path = f"{delivery.remote_folder}/set-manifest.json"
    completion_path = f"{delivery.remote_folder}/upload-complete.json"
    remote_manifest = json.loads(mega.remote[remote_manifest_path])
    completion = json.loads(mega.remote[completion_path])
    assert remote_manifest["schema"] == "mega-extracted-set-manifest/v1"
    assert remote_manifest["source_manifest_sha256"] == archive.manifest_sha256
    assert [row["path"] for row in remote_manifest["outputs"]] == [
        "001.jpg",
        "002.jpg",
    ]
    assert completion["schema"] == "mega-extracted-set-completion/v1"
    assert completion["image_count"] == 2
    assert completion["total_byte_size"] == sum(map(len, archive.image_payloads))
    assert completion["source_manifest_sha256"] == archive.manifest_sha256

    assert direct == listed[0]
    assert [item.ordinal for item in direct.items] == [1, 2]
    safe_status = direct.model_dump(mode="json")
    assert "lease_owner" not in safe_status
    assert "last_error_detail" not in safe_status
    assert all("last_error_detail" not in item for item in safe_status["items"])
    for asset_id, source in zip(approved.raw_asset_ids, approved.raw_payloads, strict=True):
        assert store.objects[f"raw/{asset_id}.png"].body == source
        assert_private_master_metadata_present(source)


@pytest.mark.asyncio
async def test_ambiguous_partial_batch_is_adopted_without_duplicate_uploads(
    derivative_approved_context: ApprovedContext,
) -> None:
    approved = derivative_approved_context
    store, archive = await _prepare_archive(approved, part_sizes=(2,))
    mega = _RecordingMegaClient(lose_first_batch_after_one=True)

    created = await _cycle_mega(approved, store, mega)
    ambiguous = await _cycle_mega(approved, store, mega)

    assert created.created_delivery
    assert ambiguous.processed_delivery
    assert not ambiguous.completed_delivery
    assert ambiguous.uploaded_items == 0
    assert mega.upload_files_calls == [("001.jpg", "002.jpg")]

    async with approved.database.sessions() as session:
        delivery = await session.scalar(select(MegaSetDelivery))
        assert delivery is not None
        assert delivery.state == MegaDeliveryState.RETRY_WAIT
        assert delivery.uploaded_item_count == 0
        delivery.available_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()

    recovered = await _cycle_mega(approved, store, mega)

    assert recovered.completed_delivery
    assert recovered.adopted_items == 1
    assert recovered.uploaded_items == 1
    assert mega.upload_files_calls == [
        ("001.jpg", "002.jpg"),
        ("002.jpg",),
    ]

    async with approved.database.sessions() as session:
        delivery = await session.scalar(select(MegaSetDelivery))
        items = tuple(
            (
                await session.scalars(
                    select(MegaSetDeliveryItem).order_by(MegaSetDeliveryItem.ordinal)
                )
            ).all()
        )
    assert delivery is not None
    assert delivery.state == MegaDeliveryState.SUCCEEDED
    assert delivery.uploaded_item_count == 2
    assert delivery.uploaded_byte_size == sum(map(len, archive.image_payloads))
    assert [item.state for item in items] == [
        MegaDeliveryState.SUCCEEDED,
        MegaDeliveryState.SUCCEEDED,
    ]
    assert items[0].remote_node_handle is not None
    assert items[0].verified_at is not None
    assert items[1].remote_node_handle is None
    assert items[1].verified_at is None
    assert [mega.remote[item.remote_path] for item in items] == list(archive.image_payloads)
    assert all(mega.write_counts[item.remote_path] == 1 for item in items)


@pytest.mark.asyncio
async def test_existing_named_set_folder_is_not_mixed_or_overwritten(
    derivative_approved_context: ApprovedContext,
) -> None:
    approved = derivative_approved_context
    store, _archive = await _prepare_archive(approved, part_sizes=(2,))
    mega = _RecordingMegaClient()

    assert (await _cycle_mega(approved, store, mega)).created_delivery
    async with approved.database.sessions() as session:
        delivery = await session.scalar(select(MegaSetDelivery))
        assert delivery is not None
        mega.remote[f"{delivery.remote_folder}/existing-file.txt"] = b"owned elsewhere"

    result = await _cycle_mega(approved, store, mega)

    assert result.processed_delivery
    assert not result.completed_delivery
    assert mega.upload_files_calls == []
    async with approved.database.sessions() as session:
        delivery = await session.scalar(select(MegaSetDelivery))
    assert delivery is not None
    assert delivery.state == MegaDeliveryState.FAILED
    assert delivery.last_error_code == "mega_set_remote_conflict"


async def _prepare_archive(
    approved: ApprovedContext,
    *,
    part_sizes: tuple[int, ...],
) -> tuple[TrackingObjectStore, _ArchiveFixture]:
    prepared = await _prepare(approved)
    await _cycle(prepared, worker_id="mega-source-derivative")
    await _cycle(prepared, worker_id="mega-source-derivative")

    async with approved.database.sessions() as session:
        rows = tuple(
            (
                await session.execute(
                    select(DerivativeOutput, ReleaseSelection)
                    .join(
                        ReleaseSelection,
                        ReleaseSelection.id == DerivativeOutput.release_selection_id,
                    )
                    .where(DerivativeOutput.target == "full")
                    .order_by(ReleaseSelection.source_generation_queue_position)
                )
            ).all()
        )
    assert len(rows) == 2
    assert sum(part_sizes) == len(rows)

    archive_id = uuid4()
    image_payloads: list[bytes] = []
    outputs: list[dict[str, object]] = []
    derivative_output_ids: list[UUID] = []
    for ordinal, (output, selection) in enumerate(rows, start=1):
        stored = prepared.store.objects[output.asset_object_key]
        extension = {
            "image/jpeg": "jpg",
            "image/png": "png",
            "image/webp": "webp",
        }[output.asset_content_type]
        image_payloads.append(stored.body)
        derivative_output_ids.append(output.id)
        outputs.append(
            {
                "ordinal": ordinal,
                "generation_queue_position": selection.source_generation_queue_position,
                "derivative_output_id": str(output.id),
                "path": f"content/{ordinal:03d}.{extension}",
                "sha256": hashlib.sha256(stored.body).hexdigest(),
                "byte_size": len(stored.body),
                "content_type": output.asset_content_type,
            }
        )

    manifest = canonical_json_bytes(
        {
            "schema": "finished-set-manifest/v1",
            "archive_id": str(archive_id),
            "selection_count": len(outputs),
            "ordering": "frozen_generation_queue",
            "ordering_key": ["generation_queue_position"],
            "outputs": outputs,
        }
    )
    manifest_sha256 = hashlib.sha256(manifest).hexdigest()
    now = RUN_AT + timedelta(minutes=10)
    archive = FinishedSetArchive(
        id=archive_id,
        review_task_id=approved.review_task_id,
        release_version_id=approved.release_version_id,
        requested_by_user_id=approved.owner_id,
        state=FinishedSetArchiveState.READY,
        selection_count=len(outputs),
        manifest_sha256=manifest_sha256,
        part_count=len(part_sizes),
        attempts=1,
        max_attempts=5,
        available_at=now,
        lease_owner=None,
        lease_expires_at=None,
        last_error_code=None,
        last_error_detail=None,
        created_at=now,
        updated_at=now,
        started_at=now,
        completed_at=now,
    )
    part_rows: list[FinishedSetArchivePart] = []
    first_ordinal = 1
    for part_number, part_size in enumerate(part_sizes, start=1):
        last_ordinal = first_ordinal + part_size - 1
        part_outputs = outputs[first_ordinal - 1 : last_ordinal]
        part_manifest = canonical_json_bytes(
            {
                "schema": "finished-set-part-manifest/v1",
                "archive_id": str(archive_id),
                "set_manifest_sha256": manifest_sha256,
                "part_number": part_number,
                "part_count": len(part_sizes),
                "first_ordinal": first_ordinal,
                "last_ordinal": last_ordinal,
                "outputs": part_outputs,
            }
        )
        buffer = BytesIO()
        with ZipFile(buffer, mode="w", compression=ZIP_STORED) as zipped:
            zipped.writestr("set-manifest.json", manifest)
            zipped.writestr("part-manifest.json", part_manifest)
            for output in part_outputs:
                part_ordinal = output["ordinal"]
                assert isinstance(part_ordinal, int)
                zipped.writestr(str(output["path"]), image_payloads[part_ordinal - 1])
        body = buffer.getvalue()
        key = f"finished-sets/{archive_id}/part-{part_number:04d}.zip"
        version_id = f"archive-version-{part_number}"
        prepared.store.objects[key] = StoredObject(
            body=body,
            content_type="application/zip",
            metadata={"sha256": hashlib.sha256(body).hexdigest()},
            version_id=version_id,
        )
        part_rows.append(
            FinishedSetArchivePart(
                archive_id=archive_id,
                part_number=part_number,
                part_count=len(part_sizes),
                first_ordinal=first_ordinal,
                last_ordinal=last_ordinal,
                storage_backend=prepared.store.backend,
                storage_bucket=prepared.store.bucket,
                object_key=key,
                object_version_id=version_id,
                sha256=hashlib.sha256(body).hexdigest(),
                manifest_sha256=manifest_sha256,
                byte_size=len(body),
                content_type="application/zip",
                created_at=now,
            )
        )
        first_ordinal = last_ordinal + 1

    async with approved.database.sessions() as session:
        session.add(archive)
        session.add_all(part_rows)
        await session.commit()
    return prepared.store, _ArchiveFixture(
        archive_id=archive_id,
        manifest_sha256=manifest_sha256,
        image_payloads=tuple(image_payloads),
        derivative_output_ids=tuple(derivative_output_ids),
    )


async def _cycle_mega(
    approved: ApprovedContext,
    store: TrackingObjectStore,
    mega: _RecordingMegaClient,
) -> MegaSetDeliveryCycleResult:
    return await run_mega_set_delivery_cycle(
        approved.database.sessions,
        store=store,
        client=mega,
        worker_id=_WORKER_ID,
        remote_root=_REMOTE_ROOT,
        lease_seconds=120,
        retry_base_seconds=1,
        retry_max_seconds=60,
        max_part_bytes=_MAX_PART_BYTES,
        batch_size=100,
    )


def _mega_profile(tmp_path: Path) -> Path:
    profile = tmp_path / "mega-profile"
    (profile / ".megaCmd").mkdir(parents=True)
    profile.chmod(0o700)
    (profile / ".megaCmd").chmod(0o700)
    return profile
