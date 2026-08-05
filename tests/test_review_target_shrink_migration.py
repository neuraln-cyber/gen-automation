import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text


def _trigger_sql(database_url: str, trigger_name: str) -> str:
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            statement = connection.execute(
                text(
                    "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = :trigger_name"
                ),
                {"trigger_name": trigger_name},
            ).scalar_one()
    finally:
        engine.dispose()
    return str(statement)


def _normalize_sql(statement: str) -> str:
    normalized = re.sub(r"\s+", " ", statement).strip()
    return normalized.replace("( ", "(").replace(" )", ")")


def test_atomic_target_shrink_migration_replaces_and_restores_sqlite_guards(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "review-target-shrink.db"
    async_url = f"sqlite+aiosqlite:///{database_path.as_posix()}"
    sync_url = f"sqlite:///{database_path.as_posix()}"
    monkeypatch.setenv("GEN_AUTOMATION_DATABASE_URL", async_url)
    configuration = Config("alembic.ini")

    command.upgrade(configuration, "20260803_0017")
    old_task_guard = _trigger_sql(sync_url, "review_tasks_guard_update")
    old_selection_guard = _trigger_sql(
        sync_url,
        "review_tasks_validate_selection_completion",
    )

    command.upgrade(configuration, "20260805_0018")
    new_task_guard = _normalize_sql(_trigger_sql(sync_url, "review_tasks_guard_update"))
    new_selection_guard = _normalize_sql(
        _trigger_sql(sync_url, "review_tasks_validate_selection_completion")
    )

    assert (
        "OLD.desired_accepted_count IS NOT NEW.desired_accepted_count "
        "AND NEW.state IS NOT 'completed'"
    ) in new_task_guard
    assert "NEW.desired_accepted_count > OLD.desired_accepted_count" in new_task_guard
    assert ") <> NEW.desired_accepted_count" in new_task_guard
    assert new_selection_guard.count(") <> NEW.desired_accepted_count") == 2
    assert ") <> OLD.desired_accepted_count" not in new_selection_guard

    command.downgrade(configuration, "20260803_0017")
    restored_task_guard = _trigger_sql(sync_url, "review_tasks_guard_update")
    restored_selection_guard = _trigger_sql(
        sync_url,
        "review_tasks_validate_selection_completion",
    )

    assert _normalize_sql(restored_task_guard) == _normalize_sql(old_task_guard)
    assert _normalize_sql(restored_selection_guard) == _normalize_sql(old_selection_guard)


def test_atomic_target_shrink_migration_replaces_postgresql_functions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configuration = Config("alembic.ini")
    revision = ScriptDirectory.from_config(configuration).get_revision("20260805_0018")
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

    task_guard = _normalize_sql(statements[0])
    selection_guard = _normalize_sql(statements[1])
    assert (
        "OLD.desired_accepted_count IS DISTINCT FROM NEW.desired_accepted_count "
        "AND NEW.state <> 'completed'"
    ) in task_guard
    assert "NEW.desired_accepted_count > OLD.desired_accepted_count" in task_guard
    assert "accepted_count <> NEW.desired_accepted_count" in task_guard
    assert selection_guard.count(") <> NEW.desired_accepted_count") == 2

    statements.clear()
    revision.module.downgrade()

    task_guard = _normalize_sql(statements[0])
    selection_guard = _normalize_sql(statements[1])
    assert (
        "OR OLD.desired_accepted_count IS DISTINCT FROM NEW.desired_accepted_count"
    ) in task_guard
    assert "accepted_count <> OLD.desired_accepted_count" in task_guard
    assert selection_guard.count(") <> OLD.desired_accepted_count") == 2
