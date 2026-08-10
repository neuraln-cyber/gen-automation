"""Add isolated image-to-video jobs and Salad deployment lanes.

Revision ID: 20260810_0034
Revises: 20260809_0033
Create Date: 2026-08-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260810_0034"
down_revision: str | None = "20260809_0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
video_content_rating = sa.Enum(
    "sfw",
    "nsfw",
    "explicit",
    name="video_content_rating",
    native_enum=False,
    create_constraint=True,
)
video_generation_state = sa.Enum(
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
    "cancel_requested",
    "cancelled",
    name="video_generation_state",
    native_enum=False,
    create_constraint=True,
)
video_generation_attempt_state = sa.Enum(
    "created",
    "submitting",
    "submitted",
    "running",
    "succeeded",
    "failed",
    "unknown",
    "cancel_requested",
    "cancelled",
    name="video_generation_attempt_state",
    native_enum=False,
    create_constraint=True,
)


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
                "purpose",
                sa.String(length=5),
                server_default=sa.text("'image'"),
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column("administrative_stop_reason", sa.String(length=100), nullable=True)
        )
        batch_op.create_check_constraint(
            "salad_deployment_purpose",
            "purpose IN ('image', 'video')",
        )

    op.drop_index(
        "uq_salad_deployments_current",
        table_name="salad_deployments",
    )
    op.create_index(
        "uq_salad_deployments_current_purpose",
        "salad_deployments",
        ["purpose"],
        unique=True,
        postgresql_where=sa.text("is_current"),
        sqlite_where=sa.text("is_current = 1"),
    )

    op.create_table(
        "video_generation_jobs",
        sa.Column("source_asset_id", sa.Uuid(), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("source_storage_backend", sa.String(length=50), nullable=False),
        sa.Column("source_storage_bucket", sa.String(length=255), nullable=False),
        sa.Column("source_object_key", sa.String(length=1024), nullable=False),
        sa.Column("source_object_version_id", sa.String(length=1024), nullable=True),
        sa.Column("source_sha256", sa.String(length=64), nullable=False),
        sa.Column("source_content_type", sa.String(length=100), nullable=False),
        sa.Column("source_image_format", sa.String(length=20), nullable=False),
        sa.Column("source_width", sa.Integer(), nullable=False),
        sa.Column("source_height", sa.Integer(), nullable=False),
        sa.Column("source_byte_size", sa.BigInteger(), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("negative_prompt", sa.Text(), nullable=False),
        sa.Column("profile_key", sa.String(length=100), nullable=False),
        sa.Column("profile_version", sa.String(length=100), nullable=False),
        sa.Column("profile_sha256", sa.String(length=64), nullable=False),
        sa.Column("seed", sa.BigInteger(), nullable=False),
        sa.Column("frame_count", sa.Integer(), nullable=False),
        sa.Column("fps", sa.Integer(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("loop_mode", sa.String(length=9), nullable=False),
        sa.Column("content_rating", video_content_rating, nullable=False),
        sa.Column("attestation_policy_version", sa.String(length=50), nullable=False),
        sa.Column("source_rights_confirmed", sa.Boolean(), nullable=False),
        sa.Column("lawful_use_confirmed", sa.Boolean(), nullable=False),
        sa.Column("all_depicted_people_are_adults", sa.Boolean(), nullable=False),
        sa.Column("consensual_adult_content_confirmed", sa.Boolean(), nullable=False),
        sa.Column("no_real_person_sexual_content", sa.Boolean(), nullable=False),
        sa.Column("request_sha256", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("state", video_generation_state, nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("lock_version", sa.Integer(), nullable=False),
        sa.Column("lease_owner", sa.String(length=200), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=100), nullable=True),
        sa.Column("last_error_detail", sa.Text(), nullable=True),
        sa.Column("estimated_cost_microusd", sa.BigInteger(), nullable=False),
        sa.Column("reserved_cost_microusd", sa.BigInteger(), nullable=False),
        sa.Column("actual_cost_microusd", sa.BigInteger(), nullable=False),
        sa.Column("billed_duration_ms", sa.BigInteger(), nullable=False),
        sa.Column("cost_metadata", json_type, nullable=False),
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
            "source_width > 0",
            name=op.f("ck_video_generation_jobs_positive_source_width"),
        ),
        sa.CheckConstraint(
            "source_height > 0",
            name=op.f("ck_video_generation_jobs_positive_source_height"),
        ),
        sa.CheckConstraint(
            "source_byte_size > 0",
            name=op.f("ck_video_generation_jobs_positive_source_byte_size"),
        ),
        sa.CheckConstraint(
            "source_content_type LIKE 'image/%'",
            name=op.f("ck_video_generation_jobs_source_is_image"),
        ),
        sa.CheckConstraint(
            _lower_hex_check("source_sha256"),
            name=op.f("ck_video_generation_jobs_valid_source_sha256"),
        ),
        sa.CheckConstraint(
            _lower_hex_check("profile_sha256"),
            name=op.f("ck_video_generation_jobs_valid_profile_sha256"),
        ),
        sa.CheckConstraint(
            _lower_hex_check("request_sha256"),
            name=op.f("ck_video_generation_jobs_valid_request_sha256"),
        ),
        sa.CheckConstraint(
            "length(prompt) <= 4000 AND length(negative_prompt) <= 4000",
            name=op.f("ck_video_generation_jobs_bounded_prompts"),
        ),
        sa.CheckConstraint(
            "seed >= 0",
            name=op.f("ck_video_generation_jobs_nonnegative_seed"),
        ),
        sa.CheckConstraint(
            "frame_count IN (73, 121)",
            name=op.f("ck_video_generation_jobs_supported_frame_count"),
        ),
        sa.CheckConstraint(
            "fps = 24",
            name=op.f("ck_video_generation_jobs_fixed_fps"),
        ),
        sa.CheckConstraint(
            "(width = 832 AND height = 480) OR (width = 480 AND height = 832)",
            name=op.f("ck_video_generation_jobs_supported_dimensions"),
        ),
        sa.CheckConstraint(
            "loop_mode = 'ping_pong'",
            name=op.f("ck_video_generation_jobs_ping_pong_loop"),
        ),
        sa.CheckConstraint(
            "max_attempts > 0",
            name=op.f("ck_video_generation_jobs_positive_max_attempts"),
        ),
        sa.CheckConstraint(
            "attempt_count >= 0 AND attempt_count <= max_attempts",
            name=op.f("ck_video_generation_jobs_valid_attempt_count"),
        ),
        sa.CheckConstraint(
            "lock_version > 0",
            name=op.f("ck_video_generation_jobs_positive_lock_version"),
        ),
        sa.CheckConstraint(
            "(lease_owner IS NULL AND lease_expires_at IS NULL) OR "
            "(lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name=op.f("ck_video_generation_jobs_lease_pair"),
        ),
        sa.CheckConstraint(
            "source_rights_confirmed = true AND lawful_use_confirmed = true",
            name=op.f("ck_video_generation_jobs_base_attestations_required"),
        ),
        sa.CheckConstraint(
            "content_rating = 'sfw' OR ("
            "all_depicted_people_are_adults = true "
            "AND consensual_adult_content_confirmed = true "
            "AND no_real_person_sexual_content = true)",
            name=op.f("ck_video_generation_jobs_adult_attestations_required"),
        ),
        sa.CheckConstraint(
            "attestation_policy_version = 'video-compliance/v1'",
            name=op.f("ck_video_generation_jobs_known_attestation_policy"),
        ),
        sa.CheckConstraint(
            "estimated_cost_microusd >= 0 "
            "AND reserved_cost_microusd >= 0 "
            "AND actual_cost_microusd >= 0 "
            "AND billed_duration_ms >= 0",
            name=op.f("ck_video_generation_jobs_nonnegative_cost_summary"),
        ),
        sa.CheckConstraint(
            "(state IN ('succeeded', 'failed', 'cancelled') AND completed_at IS NOT NULL) "
            "OR (state NOT IN ('succeeded', 'failed', 'cancelled') "
            "AND completed_at IS NULL)",
            name=op.f("ck_video_generation_jobs_terminal_state_metadata"),
        ),
        sa.ForeignKeyConstraint(
            ["source_asset_id"],
            ["assets.id"],
            name=op.f("fk_video_generation_jobs_source_asset_id_assets"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["admin_users.id"],
            name=op.f("fk_video_generation_jobs_created_by_user_id_admin_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_video_generation_jobs")),
        sa.UniqueConstraint(
            "id",
            "source_asset_id",
            name="uq_video_generation_jobs_id_source_asset_id",
        ),
    )
    op.create_index(
        op.f("ix_video_generation_jobs_source_asset_id"),
        "video_generation_jobs",
        ["source_asset_id"],
    )
    op.create_index(
        op.f("ix_video_generation_jobs_created_by_user_id"),
        "video_generation_jobs",
        ["created_by_user_id"],
    )
    op.create_index(
        op.f("ix_video_generation_jobs_request_sha256"),
        "video_generation_jobs",
        ["request_sha256"],
    )
    op.create_index(
        "ix_video_generation_jobs_schedule",
        "video_generation_jobs",
        ["state", "retry_at", "priority", "created_at"],
    )

    op.create_table(
        "video_generation_attempts",
        sa.Column("video_generation_job_id", sa.Uuid(), nullable=False),
        sa.Column("salad_deployment_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("provider_external_id", sa.String(length=200), nullable=True),
        sa.Column("submission_key", sa.String(length=64), nullable=False),
        sa.Column("request_sha256", sa.String(length=64), nullable=False),
        sa.Column("state", video_generation_attempt_state, nullable=False),
        sa.Column("worker_image_digest", sa.String(length=255), nullable=False),
        sa.Column("request_metadata", json_type, nullable=False),
        sa.Column("response_metadata", json_type, nullable=True),
        sa.Column("submit_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider_state", sa.String(length=50), nullable=True),
        sa.Column("reserved_cost_microusd", sa.BigInteger(), nullable=False),
        sa.Column("actual_cost_microusd", sa.BigInteger(), nullable=False),
        sa.Column("billed_duration_ms", sa.BigInteger(), nullable=False),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
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
            "attempt_no > 0",
            name=op.f("ck_video_generation_attempts_positive_attempt_no"),
        ),
        sa.CheckConstraint(
            _lower_hex_check("request_sha256"),
            name=op.f("ck_video_generation_attempts_valid_request_sha256"),
        ),
        sa.CheckConstraint(
            "reserved_cost_microusd >= 0 AND actual_cost_microusd >= 0 AND billed_duration_ms >= 0",
            name=op.f("ck_video_generation_attempts_nonnegative_cost_summary"),
        ),
        sa.CheckConstraint(
            "state NOT IN "
            "('submitted', 'running', 'succeeded', 'cancel_requested', 'cancelled') "
            "OR provider_external_id IS NOT NULL",
            name=op.f("ck_video_generation_attempts_remote_state_has_provider_id"),
        ),
        sa.CheckConstraint(
            "state NOT IN ('succeeded', 'failed', 'cancelled') OR completed_at IS NOT NULL",
            name=op.f("ck_video_generation_attempts_terminal_attempt_is_completed"),
        ),
        sa.ForeignKeyConstraint(
            ["video_generation_job_id"],
            ["video_generation_jobs.id"],
            name=op.f("fk_video_generation_attempts_video_generation_job_id_video_generation_jobs"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["salad_deployment_id"],
            ["salad_deployments.id"],
            name=op.f("fk_video_generation_attempts_salad_deployment_id_salad_deployments"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_video_generation_attempts")),
        sa.UniqueConstraint(
            "video_generation_job_id",
            "attempt_no",
            name=op.f("uq_video_generation_attempts_video_generation_job_id"),
        ),
        sa.UniqueConstraint(
            "id",
            "video_generation_job_id",
            name="uq_video_generation_attempts_id_job_id",
        ),
        sa.UniqueConstraint(
            "provider",
            "provider_external_id",
            name="uq_video_generation_attempts_provider_external_id",
        ),
        sa.UniqueConstraint(
            "provider",
            "submission_key",
            name="uq_video_generation_attempts_submission_key",
        ),
    )
    op.create_index(
        op.f("ix_video_generation_attempts_video_generation_job_id"),
        "video_generation_attempts",
        ["video_generation_job_id"],
    )
    op.create_index(
        op.f("ix_video_generation_attempts_salad_deployment_id"),
        "video_generation_attempts",
        ["salad_deployment_id"],
    )
    op.create_index(
        "ix_video_generation_attempts_observe",
        "video_generation_attempts",
        ["state", "last_observed_at"],
    )

    op.create_table(
        "video_generation_outputs",
        sa.Column("video_generation_job_id", sa.Uuid(), nullable=False),
        sa.Column("successful_attempt_id", sa.Uuid(), nullable=False),
        sa.Column("source_asset_id", sa.Uuid(), nullable=False),
        sa.Column("asset_id", sa.Uuid(), nullable=False),
        sa.Column("asset_lineage_id", sa.Uuid(), nullable=False),
        sa.Column("storage_backend", sa.String(length=50), nullable=False),
        sa.Column("storage_bucket", sa.String(length=255), nullable=False),
        sa.Column("object_key", sa.String(length=1024), nullable=False),
        sa.Column("object_version_id", sa.String(length=1024), nullable=True),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("content_type", sa.String(length=100), nullable=False),
        sa.Column("video_format", sa.String(length=20), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("frame_count", sa.Integer(), nullable=False),
        sa.Column("fps", sa.Integer(), nullable=False),
        sa.Column("duration_ms", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            _lower_hex_check("sha256"),
            name=op.f("ck_video_generation_outputs_valid_sha256"),
        ),
        sa.CheckConstraint(
            "content_type LIKE 'video/%'",
            name=op.f("ck_video_generation_outputs_output_is_video"),
        ),
        sa.CheckConstraint(
            "width > 0",
            name=op.f("ck_video_generation_outputs_positive_width"),
        ),
        sa.CheckConstraint(
            "height > 0",
            name=op.f("ck_video_generation_outputs_positive_height"),
        ),
        sa.CheckConstraint(
            "byte_size > 0",
            name=op.f("ck_video_generation_outputs_positive_byte_size"),
        ),
        sa.CheckConstraint(
            "frame_count > 0",
            name=op.f("ck_video_generation_outputs_positive_frame_count"),
        ),
        sa.CheckConstraint(
            "fps > 0",
            name=op.f("ck_video_generation_outputs_positive_fps"),
        ),
        sa.CheckConstraint(
            "duration_ms > 0",
            name=op.f("ck_video_generation_outputs_positive_duration"),
        ),
        sa.ForeignKeyConstraint(
            ["successful_attempt_id", "video_generation_job_id"],
            [
                "video_generation_attempts.id",
                "video_generation_attempts.video_generation_job_id",
            ],
            name="fk_video_generation_outputs_successful_attempt_job",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["video_generation_job_id", "source_asset_id"],
            ["video_generation_jobs.id", "video_generation_jobs.source_asset_id"],
            name="fk_video_generation_outputs_job_source_asset",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_asset_id"],
            ["assets.id"],
            name=op.f("fk_video_generation_outputs_source_asset_id_assets"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["asset_id"],
            ["assets.id"],
            name=op.f("fk_video_generation_outputs_asset_id_assets"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["asset_lineage_id"],
            ["asset_lineage.id"],
            name=op.f("fk_video_generation_outputs_asset_lineage_id_asset_lineage"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_video_generation_outputs")),
        sa.UniqueConstraint(
            "video_generation_job_id",
            name=op.f("uq_video_generation_outputs_video_generation_job_id"),
        ),
        sa.UniqueConstraint(
            "successful_attempt_id",
            name=op.f("uq_video_generation_outputs_successful_attempt_id"),
        ),
        sa.UniqueConstraint(
            "asset_id",
            name=op.f("uq_video_generation_outputs_asset_id"),
        ),
        sa.UniqueConstraint(
            "asset_lineage_id",
            name=op.f("uq_video_generation_outputs_asset_lineage_id"),
        ),
    )
    op.create_index(
        op.f("ix_video_generation_outputs_source_asset_id"),
        "video_generation_outputs",
        ["source_asset_id"],
    )


def downgrade() -> None:
    connection = op.get_bind()
    video_deployment_count = int(
        connection.execute(
            sa.text("SELECT COUNT(*) FROM salad_deployments WHERE purpose = 'video'")
        ).scalar_one()
    )
    video_job_count = int(
        connection.execute(sa.text("SELECT COUNT(*) FROM video_generation_jobs")).scalar_one()
    )
    if video_deployment_count or video_job_count:
        raise RuntimeError(
            "cannot downgrade Animation Studio after durable video deployment or job data exists"
        )

    op.drop_index(
        op.f("ix_video_generation_outputs_source_asset_id"),
        table_name="video_generation_outputs",
    )
    op.drop_table("video_generation_outputs")

    op.drop_index(
        "ix_video_generation_attempts_observe",
        table_name="video_generation_attempts",
    )
    op.drop_index(
        op.f("ix_video_generation_attempts_salad_deployment_id"),
        table_name="video_generation_attempts",
    )
    op.drop_index(
        op.f("ix_video_generation_attempts_video_generation_job_id"),
        table_name="video_generation_attempts",
    )
    op.drop_table("video_generation_attempts")

    op.drop_index("ix_video_generation_jobs_schedule", table_name="video_generation_jobs")
    op.drop_index(
        op.f("ix_video_generation_jobs_request_sha256"),
        table_name="video_generation_jobs",
    )
    op.drop_index(
        op.f("ix_video_generation_jobs_created_by_user_id"),
        table_name="video_generation_jobs",
    )
    op.drop_index(
        op.f("ix_video_generation_jobs_source_asset_id"),
        table_name="video_generation_jobs",
    )
    op.drop_table("video_generation_jobs")

    op.drop_index(
        "uq_salad_deployments_current_purpose",
        table_name="salad_deployments",
    )
    op.create_index(
        "uq_salad_deployments_current",
        "salad_deployments",
        ["is_current"],
        unique=True,
        postgresql_where=sa.text("is_current"),
        sqlite_where=sa.text("is_current = 1"),
    )
    with op.batch_alter_table("salad_deployments") as batch_op:
        batch_op.drop_constraint(
            op.f("ck_salad_deployments_salad_deployment_purpose"),
            type_="check",
        )
        batch_op.drop_column("administrative_stop_reason")
        batch_op.drop_column("purpose")
