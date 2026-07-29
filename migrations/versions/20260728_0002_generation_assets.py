"""Create generation job, attempt, asset, and lineage tables.

Revision ID: 20260728_0002
Revises: 20260728_0001
Create Date: 2026-07-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260728_0002"
down_revision: str | None = "20260728_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
generation_state = sa.Enum(
    "queued",
    "claimed",
    "submitting",
    "running",
    "collecting",
    "verifying",
    "succeeded",
    "unknown",
    "retry_wait",
    "failed",
    "dead_letter",
    "cancel_requested",
    "cancelled",
    name="generation_state",
    native_enum=False,
    create_constraint=True,
)
generation_attempt_state = sa.Enum(
    "created",
    "submitting",
    "submitted",
    "running",
    "succeeded",
    "failed",
    "unknown",
    "cancel_requested",
    "cancelled",
    name="generation_attempt_state",
    native_enum=False,
    create_constraint=True,
)
asset_kind = sa.Enum(
    "raw_master",
    "review_proxy",
    "derivative",
    "contact_sheet",
    "archive",
    name="asset_kind",
    native_enum=False,
    create_constraint=True,
)
asset_state = sa.Enum(
    "expected",
    "uploading",
    "verifying",
    "available",
    "quarantined",
    "archived",
    "purge_pending",
    "purged",
    name="asset_state",
    native_enum=False,
    create_constraint=True,
)


def upgrade() -> None:
    op.create_table(
        "generation_jobs",
        sa.Column("release_version_id", sa.Uuid(), nullable=False),
        sa.Column("logical_key", sa.String(length=64), nullable=False),
        sa.Column("parameters", json_type, nullable=False),
        sa.Column("parameters_sha256", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("state", generation_state, nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("expected_output_count", sa.Integer(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("lease_owner", sa.String(length=200), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=100), nullable=True),
        sa.Column("last_error_detail", sa.Text(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
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
        sa.CheckConstraint(
            "expected_output_count > 0",
            name=op.f("ck_generation_jobs_positive_expected_outputs"),
        ),
        sa.CheckConstraint(
            "max_attempts > 0",
            name=op.f("ck_generation_jobs_positive_max_attempts"),
        ),
        sa.ForeignKeyConstraint(
            ["release_version_id"],
            ["release_versions.id"],
            name=op.f("fk_generation_jobs_release_version_id_release_versions"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_generation_jobs")),
        sa.UniqueConstraint(
            "release_version_id",
            "logical_key",
            name=op.f("uq_generation_jobs_release_version_id"),
        ),
    )
    op.create_index(
        op.f("ix_generation_jobs_release_version_id"),
        "generation_jobs",
        ["release_version_id"],
    )
    op.create_table(
        "generation_attempts",
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("provider_external_id", sa.String(length=200), nullable=True),
        sa.Column("state", generation_attempt_state, nullable=False),
        sa.Column("worker_image_digest", sa.String(length=200), nullable=False),
        sa.Column("request_metadata", json_type, nullable=False),
        sa.Column("response_metadata", json_type, nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["generation_jobs.id"],
            name=op.f("fk_generation_attempts_job_id_generation_jobs"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_generation_attempts")),
        sa.UniqueConstraint(
            "job_id",
            "attempt_no",
            name=op.f("uq_generation_attempts_job_id"),
        ),
        sa.UniqueConstraint(
            "provider",
            "provider_external_id",
            name="uq_generation_attempts_provider_external_id",
        ),
    )
    op.create_index(
        op.f("ix_generation_attempts_job_id"),
        "generation_attempts",
        ["job_id"],
    )
    op.create_table(
        "assets",
        sa.Column("release_id", sa.Uuid(), nullable=False),
        sa.Column("generation_job_id", sa.Uuid(), nullable=True),
        sa.Column("output_index", sa.Integer(), nullable=True),
        sa.Column("kind", asset_kind, nullable=False),
        sa.Column("state", asset_state, nullable=False),
        sa.Column("storage_backend", sa.String(length=50), nullable=False),
        sa.Column("storage_bucket", sa.String(length=255), nullable=False),
        sa.Column("staging_object_key", sa.String(length=1024), nullable=True),
        sa.Column("object_key", sa.String(length=1024), nullable=True),
        sa.Column("object_version_id", sa.String(length=1024), nullable=True),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("content_type", sa.String(length=100), nullable=True),
        sa.Column("image_format", sa.String(length=20), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("byte_size", sa.BigInteger(), nullable=True),
        sa.Column("metadata", json_type, nullable=False),
        sa.Column(
            "verification_error_code",
            sa.String(length=100),
            nullable=True,
        ),
        sa.Column("verification_error_detail", sa.Text(), nullable=True),
        sa.Column(
            "verification_lease_owner",
            sa.String(length=200),
            nullable=True,
        ),
        sa.Column(
            "verification_lease_expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("purge_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
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
        sa.CheckConstraint(
            "output_index IS NULL OR output_index >= 0",
            name=op.f("ck_assets_valid_output_index"),
        ),
        sa.CheckConstraint(
            "byte_size IS NULL OR byte_size >= 0",
            name=op.f("ck_assets_valid_byte_size"),
        ),
        sa.CheckConstraint(
            "width IS NULL OR width > 0",
            name=op.f("ck_assets_valid_width"),
        ),
        sa.CheckConstraint(
            "height IS NULL OR height > 0",
            name=op.f("ck_assets_valid_height"),
        ),
        sa.CheckConstraint(
            "kind <> 'raw_master' OR (generation_job_id IS NOT NULL AND output_index IS NOT NULL)",
            name=op.f("ck_assets_raw_master_has_output"),
        ),
        sa.CheckConstraint(
            "state NOT IN ('uploading', 'verifying') OR staging_object_key IS NOT NULL",
            name=op.f("ck_assets_active_upload_has_staging_key"),
        ),
        sa.CheckConstraint(
            "state <> 'available' OR "
            "(object_key IS NOT NULL AND sha256 IS NOT NULL "
            "AND content_type IS NOT NULL AND image_format IS NOT NULL "
            "AND width IS NOT NULL AND height IS NOT NULL AND byte_size IS NOT NULL)",
            name=op.f("ck_assets_available_asset_complete"),
        ),
        sa.ForeignKeyConstraint(
            ["generation_job_id"],
            ["generation_jobs.id"],
            name=op.f("fk_assets_generation_job_id_generation_jobs"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["release_id"],
            ["releases.id"],
            name=op.f("fk_assets_release_id_releases"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_assets")),
        sa.UniqueConstraint(
            "generation_job_id",
            "output_index",
            "kind",
            name=op.f("uq_assets_generation_job_id"),
        ),
        sa.UniqueConstraint(
            "storage_backend",
            "storage_bucket",
            "object_key",
            name="uq_assets_master_object",
        ),
        sa.UniqueConstraint(
            "storage_backend",
            "storage_bucket",
            "staging_object_key",
            name="uq_assets_staging_object",
        ),
    )
    op.create_index(
        op.f("ix_assets_generation_job_id"),
        "assets",
        ["generation_job_id"],
    )
    op.create_index(
        op.f("ix_assets_release_id"),
        "assets",
        ["release_id"],
    )
    op.create_index(op.f("ix_assets_sha256"), "assets", ["sha256"])
    op.create_table(
        "asset_lineage",
        sa.Column("parent_asset_id", sa.Uuid(), nullable=False),
        sa.Column("child_asset_id", sa.Uuid(), nullable=False),
        sa.Column("relation", sa.String(length=50), nullable=False),
        sa.Column("recipe_version", sa.String(length=100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "parent_asset_id <> child_asset_id",
            name=op.f("ck_asset_lineage_not_self_referential"),
        ),
        sa.ForeignKeyConstraint(
            ["child_asset_id"],
            ["assets.id"],
            name=op.f("fk_asset_lineage_child_asset_id_assets"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["parent_asset_id"],
            ["assets.id"],
            name=op.f("fk_asset_lineage_parent_asset_id_assets"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_asset_lineage")),
        sa.UniqueConstraint(
            "parent_asset_id",
            "child_asset_id",
            "relation",
            "recipe_version",
            name=op.f("uq_asset_lineage_parent_asset_id"),
        ),
    )
    op.create_index(
        op.f("ix_asset_lineage_child_asset_id"),
        "asset_lineage",
        ["child_asset_id"],
    )
    op.create_index(
        op.f("ix_asset_lineage_parent_asset_id"),
        "asset_lineage",
        ["parent_asset_id"],
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_asset_lineage_parent_asset_id"),
        table_name="asset_lineage",
    )
    op.drop_index(
        op.f("ix_asset_lineage_child_asset_id"),
        table_name="asset_lineage",
    )
    op.drop_table("asset_lineage")
    op.drop_index(op.f("ix_assets_sha256"), table_name="assets")
    op.drop_index(op.f("ix_assets_release_id"), table_name="assets")
    op.drop_index(op.f("ix_assets_generation_job_id"), table_name="assets")
    op.drop_table("assets")
    op.drop_index(
        op.f("ix_generation_attempts_job_id"),
        table_name="generation_attempts",
    )
    op.drop_table("generation_attempts")
    op.drop_index(
        op.f("ix_generation_jobs_release_version_id"),
        table_name="generation_jobs",
    )
    op.drop_table("generation_jobs")
