from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text


def test_manual_review_migration_preserves_guards_and_reverses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "manual-review.db"
    monkeypatch.setenv("GEN_AUTOMATION_DATABASE_URL", f"sqlite+aiosqlite:///{path.as_posix()}")
    config = Config("alembic.ini")
    command.upgrade(config, "20260905_0041")
    engine = create_engine(f"sqlite:///{path.as_posix()}")

    def guards() -> dict[str, str]:
        with engine.connect() as connection:
            return dict(
                connection.execute(text("SELECT name, sql FROM sqlite_master WHERE type='trigger'"))
                .tuples()
                .all()
            )

    try:
        old = guards()
        command.upgrade(config, "head")
        new = guards()
        assert old.keys() == new.keys()
        assert "'skipped'" in new["scoring_runs_validate_completion"]
        assert "'skipped'" in new["asset_scores_guard_frozen_update"]
        checks = {
            c["name"]: c["sqltext"] for c in inspect(engine).get_check_constraints("asset_scores")
        }
        assert "'skipped'" in checks["ck_asset_scores_asset_score_state"]
        assert "dhash_hex IS NULL" in checks["ck_asset_scores_skipped_has_no_measurements"]
        command.downgrade(config, "20260905_0041")
        assert guards() == old
    finally:
        engine.dispose()
