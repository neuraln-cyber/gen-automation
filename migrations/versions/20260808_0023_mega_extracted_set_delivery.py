"""Add extracted finished-set delivery records for MEGA.

Revision ID: 20260808_0023
Revises: 20260806_0022
Create Date: 2026-08-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260808_0023"
down_revision: str | None = "20260806_0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

mega_delivery_state = sa.Enum(
    "pending",
    "claimed",
    "retry_wait",
    "succeeded",
    "failed",
    name="mega_delivery_state",
    native_enum=False,
    create_constraint=True,
    length=10,
)


def upgrade() -> None:
    op.create_table(
        "mega_set_deliveries",
        sa.Column("finished_set_archive_id", sa.Uuid(), nullable=False),
        sa.Column("state", mega_delivery_state, nullable=False),
        sa.Column("remote_root", sa.String(length=1024), nullable=False),
        sa.Column("remote_folder", sa.String(length=1024), nullable=False),
        sa.Column("manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column("total_item_count", sa.Integer(), nullable=False),
        sa.Column("uploaded_item_count", sa.Integer(), nullable=False),
        sa.Column("total_byte_size", sa.BigInteger(), nullable=True),
        sa.Column("source_manifest_json", sa.Text(), nullable=True),
        sa.Column("uploaded_byte_size", sa.BigInteger(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_owner", sa.String(length=200), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completion_marker_node_handle", sa.String(length=80), nullable=True),
        sa.Column("planned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=100), nullable=True),
        sa.Column("last_error_detail", sa.Text(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(manifest_sha256) = 64",
            name=op.f("ck_mega_set_deliveries_valid_manifest_sha256"),
        ),
        sa.CheckConstraint(
            "total_item_count > 0",
            name=op.f("ck_mega_set_deliveries_positive_total_item_count"),
        ),
        sa.CheckConstraint(
            "uploaded_item_count >= 0 AND uploaded_item_count <= total_item_count",
            name=op.f("ck_mega_set_deliveries_valid_uploaded_item_count"),
        ),
        sa.CheckConstraint(
            "total_byte_size IS NULL OR total_byte_size > 0",
            name=op.f("ck_mega_set_deliveries_positive_optional_total_byte_size"),
        ),
        sa.CheckConstraint(
            "uploaded_byte_size >= 0 "
            "AND (total_byte_size IS NULL OR uploaded_byte_size <= total_byte_size)",
            name=op.f("ck_mega_set_deliveries_valid_uploaded_byte_size"),
        ),
        sa.CheckConstraint(
            "(total_byte_size IS NULL AND source_manifest_json IS NULL "
            "AND planned_at IS NULL AND uploaded_byte_size = 0) OR "
            "(total_byte_size IS NOT NULL AND source_manifest_json IS NOT NULL "
            "AND length(source_manifest_json) > 0 AND planned_at IS NOT NULL)",
            name=op.f("ck_mega_set_deliveries_planning_contract"),
        ),
        sa.CheckConstraint(
            "attempts >= 0",
            name=op.f("ck_mega_set_deliveries_nonnegative_attempts"),
        ),
        sa.CheckConstraint(
            "length(trim(remote_root)) > 0 AND length(trim(remote_folder)) > 0",
            name=op.f("ck_mega_set_deliveries_complete_remote_identity"),
        ),
        sa.CheckConstraint(
            "(lease_owner IS NULL AND lease_expires_at IS NULL) OR "
            "(lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name=op.f("ck_mega_set_deliveries_lease_pair"),
        ),
        sa.CheckConstraint(
            "(state = 'claimed' AND lease_owner IS NOT NULL) OR "
            "(state <> 'claimed' AND lease_owner IS NULL)",
            name=op.f("ck_mega_set_deliveries_state_lease_contract"),
        ),
        sa.CheckConstraint(
            "(state = 'succeeded' AND uploaded_item_count = total_item_count "
            "AND total_byte_size IS NOT NULL AND uploaded_byte_size = total_byte_size "
            "AND completion_marker_node_handle IS NOT NULL "
            "AND verified_at IS NOT NULL AND completed_at IS NOT NULL "
            "AND last_error_code IS NULL) OR "
            "(state = 'failed' AND completion_marker_node_handle IS NULL "
            "AND verified_at IS NULL AND completed_at IS NOT NULL "
            "AND last_error_code IS NOT NULL) OR "
            "(state IN ('pending', 'claimed', 'retry_wait') "
            "AND completion_marker_node_handle IS NULL "
            "AND verified_at IS NULL AND completed_at IS NULL)",
            name=op.f("ck_mega_set_deliveries_terminal_result_contract"),
        ),
        sa.CheckConstraint(
            "started_at IS NULL OR started_at >= created_at",
            name=op.f("ck_mega_set_deliveries_start_after_creation"),
        ),
        sa.CheckConstraint(
            "planned_at IS NULL OR planned_at >= created_at",
            name=op.f("ck_mega_set_deliveries_plan_after_creation"),
        ),
        sa.CheckConstraint(
            "verified_at IS NULL OR verified_at >= created_at",
            name=op.f("ck_mega_set_deliveries_verification_after_creation"),
        ),
        sa.CheckConstraint(
            "completed_at IS NULL OR completed_at >= created_at",
            name=op.f("ck_mega_set_deliveries_completion_after_creation"),
        ),
        sa.ForeignKeyConstraint(
            ["finished_set_archive_id"],
            ["finished_set_archives.id"],
            name=op.f("fk_mega_set_deliveries_finished_set_archive_id_finished_set_archives"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mega_set_deliveries")),
        sa.UniqueConstraint(
            "finished_set_archive_id",
            name="uq_mega_set_deliveries_finished_set_archive",
        ),
    )
    op.create_index(
        op.f("ix_mega_set_deliveries_finished_set_archive_id"),
        "mega_set_deliveries",
        ["finished_set_archive_id"],
    )
    op.create_index(
        "ix_mega_set_deliveries_claim",
        "mega_set_deliveries",
        ["state", "available_at", "lease_expires_at", "created_at"],
    )

    op.create_table(
        "mega_set_delivery_items",
        sa.Column("delivery_id", sa.Uuid(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("source_derivative_output_id", sa.Uuid(), nullable=False),
        sa.Column("source_sha256", sa.String(length=64), nullable=False),
        sa.Column("source_byte_size", sa.BigInteger(), nullable=False),
        sa.Column("source_content_type", sa.String(length=100), nullable=False),
        sa.Column("remote_path", sa.String(length=1024), nullable=False),
        sa.Column("state", mega_delivery_state, nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("remote_node_handle", sa.String(length=80), nullable=True),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=100), nullable=True),
        sa.Column("last_error_detail", sa.Text(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "ordinal > 0",
            name=op.f("ck_mega_set_delivery_items_positive_ordinal"),
        ),
        sa.CheckConstraint(
            "length(source_sha256) = 64",
            name=op.f("ck_mega_set_delivery_items_valid_source_sha256"),
        ),
        sa.CheckConstraint(
            "source_byte_size > 0",
            name=op.f("ck_mega_set_delivery_items_positive_source_byte_size"),
        ),
        sa.CheckConstraint(
            "length(trim(source_content_type)) > 0 AND length(trim(remote_path)) > 0",
            name=op.f("ck_mega_set_delivery_items_complete_item_identity"),
        ),
        sa.CheckConstraint(
            "attempts >= 0",
            name=op.f("ck_mega_set_delivery_items_nonnegative_attempts"),
        ),
        sa.CheckConstraint(
            "(remote_node_handle IS NULL AND verified_at IS NULL) OR "
            "(remote_node_handle IS NOT NULL AND verified_at IS NOT NULL)",
            name=op.f("ck_mega_set_delivery_items_verified_node_pair"),
        ),
        sa.CheckConstraint(
            "(state = 'succeeded' AND uploaded_at IS NOT NULL "
            "AND completed_at IS NOT NULL AND last_error_code IS NULL) OR "
            "(state = 'failed' AND completed_at IS NOT NULL "
            "AND last_error_code IS NOT NULL) OR "
            "(state IN ('pending', 'claimed', 'retry_wait') AND completed_at IS NULL)",
            name=op.f("ck_mega_set_delivery_items_terminal_result_contract"),
        ),
        sa.CheckConstraint(
            "uploaded_at IS NULL OR uploaded_at >= created_at",
            name=op.f("ck_mega_set_delivery_items_upload_after_creation"),
        ),
        sa.CheckConstraint(
            "verified_at IS NULL OR verified_at >= created_at",
            name=op.f("ck_mega_set_delivery_items_verification_after_creation"),
        ),
        sa.CheckConstraint(
            "completed_at IS NULL OR completed_at >= created_at",
            name=op.f("ck_mega_set_delivery_items_completion_after_creation"),
        ),
        sa.ForeignKeyConstraint(
            ["delivery_id"],
            ["mega_set_deliveries.id"],
            name=op.f("fk_mega_set_delivery_items_delivery_id_mega_set_deliveries"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_derivative_output_id"],
            ["derivative_outputs.id"],
            name=op.f("fk_mega_set_delivery_items_source_derivative_output_id_derivative_outputs"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mega_set_delivery_items")),
        sa.UniqueConstraint(
            "delivery_id",
            "ordinal",
            name="uq_mega_set_delivery_items_delivery_ordinal",
        ),
        sa.UniqueConstraint(
            "delivery_id",
            "source_derivative_output_id",
            name="uq_mega_set_delivery_items_delivery_source",
        ),
        sa.UniqueConstraint(
            "remote_path",
            name="uq_mega_set_delivery_items_remote_path",
        ),
    )
    op.create_index(
        op.f("ix_mega_set_delivery_items_delivery_id"),
        "mega_set_delivery_items",
        ["delivery_id"],
    )
    op.create_index(
        op.f("ix_mega_set_delivery_items_source_derivative_output_id"),
        "mega_set_delivery_items",
        ["source_derivative_output_id"],
    )
    op.create_index(
        "ix_mega_set_delivery_items_progress",
        "mega_set_delivery_items",
        ["delivery_id", "state", "available_at", "ordinal"],
    )
    _create_identity_guards(op.get_bind().dialect.name)


def downgrade() -> None:
    _drop_identity_guards(op.get_bind().dialect.name)
    op.drop_index(
        "ix_mega_set_delivery_items_progress",
        table_name="mega_set_delivery_items",
    )
    op.drop_index(
        op.f("ix_mega_set_delivery_items_source_derivative_output_id"),
        table_name="mega_set_delivery_items",
    )
    op.drop_index(
        op.f("ix_mega_set_delivery_items_delivery_id"),
        table_name="mega_set_delivery_items",
    )
    op.drop_table("mega_set_delivery_items")
    op.drop_index("ix_mega_set_deliveries_claim", table_name="mega_set_deliveries")
    op.drop_index(
        op.f("ix_mega_set_deliveries_finished_set_archive_id"),
        table_name="mega_set_deliveries",
    )
    op.drop_table("mega_set_deliveries")


def _create_identity_guards(dialect_name: str) -> None:
    if dialect_name == "sqlite":
        op.execute(
            sa.text(
                "CREATE TRIGGER mega_set_deliveries_guard_update "
                "BEFORE UPDATE ON mega_set_deliveries BEGIN "
                "SELECT CASE WHEN "
                "OLD.id IS NOT NEW.id "
                "OR OLD.finished_set_archive_id IS NOT NEW.finished_set_archive_id "
                "OR OLD.remote_root IS NOT NEW.remote_root "
                "OR OLD.remote_folder IS NOT NEW.remote_folder "
                "OR OLD.manifest_sha256 IS NOT NEW.manifest_sha256 "
                "OR OLD.total_item_count IS NOT NEW.total_item_count "
                "OR OLD.created_at IS NOT NEW.created_at "
                "OR (OLD.total_byte_size IS NOT NEW.total_byte_size "
                "AND NOT (OLD.total_byte_size IS NULL AND NEW.total_byte_size IS NOT NULL)) "
                "OR (OLD.source_manifest_json IS NOT NEW.source_manifest_json "
                "AND NOT (OLD.source_manifest_json IS NULL "
                "AND NEW.source_manifest_json IS NOT NULL)) "
                "OR (OLD.planned_at IS NOT NEW.planned_at "
                "AND NOT (OLD.planned_at IS NULL AND NEW.planned_at IS NOT NULL)) "
                "THEN RAISE(ABORT, 'MEGA set delivery identity is immutable') END; END"
            )
        )
        op.execute(
            sa.text(
                "CREATE TRIGGER mega_set_deliveries_reject_delete "
                "BEFORE DELETE ON mega_set_deliveries BEGIN "
                "SELECT RAISE(ABORT, 'MEGA set deliveries cannot be deleted'); END"
            )
        )
        op.execute(
            sa.text(
                "CREATE TRIGGER mega_set_delivery_items_guard_update "
                "BEFORE UPDATE ON mega_set_delivery_items BEGIN "
                "SELECT CASE WHEN "
                "OLD.id IS NOT NEW.id "
                "OR OLD.delivery_id IS NOT NEW.delivery_id "
                "OR OLD.ordinal IS NOT NEW.ordinal "
                "OR OLD.source_derivative_output_id IS NOT NEW.source_derivative_output_id "
                "OR OLD.source_sha256 IS NOT NEW.source_sha256 "
                "OR OLD.source_byte_size IS NOT NEW.source_byte_size "
                "OR OLD.source_content_type IS NOT NEW.source_content_type "
                "OR OLD.remote_path IS NOT NEW.remote_path "
                "OR OLD.created_at IS NOT NEW.created_at "
                "THEN RAISE(ABORT, 'MEGA set delivery item identity is immutable') END; END"
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
                "gen_automation_guard_mega_set_delivery_mutation() "
                "RETURNS trigger AS $$ BEGIN "
                "IF TG_OP = 'DELETE' THEN "
                "RAISE EXCEPTION 'MEGA set deliveries cannot be deleted'; END IF; "
                "IF OLD.id IS DISTINCT FROM NEW.id "
                "OR OLD.finished_set_archive_id IS DISTINCT FROM NEW.finished_set_archive_id "
                "OR OLD.remote_root IS DISTINCT FROM NEW.remote_root "
                "OR OLD.remote_folder IS DISTINCT FROM NEW.remote_folder "
                "OR OLD.manifest_sha256 IS DISTINCT FROM NEW.manifest_sha256 "
                "OR OLD.total_item_count IS DISTINCT FROM NEW.total_item_count "
                "OR OLD.created_at IS DISTINCT FROM NEW.created_at "
                "OR (OLD.total_byte_size IS DISTINCT FROM NEW.total_byte_size "
                "AND NOT (OLD.total_byte_size IS NULL AND NEW.total_byte_size IS NOT NULL)) "
                "OR (OLD.source_manifest_json IS DISTINCT FROM NEW.source_manifest_json "
                "AND NOT (OLD.source_manifest_json IS NULL "
                "AND NEW.source_manifest_json IS NOT NULL)) "
                "OR (OLD.planned_at IS DISTINCT FROM NEW.planned_at "
                "AND NOT (OLD.planned_at IS NULL AND NEW.planned_at IS NOT NULL)) THEN "
                "RAISE EXCEPTION 'MEGA set delivery identity is immutable'; END IF; "
                "RETURN NEW; END; $$ LANGUAGE plpgsql"
            )
        )
        op.execute(
            sa.text(
                "CREATE TRIGGER mega_set_deliveries_guard_mutation "
                "BEFORE UPDATE OR DELETE ON mega_set_deliveries FOR EACH ROW "
                "EXECUTE FUNCTION gen_automation_guard_mega_set_delivery_mutation()"
            )
        )
        op.execute(
            sa.text(
                "CREATE OR REPLACE FUNCTION "
                "gen_automation_guard_mega_set_delivery_item_mutation() "
                "RETURNS trigger AS $$ BEGIN "
                "IF TG_OP = 'DELETE' THEN "
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


def _drop_identity_guards(dialect_name: str) -> None:
    if dialect_name == "sqlite":
        for name in (
            "mega_set_delivery_items_reject_delete",
            "mega_set_delivery_items_guard_update",
            "mega_set_deliveries_reject_delete",
            "mega_set_deliveries_guard_update",
        ):
            op.execute(sa.text(f"DROP TRIGGER IF EXISTS {name}"))
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
        op.execute(
            sa.text(
                "DROP TRIGGER IF EXISTS mega_set_deliveries_guard_mutation ON mega_set_deliveries"
            )
        )
        op.execute(
            sa.text("DROP FUNCTION IF EXISTS gen_automation_guard_mega_set_delivery_mutation()")
        )
