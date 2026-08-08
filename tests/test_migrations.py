import hashlib
import inspect as python_inspect
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid5

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import MetaData, create_engine, inspect, select, text
from sqlalchemy.dialects import postgresql
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


def test_postgresql_publication_effect_event_guard_closes_outer_if_once(
    monkeypatch,
) -> None:
    configuration = Config("alembic.ini")
    revision = ScriptDirectory.from_config(configuration).get_revision("20260728_0010")
    assert revision is not None

    statements: list[str] = []
    monkeypatch.setattr(
        revision.module.op,
        "execute",
        lambda statement: statements.append(str(statement)),
    )
    revision.module._create_postgresql_guards()

    guard = next(
        statement
        for statement in statements
        if "gen_automation_guard_publication_effect_event()" in statement
    )
    assert (
        "'publication request completion has no start'; "
        "END IF; RETURN NEW; END; $$ LANGUAGE plpgsql"
    ) in guard


def test_postgresql_preview_constraint_uses_fixed_convention_name(
    monkeypatch,
) -> None:
    configuration = Config("alembic.ini")
    revision = ScriptDirectory.from_config(configuration).get_revision("20260728_0012")
    assert revision is not None

    fixed_names: list[str] = []
    dropped: list[tuple[str, str, str]] = []
    created: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        revision.module.op,
        "get_bind",
        lambda: SimpleNamespace(dialect=SimpleNamespace(name="postgresql")),
    )
    monkeypatch.setattr(
        revision.module.op,
        "f",
        lambda name: fixed_names.append(name) or f"fixed::{name}",
    )
    monkeypatch.setattr(
        revision.module.op,
        "drop_constraint",
        lambda name, table, *, type_: dropped.append((name, table, type_)),
    )
    monkeypatch.setattr(
        revision.module.op,
        "create_check_constraint",
        lambda name, table, expression: created.append((name, table, expression)),
    )

    revision.module._replace_publication_preview_constraint("role = 'x_teaser'")

    assert fixed_names == ["ck_publication_inputs_role_target"]
    assert dropped == [("fixed::ck_publication_inputs_role_target", "publication_inputs", "check")]
    assert created == [
        (
            "fixed::ck_publication_inputs_role_target",
            "publication_inputs",
            "role = 'x_teaser'",
        )
    ]


def test_semantic_feedback_report_uses_jsonb_on_postgresql() -> None:
    configuration = Config("alembic.ini")
    revision = ScriptDirectory.from_config(configuration).get_revision("20260803_0017")
    assert revision is not None

    report_type = revision.module.json_type.dialect_impl(postgresql.dialect())

    assert isinstance(report_type, postgresql.JSONB)


def test_x_teaser_revision_is_the_migration_head() -> None:
    configuration = Config("alembic.ini")
    scripts = ScriptDirectory.from_config(configuration)
    independent_targets_revision = scripts.get_revision("20260808_0024")
    target_ready_revision = scripts.get_revision("20260808_0025")
    owner_retry_revision = scripts.get_revision("20260808_0026")
    revision = scripts.get_revision("20260808_0027")

    assert independent_targets_revision is not None
    assert independent_targets_revision.down_revision == "20260808_0023"
    assert target_ready_revision is not None
    assert target_ready_revision.down_revision == "20260808_0024"
    assert owner_retry_revision is not None
    assert owner_retry_revision.down_revision == "20260808_0025"
    assert revision is not None
    assert revision.down_revision == "20260808_0026"
    assert scripts.get_current_head() == "20260808_0027"


def test_derivative_owner_retry_postgresql_guard_is_narrow_and_bounded(
    monkeypatch,
) -> None:
    configuration = Config("alembic.ini")
    revision = ScriptDirectory.from_config(configuration).get_revision("20260808_0026")
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

    assert len(statements) == 1
    guard = statements[0]
    assert "OLD.state = 'failed' AND NEW.state = 'retry_wait'" in guard
    assert "NEW.max_attempts <= 10" in guard
    assert "NEW.max_attempts > OLD.max_attempts" in guard
    assert "output_object_conflict" in guard
    assert "retry_recipe.output_targets::jsonb = '[\"full\"]'::jsonb" in guard
    assert "retry_release.phase = 'rendering'" in guard


def test_x_teaser_revision_postgresql_backfill_disables_legacy_job_guards(
    monkeypatch,
) -> None:
    configuration = Config("alembic.ini")
    revision = ScriptDirectory.from_config(configuration).get_revision("20260808_0027")
    assert revision is not None

    statements: list[str] = []
    monkeypatch.setattr(
        revision.module.op,
        "execute",
        lambda statement: statements.append(str(statement)),
    )
    revision.module._drop_postgresql_job_triggers()
    revision.module._create_postgresql_job_triggers()

    assert statements[:3] == [
        "DROP TRIGGER IF EXISTS derivative_jobs_guard_insert ON derivative_jobs",
        "DROP TRIGGER IF EXISTS derivative_jobs_guard_mutation ON derivative_jobs",
        "DROP TRIGGER IF EXISTS derivative_jobs_promote_release_after_success ON derivative_jobs",
    ]
    assert statements[3:] == [
        "CREATE TRIGGER derivative_jobs_guard_insert BEFORE INSERT ON derivative_jobs "
        "FOR EACH ROW EXECUTE FUNCTION gen_automation_guard_derivative_job_insert()",
        "CREATE TRIGGER derivative_jobs_guard_mutation BEFORE UPDATE OR DELETE ON derivative_jobs "
        "FOR EACH ROW EXECUTE FUNCTION gen_automation_guard_derivative_job_mutation()",
        "CREATE TRIGGER derivative_jobs_promote_release_after_success AFTER UPDATE ON "
        "derivative_jobs FOR EACH ROW EXECUTE FUNCTION "
        "gen_automation_promote_rendered_release()",
    ]
    upgrade_source = python_inspect.getsource(revision.module.upgrade)
    drop_offset = upgrade_source.index("_drop_postgresql_job_triggers()")
    backfill_offset = upgrade_source.index("UPDATE derivative_jobs SET gates_release = true")
    recreate_offset = upgrade_source.index("_create_postgresql_job_triggers()")
    assert drop_offset < backfill_offset < recreate_offset


