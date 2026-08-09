from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, create_engine, select
from sqlalchemy.exc import IntegrityError

_PREVIOUS = "20260809_0031"
_MIGRATION = "20260809_0032"
_NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)


def _configuration(database_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    monkeypatch.setenv(
        "GEN_AUTOMATION_DATABASE_URL",
        f"sqlite+aiosqlite:///{database_path.as_posix()}",
    )
    return Config("alembic.ini")


def _seed_succeeded_delivery(
    database_path: Path,
    *,
    completion_marker_node_handle: str | None,
    suffix: str,
) -> str:
    delivery_id = uuid4().hex
    archive_id = uuid4().hex
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    metadata = MetaData()
    metadata.reflect(
        bind=engine,
        only=["finished_set_archives", "mega_set_deliveries"],
    )
    archives = metadata.tables["finished_set_archives"]
    deliveries = metadata.tables["mega_set_deliveries"]
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys = OFF")
        connection.commit()
        connection.execute(
            archives.insert(),
            {
                "id": archive_id,
                "review_task_id": uuid4().hex,
                "release_version_id": uuid4().hex,
                "media_profile": "public-png-v1",
                "state": "ready",
                "selection_count": 1,
                "manifest_sha256": "a" * 64,
                "part_count": 1,
                "attempts": 1,
                "max_attempts": 5,
                "available_at": _NOW,
                "created_at": _NOW,
                "updated_at": _NOW,
                "started_at": _NOW,
                "completed_at": _NOW,
            },
        )
        connection.execute(
            deliveries.insert(),
            {
                "id": delivery_id,
                "finished_set_archive_id": archive_id,
                "state": "succeeded",
                "remote_root": "/Future",
                "remote_folder": f"/Future/image-only-{suffix}",
                "manifest_sha256": "a" * 64,
                "total_item_count": 1,
                "uploaded_item_count": 1,
                "total_byte_size": 123,
                "source_manifest_json": "{}",
                "uploaded_byte_size": 123,
                "attempts": 1,
                "available_at": _NOW,
                "completion_marker_node_handle": completion_marker_node_handle,
                "planned_at": _NOW,
                "started_at": _NOW,
                "verified_at": _NOW,
                "completed_at": _NOW,
                "created_at": _NOW,
                "updated_at": _NOW,
            },
        )
        connection.commit()
    engine.dispose()
    return delivery_id


def test_upgrade_allows_image_only_completion_and_preserves_identity_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "image-only-mega.db"
    configuration = _configuration(database_path, monkeypatch)
    command.upgrade(configuration, _PREVIOUS)
    legacy_id = _seed_succeeded_delivery(
        database_path,
        completion_marker_node_handle="H:legacy-marker",
        suffix="legacy",
    )

    command.upgrade(configuration, _MIGRATION)
    image_only_id = _seed_succeeded_delivery(
        database_path,
        completion_marker_node_handle=None,
        suffix="current",
    )

    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    metadata = MetaData()
    metadata.reflect(bind=engine, only=["mega_set_deliveries"])
    deliveries = metadata.tables["mega_set_deliveries"]
    with engine.connect() as connection:
        rows = {row.id: row for row in connection.execute(select(deliveries)).mappings()}
        triggers = {
            row[0]
            for row in connection.exec_driver_sql(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'trigger' AND tbl_name = 'mega_set_deliveries'"
            )
        }
        with pytest.raises(IntegrityError, match="MEGA set delivery identity is immutable"):
            connection.execute(
                deliveries.update()
                .where(deliveries.c.id == image_only_id)
                .values(remote_folder="/Future/tampered")
            )
    assert rows[legacy_id].completion_marker_node_handle == "H:legacy-marker"
    assert rows[image_only_id].completion_marker_node_handle is None
    assert {
        "mega_set_deliveries_guard_update",
        "mega_set_deliveries_reject_delete",
    }.issubset(triggers)
    with pytest.raises(
        RuntimeError,
        match="cannot downgrade image-only MEGA folders",
    ):
        command.downgrade(configuration, _PREVIOUS)
    engine.dispose()


def test_downgrade_restores_legacy_marker_requirement_before_image_only_use(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "legacy-marker-mega.db"
    configuration = _configuration(database_path, monkeypatch)
    command.upgrade(configuration, _PREVIOUS)
    _seed_succeeded_delivery(
        database_path,
        completion_marker_node_handle="H:legacy-marker",
        suffix="legacy",
    )
    command.upgrade(configuration, _MIGRATION)
    command.downgrade(configuration, _PREVIOUS)

    with pytest.raises(IntegrityError):
        _seed_succeeded_delivery(
            database_path,
            completion_marker_node_handle=None,
            suffix="rejected",
        )
