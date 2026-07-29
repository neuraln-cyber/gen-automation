"""Add restart-safe delivery of completed clean packages to MEGA.

Revision ID: 20260728_0013
Revises: 20260728_0012
Create Date: 2026-07-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260728_0013"
down_revision: str | None = "20260728_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

mega_delivery_state = sa.Enum(
    "pending",
    "claimed",
    "retry_wait",
    "succeeded",
    "failed",
    name="mega_delivery_state",
    native_enum=False,
    create_constraint=True,
    length=10,
)


def upgrade() -> None:
    op.create_table(
        "mega_deliveries",
        sa.Column("publication_package_id", sa.Uuid(), nullable=False),
        sa.Column("state", mega_delivery_state, nullable=False),
        sa.Column("remote_root", sa.String(length=1024), nullable=False),
        sa.Column("remote_path", sa.String(length=1024), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_owner", sa.String(length=200), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("remote_node_handle", sa.String(length=80), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=100), nullable=True),
        sa.Column("last_error_detail", sa.Text(), nullable=True),
        sa.Column(
            "id",
            sa.Uuid(),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(trim(remote_root)) > 0 AND length(trim(remote_path)) > 0",
            name=op.f("ck_mega_deliveries_complete_remote_identity"),
        ),
        sa.CheckConstraint(
            "length(sha256) = 64",
            name=op.f("ck_mega_deliveries_valid_sha256"),
        ),
        sa.CheckConstraint(
            "byte_size > 0",
            name=op.f("ck_mega_deliveries_positive_byte_size"),
        ),
        sa.CheckConstraint(
            "attempts >= 0",
            name=op.f("ck_mega_deliveries_nonnegative_attempts"),
        ),
        sa.CheckConstraint(
            "(lease_owner IS NULL AND lease_expires_at IS NULL) OR "
            "(lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name=op.f("ck_mega_deliveries_lease_pair"),
        ),
        sa.CheckConstraint(
            "(state = 'claimed' AND lease_owner IS NOT NULL) OR "
            "(state <> 'claimed' AND lease_owner IS NULL)",
            name=op.f("ck_mega_deliveries_state_lease_contract"),
        ),
        sa.CheckConstraint(
            "(state = 'succeeded' AND remote_node_handle IS NOT NULL "
            "AND verified_at IS NOT NULL AND completed_at IS NOT NULL) OR "
            "(state <> 'succeeded' AND remote_node_handle IS NULL "
            "AND verified_at IS NULL)",
            name=op.f("ck_mega_deliveries_success_contract"),
        ),
        sa.ForeignKeyConstraint(
            ["publication_package_id"],
            ["publication_packages.id"],
            name=op.f("fk_mega_deliveries_publication_package_id_publication_packages"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mega_deliveries")),
        sa.UniqueConstraint(
            "publication_package_id",
            name="uq_mega_deliveries_publication_package",
        ),
        sa.UniqueConstraint(
            "remote_path",
            name="uq_mega_deliveries_remote_path",
        ),
    )
    op.create_index(
        op.f("ix_mega_deliveries_publication_package_id"),
        "mega_deliveries",
        ["publication_package_id"],
        unique=False,
    )
    op.create_index(
        "ix_mega_deliveries_claim",
        "mega_deliveries",
        ["state", "available_at", "lease_expires_at", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_mega_deliveries_claim", table_name="mega_deliveries")
    op.drop_index(
        op.f("ix_mega_deliveries_publication_package_id"),
        table_name="mega_deliveries",
    )
    op.drop_table("mega_deliveries")
