# ruff: noqa: F811

import asyncio
import hashlib
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4
from zipfile import ZIP_STORED, ZipFile

import pytest
from fastapi import HTTPException
from PIL import Image
from sqlalchemy import func, select

from gen_automation.api.routes.mega_deliveries import (
    _read_set,
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
    Release,
    ReleaseSelection,
)
from gen_automation.domain.canonical import canonical_json_bytes
from gen_automation.domain.enums import FinishedSetArchiveState, MegaDeliveryState
from gen_automation.integrations.mega import (
    MegaAmbiguousError,
    MegaCmdClient,
    MegaRemoteConflictError,
    MegaRemoteNode,
    MegaRetryableError,
)
from gen_automation.integrations.mega.client import MegaCommandResult
from gen_automation.services import finished_set_archives as archives
from gen_automation.services.derivatives import (
    DerivativeBundle,
    render_platform_derivatives,
)
from gen_automation.services.finished_set_archives import (
    load_finished_set_archive,
    request_finished_set_archive,
    run_finished_set_archive_cycle,
)
from gen_automation.services.mega_set_delivery import (
    ClaimedMegaSetDelivery,
    MegaSetDeliveryContractError,
    MegaSetDeliveryCycleResult,
    _next_retry_delay_seconds,
    ensure_next_mega_set_delivery,
    request_mega_set_delivery,
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


@pytest.fixture(autouse=True)
def _trusted_test_isolated_renderer(monkeypatch: pytest.MonkeyPatch) -> None:
    async def render(raw_master: bytes, **kwargs: Any) -> DerivativeBundle:
        kwargs.pop("policy", None)
        return render_platform_derivatives(raw_master, **kwargs)

    monkeypatch.setattr(archives, "render_platform_derivatives_isolated", render)


@dataclass(frozen=True, slots=True)
class _ArchiveFixture:
    archive_id: UUID
    manifest_sha256: str
    image_payloads: tuple[bytes, ...]
    derivative_output_ids: tuple[UUID, ...]
    source_asset_ids: tuple[UUID, ...]


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


def test_set_retry_backoff_counts_consecutive_no_progress_failures() -> None:
    claim = ClaimedMegaSetDelivery(
        delivery_id=uuid4(),
        archive_id=uuid4(),
        manifest_sha256="a" * 64,
        total_item_count=199,
        remote_root="/Future",
        remote_folder="/Future/Akali (NSFW) (PNG)",
        attempt=14,
        uploaded_item_count_at_claim=100,
        prior_retry_delay_seconds=20,
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )

    assert (
        _next_retry_delay_seconds(
            claim,
            uploaded_item_count=100,
            retry_base_seconds=5,
            retry_max_seconds=60,
        )
        == 40
    )
    assert (
        _next_retry_delay_seconds(
            claim,
            uploaded_item_count=101,
            retry_base_seconds=5,
            retry_max_seconds=60,
        )
        == 5
    )


def test_set_status_api_distinguishes_retry_wait_from_retired_legacy() -> None:
    retry_at = datetime(2026, 8, 9, 12, 30, tzinfo=UTC)
    common = {
        "id": uuid4(),
        "finished_set_archive_id": uuid4(),
        "remote_root": "/Future",
        "remote_folder": "/Future/Akali (NSFW) (PNG)",
        "manifest_sha256": "a" * 64,
        "total_item_count": 199,
        "uploaded_item_count": 100,
        "total_byte_size": None,
        "uploaded_byte_size": 0,
        "attempts": 14,
        "available_at": retry_at,
        "completion_marker_node_handle": None,
        "planned_at": None,
        "started_at": retry_at - timedelta(minutes=10),
        "verified_at": None,
        "completed_at": None,
        "created_at": retry_at - timedelta(hours=1),
        "updated_at": retry_at - timedelta(seconds=30),
        "items": (),
    }
    retry = _read_set(
        SimpleNamespace(
            **common,
            state=MegaDeliveryState.RETRY_WAIT,
            last_error_code="mega_set_transport_retryable",
        )
    )
    retired = _read_set(
        SimpleNamespace(
            **{
                **common,
                "state": MegaDeliveryState.FAILED,
                "completed_at": retry_at,
                "last_error_code": "mega_set_legacy_media_retired",
            }
        )
    )

    assert retry.next_retry_at == retry_at
    assert not retry.retired
    assert retired.next_retry_at is None
    assert retired.retired


@pytest.mark.asyncio
async def test_megacmd_existing_folder_is_adopted_without_mkdir(tmp_path: Path) -> None:
    profile = _mega_profile(tmp_path)
    commands: list[tuple[str, ...]] = []

    async def runner(
        command: tuple[str, ...],
        profile_home: Path,
        timeout_seconds: float,
    ) -> MegaCommandResult:
        commands.append(command)
        return MegaCommandResult(return_code=0, stdout=b"/Future/Akali (NSFW)\n")

    client = MegaCmdClient(profile_home=profile, command_timeout_seconds=30, runner=runner)
    await client.ensure_folder("/Future/Akali (NSFW)")

    assert commands == [
        (
            "mega-find",
            "/Future",
            "--pattern=Akali (NSFW)",
            "--type=d",
        )
    ]


@pytest.mark.asyncio
async def test_megacmd_missing_folder_is_created_once(tmp_path: Path) -> None:
    profile = _mega_profile(tmp_path)
    commands: list[tuple[str, ...]] = []

    async def runner(
        command: tuple[str, ...],
        profile_home: Path,
        timeout_seconds: float,
    ) -> MegaCommandResult:
        commands.append(command)
        if len(commands) == 3:
            return MegaCommandResult(return_code=0, stdout=b"/sets/fresh\n")
        return MegaCommandResult(return_code=0, stdout=b"")

    client = MegaCmdClient(profile_home=profile, command_timeout_seconds=30, runner=runner)
    await client.ensure_folder("/sets/fresh")

    assert commands == [
        (
            "mega-find",
            "/sets",
            "--pattern=fresh",
            "--type=d",
        ),
        ("mega-mkdir", "/sets/fresh"),
        (
            "mega-find",
            "/sets",
            "--pattern=fresh",
            "--type=d",
        ),
    ]


@pytest.mark.asyncio
async def test_megacmd_folder_read_failure_never_attempts_mkdir(tmp_path: Path) -> None:
    profile = _mega_profile(tmp_path)
    commands: list[tuple[str, ...]] = []

    async def runner(
        command: tuple[str, ...],
        profile_home: Path,
        timeout_seconds: float,
    ) -> MegaCommandResult:
        commands.append(command)
        return MegaCommandResult(return_code=9, stdout=b"credential-or-network-detail")

    client = MegaCmdClient(profile_home=profile, command_timeout_seconds=30, runner=runner)
    with pytest.raises(MegaRetryableError):
        await client.ensure_folder("/sets/closed")

    assert len(commands) == 1
    assert commands[0][0] == "mega-find"


@pytest.mark.asyncio
async def test_megacmd_ambiguous_mkdir_is_adopted_only_after_exact_read(
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
        if len(commands) == 1:
            return MegaCommandResult(return_code=0, stdout=b"")
        if command[0] == "mega-mkdir":
            raise TimeoutError
        return MegaCommandResult(return_code=0, stdout=b"/sets/reconciled\n")

    client = MegaCmdClient(profile_home=profile, command_timeout_seconds=30, runner=runner)
    await client.ensure_folder("/sets/reconciled")

    assert [command[0] for command in commands] == ["mega-find", "mega-mkdir", "mega-find"]


@pytest.mark.asyncio
async def test_megacmd_same_named_nested_folder_is_not_adopted(tmp_path: Path) -> None:
    profile = _mega_profile(tmp_path)
    commands: list[tuple[str, ...]] = []

    async def runner(
        command: tuple[str, ...],
        profile_home: Path,
        timeout_seconds: float,
    ) -> MegaCommandResult:
        commands.append(command)
        if command[0] == "mega-find":
            if len(commands) == 3:
                return MegaCommandResult(return_code=0, stdout=b"/sets/fresh\n")
            return MegaCommandResult(
                return_code=0,
                stdout=b"/sets/archive/fresh\n",
            )
        return MegaCommandResult(return_code=0, stdout=b"")

    client = MegaCmdClient(profile_home=profile, command_timeout_seconds=30, runner=runner)
    await client.ensure_folder("/sets/fresh")

    assert [command[0] for command in commands] == ["mega-find", "mega-mkdir", "mega-find"]


@pytest.mark.asyncio
async def test_megacmd_successful_mkdir_rejects_racing_duplicate_folders(
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
        if len(commands) < 3:
            return MegaCommandResult(return_code=0, stdout=b"")
        return MegaCommandResult(
            return_code=0,
            stdout=b"/sets/raced\n/sets/raced\n",
        )

    client = MegaCmdClient(profile_home=profile, command_timeout_seconds=30, runner=runner)
    with pytest.raises(MegaRemoteConflictError):
        await client.ensure_folder("/sets/raced")

    assert [command[0] for command in commands] == ["mega-find", "mega-mkdir", "mega-find"]


@pytest.mark.asyncio
async def test_megacmd_duplicate_exact_folders_are_terminal_conflict(tmp_path: Path) -> None:
    profile = _mega_profile(tmp_path)
    commands: list[tuple[str, ...]] = []

    async def runner(
        command: tuple[str, ...],
        profile_home: Path,
        timeout_seconds: float,
    ) -> MegaCommandResult:
        commands.append(command)
        return MegaCommandResult(
            return_code=0,
            stdout=b"/sets/duplicate\n/sets/duplicate\n",
        )

    client = MegaCmdClient(profile_home=profile, command_timeout_seconds=30, runner=runner)
    with pytest.raises(MegaRemoteConflictError):
        await client.ensure_folder("/sets/duplicate")

    assert [command[0] for command in commands] == ["mega-find"]


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
async def test_unrequested_ready_archive_is_ignored_by_mega_discovery(
    derivative_approved_context: ApprovedContext,
) -> None:
    approved = derivative_approved_context
    _store, archive = await _prepare_archive(
        approved,
        part_sizes=(2,),
        mega_requested=False,
    )

    async with approved.database.sessions() as session:
        created = await ensure_next_mega_set_delivery(
            session,
            remote_root=_REMOTE_ROOT,
            now=RUN_AT + timedelta(minutes=11),
        )
        delivery_count = await session.scalar(select(func.count(MegaSetDelivery.id)))
        source = await session.get(FinishedSetArchive, archive.archive_id)

    assert not created
    assert delivery_count == 0
    assert source is not None
    assert source.mega_requested_at is None
    assert source.mega_requested_by_user_id is None
    assert source.mega_requested_remote_root is None


@pytest.mark.asyncio
async def test_retired_legacy_delivery_is_not_reopened_by_a_new_png_request(
    derivative_approved_context: ApprovedContext,
) -> None:
    approved = derivative_approved_context
    _store, legacy_archive = await _prepare_archive(
        approved,
        part_sizes=(2,),
        mega_requested=True,
        media_profile="legacy-full-derivative-v1",
    )
    retired_at = RUN_AT + timedelta(minutes=15)
    async with approved.database.sessions() as session:
        legacy_delivery = MegaSetDelivery(
            finished_set_archive_id=legacy_archive.archive_id,
            state=MegaDeliveryState.FAILED,
            remote_root=_REMOTE_ROOT,
            remote_folder=f"{_REMOTE_ROOT}/legacy-retired",
            manifest_sha256=legacy_archive.manifest_sha256,
            total_item_count=2,
            uploaded_item_count=0,
            total_byte_size=None,
            source_manifest_json=None,
            uploaded_byte_size=0,
            attempts=0,
            available_at=retired_at,
            lease_owner=None,
            lease_expires_at=None,
            completion_marker_node_handle=None,
            planned_at=None,
            started_at=None,
            verified_at=None,
            completed_at=retired_at,
            last_error_code="mega_set_legacy_media_retired",
            last_error_detail="Legacy delivery retired safely.",
            created_at=retired_at,
            updated_at=retired_at,
        )
        session.add(legacy_delivery)
        await session.commit()
        legacy_delivery_id = legacy_delivery.id

    async with approved.database.sessions() as session:
        requested = await request_mega_set_delivery(
            session,
            review_task_id=approved.review_task_id,
            requested_by_user_id=approved.owner_id,
            remote_root=_REMOTE_ROOT,
            now=retired_at + timedelta(seconds=1),
        )
    async with approved.database.sessions() as session:
        legacy_delivery = await session.get(MegaSetDelivery, legacy_delivery_id)
        public_archive = await session.get(FinishedSetArchive, requested.archive_id)

    assert legacy_delivery is not None
    assert legacy_delivery.state == MegaDeliveryState.FAILED
    assert legacy_delivery.last_error_code == "mega_set_legacy_media_retired"
    assert requested.archive_id != legacy_archive.archive_id
    assert requested.delivery_id is None
    assert public_archive is not None
    assert public_archive.media_profile == "public-png-v1"


@pytest.mark.asyncio
async def test_requested_ready_legacy_archive_is_never_discovered(
    derivative_approved_context: ApprovedContext,
) -> None:
    approved = derivative_approved_context
    _store, legacy_archive = await _prepare_archive(
        approved,
        part_sizes=(2,),
        mega_requested=True,
        media_profile="legacy-full-derivative-v1",
    )

    async with approved.database.sessions() as session:
        created = await ensure_next_mega_set_delivery(
            session,
            remote_root=_REMOTE_ROOT,
            now=RUN_AT + timedelta(minutes=15),
        )
        delivery_count = await session.scalar(select(func.count(MegaSetDelivery.id)))
        archive = await session.get(FinishedSetArchive, legacy_archive.archive_id)

    assert not created
    assert delivery_count == 0
    assert archive is not None
    assert archive.media_profile == "legacy-full-derivative-v1"
    assert archive.mega_requested_at is not None


@pytest.mark.asyncio
async def test_escaped_legacy_delivery_fails_before_any_mega_call(
    derivative_approved_context: ApprovedContext,
) -> None:
    approved = derivative_approved_context
    store, legacy_archive = await _prepare_archive(
        approved,
        part_sizes=(2,),
        mega_requested=True,
        media_profile="legacy-full-derivative-v1",
    )
    queued_at = RUN_AT + timedelta(minutes=15)
    async with approved.database.sessions() as session:
        delivery = MegaSetDelivery(
            finished_set_archive_id=legacy_archive.archive_id,
            state=MegaDeliveryState.PENDING,
            remote_root=_REMOTE_ROOT,
            remote_folder=f"{_REMOTE_ROOT}/escaped-legacy",
            manifest_sha256=legacy_archive.manifest_sha256,
            total_item_count=2,
            uploaded_item_count=0,
            total_byte_size=None,
            source_manifest_json=None,
            uploaded_byte_size=0,
            attempts=0,
            available_at=queued_at,
            lease_owner=None,
            lease_expires_at=None,
            completion_marker_node_handle=None,
            planned_at=None,
            started_at=None,
            verified_at=None,
            completed_at=None,
            last_error_code=None,
            last_error_detail=None,
            created_at=queued_at,
            updated_at=queued_at,
        )
        session.add(delivery)
        await session.commit()
        delivery_id = delivery.id

    mega = _RecordingMegaClient()
    result = await _cycle_mega(approved, store, mega)
    async with approved.database.sessions() as session:
        failed = await session.get(MegaSetDelivery, delivery_id)

    assert result.processed_delivery
    assert failed is not None
    assert failed.state == MegaDeliveryState.FAILED
    assert failed.last_error_code == "mega_set_contract"
    assert mega.remote == {}
    assert mega.upload_files_calls == []


@pytest.mark.asyncio
async def test_early_mega_request_persists_until_archive_ready_and_queues_once(
    derivative_approved_context: ApprovedContext,
) -> None:
    approved = derivative_approved_context
    prepared = await _prepare(approved)
    await _cycle(prepared, worker_id="early-mega-source-derivative")
    await _cycle(prepared, worker_id="early-mega-source-derivative")
    requested_at = RUN_AT + timedelta(minutes=2)

    async with approved.database.sessions() as session:
        requested = await request_mega_set_delivery(
            session,
            review_task_id=approved.review_task_id,
            requested_by_user_id=approved.owner_id,
            remote_root=_REMOTE_ROOT,
            now=requested_at,
        )
        source = await session.get(FinishedSetArchive, requested.archive_id)
        assert source is not None
        assert source.state == FinishedSetArchiveState.PENDING
        assert source.requested_by_user_id is None
        assert source.mega_requested_at is not None
        assert source.mega_requested_at.replace(tzinfo=UTC) == requested_at
        assert source.mega_requested_by_user_id == approved.owner_id
        assert source.mega_requested_remote_root == _REMOTE_ROOT
        snapshot = await load_finished_set_archive(
            session,
            review_task_id=approved.review_task_id,
        )
        assert snapshot is not None
        assert snapshot.requested_by_user_id is None
        assert snapshot.mega_requested_remote_root == _REMOTE_ROOT
        assert requested.delivery_id is None
        assert not requested.replayed

        replay = await request_mega_set_delivery(
            session,
            review_task_id=approved.review_task_id,
            requested_by_user_id=approved.owner_id,
            remote_root=_REMOTE_ROOT,
            now=requested_at + timedelta(milliseconds=1),
        )
        assert replay.replayed
        with pytest.raises(
            MegaSetDeliveryContractError,
            match="existing MEGA request uses a different destination root",
        ):
            await request_mega_set_delivery(
                session,
                review_task_id=approved.review_task_id,
                requested_by_user_id=approved.owner_id,
                remote_root="/changed-request-root",
                now=requested_at + timedelta(milliseconds=2),
            )
        await session.rollback()

        created_early = await ensure_next_mega_set_delivery(
            session,
            remote_root=_REMOTE_ROOT,
            now=requested_at,
        )

    assert not created_early

    archive_result = await run_finished_set_archive_cycle(
        approved.database.sessions,
        prepared.store,
        worker_id="early-mega-finished-set",
        lease_seconds=300,
        retry_base_seconds=5,
        retry_max_seconds=60,
        max_archive_bytes=_MAX_PART_BYTES,
        now=requested_at + timedelta(seconds=1),
    )
    assert archive_result.completed_archive
    assert archive_result.archive_id == requested.archive_id

    async with approved.database.sessions() as session:
        mega_only_source = await session.get(FinishedSetArchive, requested.archive_id)
        assert mega_only_source is not None
        assert mega_only_source.state == FinishedSetArchiveState.READY
        assert mega_only_source.requested_by_user_id is None
        zip_request = await request_finished_set_archive(
            session,
            review_task_id=approved.review_task_id,
            requested_by_user_id=approved.owner_id,
            now=requested_at + timedelta(seconds=2),
        )
        assert zip_request.requested_by_user_id == approved.owner_id
        assert zip_request.mega_requested_remote_root == _REMOTE_ROOT

    async with approved.database.sessions() as session:
        created = await ensure_next_mega_set_delivery(
            session,
            remote_root="/changed-runtime-root",
            now=requested_at + timedelta(seconds=3),
        )
    async with approved.database.sessions() as session:
        duplicate = await ensure_next_mega_set_delivery(
            session,
            remote_root=_REMOTE_ROOT,
            now=requested_at + timedelta(seconds=4),
        )
        deliveries = tuple((await session.scalars(select(MegaSetDelivery))).all())

    assert created
    assert not duplicate
    assert len(deliveries) == 1
    assert deliveries[0].finished_set_archive_id == requested.archive_id
    assert deliveries[0].remote_root == _REMOTE_ROOT


@pytest.mark.asyncio
async def test_public_png_archive_reaches_mega_exactly_without_full_jpeg(
    derivative_approved_context: ApprovedContext,
) -> None:
    approved = derivative_approved_context
    prepared = await _prepare(approved)
    await _cycle(prepared, worker_id="mega-public-png-derivative")
    await _cycle(prepared, worker_id="mega-public-png-derivative")

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
        requested = await request_mega_set_delivery(
            session,
            review_task_id=approved.review_task_id,
            requested_by_user_id=approved.owner_id,
            remote_root=_REMOTE_ROOT,
            now=RUN_AT + timedelta(minutes=20),
        )
    assert len(full_rows) == 2
    assert all(output.asset_content_type == "image/jpeg" for output, _ in full_rows)
    full_jpegs = tuple(
        prepared.store.objects[output.asset_object_key].body for output, _ in full_rows
    )

    archive_result = await run_finished_set_archive_cycle(
        approved.database.sessions,
        prepared.store,
        worker_id="mega-public-png-archive",
        lease_seconds=300,
        retry_base_seconds=5,
        retry_max_seconds=60,
        max_archive_bytes=_MAX_PART_BYTES,
        now=RUN_AT + timedelta(minutes=20, seconds=1),
    )
    assert archive_result.completed_archive
    assert archive_result.archive_id == requested.archive_id

    mega = _RecordingMegaClient()
    created = await _cycle_mega(approved, prepared.store, mega)
    delivered = await _cycle_mega(approved, prepared.store, mega)
    assert created.created_delivery
    assert delivered.completed_delivery
    assert mega.upload_files_calls == [("001.png", "002.png")]

    image_paths = (
        "/sets/Derivative release/001.png",
        "/sets/Derivative release/002.png",
    )
    delivered_pngs = tuple(mega.remote[path] for path in image_paths)
    assert delivered_pngs != approved.raw_payloads
    assert all(payload not in full_jpegs for payload in delivered_pngs)
    cached_by_sha = {
        hashlib.sha256(stored.body).hexdigest(): stored.body
        for key, stored in prepared.store.objects.items()
        if key.startswith("public-media/public-png-v1/")
    }
    for payload, source in zip(delivered_pngs, approved.raw_payloads, strict=True):
        assert cached_by_sha[hashlib.sha256(payload).hexdigest()] == payload
        assert_delivery_metadata_absent(payload)
        assert_private_master_metadata_present(source)
        with Image.open(BytesIO(payload)) as image, Image.open(BytesIO(source)) as source_image:
            assert image.format == "PNG"
            assert image.size == source_image.size == (64, 64)
            assert image.convert("RGB").tobytes() == source_image.convert("RGB").tobytes()

    assert set(mega.remote) == set(image_paths)
    assert not any(path.endswith(".json") for path in mega.remote)


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
    assert mega.upload_files_calls == [("001.png",), ("002.png",)]
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
    assert delivery.completion_marker_node_handle is None
    assert delivery.verified_at is not None
    assert delivery.completed_at is not None
    assert [item.ordinal for item in items] == [1, 2]
    assert [item.readiness_derivative_output_id for item in items] == list(
        archive.derivative_output_ids
    )
    assert [item.source_asset_id for item in items] == list(archive.source_asset_ids)
    assert all(item.state == MegaDeliveryState.SUCCEEDED for item in items)
    assert all(item.remote_node_handle is None for item in items)

    image_paths = [item.remote_path for item in items]
    assert [mega.remote[path] for path in image_paths] == list(archive.image_payloads)
    for path in image_paths:
        assert_delivery_metadata_absent(mega.remote[path])
    assert all(mega.write_counts[path] == 1 for path in image_paths)
    assert set(mega.remote) == set(image_paths)
    assert not any(path.endswith(".json") for path in mega.remote)

    assert direct == listed[0]
    assert [item.ordinal for item in direct.items] == [1, 2]
    assert direct.next_retry_at is None
    assert not direct.retired
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
    assert mega.upload_files_calls == [("001.png", "002.png")]

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
        ("001.png", "002.png"),
        ("002.png",),
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


@pytest.mark.parametrize("set_title", ["Yamato", "Yamato - One Piece (SFW)", "My set (PNG)"])
@pytest.mark.asyncio
async def test_new_mega_folder_uses_set_title_without_automatic_suffix(
    derivative_approved_context: ApprovedContext,
    set_title: str,
) -> None:
    approved = derivative_approved_context
    store, _archive = await _prepare_archive(approved, part_sizes=(2,))
    async with approved.database.sessions() as session:
        release = await session.get(Release, approved.release_id)
        assert release is not None
        release.title = set_title
        await session.commit()

    mega = _RecordingMegaClient()
    assert (await _cycle_mega(approved, store, mega)).created_delivery
    assert (await _cycle_mega(approved, store, mega)).completed_delivery
    async with approved.database.sessions() as session:
        delivery = await session.scalar(select(MegaSetDelivery))
        assert delivery is not None
        assert delivery.remote_folder == f"{_REMOTE_ROOT}/{set_title}"
    assert set(mega.remote) == {
        f"{_REMOTE_ROOT}/{set_title}/001.png",
        f"{_REMOTE_ROOT}/{set_title}/002.png",
    }


@pytest.mark.asyncio
async def test_existing_png_suffixed_delivery_keeps_its_stored_destination(
    derivative_approved_context: ApprovedContext,
) -> None:
    approved = derivative_approved_context
    store, archive = await _prepare_archive(approved, part_sizes=(2,))
    mega = _RecordingMegaClient()

    # Simulate a persisted delivery created before the naming change.
    historical_folder = f"{_REMOTE_ROOT}/Derivative release (PNG)"
    async with approved.database.sessions() as session:
        delivery = MegaSetDelivery(
            finished_set_archive_id=archive.archive_id,
            remote_root=_REMOTE_ROOT,
            remote_folder=historical_folder,
            manifest_sha256=archive.manifest_sha256,
            total_item_count=2,
            available_at=RUN_AT,
            created_at=RUN_AT,
            updated_at=RUN_AT,
        )
        session.add(delivery)
        await session.commit()
        delivery_id = delivery.id

    assert (await _cycle_mega(approved, store, mega)).completed_delivery
    async with approved.database.sessions() as session:
        delivery = await session.get(MegaSetDelivery, delivery_id)
        assert delivery is not None
        assert delivery.remote_folder == historical_folder
        assert await session.scalar(select(func.count(MegaSetDelivery.id))) == 1
    assert set(mega.remote) == {
        f"{historical_folder}/001.png",
        f"{historical_folder}/002.png",
    }


@pytest.mark.parametrize("existing_filename", ["existing-file.txt", "001.jpg"])
@pytest.mark.asyncio
async def test_existing_named_set_folder_is_not_mixed_or_overwritten(
    derivative_approved_context: ApprovedContext,
    existing_filename: str,
) -> None:
    approved = derivative_approved_context
    store, _archive = await _prepare_archive(approved, part_sizes=(2,))
    mega = _RecordingMegaClient()

    assert (await _cycle_mega(approved, store, mega)).created_delivery
    async with approved.database.sessions() as session:
        delivery = await session.scalar(select(MegaSetDelivery))
        assert delivery is not None
        assert delivery.remote_folder == "/sets/Derivative release"
        mega.remote[f"{delivery.remote_folder}/{existing_filename}"] = b"owned elsewhere"

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
    mega_requested: bool = True,
    media_profile: str = "public-png-v1",
) -> tuple[TrackingObjectStore, _ArchiveFixture]:
    assert media_profile in {"public-png-v1", "legacy-full-derivative-v1"}
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
    source_asset_ids: list[UUID] = []
    for ordinal, (output, selection) in enumerate(rows, start=1):
        if media_profile == "public-png-v1":
            private_source = prepared.store.objects[f"raw/{selection.asset_id}.png"].body
            public_buffer = BytesIO()
            with Image.open(BytesIO(private_source)) as source_image:
                width, height = source_image.size
                source_image.convert("RGB").save(public_buffer, format="PNG", compress_level=6)
            payload = public_buffer.getvalue()
            extension = "png"
            content_type = "image/png"
        else:
            stored = prepared.store.objects[output.asset_object_key]
            payload = stored.body
            extension = {
                "image/jpeg": "jpg",
                "image/png": "png",
                "image/webp": "webp",
            }[output.asset_content_type]
            content_type = output.asset_content_type
        image_payloads.append(payload)
        derivative_output_ids.append(output.id)
        source_asset_ids.append(selection.asset_id)
        item: dict[str, object] = {
            "ordinal": ordinal,
            "generation_queue_position": selection.source_generation_queue_position,
            "path": f"content/{ordinal:03d}.{extension}",
            "sha256": hashlib.sha256(payload).hexdigest(),
            "byte_size": len(payload),
            "content_type": content_type,
        }
        if media_profile == "public-png-v1":
            item.update(
                {
                    "source_asset_id": str(selection.asset_id),
                    "source_sha256": selection.source_sha256,
                    "readiness_derivative_output_id": str(output.id),
                    "image_format": "PNG",
                    "width": width,
                    "height": height,
                }
            )
        else:
            item["derivative_output_id"] = str(output.id)
        outputs.append(item)

    manifest_payload: dict[str, object] = {
        "schema": (
            "finished-set-manifest/v2"
            if media_profile == "public-png-v1"
            else "finished-set-manifest/v1"
        ),
        "archive_id": str(archive_id),
        "selection_count": len(outputs),
        "ordering": "frozen_generation_queue",
        "ordering_key": ["generation_queue_position"],
        "outputs": outputs,
    }
    if media_profile == "public-png-v1":
        manifest_payload["media_profile"] = media_profile
    manifest = canonical_json_bytes(manifest_payload)
    manifest_sha256 = hashlib.sha256(manifest).hexdigest()
    now = RUN_AT + timedelta(minutes=10)
    archive = FinishedSetArchive(
        id=archive_id,
        review_task_id=approved.review_task_id,
        release_version_id=approved.release_version_id,
        media_profile=media_profile,
        requested_by_user_id=approved.owner_id,
        mega_requested_by_user_id=(approved.owner_id if mega_requested else None),
        mega_requested_at=(now if mega_requested else None),
        mega_requested_remote_root=(_REMOTE_ROOT if mega_requested else None),
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
                "schema": (
                    "finished-set-part-manifest/v2"
                    if media_profile == "public-png-v1"
                    else "finished-set-part-manifest/v1"
                ),
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
        source_asset_ids=tuple(source_asset_ids),
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
