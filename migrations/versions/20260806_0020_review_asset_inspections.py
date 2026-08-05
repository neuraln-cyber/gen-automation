"""Add durable per-owner/reviewer review inspections.

Revision ID: 20260806_0020
Revises: 20260805_0019
Create Date: 2026-08-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260806_0020"
down_revision: str | None = "20260805_0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "review_asset_inspections",
        sa.Column("review_task_id", sa.Uuid(), nullable=False),
        sa.Column("scoring_run_id", sa.Uuid(), nullable=False),
        sa.Column("asset_id", sa.Uuid(), nullable=False),
        sa.Column("inspected_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("inspected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["asset_id"],
            ["assets.id"],
            name=op.f("fk_review_asset_inspections_asset_id_assets"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["inspected_by_user_id"],
            ["admin_users.id"],
            name=op.f("fk_review_asset_inspections_inspected_by_user_id_admin_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["review_task_id", "scoring_run_id"],
            ["review_tasks.id", "review_tasks.scoring_run_id"],
            name="fk_review_asset_inspections_task_scoring_run",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["scoring_run_id", "asset_id"],
            ["asset_rankings.scoring_run_id", "asset_rankings.asset_id"],
            name="fk_review_asset_inspections_ranking_membership",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_review_asset_inspections")),
        sa.UniqueConstraint(
            "review_task_id",
            "asset_id",
            "inspected_by_user_id",
            name="uq_review_asset_inspections_task_asset_user",
        ),
    )
    op.create_index(
        "ix_review_asset_inspections_task_user",
        "review_asset_inspections",
        ["review_task_id", "inspected_by_user_id"],
    )
    _create_guards()


def downgrade() -> None:
    _drop_guards()
    op.drop_index(
        "ix_review_asset_inspections_task_user",
        table_name="review_asset_inspections",
    )
    op.drop_table("review_asset_inspections")


def _create_guards() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        op.execute(
            "CREATE TRIGGER review_asset_inspections_guard_insert "
            "BEFORE INSERT ON review_asset_inspections BEGIN "
            "SELECT CASE WHEN NOT EXISTS ("
            "SELECT 1 FROM review_tasks AS task "
            "WHERE task.id = NEW.review_task_id AND task.state = 'open'"
            ") THEN RAISE(ABORT, 'review inspections require an open task') END; END"
        )
        op.execute(
            "CREATE TRIGGER review_asset_inspections_guard_update "
            "BEFORE UPDATE ON review_asset_inspections BEGIN "
            "SELECT RAISE(ABORT, 'review asset inspections are append-only'); END"
        )
        op.execute(
            "CREATE TRIGGER review_asset_inspections_guard_delete "
            "BEFORE DELETE ON review_asset_inspections BEGIN "
            "SELECT RAISE(ABORT, 'review asset inspections are append-only'); END"
        )
        return
    op.execute(
        "CREATE OR REPLACE FUNCTION gen_automation_guard_review_asset_inspection() "
        "RETURNS trigger AS $$ BEGIN "
        "IF TG_OP <> 'INSERT' THEN "
        "RAISE EXCEPTION 'review asset inspections are append-only'; END IF; "
        "IF NOT EXISTS (SELECT 1 FROM review_tasks AS task "
        "WHERE task.id = NEW.review_task_id AND task.state = 'open') THEN "
        "RAISE EXCEPTION 'review inspections require an open task'; END IF; "
        "RETURN NEW; END; $$ LANGUAGE plpgsql"
    )
    op.execute(
        "CREATE TRIGGER review_asset_inspections_guard_mutation "
        "BEFORE INSERT OR UPDATE OR DELETE ON review_asset_inspections "
        "FOR EACH ROW EXECUTE FUNCTION gen_automation_guard_review_asset_inspection()"
    )


def _drop_guards() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS review_asset_inspections_guard_delete")
        op.execute("DROP TRIGGER IF EXISTS review_asset_inspections_guard_update")
        op.execute("DROP TRIGGER IF EXISTS review_asset_inspections_guard_insert")
        return
    op.execute(
        "DROP TRIGGER IF EXISTS review_asset_inspections_guard_mutation "
        "ON review_asset_inspections"
    )
    op.execute("DROP FUNCTION IF EXISTS gen_automation_guard_review_asset_inspection()")
