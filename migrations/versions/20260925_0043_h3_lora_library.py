"""Allow H3 file-library artifacts without extending image workflow families.

Revision ID: 20260925_0043
Revises: 20260909_0042
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260925_0043"
down_revision: str | None = "20260909_0042"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT = "ck_model_artifact_approvals_generation_model_family"


def upgrade() -> None:
    with op.batch_alter_table("model_artifact_approvals") as batch:
        batch.drop_constraint(op.f(_CONSTRAINT), type_="check")
        batch.create_check_constraint(
            op.f(_CONSTRAINT), "model_family IN ('illustrious', 'anima', 'minimax_h3')"
        )


def downgrade() -> None:
    count = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT count(*) FROM model_artifact_approvals WHERE model_family = 'minimax_h3'"
            )
        )
        .scalar_one()
    )
    if count:
        raise RuntimeError("cannot downgrade while H3 library records exist")
    with op.batch_alter_table("model_artifact_approvals") as batch:
        batch.drop_constraint(op.f(_CONSTRAINT), type_="check")
        batch.create_check_constraint(op.f(_CONSTRAINT), "model_family IN ('illustrious', 'anima')")