def test_derivative_owner_retry_downgrade_restores_terminal_failed_jobs(
    monkeypatch,
) -> None:
    configuration = Config("alembic.ini")
    revision = ScriptDirectory.from_config(configuration).get_revision("20260808_0026")
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

    revision.module.downgrade()

    assert len(statements) == 1
    guard = statements[0]
    assert "OLD.state IN ('succeeded', 'failed', 'cancelled')" in guard
    assert "OLD.max_attempts IS DISTINCT FROM NEW.max_attempts" in guard
    assert "failed derivative job rearm is invalid" not in guard


def test_target_ready_publication_postgresql_guards_allow_active_release_phases(
    monkeypatch,
) -> None:
    configuration = Config("alembic.ini")
    revision = ScriptDirectory.from_config(configuration).get_revision("20260808_0025")
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

    revision.module._replace_guards(revision.module._TARGET_READY)

    assert len(statements) == 2
    for statement in statements:
        assert "__RELEASE_PHASE_PREDICATE__" not in statement
        assert (
            "release.phase IN ('rendering', 'ready_to_publish', 'publishing', 'published')"
        ) in statement


def test_target_ready_publication_postgresql_downgrade_restores_ready_only_guard(
    monkeypatch,
) -> None:
    configuration = Config("alembic.ini")
    revision = ScriptDirectory.from_config(configuration).get_revision("20260808_0025")
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

    revision.module._replace_guards(revision.module._READY_ONLY)

    assert len(statements) == 2
    for statement in statements:
        assert "release.phase = 'ready_to_publish'" in statement
        assert "release.phase IN" not in statement


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
        "experiment_warm_leases",
        "finished_set_archive_parts",
        "finished_set_archives",
        "generation_attempts",
        "generation_jobs",
        "idempotency_records",
        "login_throttles",
        "mega_deliveries",
        "mega_set_deliveries",
        "mega_set_delivery_items",
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
        "review_asset_inspections",
        "review_decisions",
        "review_tasks",
        "review_x_selections",
        "salad_deployments",
        "scoring_runs",
        "semantic_anatomy_feedback",
        "semantic_assessments",
        "semantic_calibration_artifacts",
        "semantic_learning_policies",
        "semantic_model_promotions",
        "semantic_training_runs",
        "subject_approvals",
        "webhook_receipts",
        "workflow_approvals",
        "wildcard_libraries",
        "wildcard_library_versions",
        "x_teaser_revision_heads",
        "x_teaser_revision_members",
        "x_teaser_revisions",
    }
    assert set(inspect(engine).get_table_names()) == expected_tables
    assert {
        "gates_release",
        "x_teaser_revision_id",
    } <= {column["name"] for column in inspect(engine).get_columns("derivative_jobs")}
    assert {
        "source_generation_job_id",
        "source_output_index",
        "source_generation_ordinal",
        "source_generation_queue_position",
    } <= {column["name"] for column in inspect(engine).get_columns("release_selections")}
    assert {
        "mega_requested_at",
        "mega_requested_by_user_id",
        "mega_requested_remote_root",
    } <= {column["name"] for column in inspect(engine).get_columns("finished_set_archives")}
    mega_request_contract = next(
        constraint
        for constraint in inspect(engine).get_check_constraints("finished_set_archives")
        if constraint["name"] == "ck_finished_set_archives_mega_request_pair"
    )
    assert "mega_requested_at IS NULL" in mega_request_contract["sqltext"]
    assert "mega_requested_by_user_id IS NULL" in mega_request_contract["sqltext"]
    assert "mega_requested_remote_root IS NULL" in mega_request_contract["sqltext"]
    assert "mega_requested_remote_root IS NOT NULL" in mega_request_contract["sqltext"]
    assert "ix_finished_set_archives_mega_request" in {
        index["name"] for index in inspect(engine).get_indexes("finished_set_archives")
    }
    publication_intent_indexes = {
        index["name"]: index for index in inspect(engine).get_indexes("publication_intents")
    }
    canonical_index = publication_intent_indexes["uq_publication_intents_release_target_canonical"]
    assert canonical_index["unique"] == 1
    assert canonical_index["column_names"] == ["release_id", "target"]
    assert "state NOT IN ('failed', 'cancelled')" in str(
        canonical_index["dialect_options"]["sqlite_where"]
    )
    assert "uq_publication_intents_version_target_config" not in {
        constraint["name"]
        for constraint in inspect(engine).get_unique_constraints("publication_intents")
    }
    with engine.connect() as connection:
        trigger_names = set(
            connection.execute(
                text(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'trigger' AND name LIKE 'semantic_%'"
                )
            ).scalars()
        )
    assert {
        "semantic_assessments_guard_terminal_update",
        "semantic_assessments_guard_delete",
        "semantic_anatomy_feedback_immutable_update",
        "semantic_anatomy_feedback_immutable_delete",
        "semantic_calibration_artifacts_immutable_update",
        "semantic_calibration_artifacts_immutable_delete",
        "semantic_model_promotions_immutable_update",
        "semantic_model_promotions_immutable_delete",
        "semantic_training_runs_guard_terminal_update",
        "semantic_training_runs_guard_delete",
    } <= trigger_names
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
