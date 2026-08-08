"""Make finished-set delivery targets explicit and independent.

Revision ID: 20260808_0024
Revises: 20260808_0023
Create Date: 2026-08-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260808_0024"
down_revision: str | None = "20260808_0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("finished_set_archives") as batch_op:
        batch_op.add_column(sa.Column("mega_requested_by_user_id", sa.Uuid(), nullable=True))
        batch_op.add_column(
            sa.Column("mega_requested_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column("mega_requested_remote_root", sa.String(length=1024), nullable=True)
        )
        batch_op.create_foreign_key(
            op.f("fk_finished_set_archives_mega_requested_by_user_id_admin_users"),
            "admin_users",
            ["mega_requested_by_user_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_check_constraint(
            op.f("ck_finished_set_archives_mega_request_pair"),
            "(mega_requested_at IS NULL AND mega_requested_by_user_id IS NULL "
            "AND mega_requested_remote_root IS NULL) OR "
            "(mega_requested_at IS NOT NULL AND mega_requested_by_user_id IS NOT NULL "
            "AND mega_requested_remote_root IS NOT NULL)",
        )
    op.create_index(
        "ix_finished_set_archives_mega_request",
        "finished_set_archives",
        ["mega_requested_at", "state", "completed_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_finished_set_archives_mega_request",
        table_name="finished_set_archives",
    )
    with op.batch_alter_table("finished_set_archives") as batch_op:
        batch_op.drop_constraint(
            op.f("ck_finished_set_archives_mega_request_pair"),
            type_="check",
        )
        batch_op.drop_constraint(
            op.f("fk_finished_set_archives_mega_requested_by_user_id_admin_users"),
            type_="foreignkey",
        )
        batch_op.drop_column("mega_requested_at")
        batch_op.drop_column("mega_requested_remote_root")
        batch_op.drop_column("mega_requested_by_user_id")
