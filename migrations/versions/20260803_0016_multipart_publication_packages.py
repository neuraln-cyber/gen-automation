"""Allow deterministic multipart Patreon and MEGA packages.

Revision ID: 20260803_0016
Revises: 20260729_0015
Create Date: 2026-08-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260803_0016"
down_revision: str | None = "20260729_0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("publication_packages") as batch:
        batch.add_column(sa.Column("part_number", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("part_count", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("first_ordinal", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("last_ordinal", sa.Integer(), nullable=True))

    op.execute(
        sa.text(
            "UPDATE publication_packages "
            "SET part_number = 1, part_count = 1, first_ordinal = 1, "
            "last_ordinal = COALESCE(("
            "SELECT COUNT(*) FROM publication_inputs "
            "WHERE publication_inputs.intent_id = publication_packages.intent_id "
            "AND publication_inputs.role = 'patreon_content'"
            "), 1)"
        )
    )
    op.execute(sa.text("UPDATE publication_packages SET last_ordinal = 1 WHERE last_ordinal < 1"))

    with op.batch_alter_table("publication_packages") as batch:
        batch.alter_column("part_number", existing_type=sa.Integer(), nullable=False)
        batch.alter_column("part_count", existing_type=sa.Integer(), nullable=False)
        batch.alter_column("first_ordinal", existing_type=sa.Integer(), nullable=False)
        batch.alter_column("last_ordinal", existing_type=sa.Integer(), nullable=False)
        batch.drop_constraint("uq_publication_packages_intent", type_="unique")
        batch.create_unique_constraint(
            "uq_publication_packages_intent_part",
            ["intent_id", "part_number"],
        )
        batch.create_check_constraint(
            "ck_publication_packages_valid_part_identity",
            "part_number > 0 AND part_count > 0 AND part_number <= part_count",
        )
        batch.create_check_constraint(
            "ck_publication_packages_valid_ordinal_range",
            "first_ordinal > 0 AND last_ordinal >= first_ordinal",
        )


def downgrade() -> None:
    connection = op.get_bind()
    multipart_count = connection.scalar(
        sa.text(
            "SELECT COUNT(*) FROM publication_packages WHERE part_number <> 1 OR part_count <> 1"
        )
    )
    if multipart_count:
        raise RuntimeError("cannot downgrade while multipart publication packages exist")
    with op.batch_alter_table("publication_packages") as batch:
        batch.drop_constraint(
            "ck_publication_packages_valid_ordinal_range",
            type_="check",
        )
        batch.drop_constraint(
            "ck_publication_packages_valid_part_identity",
            type_="check",
        )
        batch.drop_constraint(
            "uq_publication_packages_intent_part",
            type_="unique",
        )
        batch.create_unique_constraint(
            "uq_publication_packages_intent",
            ["intent_id"],
        )
        batch.drop_column("last_ordinal")
        batch.drop_column("first_ordinal")
        batch.drop_column("part_count")
        batch.drop_column("part_number")
