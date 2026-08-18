from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

_PREVIOUS = "20260812_0037"
_MIGRATION = "20260818_0038"
_NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)


def _configuration(database_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    monkeypatch.setenv(
        "GEN_AUTOMATION_DATABASE_URL",
        f"sqlite+aiosqlite:///{database_path.as_posix()}",
    )
    return Config("alembic.ini")


def test_model_family_migration_backfills_and_round_trips(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "txt2img-model-families.db"
    configuration = _configuration(database_path, monkeypatch)
    command.upgrade(configuration, _PREVIOUS)

    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    artifact_id = uuid4().hex
    workflow_id = uuid4().hex
    actor_id = uuid4().hex
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO model_artifact_approvals (
                    artifact_sha256, name, kind, source_url, storage_key, license_url,
                    commercial_use_approved, adult_use_approved, safetensors_verified,
                    evidence, evidence_sha256, status, is_current, approval_version,
                    approved_by_user_id, approved_at, revoked_by_user_id, revoked_at,
                    id, created_at, updated_at
                ) VALUES (
                    :sha256, 'Existing checkpoint', 'checkpoint', 'https://example.test/model',
                    'models/existing.safetensors', 'https://example.test/license',
                    1, 1, 1, '{}', :evidence_sha256, 'approved', 1, 1,
                    :actor_id, :now, NULL, NULL, :id, :now, :now
                )
                """
            ),
            {
                "sha256": "a" * 64,
                "evidence_sha256": "b" * 64,
                "actor_id": actor_id,
                "id": artifact_id,
                "now": _NOW,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO workflow_approvals (
                    workflow_sha256, name, version, object_key, reviewed_node_classes,
                    capabilities, evidence, evidence_sha256, status, is_current,
                    approval_version, approved_by_user_id, approved_at, revoked_by_user_id,
                    revoked_at, id, created_at, updated_at
                ) VALUES (
                    :sha256, 'Existing workflow', '1', 'workflows/existing.json', '[]',
                    '[]', '{}', :evidence_sha256, 'approved', 1, 1, :actor_id, :now,
                    NULL, NULL, :id, :now, :now
                )
                """
            ),
            {
                "sha256": "c" * 64,
                "evidence_sha256": "d" * 64,
                "actor_id": actor_id,
                "id": workflow_id,
                "now": _NOW,
            },
        )

    command.upgrade(configuration, _MIGRATION)
    inspector = inspect(engine)
    expected_family_checks = {
        "model_artifact_approvals": ("ck_model_artifact_approvals_generation_model_family"),
        "workflow_approvals": ("ck_workflow_approvals_workflow_generation_model_family"),
    }
    for table_name, expected_check in expected_family_checks.items():
        columns = {column["name"] for column in inspector.get_columns(table_name)}
        assert "model_family" in columns
        checks = {check["name"] for check in inspector.get_check_constraints(table_name)}
        assert expected_check in checks
    artifact_columns = {
        column["name"] for column in inspector.get_columns("model_artifact_approvals")
    }
    assert "experiment_only" in artifact_columns
    warm_columns = {column["name"] for column in inspector.get_columns("experiment_warm_leases")}
    assert {"requested_checkpoint_sha256", "requested_lora_sha256s"}.issubset(warm_columns)
    with engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT model_family FROM model_artifact_approvals WHERE id = :id"),
                {"id": artifact_id},
            ).scalar_one()
            == "illustrious"
        )
        assert (
            connection.execute(
                text("SELECT model_family FROM workflow_approvals WHERE id = :id"),
                {"id": workflow_id},
            ).scalar_one()
            == "illustrious"
        )
        assert (
            connection.execute(
                text("SELECT experiment_only FROM model_artifact_approvals WHERE id = :id"),
                {"id": artifact_id},
            ).scalar_one()
            == 0
        )

    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            text("UPDATE model_artifact_approvals SET model_family = 'unsupported' WHERE id = :id"),
            {"id": artifact_id},
        )

    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE model_artifact_approvals SET commercial_use_approved = false, "
                "experiment_only = false WHERE id = :id"
            ),
            {"id": artifact_id},
        )

    with engine.connect() as connection:
        transaction = connection.begin()
        connection.execute(
            text(
                "UPDATE model_artifact_approvals SET commercial_use_approved = false, "
                "experiment_only = true WHERE id = :id"
            ),
            {"id": artifact_id},
        )
        assert (
            connection.execute(
                text("SELECT experiment_only FROM model_artifact_approvals WHERE id = :id"),
                {"id": artifact_id},
            ).scalar_one()
            == 1
        )
        transaction.rollback()

    command.downgrade(configuration, _PREVIOUS)
    inspector = inspect(engine)
    for table_name in ("model_artifact_approvals", "workflow_approvals"):
        columns = {column["name"] for column in inspector.get_columns(table_name)}
        assert "model_family" not in columns
    assert "experiment_only" not in {
        column["name"] for column in inspector.get_columns("model_artifact_approvals")
    }
    assert not {
        "requested_checkpoint_sha256",
        "requested_lora_sha256s",
    }.intersection(column["name"] for column in inspector.get_columns("experiment_warm_leases"))
    engine.dispose()
