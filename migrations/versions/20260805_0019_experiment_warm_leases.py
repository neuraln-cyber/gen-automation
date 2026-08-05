"""Add bounded experiment GPU warm leases.

Revision ID: 20260805_0019
Revises: 20260805_0018
Create Date: 2026-08-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260805_0019"
down_revision: str | None = "20260805_0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


warm_lease_state = sa.Enum(
    "starting",
    "active",
    "ending",
    "ended",
    "expired",
    "failed",
    name="experiment_warm_lease_state",
    native_enum=False,
)


def upgrade() -> None:
    op.create_table(
        "experiment_warm_leases",
        sa.Column("salad_deployment_id", sa.Uuid(), nullable=False),
        sa.Column("state", warm_lease_state, nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("hard_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("idle_ttl_seconds", sa.Integer(), nullable=False),
        sa.Column("max_cost_microusd", sa.BigInteger(), nullable=False),
        sa.Column("provider_version", sa.Integer(), nullable=True),
        sa.Column("created_by", sa.String(length=200), nullable=False),
        sa.Column("lock_version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "started_at < expires_at AND expires_at <= hard_expires_at",
            name=op.f("ck_experiment_warm_leases_valid_expiry_window"),
        ),
        sa.CheckConstraint(
            "max_cost_microusd > 0",
            name=op.f("ck_experiment_warm_leases_positive_max_cost"),
        ),
        sa.CheckConstraint(
            "idle_ttl_seconds >= 60 AND idle_ttl_seconds <= 5400",
            name=op.f("ck_experiment_warm_leases_valid_idle_ttl"),
        ),
        sa.CheckConstraint(
            "provider_version IS NULL OR provider_version > 0",
            name=op.f("ck_experiment_warm_leases_positive_provider_version"),
        ),
        sa.CheckConstraint(
            "lock_version > 0",
            name=op.f("ck_experiment_warm_leases_positive_lock_version"),
        ),
        sa.CheckConstraint(
            "(state IN ('starting', 'active', 'ending') AND ended_at IS NULL) "
            "OR (state IN ('ended', 'expired', 'failed') AND ended_at IS NOT NULL)",
            name=op.f("ck_experiment_warm_leases_terminal_end_timestamp"),
        ),
        sa.ForeignKeyConstraint(
            ["salad_deployment_id"],
            ["salad_deployments.id"],
            name=op.f("fk_experiment_warm_leases_salad_deployment_id_salad_deployments"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_experiment_warm_leases")),
    )
    op.create_index(
        op.f("ix_experiment_warm_leases_salad_deployment_id"),
        "experiment_warm_leases",
        ["salad_deployment_id"],
        unique=False,
    )
    op.create_index(
        "ix_experiment_warm_leases_expiry",
        "experiment_warm_leases",
        ["state", "expires_at", "hard_expires_at"],
        unique=False,
    )
    op.create_index(
        "uq_experiment_warm_leases_live_deployment",
        "experiment_warm_leases",
        ["salad_deployment_id"],
        unique=True,
        postgresql_where=sa.text("state IN ('starting', 'active', 'ending')"),
        sqlite_where=sa.text("state IN ('starting', 'active', 'ending')"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_experiment_warm_leases_live_deployment",
        table_name="experiment_warm_leases",
    )
    op.drop_index(
        "ix_experiment_warm_leases_expiry",
        table_name="experiment_warm_leases",
    )
    op.drop_index(
        op.f("ix_experiment_warm_leases_salad_deployment_id"),
        table_name="experiment_warm_leases",
    )
    op.drop_table("experiment_warm_leases")
