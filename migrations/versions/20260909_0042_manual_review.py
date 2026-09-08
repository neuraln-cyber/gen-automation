"""Allow explicit unscored review snapshots without weakening frozen rankings."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260909_0042"
down_revision: str | None = "20260905_0041"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_STATES = (
    "'pending', 'processing', 'retry_wait', "
    "'scored', 'flagged_blank', 'flagged_corrupt', 'dead_letter'"
)
_TERMINAL = "'scored', 'flagged_blank', 'flagged_corrupt', 'dead_letter'"
_RANKABLE = "'scored', 'flagged_blank', 'flagged_corrupt'"
_NO_MEASUREMENTS = (
    "state <> 'skipped' OR (scorer_version = 'manual-review-v1' "
    "AND aggregate_score_micros = 0 "
    "AND luminance_mean_micros IS NULL AND luminance_std_micros IS NULL "
    "AND dynamic_range_micros IS NULL AND entropy_bits_micros IS NULL "
    "AND entropy_normalized_micros IS NULL AND sharpness_micros IS NULL "
    "AND dhash_hex IS NULL AND score_breakdown IS NULL)"
)


def _rewrite_guards(sql: str, *, upgrade: bool) -> str:
    # Both sides are known literals in the existing immutable-row guards.
    for old in (_TERMINAL, _RANKABLE):
        new = old + ", 'skipped'"
        sql = sql.replace(old + ")", new + ")") if upgrade else sql.replace(new + ")", old + ")")
    return sql


def _change(*, upgrade: bool) -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name
    sqlite_guards: list[tuple[str, str]] = []
    if dialect == "sqlite":
        sqlite_guards = list(
            bind.execute(
                sa.text(
                    "SELECT name, sql FROM sqlite_master "
                    "WHERE type='trigger' AND sql LIKE '%asset_scores%'"
                )
            ).tuples()
        )
        for name, _ in sqlite_guards:
            quoted = bind.dialect.identifier_preparer.quote(name)
            op.execute(f"DROP TRIGGER {quoted}")
    states = _OLD_STATES + (", 'skipped'" if upgrade else "")
    terminal = _TERMINAL + (", 'skipped'" if upgrade else "")
    with op.batch_alter_table("asset_scores") as batch:
        batch.drop_constraint(op.f("ck_asset_scores_asset_score_state"), type_="check")
        batch.create_check_constraint(
            op.f("ck_asset_scores_asset_score_state"), f"state IN ({states})"
        )
        batch.drop_constraint(op.f("ck_asset_scores_terminal_is_completed"), type_="check")
        batch.create_check_constraint(
            op.f("ck_asset_scores_terminal_is_completed"),
            f"state NOT IN ({terminal}) OR completed_at IS NOT NULL",
        )
        if upgrade:
            batch.create_check_constraint(
                op.f("ck_asset_scores_skipped_has_no_measurements"), _NO_MEASUREMENTS
            )
        else:
            batch.drop_constraint(
                op.f("ck_asset_scores_skipped_has_no_measurements"), type_="check"
            )
    if dialect == "sqlite":
        for _, sql in sqlite_guards:
            op.execute(_rewrite_guards(sql, upgrade=upgrade))
    elif dialect == "postgresql":
        for name in (
            "gen_automation_guard_scoring_run_mutation",
            "gen_automation_guard_asset_score_mutation",
        ):
            sql = bind.execute(
                sa.text("SELECT pg_get_functiondef(to_regprocedure(:name))"), {"name": name + "()"}
            ).scalar_one()
            if not isinstance(sql, str):
                raise RuntimeError("Required ranking integrity guard is missing")
            rewritten = _rewrite_guards(sql, upgrade=upgrade)
            if sql == rewritten:
                raise RuntimeError("Ranking integrity guard did not match the expected contract")
            op.execute(rewritten)


def upgrade() -> None:
    _change(upgrade=True)


def downgrade() -> None:
    if (
        op.get_bind()
        .execute(sa.text("SELECT 1 FROM asset_scores WHERE state='skipped' LIMIT 1"))
        .first()
    ):
        raise RuntimeError(
            "Manual review history exists: re-enable features by configuration; "
            "do not delete history to downgrade."
        )
    _change(upgrade=False)
