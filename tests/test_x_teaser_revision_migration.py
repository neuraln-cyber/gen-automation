from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, create_engine, inspect, select, text

from gen_automation.db.session import Database
from gen_automation.services.x_teaser_revisions import (
    active_x_teaser_outputs,
    x_teaser_revision_status,
)

_MIGRATION = "20260808_0027"
_PREVIOUS = "20260808_0026"
_NOW = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)


def _hex() -> str:
    return uuid4().hex


def _seed_four_legacy_top_left_outputs(database_path: Path) -> tuple[str, str, str]:
    """Insert the exact legacy shape seen on Akali before revision ownership existed."""

    review_task_id = _hex()
    release_version_id = _hex()
    watermark_asset_id = _hex()
    owner_id = _hex()
    scoring_run_id = _hex()
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    metadata = MetaData()
    metadata.reflect(
        bind=engine,
        only=[
            "derivative_jobs",
            "derivative_outputs",
            "derivative_recipes",
            "release_selections",
            "review_tasks",
            "review_x_selections",
        ],
    )
    tasks = metadata.tables["review_tasks"]
    x_selections = metadata.tables["review_x_selections"]
    selections = metadata.tables["release_selections"]
    recipes = metadata.tables["derivative_recipes"]
    jobs = metadata.tables["derivative_jobs"]
    outputs = metadata.tables["derivative_outputs"]
    recipe_id = _hex()
    recipe_configuration = {
        "schema": "derivative-render-recipe/v1",
        "watermark": {"position": "top_left"},
    }
    with engine.begin() as connection:
        guarded_inserts = (
            "release_selections_guard_insert",
            "derivative_recipes_guard_insert",
            "derivative_jobs_guard_insert",
            "derivative_outputs_guard_insert",
        )
        trigger_sql = {
            name: connection.scalar(
                text("SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = :name"),
                {"name": name},
            )
            for name in guarded_inserts
        }
        assert all(trigger_sql.values())
        for name in guarded_inserts:
            connection.exec_driver_sql(f'DROP TRIGGER "{name}"')
        connection.execute(
            tasks.insert().values(
                id=review_task_id,
                release_version_id=release_version_id,
                release_version_no=1,
                release_specification_sha256="1" * 64,
                scoring_run_id=scoring_run_id,
                scoring_config_sha256="2" * 64,
                scoring_input_manifest_sha256="3" * 64,
                desired_accepted_count=4,
                ranked_asset_count=4,
                state="completed",
                lock_version=1,
                created_by_user_id=owner_id,
                created_at=_NOW,
                completed_by_user_id=owner_id,
                completed_at=_NOW,
                ranking_manifest_sha256="4" * 64,
            )
        )
        connection.execute(
            recipes.insert().values(
                id=recipe_id,
                release_version_id=release_version_id,
                logical_key="5" * 64,
                recipe_version=1,
                configuration=recipe_configuration,
                config_sha256="6" * 64,
                output_targets=["x_teaser"],
                expected_output_count=1,
                renderer_version="legacy-renderer",
                pillow_version="12.0.0",
                watermark_asset_id=watermark_asset_id,
                watermark_storage_backend="s3",
                watermark_storage_bucket="legacy-bucket",
                watermark_object_key="watermarks/legacy.png",
                watermark_object_version_id="watermark-version-1",
                watermark_sha256="7" * 64,
                watermark_content_type="image/png",
                watermark_image_format="PNG",
                watermark_width=320,
                watermark_height=160,
                watermark_byte_size=4096,
                created_by_user_id=owner_id,
                created_at=_NOW,
                approved_by_user_id=owner_id,
                approved_at=_NOW,
            )
        )
        for ordinal in range(1, 5):
            selection_id = _hex()
            source_asset_id = _hex()
            job_id = _hex()
            output_asset_id = _hex()
            connection.execute(
                selections.insert().values(
                    id=selection_id,
                    review_task_id=review_task_id,
                    scoring_run_id=scoring_run_id,
                    review_decision_id=_hex(),
                    decision_revision=1,
                    release_version_id=release_version_id,
                    asset_id=source_asset_id,
                    ranking_rank=ordinal,
                    display_order=ordinal,
                    ranking_manifest_sha256="4" * 64,
                    source_storage_backend="s3",
                    source_storage_bucket="legacy-bucket",
                    source_object_key=f"raw/{ordinal}.png",
                    source_object_version_id=f"raw-version-{ordinal}",
                    source_sha256=f"{ordinal}" * 64,
                    source_content_type="image/png",
                    source_image_format="PNG",
                    source_width=1150,
                    source_height=1487,
                    source_byte_size=1_000_000 + ordinal,
                    source_available_at=_NOW,
                    frozen_at=_NOW,
                )
            )
            connection.execute(
                x_selections.insert().values(
                    id=_hex(),
                    review_task_id=review_task_id,
                    asset_id=source_asset_id,
                    selected_by_user_id=owner_id,
                    selected_at=_NOW,
                )
            )
            connection.execute(
                jobs.insert().values(
                    id=job_id,
                    release_selection_id=selection_id,
                    derivative_recipe_id=recipe_id,
                    release_version_id=release_version_id,
                    logical_key=f"{ordinal + 6:x}" * 64,
                    request_payload={
                        "source": {"asset_id": source_asset_id},
                        "output_targets": ["x_teaser"],
                    },
                    request_sha256=f"{ordinal + 1}" * 64,
                    expected_output_count=1,
                    state="succeeded",
                    priority=100,
                    attempt_count=1,
                    max_attempts=3,
                    lock_version=3,
                    available_at=_NOW,
                    requested_at=_NOW,
                    claimed_at=_NOW,
                    processing_started_at=_NOW,
                    completed_at=_NOW,
                )
            )
            connection.execute(
                outputs.insert().values(
                    id=_hex(),
                    derivative_job_id=job_id,
                    release_selection_id=selection_id,
                    derivative_recipe_id=recipe_id,
                    target="x_teaser",
                    asset_id=output_asset_id,
                    source_asset_id=source_asset_id,
                    asset_lineage_id=_hex(),
                    asset_storage_backend="s3",
                    asset_storage_bucket="legacy-bucket",
                    asset_object_key=f"derivatives/x/{ordinal}.png",
                    asset_object_version_id=f"x-version-{ordinal}",
                    asset_sha256=f"{ordinal + 4}" * 64,
                    asset_content_type="image/png",
                    asset_image_format="PNG",
                    asset_width=1150,
                    asset_height=1487,
                    asset_byte_size=900_000 + ordinal,
                    lineage_relation="watermarked_teaser",
                    lineage_recipe_version="legacy-renderer",
                    recorded_by="legacy-worker",
                    recorded_at=_NOW,
                )
            )
        for statement in trigger_sql.values():
            assert statement is not None
            connection.exec_driver_sql(statement)
    engine.dispose()
    return review_task_id, release_version_id, watermark_asset_id


