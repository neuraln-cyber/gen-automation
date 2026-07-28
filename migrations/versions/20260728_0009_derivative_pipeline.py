"""Freeze release selections and add durable derivative planning.

Revision ID: 20260728_0009
Revises: 20260728_0008
Create Date: 2026-07-28
"""

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any
from uuid import UUID, uuid5

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260728_0009"
down_revision: str | None = "20260728_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
derivative_job_state = sa.Enum(
    "requested",
    "claimed",
    "processing",
    "retry_wait",
    "succeeded",
    "failed",
    "cancelled",
    name="derivative_job_state",
    native_enum=False,
    create_constraint=True,
)
_SELECTION_NAMESPACE = UUID("9b9beff8-3ab2-4ca1-b9fc-b84df8f10d1d")


def upgrade() -> None:
    _create_release_selections()
    _create_derivative_recipes()
    _create_derivative_jobs()
    _create_derivative_outputs()
    _backfill_completed_selections(op.get_bind())
    _create_guards(op.get_bind().dialect.name)


def downgrade() -> None:
    _drop_guards(op.get_bind().dialect.name)
    op.drop_index(
        "ix_derivative_outputs_selection_target",
        table_name="derivative_outputs",
    )
    op.drop_index(
        op.f("ix_derivative_outputs_asset_id"),
        table_name="derivative_outputs",
    )
    op.drop_index(
        op.f("ix_derivative_outputs_derivative_job_id"),
        table_name="derivative_outputs",
    )
    op.drop_table("derivative_outputs")

    op.drop_index(
        "ix_derivative_jobs_claim",
        table_name="derivative_jobs",
    )
    op.drop_index(
        op.f("ix_derivative_jobs_release_version_id"),
        table_name="derivative_jobs",
    )
    op.drop_index(
        op.f("ix_derivative_jobs_derivative_recipe_id"),
        table_name="derivative_jobs",
    )
    op.drop_index(
        op.f("ix_derivative_jobs_release_selection_id"),
        table_name="derivative_jobs",
    )
    op.drop_table("derivative_jobs")

    op.drop_index(
        "ix_derivative_recipes_release_created",
        table_name="derivative_recipes",
    )
    op.drop_index(
        op.f("ix_derivative_recipes_release_version_id"),
        table_name="derivative_recipes",
    )
    op.drop_table("derivative_recipes")

    op.drop_index(
        "ix_release_selections_release_version_display",
        table_name="release_selections",
    )
    op.drop_index(
        op.f("ix_release_selections_asset_id"),
        table_name="release_selections",
    )
    op.drop_index(
        op.f("ix_release_selections_release_version_id"),
        table_name="release_selections",
    )
    op.drop_index(
        op.f("ix_release_selections_review_task_id"),
        table_name="release_selections",
    )
    op.drop_table("release_selections")


