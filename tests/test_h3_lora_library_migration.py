from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect


def test_h3_family_migration_changes_only_artifacts_and_round_trips(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "h3-library.db"
    monkeypatch.setenv("GEN_AUTOMATION_DATABASE_URL", f"sqlite+aiosqlite:///{path.as_posix()}")
    config = Config("alembic.ini")
    command.upgrade(config, "20260925_0043")
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    try:
        checks = inspect(engine).get_check_constraints("model_artifact_approvals")
        assert any("minimax_h3" in item["sqltext"] for item in checks)
        workflows = inspect(engine).get_check_constraints("workflow_approvals")
        assert not any("minimax_h3" in item["sqltext"] for item in workflows)
        command.downgrade(config, "20260909_0042")
        checks = inspect(engine).get_check_constraints("model_artifact_approvals")
        assert not any("minimax_h3" in item["sqltext"] for item in checks)
        command.upgrade(config, "20260925_0043")
    finally:
        engine.dispose()