def _seed_revision_scoped_duplicate_job(database_path: Path) -> None:
    """Create a shape 0027 accepts but the 0026 uniqueness model cannot represent."""

    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    metadata = MetaData()
    metadata.reflect(
        bind=engine,
        only=[
            "derivative_jobs",
            "x_teaser_revision_heads",
        ],
    )
    jobs = metadata.tables["derivative_jobs"]
    heads = metadata.tables["x_teaser_revision_heads"]
    with engine.begin() as connection:
        source = connection.execute(select(jobs).limit(1)).mappings().one()
        active_revision_id = connection.scalar(select(heads.c.active_revision_id))
        assert active_revision_id is not None
        insert_guard = connection.scalar(
            text(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'trigger' AND name = 'derivative_jobs_guard_insert'"
            )
        )
        assert insert_guard is not None
        connection.exec_driver_sql('DROP TRIGGER "derivative_jobs_guard_insert"')
        duplicate = dict(source)
        duplicate.update(
            id=_hex(),
            logical_key="e" * 64,
            request_sha256="f" * 64,
            x_teaser_revision_id=active_revision_id,
            gates_release=False,
        )
        connection.execute(jobs.insert().values(**duplicate))
        connection.exec_driver_sql(insert_guard)
    engine.dispose()


def _seed_revision_two_with_a_different_corner(database_path: Path) -> None:
    """Record a second immutable recipe choice the legacy headless model cannot express."""

    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    metadata = MetaData()
    metadata.reflect(
        bind=engine,
        only=[
            "x_teaser_revisions",
            "x_teaser_revision_members",
        ],
    )
    revisions = metadata.tables["x_teaser_revisions"]
    members = metadata.tables["x_teaser_revision_members"]
    with engine.begin() as connection:
        revision_one = connection.execute(select(revisions).limit(1)).mappings().one()
        member_one = connection.execute(select(members).limit(1)).mappings().one()
        revision_two_id = _hex()
        revision_two = dict(revision_one)
        revision_two.update(
            id=revision_two_id,
            revision_no=2,
            request_sha256="a" * 64,
            created_at=_NOW,
        )
        connection.execute(revisions.insert().values(**revision_two))
        member_guard_sql = connection.scalar(
            text(
                "SELECT sql FROM sqlite_master WHERE type = 'trigger' "
                "AND name = 'x_teaser_revision_members_guard_insert'"
            )
        )
        assert member_guard_sql is not None
        connection.exec_driver_sql('DROP TRIGGER "x_teaser_revision_members_guard_insert"')
        member_two = dict(member_one)
        member_two.update(
            id=_hex(),
            revision_id=revision_two_id,
            watermark_position="top_right",
            derivative_job_id=None,
        )
        connection.execute(members.insert().values(**member_two))
        connection.exec_driver_sql(member_guard_sql)
    engine.dispose()


