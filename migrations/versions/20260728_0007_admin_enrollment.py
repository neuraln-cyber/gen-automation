"""Add secure administrator invitation and enrollment capabilities.

Revision ID: 20260728_0007
Revises: 20260728_0006
Create Date: 2026-07-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260728_0007"
down_revision: str | None = "20260728_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

admin_enrollment_role = sa.Enum(
    "owner",
    "admin",
    "reviewer",
    "publisher",
    name="admin_enrollment_role",
    native_enum=False,
    create_constraint=True,
)
admin_enrollment_state = sa.Enum(
    "pending",
    "consumed",
    "revoked",
    name="admin_enrollment_state",
    native_enum=False,
    create_constraint=True,
)


def upgrade() -> None:
    op.create_table(
        "admin_enrollments",
        sa.Column("username_normalized", sa.String(length=200), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("role", admin_enrollment_role, nullable=False),
        sa.Column("token_sha256", sa.String(length=64), nullable=False),
        sa.Column("totp_secret_ciphertext", sa.Text(), nullable=True),
        sa.Column("state", admin_enrollment_state, nullable=False),
        sa.Column("invited_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("invited_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lock_version", sa.Integer(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "length(token_sha256) = 64",
            name=op.f("ck_admin_enrollments_valid_token_sha256"),
        ),
        sa.CheckConstraint(
            "expires_at > invited_at",
            name=op.f("ck_admin_enrollments_expiry_after_invitation"),
        ),
        sa.CheckConstraint(
            "lock_version > 0",
            name=op.f("ck_admin_enrollments_positive_lock_version"),
        ),
        sa.CheckConstraint(
            "(state = 'pending' "
            "AND totp_secret_ciphertext IS NOT NULL "
            "AND consumed_by_user_id IS NULL AND consumed_at IS NULL "
            "AND revoked_by_user_id IS NULL AND revoked_at IS NULL) "
            "OR (state = 'consumed' "
            "AND totp_secret_ciphertext IS NULL "
            "AND consumed_by_user_id IS NOT NULL AND consumed_at IS NOT NULL "
            "AND revoked_by_user_id IS NULL AND revoked_at IS NULL) "
            "OR (state = 'revoked' "
            "AND totp_secret_ciphertext IS NULL "
            "AND consumed_by_user_id IS NULL AND consumed_at IS NULL "
            "AND revoked_by_user_id IS NOT NULL AND revoked_at IS NOT NULL)",
            name=op.f("ck_admin_enrollments_lifecycle_metadata"),
        ),
        sa.CheckConstraint(
            "consumed_at IS NULL OR consumed_at >= invited_at",
            name=op.f("ck_admin_enrollments_valid_consumption_time"),
        ),
        sa.CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= invited_at",
            name=op.f("ck_admin_enrollments_valid_revocation_time"),
        ),
        sa.ForeignKeyConstraint(
            ["consumed_by_user_id"],
            ["admin_users.id"],
            name=op.f("fk_admin_enrollments_consumed_by_user_id_admin_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["invited_by_user_id"],
            ["admin_users.id"],
            name=op.f("fk_admin_enrollments_invited_by_user_id_admin_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["revoked_by_user_id"],
            ["admin_users.id"],
            name=op.f("fk_admin_enrollments_revoked_by_user_id_admin_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_admin_enrollments")),
        sa.UniqueConstraint(
            "consumed_by_user_id",
            name="uq_admin_enrollments_consumed_by_user_id",
        ),
        sa.UniqueConstraint(
            "token_sha256",
            name="uq_admin_enrollments_token_sha256",
        ),
    )
    op.create_index(
        "ix_admin_enrollments_state_expires_at",
        "admin_enrollments",
        ["state", "expires_at"],
    )
    op.create_index(
        "uq_admin_enrollments_pending_username",
        "admin_enrollments",
        ["username_normalized"],
        unique=True,
        postgresql_where=sa.text("state = 'pending'"),
        sqlite_where=sa.text("state = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_admin_enrollments_pending_username",
        table_name="admin_enrollments",
    )
    op.drop_index(
        "ix_admin_enrollments_state_expires_at",
        table_name="admin_enrollments",
    )
    op.drop_table("admin_enrollments")
