"""Add explicit approved workflow capabilities.

Revision ID: 20260809_0029
Revises: 20260809_0028
Create Date: 2026-08-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260809_0029"
down_revision: str | None = "20260809_0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    with op.batch_alter_table("workflow_approvals") as batch_op:
        batch_op.add_column(
            sa.Column(
                "capabilities",
                json_type,
                nullable=False,
                server_default=sa.text("'[]'"),
            )
        )
    with op.batch_alter_table("workflow_approvals") as batch_op:
        batch_op.alter_column(
            "capabilities",
            existing_type=json_type,
            existing_nullable=False,
            server_default=None,
        )


def downgrade() -> None:
    with op.batch_alter_table("workflow_approvals") as batch_op:
        batch_op.drop_column("capabilities")