def _move_active_revision_to_pending_for_downgrade_fixture(database_path: Path) -> None:
    """Model a first revision still rendering without constructing the full service graph."""

    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    with engine.begin() as connection:
        guard_sql = connection.scalar(
            text(
                "SELECT sql FROM sqlite_master WHERE type = 'trigger' "
                "AND name = 'x_teaser_revision_heads_guard_update'"
            )
        )
        assert guard_sql is not None
        connection.exec_driver_sql('DROP TRIGGER "x_teaser_revision_heads_guard_update"')
        connection.execute(
            text(
                "UPDATE x_teaser_revision_heads SET "
                "pending_revision_id = active_revision_id, active_revision_id = NULL, "
                "lock_version = lock_version + 1"
            )
        )
        connection.exec_driver_sql(guard_sql)
    engine.dispose()


def _seed_unmatched_legacy_x_output_with_a_different_recipe(database_path: Path) -> None:
    """Add a second succeeded legacy X output which has no canonical head membership."""

    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    metadata = MetaData()
    metadata.reflect(
        bind=engine,
        only=["derivative_jobs", "derivative_outputs", "derivative_recipes"],
    )
    jobs = metadata.tables["derivative_jobs"]
    outputs = metadata.tables["derivative_outputs"]
    recipes = metadata.tables["derivative_recipes"]
    with engine.begin() as connection:
        source_job = connection.execute(select(jobs).limit(1)).mappings().one()
        source_output = connection.execute(select(outputs).limit(1)).mappings().one()
        source_recipe = connection.execute(select(recipes).limit(1)).mappings().one()
        guard_names = (
            "derivative_recipes_guard_insert",
            "derivative_jobs_guard_insert",
            "derivative_outputs_guard_insert",
        )
        guard_sql = {
            name: connection.scalar(
                text("SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = :name"),
                {"name": name},
            )
            for name in guard_names
        }
        assert all(guard_sql.values())
        for name in guard_names:
            connection.exec_driver_sql(f'DROP TRIGGER "{name}"')
        recipe_id = _hex()
        recipe = dict(source_recipe)
        configuration = dict(recipe["configuration"])
        configuration["watermark"] = {"position": "bottom_right"}
        recipe.update(
            id=recipe_id,
            logical_key="b" * 64,
            configuration=configuration,
            config_sha256="c" * 64,
        )
        connection.execute(recipes.insert().values(**recipe))
        job_id = _hex()
        job = dict(source_job)
        job.update(
            id=job_id,
            derivative_recipe_id=recipe_id,
            logical_key="d" * 64,
            request_sha256="e" * 64,
            x_teaser_revision_id=None,
            gates_release=True,
        )
        connection.execute(jobs.insert().values(**job))
        output = dict(source_output)
        output.update(
            id=_hex(),
            derivative_job_id=job_id,
            derivative_recipe_id=recipe_id,
            asset_id=_hex(),
            asset_lineage_id=_hex(),
            asset_object_key="derivatives/x/unmatched.png",
            asset_object_version_id="unmatched-version",
            asset_sha256="f" * 64,
        )
        connection.execute(outputs.insert().values(**output))
        for statement in guard_sql.values():
            assert statement is not None
            connection.exec_driver_sql(statement)
    engine.dispose()


