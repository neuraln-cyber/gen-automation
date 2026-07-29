"""Add immutable, versioned prompt wildcard libraries.

Revision ID: 20260728_0011
Revises: 20260728_0010
Create Date: 2026-07-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260728_0011"
down_revision: str | None = "20260728_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "wildcard_libraries",
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("current_version_no", sa.Integer(), nullable=False),
        sa.Column("lock_version", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.String(length=200), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "current_version_no > 0",
            name=op.f("ck_wildcard_libraries_positive_current_version"),
        ),
        sa.CheckConstraint(
            "lock_version > 0",
            name=op.f("ck_wildcard_libraries_positive_lock_version"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_wildcard_libraries")),
        sa.UniqueConstraint("name", name=op.f("uq_wildcard_libraries_name")),
    )
    op.create_table(
        "wildcard_library_versions",
        sa.Column("library_id", sa.Uuid(), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("entries", json_type, nullable=False),
        sa.Column("entry_count", sa.Integer(), nullable=False),
        sa.Column("entries_sha256", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "version_no > 0",
            name=op.f("ck_wildcard_library_versions_positive_version"),
        ),
        sa.CheckConstraint(
            "entry_count > 0",
            name=op.f("ck_wildcard_library_versions_positive_entry_count"),
        ),
        sa.CheckConstraint(
            "entry_count <= 2000",
            name=op.f("ck_wildcard_library_versions_entry_count_within_limit"),
        ),
        sa.CheckConstraint(
            "length(entries_sha256) = 64",
            name=op.f("ck_wildcard_library_versions_sha256_length"),
        ),
        sa.ForeignKeyConstraint(
            ["library_id"],
            ["wildcard_libraries.id"],
            name=op.f("fk_wildcard_library_versions_library_id_wildcard_libraries"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_wildcard_library_versions")),
        sa.UniqueConstraint(
            "library_id",
            "version_no",
            name=op.f("uq_wildcard_library_versions_library_id"),
        ),
    )
    op.create_index(
        op.f("ix_wildcard_library_versions_library_id"),
        "wildcard_library_versions",
        ["library_id"],
        unique=False,
    )
    op.create_index(
        "ix_wildcard_library_versions_library_created",
        "wildcard_library_versions",
        ["library_id", "created_at"],
        unique=False,
    )
    _create_guards(op.get_bind().dialect.name)


def downgrade() -> None:
    _drop_guards(op.get_bind().dialect.name)
    op.drop_index(
        "ix_wildcard_library_versions_library_created",
        table_name="wildcard_library_versions",
    )
    op.drop_index(
        op.f("ix_wildcard_library_versions_library_id"),
        table_name="wildcard_library_versions",
    )
    op.drop_table("wildcard_library_versions")
    op.drop_table("wildcard_libraries")


def _create_guards(dialect_name: str) -> None:
    if dialect_name == "postgresql":
        op.execute(
            "CREATE OR REPLACE FUNCTION "
            "gen_automation_guard_wildcard_library_mutation() "
            "RETURNS trigger AS $$ BEGIN "
            "IF TG_OP = 'DELETE' THEN "
            "RAISE EXCEPTION 'wildcard libraries cannot be deleted'; END IF; "
            "IF NEW.id IS DISTINCT FROM OLD.id "
            "OR NEW.name IS DISTINCT FROM OLD.name "
            "OR NEW.created_by IS DISTINCT FROM OLD.created_by "
            "OR NEW.created_at IS DISTINCT FROM OLD.created_at "
            "OR NEW.current_version_no <> OLD.current_version_no + 1 "
            "OR NEW.lock_version <> OLD.lock_version + 1 THEN "
            "RAISE EXCEPTION 'wildcard library head transition is invalid'; END IF; "
            "RETURN NEW; END; $$ LANGUAGE plpgsql"
        )
        op.execute(
            "CREATE TRIGGER wildcard_libraries_guard_mutation "
            "BEFORE UPDATE OR DELETE ON wildcard_libraries "
            "FOR EACH ROW EXECUTE FUNCTION "
            "gen_automation_guard_wildcard_library_mutation()"
        )
        op.execute(
            "CREATE OR REPLACE FUNCTION "
            "gen_automation_guard_wildcard_library_version_mutation() "
            "RETURNS trigger AS $$ BEGIN "
            "RAISE EXCEPTION 'wildcard library versions are append-only'; "
            "END; $$ LANGUAGE plpgsql"
        )
        op.execute(
            "CREATE TRIGGER wildcard_library_versions_guard_mutation "
            "BEFORE UPDATE OR DELETE ON wildcard_library_versions "
            "FOR EACH ROW EXECUTE FUNCTION "
            "gen_automation_guard_wildcard_library_version_mutation()"
        )
        return
    if dialect_name == "sqlite":
        op.execute(
            "CREATE TRIGGER wildcard_libraries_guard_update "
            "BEFORE UPDATE ON wildcard_libraries WHEN "
            "NEW.id <> OLD.id OR NEW.name <> OLD.name "
            "OR NEW.created_by <> OLD.created_by "
            "OR NEW.created_at <> OLD.created_at "
            "OR NEW.current_version_no <> OLD.current_version_no + 1 "
            "OR NEW.lock_version <> OLD.lock_version + 1 BEGIN "
            "SELECT RAISE(ABORT, 'wildcard library head transition is invalid'); END"
        )
        op.execute(
            "CREATE TRIGGER wildcard_libraries_guard_delete "
            "BEFORE DELETE ON wildcard_libraries BEGIN "
            "SELECT RAISE(ABORT, 'wildcard libraries cannot be deleted'); END"
        )
        op.execute(
            "CREATE TRIGGER wildcard_library_versions_immutable_update "
            "BEFORE UPDATE ON wildcard_library_versions BEGIN "
            "SELECT RAISE(ABORT, 'wildcard library versions are append-only'); END"
        )
        op.execute(
            "CREATE TRIGGER wildcard_library_versions_immutable_delete "
            "BEFORE DELETE ON wildcard_library_versions BEGIN "
            "SELECT RAISE(ABORT, 'wildcard library versions are append-only'); END"
        )


def _drop_guards(dialect_name: str) -> None:
    if dialect_name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS wildcard_libraries_guard_mutation ON wildcard_libraries")
        op.execute("DROP FUNCTION IF EXISTS gen_automation_guard_wildcard_library_mutation()")
        op.execute(
            "DROP TRIGGER IF EXISTS wildcard_library_versions_guard_mutation "
            "ON wildcard_library_versions"
        )
        op.execute(
            "DROP FUNCTION IF EXISTS gen_automation_guard_wildcard_library_version_mutation()"
        )
        return
    if dialect_name == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS wildcard_libraries_guard_update")
        op.execute("DROP TRIGGER IF EXISTS wildcard_libraries_guard_delete")
        op.execute("DROP TRIGGER IF EXISTS wildcard_library_versions_immutable_update")
        op.execute("DROP TRIGGER IF EXISTS wildcard_library_versions_immutable_delete")
