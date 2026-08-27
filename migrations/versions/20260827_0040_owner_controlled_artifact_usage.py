"""Make artifact usage classifications informational.

Revision ID: 20260827_0040
Revises: 20260822_0039
Create Date: 2026-08-27
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260827_0040"
down_revision: str | None = "20260822_0039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("model_artifact_approvals") as batch_op:
        batch_op.drop_constraint(
            op.f("ck_model_artifact_approvals_approved_usage_scope"),
            type_="check",
        )


def downgrade() -> None:
    with op.batch_alter_table("model_artifact_approvals") as batch_op:
        batch_op.create_check_constraint(
            op.f("ck_model_artifact_approvals_approved_usage_scope"),
            "commercial_use_approved = true OR experiment_only = true",
        )
