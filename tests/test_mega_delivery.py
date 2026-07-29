import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select

from gen_automation.api.routes.mega_deliveries import (
    get_mega_delivery,
    get_release_mega_deliveries,
)
from gen_automation.app import create_app
from gen_automation.config import Environment, Settings
from gen_automation.db.models import (
    AdminUser,
    MegaDelivery,
    Project,
    PublicationIntent,
    PublicationPackage,
    Release,
    ReleaseVersion,
)
from gen_automation.db.session import Database
from gen_automation.domain.enums import (
    AdminRole,
    MegaDeliveryState,
    PublicationIntentState,
    PublicationTarget,
)
from gen_automation.domain.mega import MegaDeliveryRead
from gen_automation.integrations.mega import (
    MegaAmbiguousError,
    MegaCmdClient,
    MegaConfigurationError,
    MegaRemoteNode,
)
from gen_automation.integrations.mega.client import MegaCommandResult
from gen_automation.services.mega_delivery import (
    ensure_next_mega_delivery,
    run_mega_delivery_cycle,
)


class _PackageStore:
    backend = "memory"
    bucket = "asset-bucket"

    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    async def read_bytes(
        self,
        key: str,
        *,
        max_bytes: int,
        version_id: str | None = None,
        etag: str | None = None,
    ) -> bytes:
        assert key == "packages/set.zip"
        assert version_id == "version-1"
        assert etag is None
        assert len(self.payload) <= max_bytes
        return self.payload


class _ResponseLossMegaClient:
    def __init__(self) -> None:
        self.remote: dict[str, bytes] = {}
        self.upload_calls = 0

    async def ensure_folder(self, remote_folder: str) -> None:
        assert remote_folder.startswith("/sets/")

    async def find_file(self, remote_path: str) -> tuple[MegaRemoteNode, ...]:
        if remote_path not in self.remote:
            return ()
        return (MegaRemoteNode(handle="H:abcdEFGH", remote_path=remote_path),)

    async def upload_file(self, local_file: Path, remote_folder: str) -> None:
        self.upload_calls += 1
        remote_path = f"{remote_folder}/{local_file.name}"
        self.remote[remote_path] = await asyncio.to_thread(local_file.read_bytes)
        if self.upload_calls == 1:
            raise MegaAmbiguousError("simulated response loss")

    async def download_node(self, node: MegaRemoteNode, local_folder: Path) -> Path:
        destination = local_folder / Path(node.remote_path).name
        destination.write_bytes(self.remote[node.remote_path])
        return destination


class _UnavailableMegaClient:
    async def ensure_folder(self, remote_folder: str) -> None:
        raise MegaConfigurationError("simulated unavailable profile")

    async def find_file(self, remote_path: str) -> tuple[MegaRemoteNode, ...]:
        raise AssertionError("find must not run when folder setup fails")

    async def upload_file(self, local_file: Path, remote_folder: str) -> None:
        raise AssertionError("upload must not run when folder setup fails")

    async def download_node(self, node: MegaRemoteNode, local_folder: Path) -> Path:
        raise AssertionError("download must not run when folder setup fails")


