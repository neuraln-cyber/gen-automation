"""Freeze completed rankings and bind review decisions to exact membership.

Revision ID: 20260728_0008
Revises: 20260728_0007
Create Date: 2026-07-28
"""

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260728_0008"
down_revision: str | None = "20260728_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name
    _drop_review_decision_guards(dialect)

    op.add_column(
        "scoring_runs",
        sa.Column("ranking_manifest_sha256", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "review_tasks",
        sa.Column("ranking_manifest_sha256", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "review_decisions",
        sa.Column("scoring_run_id", sa.Uuid(), nullable=True),
    )

    _backfill_and_validate(bind)
    _upgrade_constraints(dialect)
    _create_guards(dialect)


def downgrade() -> None:
    dialect = op.get_bind().dialect.name
    _drop_all_guards(dialect)
    _downgrade_constraints(dialect)
    _create_review_decision_guards(dialect)


def _upgrade_constraints(dialect: str) -> None:
    if dialect == "sqlite":
        with op.batch_alter_table("scoring_runs") as batch:
            batch.drop_constraint(
                op.f("ck_scoring_runs_completion_pair"),
                type_="check",
            )
            batch.create_check_constraint(
                op.f("ck_scoring_runs_completion_snapshot"),
                "(state = 'completed' AND completed_at IS NOT NULL "
                "AND ranking_manifest_sha256 IS NOT NULL "
                "AND length(ranking_manifest_sha256) = 64) "
                "OR (state <> 'completed' AND completed_at IS NULL "
                "AND ranking_manifest_sha256 IS NULL)",
            )
        with op.batch_alter_table("review_tasks") as batch:
            batch.alter_column(
                "ranking_manifest_sha256",
                existing_type=sa.String(length=64),
                nullable=False,
            )
            batch.create_check_constraint(
                op.f("ck_review_tasks_valid_ranking_manifest_sha256"),
                "length(ranking_manifest_sha256) = 64",
            )
            batch.create_unique_constraint(
                "uq_review_tasks_id_scoring_run",
                ["id", "scoring_run_id"],
            )
        with op.batch_alter_table("review_decisions") as batch:
            batch.alter_column(
                "scoring_run_id",
                existing_type=sa.Uuid(),
                nullable=False,
            )
            batch.create_foreign_key(
                "fk_review_decisions_task_scoring_run",
                "review_tasks",
                ["review_task_id", "scoring_run_id"],
                ["id", "scoring_run_id"],
                ondelete="RESTRICT",
            )
            batch.create_foreign_key(
                "fk_review_decisions_ranking_membership",
                "asset_rankings",
                ["scoring_run_id", "asset_id"],
                ["scoring_run_id", "asset_id"],
                ondelete="RESTRICT",
            )
        return

    op.drop_constraint(
        op.f("ck_scoring_runs_completion_pair"),
        "scoring_runs",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_scoring_runs_completion_snapshot"),
        "scoring_runs",
        "(state = 'completed' AND completed_at IS NOT NULL "
        "AND ranking_manifest_sha256 IS NOT NULL "
        "AND length(ranking_manifest_sha256) = 64) "
        "OR (state <> 'completed' AND completed_at IS NULL "
        "AND ranking_manifest_sha256 IS NULL)",
    )
    op.alter_column(
        "review_tasks",
        "ranking_manifest_sha256",
        existing_type=sa.String(length=64),
        nullable=False,
    )
    op.create_check_constraint(
        op.f("ck_review_tasks_valid_ranking_manifest_sha256"),
        "review_tasks",
        "length(ranking_manifest_sha256) = 64",
    )
    op.create_unique_constraint(
        "uq_review_tasks_id_scoring_run",
        "review_tasks",
        ["id", "scoring_run_id"],
    )
    op.alter_column(
        "review_decisions",
        "scoring_run_id",
        existing_type=sa.Uuid(),
        nullable=False,
    )
    op.create_foreign_key(
        "fk_review_decisions_task_scoring_run",
        "review_decisions",
        "review_tasks",
        ["review_task_id", "scoring_run_id"],
        ["id", "scoring_run_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_review_decisions_ranking_membership",
        "review_decisions",
        "asset_rankings",
        ["scoring_run_id", "asset_id"],
        ["scoring_run_id", "asset_id"],
        ondelete="RESTRICT",
    )


def _downgrade_constraints(dialect: str) -> None:
    if dialect == "sqlite":
        with op.batch_alter_table("review_decisions") as batch:
            batch.drop_constraint(
                "fk_review_decisions_ranking_membership",
                type_="foreignkey",
            )
            batch.drop_constraint(
                "fk_review_decisions_task_scoring_run",
                type_="foreignkey",
            )
            batch.drop_column("scoring_run_id")
        with op.batch_alter_table("review_tasks") as batch:
            batch.drop_constraint(
                "uq_review_tasks_id_scoring_run",
                type_="unique",
            )
            batch.drop_constraint(
                op.f("ck_review_tasks_valid_ranking_manifest_sha256"),
                type_="check",
            )
            batch.drop_column("ranking_manifest_sha256")
        with op.batch_alter_table("scoring_runs") as batch:
            batch.drop_constraint(
                op.f("ck_scoring_runs_completion_snapshot"),
                type_="check",
            )
            batch.create_check_constraint(
                op.f("ck_scoring_runs_completion_pair"),
                "(state = 'completed' AND completed_at IS NOT NULL) "
                "OR (state <> 'completed' AND completed_at IS NULL)",
            )
            batch.drop_column("ranking_manifest_sha256")
        return

    op.drop_constraint(
        "fk_review_decisions_ranking_membership",
        "review_decisions",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_review_decisions_task_scoring_run",
        "review_decisions",
        type_="foreignkey",
    )
    op.drop_column("review_decisions", "scoring_run_id")
    op.drop_constraint(
        "uq_review_tasks_id_scoring_run",
        "review_tasks",
        type_="unique",
    )
    op.drop_constraint(
        op.f("ck_review_tasks_valid_ranking_manifest_sha256"),
        "review_tasks",
        type_="check",
    )
    op.drop_column("review_tasks", "ranking_manifest_sha256")
    op.drop_constraint(
        op.f("ck_scoring_runs_completion_snapshot"),
        "scoring_runs",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_scoring_runs_completion_pair"),
        "scoring_runs",
        "(state = 'completed' AND completed_at IS NOT NULL) "
        "OR (state <> 'completed' AND completed_at IS NULL)",
    )
    op.drop_column("scoring_runs", "ranking_manifest_sha256")


def _backfill_and_validate(bind: sa.Connection) -> None:
    runs = sa.table(
        "scoring_runs",
        sa.column("id", sa.Uuid()),
        sa.column("release_version_id", sa.Uuid()),
        sa.column("config_sha256", sa.String()),
        sa.column("input_manifest_sha256", sa.String()),
        sa.column("ranking_manifest_sha256", sa.String()),
        sa.column("scorer_version", sa.String()),
        sa.column("pillow_version", sa.String()),
        sa.column("state", sa.String()),
        sa.column("asset_count", sa.Integer()),
        sa.column("completed_at", sa.DateTime(timezone=True)),
    )
    scores = _asset_scores_table()
    rankings = _asset_rankings_table()
    tasks = sa.table(
        "review_tasks",
        sa.column("id", sa.Uuid()),
        sa.column("release_version_id", sa.Uuid()),
        sa.column("scoring_run_id", sa.Uuid()),
        sa.column("scoring_config_sha256", sa.String()),
        sa.column("scoring_input_manifest_sha256", sa.String()),
        sa.column("ranking_manifest_sha256", sa.String()),
        sa.column("desired_accepted_count", sa.Integer()),
        sa.column("ranked_asset_count", sa.Integer()),
        sa.column("state", sa.String()),
    )
    decisions = sa.table(
        "review_decisions",
        sa.column("id", sa.Uuid()),
        sa.column("review_task_id", sa.Uuid()),
        sa.column("scoring_run_id", sa.Uuid()),
        sa.column("asset_id", sa.Uuid()),
        sa.column("revision", sa.Integer()),
        sa.column("decision", sa.String()),
    )

    completed_runs = bind.execute(
        sa.select(runs).where(runs.c.state == "completed").order_by(runs.c.id)
    ).mappings()
    manifests: dict[str, str] = {}
    for run in completed_runs:
        rows = list(
            bind.execute(
                sa.select(
                    *[column.label(f"ranking_{column.name}") for column in rankings.c],
                    *[column.label(f"score_{column.name}") for column in scores.c],
                )
                .select_from(
                    rankings.join(
                        scores,
                        (scores.c.id == rankings.c.asset_score_id)
                        & (scores.c.scoring_run_id == rankings.c.scoring_run_id)
                        & (scores.c.asset_id == rankings.c.asset_id),
                    )
                )
                .where(rankings.c.scoring_run_id == run["id"])
                .order_by(rankings.c.rank)
            ).mappings()
        )
        _validate_backfill_ranking(run, rows)
        digest = _ranking_manifest_sha256(run, rows)
        bind.execute(
            runs.update().where(runs.c.id == run["id"]).values(ranking_manifest_sha256=digest)
        )
        manifests[str(run["id"])] = digest

    task_rows = list(bind.execute(sa.select(tasks).order_by(tasks.c.id)).mappings())
    for task in task_rows:
        run = (
            bind.execute(sa.select(runs).where(runs.c.id == task["scoring_run_id"]))
            .mappings()
            .one_or_none()
        )
        digest = manifests.get(str(task["scoring_run_id"]))
        if (
            run is None
            or digest is None
            or run["state"] != "completed"
            or run["release_version_id"] != task["release_version_id"]
            or run["config_sha256"] != task["scoring_config_sha256"]
            or run["input_manifest_sha256"] != task["scoring_input_manifest_sha256"]
            or run["asset_count"] != task["ranked_asset_count"]
        ):
            raise RuntimeError("revision 0008 cannot backfill an inconsistent review task")
        bind.execute(
            tasks.update().where(tasks.c.id == task["id"]).values(ranking_manifest_sha256=digest)
        )

    task_run = (
        sa.select(tasks.c.scoring_run_id)
        .where(tasks.c.id == decisions.c.review_task_id)
        .scalar_subquery()
    )
    bind.execute(decisions.update().values(scoring_run_id=task_run))
    if bind.scalar(
        sa.select(sa.func.count())
        .select_from(decisions)
        .where(decisions.c.scoring_run_id.is_(None))
    ):
        raise RuntimeError("revision 0008 cannot bind a review decision to its scoring run")

    invalid_membership = bind.scalar(
        sa.select(sa.func.count())
        .select_from(
            decisions.outerjoin(
                rankings,
                (rankings.c.scoring_run_id == decisions.c.scoring_run_id)
                & (rankings.c.asset_id == decisions.c.asset_id),
            )
        )
        .where(rankings.c.id.is_(None))
    )
    if invalid_membership:
        raise RuntimeError("revision 0008 found a review decision outside its ranking snapshot")

    for task in task_rows:
        if task["state"] != "completed":
            continue
        latest: dict[str, Mapping[str, Any]] = {}
        for decision in bind.execute(
            sa.select(decisions)
            .where(decisions.c.review_task_id == task["id"])
            .order_by(decisions.c.asset_id, decisions.c.revision)
        ).mappings():
            latest[str(decision["asset_id"])] = decision
        accepted = sum(1 for decision in latest.values() if decision["decision"] == "accept")
        if accepted != task["desired_accepted_count"]:
            raise RuntimeError("revision 0008 found a completed review task with an invalid target")


def _asset_scores_table() -> sa.TableClause:
    return sa.table(
        "asset_scores",
        sa.column("id", sa.Uuid()),
        sa.column("scoring_run_id", sa.Uuid()),
        sa.column("asset_id", sa.Uuid()),
        sa.column("asset_storage_backend", sa.String()),
        sa.column("asset_storage_bucket", sa.String()),
        sa.column("asset_sha256", sa.String()),
        sa.column("asset_object_key", sa.String()),
        sa.column("asset_object_version_id", sa.String()),
        sa.column("asset_byte_size", sa.BigInteger()),
        sa.column("asset_image_format", sa.String()),
        sa.column("asset_width", sa.Integer()),
        sa.column("asset_height", sa.Integer()),
        sa.column("state", sa.String()),
        sa.column("luminance_mean_micros", sa.Integer()),
        sa.column("luminance_std_micros", sa.Integer()),
        sa.column("dynamic_range_micros", sa.Integer()),
        sa.column("entropy_bits_micros", sa.Integer()),
        sa.column("entropy_normalized_micros", sa.Integer()),
        sa.column("sharpness_micros", sa.Integer()),
        sa.column("dhash_hex", sa.String()),
        sa.column("aggregate_score_micros", sa.Integer()),
        sa.column("score_breakdown", json_type),
        sa.column("signal_detail", json_type),
        sa.column("scorer_version", sa.String()),
        sa.column("pillow_version", sa.String()),
        sa.column("config_sha256", sa.String()),
        sa.column("last_error_code", sa.String()),
        sa.column("last_error_detail", sa.Text()),
        sa.column("completed_at", sa.DateTime(timezone=True)),
    )


def _asset_rankings_table() -> sa.TableClause:
    return sa.table(
        "asset_rankings",
        sa.column("id", sa.Uuid()),
        sa.column("scoring_run_id", sa.Uuid()),
        sa.column("asset_score_id", sa.Uuid()),
        sa.column("asset_id", sa.Uuid()),
        sa.column("rank", sa.Integer()),
        sa.column("aggregate_score_micros", sa.Integer()),
        sa.column("disposition", sa.String()),
        sa.column("explanation", json_type),
        sa.column("duplicate_cluster_id", sa.String()),
        sa.column("duplicate_representative_asset_id", sa.Uuid()),
        sa.column("is_duplicate_representative", sa.Boolean()),
        sa.column("scorer_version", sa.String()),
        sa.column("pillow_version", sa.String()),
        sa.column("config_sha256", sa.String()),
    )


def _validate_backfill_ranking(
    run: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
) -> None:
    expected_count = int(run["asset_count"])
    if (
        run["completed_at"] is None
        or len(rows) != expected_count
        or [row["ranking_rank"] for row in rows] != list(range(1, expected_count + 1))
    ):
        raise RuntimeError("revision 0008 cannot hash an incomplete completed scoring run")
    for row in rows:
        if (
            row["ranking_scoring_run_id"] != run["id"]
            or row["score_scoring_run_id"] != run["id"]
            or row["ranking_asset_score_id"] != row["score_id"]
            or row["ranking_asset_id"] != row["score_asset_id"]
            or row["score_state"] not in {"scored", "flagged_blank", "flagged_corrupt"}
            or row["score_completed_at"] is None
            or row["score_aggregate_score_micros"] is None
            or row["ranking_aggregate_score_micros"] != row["score_aggregate_score_micros"]
            or row["ranking_scorer_version"] != run["scorer_version"]
            or row["score_scorer_version"] != run["scorer_version"]
            or row["ranking_pillow_version"] != run["pillow_version"]
            or row["score_pillow_version"] != run["pillow_version"]
            or row["ranking_config_sha256"] != run["config_sha256"]
            or row["score_config_sha256"] != run["config_sha256"]
            or not isinstance(row["ranking_explanation"], dict)
            or not isinstance(row["score_signal_detail"], dict)
        ):
            raise RuntimeError("revision 0008 found inconsistent completed ranking rows")


def _ranking_manifest_sha256(
    run: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
) -> str:
    payload = {
        "schema": "ranking-manifest/v1",
        "scoring_run": {
            "id": str(run["id"]),
            "release_version_id": str(run["release_version_id"]),
            "asset_count": run["asset_count"],
            "config_sha256": run["config_sha256"],
            "input_manifest_sha256": run["input_manifest_sha256"],
            "scorer_version": run["scorer_version"],
            "pillow_version": run["pillow_version"],
        },
        "rankings": [_ranking_entry(row) for row in rows],
    }
    try:
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise RuntimeError("revision 0008 found non-canonical ranking data") from None
    return hashlib.sha256(serialized).hexdigest()


def _ranking_entry(row: Mapping[str, Any]) -> dict[str, Any]:
    representative_id = row["ranking_duplicate_representative_asset_id"]
    return {
        "rank": row["ranking_rank"],
        "asset_id": str(row["ranking_asset_id"]),
        "asset_score_id": str(row["ranking_asset_score_id"]),
        "aggregate_score_micros": row["ranking_aggregate_score_micros"],
        "disposition": str(row["ranking_disposition"]),
        "explanation": row["ranking_explanation"],
        "duplicate_cluster_id": row["ranking_duplicate_cluster_id"],
        "duplicate_representative_asset_id": (
            str(representative_id) if representative_id is not None else None
        ),
        "is_duplicate_representative": row["ranking_is_duplicate_representative"],
        "scorer_version": row["ranking_scorer_version"],
        "pillow_version": row["ranking_pillow_version"],
        "config_sha256": row["ranking_config_sha256"],
        "score": {
            "state": str(row["score_state"]),
            "asset_storage_backend": row["score_asset_storage_backend"],
            "asset_storage_bucket": row["score_asset_storage_bucket"],
            "asset_sha256": row["score_asset_sha256"],
            "asset_object_key": row["score_asset_object_key"],
            "asset_object_version_id": row["score_asset_object_version_id"],
            "asset_byte_size": row["score_asset_byte_size"],
            "asset_image_format": row["score_asset_image_format"],
            "asset_width": row["score_asset_width"],
            "asset_height": row["score_asset_height"],
            "luminance_mean_micros": row["score_luminance_mean_micros"],
            "luminance_std_micros": row["score_luminance_std_micros"],
            "dynamic_range_micros": row["score_dynamic_range_micros"],
            "entropy_bits_micros": row["score_entropy_bits_micros"],
            "entropy_normalized_micros": row["score_entropy_normalized_micros"],
            "sharpness_micros": row["score_sharpness_micros"],
            "dhash_hex": row["score_dhash_hex"],
            "aggregate_score_micros": row["score_aggregate_score_micros"],
            "score_breakdown": row["score_score_breakdown"],
            "signal_detail": row["score_signal_detail"],
            "scorer_version": row["score_scorer_version"],
            "pillow_version": row["score_pillow_version"],
            "config_sha256": row["score_config_sha256"],
            "last_error_code": row["score_last_error_code"],
            "last_error_detail": row["score_last_error_detail"],
        },
    }


def _drop_review_decision_guards(dialect: str) -> None:
    if dialect == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS review_decisions_reject_delete")
        op.execute("DROP TRIGGER IF EXISTS review_decisions_reject_update")
    elif dialect == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS review_decisions_reject_mutation ON review_decisions")


def _create_review_decision_guards(dialect: str) -> None:
    if dialect == "sqlite":
        op.execute(
            "CREATE TRIGGER review_decisions_reject_update "
            "BEFORE UPDATE ON review_decisions "
            "BEGIN "
            "SELECT RAISE(ABORT, 'review_decisions are append-only'); "
            "END"
        )
        op.execute(
            "CREATE TRIGGER review_decisions_reject_delete "
            "BEFORE DELETE ON review_decisions "
            "BEGIN "
            "SELECT RAISE(ABORT, 'review_decisions are append-only'); "
            "END"
        )
    elif dialect == "postgresql":
        op.execute(
            "CREATE OR REPLACE FUNCTION "
            "gen_automation_reject_review_decision_mutation() "
            "RETURNS trigger AS $$ "
            "BEGIN "
            "RAISE EXCEPTION 'review_decisions are append-only'; "
            "END; "
            "$$ LANGUAGE plpgsql"
        )
        op.execute(
            "CREATE TRIGGER review_decisions_reject_mutation "
            "BEFORE UPDATE OR DELETE ON review_decisions "
            "FOR EACH ROW EXECUTE FUNCTION "
            "gen_automation_reject_review_decision_mutation()"
        )


def _create_guards(dialect: str) -> None:
    _create_review_decision_guards(dialect)
    if dialect == "sqlite":
        _create_sqlite_guards()
    elif dialect == "postgresql":
        _create_postgresql_guards()


def _drop_all_guards(dialect: str) -> None:
    _drop_review_decision_guards(dialect)
    if dialect == "sqlite":
        for trigger in (
            "scoring_runs_guard_completed_update",
            "scoring_runs_guard_completed_delete",
            "scoring_runs_validate_completion",
            "asset_scores_guard_frozen_update",
            "asset_scores_guard_frozen_delete",
            "asset_scores_guard_late_insert",
            "asset_rankings_reject_update",
            "asset_rankings_reject_delete",
            "asset_rankings_guard_late_insert",
            "review_tasks_guard_update",
            "review_tasks_reject_delete",
        ):
            op.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    elif dialect == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS scoring_runs_guard_mutation ON scoring_runs")
        op.execute("DROP TRIGGER IF EXISTS asset_scores_guard_mutation ON asset_scores")
        op.execute("DROP TRIGGER IF EXISTS asset_rankings_guard_mutation ON asset_rankings")
        op.execute("DROP TRIGGER IF EXISTS review_tasks_guard_mutation ON review_tasks")
        for function in (
            "gen_automation_guard_scoring_run_mutation",
            "gen_automation_guard_asset_score_mutation",
            "gen_automation_guard_asset_ranking_mutation",
            "gen_automation_guard_review_task_mutation",
        ):
            op.execute(f"DROP FUNCTION IF EXISTS {function}()")


def _create_sqlite_guards() -> None:
    statements = (
        "CREATE TRIGGER scoring_runs_guard_completed_update "
        "BEFORE UPDATE ON scoring_runs WHEN OLD.state = 'completed' "
        "BEGIN SELECT RAISE(ABORT, 'completed scoring runs are immutable'); END",
        "CREATE TRIGGER scoring_runs_guard_completed_delete "
        "BEFORE DELETE ON scoring_runs WHEN OLD.state = 'completed' "
        "BEGIN SELECT RAISE(ABORT, 'completed scoring runs are immutable'); END",
        "CREATE TRIGGER scoring_runs_validate_completion "
        "BEFORE UPDATE ON scoring_runs "
        "WHEN OLD.state = 'running' AND NEW.state = 'completed' "
        "BEGIN SELECT CASE WHEN "
        "(SELECT count(*) FROM asset_rankings "
        "WHERE scoring_run_id = NEW.id) <> NEW.asset_count "
        "OR (SELECT min(rank) FROM asset_rankings "
        "WHERE scoring_run_id = NEW.id) <> 1 "
        "OR (SELECT max(rank) FROM asset_rankings "
        "WHERE scoring_run_id = NEW.id) <> NEW.asset_count "
        "OR EXISTS (SELECT 1 FROM asset_rankings AS ranking "
        "LEFT JOIN asset_scores AS score "
        "ON score.id = ranking.asset_score_id "
        "AND score.scoring_run_id = ranking.scoring_run_id "
        "AND score.asset_id = ranking.asset_id "
        "WHERE ranking.scoring_run_id = NEW.id "
        "AND (score.id IS NULL "
        "OR score.state NOT IN ('scored', 'flagged_blank', 'flagged_corrupt') "
        "OR score.completed_at IS NULL "
        "OR score.aggregate_score_micros IS NULL "
        "OR score.aggregate_score_micros <> ranking.aggregate_score_micros "
        "OR score.scorer_version <> NEW.scorer_version "
        "OR ranking.scorer_version <> NEW.scorer_version "
        "OR score.pillow_version <> NEW.pillow_version "
        "OR ranking.pillow_version <> NEW.pillow_version "
        "OR score.config_sha256 <> NEW.config_sha256 "
        "OR ranking.config_sha256 <> NEW.config_sha256)) "
        "THEN RAISE(ABORT, 'scoring run completion snapshot is invalid') END; END",
        "CREATE TRIGGER asset_scores_guard_frozen_update "
        "BEFORE UPDATE ON asset_scores "
        "WHEN OLD.state IN ('scored', 'flagged_blank', 'flagged_corrupt', 'dead_letter') "
        "OR EXISTS (SELECT 1 FROM scoring_runs "
        "WHERE id = OLD.scoring_run_id AND state = 'completed') "
        "BEGIN SELECT RAISE(ABORT, 'terminal asset scores are immutable'); END",
        "CREATE TRIGGER asset_scores_guard_frozen_delete "
        "BEFORE DELETE ON asset_scores "
        "WHEN OLD.state IN ('scored', 'flagged_blank', 'flagged_corrupt', 'dead_letter') "
        "OR EXISTS (SELECT 1 FROM scoring_runs "
        "WHERE id = OLD.scoring_run_id AND state = 'completed') "
        "BEGIN SELECT RAISE(ABORT, 'terminal asset scores are immutable'); END",
        "CREATE TRIGGER asset_scores_guard_late_insert "
        "BEFORE INSERT ON asset_scores "
        "WHEN EXISTS (SELECT 1 FROM scoring_runs "
        "WHERE id = NEW.scoring_run_id AND state = 'completed') "
        "BEGIN SELECT RAISE(ABORT, 'completed scoring runs reject new scores'); END",
        "CREATE TRIGGER asset_rankings_reject_update "
        "BEFORE UPDATE ON asset_rankings "
        "BEGIN SELECT RAISE(ABORT, 'asset rankings are append-only'); END",
        "CREATE TRIGGER asset_rankings_reject_delete "
        "BEFORE DELETE ON asset_rankings "
        "BEGIN SELECT RAISE(ABORT, 'asset rankings are append-only'); END",
        "CREATE TRIGGER asset_rankings_guard_late_insert "
        "BEFORE INSERT ON asset_rankings "
        "WHEN EXISTS (SELECT 1 FROM scoring_runs "
        "WHERE id = NEW.scoring_run_id AND state = 'completed') "
        "BEGIN SELECT RAISE(ABORT, 'completed scoring runs reject new rankings'); END",
        "CREATE TRIGGER review_tasks_guard_update "
        "BEFORE UPDATE ON review_tasks BEGIN "
        "SELECT CASE WHEN OLD.state <> 'open' "
        "OR NEW.lock_version <> OLD.lock_version + 1 "
        "OR OLD.id IS NOT NEW.id "
        "OR OLD.release_version_id IS NOT NEW.release_version_id "
        "OR OLD.release_version_no IS NOT NEW.release_version_no "
        "OR OLD.release_specification_sha256 IS NOT NEW.release_specification_sha256 "
        "OR OLD.scoring_run_id IS NOT NEW.scoring_run_id "
        "OR OLD.scoring_config_sha256 IS NOT NEW.scoring_config_sha256 "
        "OR OLD.scoring_input_manifest_sha256 IS NOT NEW.scoring_input_manifest_sha256 "
        "OR OLD.ranking_manifest_sha256 IS NOT NEW.ranking_manifest_sha256 "
        "OR OLD.desired_accepted_count IS NOT NEW.desired_accepted_count "
        "OR OLD.ranked_asset_count IS NOT NEW.ranked_asset_count "
        "OR OLD.created_by_user_id IS NOT NEW.created_by_user_id "
        "OR OLD.created_at IS NOT NEW.created_at "
        "THEN RAISE(ABORT, 'review task identity is immutable') END; "
        "SELECT CASE WHEN NEW.state = 'completed' AND ("
        "SELECT count(*) FROM review_decisions AS decision "
        "WHERE decision.review_task_id = OLD.id "
        "AND decision.decision = 'accept' "
        "AND NOT EXISTS (SELECT 1 FROM review_decisions AS newer "
        "WHERE newer.review_task_id = decision.review_task_id "
        "AND newer.asset_id = decision.asset_id "
        "AND newer.revision > decision.revision)"
        ") <> OLD.desired_accepted_count "
        "THEN RAISE(ABORT, 'review task acceptance target is not satisfied') END; END",
        "CREATE TRIGGER review_tasks_reject_delete "
        "BEFORE DELETE ON review_tasks "
        "BEGIN SELECT RAISE(ABORT, 'review tasks cannot be deleted'); END",
    )
    for statement in statements:
        op.execute(statement)


def _create_postgresql_guards() -> None:
    op.execute(
        "CREATE OR REPLACE FUNCTION gen_automation_guard_scoring_run_mutation() "
        "RETURNS trigger AS $$ "
        "DECLARE ranking_count integer; minimum_rank integer; maximum_rank integer; "
        "BEGIN "
        "IF TG_OP = 'DELETE' THEN "
        "IF OLD.state = 'completed' THEN "
        "RAISE EXCEPTION 'completed scoring runs are immutable'; END IF; "
        "RETURN OLD; END IF; "
        "IF OLD.state = 'completed' THEN "
        "RAISE EXCEPTION 'completed scoring runs are immutable'; END IF; "
        "IF OLD.state = 'running' AND NEW.state = 'completed' THEN "
        "SELECT count(*), min(rank), max(rank) "
        "INTO ranking_count, minimum_rank, maximum_rank "
        "FROM asset_rankings WHERE scoring_run_id = NEW.id; "
        "IF ranking_count <> NEW.asset_count OR minimum_rank <> 1 "
        "OR maximum_rank <> NEW.asset_count OR EXISTS ("
        "SELECT 1 FROM asset_rankings AS ranking "
        "LEFT JOIN asset_scores AS score "
        "ON score.id = ranking.asset_score_id "
        "AND score.scoring_run_id = ranking.scoring_run_id "
        "AND score.asset_id = ranking.asset_id "
        "WHERE ranking.scoring_run_id = NEW.id "
        "AND (score.id IS NULL "
        "OR score.state NOT IN ('scored', 'flagged_blank', 'flagged_corrupt') "
        "OR score.completed_at IS NULL "
        "OR score.aggregate_score_micros IS NULL "
        "OR score.aggregate_score_micros <> ranking.aggregate_score_micros "
        "OR score.scorer_version <> NEW.scorer_version "
        "OR ranking.scorer_version <> NEW.scorer_version "
        "OR score.pillow_version <> NEW.pillow_version "
        "OR ranking.pillow_version <> NEW.pillow_version "
        "OR score.config_sha256 <> NEW.config_sha256 "
        "OR ranking.config_sha256 <> NEW.config_sha256)) THEN "
        "RAISE EXCEPTION 'scoring run completion snapshot is invalid'; END IF; "
        "END IF; RETURN NEW; END; $$ LANGUAGE plpgsql"
    )
    op.execute(
        "CREATE TRIGGER scoring_runs_guard_mutation "
        "BEFORE UPDATE OR DELETE ON scoring_runs "
        "FOR EACH ROW EXECUTE FUNCTION gen_automation_guard_scoring_run_mutation()"
    )
    op.execute(
        "CREATE OR REPLACE FUNCTION gen_automation_guard_asset_score_mutation() "
        "RETURNS trigger AS $$ BEGIN "
        "IF TG_OP = 'INSERT' THEN "
        "IF EXISTS (SELECT 1 FROM scoring_runs "
        "WHERE id = NEW.scoring_run_id AND state = 'completed') THEN "
        "RAISE EXCEPTION 'completed scoring runs reject new scores'; END IF; "
        "RETURN NEW; END IF; "
        "IF OLD.state IN ('scored', 'flagged_blank', 'flagged_corrupt', 'dead_letter') "
        "OR EXISTS (SELECT 1 FROM scoring_runs "
        "WHERE id = OLD.scoring_run_id AND state = 'completed') THEN "
        "RAISE EXCEPTION 'terminal asset scores are immutable'; END IF; "
        "IF TG_OP = 'DELETE' THEN RETURN OLD; END IF; "
        "RETURN NEW; END; $$ LANGUAGE plpgsql"
    )
    op.execute(
        "CREATE TRIGGER asset_scores_guard_mutation "
        "BEFORE INSERT OR UPDATE OR DELETE ON asset_scores "
        "FOR EACH ROW EXECUTE FUNCTION gen_automation_guard_asset_score_mutation()"
    )
    op.execute(
        "CREATE OR REPLACE FUNCTION gen_automation_guard_asset_ranking_mutation() "
        "RETURNS trigger AS $$ BEGIN "
        "IF TG_OP = 'INSERT' THEN "
        "IF EXISTS (SELECT 1 FROM scoring_runs "
        "WHERE id = NEW.scoring_run_id AND state = 'completed') THEN "
        "RAISE EXCEPTION 'completed scoring runs reject new rankings'; END IF; "
        "RETURN NEW; END IF; "
        "RAISE EXCEPTION 'asset rankings are append-only'; "
        "END; $$ LANGUAGE plpgsql"
    )
    op.execute(
        "CREATE TRIGGER asset_rankings_guard_mutation "
        "BEFORE INSERT OR UPDATE OR DELETE ON asset_rankings "
        "FOR EACH ROW EXECUTE FUNCTION gen_automation_guard_asset_ranking_mutation()"
    )
    op.execute(
        "CREATE OR REPLACE FUNCTION gen_automation_guard_review_task_mutation() "
        "RETURNS trigger AS $$ DECLARE accepted_count integer; BEGIN "
        "IF TG_OP = 'DELETE' THEN "
        "RAISE EXCEPTION 'review tasks cannot be deleted'; END IF; "
        "IF OLD.state <> 'open' OR NEW.lock_version <> OLD.lock_version + 1 "
        "OR OLD.id IS DISTINCT FROM NEW.id "
        "OR OLD.release_version_id IS DISTINCT FROM NEW.release_version_id "
        "OR OLD.release_version_no IS DISTINCT FROM NEW.release_version_no "
        "OR OLD.release_specification_sha256 IS DISTINCT FROM "
        "NEW.release_specification_sha256 "
        "OR OLD.scoring_run_id IS DISTINCT FROM NEW.scoring_run_id "
        "OR OLD.scoring_config_sha256 IS DISTINCT FROM NEW.scoring_config_sha256 "
        "OR OLD.scoring_input_manifest_sha256 IS DISTINCT FROM "
        "NEW.scoring_input_manifest_sha256 "
        "OR OLD.ranking_manifest_sha256 IS DISTINCT FROM NEW.ranking_manifest_sha256 "
        "OR OLD.desired_accepted_count IS DISTINCT FROM NEW.desired_accepted_count "
        "OR OLD.ranked_asset_count IS DISTINCT FROM NEW.ranked_asset_count "
        "OR OLD.created_by_user_id IS DISTINCT FROM NEW.created_by_user_id "
        "OR OLD.created_at IS DISTINCT FROM NEW.created_at THEN "
        "RAISE EXCEPTION 'review task identity is immutable'; END IF; "
        "IF NEW.state = 'completed' THEN "
        "SELECT count(*) INTO accepted_count "
        "FROM review_decisions AS decision "
        "WHERE decision.review_task_id = OLD.id "
        "AND decision.decision = 'accept' "
        "AND NOT EXISTS (SELECT 1 FROM review_decisions AS newer "
        "WHERE newer.review_task_id = decision.review_task_id "
        "AND newer.asset_id = decision.asset_id "
        "AND newer.revision > decision.revision); "
        "IF accepted_count <> OLD.desired_accepted_count THEN "
        "RAISE EXCEPTION 'review task acceptance target is not satisfied'; END IF; "
        "END IF; RETURN NEW; END; $$ LANGUAGE plpgsql"
    )
    op.execute(
        "CREATE TRIGGER review_tasks_guard_mutation "
        "BEFORE UPDATE OR DELETE ON review_tasks "
        "FOR EACH ROW EXECUTE FUNCTION gen_automation_guard_review_task_mutation()"
    )
