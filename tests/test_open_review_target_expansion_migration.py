import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text


def _trigger_sql(database_url: str) -> str:
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            return str(
                connection.execute(
                    text(
                        "SELECT sql FROM sqlite_master "
                        "WHERE type = 'trigger' AND name = 'review_tasks_guard_update'"
                    )
                ).scalar_one()
            )
    finally:
        engine.dispose()


def _normalize(statement: str) -> str:
    return re.sub(r"\s+", " ", statement).strip()


def test_open_review_target_expansion_migration_replaces_and_restores_sqlite_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "review-target-expansion.db"
    async_url = f"sqlite+aiosqlite:///{database_path.as_posix()}"
    sync_url = f"sqlite:///{database_path.as_posix()}"
    monkeypatch.setenv("GEN_AUTOMATION_DATABASE_URL", async_url)
    configuration = Config("alembic.ini")

    command.upgrade(configuration, "20260818_0038")
    old_guard = _trigger_sql(sync_url)
    command.upgrade(configuration, "20260822_0039")
    new_guard = _normalize(_trigger_sql(sync_url))

    assert "NEW.state IS 'open'" in new_guard
    assert "NEW.desired_accepted_count < OLD.desired_accepted_count" in new_guard
    assert "NEW.desired_accepted_count > OLD.ranked_asset_count" in new_guard
    assert "open review target expansion is invalid" in new_guard

    command.downgrade(configuration, "20260818_0038")
    restored_guard = _normalize(_trigger_sql(sync_url))
    assert "NEW.state IS NOT 'completed'" in restored_guard
    assert "may shrink only on completion" in restored_guard
    assert "open review target expansion is invalid" not in restored_guard
    assert "acceptance target is not satisfied" in restored_guard
    assert _normalize(old_guard).count("acceptance target is not satisfied") == 1


def test_open_review_target_expansion_migration_replaces_postgresql_function(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configuration = Config("alembic.ini")
    revision = ScriptDirectory.from_config(configuration).get_revision("20260822_0039")
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

    revision.module.upgrade()
    upgraded = _normalize(statements[-1])
    assert "NEW.state = 'open'" in upgraded
    assert "NEW.desired_accepted_count > OLD.ranked_asset_count" in upgraded
    assert "open review target expansion is invalid" in upgraded

    statements.clear()
    revision.module.downgrade()
    downgraded = _normalize(statements[-1])
    assert "NEW.state <> 'completed'" in downgraded
    assert "may shrink only on completion" in downgraded