@pytest.mark.asyncio
async def test_response_loss_is_adopted_without_a_second_upload(tmp_path: Path) -> None:
    payload = b"deterministic clean Patreon package"
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'mega.db').as_posix()}")
    await database.create_schema()
    try:
        await _insert_package(database, payload)
        store = _PackageStore(payload)
        mega = _ResponseLossMegaClient()

        created = await run_mega_delivery_cycle(
            database.sessions,
            store=store,  # type: ignore[arg-type]
            client=mega,
            worker_id="test:mega",
            remote_root="/sets",
            lease_seconds=120,
            retry_base_seconds=1,
            retry_max_seconds=60,
            max_package_bytes=1024,
        )
        assert created.created_delivery

        ambiguous = await run_mega_delivery_cycle(
            database.sessions,
            store=store,  # type: ignore[arg-type]
            client=mega,
            worker_id="test:mega",
            remote_root="/sets",
            lease_seconds=120,
            retry_base_seconds=1,
            retry_max_seconds=60,
            max_package_bytes=1024,
        )
        assert ambiguous.processed_delivery
        assert not ambiguous.completed_delivery
        assert mega.upload_calls == 1

        async with database.sessions() as session:
            delivery = await session.scalar(select(MegaDelivery))
            assert delivery is not None
            delivery.available_at = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()

        adopted = await run_mega_delivery_cycle(
            database.sessions,
            store=store,  # type: ignore[arg-type]
            client=mega,
            worker_id="test:mega",
            remote_root="/sets",
            lease_seconds=120,
            retry_base_seconds=1,
            retry_max_seconds=60,
            max_package_bytes=1024,
        )
        assert adopted.completed_delivery
        assert mega.upload_calls == 1

        async with database.sessions() as session:
            delivery = await session.scalar(select(MegaDelivery))
            assert delivery is not None
            assert delivery.state == MegaDeliveryState.SUCCEEDED
            assert delivery.remote_node_handle == "H:abcdEFGH"
            assert delivery.sha256 == hashlib.sha256(payload).hexdigest()
            assert delivery.byte_size == len(payload)
            assert delivery.verified_at is not None
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_retryable_failure_is_not_stranded_after_many_attempts(tmp_path: Path) -> None:
    payload = b"small clean package"
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'retry.db').as_posix()}")
    await database.create_schema()
    try:
        await _insert_package(database, payload)
        store = _PackageStore(payload)
        mega = _UnavailableMegaClient()
        created = await run_mega_delivery_cycle(
            database.sessions,
            store=store,  # type: ignore[arg-type]
            client=mega,
            worker_id="test:mega",
            remote_root="/sets",
            lease_seconds=120,
            retry_base_seconds=1,
            retry_max_seconds=60,
            max_package_bytes=1024,
        )
        assert created.created_delivery

        for _ in range(12):
            result = await run_mega_delivery_cycle(
                database.sessions,
                store=store,  # type: ignore[arg-type]
                client=mega,
                worker_id="test:mega",
                remote_root="/sets",
                lease_seconds=120,
                retry_base_seconds=1,
                retry_max_seconds=60,
                max_package_bytes=1024,
            )
            assert result.processed_delivery
            async with database.sessions() as session:
                delivery = await session.scalar(select(MegaDelivery))
                assert delivery is not None
                assert delivery.state == MegaDeliveryState.RETRY_WAIT
                assert delivery.completed_at is None
                delivery.available_at = datetime.now(UTC) - timedelta(seconds=1)
                await session.commit()

        async with database.sessions() as session:
            delivery = await session.scalar(select(MegaDelivery))
            assert delivery is not None
            assert delivery.attempts == 12
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_megacmd_failure_never_exposes_command_output_or_credentials(
    tmp_path: Path,
) -> None:
    profile = tmp_path / "profile"
    (profile / ".megaCmd").mkdir(parents=True)
    profile.chmod(0o700)
    (profile / ".megaCmd").chmod(0o700)
    local_file = tmp_path / f"{'a' * 64}.zip"
    local_file.write_bytes(b"package")
    secret = b"folder-write-auth-key-and-session"
    commands: list[tuple[str, ...]] = []

    async def runner(
        command: tuple[str, ...],
        profile_home: Path,
        timeout_seconds: float,
    ) -> MegaCommandResult:
        commands.append(command)
        assert profile_home == profile.resolve()
        assert timeout_seconds == 5
        return MegaCommandResult(return_code=3, stdout=secret)

    client = MegaCmdClient(
        profile_home=profile,
        command_timeout_seconds=5,
        runner=runner,
    )
    with pytest.raises(MegaAmbiguousError) as captured:
        await client.upload_file(local_file, "/sets")
    assert secret.decode() not in str(captured.value)
    assert secret.decode() not in repr(client)
    assert all(secret.decode() not in argument for command in commands for argument in command)


