"""Add explicit txt2img model-family compatibility metadata.

Revision ID: 20260818_0038
Revises: 20260812_0037
Create Date: 2026-08-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260818_0038"
down_revision: str | None = "20260812_0037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DEFAULT_FAMILY = "illustrious"
_FAMILY_CHECK = "model_family IN ('illustrious', 'anima')"
_JSON_TYPE = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    _add_model_family(
        "model_artifact_approvals",
        constraint_name="ck_model_artifact_approvals_generation_model_family",
    )
    _add_experiment_only_scope()
    _add_model_family(
        "workflow_approvals",
        constraint_name="ck_workflow_approvals_workflow_generation_model_family",
    )
    _add_experiment_warm_selection()


def downgrade() -> None:
    _drop_experiment_warm_selection()
    _drop_experiment_only_scope()
    _drop_model_family(
        "workflow_approvals",
        constraint_name="ck_workflow_approvals_workflow_generation_model_family",
    )
    _drop_model_family(
        "model_artifact_approvals",
        constraint_name="ck_model_artifact_approvals_generation_model_family",
    )


def _add_model_family(table_name: str, *, constraint_name: str) -> None:
    with op.batch_alter_table(table_name) as batch_op:
        batch_op.add_column(
            sa.Column(
                "model_family",
                sa.String(length=20),
                nullable=False,
                server_default=sa.text(f"'{_DEFAULT_FAMILY}'"),
            )
        )
        batch_op.create_check_constraint(op.f(constraint_name), _FAMILY_CHECK)

    # The one-time default above backfills every existing approval. New writes
    # must carry the family explicitly through the compliance registry.
    with op.batch_alter_table(table_name) as batch_op:
        batch_op.alter_column(
            "model_family",
            existing_type=sa.String(length=20),
            existing_nullable=False,
            server_default=None,
        )


def _drop_model_family(table_name: str, *, constraint_name: str) -> None:
    with op.batch_alter_table(table_name) as batch_op:
        batch_op.drop_constraint(op.f(constraint_name), type_="check")
        batch_op.drop_column("model_family")


def _add_experiment_only_scope() -> None:
    with op.batch_alter_table("model_artifact_approvals") as batch_op:
        batch_op.add_column(
            sa.Column(
                "experiment_only",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch_op.drop_constraint(
            op.f("ck_model_artifact_approvals_commercial_use_required"),
            type_="check",
        )
        batch_op.create_check_constraint(
            op.f("ck_model_artifact_approvals_approved_usage_scope"),
            "commercial_use_approved = true OR experiment_only = true",
        )

    with op.batch_alter_table("model_artifact_approvals") as batch_op:
        batch_op.alter_column(
            "experiment_only",
            existing_type=sa.Boolean(),
            existing_nullable=False,
            server_default=None,
        )


def _drop_experiment_only_scope() -> None:
    noncommercial_rows = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT count(*) FROM model_artifact_approvals "
                "WHERE commercial_use_approved = false"
            )
        )
        .scalar_one()
    )
    if noncommercial_rows:
        raise RuntimeError("cannot downgrade while experiment-only non-commercial approvals exist")
    with op.batch_alter_table("model_artifact_approvals") as batch_op:
        batch_op.drop_constraint(
            op.f("ck_model_artifact_approvals_approved_usage_scope"),
            type_="check",
        )
        batch_op.create_check_constraint(
            op.f("ck_model_artifact_approvals_commercial_use_required"),
            "commercial_use_approved = true",
        )
        batch_op.drop_column("experiment_only")


def _add_experiment_warm_selection() -> None:
    with op.batch_alter_table("experiment_warm_leases") as batch_op:
        batch_op.add_column(
            sa.Column("requested_checkpoint_sha256", sa.String(length=64), nullable=True)
        )
        batch_op.add_column(sa.Column("requested_lora_sha256s", _JSON_TYPE, nullable=True))


def _drop_experiment_warm_selection() -> None:
    with op.batch_alter_table("experiment_warm_leases") as batch_op:
        batch_op.drop_column("requested_lora_sha256s")
        batch_op.drop_column("requested_checkpoint_sha256")
