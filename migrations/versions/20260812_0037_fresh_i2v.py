"""Add the clean, isolated image-to-video queue.

Revision ID: 20260812_0037
Revises: 20260811_0036
Create Date: 2026-08-12

The retired ``video_generation_*`` tables are intentionally left untouched.  This
revision creates a new namespace so the new worker contract does not inherit any of
the former pipeline's policy or runtime ceilings.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260812_0037"
down_revision: str | None = "20260811_0036"
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
    op.create_table(
        "i2v_inputs",
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("source", sa.String(length=10), nullable=False),
        sa.Column("asset_id", sa.Uuid(), nullable=True),
        sa.Column("display_name", sa.String(length=500), nullable=False),
        sa.Column("storage_backend", sa.String(length=50), nullable=False),
        sa.Column("storage_bucket", sa.String(length=255), nullable=False),
        sa.Column("object_key", sa.String(length=1024), nullable=False),
        sa.Column("object_version_id", sa.String(length=1024), nullable=True),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("content_type", sa.String(length=100), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("metadata", json_type, nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "source IN ('upload', 'generation')",
            name=op.f("ck_i2v_inputs_known_source"),
        ),
        sa.CheckConstraint(
            "source <> 'generation' OR asset_id IS NOT NULL",
            name=op.f("ck_i2v_inputs_generation_has_asset"),
        ),
        sa.CheckConstraint(
            "content_type LIKE 'image/%'",
            name=op.f("ck_i2v_inputs_image_content"),
        ),
        sa.CheckConstraint(
            "width > 0 AND height > 0 AND byte_size > 0",
            name=op.f("ck_i2v_inputs_positive_dimensions_and_size"),
        ),
        sa.CheckConstraint(
            _lower_hex_check("sha256"),
            name=op.f("ck_i2v_inputs_valid_sha256"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["admin_users.id"],
            name=op.f("fk_i2v_inputs_created_by_user_id_admin_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["asset_id"],
            ["assets.id"],
            name=op.f("fk_i2v_inputs_asset_id_assets"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_i2v_inputs")),
        sa.UniqueConstraint(
            "storage_backend",
            "storage_bucket",
            "object_key",
            "object_version_id",
            name="uq_i2v_inputs_frozen_object",
        ),
    )
    op.create_index(
        op.f("ix_i2v_inputs_created_by_user_id"),
        "i2v_inputs",
        ["created_by_user_id"],
    )
    op.create_index(op.f("ix_i2v_inputs_asset_id"), "i2v_inputs", ["asset_id"])
    op.create_index("ix_i2v_inputs_recent", "i2v_inputs", ["created_at", "id"])
    op.create_index(
        "uq_i2v_inputs_unversioned_object",
        "i2v_inputs",
        ["storage_backend", "storage_bucket", "object_key"],
        unique=True,
        postgresql_where=sa.text("object_version_id IS NULL"),
        sqlite_where=sa.text("object_version_id IS NULL"),
    )

    op.create_table(
        "i2v_presets",
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=500), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("positive_prompt", sa.Text(), nullable=False),
        sa.Column("negative_prompt", sa.Text(), nullable=False),
        sa.Column("settings", json_type, nullable=False),
        sa.Column("lock_version", sa.BigInteger(), nullable=False),
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
            "lock_version > 0",
            name=op.f("ck_i2v_presets_positive_lock_version"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["admin_users.id"],
            name=op.f("fk_i2v_presets_created_by_user_id_admin_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_i2v_presets")),
        sa.UniqueConstraint(
            "created_by_user_id",
            "name",
            name=op.f("uq_i2v_presets_created_by_user_id"),
        ),
    )
    op.create_index(
        op.f("ix_i2v_presets_created_by_user_id"),
        "i2v_presets",
        ["created_by_user_id"],
    )

    op.create_table(
        "i2v_worker_deployments",
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("provider_group_id", sa.String(length=255), nullable=True),
        sa.Column("provider_instance_id", sa.String(length=255), nullable=True),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("gpu_class", sa.String(length=100), nullable=False),
        sa.Column("worker_image_digest", sa.String(length=255), nullable=False),
        sa.Column("current_job_id", sa.Uuid(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ready_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata", json_type, nullable=False),
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
            "state IN ('stopped', 'provisioning', 'starting', 'ready', "
            "'busy', 'draining', 'failed')",
            name=op.f("ck_i2v_worker_deployments_known_state"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_i2v_worker_deployments")),
        sa.UniqueConstraint(
            "provider",
            "provider_group_id",
            name=op.f("uq_i2v_worker_deployments_provider"),
        ),
    )
    op.create_index(
        "ix_i2v_worker_deployments_state",
        "i2v_worker_deployments",
        ["state", "updated_at"],
    )

    op.create_table(
        "i2v_jobs",
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("input_id", sa.Uuid(), nullable=False),
        sa.Column("preset_id", sa.Uuid(), nullable=True),
        sa.Column("positive_prompt", sa.Text(), nullable=False),
        sa.Column("negative_prompt", sa.Text(), nullable=False),
        sa.Column("input_snapshot", json_type, nullable=False),
        sa.Column("preset_snapshot", json_type, nullable=False),
        sa.Column("settings_snapshot", json_type, nullable=False),
        sa.Column("request_sha256", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("queue_position", sa.BigInteger(), nullable=True),
        sa.Column("attempt_count", sa.BigInteger(), nullable=False),
        sa.Column("lease_owner", sa.String(length=255), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
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
            "state IN ('queued', 'claimed', 'running', 'cancel_requested', "
            "'succeeded', 'failed', 'cancelled')",
            name=op.f("ck_i2v_jobs_known_state"),
        ),
        sa.CheckConstraint(
            "(state = 'queued' AND queue_position IS NOT NULL AND queue_position > 0) "
            "OR (state <> 'queued' AND queue_position IS NULL)",
            name=op.f("ck_i2v_jobs_queue_position_matches_state"),
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name=op.f("ck_i2v_jobs_nonnegative_attempt_count"),
        ),
        sa.CheckConstraint(
            "(lease_owner IS NULL AND lease_expires_at IS NULL) OR "
            "(lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name=op.f("ck_i2v_jobs_lease_pair"),
        ),
        sa.CheckConstraint(
            "(state IN ('succeeded', 'failed', 'cancelled') AND completed_at IS NOT NULL) "
            "OR (state NOT IN ('succeeded', 'failed', 'cancelled') AND completed_at IS NULL)",
            name=op.f("ck_i2v_jobs_terminal_completion"),
        ),
        sa.CheckConstraint(
            _lower_hex_check("request_sha256"),
            name=op.f("ck_i2v_jobs_valid_request_sha256"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["admin_users.id"],
            name=op.f("fk_i2v_jobs_created_by_user_id_admin_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["input_id"],
            ["i2v_inputs.id"],
            name=op.f("fk_i2v_jobs_input_id_i2v_inputs"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["preset_id"],
            ["i2v_presets.id"],
            name=op.f("fk_i2v_jobs_preset_id_i2v_presets"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_i2v_jobs")),
    )
    op.create_index(
        op.f("ix_i2v_jobs_created_by_user_id"),
        "i2v_jobs",
        ["created_by_user_id"],
    )
    op.create_index(op.f("ix_i2v_jobs_input_id"), "i2v_jobs", ["input_id"])
    op.create_index(op.f("ix_i2v_jobs_preset_id"), "i2v_jobs", ["preset_id"])
    op.create_index(
        "uq_i2v_jobs_queue_position",
        "i2v_jobs",
        ["queue_position"],
        unique=True,
        postgresql_where=sa.text("queue_position IS NOT NULL"),
        sqlite_where=sa.text("queue_position IS NOT NULL"),
    )
    op.create_index(
        "ix_i2v_jobs_queue",
        "i2v_jobs",
        ["state", "queue_position", "created_at", "id"],
    )
    op.create_index(
        "ix_i2v_jobs_recent",
        "i2v_jobs",
        ["created_by_user_id", "created_at", "id"],
    )

    op.create_table(
        "i2v_attempts",
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("worker_deployment_id", sa.Uuid(), nullable=True),
        sa.Column("attempt_no", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("worker_id", sa.String(length=255), nullable=True),
        sa.Column("worker_image_digest", sa.String(length=255), nullable=True),
        sa.Column("provider_job_id", sa.String(length=255), nullable=True),
        sa.Column("request_metadata", json_type, nullable=False),
        sa.Column("response_metadata", json_type, nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
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
            name=op.f("ck_i2v_attempts_positive_attempt_no"),
        ),
        sa.CheckConstraint(
            "state IN ('created', 'running', 'succeeded', 'failed', 'cancelled')",
            name=op.f("ck_i2v_attempts_known_state"),
        ),
        sa.CheckConstraint(
            "state NOT IN ('succeeded', 'failed', 'cancelled') OR completed_at IS NOT NULL",
            name=op.f("ck_i2v_attempts_terminal_completion"),
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["i2v_jobs.id"],
            name=op.f("fk_i2v_attempts_job_id_i2v_jobs"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["worker_deployment_id"],
            ["i2v_worker_deployments.id"],
            name=op.f("fk_i2v_attempts_worker_deployment_id_i2v_worker_deployments"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_i2v_attempts")),
        sa.UniqueConstraint(
            "job_id",
            "attempt_no",
            name=op.f("uq_i2v_attempts_job_id"),
        ),
        sa.UniqueConstraint(
            "id",
            "job_id",
            name="uq_i2v_attempts_id_job_id",
        ),
        sa.UniqueConstraint(
            "worker_deployment_id",
            "provider_job_id",
            name="uq_i2v_attempts_provider_job",
        ),
    )
    op.create_index(op.f("ix_i2v_attempts_job_id"), "i2v_attempts", ["job_id"])
    op.create_index(
        op.f("ix_i2v_attempts_worker_deployment_id"),
        "i2v_attempts",
        ["worker_deployment_id"],
    )
    op.create_index(
        "ix_i2v_attempts_state",
        "i2v_attempts",
        ["state", "updated_at"],
    )

    op.create_table(
        "i2v_outputs",
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_id", sa.Uuid(), nullable=False),
        sa.Column("storage_backend", sa.String(length=50), nullable=False),
        sa.Column("storage_bucket", sa.String(length=255), nullable=False),
        sa.Column("object_key", sa.String(length=1024), nullable=False),
        sa.Column("object_version_id", sa.String(length=1024), nullable=True),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("content_type", sa.String(length=100), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("frame_count", sa.BigInteger(), nullable=False),
        sa.Column("fps", sa.Float(), nullable=False),
        sa.Column("duration_ms", sa.BigInteger(), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("metadata", json_type, nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            _lower_hex_check("sha256"),
            name=op.f("ck_i2v_outputs_valid_sha256"),
        ),
        sa.CheckConstraint(
            "content_type LIKE 'video/%'",
            name=op.f("ck_i2v_outputs_video_content"),
        ),
        sa.CheckConstraint(
            "width > 0 AND height > 0 AND frame_count > 0 AND fps > 0 "
            "AND duration_ms > 0 AND byte_size > 0",
            name=op.f("ck_i2v_outputs_positive_media_values"),
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id", "job_id"],
            ["i2v_attempts.id", "i2v_attempts.job_id"],
            name="fk_i2v_outputs_attempt_job",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_i2v_outputs")),
        sa.UniqueConstraint(
            "attempt_id",
            name=op.f("uq_i2v_outputs_attempt_id"),
        ),
        sa.UniqueConstraint(
            "storage_backend",
            "storage_bucket",
            "object_key",
            "object_version_id",
            name="uq_i2v_outputs_object",
        ),
    )
    op.create_index(op.f("ix_i2v_outputs_job_id"), "i2v_outputs", ["job_id"])
    op.create_index(
        "ix_i2v_outputs_recent",
        "i2v_outputs",
        ["created_at", "id"],
    )
    op.create_index(
        "uq_i2v_outputs_unversioned_object",
        "i2v_outputs",
        ["storage_backend", "storage_bucket", "object_key"],
        unique=True,
        postgresql_where=sa.text("object_version_id IS NULL"),
        sqlite_where=sa.text("object_version_id IS NULL"),
    )

    _create_job_snapshot_guards(op.get_bind().dialect.name)


def downgrade() -> None:
    _drop_job_snapshot_guards(op.get_bind().dialect.name)
    op.drop_index("uq_i2v_outputs_unversioned_object", table_name="i2v_outputs")
    op.drop_index("ix_i2v_outputs_recent", table_name="i2v_outputs")
    op.drop_index(op.f("ix_i2v_outputs_job_id"), table_name="i2v_outputs")
    op.drop_table("i2v_outputs")
    op.drop_index("ix_i2v_attempts_state", table_name="i2v_attempts")
    op.drop_index(
        op.f("ix_i2v_attempts_worker_deployment_id"),
        table_name="i2v_attempts",
    )
    op.drop_index(op.f("ix_i2v_attempts_job_id"), table_name="i2v_attempts")
    op.drop_table("i2v_attempts")
    op.drop_index("ix_i2v_jobs_recent", table_name="i2v_jobs")
    op.drop_index("ix_i2v_jobs_queue", table_name="i2v_jobs")
    op.drop_index("uq_i2v_jobs_queue_position", table_name="i2v_jobs")
    op.drop_index(op.f("ix_i2v_jobs_preset_id"), table_name="i2v_jobs")
    op.drop_index(op.f("ix_i2v_jobs_input_id"), table_name="i2v_jobs")
    op.drop_index(op.f("ix_i2v_jobs_created_by_user_id"), table_name="i2v_jobs")
    op.drop_table("i2v_jobs")
    op.drop_index(
        "ix_i2v_worker_deployments_state",
        table_name="i2v_worker_deployments",
    )
    op.drop_table("i2v_worker_deployments")
    op.drop_index(op.f("ix_i2v_presets_created_by_user_id"), table_name="i2v_presets")
    op.drop_table("i2v_presets")
    op.drop_index("uq_i2v_inputs_unversioned_object", table_name="i2v_inputs")
    op.drop_index("ix_i2v_inputs_recent", table_name="i2v_inputs")
    op.drop_index(op.f("ix_i2v_inputs_asset_id"), table_name="i2v_inputs")
    op.drop_index(op.f("ix_i2v_inputs_created_by_user_id"), table_name="i2v_inputs")
    op.drop_table("i2v_inputs")


def _create_job_snapshot_guards(dialect_name: str) -> None:
    if dialect_name == "sqlite":
        op.execute(
            sa.text(
                "CREATE TRIGGER i2v_jobs_immutable_request "
                "BEFORE UPDATE ON i2v_jobs BEGIN SELECT CASE WHEN "
                "OLD.id IS NOT NEW.id OR OLD.created_by_user_id IS NOT NEW.created_by_user_id "
                "OR OLD.input_id IS NOT NEW.input_id "
                "OR OLD.positive_prompt IS NOT NEW.positive_prompt "
                "OR OLD.negative_prompt IS NOT NEW.negative_prompt "
                "OR OLD.input_snapshot IS NOT NEW.input_snapshot "
                "OR OLD.preset_snapshot IS NOT NEW.preset_snapshot "
                "OR OLD.settings_snapshot IS NOT NEW.settings_snapshot "
                "OR OLD.request_sha256 IS NOT NEW.request_sha256 "
                "OR OLD.created_at IS NOT NEW.created_at "
                "THEN RAISE(ABORT, 'i2v job request snapshots are immutable') END; END"
            )
        )
        return
    if dialect_name == "postgresql":
        op.execute(
            sa.text(
                "CREATE FUNCTION guard_i2v_job_request() RETURNS trigger AS $$ BEGIN "
                "IF OLD.id IS DISTINCT FROM NEW.id "
                "OR OLD.created_by_user_id IS DISTINCT FROM NEW.created_by_user_id "
                "OR OLD.input_id IS DISTINCT FROM NEW.input_id "
                "OR OLD.positive_prompt IS DISTINCT FROM NEW.positive_prompt "
                "OR OLD.negative_prompt IS DISTINCT FROM NEW.negative_prompt "
                "OR OLD.input_snapshot IS DISTINCT FROM NEW.input_snapshot "
                "OR OLD.preset_snapshot IS DISTINCT FROM NEW.preset_snapshot "
                "OR OLD.settings_snapshot IS DISTINCT FROM NEW.settings_snapshot "
                "OR OLD.request_sha256 IS DISTINCT FROM NEW.request_sha256 "
                "OR OLD.created_at IS DISTINCT FROM NEW.created_at THEN "
                "RAISE EXCEPTION 'i2v job request snapshots are immutable'; END IF; "
                "RETURN NEW; END; $$ LANGUAGE plpgsql"
            )
        )
        op.execute(
            sa.text(
                "CREATE TRIGGER i2v_jobs_immutable_request BEFORE UPDATE ON i2v_jobs "
                "FOR EACH ROW EXECUTE FUNCTION guard_i2v_job_request()"
            )
        )


def _drop_job_snapshot_guards(dialect_name: str) -> None:
    if dialect_name == "sqlite":
        op.execute(sa.text("DROP TRIGGER IF EXISTS i2v_jobs_immutable_request"))
        return
    if dialect_name == "postgresql":
        op.execute(sa.text("DROP TRIGGER IF EXISTS i2v_jobs_immutable_request ON i2v_jobs"))
        op.execute(sa.text("DROP FUNCTION IF EXISTS guard_i2v_job_request()"))