@pytest.mark.asyncio
async def test_megacmd_rejects_a_symlinked_profile(tmp_path: Path) -> None:
    profile = tmp_path / "real-profile"
    (profile / ".megaCmd").mkdir(parents=True)
    profile.chmod(0o700)
    (profile / ".megaCmd").chmod(0o700)
    linked_profile = tmp_path / "linked-profile"
    try:
        linked_profile.symlink_to(profile, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable on this test host")

    async def runner(
        command: tuple[str, ...],
        profile_home: Path,
        timeout_seconds: float,
    ) -> MegaCommandResult:
        raise AssertionError("unsafe profile must be rejected before a command runs")

    client = MegaCmdClient(
        profile_home=linked_profile,
        command_timeout_seconds=5,
        runner=runner,
    )
    with pytest.raises(MegaConfigurationError):
        await client.ensure_folder("/sets")


def test_mega_delivery_status_endpoints_return_ordered_safe_references(
    tmp_path: Path,
) -> None:
    settings = Settings(
        environment=Environment.TEST,
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'mega-api.db').as_posix()}",
        auto_create_schema=False,
        auth_development_bypass_enabled=True,
        session_secret="test-session-secret-with-more-than-32-characters",  # noqa: S106
    )
    release_id, delivery_ids = asyncio.run(_seed_api_deliveries(settings))
    direct_route, list_route, missing_detail = asyncio.run(
        _exercise_status_routes(settings, release_id, delivery_ids[0])
    )

    with TestClient(create_app(settings)) as client:
        direct = client.get(f"/api/v1/mega-deliveries/{delivery_ids[0]}")
        missing = client.get(f"/api/v1/mega-deliveries/{uuid4()}")
        release_deliveries = client.get(f"/api/v1/releases/{release_id}/mega-deliveries")

    assert direct.status_code == 200
    assert missing.status_code == 404
    assert missing.json() == {"detail": "MEGA delivery was not found"}
    assert release_deliveries.status_code == 200
    rows = release_deliveries.json()
    assert [row["id"] for row in rows] == [str(delivery_id) for delivery_id in delivery_ids]
    assert direct.json() == rows[0]
    assert direct_route.model_dump(mode="json") == direct.json()
    assert [row.model_dump(mode="json") for row in list_route] == rows
    assert missing_detail == "MEGA delivery was not found"
    assert all(row["state"] == MegaDeliveryState.SUCCEEDED for row in rows)
    assert all(row["remote_node_handle"] is not None for row in rows)
    assert set(rows[0]) == {
        "id",
        "publication_package_id",
        "state",
        "remote_path",
        "sha256",
        "byte_size",
        "attempts",
        "available_at",
        "remote_node_handle",
        "verified_at",
        "completed_at",
        "last_error_code",
        "created_at",
        "updated_at",
    }


async def _exercise_status_routes(
    settings: Settings,
    release_id: UUID,
    delivery_id: UUID,
) -> tuple[MegaDeliveryRead, list[MegaDeliveryRead], str]:
    database = Database(settings.database_url)
    try:
        async with database.sessions() as session:
            direct = await get_mega_delivery(
                delivery_id,
                session,
                None,  # type: ignore[arg-type]
            )
            deliveries = await get_release_mega_deliveries(
                release_id,
                session,
                None,  # type: ignore[arg-type]
            )
            with pytest.raises(HTTPException) as missing:
                await get_mega_delivery(
                    uuid4(),
                    session,
                    None,  # type: ignore[arg-type]
                )
            return direct, list(deliveries), str(missing.value.detail)
    finally:
        await database.dispose()


