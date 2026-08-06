"""Add provider-independent finished-set archives.

Revision ID: 20260806_0022
Revises: 20260806_0021
Create Date: 2026-08-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260806_0022"
down_revision: str | None = "20260806_0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "release_selections",
        sa.Column("source_generation_job_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "release_selections",
        sa.Column("source_output_index", sa.Integer(), nullable=True),
    )
    op.add_column(
        "release_selections",
        sa.Column("source_generation_ordinal", sa.Integer(), nullable=True),
    )
    op.add_column(
        "release_selections",
        sa.Column("source_generation_queue_position", sa.Integer(), nullable=True),
    )
    op.create_index(
        "uq_release_selections_task_generation_queue_position",
        "release_selections",
        ["review_task_id", "source_generation_queue_position"],
        unique=True,
    )

    op.create_table(
        "finished_set_archives",
        sa.Column("review_task_id", sa.Uuid(), nullable=False),
        sa.Column("release_version_id", sa.Uuid(), nullable=False),
        sa.Column("requested_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("state", sa.String(length=12), nullable=False),
        sa.Column("selection_count", sa.Integer(), nullable=False),
        sa.Column("manifest_sha256", sa.String(length=64), nullable=True),
        sa.Column("part_count", sa.Integer(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_owner", sa.String(length=200), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=100), nullable=True),
        sa.Column("last_error_detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "state IN ('pending', 'processing', 'retry_wait', 'ready', 'failed')",
            name=op.f("ck_finished_set_archives_finished_set_archive_state"),
        ),
        sa.CheckConstraint(
            "selection_count > 0",
            name=op.f("ck_finished_set_archives_positive_selection_count"),
        ),
        sa.CheckConstraint(
            "attempts >= 0",
            name=op.f("ck_finished_set_archives_nonnegative_attempts"),
        ),
        sa.CheckConstraint(
            "max_attempts > 0",
            name=op.f("ck_finished_set_archives_positive_max_attempts"),
        ),
        sa.CheckConstraint(
            "attempts <= max_attempts",
            name=op.f("ck_finished_set_archives_attempts_within_limit"),
        ),
        sa.CheckConstraint(
            "(lease_owner IS NULL AND lease_expires_at IS NULL) OR "
            "(lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name=op.f("ck_finished_set_archives_lease_pair"),
        ),
        sa.CheckConstraint(
            "(state = 'processing' AND lease_owner IS NOT NULL) OR "
            "(state <> 'processing' AND lease_owner IS NULL)",
            name=op.f("ck_finished_set_archives_state_lease_contract"),
        ),
        sa.CheckConstraint(
            "manifest_sha256 IS NULL OR length(manifest_sha256) = 64",
            name=op.f("ck_finished_set_archives_valid_manifest_sha256"),
        ),
        sa.CheckConstraint(
            "part_count IS NULL OR part_count > 0",
            name=op.f("ck_finished_set_archives_positive_part_count"),
        ),
        sa.CheckConstraint(
            "(state = 'ready' AND manifest_sha256 IS NOT NULL AND part_count IS NOT NULL "
            "AND completed_at IS NOT NULL AND last_error_code IS NULL) OR "
            "(state = 'failed' AND completed_at IS NOT NULL AND last_error_code IS NOT NULL) OR "
            "(state NOT IN ('ready', 'failed') AND completed_at IS NULL)",
            name=op.f("ck_finished_set_archives_terminal_result_contract"),
        ),
        sa.ForeignKeyConstraint(
            ["release_version_id"],
            ["release_versions.id"],
            name=op.f("fk_finished_set_archives_release_version_id_release_versions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_user_id"],
            ["admin_users.id"],
            name=op.f("fk_finished_set_archives_requested_by_user_id_admin_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["review_task_id"],
            ["review_tasks.id"],
            name=op.f("fk_finished_set_archives_review_task_id_review_tasks"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_finished_set_archives")),
        sa.UniqueConstraint(
            "review_task_id",
            name="uq_finished_set_archives_review_task",
        ),
    )
    op.create_index(
        op.f("ix_finished_set_archives_review_task_id"),
        "finished_set_archives",
        ["review_task_id"],
    )
    op.create_index(
        op.f("ix_finished_set_archives_release_version_id"),
        "finished_set_archives",
        ["release_version_id"],
    )
    op.create_index(
        "ix_finished_set_archives_claim",
        "finished_set_archives",
        ["state", "available_at", "lease_expires_at", "created_at"],
    )

    op.create_table(
        "finished_set_archive_parts",
        sa.Column("archive_id", sa.Uuid(), nullable=False),
        sa.Column("part_number", sa.Integer(), nullable=False),
        sa.Column("part_count", sa.Integer(), nullable=False),
        sa.Column("first_ordinal", sa.Integer(), nullable=False),
        sa.Column("last_ordinal", sa.Integer(), nullable=False),
        sa.Column("storage_backend", sa.String(length=50), nullable=False),
        sa.Column("storage_bucket", sa.String(length=255), nullable=False),
        sa.Column("object_key", sa.String(length=1024), nullable=False),
        sa.Column("object_version_id", sa.String(length=1024), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("content_type", sa.String(length=100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "part_number > 0 AND part_count > 0 AND part_number <= part_count",
            name=op.f("ck_finished_set_archive_parts_valid_part_identity"),
        ),
        sa.CheckConstraint(
            "first_ordinal > 0 AND last_ordinal >= first_ordinal",
            name=op.f("ck_finished_set_archive_parts_valid_ordinal_range"),
        ),
        sa.CheckConstraint(
            "length(sha256) = 64",
            name=op.f("ck_finished_set_archive_parts_valid_sha256"),
        ),
        sa.CheckConstraint(
            "length(manifest_sha256) = 64",
            name=op.f("ck_finished_set_archive_parts_valid_manifest_sha256"),
        ),
        sa.CheckConstraint(
            "byte_size > 0",
            name=op.f("ck_finished_set_archive_parts_positive_byte_size"),
        ),
        sa.CheckConstraint(
            "length(trim(storage_backend)) > 0 "
            "AND length(trim(storage_bucket)) > 0 "
            "AND length(trim(object_key)) > 0 "
            "AND length(trim(object_version_id)) > 0 "
            "AND content_type = 'application/zip'",
            name=op.f("ck_finished_set_archive_parts_complete_storage_identity"),
        ),
        sa.ForeignKeyConstraint(
            ["archive_id"],
            ["finished_set_archives.id"],
            name=op.f("fk_finished_set_archive_parts_archive_id_finished_set_archives"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_finished_set_archive_parts")),
        sa.UniqueConstraint(
            "archive_id",
            "part_number",
            name="uq_finished_set_archive_parts_archive_part",
        ),
        sa.UniqueConstraint(
            "storage_backend",
            "storage_bucket",
            "object_key",
            "object_version_id",
            name="uq_finished_set_archive_parts_storage_version",
        ),
    )
    op.create_index(
        op.f("ix_finished_set_archive_parts_archive_id"),
        "finished_set_archive_parts",
        ["archive_id"],
    )
    _create_part_guards(op.get_bind().dialect.name)


def downgrade() -> None:
    _drop_part_guards(op.get_bind().dialect.name)
    op.drop_index(
        op.f("ix_finished_set_archive_parts_archive_id"),
        table_name="finished_set_archive_parts",
    )
    op.drop_table("finished_set_archive_parts")
    op.drop_index("ix_finished_set_archives_claim", table_name="finished_set_archives")
    op.drop_index(
        op.f("ix_finished_set_archives_release_version_id"),
        table_name="finished_set_archives",
    )
    op.drop_index(
        op.f("ix_finished_set_archives_review_task_id"),
        table_name="finished_set_archives",
    )
    op.drop_table("finished_set_archives")
    op.drop_index(
        "uq_release_selections_task_generation_queue_position",
        table_name="release_selections",
    )
    op.drop_column("release_selections", "source_generation_queue_position")
    op.drop_column("release_selections", "source_generation_ordinal")
    op.drop_column("release_selections", "source_output_index")
    op.drop_column("release_selections", "source_generation_job_id")


def _create_part_guards(dialect_name: str) -> None:
    if dialect_name == "sqlite":
        for operation in ("UPDATE", "DELETE"):
            op.execute(
                sa.text(
                    "CREATE TRIGGER finished_set_archive_parts_immutable_"
                    f"{operation.lower()} BEFORE {operation} ON finished_set_archive_parts "
                    "BEGIN SELECT RAISE(ABORT, 'finished set archive parts are append-only'); END"
                )
            )
    elif dialect_name == "postgresql":
        op.execute(
            sa.text(
                "CREATE OR REPLACE FUNCTION "
                "gen_automation_guard_finished_set_archive_parts_mutation() "
                "RETURNS trigger AS $$ BEGIN "
                "RAISE EXCEPTION 'finished set archive parts are append-only'; "
                "END; $$ LANGUAGE plpgsql"
            )
        )
        op.execute(
            sa.text(
                "CREATE TRIGGER finished_set_archive_parts_guard_mutation "
                "BEFORE UPDATE OR DELETE ON finished_set_archive_parts FOR EACH ROW "
                "EXECUTE FUNCTION gen_automation_guard_finished_set_archive_parts_mutation()"
            )
        )


def _drop_part_guards(dialect_name: str) -> None:
    if dialect_name == "sqlite":
        for operation in ("update", "delete"):
            op.execute(
                sa.text("DROP TRIGGER IF EXISTS finished_set_archive_parts_immutable_" + operation)
            )
    elif dialect_name == "postgresql":
        op.execute(
            sa.text(
                "DROP TRIGGER IF EXISTS finished_set_archive_parts_guard_mutation "
                "ON finished_set_archive_parts"
            )
        )
        op.execute(
            sa.text(
                "DROP FUNCTION IF EXISTS gen_automation_guard_finished_set_archive_parts_mutation()"
            )
        )
