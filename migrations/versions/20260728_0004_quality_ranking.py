"""Add durable quality scoring signals and frozen asset rankings.

Revision ID: 20260728_0004
Revises: 20260728_0003
Create Date: 2026-07-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260728_0004"
down_revision: str | None = "20260728_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
scoring_run_state = sa.Enum(
    "running",
    "completed",
    name="scoring_run_state",
    native_enum=False,
    create_constraint=True,
)
asset_score_state = sa.Enum(
    "pending",
    "processing",
    "retry_wait",
    "scored",
    "flagged_blank",
    "flagged_corrupt",
    "dead_letter",
    name="asset_score_state",
    native_enum=False,
    create_constraint=True,
)
ranking_disposition = sa.Enum(
    "review_candidate",
    "near_duplicate",
    "flagged_review",
    name="ranking_disposition",
    native_enum=False,
    create_constraint=True,
)


def upgrade() -> None:
    op.create_table(
        "scoring_runs",
        sa.Column("release_version_id", sa.Uuid(), nullable=False),
        sa.Column("configuration", json_type, nullable=False),
        sa.Column("config_sha256", sa.String(length=64), nullable=False),
        sa.Column("input_manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column("scorer_version", sa.String(length=100), nullable=False),
        sa.Column("pillow_version", sa.String(length=50), nullable=False),
        sa.Column("state", scoring_run_state, nullable=False),
        sa.Column("asset_count", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "asset_count > 0",
            name=op.f("ck_scoring_runs_positive_asset_count"),
        ),
        sa.CheckConstraint(
            "max_attempts > 0",
            name=op.f("ck_scoring_runs_positive_max_attempts"),
        ),
        sa.CheckConstraint(
            "(state = 'completed' AND completed_at IS NOT NULL) "
            "OR (state <> 'completed' AND completed_at IS NULL)",
            name=op.f("ck_scoring_runs_completion_pair"),
        ),
        sa.ForeignKeyConstraint(
            ["release_version_id"],
            ["release_versions.id"],
            name=op.f("fk_scoring_runs_release_version_id_release_versions"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_scoring_runs")),
        sa.UniqueConstraint(
            "release_version_id",
            "config_sha256",
            "scorer_version",
            name="uq_scoring_runs_identity",
        ),
    )
    op.create_index(
        op.f("ix_scoring_runs_release_version_id"),
        "scoring_runs",
        ["release_version_id"],
    )

    op.create_table(
        "asset_scores",
        sa.Column("scoring_run_id", sa.Uuid(), nullable=False),
        sa.Column("asset_id", sa.Uuid(), nullable=False),
        sa.Column("asset_storage_backend", sa.String(length=50), nullable=False),
        sa.Column("asset_storage_bucket", sa.String(length=255), nullable=False),
        sa.Column("asset_sha256", sa.String(length=64), nullable=False),
        sa.Column("asset_object_key", sa.String(length=1024), nullable=False),
        sa.Column("asset_object_version_id", sa.String(length=1024), nullable=False),
        sa.Column("asset_byte_size", sa.BigInteger(), nullable=False),
        sa.Column("asset_image_format", sa.String(length=20), nullable=False),
        sa.Column("asset_width", sa.Integer(), nullable=False),
        sa.Column("asset_height", sa.Integer(), nullable=False),
        sa.Column("state", asset_score_state, nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_owner", sa.String(length=200), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("luminance_mean_micros", sa.Integer(), nullable=True),
        sa.Column("luminance_std_micros", sa.Integer(), nullable=True),
        sa.Column("dynamic_range_micros", sa.Integer(), nullable=True),
        sa.Column("entropy_bits_micros", sa.Integer(), nullable=True),
        sa.Column("entropy_normalized_micros", sa.Integer(), nullable=True),
        sa.Column("sharpness_micros", sa.Integer(), nullable=True),
        sa.Column("dhash_hex", sa.String(length=16), nullable=True),
        sa.Column("aggregate_score_micros", sa.Integer(), nullable=True),
        sa.Column("score_breakdown", json_type, nullable=True),
        sa.Column("signal_detail", json_type, nullable=False),
        sa.Column("scorer_version", sa.String(length=100), nullable=False),
        sa.Column("pillow_version", sa.String(length=50), nullable=False),
        sa.Column("config_sha256", sa.String(length=64), nullable=False),
        sa.Column("last_error_code", sa.String(length=100), nullable=True),
        sa.Column("last_error_detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "attempts >= 0",
            name=op.f("ck_asset_scores_nonnegative_attempts"),
        ),
        sa.CheckConstraint(
            "max_attempts > 0",
            name=op.f("ck_asset_scores_positive_max_attempts"),
        ),
        sa.CheckConstraint(
            "attempts <= max_attempts",
            name=op.f("ck_asset_scores_attempts_within_limit"),
        ),
        sa.CheckConstraint(
            "asset_byte_size > 0",
            name=op.f("ck_asset_scores_positive_asset_byte_size"),
        ),
        sa.CheckConstraint(
            "(state = 'processing' "
            "AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL) "
            "OR (state <> 'processing' "
            "AND lease_owner IS NULL AND lease_expires_at IS NULL)",
            name=op.f("ck_asset_scores_lease_state"),
        ),
        sa.CheckConstraint(
            "state NOT IN "
            "('scored', 'flagged_blank', 'flagged_corrupt', 'dead_letter') "
            "OR completed_at IS NOT NULL",
            name=op.f("ck_asset_scores_terminal_is_completed"),
        ),
        sa.CheckConstraint(
            "state NOT IN ('scored', 'flagged_blank') OR "
            "(luminance_mean_micros IS NOT NULL "
            "AND luminance_std_micros IS NOT NULL "
            "AND dynamic_range_micros IS NOT NULL "
            "AND entropy_bits_micros IS NOT NULL "
            "AND entropy_normalized_micros IS NOT NULL "
            "AND sharpness_micros IS NOT NULL "
            "AND dhash_hex IS NOT NULL "
            "AND aggregate_score_micros IS NOT NULL "
            "AND score_breakdown IS NOT NULL)",
            name=op.f("ck_asset_scores_scored_signal_complete"),
        ),
        sa.CheckConstraint(
            "luminance_mean_micros IS NULL OR luminance_mean_micros BETWEEN 0 AND 1000000",
            name=op.f("ck_asset_scores_valid_luminance_mean"),
        ),
        sa.CheckConstraint(
            "luminance_std_micros IS NULL OR luminance_std_micros BETWEEN 0 AND 1000000",
            name=op.f("ck_asset_scores_valid_luminance_std"),
        ),
        sa.CheckConstraint(
            "dynamic_range_micros IS NULL OR dynamic_range_micros BETWEEN 0 AND 1000000",
            name=op.f("ck_asset_scores_valid_dynamic_range"),
        ),
        sa.CheckConstraint(
            "entropy_bits_micros IS NULL OR entropy_bits_micros BETWEEN 0 AND 8000000",
            name=op.f("ck_asset_scores_valid_entropy_bits"),
        ),
        sa.CheckConstraint(
            "entropy_normalized_micros IS NULL OR entropy_normalized_micros BETWEEN 0 AND 1000000",
            name=op.f("ck_asset_scores_valid_entropy_normalized"),
        ),
        sa.CheckConstraint(
            "sharpness_micros IS NULL OR sharpness_micros BETWEEN 0 AND 1000000",
            name=op.f("ck_asset_scores_valid_sharpness"),
        ),
        sa.CheckConstraint(
            "aggregate_score_micros IS NULL OR aggregate_score_micros BETWEEN 0 AND 1000000",
            name=op.f("ck_asset_scores_valid_aggregate_score"),
        ),
        sa.CheckConstraint(
            "dhash_hex IS NULL OR length(dhash_hex) = 16",
            name=op.f("ck_asset_scores_valid_dhash_length"),
        ),
        sa.ForeignKeyConstraint(
            ["asset_id"],
            ["assets.id"],
            name=op.f("fk_asset_scores_asset_id_assets"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["scoring_run_id"],
            ["scoring_runs.id"],
            name=op.f("fk_asset_scores_scoring_run_id_scoring_runs"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_asset_scores")),
        sa.UniqueConstraint(
            "scoring_run_id",
            "asset_id",
            name="uq_asset_scores_run_asset",
        ),
    )
    op.create_index(
        "ix_asset_scores_claim",
        "asset_scores",
        ["state", "available_at", "lease_expires_at", "created_at"],
    )
    op.create_index(
        op.f("ix_asset_scores_asset_id"),
        "asset_scores",
        ["asset_id"],
    )
    op.create_index(
        op.f("ix_asset_scores_scoring_run_id"),
        "asset_scores",
        ["scoring_run_id"],
    )

    op.create_table(
        "asset_rankings",
        sa.Column("scoring_run_id", sa.Uuid(), nullable=False),
        sa.Column("asset_score_id", sa.Uuid(), nullable=False),
        sa.Column("asset_id", sa.Uuid(), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("aggregate_score_micros", sa.Integer(), nullable=False),
        sa.Column("disposition", ranking_disposition, nullable=False),
        sa.Column("explanation", json_type, nullable=False),
        sa.Column("duplicate_cluster_id", sa.String(length=64), nullable=True),
        sa.Column("duplicate_representative_asset_id", sa.Uuid(), nullable=True),
        sa.Column("is_duplicate_representative", sa.Boolean(), nullable=False),
        sa.Column("scorer_version", sa.String(length=100), nullable=False),
        sa.Column("pillow_version", sa.String(length=50), nullable=False),
        sa.Column("config_sha256", sa.String(length=64), nullable=False),
        sa.Column("frozen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "rank > 0",
            name=op.f("ck_asset_rankings_positive_rank"),
        ),
        sa.CheckConstraint(
            "aggregate_score_micros BETWEEN 0 AND 1000000",
            name=op.f("ck_asset_rankings_valid_aggregate_score"),
        ),
        sa.CheckConstraint(
            "(duplicate_cluster_id IS NULL "
            "AND duplicate_representative_asset_id IS NULL "
            "AND is_duplicate_representative = false) "
            "OR (duplicate_cluster_id IS NOT NULL "
            "AND duplicate_representative_asset_id IS NOT NULL)",
            name=op.f("ck_asset_rankings_duplicate_identity"),
        ),
        sa.CheckConstraint(
            "is_duplicate_representative = false OR duplicate_representative_asset_id = asset_id",
            name=op.f("ck_asset_rankings_representative_is_self"),
        ),
        sa.CheckConstraint(
            "disposition <> 'near_duplicate' "
            "OR (duplicate_cluster_id IS NOT NULL "
            "AND is_duplicate_representative = false)",
            name=op.f("ck_asset_rankings_near_duplicate_identity"),
        ),
        sa.ForeignKeyConstraint(
            ["asset_id"],
            ["assets.id"],
            name=op.f("fk_asset_rankings_asset_id_assets"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["asset_score_id"],
            ["asset_scores.id"],
            name=op.f("fk_asset_rankings_asset_score_id_asset_scores"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["duplicate_representative_asset_id"],
            ["assets.id"],
            name=op.f("fk_asset_rankings_duplicate_representative_asset_id_assets"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["scoring_run_id"],
            ["scoring_runs.id"],
            name=op.f("fk_asset_rankings_scoring_run_id_scoring_runs"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_asset_rankings")),
        sa.UniqueConstraint(
            "asset_score_id",
            name="uq_asset_rankings_asset_score",
        ),
        sa.UniqueConstraint(
            "scoring_run_id",
            "asset_id",
            name="uq_asset_rankings_run_asset",
        ),
        sa.UniqueConstraint(
            "scoring_run_id",
            "rank",
            name="uq_asset_rankings_run_rank",
        ),
    )
    op.create_index(
        op.f("ix_asset_rankings_asset_id"),
        "asset_rankings",
        ["asset_id"],
    )
    op.create_index(
        op.f("ix_asset_rankings_scoring_run_id"),
        "asset_rankings",
        ["scoring_run_id"],
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_asset_rankings_scoring_run_id"),
        table_name="asset_rankings",
    )
    op.drop_index(
        op.f("ix_asset_rankings_asset_id"),
        table_name="asset_rankings",
    )
    op.drop_table("asset_rankings")

    op.drop_index(
        op.f("ix_asset_scores_scoring_run_id"),
        table_name="asset_scores",
    )
    op.drop_index(
        op.f("ix_asset_scores_asset_id"),
        table_name="asset_scores",
    )
    op.drop_index("ix_asset_scores_claim", table_name="asset_scores")
    op.drop_table("asset_scores")

    op.drop_index(
        op.f("ix_scoring_runs_release_version_id"),
        table_name="scoring_runs",
    )
    op.drop_table("scoring_runs")