def _create_release_selections() -> None:
    op.create_table(
        "release_selections",
        sa.Column("review_task_id", sa.Uuid(), nullable=False),
        sa.Column("scoring_run_id", sa.Uuid(), nullable=False),
        sa.Column("review_decision_id", sa.Uuid(), nullable=False),
        sa.Column("decision_revision", sa.Integer(), nullable=False),
        sa.Column("release_version_id", sa.Uuid(), nullable=False),
        sa.Column("asset_id", sa.Uuid(), nullable=False),
        sa.Column("ranking_rank", sa.Integer(), nullable=False),
        sa.Column("display_order", sa.Integer(), nullable=False),
        sa.Column("ranking_manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column("source_storage_backend", sa.String(length=50), nullable=False),
        sa.Column("source_storage_bucket", sa.String(length=255), nullable=False),
        sa.Column("source_object_key", sa.String(length=1024), nullable=False),
        sa.Column(
            "source_object_version_id",
            sa.String(length=1024),
            nullable=False,
        ),
        sa.Column("source_sha256", sa.String(length=64), nullable=False),
        sa.Column("source_content_type", sa.String(length=100), nullable=False),
        sa.Column("source_image_format", sa.String(length=20), nullable=False),
        sa.Column("source_width", sa.Integer(), nullable=False),
        sa.Column("source_height", sa.Integer(), nullable=False),
        sa.Column("source_byte_size", sa.BigInteger(), nullable=False),
        sa.Column(
            "source_available_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("frozen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "decision_revision > 0",
            name=op.f("ck_release_selections_positive_decision_revision"),
        ),
        sa.CheckConstraint(
            "ranking_rank > 0",
            name=op.f("ck_release_selections_positive_ranking_rank"),
        ),
        sa.CheckConstraint(
            "display_order > 0",
            name=op.f("ck_release_selections_positive_display_order"),
        ),
        sa.CheckConstraint(
            "length(ranking_manifest_sha256) = 64",
            name=op.f("ck_release_selections_valid_ranking_manifest_sha256"),
        ),
        sa.CheckConstraint(
            "length(source_sha256) = 64",
            name=op.f("ck_release_selections_valid_source_sha256"),
        ),
        sa.CheckConstraint(
            "length(trim(source_storage_backend)) > 0 "
            "AND length(trim(source_storage_bucket)) > 0 "
            "AND length(trim(source_object_key)) > 0 "
            "AND length(trim(source_object_version_id)) > 0 "
            "AND length(trim(source_content_type)) > 0 "
            "AND length(trim(source_image_format)) > 0",
            name=op.f("ck_release_selections_complete_source_storage_identity"),
        ),
        sa.CheckConstraint(
            "source_width > 0 AND source_height > 0 AND source_byte_size > 0",
            name=op.f("ck_release_selections_positive_source_dimensions"),
        ),
        sa.CheckConstraint(
            "frozen_at >= source_available_at",
            name=op.f("ck_release_selections_frozen_after_source_available"),
        ),
        sa.ForeignKeyConstraint(
            ["asset_id"],
            ["assets.id"],
            name=op.f("fk_release_selections_asset_id_assets"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["release_version_id"],
            ["release_versions.id"],
            name=op.f("fk_release_selections_release_version_id_release_versions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["review_decision_id"],
            ["review_decisions.id"],
            name=op.f("fk_release_selections_review_decision_id_review_decisions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["review_task_id"],
            ["review_tasks.id"],
            name=op.f("fk_release_selections_review_task_id_review_tasks"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["review_task_id", "scoring_run_id"],
            ["review_tasks.id", "review_tasks.scoring_run_id"],
            name="fk_release_selections_task_scoring_run",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            [
                "review_task_id",
                "asset_id",
                "decision_revision",
                "review_decision_id",
            ],
            [
                "review_decisions.review_task_id",
                "review_decisions.asset_id",
                "review_decisions.revision",
                "review_decisions.id",
            ],
            name="fk_release_selections_review_decision",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["scoring_run_id", "asset_id"],
            ["asset_rankings.scoring_run_id", "asset_rankings.asset_id"],
            name="fk_release_selections_ranking_membership",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_release_selections")),
        sa.UniqueConstraint(
            "review_task_id",
            "asset_id",
            name="uq_release_selections_task_asset",
        ),
        sa.UniqueConstraint(
            "review_task_id",
            "display_order",
            name="uq_release_selections_task_display_order",
        ),
        sa.UniqueConstraint(
            "review_decision_id",
            name="uq_release_selections_review_decision",
        ),
        sa.UniqueConstraint(
            "id",
            "asset_id",
            name="uq_release_selections_id_asset",
        ),
        sa.UniqueConstraint(
            "id",
            "release_version_id",
            name="uq_release_selections_id_release_version",
        ),
    )
    op.create_index(
        op.f("ix_release_selections_review_task_id"),
        "release_selections",
        ["review_task_id"],
    )
    op.create_index(
        op.f("ix_release_selections_release_version_id"),
        "release_selections",
        ["release_version_id"],
    )
    op.create_index(
        op.f("ix_release_selections_asset_id"),
        "release_selections",
        ["asset_id"],
    )
    op.create_index(
        "ix_release_selections_release_version_display",
        "release_selections",
        ["release_version_id", "display_order"],
    )


def _create_derivative_recipes() -> None:
    op.create_table(
        "derivative_recipes",
        sa.Column("release_version_id", sa.Uuid(), nullable=False),
        sa.Column("logical_key", sa.String(length=64), nullable=False),
        sa.Column("recipe_version", sa.Integer(), nullable=False),
        sa.Column("configuration", json_type, nullable=False),
        sa.Column("config_sha256", sa.String(length=64), nullable=False),
        sa.Column("output_targets", json_type, nullable=False),
        sa.Column("expected_output_count", sa.Integer(), nullable=False),
        sa.Column("renderer_version", sa.String(length=100), nullable=False),
        sa.Column("pillow_version", sa.String(length=50), nullable=False),
        sa.Column("watermark_asset_id", sa.Uuid(), nullable=True),
        sa.Column("watermark_storage_backend", sa.String(length=50), nullable=True),
        sa.Column("watermark_storage_bucket", sa.String(length=255), nullable=True),
        sa.Column("watermark_object_key", sa.String(length=1024), nullable=True),
        sa.Column(
            "watermark_object_version_id",
            sa.String(length=1024),
            nullable=True,
        ),
        sa.Column("watermark_sha256", sa.String(length=64), nullable=True),
        sa.Column("watermark_content_type", sa.String(length=100), nullable=True),
        sa.Column("watermark_image_format", sa.String(length=20), nullable=True),
        sa.Column("watermark_width", sa.Integer(), nullable=True),
        sa.Column("watermark_height", sa.Integer(), nullable=True),
        sa.Column("watermark_byte_size", sa.BigInteger(), nullable=True),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approved_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "recipe_version > 0",
            name=op.f("ck_derivative_recipes_positive_recipe_version"),
        ),
        sa.CheckConstraint(
            "expected_output_count > 0",
            name=op.f("ck_derivative_recipes_positive_expected_output_count"),
        ),
        sa.CheckConstraint(
            "length(logical_key) = 64",
            name=op.f("ck_derivative_recipes_valid_logical_key"),
        ),
        sa.CheckConstraint(
            "length(config_sha256) = 64",
            name=op.f("ck_derivative_recipes_valid_config_sha256"),
        ),
        sa.CheckConstraint(
            "(watermark_asset_id IS NULL "
            "AND watermark_storage_backend IS NULL "
            "AND watermark_storage_bucket IS NULL "
            "AND watermark_object_key IS NULL "
            "AND watermark_object_version_id IS NULL "
            "AND watermark_sha256 IS NULL "
            "AND watermark_content_type IS NULL "
            "AND watermark_image_format IS NULL "
            "AND watermark_width IS NULL "
            "AND watermark_height IS NULL "
            "AND watermark_byte_size IS NULL) "
            "OR (watermark_asset_id IS NOT NULL "
            "AND watermark_storage_backend IS NOT NULL "
            "AND watermark_storage_bucket IS NOT NULL "
            "AND watermark_object_key IS NOT NULL "
            "AND watermark_object_version_id IS NOT NULL "
            "AND watermark_sha256 IS NOT NULL "
            "AND length(watermark_sha256) = 64 "
            "AND watermark_content_type IS NOT NULL "
            "AND watermark_image_format IS NOT NULL "
            "AND watermark_width > 0 "
            "AND watermark_height > 0 "
            "AND watermark_byte_size > 0)",
            name=op.f("ck_derivative_recipes_watermark_snapshot_pair"),
        ),
        sa.CheckConstraint(
            "approved_at >= created_at",
            name=op.f("ck_derivative_recipes_approval_after_creation"),
        ),
        sa.ForeignKeyConstraint(
            ["approved_by_user_id"],
            ["admin_users.id"],
            name=op.f("fk_derivative_recipes_approved_by_user_id_admin_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["admin_users.id"],
            name=op.f("fk_derivative_recipes_created_by_user_id_admin_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["release_version_id"],
            ["release_versions.id"],
            name=op.f("fk_derivative_recipes_release_version_id_release_versions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["watermark_asset_id"],
            ["assets.id"],
            name=op.f("fk_derivative_recipes_watermark_asset_id_assets"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_derivative_recipes")),
        sa.UniqueConstraint(
            "logical_key",
            name="uq_derivative_recipes_logical_key",
        ),
        sa.UniqueConstraint(
            "id",
            "release_version_id",
            "expected_output_count",
            name="uq_derivative_recipes_id_release_outputs",
        ),
    )
    op.create_index(
        op.f("ix_derivative_recipes_release_version_id"),
        "derivative_recipes",
        ["release_version_id"],
    )
    op.create_index(
        "ix_derivative_recipes_release_created",
        "derivative_recipes",
        ["release_version_id", "created_at"],
    )


def _create_derivative_jobs() -> None:
    op.create_table(
        "derivative_jobs",
        sa.Column("release_selection_id", sa.Uuid(), nullable=False),
        sa.Column("derivative_recipe_id", sa.Uuid(), nullable=False),
        sa.Column("release_version_id", sa.Uuid(), nullable=False),
        sa.Column("logical_key", sa.String(length=64), nullable=False),
        sa.Column("request_payload", json_type, nullable=False),
        sa.Column("request_sha256", sa.String(length=64), nullable=False),
        sa.Column("expected_output_count", sa.Integer(), nullable=False),
        sa.Column("state", derivative_job_state, nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("lock_version", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_owner", sa.String(length=200), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "processing_started_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=100), nullable=True),
        sa.Column("last_error_detail", sa.Text(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "length(logical_key) = 64",
            name=op.f("ck_derivative_jobs_valid_logical_key"),
        ),
        sa.CheckConstraint(
            "length(request_sha256) = 64",
            name=op.f("ck_derivative_jobs_valid_request_sha256"),
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name=op.f("ck_derivative_jobs_nonnegative_attempt_count"),
        ),
        sa.CheckConstraint(
            "max_attempts > 0",
            name=op.f("ck_derivative_jobs_positive_max_attempts"),
        ),
        sa.CheckConstraint(
            "attempt_count <= max_attempts",
            name=op.f("ck_derivative_jobs_attempts_within_limit"),
        ),
        sa.CheckConstraint(
            "expected_output_count > 0",
            name=op.f("ck_derivative_jobs_positive_output_count"),
        ),
        sa.CheckConstraint(
            "lock_version > 0",
            name=op.f("ck_derivative_jobs_positive_lock_version"),
        ),
        sa.CheckConstraint(
            "(state IN ('claimed', 'processing') "
            "AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL) "
            "OR (state NOT IN ('claimed', 'processing') "
            "AND lease_owner IS NULL AND lease_expires_at IS NULL)",
            name=op.f("ck_derivative_jobs_lease_state"),
        ),
        sa.CheckConstraint(
            "(state = 'retry_wait' AND retry_at IS NOT NULL) "
            "OR (state <> 'retry_wait' AND retry_at IS NULL)",
            name=op.f("ck_derivative_jobs_retry_state"),
        ),
        sa.CheckConstraint(
            "(state IN ('succeeded', 'failed', 'cancelled') "
            "AND completed_at IS NOT NULL) "
            "OR (state NOT IN ('succeeded', 'failed', 'cancelled') "
            "AND completed_at IS NULL)",
            name=op.f("ck_derivative_jobs_terminal_state"),
        ),
        sa.CheckConstraint(
            "state <> 'requested' OR attempt_count = 0",
            name=op.f("ck_derivative_jobs_requested_has_no_attempt"),
        ),
        sa.CheckConstraint(
            "state NOT IN ('claimed', 'processing') OR attempt_count > 0",
            name=op.f("ck_derivative_jobs_active_has_attempt"),
        ),
        sa.CheckConstraint(
            "claimed_at IS NULL OR claimed_at >= requested_at",
            name=op.f("ck_derivative_jobs_valid_claim_time"),
        ),
        sa.CheckConstraint(
            "processing_started_at IS NULL OR processing_started_at >= requested_at",
            name=op.f("ck_derivative_jobs_valid_processing_time"),
        ),
        sa.CheckConstraint(
            "completed_at IS NULL OR completed_at >= requested_at",
            name=op.f("ck_derivative_jobs_valid_completion_time"),
        ),
        sa.ForeignKeyConstraint(
            ["derivative_recipe_id"],
            ["derivative_recipes.id"],
            name=op.f("fk_derivative_jobs_derivative_recipe_id_derivative_recipes"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["release_selection_id"],
            ["release_selections.id"],
            name=op.f("fk_derivative_jobs_release_selection_id_release_selections"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["release_version_id"],
            ["release_versions.id"],
            name=op.f("fk_derivative_jobs_release_version_id_release_versions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["release_selection_id", "release_version_id"],
            ["release_selections.id", "release_selections.release_version_id"],
            name="fk_derivative_jobs_selection_release_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            [
                "derivative_recipe_id",
                "release_version_id",
                "expected_output_count",
            ],
            [
                "derivative_recipes.id",
                "derivative_recipes.release_version_id",
                "derivative_recipes.expected_output_count",
            ],
            name="fk_derivative_jobs_recipe_release_outputs",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_derivative_jobs")),
        sa.UniqueConstraint(
            "logical_key",
            name="uq_derivative_jobs_logical_key",
        ),
        sa.UniqueConstraint(
            "release_selection_id",
            "derivative_recipe_id",
            name="uq_derivative_jobs_selection_recipe",
        ),
        sa.UniqueConstraint(
            "id",
            "release_selection_id",
            "derivative_recipe_id",
            name="uq_derivative_jobs_id_selection_recipe",
        ),
    )
    op.create_index(
        op.f("ix_derivative_jobs_release_selection_id"),
        "derivative_jobs",
        ["release_selection_id"],
    )
    op.create_index(
        op.f("ix_derivative_jobs_derivative_recipe_id"),
        "derivative_jobs",
        ["derivative_recipe_id"],
    )
    op.create_index(
        op.f("ix_derivative_jobs_release_version_id"),
        "derivative_jobs",
        ["release_version_id"],
    )
    op.create_index(
        "ix_derivative_jobs_claim",
        "derivative_jobs",
        [
            "state",
            "retry_at",
            "lease_expires_at",
            "priority",
            "requested_at",
        ],
    )


def _create_derivative_outputs() -> None:
    op.create_table(
        "derivative_outputs",
        sa.Column("derivative_job_id", sa.Uuid(), nullable=False),
        sa.Column("release_selection_id", sa.Uuid(), nullable=False),
        sa.Column("derivative_recipe_id", sa.Uuid(), nullable=False),
        sa.Column("target", sa.String(length=50), nullable=False),
        sa.Column("asset_id", sa.Uuid(), nullable=False),
        sa.Column("source_asset_id", sa.Uuid(), nullable=False),
        sa.Column("asset_lineage_id", sa.Uuid(), nullable=False),
        sa.Column("asset_storage_backend", sa.String(length=50), nullable=False),
        sa.Column("asset_storage_bucket", sa.String(length=255), nullable=False),
        sa.Column("asset_object_key", sa.String(length=1024), nullable=False),
        sa.Column(
            "asset_object_version_id",
            sa.String(length=1024),
            nullable=False,
        ),
        sa.Column("asset_sha256", sa.String(length=64), nullable=False),
        sa.Column("asset_content_type", sa.String(length=100), nullable=False),
        sa.Column("asset_image_format", sa.String(length=20), nullable=False),
        sa.Column("asset_width", sa.Integer(), nullable=False),
        sa.Column("asset_height", sa.Integer(), nullable=False),
        sa.Column("asset_byte_size", sa.BigInteger(), nullable=False),
        sa.Column("lineage_relation", sa.String(length=50), nullable=False),
        sa.Column("lineage_recipe_version", sa.String(length=100), nullable=False),
        sa.Column("recorded_by", sa.String(length=200), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "length(trim(target)) > 0",
            name=op.f("ck_derivative_outputs_nonempty_target"),
        ),
        sa.CheckConstraint(
            "length(asset_sha256) = 64",
            name=op.f("ck_derivative_outputs_valid_asset_sha256"),
        ),
        sa.CheckConstraint(
            "length(trim(asset_storage_backend)) > 0 "
            "AND length(trim(asset_storage_bucket)) > 0 "
            "AND length(trim(asset_object_key)) > 0 "
            "AND length(trim(asset_object_version_id)) > 0 "
            "AND length(trim(asset_content_type)) > 0 "
            "AND length(trim(asset_image_format)) > 0",
            name=op.f("ck_derivative_outputs_complete_asset_storage_identity"),
        ),
        sa.CheckConstraint(
            "asset_width > 0 AND asset_height > 0 AND asset_byte_size > 0",
            name=op.f("ck_derivative_outputs_positive_asset_dimensions"),
        ),
        sa.CheckConstraint(
            "length(trim(lineage_relation)) > 0 AND length(trim(lineage_recipe_version)) > 0",
            name=op.f("ck_derivative_outputs_complete_lineage_identity"),
        ),
        sa.ForeignKeyConstraint(
            ["asset_id"],
            ["assets.id"],
            name=op.f("fk_derivative_outputs_asset_id_assets"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["asset_lineage_id"],
            ["asset_lineage.id"],
            name=op.f("fk_derivative_outputs_asset_lineage_id_asset_lineage"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["derivative_job_id"],
            ["derivative_jobs.id"],
            name=op.f("fk_derivative_outputs_derivative_job_id_derivative_jobs"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_asset_id"],
            ["assets.id"],
            name=op.f("fk_derivative_outputs_source_asset_id_assets"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            [
                "derivative_job_id",
                "release_selection_id",
                "derivative_recipe_id",
            ],
            [
                "derivative_jobs.id",
                "derivative_jobs.release_selection_id",
                "derivative_jobs.derivative_recipe_id",
            ],
            name="fk_derivative_outputs_job_identity",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["release_selection_id", "source_asset_id"],
            ["release_selections.id", "release_selections.asset_id"],
            name="fk_derivative_outputs_selection_source",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_derivative_outputs")),
        sa.UniqueConstraint(
            "derivative_job_id",
            "target",
            name="uq_derivative_outputs_job_target",
        ),
        sa.UniqueConstraint(
            "asset_id",
            name="uq_derivative_outputs_asset",
        ),
    )
    op.create_index(
        op.f("ix_derivative_outputs_derivative_job_id"),
        "derivative_outputs",
        ["derivative_job_id"],
    )
    op.create_index(
        op.f("ix_derivative_outputs_asset_id"),
        "derivative_outputs",
        ["asset_id"],
    )
    op.create_index(
        "ix_derivative_outputs_selection_target",
        "derivative_outputs",
        ["release_selection_id", "target"],
    )


def _backfill_completed_selections(bind: sa.Connection) -> None:
    tasks = sa.table(
        "review_tasks",
        sa.column("id", sa.Uuid()),
        sa.column("release_version_id", sa.Uuid()),
        sa.column("scoring_run_id", sa.Uuid()),
        sa.column("ranking_manifest_sha256", sa.String()),
        sa.column("desired_accepted_count", sa.Integer()),
        sa.column("state", sa.String()),
        sa.column("completed_at", sa.DateTime(timezone=True)),
    )
    decisions = sa.table(
        "review_decisions",
        sa.column("id", sa.Uuid()),
        sa.column("review_task_id", sa.Uuid()),
        sa.column("scoring_run_id", sa.Uuid()),
        sa.column("asset_id", sa.Uuid()),
        sa.column("revision", sa.Integer()),
        sa.column("decision", sa.String()),
    )
    rankings = sa.table(
        "asset_rankings",
        sa.column("scoring_run_id", sa.Uuid()),
        sa.column("asset_id", sa.Uuid()),
        sa.column("rank", sa.Integer()),
    )
    versions = sa.table(
        "release_versions",
        sa.column("id", sa.Uuid()),
        sa.column("release_id", sa.Uuid()),
        sa.column("version_no", sa.Integer()),
    )
    releases = sa.table(
        "releases",
        sa.column("id", sa.Uuid()),
        sa.column("phase", sa.String()),
        sa.column("current_version_no", sa.Integer()),
        sa.column("lock_version", sa.Integer()),
    )
    assets = sa.table(
        "assets",
        sa.column("id", sa.Uuid()),
        sa.column("release_id", sa.Uuid()),
        sa.column("kind", sa.String()),
        sa.column("state", sa.String()),
        sa.column("storage_backend", sa.String()),
        sa.column("storage_bucket", sa.String()),
        sa.column("object_key", sa.String()),
        sa.column("object_version_id", sa.String()),
        sa.column("sha256", sa.String()),
        sa.column("content_type", sa.String()),
        sa.column("image_format", sa.String()),
        sa.column("width", sa.Integer()),
        sa.column("height", sa.Integer()),
        sa.column("byte_size", sa.BigInteger()),
        sa.column("available_at", sa.DateTime(timezone=True)),
    )
    selections = sa.table(
        "release_selections",
        sa.column("id", sa.Uuid()),
        sa.column("review_task_id", sa.Uuid()),
        sa.column("scoring_run_id", sa.Uuid()),
        sa.column("review_decision_id", sa.Uuid()),
        sa.column("decision_revision", sa.Integer()),
        sa.column("release_version_id", sa.Uuid()),
        sa.column("asset_id", sa.Uuid()),
        sa.column("ranking_rank", sa.Integer()),
        sa.column("display_order", sa.Integer()),
        sa.column("ranking_manifest_sha256", sa.String()),
        sa.column("source_storage_backend", sa.String()),
        sa.column("source_storage_bucket", sa.String()),
        sa.column("source_object_key", sa.String()),
        sa.column("source_object_version_id", sa.String()),
        sa.column("source_sha256", sa.String()),
        sa.column("source_content_type", sa.String()),
        sa.column("source_image_format", sa.String()),
        sa.column("source_width", sa.Integer()),
        sa.column("source_height", sa.Integer()),
        sa.column("source_byte_size", sa.BigInteger()),
        sa.column("source_available_at", sa.DateTime(timezone=True)),
        sa.column("frozen_at", sa.DateTime(timezone=True)),
    )

    completed_tasks = list(
        bind.execute(
            sa.select(tasks).where(tasks.c.state == "completed").order_by(tasks.c.id)
        ).mappings()
    )
    for task in completed_tasks:
        if task["completed_at"] is None:
            raise RuntimeError("revision 0009 found a completed review task without a timestamp")
        version = (
            bind.execute(sa.select(versions).where(versions.c.id == task["release_version_id"]))
            .mappings()
            .one_or_none()
        )
        if version is None:
            raise RuntimeError("revision 0009 found a review task without a release version")
        latest_revisions = (
            sa.select(
                decisions.c.asset_id.label("asset_id"),
                sa.func.max(decisions.c.revision).label("revision"),
            )
            .where(decisions.c.review_task_id == task["id"])
            .group_by(decisions.c.asset_id)
            .subquery()
        )
        rows = list(
            bind.execute(
                sa.select(
                    decisions.c.id.label("decision_id"),
                    decisions.c.scoring_run_id.label("decision_scoring_run_id"),
                    decisions.c.asset_id.label("decision_asset_id"),
                    decisions.c.revision.label("decision_revision"),
                    decisions.c.decision.label("decision_value"),
                    rankings.c.rank.label("ranking_rank"),
                    *[column.label(f"asset_{column.name}") for column in assets.c],
                )
                .join(
                    latest_revisions,
                    (latest_revisions.c.asset_id == decisions.c.asset_id)
                    & (latest_revisions.c.revision == decisions.c.revision),
                )
                .join(
                    rankings,
                    (rankings.c.scoring_run_id == task["scoring_run_id"])
                    & (rankings.c.asset_id == decisions.c.asset_id),
                )
                .join(assets, assets.c.id == decisions.c.asset_id)
                .where(
                    decisions.c.review_task_id == task["id"],
                    decisions.c.decision == "accept",
                )
                .order_by(rankings.c.rank, decisions.c.asset_id)
            ).mappings()
        )
        if len(rows) != task["desired_accepted_count"]:
            raise RuntimeError("revision 0009 cannot freeze an invalid completed review target")
        values: list[dict[str, Any]] = []
        for display_order, row in enumerate(rows, start=1):
            _validate_backfill_source(task, version, row)
            selection_id = uuid5(
                _SELECTION_NAMESPACE,
                f"{task['id']}:{row['decision_id']}",
            )
            values.append(
                {
                    "id": selection_id,
                    "review_task_id": task["id"],
                    "scoring_run_id": task["scoring_run_id"],
                    "review_decision_id": row["decision_id"],
                    "decision_revision": row["decision_revision"],
                    "release_version_id": task["release_version_id"],
                    "asset_id": row["decision_asset_id"],
                    "ranking_rank": row["ranking_rank"],
                    "display_order": display_order,
                    "ranking_manifest_sha256": task["ranking_manifest_sha256"],
                    "source_storage_backend": row["asset_storage_backend"],
                    "source_storage_bucket": row["asset_storage_bucket"],
                    "source_object_key": row["asset_object_key"],
                    "source_object_version_id": row["asset_object_version_id"],
                    "source_sha256": row["asset_sha256"],
                    "source_content_type": row["asset_content_type"],
                    "source_image_format": row["asset_image_format"],
                    "source_width": row["asset_width"],
                    "source_height": row["asset_height"],
                    "source_byte_size": row["asset_byte_size"],
                    "source_available_at": row["asset_available_at"],
                    "frozen_at": task["completed_at"],
                }
            )
        if values:
            bind.execute(selections.insert(), values)

        release = (
            bind.execute(sa.select(releases).where(releases.c.id == version["release_id"]))
            .mappings()
            .one_or_none()
        )
        if (
            release is not None
            and release["current_version_no"] == version["version_no"]
            and release["phase"] == "reviewing"
        ):
            bind.execute(
                releases.update()
                .where(
                    releases.c.id == release["id"],
                    releases.c.phase == "reviewing",
                    releases.c.lock_version == release["lock_version"],
                )
                .values(
                    phase="approved",
                    lock_version=releases.c.lock_version + 1,
                )
            )


def _validate_backfill_source(
    task: Mapping[str, Any],
    version: Mapping[str, Any],
    row: Mapping[str, Any],
) -> None:
    available_at = row["asset_available_at"]
    completed_at = task["completed_at"]
    if (
        row["decision_scoring_run_id"] != task["scoring_run_id"]
        or row["asset_release_id"] != version["release_id"]
        or row["asset_kind"] != "raw_master"
        or row["asset_state"] != "available"
        or not _nonempty(row["asset_storage_backend"])
        or not _nonempty(row["asset_storage_bucket"])
        or not _nonempty(row["asset_object_key"])
        or not _nonempty(row["asset_object_version_id"])
        or not _sha256(row["asset_sha256"])
        or not _nonempty(row["asset_content_type"])
        or not _nonempty(row["asset_image_format"])
        or not _positive(row["asset_width"])
        or not _positive(row["asset_height"])
        or not _positive(row["asset_byte_size"])
        or available_at is None
        or not isinstance(completed_at, datetime)
        or not isinstance(available_at, datetime)
        or available_at > completed_at
    ):
        raise RuntimeError("revision 0009 cannot freeze an unavailable accepted raw master")


def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _positive(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _create_guards(dialect: str) -> None:
    if dialect == "sqlite":
        _create_sqlite_guards()
    elif dialect == "postgresql":
        _create_postgresql_guards()


def _drop_guards(dialect: str) -> None:
    if dialect == "sqlite":
        for trigger in _sqlite_trigger_names():
            op.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    elif dialect == "postgresql":
        for trigger, table in _postgresql_triggers():
            op.execute(f"DROP TRIGGER IF EXISTS {trigger} ON {table}")
        for function in _postgresql_functions():
            op.execute(f"DROP FUNCTION IF EXISTS {function}()")


def _create_sqlite_guards() -> None:
    for statement in _sqlite_guard_statements():
        op.execute(statement)


def _create_postgresql_guards() -> None:
    for statement in _postgresql_function_statements():
        op.execute(statement)
    for trigger, table, timing, function in _postgresql_trigger_specs():
        op.execute(
            f"CREATE TRIGGER {trigger} {timing} ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION {function}()"
        )


def _sqlite_guard_statements() -> tuple[str, ...]:
    return (
        """
        CREATE TRIGGER review_decisions_guard_late_insert
        BEFORE INSERT ON review_decisions
        WHEN NOT EXISTS (
            SELECT 1
            FROM review_tasks
            WHERE id = NEW.review_task_id
              AND scoring_run_id = NEW.scoring_run_id
              AND state = 'open'
        )
        BEGIN
            SELECT RAISE(
                ABORT,
                'terminal review tasks reject new decisions'
            );
        END
        """,
        """
        CREATE TRIGGER release_selections_guard_insert
        BEFORE INSERT ON release_selections
        BEGIN
            SELECT CASE WHEN NOT EXISTS (
                SELECT 1
                FROM review_tasks AS task
                JOIN release_versions AS version
                  ON version.id = task.release_version_id
                JOIN review_decisions AS decision
                  ON decision.id = NEW.review_decision_id
                 AND decision.review_task_id = task.id
                 AND decision.asset_id = NEW.asset_id
                 AND decision.revision = NEW.decision_revision
                 AND decision.scoring_run_id = task.scoring_run_id
                JOIN asset_rankings AS ranking
                  ON ranking.scoring_run_id = task.scoring_run_id
                 AND ranking.asset_id = NEW.asset_id
                JOIN assets AS asset
                  ON asset.id = NEW.asset_id
                WHERE task.id = NEW.review_task_id
                  AND task.state = 'open'
                  AND task.scoring_run_id = NEW.scoring_run_id
                  AND task.release_version_id = NEW.release_version_id
                  AND task.ranking_manifest_sha256 =
                      NEW.ranking_manifest_sha256
                  AND decision.decision = 'accept'
                  AND NOT EXISTS (
                      SELECT 1
                      FROM review_decisions AS newer
                      WHERE newer.review_task_id =
                            decision.review_task_id
                        AND newer.asset_id = decision.asset_id
                        AND newer.revision > decision.revision
                  )
                  AND ranking.rank = NEW.ranking_rank
                  AND asset.release_id = version.release_id
                  AND asset.kind = 'raw_master'
                  AND asset.state = 'available'
                  AND asset.storage_backend =
                      NEW.source_storage_backend
                  AND asset.storage_bucket =
                      NEW.source_storage_bucket
                  AND asset.object_key = NEW.source_object_key
                  AND asset.object_version_id =
                      NEW.source_object_version_id
                  AND asset.sha256 = NEW.source_sha256
                  AND asset.content_type = NEW.source_content_type
                  AND asset.image_format = NEW.source_image_format
                  AND asset.width = NEW.source_width
                  AND asset.height = NEW.source_height
                  AND asset.byte_size = NEW.source_byte_size
                  AND asset.available_at = NEW.source_available_at
            ) THEN RAISE(
                ABORT,
                'release selection snapshot is invalid'
            ) END;
        END
        """,
        """
        CREATE TRIGGER release_selections_reject_update
        BEFORE UPDATE ON release_selections
        BEGIN
            SELECT RAISE(ABORT, 'release selections are immutable');
        END
        """,
        """
        CREATE TRIGGER release_selections_reject_delete
        BEFORE DELETE ON release_selections
        BEGIN
            SELECT RAISE(ABORT, 'release selections are immutable');
        END
        """,
        """
        CREATE TRIGGER review_tasks_validate_selection_completion
        BEFORE UPDATE ON review_tasks
        WHEN OLD.state = 'open' AND NEW.state = 'completed'
        BEGIN
            SELECT CASE WHEN
                (
                    SELECT count(*)
                    FROM release_selections
                    WHERE review_task_id = OLD.id
                ) <> OLD.desired_accepted_count
                OR (
                    SELECT min(display_order)
                    FROM release_selections
                    WHERE review_task_id = OLD.id
                ) <> 1
                OR (
                    SELECT max(display_order)
                    FROM release_selections
                    WHERE review_task_id = OLD.id
                ) <> OLD.desired_accepted_count
                OR NOT EXISTS (
                    SELECT 1
                    FROM release_versions AS current_version
                    JOIN releases AS current_release
                      ON current_release.id = current_version.release_id
                    WHERE current_version.id = OLD.release_version_id
                      AND current_release.current_version_no =
                          current_version.version_no
                      AND current_release.phase = 'reviewing'
                )
                OR EXISTS (
                    SELECT 1
                    FROM release_selections AS selection
                    JOIN review_decisions AS decision
                      ON decision.id = selection.review_decision_id
                    JOIN asset_rankings AS ranking
                      ON ranking.scoring_run_id =
                          selection.scoring_run_id
                     AND ranking.asset_id = selection.asset_id
                    JOIN release_versions AS version
                      ON version.id = selection.release_version_id
                    JOIN assets AS asset
                      ON asset.id = selection.asset_id
                    WHERE selection.review_task_id = OLD.id
                      AND (
                          selection.scoring_run_id IS NOT
                              OLD.scoring_run_id
                          OR selection.release_version_id IS NOT
                              OLD.release_version_id
                          OR selection.ranking_manifest_sha256 IS NOT
                              OLD.ranking_manifest_sha256
                          OR selection.frozen_at IS NOT NEW.completed_at
                          OR decision.review_task_id IS NOT OLD.id
                          OR decision.asset_id IS NOT selection.asset_id
                          OR decision.revision IS NOT
                              selection.decision_revision
                          OR decision.decision <> 'accept'
                          OR EXISTS (
                              SELECT 1
                              FROM review_decisions AS newer
                              WHERE newer.review_task_id =
                                    decision.review_task_id
                                AND newer.asset_id = decision.asset_id
                                AND newer.revision > decision.revision
                          )
                          OR ranking.rank IS NOT selection.ranking_rank
                          OR asset.release_id IS NOT version.release_id
                          OR asset.kind <> 'raw_master'
                          OR asset.state <> 'available'
                          OR asset.storage_backend IS NOT
                              selection.source_storage_backend
                          OR asset.storage_bucket IS NOT
                              selection.source_storage_bucket
                          OR asset.object_key IS NOT
                              selection.source_object_key
                          OR asset.object_version_id IS NOT
                              selection.source_object_version_id
                          OR asset.sha256 IS NOT selection.source_sha256
                          OR asset.content_type IS NOT
                              selection.source_content_type
                          OR asset.image_format IS NOT
                              selection.source_image_format
                          OR asset.width IS NOT selection.source_width
                          OR asset.height IS NOT selection.source_height
                          OR asset.byte_size IS NOT
                              selection.source_byte_size
                          OR asset.available_at IS NOT
                              selection.source_available_at
                      )
                )
                OR EXISTS (
                    SELECT 1
                    FROM review_decisions AS decision
                    WHERE decision.review_task_id = OLD.id
                      AND decision.decision = 'accept'
                      AND NOT EXISTS (
                          SELECT 1
                          FROM review_decisions AS newer
                          WHERE newer.review_task_id =
                                decision.review_task_id
                            AND newer.asset_id = decision.asset_id
                            AND newer.revision > decision.revision
                      )
                      AND NOT EXISTS (
                          SELECT 1
                          FROM release_selections AS selection
                          WHERE selection.review_task_id = OLD.id
                            AND selection.review_decision_id =
                                decision.id
                      )
                )
                OR EXISTS (
                    SELECT 1
                    FROM release_selections AS earlier
                    JOIN release_selections AS later
                      ON later.review_task_id = earlier.review_task_id
                    WHERE earlier.review_task_id = OLD.id
                      AND earlier.ranking_rank < later.ranking_rank
                      AND earlier.display_order > later.display_order
                )
            THEN RAISE(
                ABORT,
                'review completion selection snapshot is invalid'
            ) END;
        END
        """,
        """
        CREATE TRIGGER review_tasks_promote_release_after_completion
        AFTER UPDATE ON review_tasks
        WHEN OLD.state = 'open' AND NEW.state = 'completed'
        BEGIN
            UPDATE releases
            SET phase = 'approved',
                lock_version = lock_version + 1
            WHERE phase = 'reviewing'
              AND id = (
                  SELECT version.release_id
                  FROM release_versions AS version
                  WHERE version.id = NEW.release_version_id
                    AND version.version_no =
                        releases.current_version_no
              );
            SELECT CASE WHEN changes() <> 1
                THEN RAISE(
                    ABORT,
                    'review release approval compare-and-swap failed'
                )
            END;
        END
        """,
        """
        CREATE TRIGGER derivative_recipes_guard_insert
        BEFORE INSERT ON derivative_recipes
        WHEN NEW.watermark_asset_id IS NOT NULL
        BEGIN
            SELECT CASE WHEN NOT EXISTS (
                SELECT 1
                FROM assets AS asset
                JOIN release_versions AS version
                  ON version.id = NEW.release_version_id
                WHERE asset.id = NEW.watermark_asset_id
                  AND asset.release_id = version.release_id
                  AND asset.state = 'available'
                  AND asset.storage_backend =
                      NEW.watermark_storage_backend
                  AND asset.storage_bucket =
                      NEW.watermark_storage_bucket
                  AND asset.object_key = NEW.watermark_object_key
                  AND asset.object_version_id =
                      NEW.watermark_object_version_id
                  AND asset.sha256 = NEW.watermark_sha256
                  AND asset.content_type = NEW.watermark_content_type
                  AND asset.image_format = NEW.watermark_image_format
                  AND asset.width = NEW.watermark_width
                  AND asset.height = NEW.watermark_height
                  AND asset.byte_size = NEW.watermark_byte_size
            ) THEN RAISE(
                ABORT,
                'derivative recipe watermark snapshot is invalid'
            ) END;
        END
        """,
        """
        CREATE TRIGGER derivative_recipes_reject_update
        BEFORE UPDATE ON derivative_recipes
        BEGIN
            SELECT RAISE(ABORT, 'derivative recipes are immutable');
        END
        """,
        """
        CREATE TRIGGER derivative_recipes_reject_delete
        BEFORE DELETE ON derivative_recipes
        BEGIN
            SELECT RAISE(ABORT, 'derivative recipes are immutable');
        END
        """,
        """
        CREATE TRIGGER derivative_jobs_guard_insert
        BEFORE INSERT ON derivative_jobs
        BEGIN
            SELECT CASE WHEN NOT EXISTS (
                SELECT 1
                FROM release_selections AS selection
                JOIN derivative_recipes AS recipe
                  ON recipe.id = NEW.derivative_recipe_id
                JOIN release_versions AS version
                  ON version.id = NEW.release_version_id
                JOIN releases AS release
                  ON release.id = version.release_id
                WHERE selection.id = NEW.release_selection_id
                  AND selection.release_version_id =
                      NEW.release_version_id
                  AND recipe.release_version_id =
                      NEW.release_version_id
                  AND recipe.expected_output_count =
                      NEW.expected_output_count
                  AND release.current_version_no = version.version_no
                  AND release.phase = 'rendering'
            ) THEN RAISE(
                ABORT,
                'derivative job release snapshot is invalid'
            ) END;
        END
        """,
        """
        CREATE TRIGGER derivative_jobs_guard_update
        BEFORE UPDATE ON derivative_jobs
        BEGIN
            SELECT CASE WHEN
                OLD.state IN ('succeeded', 'failed', 'cancelled')
                OR NEW.lock_version <> OLD.lock_version + 1
                OR OLD.id IS NOT NEW.id
                OR OLD.release_selection_id IS NOT
                    NEW.release_selection_id
                OR OLD.derivative_recipe_id IS NOT
                    NEW.derivative_recipe_id
                OR OLD.release_version_id IS NOT NEW.release_version_id
                OR OLD.logical_key IS NOT NEW.logical_key
                OR OLD.request_payload IS NOT NEW.request_payload
                OR OLD.request_sha256 IS NOT NEW.request_sha256
                OR OLD.expected_output_count IS NOT
                    NEW.expected_output_count
                OR OLD.priority IS NOT NEW.priority
                OR OLD.max_attempts IS NOT NEW.max_attempts
                OR OLD.available_at IS NOT NEW.available_at
                OR OLD.requested_at IS NOT NEW.requested_at
            THEN RAISE(
                ABORT,
                'derivative job identity is immutable'
            ) END;
            SELECT CASE WHEN NOT (
                (
                    OLD.state IN ('requested', 'retry_wait')
                    AND NEW.state IN ('claimed', 'cancelled')
                )
                OR (
                    OLD.state = 'claimed'
                    AND NEW.state IN (
                        'claimed',
                        'processing',
                        'retry_wait',
                        'failed',
                        'cancelled'
                    )
                )
                OR (
                    OLD.state = 'processing'
                    AND NEW.state IN (
                        'claimed',
                        'retry_wait',
                        'succeeded',
                        'failed',
                        'cancelled'
                    )
                )
            ) THEN RAISE(
                ABORT,
                'derivative job state transition is invalid'
            ) END;
            SELECT CASE WHEN
                NEW.state = 'claimed'
                AND NEW.attempt_count <> OLD.attempt_count + 1
            THEN RAISE(
                ABORT,
                'derivative job claim attempt is invalid'
            ) END;
            SELECT CASE WHEN
                NEW.state <> 'claimed'
                AND NEW.attempt_count <> OLD.attempt_count
            THEN RAISE(
                ABORT,
                'derivative job attempt count is immutable'
            ) END;
            SELECT CASE WHEN
                OLD.state IN ('claimed', 'processing')
                AND NEW.state = 'claimed'
                AND (
                    OLD.lease_expires_at IS NULL
                    OR NEW.claimed_at < OLD.lease_expires_at
                )
            THEN RAISE(
                ABORT,
                'active derivative job lease cannot be stolen'
            ) END;
            SELECT CASE WHEN
                NEW.state = 'succeeded'
                AND (
                    SELECT count(*)
                    FROM derivative_outputs
                    WHERE derivative_job_id = OLD.id
                ) <> OLD.expected_output_count
            THEN RAISE(
                ABORT,
                'derivative job outputs are incomplete'
            ) END;
            SELECT CASE WHEN
                NEW.state = 'succeeded'
                AND NOT EXISTS (
                    SELECT 1
                    FROM release_versions AS version
                    JOIN releases AS release
                      ON release.id = version.release_id
                    WHERE version.id = OLD.release_version_id
                      AND release.current_version_no =
                          version.version_no
                      AND release.phase = 'rendering'
                )
            THEN RAISE(
                ABORT,
                'derivative job release phase is invalid'
            ) END;
        END
        """,
        """
        CREATE TRIGGER derivative_jobs_reject_delete
        BEFORE DELETE ON derivative_jobs
        BEGIN
            SELECT RAISE(ABORT, 'derivative jobs cannot be deleted');
        END
        """,
        """
        CREATE TRIGGER derivative_jobs_promote_release_after_success
        AFTER UPDATE ON derivative_jobs
        WHEN OLD.state <> 'succeeded'
         AND NEW.state = 'succeeded'
         AND NOT EXISTS (
             SELECT 1
             FROM derivative_jobs AS pending
             WHERE pending.release_version_id = NEW.release_version_id
               AND pending.state <> 'succeeded'
         )
        BEGIN
            UPDATE releases
            SET phase = 'ready_to_publish',
                lock_version = lock_version + 1
            WHERE phase = 'rendering'
              AND id = (
                  SELECT version.release_id
                  FROM release_versions AS version
                  WHERE version.id = NEW.release_version_id
                    AND version.version_no =
                        releases.current_version_no
              );
            SELECT CASE WHEN changes() <> 1
                THEN RAISE(
                    ABORT,
                    'release readiness compare-and-swap failed'
                )
            END;
        END
        """,
        """
        CREATE TRIGGER derivative_outputs_guard_insert
        BEFORE INSERT ON derivative_outputs
        BEGIN
            SELECT CASE WHEN NOT EXISTS (
                SELECT 1
                FROM derivative_jobs AS job
                JOIN derivative_recipes AS recipe
                  ON recipe.id = job.derivative_recipe_id
                JOIN release_selections AS selection
                  ON selection.id = job.release_selection_id
                JOIN release_versions AS version
                  ON version.id = job.release_version_id
                JOIN assets AS asset
                  ON asset.id = NEW.asset_id
                JOIN asset_lineage AS lineage
                  ON lineage.id = NEW.asset_lineage_id
                WHERE job.id = NEW.derivative_job_id
                  AND job.release_selection_id =
                      NEW.release_selection_id
                  AND job.derivative_recipe_id =
                      NEW.derivative_recipe_id
                  AND job.state = 'processing'
                  AND job.lease_expires_at > NEW.recorded_at
                  AND EXISTS (
                      SELECT 1
                      FROM json_each(recipe.output_targets)
                      WHERE value = NEW.target
                  )
                  AND selection.asset_id = NEW.source_asset_id
                  AND asset.release_id = version.release_id
                  AND asset.kind = 'derivative'
                  AND asset.state = 'available'
                  AND asset.storage_backend =
                      NEW.asset_storage_backend
                  AND asset.storage_bucket =
                      NEW.asset_storage_bucket
                  AND asset.object_key = NEW.asset_object_key
                  AND asset.object_version_id =
                      NEW.asset_object_version_id
                  AND asset.sha256 = NEW.asset_sha256
                  AND asset.content_type = NEW.asset_content_type
                  AND asset.image_format = NEW.asset_image_format
                  AND asset.width = NEW.asset_width
                  AND asset.height = NEW.asset_height
                  AND asset.byte_size = NEW.asset_byte_size
                  AND lineage.parent_asset_id = selection.asset_id
                  AND lineage.child_asset_id = asset.id
                  AND lineage.relation = NEW.lineage_relation
                  AND lineage.relation = 'derivative'
                  AND lineage.recipe_version =
                      NEW.lineage_recipe_version
                  AND lineage.recipe_version = recipe.config_sha256
            ) THEN RAISE(
                ABORT,
                'derivative output snapshot is invalid'
            ) END;
        END
        """,
        """
        CREATE TRIGGER derivative_outputs_reject_update
        BEFORE UPDATE ON derivative_outputs
        BEGIN
            SELECT RAISE(ABORT, 'derivative outputs are append-only');
        END
        """,
        """
        CREATE TRIGGER derivative_outputs_reject_delete
        BEFORE DELETE ON derivative_outputs
        BEGIN
            SELECT RAISE(ABORT, 'derivative outputs are append-only');
        END
        """,
        """
        CREATE TRIGGER asset_lineage_guard_derivative_update
        BEFORE UPDATE ON asset_lineage
        WHEN EXISTS (
            SELECT 1
            FROM derivative_outputs
            WHERE asset_lineage_id = OLD.id
        )
        BEGIN
            SELECT RAISE(
                ABORT,
                'recorded derivative lineage is immutable'
            );
        END
        """,
        """
        CREATE TRIGGER asset_lineage_guard_derivative_delete
        BEFORE DELETE ON asset_lineage
        WHEN EXISTS (
            SELECT 1
            FROM derivative_outputs
            WHERE asset_lineage_id = OLD.id
        )
        BEGIN
            SELECT RAISE(
                ABORT,
                'recorded derivative lineage is immutable'
            );
        END
        """,
    )


def _sqlite_trigger_names() -> tuple[str, ...]:
    return tuple(statement.split()[2] for statement in _sqlite_guard_statements())


def _postgresql_triggers() -> tuple[tuple[str, str], ...]:
    return tuple(
        (trigger, table) for trigger, table, _timing, _function in _postgresql_trigger_specs()
    )


def _postgresql_function_statements() -> tuple[str, ...]:
    return (
        """
        CREATE OR REPLACE FUNCTION gen_automation_guard_review_decision_insert()
        RETURNS trigger AS $$
        BEGIN
            PERFORM 1
            FROM review_tasks
            WHERE id = NEW.review_task_id
              AND scoring_run_id = NEW.scoring_run_id
              AND state = 'open'
            FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION
                    'terminal review tasks reject new decisions';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """,
        """
        CREATE OR REPLACE FUNCTION
            gen_automation_guard_release_selection_mutation()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP <> 'INSERT' THEN
                RAISE EXCEPTION 'release selections are immutable';
            END IF;
            PERFORM 1
            FROM review_tasks
            WHERE id = NEW.review_task_id
              AND state = 'open'
            FOR UPDATE;
            IF NOT FOUND OR NOT EXISTS (
                SELECT 1
                FROM review_tasks AS task
                JOIN release_versions AS version
                  ON version.id = task.release_version_id
                JOIN review_decisions AS decision
                  ON decision.id = NEW.review_decision_id
                 AND decision.review_task_id = task.id
                 AND decision.asset_id = NEW.asset_id
                 AND decision.revision = NEW.decision_revision
                 AND decision.scoring_run_id = task.scoring_run_id
                JOIN asset_rankings AS ranking
                  ON ranking.scoring_run_id = task.scoring_run_id
                 AND ranking.asset_id = NEW.asset_id
                JOIN assets AS asset
                  ON asset.id = NEW.asset_id
                WHERE task.id = NEW.review_task_id
                  AND task.scoring_run_id = NEW.scoring_run_id
                  AND task.release_version_id = NEW.release_version_id
                  AND task.ranking_manifest_sha256 =
                      NEW.ranking_manifest_sha256
                  AND decision.decision = 'accept'
                  AND NOT EXISTS (
                      SELECT 1
                      FROM review_decisions AS newer
                      WHERE newer.review_task_id =
                            decision.review_task_id
                        AND newer.asset_id = decision.asset_id
                        AND newer.revision > decision.revision
                  )
                  AND ranking.rank = NEW.ranking_rank
                  AND asset.release_id = version.release_id
                  AND asset.kind = 'raw_master'
                  AND asset.state = 'available'
                  AND asset.storage_backend =
                      NEW.source_storage_backend
                  AND asset.storage_bucket =
                      NEW.source_storage_bucket
                  AND asset.object_key = NEW.source_object_key
                  AND asset.object_version_id =
                      NEW.source_object_version_id
                  AND asset.sha256 = NEW.source_sha256
                  AND asset.content_type = NEW.source_content_type
                  AND asset.image_format = NEW.source_image_format
                  AND asset.width = NEW.source_width
                  AND asset.height = NEW.source_height
                  AND asset.byte_size = NEW.source_byte_size
                  AND asset.available_at = NEW.source_available_at
            ) THEN
                RAISE EXCEPTION
                    'release selection snapshot is invalid';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """,
        """
        CREATE OR REPLACE FUNCTION
            gen_automation_validate_selection_completion()
        RETURNS trigger AS $$
        BEGIN
            IF OLD.state = 'open' AND NEW.state = 'completed' AND (
                (
                    SELECT count(*)
                    FROM release_selections
                    WHERE review_task_id = OLD.id
                ) <> OLD.desired_accepted_count
                OR (
                    SELECT min(display_order)
                    FROM release_selections
                    WHERE review_task_id = OLD.id
                ) <> 1
                OR (
                    SELECT max(display_order)
                    FROM release_selections
                    WHERE review_task_id = OLD.id
                ) <> OLD.desired_accepted_count
                OR NOT EXISTS (
                    SELECT 1
                    FROM release_versions AS current_version
                    JOIN releases AS current_release
                      ON current_release.id =
                         current_version.release_id
                    WHERE current_version.id =
                          OLD.release_version_id
                      AND current_release.current_version_no =
                          current_version.version_no
                      AND current_release.phase = 'reviewing'
                )
                OR EXISTS (
                    SELECT 1
                    FROM release_selections AS selection
                    JOIN review_decisions AS decision
                      ON decision.id =
                         selection.review_decision_id
                    JOIN asset_rankings AS ranking
                      ON ranking.scoring_run_id =
                         selection.scoring_run_id
                     AND ranking.asset_id = selection.asset_id
                    JOIN release_versions AS version
                      ON version.id =
                         selection.release_version_id
                    JOIN assets AS asset
                      ON asset.id = selection.asset_id
                    WHERE selection.review_task_id = OLD.id
                      AND (
                          selection.scoring_run_id IS DISTINCT FROM
                              OLD.scoring_run_id
                          OR selection.release_version_id IS DISTINCT FROM
                              OLD.release_version_id
                          OR selection.ranking_manifest_sha256
                              IS DISTINCT FROM
                              OLD.ranking_manifest_sha256
                          OR selection.frozen_at IS DISTINCT FROM
                              NEW.completed_at
                          OR decision.review_task_id IS DISTINCT FROM
                              OLD.id
                          OR decision.asset_id IS DISTINCT FROM
                              selection.asset_id
                          OR decision.revision IS DISTINCT FROM
                              selection.decision_revision
                          OR decision.decision <> 'accept'
                          OR EXISTS (
                              SELECT 1
                              FROM review_decisions AS newer
                              WHERE newer.review_task_id =
                                    decision.review_task_id
                                AND newer.asset_id =
                                    decision.asset_id
                                AND newer.revision >
                                    decision.revision
                          )
                          OR ranking.rank IS DISTINCT FROM
                              selection.ranking_rank
                          OR asset.release_id IS DISTINCT FROM
                              version.release_id
                          OR asset.kind <> 'raw_master'
                          OR asset.state <> 'available'
                          OR asset.storage_backend IS DISTINCT FROM
                              selection.source_storage_backend
                          OR asset.storage_bucket IS DISTINCT FROM
                              selection.source_storage_bucket
                          OR asset.object_key IS DISTINCT FROM
                              selection.source_object_key
                          OR asset.object_version_id IS DISTINCT FROM
                              selection.source_object_version_id
                          OR asset.sha256 IS DISTINCT FROM
                              selection.source_sha256
                          OR asset.content_type IS DISTINCT FROM
                              selection.source_content_type
                          OR asset.image_format IS DISTINCT FROM
                              selection.source_image_format
                          OR asset.width IS DISTINCT FROM
                              selection.source_width
                          OR asset.height IS DISTINCT FROM
                              selection.source_height
                          OR asset.byte_size IS DISTINCT FROM
                              selection.source_byte_size
                          OR asset.available_at IS DISTINCT FROM
                              selection.source_available_at
                      )
                )
                OR EXISTS (
                    SELECT 1
                    FROM review_decisions AS decision
                    WHERE decision.review_task_id = OLD.id
                      AND decision.decision = 'accept'
                      AND NOT EXISTS (
                          SELECT 1
                          FROM review_decisions AS newer
                          WHERE newer.review_task_id =
                                decision.review_task_id
                            AND newer.asset_id = decision.asset_id
                            AND newer.revision > decision.revision
                      )
                      AND NOT EXISTS (
                          SELECT 1
                          FROM release_selections AS selection
                          WHERE selection.review_task_id = OLD.id
                            AND selection.review_decision_id =
                                decision.id
                      )
                )
                OR EXISTS (
                    SELECT 1
                    FROM release_selections AS earlier
                    JOIN release_selections AS later
                      ON later.review_task_id =
                         earlier.review_task_id
                    WHERE earlier.review_task_id = OLD.id
                      AND earlier.ranking_rank <
                          later.ranking_rank
                      AND earlier.display_order >
                          later.display_order
                )
            ) THEN
                RAISE EXCEPTION
                    'review completion selection snapshot is invalid';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """,
        """
        CREATE OR REPLACE FUNCTION
            gen_automation_promote_reviewed_release()
        RETURNS trigger AS $$
        DECLARE
            changed_count integer;
        BEGIN
            IF OLD.state = 'open' AND NEW.state = 'completed' THEN
                UPDATE releases
                SET phase = 'approved',
                    lock_version = lock_version + 1
                WHERE phase = 'reviewing'
                  AND id = (
                      SELECT version.release_id
                      FROM release_versions AS version
                      WHERE version.id = NEW.release_version_id
                        AND version.version_no =
                            releases.current_version_no
                  );
                GET DIAGNOSTICS changed_count = ROW_COUNT;
                IF changed_count <> 1 THEN
                    RAISE EXCEPTION
                        'review release approval compare-and-swap failed';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """,
        """
        CREATE OR REPLACE FUNCTION
            gen_automation_guard_derivative_recipe_mutation()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP <> 'INSERT' THEN
                RAISE EXCEPTION 'derivative recipes are immutable';
            END IF;
            IF NEW.watermark_asset_id IS NOT NULL
               AND NOT EXISTS (
                   SELECT 1
                   FROM assets AS asset
                   JOIN release_versions AS version
                     ON version.id = NEW.release_version_id
                   WHERE asset.id = NEW.watermark_asset_id
                     AND asset.release_id = version.release_id
                     AND asset.state = 'available'
                     AND asset.storage_backend =
                         NEW.watermark_storage_backend
                     AND asset.storage_bucket =
                         NEW.watermark_storage_bucket
                     AND asset.object_key =
                         NEW.watermark_object_key
                     AND asset.object_version_id =
                         NEW.watermark_object_version_id
                     AND asset.sha256 = NEW.watermark_sha256
                     AND asset.content_type =
                         NEW.watermark_content_type
                     AND asset.image_format =
                         NEW.watermark_image_format
                     AND asset.width = NEW.watermark_width
                     AND asset.height = NEW.watermark_height
                     AND asset.byte_size =
                         NEW.watermark_byte_size
               ) THEN
                RAISE EXCEPTION
                    'derivative recipe watermark snapshot is invalid';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """,
        """
        CREATE OR REPLACE FUNCTION
            gen_automation_guard_derivative_job_insert()
        RETURNS trigger AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM release_selections AS selection
                JOIN derivative_recipes AS recipe
                  ON recipe.id = NEW.derivative_recipe_id
                JOIN release_versions AS version
                  ON version.id = NEW.release_version_id
                JOIN releases AS release
                  ON release.id = version.release_id
                WHERE selection.id =
                      NEW.release_selection_id
                  AND selection.release_version_id =
                      NEW.release_version_id
                  AND recipe.release_version_id =
                      NEW.release_version_id
                  AND recipe.expected_output_count =
                      NEW.expected_output_count
                  AND release.current_version_no =
                      version.version_no
                  AND release.phase = 'rendering'
            ) THEN
                RAISE EXCEPTION
                    'derivative job release snapshot is invalid';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """,
        """
        CREATE OR REPLACE FUNCTION
            gen_automation_guard_derivative_job_mutation()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION
                    'derivative jobs cannot be deleted';
            END IF;
            IF OLD.state IN ('succeeded', 'failed', 'cancelled')
               OR NEW.lock_version <> OLD.lock_version + 1
               OR OLD.id IS DISTINCT FROM NEW.id
               OR OLD.release_selection_id IS DISTINCT FROM
                  NEW.release_selection_id
               OR OLD.derivative_recipe_id IS DISTINCT FROM
                  NEW.derivative_recipe_id
               OR OLD.release_version_id IS DISTINCT FROM
                  NEW.release_version_id
               OR OLD.logical_key IS DISTINCT FROM NEW.logical_key
               OR OLD.request_payload IS DISTINCT FROM
                  NEW.request_payload
               OR OLD.request_sha256 IS DISTINCT FROM
                  NEW.request_sha256
               OR OLD.expected_output_count IS DISTINCT FROM
                  NEW.expected_output_count
               OR OLD.priority IS DISTINCT FROM NEW.priority
               OR OLD.max_attempts IS DISTINCT FROM
                  NEW.max_attempts
               OR OLD.available_at IS DISTINCT FROM NEW.available_at
               OR OLD.requested_at IS DISTINCT FROM NEW.requested_at
            THEN
                RAISE EXCEPTION
                    'derivative job identity is immutable';
            END IF;
            IF NOT (
                (
                    OLD.state IN ('requested', 'retry_wait')
                    AND NEW.state IN ('claimed', 'cancelled')
                )
                OR (
                    OLD.state = 'claimed'
                    AND NEW.state IN (
                        'claimed',
                        'processing',
                        'retry_wait',
                        'failed',
                        'cancelled'
                    )
                )
                OR (
                    OLD.state = 'processing'
                    AND NEW.state IN (
                        'claimed',
                        'retry_wait',
                        'succeeded',
                        'failed',
                        'cancelled'
                    )
                )
            ) THEN
                RAISE EXCEPTION
                    'derivative job state transition is invalid';
            END IF;
            IF NEW.state = 'claimed'
               AND NEW.attempt_count <> OLD.attempt_count + 1
            THEN
                RAISE EXCEPTION
                    'derivative job claim attempt is invalid';
            END IF;
            IF NEW.state <> 'claimed'
               AND NEW.attempt_count <> OLD.attempt_count
            THEN
                RAISE EXCEPTION
                    'derivative job attempt count is immutable';
            END IF;
            IF OLD.state IN ('claimed', 'processing')
               AND NEW.state = 'claimed'
               AND (
                   OLD.lease_expires_at IS NULL
                   OR NEW.claimed_at < OLD.lease_expires_at
               )
            THEN
                RAISE EXCEPTION
                    'active derivative job lease cannot be stolen';
            END IF;
            IF NEW.state = 'succeeded'
               AND (
                   SELECT count(*)
                   FROM derivative_outputs
                   WHERE derivative_job_id = OLD.id
               ) <> OLD.expected_output_count
            THEN
                RAISE EXCEPTION
                    'derivative job outputs are incomplete';
            END IF;
            IF NEW.state = 'succeeded'
               AND NOT EXISTS (
                   SELECT 1
                   FROM release_versions AS version
                   JOIN releases AS release
                     ON release.id = version.release_id
                   WHERE version.id = OLD.release_version_id
                     AND release.current_version_no =
                         version.version_no
                     AND release.phase = 'rendering'
               )
            THEN
                RAISE EXCEPTION
                    'derivative job release phase is invalid';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """,
        """
        CREATE OR REPLACE FUNCTION
            gen_automation_promote_rendered_release()
        RETURNS trigger AS $$
        DECLARE
            changed_count integer;
        BEGIN
            IF OLD.state <> 'succeeded'
               AND NEW.state = 'succeeded'
               AND NOT EXISTS (
                   SELECT 1
                   FROM derivative_jobs AS pending
                   WHERE pending.release_version_id =
                         NEW.release_version_id
                     AND pending.state <> 'succeeded'
               )
            THEN
                UPDATE releases
                SET phase = 'ready_to_publish',
                    lock_version = lock_version + 1
                WHERE phase = 'rendering'
                  AND id = (
                      SELECT version.release_id
                      FROM release_versions AS version
                      WHERE version.id = NEW.release_version_id
                        AND version.version_no =
                            releases.current_version_no
                  );
                GET DIAGNOSTICS changed_count = ROW_COUNT;
                IF changed_count <> 1 THEN
                    RAISE EXCEPTION
                        'release readiness compare-and-swap failed';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """,
        """
        CREATE OR REPLACE FUNCTION
            gen_automation_guard_derivative_output_mutation()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP <> 'INSERT' THEN
                RAISE EXCEPTION
                    'derivative outputs are append-only';
            END IF;
            PERFORM 1
            FROM derivative_jobs
            WHERE id = NEW.derivative_job_id
              AND state = 'processing'
            FOR UPDATE;
            IF NOT FOUND OR NOT EXISTS (
                SELECT 1
                FROM derivative_jobs AS job
                JOIN derivative_recipes AS recipe
                  ON recipe.id = job.derivative_recipe_id
                JOIN release_selections AS selection
                  ON selection.id = job.release_selection_id
                JOIN release_versions AS version
                  ON version.id = job.release_version_id
                JOIN assets AS asset
                  ON asset.id = NEW.asset_id
                JOIN asset_lineage AS lineage
                  ON lineage.id = NEW.asset_lineage_id
                WHERE job.id = NEW.derivative_job_id
                  AND job.release_selection_id =
                      NEW.release_selection_id
                  AND job.derivative_recipe_id =
                      NEW.derivative_recipe_id
                  AND job.state = 'processing'
                  AND job.lease_expires_at > NEW.recorded_at
                  AND recipe.output_targets ? NEW.target
                  AND selection.asset_id =
                      NEW.source_asset_id
                  AND asset.release_id = version.release_id
                  AND asset.kind = 'derivative'
                  AND asset.state = 'available'
                  AND asset.storage_backend =
                      NEW.asset_storage_backend
                  AND asset.storage_bucket =
                      NEW.asset_storage_bucket
                  AND asset.object_key =
                      NEW.asset_object_key
                  AND asset.object_version_id =
                      NEW.asset_object_version_id
                  AND asset.sha256 = NEW.asset_sha256
                  AND asset.content_type =
                      NEW.asset_content_type
                  AND asset.image_format =
                      NEW.asset_image_format
                  AND asset.width = NEW.asset_width
                  AND asset.height = NEW.asset_height
                  AND asset.byte_size = NEW.asset_byte_size
                  AND lineage.parent_asset_id =
                      selection.asset_id
                  AND lineage.child_asset_id = asset.id
                  AND lineage.relation =
                      NEW.lineage_relation
                  AND lineage.relation = 'derivative'
                  AND lineage.recipe_version =
                      NEW.lineage_recipe_version
                  AND lineage.recipe_version =
                      recipe.config_sha256
            ) THEN
                RAISE EXCEPTION
                    'derivative output snapshot is invalid';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """,
        """
        CREATE OR REPLACE FUNCTION
            gen_automation_guard_recorded_lineage_mutation()
        RETURNS trigger AS $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM derivative_outputs
                WHERE asset_lineage_id = OLD.id
            ) THEN
                RAISE EXCEPTION
                    'recorded derivative lineage is immutable';
            END IF;
            IF TG_OP = 'DELETE' THEN
                RETURN OLD;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """,
    )


def _postgresql_functions() -> tuple[str, ...]:
    return (
        "gen_automation_guard_review_decision_insert",
        "gen_automation_guard_release_selection_mutation",
        "gen_automation_validate_selection_completion",
        "gen_automation_promote_reviewed_release",
        "gen_automation_guard_derivative_recipe_mutation",
        "gen_automation_guard_derivative_job_insert",
        "gen_automation_guard_derivative_job_mutation",
        "gen_automation_promote_rendered_release",
        "gen_automation_guard_derivative_output_mutation",
        "gen_automation_guard_recorded_lineage_mutation",
    )


def _postgresql_trigger_specs() -> tuple[tuple[str, str, str, str], ...]:
    return (
        (
            "review_decisions_guard_late_insert",
            "review_decisions",
            "BEFORE INSERT",
            "gen_automation_guard_review_decision_insert",
        ),
        (
            "release_selections_guard_mutation",
            "release_selections",
            "BEFORE INSERT OR UPDATE OR DELETE",
            "gen_automation_guard_release_selection_mutation",
        ),
        (
            "review_tasks_validate_selection_completion",
            "review_tasks",
            "BEFORE UPDATE",
            "gen_automation_validate_selection_completion",
        ),
        (
            "review_tasks_promote_release_after_completion",
            "review_tasks",
            "AFTER UPDATE",
            "gen_automation_promote_reviewed_release",
        ),
        (
            "derivative_recipes_guard_mutation",
            "derivative_recipes",
            "BEFORE INSERT OR UPDATE OR DELETE",
            "gen_automation_guard_derivative_recipe_mutation",
        ),
        (
            "derivative_jobs_guard_insert",
            "derivative_jobs",
            "BEFORE INSERT",
            "gen_automation_guard_derivative_job_insert",
        ),
        (
            "derivative_jobs_guard_mutation",
            "derivative_jobs",
            "BEFORE UPDATE OR DELETE",
            "gen_automation_guard_derivative_job_mutation",
        ),
        (
            "derivative_jobs_promote_release_after_success",
            "derivative_jobs",
            "AFTER UPDATE",
            "gen_automation_promote_rendered_release",
        ),
        (
            "derivative_outputs_guard_mutation",
            "derivative_outputs",
            "BEFORE INSERT OR UPDATE OR DELETE",
            "gen_automation_guard_derivative_output_mutation",
        ),
        (
            "asset_lineage_guard_derivative_mutation",
            "asset_lineage",
            "BEFORE UPDATE OR DELETE",
            "gen_automation_guard_recorded_lineage_mutation",
        ),
    )
