from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

_PREVIOUS = "20260822_0039"
_MIGRATION = "20260827_0040"


def test_artifact_usage_classifications_become_informational(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "owner-controlled-artifact-usage.db"
    monkeypatch.setenv(
        "GEN_AUTOMATION_DATABASE_URL",
        f"sqlite+aiosqlite:///{database_path.as_posix()}",
    )
    configuration = Config("alembic.ini")
    command.upgrade(configuration, _PREVIOUS)

    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    artifact_id = uuid4().hex
    actor_id = uuid4().hex
    now = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO model_artifact_approvals (
                    artifact_sha256, name, kind, model_family, source_url, storage_key,
                    license_url, commercial_use_approved, experiment_only,
                    adult_use_approved, safetensors_verified, evidence, evidence_sha256,
                    status, is_current, approval_version, approved_by_user_id, approved_at,
                    revoked_by_user_id, revoked_at, id, created_at, updated_at
                ) VALUES (
                    :sha256, 'Owner selected LoRA', 'lora', 'anima',
                    'https://example.test/lora', 'models/owner-selected.safetensors',
                    'https://example.test/license', true, false, true, true, '{}',
                    :evidence_sha256, 'approved', true, 1, :actor_id, :now,
                    NULL, NULL, :id, :now, :now
                )
                """
            ),
            {
                "sha256": "a" * 64,
                "evidence_sha256": "b" * 64,
                "actor_id": actor_id,
                "id": artifact_id,
                "now": now,
            },
        )

    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE model_artifact_approvals "
                "SET commercial_use_approved = false, experiment_only = false "
                "WHERE id = :id"
            ),
            {"id": artifact_id},
        )

    command.upgrade(configuration, _MIGRATION)
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE model_artifact_approvals "
                "SET commercial_use_approved = false, experiment_only = false "
                "WHERE id = :id"
            ),
            {"id": artifact_id},
        )
    engine.dispose()
