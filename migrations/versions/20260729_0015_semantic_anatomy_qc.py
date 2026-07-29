"""Add durable, failure-safe semantic anatomy assessments.

Revision ID: 20260729_0015
Revises: 20260729_0014
Create Date: 2026-07-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260729_0015"
down_revision: str | None = "20260729_0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
semantic_assessment_state = sa.Enum(
    "pending",
    "processing",
    "retry_wait",
    "completed",
    "unavailable",
    name="semantic_assessment_state",
    native_enum=False,
    create_constraint=True,
)
semantic_verdict = sa.Enum(
    "pass",
    "review",
    "severe",
    name="semantic_verdict",
    native_enum=False,
    create_constraint=True,
)


def upgrade() -> None:
    op.create_table(
        "semantic_assessments",
        sa.Column("scoring_run_id", sa.Uuid(), nullable=False),
        sa.Column("asset_score_id", sa.Uuid(), nullable=False),
        sa.Column("asset_id", sa.Uuid(), nullable=False),
        sa.Column("asset_storage_backend", sa.String(length=50), nullable=False),
        sa.Column("asset_storage_bucket", sa.String(length=255), nullable=False),
        sa.Column("asset_object_key", sa.String(length=1024), nullable=False),
        sa.Column("asset_object_version_id", sa.String(length=1024), nullable=False),
        sa.Column("asset_sha256", sa.String(length=64), nullable=False),
        sa.Column("asset_content_type", sa.String(length=100), nullable=False),
        sa.Column("asset_byte_size", sa.BigInteger(), nullable=False),
        sa.Column("profile_sha256", sa.String(length=64), nullable=False),
        sa.Column("model_name", sa.String(length=200), nullable=False),
        sa.Column("model_revision", sa.String(length=200), nullable=False),
        sa.Column("prompt_sha256", sa.String(length=64), nullable=False),
        sa.Column("schema_sha256", sa.String(length=64), nullable=False),
        sa.Column("state", semantic_assessment_state, nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_owner", sa.String(length=200), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verdict", semantic_verdict, nullable=True),
        sa.Column("confidence_micros", sa.Integer(), nullable=True),
        sa.Column("issues", json_type, nullable=True),
        sa.Column("response_sha256", sa.String(length=64), nullable=True),
        sa.Column("last_error_code", sa.String(length=100), nullable=True),
        sa.Column("last_error_detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "attempts >= 0",
            name=op.f("ck_semantic_assessments_nonnegative_attempts"),
        ),
        sa.CheckConstraint(
            "max_attempts > 0",
            name=op.f("ck_semantic_assessments_positive_max_attempts"),
        ),
        sa.CheckConstraint(
            "attempts <= max_attempts",
            name=op.f("ck_semantic_assessments_attempts_within_limit"),
        ),
        sa.CheckConstraint(
            "asset_byte_size > 0",
            name=op.f("ck_semantic_assessments_positive_asset_byte_size"),
        ),
        sa.CheckConstraint(
            "length(asset_sha256) = 64",
            name=op.f("ck_semantic_assessments_valid_asset_sha256"),
        ),
        sa.CheckConstraint(
            "length(profile_sha256) = 64",
            name=op.f("ck_semantic_assessments_valid_profile_sha256"),
        ),
        sa.CheckConstraint(
            "length(prompt_sha256) = 64",
            name=op.f("ck_semantic_assessments_valid_prompt_sha256"),
        ),
        sa.CheckConstraint(
            "length(schema_sha256) = 64",
            name=op.f("ck_semantic_assessments_valid_schema_sha256"),
        ),
        sa.CheckConstraint(
            "(state = 'processing' "
            "AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL) "
            "OR (state <> 'processing' "
            "AND lease_owner IS NULL AND lease_expires_at IS NULL)",
            name=op.f("ck_semantic_assessments_lease_state"),
        ),
        sa.CheckConstraint(
            "(state = 'completed' "
            "AND verdict IS NOT NULL "
            "AND confidence_micros IS NOT NULL "
            "AND issues IS NOT NULL "
            "AND response_sha256 IS NOT NULL "
            "AND completed_at IS NOT NULL "
            "AND last_error_code IS NULL) "
            "OR (state = 'unavailable' "
            "AND verdict IS NULL "
            "AND confidence_micros IS NULL "
            "AND issues IS NULL "
            "AND response_sha256 IS NULL "
            "AND completed_at IS NOT NULL "
            "AND last_error_code IS NOT NULL) "
            "OR (state NOT IN ('completed', 'unavailable') "
            "AND verdict IS NULL "
            "AND confidence_micros IS NULL "
            "AND issues IS NULL "
            "AND response_sha256 IS NULL "
            "AND completed_at IS NULL)",
            name=op.f("ck_semantic_assessments_result_state"),
        ),
        sa.CheckConstraint(
            "confidence_micros IS NULL OR confidence_micros BETWEEN 0 AND 1000000",
            name=op.f("ck_semantic_assessments_valid_confidence"),
        ),
        sa.ForeignKeyConstraint(
            ["asset_id"],
            ["assets.id"],
            name=op.f("fk_semantic_assessments_asset_id_assets"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["asset_score_id"],
            ["asset_scores.id"],
            name=op.f("fk_semantic_assessments_asset_score_id_asset_scores"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["scoring_run_id"],
            ["scoring_runs.id"],
            name=op.f("fk_semantic_assessments_scoring_run_id_scoring_runs"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["scoring_run_id", "asset_id"],
            ["asset_scores.scoring_run_id", "asset_scores.asset_id"],
            name="fk_semantic_assessments_score_snapshot",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_semantic_assessments")),
        sa.UniqueConstraint(
            "scoring_run_id",
            "asset_id",
            "profile_sha256",
            name="uq_semantic_assessments_run_asset_profile",
        ),
    )
    op.create_index(
        "ix_semantic_assessments_claim",
        "semantic_assessments",
        ["state", "available_at", "lease_expires_at", "created_at"],
    )
    op.create_index(
        op.f("ix_semantic_assessments_asset_id"),
        "semantic_assessments",
        ["asset_id"],
    )
    op.create_index(
        op.f("ix_semantic_assessments_asset_score_id"),
        "semantic_assessments",
        ["asset_score_id"],
    )
    op.create_index(
        op.f("ix_semantic_assessments_scoring_run_id"),
        "semantic_assessments",
        ["scoring_run_id"],
    )
    if op.get_bind().dialect.name == "sqlite":
        op.execute(sa.text(_SQLITE_TERMINAL_UPDATE_TRIGGER))
        op.execute(sa.text(_SQLITE_DELETE_TRIGGER))
    else:
        op.execute(sa.text(_POSTGRES_GUARD_FUNCTION))
        op.execute(sa.text(_POSTGRES_GUARD_TRIGGER))


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        op.execute(sa.text("DROP TRIGGER IF EXISTS semantic_assessments_guard_terminal_update"))
        op.execute(sa.text("DROP TRIGGER IF EXISTS semantic_assessments_guard_delete"))
    else:
        op.execute(
            sa.text(
                "DROP TRIGGER IF EXISTS semantic_assessments_guard_mutation ON semantic_assessments"
            )
        )
        op.execute(
            sa.text("DROP FUNCTION IF EXISTS gen_automation_guard_semantic_assessment_mutation()")
        )
    op.drop_index(
        op.f("ix_semantic_assessments_scoring_run_id"),
        table_name="semantic_assessments",
    )
    op.drop_index(
        op.f("ix_semantic_assessments_asset_score_id"),
        table_name="semantic_assessments",
    )
    op.drop_index(
        op.f("ix_semantic_assessments_asset_id"),
        table_name="semantic_assessments",
    )
    op.drop_index("ix_semantic_assessments_claim", table_name="semantic_assessments")
    op.drop_table("semantic_assessments")


_SQLITE_TERMINAL_UPDATE_TRIGGER = """
CREATE TRIGGER semantic_assessments_guard_terminal_update
BEFORE UPDATE ON semantic_assessments
WHEN OLD.state IN ('completed', 'unavailable')
BEGIN
    SELECT RAISE(ABORT, 'terminal semantic assessments are immutable');
END
"""

_SQLITE_DELETE_TRIGGER = """
CREATE TRIGGER semantic_assessments_guard_delete
BEFORE DELETE ON semantic_assessments
BEGIN
    SELECT RAISE(ABORT, 'semantic assessments cannot be deleted');
END
"""

_POSTGRES_GUARD_FUNCTION = """
CREATE OR REPLACE FUNCTION gen_automation_guard_semantic_assessment_mutation()
RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'semantic assessments cannot be deleted';
    END IF;
    IF OLD.state IN ('completed', 'unavailable') THEN
        RAISE EXCEPTION 'terminal semantic assessments are immutable';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""

_POSTGRES_GUARD_TRIGGER = """
CREATE TRIGGER semantic_assessments_guard_mutation
BEFORE UPDATE OR DELETE ON semantic_assessments
FOR EACH ROW
EXECUTE FUNCTION gen_automation_guard_semantic_assessment_mutation()
"""
