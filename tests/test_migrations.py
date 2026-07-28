import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid5

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, create_engine, inspect, select
from sqlalchemy.exc import IntegrityError

_LEGACY_DEPLOYMENT_NAMESPACE = UUID("ed71d302-4781-4ddc-949b-3b0c1c75f95a")
_RELEASE_SELECTION_NAMESPACE = UUID("9b9beff8-3ab2-4ca1-b9fc-b84df8f10d1d")


def _canonical_sha256(value: object) -> str:
    serialized = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def test_foundation_migration_round_trip(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "migration.db"
    monkeypatch.setenv(
        "GEN_AUTOMATION_DATABASE_URL",
        f"sqlite+aiosqlite:///{database_path.as_posix()}",
    )
    configuration = Config("alembic.ini")

    command.upgrade(configuration, "head")

    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    expected_tables = {
        "alembic_version",
        "admin_enrollments",
        "admin_sessions",
        "admin_users",
        "audit_events",
        "asset_lineage",
        "asset_rankings",
        "asset_scores",
        "assets",
        "compliance_checks",
        "derivative_jobs",
        "derivative_outputs",
        "derivative_recipes",
        "generation_attempts",
        "generation_jobs",
        "idempotency_records",
        "login_throttles",
        "mega_deliveries",
        "model_artifact_approvals",
        "outbox_events",
        "provider_budget_guards",
        "provider_spend_entries",
        "projects",
        "publication_approvals",
        "publication_attempts",
        "publication_effect_events",
        "publication_inputs",
        "publication_intents",
        "publication_packages",
        "publication_provider_guards",
        "publication_reconciliations",
        "publication_steps",
        "release_versions",
        "release_selections",
        "releases",
        "review_decisions",
        "review_tasks",
        "review_x_selections",
        "salad_deployments",
        "scoring_runs",
        "subject_approvals",
        "webhook_receipts",
        "workflow_approvals",
        "wildcard_libraries",
        "wildcard_library_versions",
    }
    assert set(inspect(engine).get_table_names()) == expected_tables
    engine.dispose()

    command.check(configuration)
    command.downgrade(configuration, "base")

    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    assert inspect(engine).get_table_names() == ["alembic_version"]
    engine.dispose()


def test_populated_generation_attempts_upgrade_from_0002(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "populated-migration.db"
    monkeypatch.setenv(
        "GEN_AUTOMATION_DATABASE_URL",
        f"sqlite+aiosqlite:///{database_path.as_posix()}",
    )
    configuration = Config("alembic.ini")
    command.upgrade(configuration, "20260728_0002")

    project_id = UUID("10000000-0000-4000-8000-000000000001")
    release_id = UUID("20000000-0000-4000-8000-000000000002")
    release_version_id = UUID("30000000-0000-4000-8000-000000000003")
    generation_job_id = UUID("40000000-0000-4000-8000-000000000004")
    generation_attempt_id = UUID("50000000-0000-4000-8000-000000000005")
    parameters_sha256 = "a" * 64
    worker_image_digest = f"ghcr.io/example/worker@sha256:{'b' * 64}"
    request_metadata = {
        "prompt": "夜空のテスト",
        "seed": 42,
        "nested": {"z": 2, "a": 1},
    }
    created_at = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)

    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    with engine.begin() as connection:
        metadata = MetaData()
        metadata.reflect(
            bind=connection,
            only=[
                "projects",
                "releases",
                "release_versions",
                "generation_jobs",
                "generation_attempts",
            ],
        )
        connection.execute(
            metadata.tables["projects"].insert(),
            {
                "slug": "migration-project",
                "name": "Migration project",
                "id": project_id.hex,
            },
        )
        connection.execute(
            metadata.tables["releases"].insert(),
            {
                "project_id": project_id.hex,
                "slug": "migration-release",
                "title": "Migration release",
                "phase": "generating",
                "health": "healthy",
                "current_version_no": 1,
                "desired_accepted_count": 1,
                "lock_version": 1,
                "id": release_id.hex,
            },
        )
        connection.execute(
            metadata.tables["release_versions"].insert(),
            {
                "release_id": release_id.hex,
                "version_no": 1,
                "specification": {"title": "Legacy specification"},
                "specification_sha256": "c" * 64,
                "created_by": "migration-test",
                "created_at": created_at,
                "id": release_version_id.hex,
            },
        )
        connection.execute(
            metadata.tables["generation_jobs"].insert(),
            {
                "release_version_id": release_version_id.hex,
                "logical_key": "d" * 64,
                "parameters": {"batch_size": 1},
                "parameters_sha256": parameters_sha256,
                "provider": "salad",
                "state": "succeeded",
                "priority": 100,
                "expected_output_count": 1,
                "attempt_count": 1,
                "max_attempts": 3,
                "id": generation_job_id.hex,
            },
        )
        connection.execute(
            metadata.tables["generation_attempts"].insert(),
            {
                "job_id": generation_job_id.hex,
                "attempt_no": 1,
                "provider": "salad",
                "provider_external_id": "legacy-provider-job",
                "state": "succeeded",
                "worker_image_digest": worker_image_digest,
                "request_metadata": request_metadata,
                "response_metadata": {"status": "complete"},
                "started_at": created_at,
                "completed_at": created_at,
                "created_at": created_at,
                "id": generation_attempt_id.hex,
            },
        )
    engine.dispose()

    command.upgrade(configuration, "head")

    deployment_configuration = {
        "schema": "legacy-salad-deployment/v1",
        "history_only": True,
        "worker_image_digest": worker_image_digest,
        "max_hourly_cost_is_placeholder": True,
    }
    deployment_config_sha256 = _canonical_sha256(deployment_configuration)
    expected_deployment_id = uuid5(
        _LEGACY_DEPLOYMENT_NAMESPACE,
        deployment_config_sha256,
    )
    expected_submission_key = _canonical_sha256(
        {
            "schema": "legacy-submission-key/v1",
            "provider": "salad",
            "generation_attempt_id": str(generation_attempt_id),
        }
    )
    expected_request_sha256 = _canonical_sha256(
        {
            "schema": "legacy-generation-request/v1",
            "provider": "salad",
            "generation_job_id": str(generation_job_id),
            "release_version_id": str(release_version_id),
            "parameters_sha256": parameters_sha256,
            "worker_image_digest": worker_image_digest,
            "request_metadata": request_metadata,
        }
    )

    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    with engine.connect() as connection:
        metadata = MetaData()
        metadata.reflect(
            bind=connection,
            only=["generation_attempts", "salad_deployments"],
        )
        attempts = metadata.tables["generation_attempts"]
        deployments = metadata.tables["salad_deployments"]
        attempt = (
            connection.execute(select(attempts).where(attempts.c.id == generation_attempt_id.hex))
            .mappings()
            .one()
        )
        deployment = (
            connection.execute(
                select(deployments).where(deployments.c.id == expected_deployment_id.hex)
            )
            .mappings()
            .one()
        )

        assert UUID(str(attempt["salad_deployment_id"])) == expected_deployment_id
        assert attempt["submission_key"] == expected_submission_key
        assert attempt["request_sha256"] == expected_request_sha256
        assert attempt["request_metadata"] == request_metadata
        assert attempt["cost_reservation_microusd"] == 0
        assert attempt["lock_version"] == 1
        assert deployment["version_no"] == 1
        assert deployment["config_sha256"] == deployment_config_sha256
        assert deployment["provider_configuration"] == deployment_configuration
        assert deployment["worker_image_digest"] == worker_image_digest
        assert deployment["state"] == "failed"
        assert deployment["desired_state"] == "stopped"
        assert deployment["is_current"] is False

        attempt_columns = {
            column["name"]: column
            for column in inspect(connection).get_columns("generation_attempts")
        }
        assert attempt_columns["salad_deployment_id"]["nullable"] is False
        assert attempt_columns["submission_key"]["nullable"] is False
        assert attempt_columns["request_sha256"]["nullable"] is False
    engine.dispose()

    command.check(configuration)
    command.downgrade(configuration, "20260728_0002")

    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    with engine.connect() as connection:
        downgraded_columns = {
            column["name"] for column in inspect(connection).get_columns("generation_attempts")
        }
        assert "salad_deployment_id" not in downgraded_columns
        assert "submission_key" not in downgraded_columns
        assert "request_sha256" not in downgraded_columns
        assert "salad_deployments" not in inspect(connection).get_table_names()

        metadata = MetaData()
        metadata.reflect(bind=connection, only=["generation_attempts"])
        attempts = metadata.tables["generation_attempts"]
        original_attempt = (
            connection.execute(select(attempts).where(attempts.c.id == generation_attempt_id.hex))
            .mappings()
            .one()
        )
        assert original_attempt["provider_external_id"] == "legacy-provider-job"
        assert original_attempt["request_metadata"] == request_metadata
    engine.dispose()


def test_populated_ranking_snapshot_upgrade_from_0007(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "populated-ranking-migration.db"
    monkeypatch.setenv(
        "GEN_AUTOMATION_DATABASE_URL",
        f"sqlite+aiosqlite:///{database_path.as_posix()}",
    )
    configuration = Config("alembic.ini")
    command.upgrade(configuration, "20260728_0007")

    project_id = UUID("11000000-0000-4000-8000-000000000001")
    release_id = UUID("22000000-0000-4000-8000-000000000002")
    release_version_id = UUID("33000000-0000-4000-8000-000000000003")
    generation_job_id = UUID("44000000-0000-4000-8000-000000000004")
    asset_id = UUID("55000000-0000-4000-8000-000000000005")
    scoring_run_id = UUID("66000000-0000-4000-8000-000000000006")
    asset_score_id = UUID("77000000-0000-4000-8000-000000000007")
    ranking_id = UUID("88000000-0000-4000-8000-000000000008")
    reviewer_id = UUID("99000000-0000-4000-8000-000000000009")
    review_task_id = UUID("aa000000-0000-4000-8000-00000000000a")
    decision_id = UUID("bb000000-0000-4000-8000-00000000000b")
    created_at = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
    frozen_at = datetime(2026, 7, 28, 12, 1, tzinfo=UTC)
    completed_at = datetime(2026, 7, 28, 12, 2, tzinfo=UTC)
    config_sha256 = "d" * 64
    input_manifest_sha256 = "e" * 64

    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    with engine.begin() as connection:
        metadata = MetaData()
        metadata.reflect(
            bind=connection,
            only=[
                "projects",
                "releases",
                "release_versions",
                "generation_jobs",
                "assets",
                "scoring_runs",
                "asset_scores",
                "asset_rankings",
                "admin_users",
                "review_tasks",
                "review_decisions",
            ],
        )
        tables = metadata.tables
        connection.execute(
            tables["projects"].insert(),
            {
                "id": project_id.hex,
                "slug": "ranking-migration",
                "name": "Ranking migration",
            },
        )
        connection.execute(
            tables["releases"].insert(),
            {
                "id": release_id.hex,
                "project_id": project_id.hex,
                "slug": "snapshot",
                "title": "Snapshot",
                "phase": "reviewing",
                "health": "healthy",
                "current_version_no": 1,
                "desired_accepted_count": 1,
                "lock_version": 1,
            },
        )
        connection.execute(
            tables["release_versions"].insert(),
            {
                "id": release_version_id.hex,
                "release_id": release_id.hex,
                "version_no": 1,
                "specification": {"schema_version": 1},
                "specification_sha256": "c" * 64,
                "created_by": "migration-test",
                "created_at": created_at,
            },
        )
        connection.execute(
            tables["generation_jobs"].insert(),
            {
                "id": generation_job_id.hex,
                "release_version_id": release_version_id.hex,
                "logical_key": "b" * 64,
                "parameters": {"batch_size": 1},
                "parameters_sha256": "a" * 64,
                "provider": "salad",
                "state": "succeeded",
                "priority": 100,
                "expected_output_count": 1,
                "attempt_count": 1,
                "max_attempts": 3,
                "lock_version": 1,
            },
        )
        connection.execute(
            tables["assets"].insert(),
            {
                "id": asset_id.hex,
                "release_id": release_id.hex,
                "generation_job_id": generation_job_id.hex,
                "output_index": 0,
                "kind": "raw_master",
                "state": "available",
                "storage_backend": "s3",
                "storage_bucket": "migration",
                "object_key": "raw/master.png",
                "object_version_id": "version-1",
                "sha256": "f" * 64,
                "content_type": "image/png",
                "image_format": "PNG",
                "width": 1024,
                "height": 1024,
                "byte_size": 4096,
                "metadata": {"immutable": True},
                "available_at": created_at,
            },
        )
        connection.execute(
            tables["scoring_runs"].insert(),
            {
                "id": scoring_run_id.hex,
                "release_version_id": release_version_id.hex,
                "configuration": {"quality": "v1"},
                "config_sha256": config_sha256,
                "input_manifest_sha256": input_manifest_sha256,
                "scorer_version": "migration-scorer-v1",
                "pillow_version": "12.0.0",
                "state": "completed",
                "asset_count": 1,
                "max_attempts": 3,
                "created_at": created_at,
                "started_at": created_at,
                "completed_at": frozen_at,
            },
        )
        connection.execute(
            tables["asset_scores"].insert(),
            {
                "id": asset_score_id.hex,
                "scoring_run_id": scoring_run_id.hex,
                "asset_id": asset_id.hex,
                "asset_storage_backend": "s3",
                "asset_storage_bucket": "migration",
                "asset_sha256": "f" * 64,
                "asset_object_key": "raw/master.png",
                "asset_object_version_id": "version-1",
                "asset_byte_size": 4096,
                "asset_image_format": "PNG",
                "asset_width": 1024,
                "asset_height": 1024,
                "state": "flagged_corrupt",
                "attempts": 1,
                "max_attempts": 3,
                "available_at": created_at,
                "aggregate_score_micros": 125_000,
                "signal_detail": {"decoder": "failed"},
                "scorer_version": "migration-scorer-v1",
                "pillow_version": "12.0.0",
                "config_sha256": config_sha256,
                "last_error_code": "decode_failed",
                "last_error_detail": "legacy decoder result",
                "created_at": created_at,
                "completed_at": frozen_at,
            },
        )
        connection.execute(
            tables["asset_rankings"].insert(),
            {
                "id": ranking_id.hex,
                "scoring_run_id": scoring_run_id.hex,
                "asset_score_id": asset_score_id.hex,
                "asset_id": asset_id.hex,
                "rank": 1,
                "aggregate_score_micros": 125_000,
                "disposition": "flagged_review",
                "explanation": {"reason": "decode_failed"},
                "is_duplicate_representative": False,
                "scorer_version": "migration-scorer-v1",
                "pillow_version": "12.0.0",
                "config_sha256": config_sha256,
                "frozen_at": frozen_at,
            },
        )
        connection.execute(
            tables["admin_users"].insert(),
            {
                "id": reviewer_id.hex,
                "username_normalized": "migration-reviewer",
                "display_name": "Migration Reviewer",
                "password_hash": "disabled-migration-password-hash",
                "role": "reviewer",
                "is_active": True,
                "failed_login_count": 0,
                "password_changed_at": created_at,
                "credential_version": 1,
                "lock_version": 1,
            },
        )
        connection.execute(
            tables["review_tasks"].insert(),
            {
                "id": review_task_id.hex,
                "release_version_id": release_version_id.hex,
                "release_version_no": 1,
                "release_specification_sha256": "c" * 64,
                "scoring_run_id": scoring_run_id.hex,
                "scoring_config_sha256": config_sha256,
                "scoring_input_manifest_sha256": input_manifest_sha256,
                "desired_accepted_count": 1,
                "ranked_asset_count": 1,
                "state": "completed",
                "lock_version": 2,
                "created_by_user_id": reviewer_id.hex,
                "created_at": created_at,
                "completed_by_user_id": reviewer_id.hex,
                "completed_at": completed_at,
            },
        )
        connection.execute(
            tables["review_decisions"].insert(),
            {
                "id": decision_id.hex,
                "review_task_id": review_task_id.hex,
                "asset_id": asset_id.hex,
                "revision": 1,
                "decision": "accept",
                "reason_code": "migration",
                "decided_by_user_id": reviewer_id.hex,
                "decided_at": frozen_at,
            },
        )
    engine.dispose()

    command.upgrade(configuration, "20260728_0008")

    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    with engine.connect() as connection:
        metadata = MetaData()
        metadata.reflect(
            bind=connection,
            only=[
                "scoring_runs",
                "review_tasks",
                "review_decisions",
                "asset_rankings",
            ],
        )
        runs = metadata.tables["scoring_runs"]
        tasks = metadata.tables["review_tasks"]
        decisions = metadata.tables["review_decisions"]
        run = (
            connection.execute(select(runs).where(runs.c.id == scoring_run_id.hex)).mappings().one()
        )
        task = (
            connection.execute(select(tasks).where(tasks.c.id == review_task_id.hex))
            .mappings()
            .one()
        )
        decision = (
            connection.execute(select(decisions).where(decisions.c.id == decision_id.hex))
            .mappings()
            .one()
        )
        first_manifest = str(run["ranking_manifest_sha256"])
        assert len(first_manifest) == 64
        assert task["ranking_manifest_sha256"] == first_manifest
        assert UUID(str(decision["scoring_run_id"])) == scoring_run_id
        trigger_names = set(
            connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            ).scalars()
        )
        assert {
            "scoring_runs_guard_completed_update",
            "asset_scores_guard_frozen_update",
            "asset_rankings_reject_delete",
            "review_tasks_guard_update",
            "review_decisions_reject_update",
        } <= trigger_names
    engine.dispose()

    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    with pytest.raises(IntegrityError, match="completed scoring runs are immutable"):
        with engine.begin() as connection:
            metadata = MetaData()
            metadata.reflect(bind=connection, only=["scoring_runs"])
            runs = metadata.tables["scoring_runs"]
            connection.execute(
                runs.update().where(runs.c.id == scoring_run_id.hex).values(config_sha256="0" * 64)
            )
    with pytest.raises(IntegrityError, match="asset rankings are append-only"):
        with engine.begin() as connection:
            metadata = MetaData()
            metadata.reflect(bind=connection, only=["asset_rankings"])
            rankings = metadata.tables["asset_rankings"]
            connection.execute(rankings.delete().where(rankings.c.id == ranking_id.hex))
    with pytest.raises(IntegrityError, match="review task identity is immutable"):
        with engine.begin() as connection:
            metadata = MetaData()
            metadata.reflect(bind=connection, only=["review_tasks"])
            tasks = metadata.tables["review_tasks"]
            connection.execute(
                tasks.update()
                .where(tasks.c.id == review_task_id.hex)
                .values(ranking_manifest_sha256="0" * 64, lock_version=3)
            )
    engine.dispose()

    command.downgrade(configuration, "20260728_0007")
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    with engine.connect() as connection:
        assert "ranking_manifest_sha256" not in {
            column["name"] for column in inspect(connection).get_columns("scoring_runs")
        }
        assert "ranking_manifest_sha256" not in {
            column["name"] for column in inspect(connection).get_columns("review_tasks")
        }
        assert "scoring_run_id" not in {
            column["name"] for column in inspect(connection).get_columns("review_decisions")
        }
    engine.dispose()

    command.upgrade(configuration, "head")
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    with engine.connect() as connection:
        metadata = MetaData()
        metadata.reflect(
            bind=connection,
            only=["scoring_runs", "release_selections", "releases"],
        )
        runs = metadata.tables["scoring_runs"]
        selections = metadata.tables["release_selections"]
        releases = metadata.tables["releases"]
        second_manifest = connection.scalar(
            select(runs.c.ranking_manifest_sha256).where(runs.c.id == scoring_run_id.hex)
        )
        assert second_manifest == first_manifest
        selection = (
            connection.execute(
                select(selections).where(selections.c.review_task_id == review_task_id.hex)
            )
            .mappings()
            .one()
        )
        assert UUID(str(selection["id"])) == uuid5(
            _RELEASE_SELECTION_NAMESPACE,
            f"{review_task_id}:{decision_id}",
        )
        assert UUID(str(selection["asset_id"])) == asset_id
        assert UUID(str(selection["review_decision_id"])) == decision_id
        assert selection["decision_revision"] == 1
        assert selection["ranking_rank"] == 1
        assert selection["display_order"] == 1
        assert selection["source_object_key"] == "raw/master.png"
        assert selection["source_object_version_id"] == "version-1"
        assert selection["source_sha256"] == "f" * 64
        assert selection["frozen_at"] == completed_at.replace(tzinfo=None)
        release = (
            connection.execute(select(releases).where(releases.c.id == release_id.hex))
            .mappings()
            .one()
        )
        assert release["phase"] == "approved"
        assert release["lock_version"] == 2
    with pytest.raises(IntegrityError, match="release selections are immutable"):
        with engine.begin() as connection:
            metadata = MetaData()
            metadata.reflect(bind=connection, only=["release_selections"])
            selections = metadata.tables["release_selections"]
            connection.execute(
                selections.update()
                .where(selections.c.review_task_id == review_task_id.hex)
                .values(source_sha256="0" * 64)
            )
    engine.dispose()
    command.check(configuration)
