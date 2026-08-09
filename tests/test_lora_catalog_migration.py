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

_PREVIOUS = "20260809_0032"
_MIGRATION = "20260809_0033"
_NOW = datetime(2026, 8, 9, 15, 0, tzinfo=UTC)


def _configuration(database_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    monkeypatch.setenv(
        "GEN_AUTOMATION_DATABASE_URL",
        f"sqlite+aiosqlite:///{database_path.as_posix()}",
    )
    return Config("alembic.ini")


def _job_values() -> dict[str, object]:
    return {
        "id": uuid4().hex,
        "source_type": "civitai",
        "state": "queued",
        "display_name": "Migration fixture",
        "canonical_source_url": "https://civitai.com/models/1234/fixture",
        "license_url": "https://civitai.com/models/1234/fixture",
        "commercial_use_attested": True,
        "adult_use_attested": True,
        "civitai_model_id": 1234,
        "civitai_version_id": 5678,
        "civitai_file_id": 9012,
        "staging_bucket": None,
        "staging_object_key": None,
        "staging_object_version_id": None,
        "staging_object_etag": None,
        "staging_byte_size": None,
        "target_filename": "fixture.safetensors",
        "expected_sha256": None,
        "expected_byte_size": None,
        "expected_metadata": {},
        "trigger_words": [],
        "progress_bytes": 0,
        "total_bytes": None,
        "attempts": 0,
        "max_attempts": 3,
        "available_at": _NOW,
        "lease_owner": None,
        "lease_expires_at": None,
        "last_error_code": None,
        "last_error_detail": None,
        "result_artifact_id": None,
        "requested_by_user_id": uuid4().hex,
        "cancelled_by_user_id": None,
        "lock_version": 1,
        "started_at": None,
        "last_progress_at": None,
        "completed_at": None,
        "created_at": _NOW,
        "updated_at": _NOW,
    }


def test_lora_catalog_uses_jsonb_on_postgresql() -> None:
    revision = ScriptDirectory.from_config(Config("alembic.ini")).get_revision(_MIGRATION)
    assert revision is not None
    assert isinstance(
        revision.module.json_type.dialect_impl(postgresql.dialect()),
        postgresql.JSONB,
    )


def test_lora_catalog_empty_migration_round_trip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "lora-catalog-round-trip.db"
    configuration = _configuration(database_path, monkeypatch)
    command.upgrade(configuration, _PREVIOUS)
    command.upgrade(configuration, _MIGRATION)

    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    inspector = inspect(engine)
    assert {"managed_lora_artifacts", "lora_import_jobs"} <= set(inspector.get_table_names())
    salad_columns = {column["name"] for column in inspector.get_columns("salad_deployments")}
    assert "runtime_artifact_manifest_sha256" in salad_columns
    assert "runtime_managed_lora_sha256s" in salad_columns
    salad_checks = {
        check["name"]: check["sqltext"]
        for check in inspector.get_check_constraints("salad_deployments")
    }
    manifest_check = salad_checks["ck_salad_deployments_valid_runtime_artifact_manifest_sha256"]
    assert "length(runtime_artifact_manifest_sha256) = 64" in manifest_check
    assert "replace(" in manifest_check

    with engine.connect() as connection:
        triggers = {
            row[0]
            for row in connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' "
                "AND tbl_name IN ('managed_lora_artifacts', 'lora_import_jobs')"
            )
        }
        live_unique_indexes = {
            row[0]: row[1]
            for row in connection.exec_driver_sql(
                "SELECT name, sql FROM sqlite_master WHERE type = 'index' "
                "AND tbl_name = 'managed_lora_artifacts' "
                "AND name LIKE 'uq_managed_lora_artifacts_live_%'"
            )
        }
    assert {
        "managed_lora_artifacts_guard_update",
        "managed_lora_artifacts_reject_delete",
        "lora_import_jobs_guard_update",
        "lora_import_jobs_reject_delete",
    } <= triggers
    assert set(live_unique_indexes) == {
        "uq_managed_lora_artifacts_live_sha256",
        "uq_managed_lora_artifacts_live_approval",
        "uq_managed_lora_artifacts_live_target_filename",
    }
    assert all(
        "UNIQUE INDEX" in sql and "WHERE lifecycle <> 'purged'" in sql
        for sql in live_unique_indexes.values()
    )
    engine.dispose()

    command.downgrade(configuration, _PREVIOUS)
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    inspector = inspect(engine)
    assert "managed_lora_artifacts" not in inspector.get_table_names()
    assert "lora_import_jobs" not in inspector.get_table_names()
    assert "runtime_artifact_manifest_sha256" not in {
        column["name"] for column in inspector.get_columns("salad_deployments")
    }
    assert "runtime_managed_lora_sha256s" not in {
        column["name"] for column in inspector.get_columns("salad_deployments")
    }
    engine.dispose()


def test_lora_catalog_constraints_guards_and_populated_downgrade_refusal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "lora-catalog-guards.db"
    configuration = _configuration(database_path, monkeypatch)
    command.upgrade(configuration, _MIGRATION)
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    metadata = MetaData()
    metadata.reflect(
        bind=engine,
        only=["managed_lora_artifacts", "lora_import_jobs"],
    )
    artifacts = metadata.tables["managed_lora_artifacts"]
    jobs = metadata.tables["lora_import_jobs"]

    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys = OFF")
        connection.commit()
        invalid_sha = "g" * 64
        with pytest.raises(IntegrityError):
            connection.execute(
                artifacts.insert(),
                {
                    "id": uuid4().hex,
                    "artifact_sha256": invalid_sha,
                    "display_name": "Invalid hash",
                    "source_type": "manual",
                    "canonical_source_url": "https://models.example.test/invalid",
                    "license_url": "https://models.example.test/invalid/license",
                    "civitai_model_id": None,
                    "civitai_version_id": None,
                    "civitai_file_id": None,
                    "provenance": {},
                    "storage_bucket": "model-bucket",
                    "object_key": (f"worker/managed-loras/sha256/{invalid_sha}.safetensors"),
                    "object_version_id": "version-1",
                    "object_etag": "a" * 32,
                    "byte_size": 1_024,
                    "target_filename": "invalid.safetensors",
                    "approval_id": uuid4().hex,
                    "trigger_words": [],
                    "lifecycle": "pending_activation",
                    "purge_requested": False,
                    "registered_by_user_id": uuid4().hex,
                    "retirement_requested_by_user_id": None,
                    "restored_by_user_id": None,
                    "activated_at": None,
                    "retirement_requested_at": None,
                    "retired_at": None,
                    "restored_at": None,
                    "purged_at": None,
                    "lock_version": 1,
                    "created_at": _NOW,
                    "updated_at": _NOW,
                },
            )
        connection.rollback()
        connection.execute(jobs.insert(), _job_values())
        connection.commit()
        with pytest.raises(IntegrityError, match="LoRA import jobs cannot be deleted"):
            connection.execute(jobs.delete())
        connection.rollback()
    engine.dispose()

    with pytest.raises(
        RuntimeError,
        match="cannot downgrade after durable LoRA catalog data exists",
    ):
        command.downgrade(configuration, _PREVIOUS)
