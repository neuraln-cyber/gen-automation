import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from gen_automation.db.models import GenerationJob, Project, Release, ReleaseVersion
from gen_automation.db.session import Database
from gen_automation.domain.enums import AdminRole, GenerationState, ReleasePhase, ReviewBulkAction
from gen_automation.services.quality import create_manual_review_run
from gen_automation.services.ranking_manifest import (
    load_ranking_manifest_rows,
    validate_completed_ranking_manifest,
)
from gen_automation.services.review import (
    _accepted_release_selection_sources_statement,
    _latest_review_decisions_for_update_statement,
    apply_bulk_review_action,
    create_review_task,
    get_review_summary,
)
from tests.test_review_api import _raw_asset, _seed_review_api, _settings

POSTGRESQL_URL = os.getenv("GEN_AUTOMATION_DATABASE_URL", "")


@pytest.mark.asyncio
async def test_manual_review_freezes_without_analysis(tmp_path: Path) -> None:
    from gen_automation.db.models import ScoringRun

    postgres = POSTGRESQL_URL.startswith("postgresql")
    database = Database(
        POSTGRESQL_URL if postgres else f"sqlite+aiosqlite:///{(tmp_path / 'manual.db').as_posix()}"
    )
    if not postgres:
        await database.create_schema()
    try:
        async with database.sessions() as session:
            project = Project(slug="manual-contract", name="Manual contract")
            session.add(project)
            await session.flush()
            release = Release(
                project_id=project.id,
                slug="manual",
                title="Manual",
                phase=ReleasePhase.REVIEWING,
                current_version_no=1,
                desired_accepted_count=1,
            )
            session.add(release)
            await session.flush()
            version = ReleaseVersion(
                release_id=release.id,
                version_no=1,
                specification={},
                specification_sha256="a" * 64,
                created_by="test",
                created_at=datetime.now(UTC),
            )
            session.add(version)
            await session.flush()
            job = GenerationJob(
                release_version_id=version.id,
                logical_key="b" * 64,
                parameters={},
                parameters_sha256="c" * 64,
                state=GenerationState.SUCCEEDED,
                expected_output_count=1,
            )
            session.add(job)
            await session.flush()
            asset = _raw_asset(
                asset_id=uuid4(),
                release_id=release.id,
                job_id=job.id,
                output_index=0,
            )
            asset.object_key = f"manual-contract/{asset.id}.png"
            session.add(asset)
            await session.commit()
            result = await create_manual_review_run(session, release_version_id=version.id)
            run = await session.get(ScoringRun, result.run_id)
            rows = await load_ranking_manifest_rows(session, result.run_id)
            assert run is not None
            validate_completed_ranking_manifest(run, rows)
            assert rows[0][1].state.value == "skipped"
            assert rows[0][1].dhash_hex is None
            assert (
                await create_manual_review_run(
                    session,
                    release_version_id=version.id,
                )
            ).replayed
    finally:
        await database.dispose()


def test_release_selection_lock_is_scoped_away_from_grouped_subquery() -> None:
    statement = _accepted_release_selection_sources_statement(
        review_task_id=uuid4(),
        scoring_run_id=uuid4(),
    )

    sql = str(statement.compile(dialect=postgresql.dialect()))

    assert " GROUP BY " in sql
    assert sql.endswith("FOR UPDATE OF review_decisions, asset_rankings, assets, generation_jobs")


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
