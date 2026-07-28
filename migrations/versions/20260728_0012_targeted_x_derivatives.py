"""Add owner-selected X images and clean Patreon preview inputs.

Revision ID: 20260728_0012
Revises: 20260728_0011
Create Date: 2026-07-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260728_0012"
down_revision: str | None = "20260728_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CLEAN_PREVIEW_CONSTRAINT = (
    "(role = 'x_teaser' AND derivative_target = 'x_teaser') "
    "OR (role IN ('patreon_content', 'patreon_preview') "
    "AND derivative_target = 'full')"
)
_LEGACY_PREVIEW_CONSTRAINT = (
    "(role IN ('x_teaser', 'patreon_preview') "
    "AND derivative_target = 'x_teaser') "
    "OR (role = 'patreon_content' AND derivative_target = 'full')"
)


def upgrade() -> None:
    op.create_table(
        "review_x_selections",
        sa.Column("review_task_id", sa.Uuid(), nullable=False),
        sa.Column("asset_id", sa.Uuid(), nullable=False),
        sa.Column("selected_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("selected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["asset_id"],
            ["assets.id"],
            name=op.f("fk_review_x_selections_asset_id_assets"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["review_task_id"],
            ["review_tasks.id"],
            name=op.f("fk_review_x_selections_review_task_id_review_tasks"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["selected_by_user_id"],
            ["admin_users.id"],
            name=op.f("fk_review_x_selections_selected_by_user_id_admin_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_review_x_selections")),
        sa.UniqueConstraint(
            "review_task_id",
            "asset_id",
            name="uq_review_x_selections_task_asset",
        ),
    )
    op.create_index(
        op.f("ix_review_x_selections_review_task_id"),
        "review_x_selections",
        ["review_task_id"],
    )
    op.create_index(
        op.f("ix_review_x_selections_asset_id"),
        "review_x_selections",
        ["asset_id"],
    )
    op.create_index(
        "ix_review_x_selections_task_selected_at",
        "review_x_selections",
        ["review_task_id", "selected_at"],
    )
    _replace_publication_preview_constraint(_CLEAN_PREVIEW_CONSTRAINT)


def downgrade() -> None:
    _replace_publication_preview_constraint(_LEGACY_PREVIEW_CONSTRAINT)
    op.drop_index(
        "ix_review_x_selections_task_selected_at",
        table_name="review_x_selections",
    )
    op.drop_index(
        op.f("ix_review_x_selections_asset_id"),
        table_name="review_x_selections",
    )
    op.drop_index(
        op.f("ix_review_x_selections_review_task_id"),
        table_name="review_x_selections",
    )
    op.drop_table("review_x_selections")


def _replace_publication_preview_constraint(expression: str) -> None:
    dialect_name = op.get_bind().dialect.name
    if dialect_name == "sqlite":
        for trigger_name in (
            "publication_inputs_immutable_update",
            "publication_inputs_immutable_delete",
        ):
            op.execute(sa.text(f"DROP TRIGGER IF EXISTS {trigger_name}"))
        with op.batch_alter_table(
            "publication_inputs",
            recreate="always",
        ) as batch_op:
            batch_op.drop_constraint(
                "role_target",
                type_="check",
            )
            batch_op.create_check_constraint(
                "role_target",
                expression,
            )
        for operation in ("update", "delete"):
            op.execute(
                sa.text(
                    f"CREATE TRIGGER publication_inputs_immutable_{operation} "
                    f"BEFORE {operation.upper()} ON publication_inputs BEGIN "
                    "SELECT RAISE(ABORT, 'publication_inputs are append-only'); END"
                )
            )
        return

    op.drop_constraint(
        "ck_publication_inputs_role_target",
        "publication_inputs",
        type_="check",
    )
    op.create_check_constraint(
        "ck_publication_inputs_role_target",
        "publication_inputs",
        expression,
    )
