"""Add durable human review tasks and append-only decision revisions.

Revision ID: 20260728_0006
Revises: 20260728_0005
Create Date: 2026-07-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260728_0006"
down_revision: str | None = "20260728_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

review_task_state = sa.Enum(
    "open",
    "completed",
    "cancelled",
    name="review_task_state",
    native_enum=False,
    create_constraint=True,
)
review_decision_value = sa.Enum(
    "accept",
    "reject",
    "hold",
    name="review_decision_value",
    native_enum=False,
    create_constraint=True,
)


def upgrade() -> None:
    op.create_table(
        "review_tasks",
        sa.Column("release_version_id", sa.Uuid(), nullable=False),
        sa.Column("release_version_no", sa.Integer(), nullable=False),
        sa.Column(
            "release_specification_sha256",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column("scoring_run_id", sa.Uuid(), nullable=False),
        sa.Column("scoring_config_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "scoring_input_manifest_sha256",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column("desired_accepted_count", sa.Integer(), nullable=False),
        sa.Column("ranked_asset_count", sa.Integer(), nullable=False),
        sa.Column("state", review_task_state, nullable=False),
        sa.Column("lock_version", sa.Integer(), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "release_version_no > 0",
            name=op.f("ck_review_tasks_positive_release_version_no"),
        ),
        sa.CheckConstraint(
            "desired_accepted_count > 0",
            name=op.f("ck_review_tasks_positive_desired_accepted_count"),
        ),
        sa.CheckConstraint(
            "ranked_asset_count > 0",
            name=op.f("ck_review_tasks_positive_ranked_asset_count"),
        ),
        sa.CheckConstraint(
            "desired_accepted_count <= ranked_asset_count",
            name=op.f("ck_review_tasks_desired_count_within_ranked_assets"),
        ),
        sa.CheckConstraint(
            "lock_version > 0",
            name=op.f("ck_review_tasks_positive_lock_version"),
        ),
        sa.CheckConstraint(
            "(state = 'open' "
            "AND completed_by_user_id IS NULL AND completed_at IS NULL "
            "AND cancelled_by_user_id IS NULL AND cancelled_at IS NULL) "
            "OR (state = 'completed' "
            "AND completed_by_user_id IS NOT NULL AND completed_at IS NOT NULL "
            "AND cancelled_by_user_id IS NULL AND cancelled_at IS NULL) "
            "OR (state = 'cancelled' "
            "AND completed_by_user_id IS NULL AND completed_at IS NULL "
            "AND cancelled_by_user_id IS NOT NULL AND cancelled_at IS NOT NULL)",
            name=op.f("ck_review_tasks_terminal_state_metadata"),
        ),
        sa.CheckConstraint(
            "completed_at IS NULL OR completed_at >= created_at",
            name=op.f("ck_review_tasks_valid_completion_time"),
        ),
        sa.CheckConstraint(
            "cancelled_at IS NULL OR cancelled_at >= created_at",
            name=op.f("ck_review_tasks_valid_cancellation_time"),
        ),
        sa.ForeignKeyConstraint(
            ["cancelled_by_user_id"],
            ["admin_users.id"],
            name=op.f("fk_review_tasks_cancelled_by_user_id_admin_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["completed_by_user_id"],
            ["admin_users.id"],
            name=op.f("fk_review_tasks_completed_by_user_id_admin_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["admin_users.id"],
            name=op.f("fk_review_tasks_created_by_user_id_admin_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["release_version_id"],
            ["release_versions.id"],
            name=op.f("fk_review_tasks_release_version_id_release_versions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["scoring_run_id"],
            ["scoring_runs.id"],
            name=op.f("fk_review_tasks_scoring_run_id_scoring_runs"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_review_tasks")),
        sa.UniqueConstraint(
            "scoring_run_id",
            name="uq_review_tasks_scoring_run",
        ),
    )
    op.create_index(
        op.f("ix_review_tasks_release_version_id"),
        "review_tasks",
        ["release_version_id"],
    )
    op.create_index(
        "ix_review_tasks_state_created_at",
        "review_tasks",
        ["state", "created_at"],
    )

    op.create_table(
        "review_decisions",
        sa.Column("review_task_id", sa.Uuid(), nullable=False),
        sa.Column("asset_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("decision", review_decision_value, nullable=False),
        sa.Column("reason_code", sa.String(length=100), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("decided_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("supersedes_revision", sa.Integer(), nullable=True),
        sa.Column("supersedes_decision_id", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "revision > 0",
            name=op.f("ck_review_decisions_positive_revision"),
        ),
        sa.CheckConstraint(
            "(revision = 1 AND supersedes_revision IS NULL "
            "AND supersedes_decision_id IS NULL) "
            "OR (revision > 1 AND supersedes_revision = revision - 1 "
            "AND supersedes_decision_id IS NOT NULL)",
            name=op.f("ck_review_decisions_linear_revision_chain"),
        ),
        sa.CheckConstraint(
            "reason_code IS NULL OR length(trim(reason_code)) > 0",
            name=op.f("ck_review_decisions_nonempty_reason_code"),
        ),
        sa.CheckConstraint(
            "note IS NULL OR length(trim(note)) > 0",
            name=op.f("ck_review_decisions_nonempty_note"),
        ),
        sa.ForeignKeyConstraint(
            ["asset_id"],
            ["assets.id"],
            name=op.f("fk_review_decisions_asset_id_assets"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["decided_by_user_id"],
            ["admin_users.id"],
            name=op.f("fk_review_decisions_decided_by_user_id_admin_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            [
                "review_task_id",
                "asset_id",
                "supersedes_revision",
                "supersedes_decision_id",
            ],
            [
                "review_decisions.review_task_id",
                "review_decisions.asset_id",
                "review_decisions.revision",
                "review_decisions.id",
            ],
            name="fk_review_decisions_prior_revision",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["review_task_id"],
            ["review_tasks.id"],
            name=op.f("fk_review_decisions_review_task_id_review_tasks"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_review_decisions")),
        sa.UniqueConstraint(
            "review_task_id",
            "asset_id",
            "revision",
            name="uq_review_decisions_task_asset_revision",
        ),
        sa.UniqueConstraint(
            "review_task_id",
            "asset_id",
            "revision",
            "id",
            name="uq_review_decisions_chain_target",
        ),
        sa.UniqueConstraint(
            "supersedes_decision_id",
            name="uq_review_decisions_superseded_once",
        ),
    )
    op.create_index(
        "ix_review_decisions_decided_by_user_id",
        "review_decisions",
        ["decided_by_user_id"],
    )
    op.create_index(
        "ix_review_decisions_task_asset_revision",
        "review_decisions",
        ["review_task_id", "asset_id", "revision"],
    )
    dialect = op.get_bind().dialect.name
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


def downgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS review_decisions_reject_delete")
        op.execute("DROP TRIGGER IF EXISTS review_decisions_reject_update")
    elif dialect == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS review_decisions_reject_mutation ON review_decisions")
        op.execute("DROP FUNCTION IF EXISTS gen_automation_reject_review_decision_mutation()")

    op.drop_index(
        "ix_review_decisions_task_asset_revision",
        table_name="review_decisions",
    )
    op.drop_index(
        "ix_review_decisions_decided_by_user_id",
        table_name="review_decisions",
    )
    op.drop_table("review_decisions")

    op.drop_index(
        "ix_review_tasks_state_created_at",
        table_name="review_tasks",
    )
    op.drop_index(
        op.f("ix_review_tasks_release_version_id"),
        table_name="review_tasks",
    )
    op.drop_table("review_tasks")
