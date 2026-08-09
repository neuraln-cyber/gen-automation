"""Add durable managed-LoRA catalog and import jobs.

Revision ID: 20260809_0033
Revises: 20260809_0032
Create Date: 2026-08-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260809_0033"
down_revision: str | None = "20260809_0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def _lower_hex_check(column_name: str) -> str:
    expression = column_name
    for character in "0123456789abcdef":
        expression = f"replace({expression}, '{character}', '')"
    return (
        f"length({column_name}) = 64 AND {column_name} = lower({column_name}) "
        f"AND length({expression}) = 0"
    )


def upgrade() -> None:
    with op.batch_alter_table("salad_deployments") as batch_op:
        batch_op.add_column(
            sa.Column(
                "runtime_artifact_manifest_sha256",
                sa.String(length=64),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "runtime_managed_lora_sha256s",
                json_type,
                nullable=True,
            )
        )
        batch_op.create_check_constraint(
            "valid_runtime_artifact_manifest_sha256",
            "runtime_artifact_manifest_sha256 IS NULL OR ("
            + _lower_hex_check("runtime_artifact_manifest_sha256")
            + ")",
        )

    op.create_table(
        "managed_lora_artifacts",
        sa.Column("artifact_sha256", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("source_type", sa.String(length=7), nullable=False),
        sa.Column("canonical_source_url", sa.Text(), nullable=False),
        sa.Column("license_url", sa.Text(), nullable=False),
        sa.Column("civitai_model_id", sa.BigInteger(), nullable=True),
        sa.Column("civitai_version_id", sa.BigInteger(), nullable=True),
        sa.Column("civitai_file_id", sa.BigInteger(), nullable=True),
        sa.Column("provenance", json_type, nullable=False),
        sa.Column("storage_bucket", sa.String(length=255), nullable=False),
        sa.Column("object_key", sa.String(length=1024), nullable=False),
        sa.Column("object_version_id", sa.String(length=1024), nullable=False),
        sa.Column("object_etag", sa.String(length=80), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("target_filename", sa.String(length=236), nullable=False),
        sa.Column("approval_id", sa.Uuid(), nullable=False),
        sa.Column("trigger_words", json_type, nullable=False),
        sa.Column("lifecycle", sa.String(length=18), nullable=False),
        sa.Column("purge_requested", sa.Boolean(), nullable=False),
        sa.Column("registered_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("retirement_requested_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("restored_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retirement_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("restored_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("purged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lifecycle_error_code", sa.String(length=100), nullable=True),
        sa.Column("lifecycle_error_detail", sa.Text(), nullable=True),
        sa.Column(
            "lifecycle_error_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("lifecycle_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lock_version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "source_type IN ('manual', 'civitai')",
            name=op.f("ck_managed_lora_artifacts_lora_import_source"),
        ),
        sa.CheckConstraint(
            "lifecycle IN ('pending_activation', 'active', 'retiring', 'retired', 'purged')",
            name=op.f("ck_managed_lora_artifacts_managed_lora_lifecycle"),
        ),
        sa.CheckConstraint(
            _lower_hex_check("artifact_sha256"),
            name=op.f("ck_managed_lora_artifacts_valid_artifact_sha256"),
        ),
        sa.CheckConstraint(
            "byte_size > 0",
            name=op.f("ck_managed_lora_artifacts_positive_byte_size"),
        ),
        sa.CheckConstraint(
            "lock_version > 0",
            name=op.f("ck_managed_lora_artifacts_positive_lock_version"),
        ),
        sa.CheckConstraint(
            "lifecycle_error_count >= 0",
            name=op.f("ck_managed_lora_artifacts_nonnegative_lifecycle_error_count"),
        ),
        sa.CheckConstraint(
            "(lifecycle_error_code IS NULL AND lifecycle_error_detail IS NULL) "
            "OR (lifecycle_error_code IS NOT NULL AND lifecycle_error_detail IS NOT NULL)",
            name=op.f("ck_managed_lora_artifacts_complete_lifecycle_error"),
        ),
        sa.CheckConstraint(
            "length(trim(display_name)) > 0 "
            "AND length(trim(canonical_source_url)) > 0 "
            "AND length(trim(license_url)) > 0 "
            "AND length(trim(storage_bucket)) > 0 "
            "AND length(trim(object_key)) > 0 "
            "AND length(trim(object_version_id)) > 0 "
            "AND length(trim(object_etag)) > 0 "
            "AND length(trim(target_filename)) > 0",
            name=op.f("ck_managed_lora_artifacts_complete_identity"),
        ),
        sa.CheckConstraint(
            "lower(target_filename) LIKE '%.safetensors'",
            name=op.f("ck_managed_lora_artifacts_safetensors_target"),
        ),
        sa.CheckConstraint(
            "object_key = 'worker/managed-loras/sha256/' || artifact_sha256 || '.safetensors'",
            name=op.f("ck_managed_lora_artifacts_content_addressed_object_key"),
        ),
        sa.CheckConstraint(
            "(lifecycle = 'active' AND activated_at IS NOT NULL) OR lifecycle <> 'active'",
            name=op.f("ck_managed_lora_artifacts_active_timestamp"),
        ),
        sa.CheckConstraint(
            "(lifecycle IN ('retiring', 'retired', 'purged') "
            "AND retirement_requested_at IS NOT NULL) "
            "OR lifecycle IN ('pending_activation', 'active')",
            name=op.f("ck_managed_lora_artifacts_retirement_timestamp"),
        ),
        sa.CheckConstraint(
            "(lifecycle = 'purged' AND purge_requested = true "
            "AND retired_at IS NOT NULL AND purged_at IS NOT NULL) "
            "OR (lifecycle <> 'purged' AND purged_at IS NULL)",
            name=op.f("ck_managed_lora_artifacts_purge_state"),
        ),
        sa.ForeignKeyConstraint(
            ["approval_id"],
            ["model_artifact_approvals.id"],
            name=op.f("fk_managed_lora_artifacts_approval_id_model_artifact_approvals"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["registered_by_user_id"],
            ["admin_users.id"],
            name=op.f("fk_managed_lora_artifacts_registered_by_user_id_admin_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["retirement_requested_by_user_id"],
            ["admin_users.id"],
            name=op.f("fk_managed_lora_artifacts_retirement_requested_by_user_id_admin_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["restored_by_user_id"],
            ["admin_users.id"],
            name=op.f("fk_managed_lora_artifacts_restored_by_user_id_admin_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_managed_lora_artifacts")),
        sa.UniqueConstraint(
            "storage_bucket",
            "object_key",
            "object_version_id",
            name="uq_managed_lora_artifacts_storage_version",
        ),
    )
    op.create_index(
        op.f("ix_managed_lora_artifacts_approval_id"),
        "managed_lora_artifacts",
        ["approval_id"],
    )
    op.create_index(
        op.f("ix_managed_lora_artifacts_registered_by_user_id"),
        "managed_lora_artifacts",
        ["registered_by_user_id"],
    )
    op.create_index(
        "ix_managed_lora_artifacts_lifecycle_created",
        "managed_lora_artifacts",
        ["lifecycle", "lifecycle_retry_at", "created_at"],
    )
    for name, column in (
        ("uq_managed_lora_artifacts_live_sha256", "artifact_sha256"),
        ("uq_managed_lora_artifacts_live_approval", "approval_id"),
        ("uq_managed_lora_artifacts_live_target_filename", "target_filename"),
    ):
        op.create_index(
            name,
            "managed_lora_artifacts",
            [column],
            unique=True,
            postgresql_where=sa.text("lifecycle <> 'purged'"),
            sqlite_where=sa.text("lifecycle <> 'purged'"),
        )

    op.create_table(
        "lora_import_jobs",
        sa.Column("source_type", sa.String(length=7), nullable=False),
        sa.Column("state", sa.String(length=15), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("canonical_source_url", sa.Text(), nullable=False),
        sa.Column("license_url", sa.Text(), nullable=False),
        sa.Column("commercial_use_attested", sa.Boolean(), nullable=False),
        sa.Column("adult_use_attested", sa.Boolean(), nullable=False),
        sa.Column("civitai_model_id", sa.BigInteger(), nullable=True),
        sa.Column("civitai_version_id", sa.BigInteger(), nullable=True),
        sa.Column("civitai_file_id", sa.BigInteger(), nullable=True),
        sa.Column("staging_bucket", sa.String(length=255), nullable=True),
        sa.Column("staging_object_key", sa.String(length=1024), nullable=True),
        sa.Column("staging_object_version_id", sa.String(length=1024), nullable=True),
        sa.Column("staging_object_etag", sa.String(length=80), nullable=True),
        sa.Column("staging_byte_size", sa.BigInteger(), nullable=True),
        sa.Column("target_filename", sa.String(length=236), nullable=False),
        sa.Column("expected_sha256", sa.String(length=64), nullable=True),
        sa.Column("expected_byte_size", sa.BigInteger(), nullable=True),
        sa.Column("expected_metadata", json_type, nullable=False),
        sa.Column("trigger_words", json_type, nullable=False),
        sa.Column("progress_bytes", sa.BigInteger(), nullable=False),
        sa.Column("total_bytes", sa.BigInteger(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_owner", sa.String(length=200), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=100), nullable=True),
        sa.Column("last_error_detail", sa.Text(), nullable=True),
        sa.Column("result_artifact_id", sa.Uuid(), nullable=True),
        sa.Column("requested_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("cancelled_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("lock_version", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_progress_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "source_type IN ('manual', 'civitai')",
            name=op.f("ck_lora_import_jobs_lora_import_source"),
        ),
        sa.CheckConstraint(
            "state IN ('awaiting_upload', 'queued', 'claimed', 'retry_wait', "
            "'failed', 'duplicate', 'completed', 'cancelled')",
            name=op.f("ck_lora_import_jobs_lora_import_job_state"),
        ),
        sa.CheckConstraint(
            "attempts >= 0",
            name=op.f("ck_lora_import_jobs_nonnegative_attempts"),
        ),
        sa.CheckConstraint(
            "max_attempts > 0",
            name=op.f("ck_lora_import_jobs_positive_max_attempts"),
        ),
        sa.CheckConstraint(
            "attempts <= max_attempts",
            name=op.f("ck_lora_import_jobs_attempts_within_limit"),
        ),
        sa.CheckConstraint(
            "commercial_use_attested = true AND adult_use_attested = true",
            name=op.f("ck_lora_import_jobs_rights_attested"),
        ),
        sa.CheckConstraint(
            "lock_version > 0",
            name=op.f("ck_lora_import_jobs_positive_lock_version"),
        ),
        sa.CheckConstraint(
            "progress_bytes >= 0",
            name=op.f("ck_lora_import_jobs_nonnegative_progress"),
        ),
        sa.CheckConstraint(
            "total_bytes IS NULL OR total_bytes > 0",
            name=op.f("ck_lora_import_jobs_positive_total_bytes"),
        ),
        sa.CheckConstraint(
            "total_bytes IS NULL OR progress_bytes <= total_bytes",
            name=op.f("ck_lora_import_jobs_progress_within_total"),
        ),
        sa.CheckConstraint(
            "expected_sha256 IS NULL OR (" + _lower_hex_check("expected_sha256") + ")",
            name=op.f("ck_lora_import_jobs_valid_expected_sha256"),
        ),
        sa.CheckConstraint(
            "expected_byte_size IS NULL OR expected_byte_size > 0",
            name=op.f("ck_lora_import_jobs_positive_expected_byte_size"),
        ),
        sa.CheckConstraint(
            "(source_type = 'manual' AND staging_bucket IS NOT NULL "
            "AND staging_object_key IS NOT NULL "
            "AND civitai_model_id IS NULL AND civitai_version_id IS NULL "
            "AND civitai_file_id IS NULL) "
            "OR (source_type = 'civitai' AND staging_bucket IS NULL "
            "AND staging_object_key IS NULL)",
            name=op.f("ck_lora_import_jobs_source_contract"),
        ),
        sa.CheckConstraint(
            "(staging_object_version_id IS NULL AND staging_object_etag IS NULL "
            "AND staging_byte_size IS NULL) OR "
            "(staging_object_version_id IS NOT NULL AND staging_object_etag IS NOT NULL "
            "AND staging_byte_size IS NOT NULL AND staging_byte_size > 0)",
            name=op.f("ck_lora_import_jobs_staging_version_tuple"),
        ),
        sa.CheckConstraint(
            "source_type = 'manual' OR staging_object_version_id IS NULL",
            name=op.f("ck_lora_import_jobs_manual_staging_version_only"),
        ),
        sa.CheckConstraint(
            "state <> 'awaiting_upload' OR (source_type = 'manual' "
            "AND staging_object_version_id IS NULL)",
            name=op.f("ck_lora_import_jobs_awaiting_manual_upload"),
        ),
        sa.CheckConstraint(
            "state NOT IN ('queued', 'claimed', 'retry_wait', 'failed') "
            "OR source_type = 'civitai' OR staging_object_version_id IS NOT NULL",
            name=op.f("ck_lora_import_jobs_manual_processing_has_version"),
        ),
        sa.CheckConstraint(
            "(state = 'claimed' AND lease_owner IS NOT NULL "
            "AND lease_expires_at IS NOT NULL) OR "
            "(state <> 'claimed' AND lease_owner IS NULL AND lease_expires_at IS NULL)",
            name=op.f("ck_lora_import_jobs_lease_state"),
        ),
        sa.CheckConstraint(
            "(state = 'completed' AND result_artifact_id IS NOT NULL "
            "AND completed_at IS NOT NULL AND last_error_code IS NULL) OR "
            "(state = 'duplicate' AND completed_at IS NOT NULL AND ("
            "(result_artifact_id IS NOT NULL AND last_error_code IS NULL) OR "
            "(result_artifact_id IS NULL "
            "AND last_error_code = 'already_available_static' "
            "AND last_error_detail IS NOT NULL))) OR "
            "(state = 'failed' AND result_artifact_id IS NULL "
            "AND completed_at IS NOT NULL AND last_error_code IS NOT NULL) OR "
            "(state = 'cancelled' AND result_artifact_id IS NULL "
            "AND completed_at IS NOT NULL) OR "
            "(state NOT IN ('completed', 'duplicate', 'failed', 'cancelled') "
            "AND result_artifact_id IS NULL AND completed_at IS NULL)",
            name=op.f("ck_lora_import_jobs_terminal_result"),
        ),
        sa.ForeignKeyConstraint(
            ["result_artifact_id"],
            ["managed_lora_artifacts.id"],
            name=op.f("fk_lora_import_jobs_result_artifact_id_managed_lora_artifacts"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_user_id"],
            ["admin_users.id"],
            name=op.f("fk_lora_import_jobs_requested_by_user_id_admin_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["cancelled_by_user_id"],
            ["admin_users.id"],
            name=op.f("fk_lora_import_jobs_cancelled_by_user_id_admin_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_lora_import_jobs")),
        sa.UniqueConstraint(
            "staging_bucket",
            "staging_object_key",
            name="uq_lora_import_jobs_staging_object",
        ),
    )
    op.create_index(
        op.f("ix_lora_import_jobs_result_artifact_id"),
        "lora_import_jobs",
        ["result_artifact_id"],
    )
    op.create_index(
        op.f("ix_lora_import_jobs_requested_by_user_id"),
        "lora_import_jobs",
        ["requested_by_user_id"],
    )
    op.create_index(
        "ix_lora_import_jobs_claim",
        "lora_import_jobs",
        ["state", "available_at", "lease_expires_at", "created_at"],
    )
    op.create_index(
        "ix_lora_import_jobs_requester_created",
        "lora_import_jobs",
        ["requested_by_user_id", "created_at"],
    )
    _create_catalog_guards(op.get_bind().dialect.name)


def downgrade() -> None:
    connection = op.get_bind()
    for table_name in ("lora_import_jobs", "managed_lora_artifacts"):
        if connection.execute(sa.text(f"SELECT 1 FROM {table_name} LIMIT 1")).first():  # noqa: S608
            raise RuntimeError("cannot downgrade after durable LoRA catalog data exists")
    _drop_catalog_guards(connection.dialect.name)
    op.drop_index("ix_lora_import_jobs_requester_created", table_name="lora_import_jobs")
    op.drop_index("ix_lora_import_jobs_claim", table_name="lora_import_jobs")
    op.drop_index(
        op.f("ix_lora_import_jobs_requested_by_user_id"),
        table_name="lora_import_jobs",
    )
    op.drop_index(
        op.f("ix_lora_import_jobs_result_artifact_id"),
        table_name="lora_import_jobs",
    )
    op.drop_table("lora_import_jobs")
    op.drop_index(
        "ix_managed_lora_artifacts_lifecycle_created",
        table_name="managed_lora_artifacts",
    )
    op.drop_index(
        op.f("ix_managed_lora_artifacts_registered_by_user_id"),
        table_name="managed_lora_artifacts",
    )
    op.drop_index(
        op.f("ix_managed_lora_artifacts_approval_id"),
        table_name="managed_lora_artifacts",
    )
    op.drop_table("managed_lora_artifacts")
    with op.batch_alter_table("salad_deployments") as batch_op:
        batch_op.drop_constraint(
            "valid_runtime_artifact_manifest_sha256",
            type_="check",
        )
        batch_op.drop_column("runtime_managed_lora_sha256s")
        batch_op.drop_column("runtime_artifact_manifest_sha256")


def _create_catalog_guards(dialect_name: str) -> None:
    if dialect_name == "sqlite":
        op.execute(
            sa.text(
                "CREATE TRIGGER managed_lora_artifacts_guard_update "
                "BEFORE UPDATE ON managed_lora_artifacts BEGIN SELECT CASE WHEN "
                "OLD.id IS NOT NEW.id OR OLD.artifact_sha256 IS NOT NEW.artifact_sha256 "
                "OR OLD.display_name IS NOT NEW.display_name "
                "OR OLD.source_type IS NOT NEW.source_type "
                "OR OLD.canonical_source_url IS NOT NEW.canonical_source_url "
                "OR OLD.license_url IS NOT NEW.license_url "
                "OR OLD.civitai_model_id IS NOT NEW.civitai_model_id "
                "OR OLD.civitai_version_id IS NOT NEW.civitai_version_id "
                "OR OLD.civitai_file_id IS NOT NEW.civitai_file_id "
                "OR OLD.provenance IS NOT NEW.provenance "
                "OR OLD.storage_bucket IS NOT NEW.storage_bucket "
                "OR OLD.object_key IS NOT NEW.object_key "
                "OR OLD.object_version_id IS NOT NEW.object_version_id "
                "OR OLD.object_etag IS NOT NEW.object_etag "
                "OR OLD.byte_size IS NOT NEW.byte_size "
                "OR OLD.target_filename IS NOT NEW.target_filename "
                "OR OLD.approval_id IS NOT NEW.approval_id "
                "OR OLD.trigger_words IS NOT NEW.trigger_words "
                "OR OLD.registered_by_user_id IS NOT NEW.registered_by_user_id "
                "OR OLD.created_at IS NOT NEW.created_at "
                "THEN RAISE(ABORT, 'managed LoRA identity is immutable') END; END"
            )
        )
        op.execute(
            sa.text(
                "CREATE TRIGGER managed_lora_artifacts_reject_delete "
                "BEFORE DELETE ON managed_lora_artifacts BEGIN "
                "SELECT RAISE(ABORT, 'managed LoRAs cannot be deleted'); END"
            )
        )
        op.execute(
            sa.text(
                "CREATE TRIGGER lora_import_jobs_guard_update "
                "BEFORE UPDATE ON lora_import_jobs BEGIN SELECT CASE WHEN "
                "OLD.id IS NOT NEW.id OR OLD.source_type IS NOT NEW.source_type "
                "OR OLD.display_name IS NOT NEW.display_name "
                "OR OLD.canonical_source_url IS NOT NEW.canonical_source_url "
                "OR OLD.license_url IS NOT NEW.license_url "
                "OR OLD.commercial_use_attested IS NOT NEW.commercial_use_attested "
                "OR OLD.adult_use_attested IS NOT NEW.adult_use_attested "
                "OR OLD.civitai_model_id IS NOT NEW.civitai_model_id "
                "OR OLD.civitai_version_id IS NOT NEW.civitai_version_id "
                "OR OLD.civitai_file_id IS NOT NEW.civitai_file_id "
                "OR OLD.staging_bucket IS NOT NEW.staging_bucket "
                "OR OLD.staging_object_key IS NOT NEW.staging_object_key "
                "OR (OLD.staging_object_version_id IS NOT NEW.staging_object_version_id "
                "AND NOT (OLD.staging_object_version_id IS NULL "
                "AND NEW.staging_object_version_id IS NOT NULL)) "
                "OR (OLD.staging_object_etag IS NOT NEW.staging_object_etag "
                "AND NOT (OLD.staging_object_etag IS NULL "
                "AND NEW.staging_object_etag IS NOT NULL)) "
                "OR (OLD.staging_byte_size IS NOT NEW.staging_byte_size "
                "AND NOT (OLD.staging_byte_size IS NULL "
                "AND NEW.staging_byte_size IS NOT NULL)) "
                "OR OLD.target_filename IS NOT NEW.target_filename "
                "OR OLD.expected_sha256 IS NOT NEW.expected_sha256 "
                "OR OLD.expected_byte_size IS NOT NEW.expected_byte_size "
                "OR OLD.expected_metadata IS NOT NEW.expected_metadata "
                "OR OLD.trigger_words IS NOT NEW.trigger_words "
                "OR OLD.max_attempts IS NOT NEW.max_attempts "
                "OR OLD.requested_by_user_id IS NOT NEW.requested_by_user_id "
                "OR OLD.created_at IS NOT NEW.created_at "
                "OR (OLD.result_artifact_id IS NOT NEW.result_artifact_id "
                "AND NOT (OLD.result_artifact_id IS NULL "
                "AND NEW.result_artifact_id IS NOT NULL)) "
                "THEN RAISE(ABORT, 'LoRA import identity is immutable') END; END"
            )
        )
        op.execute(
            sa.text(
                "CREATE TRIGGER lora_import_jobs_reject_delete "
                "BEFORE DELETE ON lora_import_jobs BEGIN "
                "SELECT RAISE(ABORT, 'LoRA import jobs cannot be deleted'); END"
            )
        )
        return

    op.execute(
        sa.text(
            "CREATE OR REPLACE FUNCTION gen_automation_guard_managed_lora_mutation() "
            "RETURNS trigger AS $$ BEGIN IF TG_OP = 'DELETE' THEN "
            "RAISE EXCEPTION 'managed LoRAs cannot be deleted'; END IF; "
            "IF OLD.id IS DISTINCT FROM NEW.id "
            "OR OLD.artifact_sha256 IS DISTINCT FROM NEW.artifact_sha256 "
            "OR OLD.display_name IS DISTINCT FROM NEW.display_name "
            "OR OLD.source_type IS DISTINCT FROM NEW.source_type "
            "OR OLD.canonical_source_url IS DISTINCT FROM NEW.canonical_source_url "
            "OR OLD.license_url IS DISTINCT FROM NEW.license_url "
            "OR OLD.civitai_model_id IS DISTINCT FROM NEW.civitai_model_id "
            "OR OLD.civitai_version_id IS DISTINCT FROM NEW.civitai_version_id "
            "OR OLD.civitai_file_id IS DISTINCT FROM NEW.civitai_file_id "
            "OR OLD.provenance IS DISTINCT FROM NEW.provenance "
            "OR OLD.storage_bucket IS DISTINCT FROM NEW.storage_bucket "
            "OR OLD.object_key IS DISTINCT FROM NEW.object_key "
            "OR OLD.object_version_id IS DISTINCT FROM NEW.object_version_id "
            "OR OLD.object_etag IS DISTINCT FROM NEW.object_etag "
            "OR OLD.byte_size IS DISTINCT FROM NEW.byte_size "
            "OR OLD.target_filename IS DISTINCT FROM NEW.target_filename "
            "OR OLD.approval_id IS DISTINCT FROM NEW.approval_id "
            "OR OLD.trigger_words IS DISTINCT FROM NEW.trigger_words "
            "OR OLD.registered_by_user_id IS DISTINCT FROM NEW.registered_by_user_id "
            "OR OLD.created_at IS DISTINCT FROM NEW.created_at THEN "
            "RAISE EXCEPTION 'managed LoRA identity is immutable'; END IF; "
            "RETURN NEW; END; $$ LANGUAGE plpgsql"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER managed_lora_artifacts_guard_mutation "
            "BEFORE UPDATE OR DELETE ON managed_lora_artifacts FOR EACH ROW "
            "EXECUTE FUNCTION gen_automation_guard_managed_lora_mutation()"
        )
    )
    op.execute(
        sa.text(
            "CREATE OR REPLACE FUNCTION gen_automation_guard_lora_import_mutation() "
            "RETURNS trigger AS $$ BEGIN IF TG_OP = 'DELETE' THEN "
            "RAISE EXCEPTION 'LoRA import jobs cannot be deleted'; END IF; "
            "IF OLD.id IS DISTINCT FROM NEW.id "
            "OR OLD.source_type IS DISTINCT FROM NEW.source_type "
            "OR OLD.display_name IS DISTINCT FROM NEW.display_name "
            "OR OLD.canonical_source_url IS DISTINCT FROM NEW.canonical_source_url "
            "OR OLD.license_url IS DISTINCT FROM NEW.license_url "
            "OR OLD.commercial_use_attested IS DISTINCT FROM NEW.commercial_use_attested "
            "OR OLD.adult_use_attested IS DISTINCT FROM NEW.adult_use_attested "
            "OR OLD.civitai_model_id IS DISTINCT FROM NEW.civitai_model_id "
            "OR OLD.civitai_version_id IS DISTINCT FROM NEW.civitai_version_id "
            "OR OLD.civitai_file_id IS DISTINCT FROM NEW.civitai_file_id "
            "OR OLD.staging_bucket IS DISTINCT FROM NEW.staging_bucket "
            "OR OLD.staging_object_key IS DISTINCT FROM NEW.staging_object_key "
            "OR (OLD.staging_object_version_id IS DISTINCT FROM NEW.staging_object_version_id "
            "AND NOT (OLD.staging_object_version_id IS NULL "
            "AND NEW.staging_object_version_id IS NOT NULL)) "
            "OR (OLD.staging_object_etag IS DISTINCT FROM NEW.staging_object_etag "
            "AND NOT (OLD.staging_object_etag IS NULL AND NEW.staging_object_etag IS NOT NULL)) "
            "OR (OLD.staging_byte_size IS DISTINCT FROM NEW.staging_byte_size "
            "AND NOT (OLD.staging_byte_size IS NULL AND NEW.staging_byte_size IS NOT NULL)) "
            "OR OLD.target_filename IS DISTINCT FROM NEW.target_filename "
            "OR OLD.expected_sha256 IS DISTINCT FROM NEW.expected_sha256 "
            "OR OLD.expected_byte_size IS DISTINCT FROM NEW.expected_byte_size "
            "OR OLD.expected_metadata IS DISTINCT FROM NEW.expected_metadata "
            "OR OLD.trigger_words IS DISTINCT FROM NEW.trigger_words "
            "OR OLD.max_attempts IS DISTINCT FROM NEW.max_attempts "
            "OR OLD.requested_by_user_id IS DISTINCT FROM NEW.requested_by_user_id "
            "OR OLD.created_at IS DISTINCT FROM NEW.created_at "
            "OR (OLD.result_artifact_id IS DISTINCT FROM NEW.result_artifact_id "
            "AND NOT (OLD.result_artifact_id IS NULL "
            "AND NEW.result_artifact_id IS NOT NULL)) THEN "
            "RAISE EXCEPTION 'LoRA import identity is immutable'; END IF; "
            "RETURN NEW; END; $$ LANGUAGE plpgsql"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER lora_import_jobs_guard_mutation "
            "BEFORE UPDATE OR DELETE ON lora_import_jobs FOR EACH ROW "
            "EXECUTE FUNCTION gen_automation_guard_lora_import_mutation()"
        )
    )


def _drop_catalog_guards(dialect_name: str) -> None:
    if dialect_name == "sqlite":
        for name in (
            "lora_import_jobs_reject_delete",
            "lora_import_jobs_guard_update",
            "managed_lora_artifacts_reject_delete",
            "managed_lora_artifacts_guard_update",
        ):
            op.execute(sa.text(f"DROP TRIGGER IF EXISTS {name}"))
        return
    op.execute(
        sa.text("DROP TRIGGER IF EXISTS lora_import_jobs_guard_mutation ON lora_import_jobs")
    )
    op.execute(
        sa.text(
            "DROP TRIGGER IF EXISTS managed_lora_artifacts_guard_mutation ON managed_lora_artifacts"
        )
    )
    op.execute(sa.text("DROP FUNCTION IF EXISTS gen_automation_guard_lora_import_mutation()"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS gen_automation_guard_managed_lora_mutation()"))