def _make_active_revision_job_nonterminal(database_path: Path) -> None:
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    with engine.begin() as connection:
        guard_sql = connection.scalar(
            text(
                "SELECT sql FROM sqlite_master WHERE type = 'trigger' "
                "AND name = 'derivative_jobs_guard_update'"
            )
        )
        assert guard_sql is not None
        connection.exec_driver_sql('DROP TRIGGER "derivative_jobs_guard_update"')
        connection.execute(
            text(
                "UPDATE derivative_jobs SET state = 'requested', attempt_count = 0, "
                "claimed_at = NULL, processing_started_at = NULL, completed_at = NULL, "
                "x_teaser_revision_id = (SELECT active_revision_id "
                "FROM x_teaser_revision_heads LIMIT 1), gates_release = 0 "
                "WHERE id = (SELECT id FROM derivative_jobs LIMIT 1)"
            )
        )
        connection.exec_driver_sql(guard_sql)
    engine.dispose()


def test_legacy_four_of_four_x_outputs_are_adopted_as_active_revision_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "legacy-x-revision.db"
    database_url = f"sqlite+aiosqlite:///{database_path.as_posix()}"
    monkeypatch.setenv("GEN_AUTOMATION_DATABASE_URL", database_url)
    configuration = Config("alembic.ini")
    command.upgrade(configuration, _PREVIOUS)
    review_task_hex, _release_version_hex, watermark_asset_hex = _seed_four_legacy_top_left_outputs(
        database_path
    )

    command.upgrade(configuration, _MIGRATION)
    inspector = inspect(create_engine(f"sqlite:///{database_path.as_posix()}"))
    assert {
        "x_teaser_revisions",
        "x_teaser_revision_members",
        "x_teaser_revision_heads",
    } <= set(inspector.get_table_names())
    assert {"x_teaser_revision_id", "gates_release"} <= {
        column["name"] for column in inspector.get_columns("derivative_jobs")
    }

    async def audit_backfill() -> None:
        database = Database(database_url)
        try:
            async with database.sessions() as session:
                status = await x_teaser_revision_status(
                    session,
                    review_task_id=UUID(review_task_hex),
                )
                outputs = await active_x_teaser_outputs(
                    session,
                    review_task_id=UUID(review_task_hex),
                )
                assert status.active_revision_no == 1
                assert status.pending_revision_id is None
                assert status.can_replace is True
                assert status.current_watermark_asset_id == UUID(watermark_asset_hex)
                assert len(status.current_positions_by_asset_id) == 4
                assert set(status.current_positions_by_asset_id.values()) == {"top_left"}
                assert len(outputs) == len({output.id for output in outputs}) == 4
        finally:
            await database.dispose()

    asyncio.run(audit_backfill())

    command.downgrade(configuration, _PREVIOUS)
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        inspector = inspect(engine)
        assert "x_teaser_revisions" not in inspector.get_table_names()
        assert "x_teaser_revision_members" not in inspector.get_table_names()
        assert "x_teaser_revision_heads" not in inspector.get_table_names()
        assert "x_teaser_revision_id" not in {
            column["name"] for column in inspector.get_columns("derivative_jobs")
        }
        assert "gates_release" not in {
            column["name"] for column in inspector.get_columns("derivative_jobs")
        }
        metadata = MetaData()
        metadata.reflect(bind=engine, only=["derivative_outputs"])
        outputs = metadata.tables["derivative_outputs"]
        with engine.connect() as connection:
            assert len(connection.execute(select(outputs)).all()) == 4
    finally:
        engine.dispose()


def test_downgrade_refuses_revision_scoped_duplicate_jobs_before_changing_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "x-revision-downgrade-preflight.db"
    database_url = f"sqlite+aiosqlite:///{database_path.as_posix()}"
    monkeypatch.setenv("GEN_AUTOMATION_DATABASE_URL", database_url)
    configuration = Config("alembic.ini")
    command.upgrade(configuration, _PREVIOUS)
    _seed_four_legacy_top_left_outputs(database_path)
    command.upgrade(configuration, _MIGRATION)
    _seed_revision_scoped_duplicate_job(database_path)

    with pytest.raises(
        RuntimeError,
        match="cannot downgrade X teaser revisions without losing derivative jobs",
    ):
        command.downgrade(configuration, _PREVIOUS)

    inspector = inspect(create_engine(f"sqlite:///{database_path.as_posix()}"))
    assert {
        "x_teaser_revisions",
        "x_teaser_revision_members",
        "x_teaser_revision_heads",
    } <= set(inspector.get_table_names())
    assert {"x_teaser_revision_id", "gates_release"} <= {
        column["name"] for column in inspector.get_columns("derivative_jobs")
    }


