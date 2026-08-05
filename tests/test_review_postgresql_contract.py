import os
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from gen_automation.db.session import Database
from gen_automation.services.review import _accepted_release_selection_sources_statement

POSTGRESQL_URL = os.getenv("GEN_AUTOMATION_DATABASE_URL", "")


def test_release_selection_lock_is_scoped_away_from_grouped_subquery() -> None:
    statement = _accepted_release_selection_sources_statement(
        review_task_id=uuid4(),
        scoring_run_id=uuid4(),
    )

    sql = str(statement.compile(dialect=postgresql.dialect()))

    assert " GROUP BY " in sql
    assert sql.endswith("FOR UPDATE OF review_decisions, asset_rankings, assets")


@pytest.mark.skipif(
    not POSTGRESQL_URL.startswith("postgresql"),
    reason="requires the PostgreSQL contract database",
)
@pytest.mark.asyncio
async def test_release_selection_lock_executes_on_postgresql() -> None:
    database = Database(POSTGRESQL_URL)
    try:
        async with database.sessions() as session:
            result = await session.execute(
                _accepted_release_selection_sources_statement(
                    review_task_id=uuid4(),
                    scoring_run_id=uuid4(),
                )
            )
            assert result.all() == []
            await session.rollback()
    finally:
        await database.dispose()
