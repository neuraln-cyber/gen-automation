import os
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from gen_automation.db.session import Database
from gen_automation.domain.enums import AdminRole, ReviewBulkAction
from gen_automation.services.review import (
    _accepted_release_selection_sources_statement,
    _latest_review_decisions_for_update_statement,
    apply_bulk_review_action,
    create_review_task,
    get_review_summary,
)
from tests.test_review_api import _seed_review_api, _settings

POSTGRESQL_URL = os.getenv("GEN_AUTOMATION_DATABASE_URL", "")


def test_release_selection_lock_is_scoped_away_from_grouped_subquery() -> None:
    statement = _accepted_release_selection_sources_statement(
        review_task_id=uuid4(),
        scoring_run_id=uuid4(),
    )

    sql = str(statement.compile(dialect=postgresql.dialect()))

    assert " GROUP BY " in sql
    assert sql.endswith("FOR UPDATE OF review_decisions, asset_rankings, assets")


def test_bulk_decision_lock_is_scoped_away_from_grouped_subquery() -> None:
    statement = _latest_review_decisions_for_update_statement(
        review_task_id=uuid4(),
        asset_ids=(uuid4(), uuid4()),
    )

    sql = str(statement.compile(dialect=postgresql.dialect()))

    assert " GROUP BY " in sql
    assert sql.endswith("FOR UPDATE OF review_decisions")


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


@pytest.mark.skipif(
    not POSTGRESQL_URL.startswith("postgresql"),
    reason="requires the PostgreSQL contract database",
)
@pytest.mark.asyncio
async def test_bulk_review_decisions_execute_on_postgresql() -> None:
    settings = _settings(Path("unused-postgresql-review.db")).model_copy(
        update={"database_url": POSTGRESQL_URL}
    )
    context = await _seed_review_api(settings, create_schema=False)
    database = Database(POSTGRESQL_URL)
    try:
        async with database.sessions() as session:
            owner_id = context.users[AdminRole.OWNER].id
            task = await create_review_task(
                session,
                scoring_run_id=context.scoring_run_id,
                created_by_user_id=owner_id,
                idempotency_key="postgres-review-task-v1",
            )
            excluded = await apply_bulk_review_action(
                session,
                review_task_id=task.task_id,
                asset_ids=context.asset_ids,
                action=ReviewBulkAction.REJECT,
                changed_by_user_id=owner_id,
                expected_lock_version=task.lock_version,
                idempotency_key="postgres-bulk-exclude-v1",
            )
            accepted = await apply_bulk_review_action(
                session,
                review_task_id=task.task_id,
                asset_ids=context.asset_ids,
                action=ReviewBulkAction.ACCEPT,
                changed_by_user_id=owner_id,
                expected_lock_version=excluded.task_lock_version,
                idempotency_key="postgres-bulk-accept-v1",
            )
            excluded_again = await apply_bulk_review_action(
                session,
                review_task_id=task.task_id,
                asset_ids=context.asset_ids,
                action=ReviewBulkAction.REJECT,
                changed_by_user_id=owner_id,
                expected_lock_version=accepted.task_lock_version,
                idempotency_key="postgres-bulk-exclude-v2",
            )
            summary = await get_review_summary(session, review_task_id=task.task_id)

        assert excluded.changed_count == len(context.asset_ids)
        assert accepted.changed_count == len(context.asset_ids)
        assert excluded_again.changed_count == len(context.asset_ids)
        assert summary.rejected_count == len(context.asset_ids)
        assert summary.lock_version == 4
    finally:
        await database.dispose()
