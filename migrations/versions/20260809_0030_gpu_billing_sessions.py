"""Persist exact Salad running-instance billing sessions.

Revision ID: 20260809_0030
Revises: 20260809_0029
Create Date: 2026-08-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260809_0030"
down_revision: str | None = "20260809_0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("salad_deployments") as batch_op:
        batch_op.add_column(
            sa.Column("billing_session_started_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column("billing_session_ended_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "billing_accumulated_microseconds",
                sa.BigInteger(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
        batch_op.add_column(
            sa.Column("billing_active_instance_id", sa.String(length=200), nullable=True)
        )
        batch_op.add_column(
            sa.Column("billing_active_started_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column("billing_observed_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "billing_observation_stale",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch_op.add_column(
            sa.Column(
                "billing_estimated",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch_op.create_check_constraint(
            "nonnegative_billing_runtime",
            "billing_accumulated_microseconds >= 0",
        )
        batch_op.create_check_constraint(
            "billing_active_pair",
            "(billing_active_instance_id IS NULL AND billing_active_started_at IS NULL) "
            "OR (billing_active_instance_id IS NOT NULL "
            "AND billing_active_started_at IS NOT NULL)",
        )
        batch_op.create_check_constraint(
            "billing_session_required",
            "billing_session_started_at IS NOT NULL "
            "OR (billing_session_ended_at IS NULL "
            "AND billing_active_instance_id IS NULL "
            "AND billing_active_started_at IS NULL "
            "AND billing_accumulated_microseconds = 0)",
        )
        batch_op.create_check_constraint(
            "ended_billing_not_active",
            "billing_session_ended_at IS NULL OR billing_active_instance_id IS NULL",
        )
        batch_op.create_check_constraint(
            "billing_active_after_session_start",
            "billing_active_started_at IS NULL "
            "OR billing_session_started_at <= billing_active_started_at",
        )
        batch_op.create_check_constraint(
            "billing_end_after_session_start",
            "billing_session_ended_at IS NULL "
            "OR billing_session_started_at <= billing_session_ended_at",
        )

    with op.batch_alter_table("salad_deployments") as batch_op:
        batch_op.alter_column(
            "billing_accumulated_microseconds",
            existing_type=sa.BigInteger(),
            existing_nullable=False,
            server_default=None,
        )
        batch_op.alter_column(
            "billing_observation_stale",
            existing_type=sa.Boolean(),
            existing_nullable=False,
            server_default=None,
        )
        batch_op.alter_column(
            "billing_estimated",
            existing_type=sa.Boolean(),
            existing_nullable=False,
            server_default=None,
        )


def downgrade() -> None:
    with op.batch_alter_table("salad_deployments") as batch_op:
        batch_op.drop_constraint("billing_end_after_session_start", type_="check")
        batch_op.drop_constraint("billing_active_after_session_start", type_="check")
        batch_op.drop_constraint("ended_billing_not_active", type_="check")
        batch_op.drop_constraint("billing_session_required", type_="check")
        batch_op.drop_constraint("billing_active_pair", type_="check")
        batch_op.drop_constraint("nonnegative_billing_runtime", type_="check")
        batch_op.drop_column("billing_estimated")
        batch_op.drop_column("billing_observation_stale")
        batch_op.drop_column("billing_observed_at")
        batch_op.drop_column("billing_active_started_at")
        batch_op.drop_column("billing_active_instance_id")
        batch_op.drop_column("billing_accumulated_microseconds")
        batch_op.drop_column("billing_session_ended_at")
        batch_op.drop_column("billing_session_started_at")
