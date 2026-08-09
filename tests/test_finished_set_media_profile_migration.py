from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import MetaData, create_engine, inspect, select
from sqlalchemy.exc import IntegrityError

_MIGRATION = "20260809_0028"
_PREVIOUS = "20260808_0027"
_LEGACY_PROFILE = "legacy-full-derivative-v1"
_PUBLIC_PROFILE = "public-png-v1"
_NOW = datetime(2026, 8, 9, 3, 0, tzinfo=UTC)


def _hex() -> str:
    return uuid4().hex


def _uuid_hex(value: object) -> str:
    return str(value).replace("-", "")


def _configuration(database_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    monkeypatch.setenv(
        "GEN_AUTOMATION_DATABASE_URL",
        f"sqlite+aiosqlite:///{database_path.as_posix()}",
    )
    return Config("alembic.ini")


def _seed_legacy_archive_and_mega_item(database_path: Path) -> dict[str, str]:
    """Seed the exact archive/item identities that existed before revision 0028."""

    identities = {
        "review_task_id": _hex(),
        "release_version_id": _hex(),
        "archive_id": _hex(),
        "delivery_id": _hex(),
        "item_id": _hex(),
        "derivative_output_id": _hex(),
        "source_asset_id": _hex(),
    }
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    metadata = MetaData()
    metadata.reflect(
        bind=engine,
        only=[
            "derivative_outputs",
            "finished_set_archives",
            "mega_set_delivery_items",
        ],
    )
    outputs = metadata.tables["derivative_outputs"]
    archives = metadata.tables["finished_set_archives"]
    items = metadata.tables["mega_set_delivery_items"]

    with engine.connect() as connection:
        # This fixture is intentionally narrow: revision 0028 only needs the frozen
        # identity columns below, while all referenced parent rows are unrelated to
        # the migration contract under test.
        connection.exec_driver_sql("PRAGMA foreign_keys = OFF")
        connection.exec_driver_sql("DROP TRIGGER IF EXISTS derivative_outputs_guard_insert")
        connection.commit()
        connection.execute(
            outputs.insert(),
            {
                "id": identities["derivative_output_id"],
                "derivative_job_id": _hex(),
                "release_selection_id": _hex(),
                "derivative_recipe_id": _hex(),
                "target": "full",
                "asset_id": _hex(),
                "source_asset_id": identities["source_asset_id"],
                "asset_lineage_id": _hex(),
                "asset_storage_backend": "s3",
                "asset_storage_bucket": "legacy-bucket",
                "asset_object_key": "derivatives/full/legacy.jpg",
                "asset_object_version_id": "legacy-version",
                "asset_sha256": "a" * 64,
                "asset_content_type": "image/jpeg",
                "asset_image_format": "JPEG",
                "asset_width": 1144,
                "asset_height": 1480,
                "asset_byte_size": 450_000,
                "lineage_relation": "clean_full",
                "lineage_recipe_version": "legacy-full-v1",
                "recorded_by": "migration-fixture",
                "recorded_at": _NOW,
            },
        )
        connection.execute(
            archives.insert(),
            {
                "id": identities["archive_id"],
                "review_task_id": identities["review_task_id"],
                "release_version_id": identities["release_version_id"],
                "state": "pending",
                "selection_count": 1,
                "attempts": 0,
                "max_attempts": 3,
                "available_at": _NOW,
                "created_at": _NOW,
                "updated_at": _NOW,
            },
        )
        connection.execute(
            items.insert(),
            {
                "id": identities["item_id"],
                "delivery_id": identities["delivery_id"],
                "ordinal": 1,
                "source_derivative_output_id": identities["derivative_output_id"],
                "source_sha256": "a" * 64,
                "source_byte_size": 450_000,
                "source_content_type": "image/jpeg",
                "remote_path": "/sets/legacy/001.jpg",
                "state": "pending",
                "attempts": 0,
                "available_at": _NOW,
            },
        )
        connection.commit()
    engine.dispose()
    return identities


def _insert_archive(
    database_path: Path,
    *,
    review_task_id: str,
    release_version_id: str,
    media_profile: str | None,
) -> str:
    archive_id = _hex()
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    metadata = MetaData()
    metadata.reflect(bind=engine, only=["finished_set_archives"])
    archives = metadata.tables["finished_set_archives"]
    values: dict[str, object] = {
        "id": archive_id,
        "review_task_id": review_task_id,
        "release_version_id": release_version_id,
        "state": "pending",
        "selection_count": 1,
        "attempts": 0,
        "max_attempts": 3,
        "available_at": _NOW,
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    if media_profile is not None:
        values["media_profile"] = media_profile
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys = OFF")
            connection.commit()
            connection.execute(archives.insert(), values)
            connection.commit()
    finally:
        engine.dispose()
    return archive_id


def test_media_profile_and_mega_provenance_migration_round_trip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "finished-set-media-profile-round-trip.db"
    configuration = _configuration(database_path, monkeypatch)
    command.upgrade(configuration, _PREVIOUS)
    identities = _seed_legacy_archive_and_mega_item(database_path)

    command.upgrade(configuration, _MIGRATION)
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    inspector = inspect(engine)
    archive_columns = {
        column["name"]: column for column in inspector.get_columns("finished_set_archives")
    }
    item_columns = {column["name"] for column in inspector.get_columns("mega_set_delivery_items")}
    assert archive_columns["media_profile"]["nullable"] is False
    assert archive_columns["media_profile"]["default"] is None
    assert "source_derivative_output_id" not in item_columns
    assert {"source_asset_id", "readiness_derivative_output_id"} <= item_columns

    metadata = MetaData()
    metadata.reflect(
        bind=engine,
        only=["finished_set_archives", "mega_set_delivery_items"],
    )
    archives = metadata.tables["finished_set_archives"]
    items = metadata.tables["mega_set_delivery_items"]
    with engine.connect() as connection:
        archive = (
            connection.execute(select(archives).where(archives.c.id == identities["archive_id"]))
            .mappings()
            .one()
        )
        item = (
            connection.execute(select(items).where(items.c.id == identities["item_id"]))
            .mappings()
            .one()
        )
        assert archive["media_profile"] == _LEGACY_PROFILE
        assert _uuid_hex(item["source_asset_id"]) == identities["source_asset_id"]
        assert (
            _uuid_hex(item["readiness_derivative_output_id"]) == identities["derivative_output_id"]
        )

    with pytest.raises(IntegrityError, match="MEGA set delivery item identity is immutable"):
        with engine.begin() as connection:
            connection.execute(
                items.update()
                .where(items.c.id == identities["item_id"])
                .values(source_asset_id=_hex())
            )
    with pytest.raises(IntegrityError, match="MEGA set delivery item identity is immutable"):
        with engine.begin() as connection:
            connection.execute(
                items.update()
                .where(items.c.id == identities["item_id"])
                .values(readiness_derivative_output_id=_hex())
            )
    engine.dispose()

    command.downgrade(configuration, _PREVIOUS)
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    inspector = inspect(engine)
    assert "media_profile" not in {
        column["name"] for column in inspector.get_columns("finished_set_archives")
    }
    item_columns = {column["name"] for column in inspector.get_columns("mega_set_delivery_items")}
    assert "source_derivative_output_id" in item_columns
    assert "source_asset_id" not in item_columns
    assert "readiness_derivative_output_id" not in item_columns
    metadata = MetaData()
    metadata.reflect(bind=engine, only=["mega_set_delivery_items"])
    items = metadata.tables["mega_set_delivery_items"]
    with engine.connect() as connection:
        restored = (
            connection.execute(select(items).where(items.c.id == identities["item_id"]))
            .mappings()
            .one()
        )
        assert (
            _uuid_hex(restored["source_derivative_output_id"]) == identities["derivative_output_id"]
        )
    engine.dispose()


def test_media_profile_downgrade_refuses_two_profiles_before_ddl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "finished-set-media-profile-preflight.db"
    configuration = _configuration(database_path, monkeypatch)
    command.upgrade(configuration, _PREVIOUS)
    identities = _seed_legacy_archive_and_mega_item(database_path)
    command.upgrade(configuration, _MIGRATION)
    _insert_archive(
        database_path,
        review_task_id=identities["review_task_id"],
        release_version_id=identities["release_version_id"],
        media_profile=_PUBLIC_PROFILE,
    )

    with pytest.raises(
        RuntimeError,
        match="cannot downgrade finished-set media profiles without losing",
    ):
        command.downgrade(configuration, _PREVIOUS)

    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    inspector = inspect(engine)
    assert "media_profile" in {
        column["name"] for column in inspector.get_columns("finished_set_archives")
    }
    assert {"source_asset_id", "readiness_derivative_output_id"} <= {
        column["name"] for column in inspector.get_columns("mega_set_delivery_items")
    }
    engine.dispose()


def test_media_profile_downgrade_refuses_a_single_public_png_archive_before_ddl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "finished-set-public-profile-preflight.db"
    configuration = _configuration(database_path, monkeypatch)
    command.upgrade(configuration, _PREVIOUS)
    identities = _seed_legacy_archive_and_mega_item(database_path)
    command.upgrade(configuration, _MIGRATION)

    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    metadata = MetaData()
    metadata.reflect(bind=engine, only=["finished_set_archives"])
    archives = metadata.tables["finished_set_archives"]
    with engine.begin() as connection:
        connection.execute(
            archives.update()
            .where(archives.c.id == identities["archive_id"])
            .values(media_profile=_PUBLIC_PROFILE)
        )
    engine.dispose()

    with pytest.raises(
        RuntimeError,
        match="cannot downgrade finished-set media profiles",
    ):
        command.downgrade(configuration, _PREVIOUS)

    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    inspector = inspect(engine)
    assert "media_profile" in {
        column["name"] for column in inspector.get_columns("finished_set_archives")
    }
    assert {"source_asset_id", "readiness_derivative_output_id"} <= {
        column["name"] for column in inspector.get_columns("mega_set_delivery_items")
    }
    with engine.connect() as connection:
        assert (
            connection.scalar(
                select(archives.c.media_profile).where(archives.c.id == identities["archive_id"])
            )
            == _PUBLIC_PROFILE
        )
    engine.dispose()


def test_media_profile_has_no_insert_default_and_compound_uniqueness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "finished-set-media-profile-contract.db"
    configuration = _configuration(database_path, monkeypatch)
    command.upgrade(configuration, _PREVIOUS)
    identities = _seed_legacy_archive_and_mega_item(database_path)
    command.upgrade(configuration, _MIGRATION)

    public_id = _insert_archive(
        database_path,
        review_task_id=identities["review_task_id"],
        release_version_id=identities["release_version_id"],
        media_profile=_PUBLIC_PROFILE,
    )
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    metadata = MetaData()
    metadata.reflect(bind=engine, only=["finished_set_archives"])
    archives = metadata.tables["finished_set_archives"]
    with engine.connect() as connection:
        profiles = set(
            connection.execute(
                select(archives.c.media_profile).where(
                    archives.c.review_task_id == identities["review_task_id"]
                )
            ).scalars()
        )
        assert profiles == {_LEGACY_PROFILE, _PUBLIC_PROFILE}
        assert _uuid_hex(
            connection.scalar(select(archives.c.id).where(archives.c.id == public_id))
        ) == _uuid_hex(public_id)
    engine.dispose()

    with pytest.raises(IntegrityError):
        _insert_archive(
            database_path,
            review_task_id=_hex(),
            release_version_id=identities["release_version_id"],
            media_profile=None,
        )
    with pytest.raises(IntegrityError):
        _insert_archive(
            database_path,
            review_task_id=identities["review_task_id"],
            release_version_id=identities["release_version_id"],
            media_profile=_PUBLIC_PROFILE,
        )


def test_media_profile_migration_postgresql_provenance_guards_are_symmetric(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configuration = Config("alembic.ini")
    revision = ScriptDirectory.from_config(configuration).get_revision(_MIGRATION)
    assert revision is not None

    statements: list[str] = []
    monkeypatch.setattr(
        revision.module.op,
        "get_bind",
        lambda: SimpleNamespace(dialect=SimpleNamespace(name="postgresql")),
    )
    monkeypatch.setattr(
        revision.module.op,
        "execute",
        lambda statement: statements.append(str(statement)),
    )

    revision.module._create_mega_item_identity_guards("postgresql")

    guard = next(
        statement
        for statement in statements
        if "gen_automation_guard_mega_set_delivery_item_mutation" in statement
        and "CREATE OR REPLACE FUNCTION" in statement
    )
    assert "OLD.source_asset_id IS DISTINCT FROM NEW.source_asset_id" in guard
    assert (
        "OLD.readiness_derivative_output_id IS DISTINCT FROM NEW.readiness_derivative_output_id"
    ) in " ".join(guard.split())
    assert any(
        "CREATE TRIGGER mega_set_delivery_items_guard_mutation" in statement
        for statement in statements
    )

    statements.clear()
    revision.module._drop_mega_item_identity_guards("postgresql")
    assert statements == [
        "DROP TRIGGER IF EXISTS mega_set_delivery_items_guard_mutation ON mega_set_delivery_items",
        "DROP FUNCTION IF EXISTS gen_automation_guard_mega_set_delivery_item_mutation()",
    ]

    statements.clear()
    revision.module._create_legacy_mega_item_identity_guards("postgresql")
    legacy_guard = next(
        statement
        for statement in statements
        if "gen_automation_guard_mega_set_delivery_item_mutation" in statement
        and "CREATE OR REPLACE FUNCTION" in statement
    )
    assert (
        "OLD.source_derivative_output_id IS DISTINCT FROM NEW.source_derivative_output_id"
    ) in " ".join(legacy_guard.split())
    assert "source_asset_id" not in legacy_guard
    assert "readiness_derivative_output_id" not in legacy_guard
