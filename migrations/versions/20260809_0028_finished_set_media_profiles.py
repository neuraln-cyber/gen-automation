"""Version finished-set archives by outward media profile.

Revision ID: 20260809_0028
Revises: 20260808_0027
Create Date: 2026-08-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260809_0028"
down_revision: str | None = "20260808_0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LEGACY_PROFILE = "legacy-full-derivative-v1"


def upgrade() -> None:
    dialect_name = op.get_bind().dialect.name
    _drop_mega_item_identity_guards(dialect_name)
    with op.batch_alter_table("finished_set_archives") as batch_op:
        batch_op.add_column(
            sa.Column(
                "media_profile",
                sa.String(length=50),
                nullable=False,
                server_default=_LEGACY_PROFILE,
            )
        )
        batch_op.drop_constraint(
            "uq_finished_set_archives_review_task",
            type_="unique",
        )
        batch_op.create_unique_constraint(
            "uq_finished_set_archives_review_profile",
            ["review_task_id", "media_profile"],
        )
        batch_op.create_check_constraint(
            op.f("ck_finished_set_archives_nonempty_media_profile"),
            "length(trim(media_profile)) > 0",
        )
    with op.batch_alter_table("finished_set_archives") as batch_op:
        batch_op.alter_column(
            "media_profile",
            existing_type=sa.String(length=50),
            existing_nullable=False,
            server_default=None,
        )

    with op.batch_alter_table("mega_set_delivery_items") as batch_op:
        batch_op.add_column(sa.Column("source_asset_id", sa.Uuid(), nullable=True))
        batch_op.add_column(sa.Column("readiness_derivative_output_id", sa.Uuid(), nullable=True))
    op.execute(
        sa.text(
            "UPDATE mega_set_delivery_items SET "
            "readiness_derivative_output_id = source_derivative_output_id, "
            "source_asset_id = (SELECT derivative_outputs.source_asset_id "
            "FROM derivative_outputs WHERE derivative_outputs.id = "
            "mega_set_delivery_items.source_derivative_output_id)"
        )
    )
    missing = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT id FROM mega_set_delivery_items "
                "WHERE source_asset_id IS NULL OR readiness_derivative_output_id IS NULL LIMIT 1"
            )
        )
        .first()
    )
    if missing is not None:
        raise RuntimeError("cannot backfill MEGA delivery item provenance")
    op.drop_index(
        op.f("ix_mega_set_delivery_items_source_derivative_output_id"),
        table_name="mega_set_delivery_items",
    )
    with op.batch_alter_table("mega_set_delivery_items") as batch_op:
        batch_op.drop_constraint(
            "uq_mega_set_delivery_items_delivery_source",
            type_="unique",
        )
        batch_op.drop_constraint(
            op.f("fk_mega_set_delivery_items_source_derivative_output_id_derivative_outputs"),
            type_="foreignkey",
        )
        batch_op.alter_column(
            "source_asset_id",
            existing_type=sa.Uuid(),
            nullable=False,
        )
        batch_op.alter_column(
            "readiness_derivative_output_id",
            existing_type=sa.Uuid(),
            nullable=False,
        )
        batch_op.create_foreign_key(
            op.f("fk_mega_set_delivery_items_source_asset_id_assets"),
            "assets",
            ["source_asset_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_foreign_key(
            op.f("fk_mega_set_delivery_items_readiness_derivative_output_id_derivative_outputs"),
            "derivative_outputs",
            ["readiness_derivative_output_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_unique_constraint(
            "uq_mega_set_delivery_items_delivery_source",
            ["delivery_id", "source_asset_id"],
        )
        batch_op.create_unique_constraint(
            "uq_mega_set_delivery_items_delivery_readiness",
            ["delivery_id", "readiness_derivative_output_id"],
        )
        batch_op.drop_column("source_derivative_output_id")
    op.create_index(
        op.f("ix_mega_set_delivery_items_source_asset_id"),
        "mega_set_delivery_items",
        ["source_asset_id"],
    )
    op.create_index(
        op.f("ix_mega_set_delivery_items_readiness_derivative_output_id"),
        "mega_set_delivery_items",
        ["readiness_derivative_output_id"],
    )
    _create_mega_item_identity_guards(dialect_name)


def downgrade() -> None:
    public_archive = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT review_task_id FROM finished_set_archives "
                "WHERE media_profile != :legacy_profile LIMIT 1"
            ),
            {"legacy_profile": _LEGACY_PROFILE},
        )
        .first()
    )
    if public_archive is not None:
        raise RuntimeError(
            "cannot downgrade finished-set media profiles without losing public PNG archives"
        )
    duplicate = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT review_task_id FROM finished_set_archives "
                "GROUP BY review_task_id HAVING count(*) > 1 LIMIT 1"
            )
        )
        .first()
    )
    if duplicate is not None:
        raise RuntimeError("cannot downgrade finished-set media profiles without losing archives")
    dialect_name = op.get_bind().dialect.name
    _drop_mega_item_identity_guards(dialect_name)
    op.drop_index(
        op.f("ix_mega_set_delivery_items_readiness_derivative_output_id"),
        table_name="mega_set_delivery_items",
    )
    op.drop_index(
        op.f("ix_mega_set_delivery_items_source_asset_id"),
        table_name="mega_set_delivery_items",
    )
    with op.batch_alter_table("mega_set_delivery_items") as batch_op:
        batch_op.add_column(sa.Column("source_derivative_output_id", sa.Uuid(), nullable=True))
    op.execute(
        sa.text(
            "UPDATE mega_set_delivery_items SET source_derivative_output_id = "
            "readiness_derivative_output_id"
        )
    )
    with op.batch_alter_table("mega_set_delivery_items") as batch_op:
        batch_op.drop_constraint(
            "uq_mega_set_delivery_items_delivery_readiness",
            type_="unique",
        )
        batch_op.drop_constraint(
            "uq_mega_set_delivery_items_delivery_source",
            type_="unique",
        )
        batch_op.drop_constraint(
            op.f("fk_mega_set_delivery_items_readiness_derivative_output_id_derivative_outputs"),
            type_="foreignkey",
        )
        batch_op.drop_constraint(
            op.f("fk_mega_set_delivery_items_source_asset_id_assets"),
            type_="foreignkey",
        )
        batch_op.alter_column(
            "source_derivative_output_id",
            existing_type=sa.Uuid(),
            nullable=False,
        )
        batch_op.create_foreign_key(
            op.f("fk_mega_set_delivery_items_source_derivative_output_id_derivative_outputs"),
            "derivative_outputs",
            ["source_derivative_output_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_unique_constraint(
            "uq_mega_set_delivery_items_delivery_source",
            ["delivery_id", "source_derivative_output_id"],
        )
        batch_op.drop_column("readiness_derivative_output_id")
        batch_op.drop_column("source_asset_id")
    op.create_index(
        op.f("ix_mega_set_delivery_items_source_derivative_output_id"),
        "mega_set_delivery_items",
        ["source_derivative_output_id"],
    )
    with op.batch_alter_table("finished_set_archives") as batch_op:
        batch_op.drop_constraint(
            op.f("ck_finished_set_archives_nonempty_media_profile"),
            type_="check",
        )
        batch_op.drop_constraint(
            "uq_finished_set_archives_review_profile",
            type_="unique",
        )
        batch_op.drop_column("media_profile")
        batch_op.create_unique_constraint(
            "uq_finished_set_archives_review_task",
            ["review_task_id"],
        )
    _create_legacy_mega_item_identity_guards(dialect_name)


def _drop_mega_item_identity_guards(dialect_name: str) -> None:
    if dialect_name == "sqlite":
        op.execute(sa.text("DROP TRIGGER IF EXISTS mega_set_delivery_items_guard_update"))
        op.execute(sa.text("DROP TRIGGER IF EXISTS mega_set_delivery_items_reject_delete"))
    elif dialect_name == "postgresql":
        op.execute(
            sa.text(
                "DROP TRIGGER IF EXISTS mega_set_delivery_items_guard_mutation "
                "ON mega_set_delivery_items"
            )
        )
        op.execute(
            sa.text(
                "DROP FUNCTION IF EXISTS gen_automation_guard_mega_set_delivery_item_mutation()"
            )
        )


def _create_mega_item_identity_guards(dialect_name: str) -> None:
    if dialect_name == "sqlite":
        op.execute(
            sa.text(
                "CREATE TRIGGER mega_set_delivery_items_guard_update "
                "BEFORE UPDATE ON mega_set_delivery_items BEGIN SELECT CASE WHEN "
                "OLD.id IS NOT NEW.id OR OLD.delivery_id IS NOT NEW.delivery_id "
                "OR OLD.ordinal IS NOT NEW.ordinal "
                "OR OLD.source_asset_id IS NOT NEW.source_asset_id "
                "OR OLD.readiness_derivative_output_id "
                "IS NOT NEW.readiness_derivative_output_id "
                "OR OLD.source_sha256 IS NOT NEW.source_sha256 "
                "OR OLD.source_byte_size IS NOT NEW.source_byte_size "
                "OR OLD.source_content_type IS NOT NEW.source_content_type "
                "OR OLD.remote_path IS NOT NEW.remote_path "
                "OR OLD.created_at IS NOT NEW.created_at "
                "THEN RAISE(ABORT, 'MEGA set delivery item identity is immutable') "
                "END; END"
            )
        )
        op.execute(
            sa.text(
                "CREATE TRIGGER mega_set_delivery_items_reject_delete "
                "BEFORE DELETE ON mega_set_delivery_items BEGIN "
                "SELECT RAISE(ABORT, 'MEGA set delivery items cannot be deleted'); END"
            )
        )
    elif dialect_name == "postgresql":
        op.execute(
            sa.text(
                "CREATE OR REPLACE FUNCTION "
                "gen_automation_guard_mega_set_delivery_item_mutation() "
                "RETURNS trigger AS $$ BEGIN IF TG_OP = 'DELETE' THEN "
                "RAISE EXCEPTION 'MEGA set delivery items cannot be deleted'; END IF; "
                "IF OLD.id IS DISTINCT FROM NEW.id "
                "OR OLD.delivery_id IS DISTINCT FROM NEW.delivery_id "
                "OR OLD.ordinal IS DISTINCT FROM NEW.ordinal "
                "OR OLD.source_asset_id IS DISTINCT FROM NEW.source_asset_id "
                "OR OLD.readiness_derivative_output_id "
                "IS DISTINCT FROM NEW.readiness_derivative_output_id "
                "OR OLD.source_sha256 IS DISTINCT FROM NEW.source_sha256 "
                "OR OLD.source_byte_size IS DISTINCT FROM NEW.source_byte_size "
                "OR OLD.source_content_type IS DISTINCT FROM NEW.source_content_type "
                "OR OLD.remote_path IS DISTINCT FROM NEW.remote_path "
                "OR OLD.created_at IS DISTINCT FROM NEW.created_at THEN "
                "RAISE EXCEPTION 'MEGA set delivery item identity is immutable'; END IF; "
                "RETURN NEW; END; $$ LANGUAGE plpgsql"
            )
        )
        op.execute(
            sa.text(
                "CREATE TRIGGER mega_set_delivery_items_guard_mutation "
                "BEFORE UPDATE OR DELETE ON mega_set_delivery_items FOR EACH ROW "
                "EXECUTE FUNCTION gen_automation_guard_mega_set_delivery_item_mutation()"
            )
        )


def _create_legacy_mega_item_identity_guards(dialect_name: str) -> None:
    if dialect_name == "sqlite":
        op.execute(
            sa.text(
                "CREATE TRIGGER mega_set_delivery_items_guard_update "
                "BEFORE UPDATE ON mega_set_delivery_items BEGIN SELECT CASE WHEN "
                "OLD.id IS NOT NEW.id OR OLD.delivery_id IS NOT NEW.delivery_id "
                "OR OLD.ordinal IS NOT NEW.ordinal "
                "OR OLD.source_derivative_output_id IS NOT NEW.source_derivative_output_id "
                "OR OLD.source_sha256 IS NOT NEW.source_sha256 "
                "OR OLD.source_byte_size IS NOT NEW.source_byte_size "
                "OR OLD.source_content_type IS NOT NEW.source_content_type "
                "OR OLD.remote_path IS NOT NEW.remote_path "
                "OR OLD.created_at IS NOT NEW.created_at "
                "THEN RAISE(ABORT, 'MEGA set delivery item identity is immutable') "
                "END; END"
            )
        )
        op.execute(
            sa.text(
                "CREATE TRIGGER mega_set_delivery_items_reject_delete "
                "BEFORE DELETE ON mega_set_delivery_items BEGIN "
                "SELECT RAISE(ABORT, 'MEGA set delivery items cannot be deleted'); END"
            )
        )
    elif dialect_name == "postgresql":
        op.execute(
            sa.text(
                "CREATE OR REPLACE FUNCTION "
                "gen_automation_guard_mega_set_delivery_item_mutation() "
                "RETURNS trigger AS $$ BEGIN IF TG_OP = 'DELETE' THEN "
                "RAISE EXCEPTION 'MEGA set delivery items cannot be deleted'; END IF; "
                "IF OLD.id IS DISTINCT FROM NEW.id "
                "OR OLD.delivery_id IS DISTINCT FROM NEW.delivery_id "
                "OR OLD.ordinal IS DISTINCT FROM NEW.ordinal "
                "OR OLD.source_derivative_output_id "
                "IS DISTINCT FROM NEW.source_derivative_output_id "
                "OR OLD.source_sha256 IS DISTINCT FROM NEW.source_sha256 "
                "OR OLD.source_byte_size IS DISTINCT FROM NEW.source_byte_size "
                "OR OLD.source_content_type IS DISTINCT FROM NEW.source_content_type "
                "OR OLD.remote_path IS DISTINCT FROM NEW.remote_path "
                "OR OLD.created_at IS DISTINCT FROM NEW.created_at THEN "
                "RAISE EXCEPTION 'MEGA set delivery item identity is immutable'; END IF; "
                "RETURN NEW; END; $$ LANGUAGE plpgsql"
            )
        )
        op.execute(
            sa.text(
                "CREATE TRIGGER mega_set_delivery_items_guard_mutation "
                "BEFORE UPDATE OR DELETE ON mega_set_delivery_items FOR EACH ROW "
                "EXECUTE FUNCTION gen_automation_guard_mega_set_delivery_item_mutation()"
            )
        )
