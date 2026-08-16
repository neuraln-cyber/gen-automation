from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import MetaData, create_engine, inspect
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError

_PREVIOUS = "20260811_0036"
_MIGRATION = "20260812_0037"
_NOW = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)
_TABLES = {
    "i2v_inputs",
    "i2v_presets",
    "i2v_jobs",
    "i2v_attempts",
    "i2v_outputs",
    "i2v_worker_deployments",
}


def _configuration(database_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    monkeypatch.setenv(
        "GEN_AUTOMATION_DATABASE_URL",
        f"sqlite+aiosqlite:///{database_path.as_posix()}",
    )
    configuration = Config("alembic.ini")
    # Materialize the parsed options before clearing the filename so the
    # migration keeps its script location without env.py re-running fileConfig.
    _ = configuration.file_config
    # Alembic's env.py applies fileConfig when this is set, which removes
    # pytest's live log-capture handler and pollutes every later test. The
    # script location and all parsed options remain available without it.
    configuration.config_file_name = None
    return configuration


def test_fresh_i2v_revision_isolated_and_uses_jsonb_on_postgresql() -> None:
    revision = ScriptDirectory.from_config(Config("alembic.ini")).get_revision(_MIGRATION)
    assert revision is not None
    assert revision.down_revision == _PREVIOUS
    assert isinstance(
        revision.module.json_type.dialect_impl(postgresql.dialect()),
        postgresql.JSONB,
    )


def test_fresh_i2v_migration_round_trip_preserves_legacy_video_tables(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "fresh-i2v-round-trip.db"
    configuration = _configuration(database_path, monkeypatch)
    command.upgrade(configuration, _PREVIOUS)

    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    before = set(inspect(engine).get_table_names())
    assert "video_generation_jobs" in before
    assert not (_TABLES & before)
    engine.dispose()

    command.upgrade(configuration, _MIGRATION)
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    after = set(inspect(engine).get_table_names())
    assert _TABLES <= after
    assert "video_generation_jobs" in after
    job_columns = {column["name"] for column in inspect(engine).get_columns("i2v_jobs")}
    assert (
        not {
            "content_rating",
            "license_policy",
            "character_policy",
            "source_rights_confirmed",
            "lawful_use_confirmed",
            "max_attempts",
        }
        & job_columns
    )
    with engine.connect() as connection:
        triggers = {
            row[0]
            for row in connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'i2v_jobs'"
            )
        }
    assert "i2v_jobs_immutable_request" in triggers
    engine.dispose()

    command.downgrade(configuration, _PREVIOUS)
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    downgraded = set(inspect(engine).get_table_names())
    assert not (_TABLES & downgraded)
    assert "video_generation_jobs" in downgraded
    engine.dispose()


def test_i2v_queue_has_immutable_snapshots_without_generation_ceilings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "fresh-i2v-contract.db"
    configuration = _configuration(database_path, monkeypatch)
    command.upgrade(configuration, _MIGRATION)
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    metadata = MetaData()
    metadata.reflect(bind=engine, only=sorted(_TABLES))
    inputs = metadata.tables["i2v_inputs"]
    presets = metadata.tables["i2v_presets"]
    jobs = metadata.tables["i2v_jobs"]

    actor_id = uuid4().hex
    input_id = uuid4().hex
    preset_id = uuid4().hex
    job_id = uuid4().hex
    long_positive_prompt = "smooth motion, " * 2_000
    settings = {
        "frame_count": 100_001,
        "runtime_seconds": 999_999,
        "width": 8_192,
        "height": 8_192,
        "custom_workflow": {"anything": [1, 2, 3]},
    }
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys = OFF")
        connection.execute(
            inputs.insert(),
            {
                "id": input_id,
                "created_by_user_id": actor_id,
                "source": "upload",
                "asset_id": None,
                "display_name": "Source image",
                "storage_backend": "s3",
                "storage_bucket": "private-inputs",
                "object_key": "i2v/source.png",
                "object_version_id": "v1",
                "sha256": "a" * 64,
                "content_type": "image/png",
                "width": 4096,
                "height": 4096,
                "byte_size": 123_456_789,
                "metadata": {},
                "created_at": _NOW,
            },
        )
        connection.execute(
            presets.insert(),
            {
                "id": preset_id,
                "created_by_user_id": actor_id,
                "name": "Unbounded workflow",
                "description": "",
                "positive_prompt": long_positive_prompt,
                "negative_prompt": "",
                "settings": settings,
                "lock_version": 1,
                "created_at": _NOW,
                "updated_at": _NOW,
            },
        )
        connection.execute(
            jobs.insert(),
            {
                "id": job_id,
                "created_by_user_id": actor_id,
                "input_id": input_id,
                "preset_id": preset_id,
                "positive_prompt": long_positive_prompt,
                "negative_prompt": "",
                "input_snapshot": {"input_id": input_id, "sha256": "a" * 64},
                "preset_snapshot": {"preset_id": preset_id, "settings": settings},
                "settings_snapshot": settings,
                "request_sha256": "b" * 64,
                "state": "queued",
                "queue_position": 1,
                "attempt_count": 0,
                "lease_owner": None,
                "lease_expires_at": None,
                "cancel_requested_at": None,
                "completed_at": None,
                "last_error_code": None,
                "last_error_detail": None,
                "created_at": _NOW,
                "updated_at": _NOW,
            },
        )

    with engine.begin() as connection:
        with pytest.raises(IntegrityError, match="request snapshots are immutable"):
            connection.execute(
                jobs.update().where(jobs.c.id == job_id).values(settings_snapshot={"steps": 1})
            )
    with engine.begin() as connection:
        connection.execute(
            jobs.update()
            .where(jobs.c.id == job_id)
            .values(
                state="failed",
                queue_position=None,
                completed_at=_NOW,
                last_error_code="worker_error",
            )
        )
    engine.dispose()


def test_i2v_database_rejects_policy_free_but_structurally_invalid_media(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "fresh-i2v-invalid.db"
    configuration = _configuration(database_path, monkeypatch)
    command.upgrade(configuration, _MIGRATION)
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    metadata = MetaData()
    metadata.reflect(bind=engine, only=["i2v_inputs"])
    inputs = metadata.tables["i2v_inputs"]
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys = OFF")
        with pytest.raises(IntegrityError):
            connection.execute(
                inputs.insert(),
                {
                    "id": uuid4().hex,
                    "created_by_user_id": uuid4().hex,
                    "source": "upload",
                    "asset_id": None,
                    "display_name": "Not an image",
                    "storage_backend": "s3",
                    "storage_bucket": "private-inputs",
                    "object_key": "bad.bin",
                    "object_version_id": None,
                    "sha256": "not-a-sha",
                    "content_type": "application/octet-stream",
                    "width": 0,
                    "height": 0,
                    "byte_size": 0,
                    "metadata": {},
                    "created_at": _NOW,
                },
            )
    engine.dispose()