async def _seed_api_deliveries(settings: Settings) -> tuple[UUID, tuple[UUID, UUID]]:
    database = Database(settings.database_url)
    await database.create_schema()
    try:
        await _insert_package(database, b"first clean package")
        now = datetime.now(UTC)
        async with database.sessions() as session:
            first_intent = await session.scalar(select(PublicationIntent))
            assert first_intent is not None
            second_intent = PublicationIntent(
                release_id=first_intent.release_id,
                release_version_id=first_intent.release_version_id,
                target=PublicationTarget.PATREON,
                state=PublicationIntentState.AWAITING_HUMAN,
                configuration={"package": 2},
                configuration_sha256="1" * 64,
                input_manifest_sha256="2" * 64,
                intent_digest="3" * 64,
                input_count=1,
                credential_reference=None,
                scheduled_at=None,
                public_preview_attester_name=first_intent.public_preview_attester_name,
                public_preview_attester_user_id=(first_intent.public_preview_attester_user_id),
                public_preview_attested_at=now,
                public_preview_attestation_timezone="UTC",
                public_preview_attestation_sha256="4" * 64,
                planned_by_user_id=first_intent.planned_by_user_id,
                planned_at=now,
                lock_version=1,
                completed_at=None,
                last_error_code=None,
                last_error_detail=None,
            )
            session.add(second_intent)
            await session.flush()
            second_payload = b"second clean package"
            session.add(
                PublicationPackage(
                    intent_id=second_intent.id,
                    storage_backend="memory",
                    storage_bucket="asset-bucket",
                    object_key="packages/set-2.zip",
                    object_version_id="version-2",
                    sha256=hashlib.sha256(second_payload).hexdigest(),
                    manifest_sha256="5" * 64,
                    byte_size=len(second_payload),
                    content_type="application/zip",
                    created_at=now + timedelta(seconds=1),
                )
            )
            await session.commit()

        async with database.sessions() as session:
            assert await ensure_next_mega_delivery(session, remote_root="/sets")
        async with database.sessions() as session:
            assert await ensure_next_mega_delivery(session, remote_root="/sets")

        completed_at = now + timedelta(minutes=1)
        async with database.sessions() as session:
            deliveries = list(
                (
                    await session.scalars(
                        select(MegaDelivery).order_by(
                            MegaDelivery.created_at,
                            MegaDelivery.id,
                        )
                    )
                ).all()
            )
            assert len(deliveries) == 2
            for index, delivery in enumerate(deliveries):
                delivery.state = MegaDeliveryState.SUCCEEDED
                delivery.attempts = index + 1
                delivery.remote_node_handle = f"H:abcdEFG{index}"
                delivery.verified_at = completed_at + timedelta(seconds=index)
                delivery.completed_at = completed_at + timedelta(seconds=index)
                delivery.created_at = now + timedelta(seconds=index)
                delivery.updated_at = completed_at + timedelta(seconds=index)
            await session.commit()
            return (
                first_intent.release_id,
                (deliveries[0].id, deliveries[1].id),
            )
    finally:
        await database.dispose()


async def _insert_package(database: Database, payload: bytes) -> None:
    now = datetime.now(UTC)
    sha256 = hashlib.sha256(payload).hexdigest()
    async with database.sessions() as session:
        user = AdminUser(
            username_normalized="owner",
            display_name="Owner",
            password_hash="not-used",  # noqa: S106 - inert fixture value
            role=AdminRole.OWNER,
            is_active=True,
            totp_secret_ciphertext=None,
            totp_confirmed_at=None,
            last_totp_counter=None,
            failed_login_count=0,
            failed_login_window_started_at=None,
            locked_until=None,
            password_changed_at=now,
            last_login_at=None,
            credential_version=1,
            lock_version=1,
            created_at=now,
            updated_at=now,
        )
        project = Project(slug="creator", name="Creator", created_at=now, updated_at=now)
        session.add_all((user, project))
        await session.flush()
        release = Release(
            project_id=project.id,
            slug="sample-set",
            title="Sample set",
            desired_accepted_count=1,
            current_version_no=1,
            scheduled_at=None,
            lock_version=1,
            created_at=now,
            updated_at=now,
        )
        session.add(release)
        await session.flush()
        version = ReleaseVersion(
            release_id=release.id,
            version_no=1,
            specification={},
            specification_sha256="a" * 64,
            created_by="test",
            created_at=now,
        )
        session.add(version)
        await session.flush()
        intent = PublicationIntent(
            release_id=release.id,
            release_version_id=version.id,
            target=PublicationTarget.PATREON,
            state=PublicationIntentState.AWAITING_HUMAN,
            configuration={},
            configuration_sha256="b" * 64,
            input_manifest_sha256="c" * 64,
            intent_digest="d" * 64,
            input_count=1,
            credential_reference=None,
            scheduled_at=None,
            public_preview_attester_name="Owner",
            public_preview_attester_user_id=user.id,
            public_preview_attested_at=now,
            public_preview_attestation_timezone="UTC",
            public_preview_attestation_sha256="e" * 64,
            planned_by_user_id=user.id,
            planned_at=now,
            lock_version=1,
            completed_at=None,
            last_error_code=None,
            last_error_detail=None,
        )
        session.add(intent)
        await session.flush()
        session.add(
            PublicationPackage(
                intent_id=intent.id,
                storage_backend="memory",
                storage_bucket="asset-bucket",
                object_key="packages/set.zip",
                object_version_id="version-1",
                sha256=sha256,
                manifest_sha256="f" * 64,
                byte_size=len(payload),
                content_type="application/zip",
                created_at=now,
            )
        )
        await session.commit()