def test_downgrade_refuses_different_corner_revision_history_before_ddl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "x-revision-different-corner-downgrade.db"
    database_url = f"sqlite+aiosqlite:///{database_path.as_posix()}"
    monkeypatch.setenv("GEN_AUTOMATION_DATABASE_URL", database_url)
    configuration = Config("alembic.ini")
    command.upgrade(configuration, _PREVIOUS)
    _seed_four_legacy_top_left_outputs(database_path)
    command.upgrade(configuration, _MIGRATION)
    _seed_revision_two_with_a_different_corner(database_path)

    with pytest.raises(
        RuntimeError,
        match="cannot downgrade X teaser revisions without losing active revision history",
    ):
        command.downgrade(configuration, _PREVIOUS)

    inspector = inspect(create_engine(f"sqlite:///{database_path.as_posix()}"))
    assert "x_teaser_revisions" in inspector.get_table_names()
    assert "x_teaser_revision_id" in {
        column["name"] for column in inspector.get_columns("derivative_jobs")
    }


def test_downgrade_refuses_a_pending_first_revision_before_ddl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "x-revision-pending-downgrade.db"
    database_url = f"sqlite+aiosqlite:///{database_path.as_posix()}"
    monkeypatch.setenv("GEN_AUTOMATION_DATABASE_URL", database_url)
    configuration = Config("alembic.ini")
    command.upgrade(configuration, _PREVIOUS)
    _seed_four_legacy_top_left_outputs(database_path)
    command.upgrade(configuration, _MIGRATION)
    _move_active_revision_to_pending_for_downgrade_fixture(database_path)

    with pytest.raises(
        RuntimeError,
        match="cannot downgrade X teaser revisions while a revision is pending",
    ):
        command.downgrade(configuration, _PREVIOUS)

    inspector = inspect(create_engine(f"sqlite:///{database_path.as_posix()}"))
    assert "x_teaser_revision_heads" in inspector.get_table_names()
    assert "gates_release" in {
        column["name"] for column in inspector.get_columns("derivative_jobs")
    }


def test_downgrade_refuses_unmatched_legacy_x_output_before_ddl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "x-revision-unmatched-legacy-output.db"
    database_url = f"sqlite+aiosqlite:///{database_path.as_posix()}"
    monkeypatch.setenv("GEN_AUTOMATION_DATABASE_URL", database_url)
    configuration = Config("alembic.ini")
    command.upgrade(configuration, _PREVIOUS)
    _seed_four_legacy_top_left_outputs(database_path)
    command.upgrade(configuration, _MIGRATION)
    _seed_unmatched_legacy_x_output_with_a_different_recipe(database_path)

    with pytest.raises(
        RuntimeError,
        match="cannot downgrade X teaser revisions without losing the canonical X output",
    ):
        command.downgrade(configuration, _PREVIOUS)

    inspector = inspect(create_engine(f"sqlite:///{database_path.as_posix()}"))
    assert "x_teaser_revision_members" in inspector.get_table_names()
    assert "x_teaser_revision_id" in {
        column["name"] for column in inspector.get_columns("derivative_jobs")
    }


def test_downgrade_refuses_nonterminal_revision_jobs_before_ddl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "x-revision-nonterminal-downgrade.db"
    database_url = f"sqlite+aiosqlite:///{database_path.as_posix()}"
    monkeypatch.setenv("GEN_AUTOMATION_DATABASE_URL", database_url)
    configuration = Config("alembic.ini")
    command.upgrade(configuration, _PREVIOUS)
    _seed_four_legacy_top_left_outputs(database_path)
    command.upgrade(configuration, _MIGRATION)
    _make_active_revision_job_nonterminal(database_path)

    with pytest.raises(
        RuntimeError,
        match="cannot downgrade X teaser revisions while revision jobs are nonterminal",
    ):
        command.downgrade(configuration, _PREVIOUS)

    inspector = inspect(create_engine(f"sqlite:///{database_path.as_posix()}"))
    assert "x_teaser_revisions" in inspector.get_table_names()
    assert "gates_release" in {
        column["name"] for column in inspector.get_columns("derivative_jobs")
    }
