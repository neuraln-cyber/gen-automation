"""Add immutable semantic anatomy feedback and calibration artifacts.

Revision ID: 20260803_0017
Revises: 20260803_0016
Create Date: 2026-08-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260803_0017"
down_revision: str | None = "20260803_0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


semantic_feedback_agreement = sa.Enum(
    "correct",
    "incorrect",
    "unsure",
    name="semantic_feedback_agreement",
    native_enum=False,
)
semantic_ground_truth = sa.Enum(
    "anatomy_good",
    "anatomy_defect",
    "unjudgeable",
    name="semantic_ground_truth",
    native_enum=False,
)
semantic_issue_code = sa.Enum(
    "extra_finger",
    "missing_finger",
    "malformed_hand",
    "extra_toe",
    "missing_toe",
    "malformed_foot",
    "extra_limb",
    "missing_limb",
    "duplicate_body_part",
    "impossible_joint",
    "implausible_proportion",
    "severe_face_deformation",
    name="semantic_issue_code",
    native_enum=False,
)


def upgrade() -> None:
    op.create_index(
        "uq_semantic_assessments_feedback_identity",
        "semantic_assessments",
        ["id", "asset_id", "profile_sha256"],
        unique=True,
    )

    op.create_table(
        "semantic_anatomy_feedback",
        sa.Column("semantic_assessment_id", sa.Uuid(), nullable=False),
        sa.Column("asset_id", sa.Uuid(), nullable=False),
        sa.Column("profile_sha256", sa.String(length=64), nullable=False),
        sa.Column("feedback_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("agreement", semantic_feedback_agreement, nullable=False),
        sa.Column("ground_truth", semantic_ground_truth, nullable=False),
        sa.Column("issue_code", semantic_issue_code, nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "ground_truth = 'anatomy_defect' OR issue_code IS NULL",
            name=op.f("ck_semantic_anatomy_feedback_issue_requires_defect"),
        ),
        sa.CheckConstraint(
            "(ground_truth = 'unjudgeable' AND agreement = 'unsure') "
            "OR (ground_truth <> 'unjudgeable' AND agreement <> 'unsure')",
            name=op.f("ck_semantic_anatomy_feedback_unjudgeable_agreement"),
        ),
        sa.CheckConstraint(
            "note IS NULL OR length(trim(note)) > 0",
            name=op.f("ck_semantic_anatomy_feedback_nonempty_note"),
        ),
        sa.ForeignKeyConstraint(
            ["asset_id"],
            ["assets.id"],
            name=op.f("fk_semantic_anatomy_feedback_asset_id_assets"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["feedback_by_user_id"],
            ["admin_users.id"],
            name=op.f("fk_semantic_anatomy_feedback_feedback_by_user_id_admin_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["semantic_assessment_id", "asset_id", "profile_sha256"],
            [
                "semantic_assessments.id",
                "semantic_assessments.asset_id",
                "semantic_assessments.profile_sha256",
            ],
            name="fk_semantic_anatomy_feedback_assessment_identity",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_semantic_anatomy_feedback")),
        sa.UniqueConstraint(
            "semantic_assessment_id",
            "feedback_by_user_id",
            name="uq_semantic_anatomy_feedback_assessment_user",
        ),
    )
    op.create_index(
        op.f("ix_semantic_anatomy_feedback_asset_id"),
        "semantic_anatomy_feedback",
        ["asset_id"],
    )
    op.create_index(
        op.f("ix_semantic_anatomy_feedback_feedback_by_user_id"),
        "semantic_anatomy_feedback",
        ["feedback_by_user_id"],
    )
    op.create_index(
        "ix_semantic_anatomy_feedback_profile_created",
        "semantic_anatomy_feedback",
        ["profile_sha256", "created_at"],
    )

    op.create_table(
        "semantic_calibration_artifacts",
        sa.Column("profile_sha256", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("calibration_schema_version", sa.String(length=100), nullable=False),
        sa.Column("dataset_sha256", sa.String(length=64), nullable=False),
        sa.Column("sample_count", sa.Integer(), nullable=False),
        sa.Column("recommended_threshold_micros", sa.Integer(), nullable=True),
        sa.Column("ready_for_enforcement", sa.Boolean(), nullable=False),
        sa.Column("report", json_type, nullable=False),
        sa.Column("report_sha256", sa.String(length=64), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "version > 0",
            name=op.f("ck_semantic_calibration_artifacts_positive_version"),
        ),
        sa.CheckConstraint(
            "sample_count >= 0",
            name=op.f("ck_semantic_calibration_artifacts_nonnegative_sample_count"),
        ),
        sa.CheckConstraint(
            "length(profile_sha256) = 64",
            name=op.f("ck_semantic_calibration_artifacts_valid_profile_sha256"),
        ),
        sa.CheckConstraint(
            "length(dataset_sha256) = 64",
            name=op.f("ck_semantic_calibration_artifacts_valid_dataset_sha256"),
        ),
        sa.CheckConstraint(
            "length(report_sha256) = 64",
            name=op.f("ck_semantic_calibration_artifacts_valid_report_sha256"),
        ),
        sa.CheckConstraint(
            "recommended_threshold_micros IS NULL "
            "OR recommended_threshold_micros BETWEEN 0 AND 1000000",
            name=op.f("ck_semantic_calibration_artifacts_valid_recommended_threshold"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["admin_users.id"],
            name=op.f("fk_semantic_calibration_artifacts_created_by_user_id_admin_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_semantic_calibration_artifacts")),
        sa.UniqueConstraint(
            "profile_sha256",
            "report_sha256",
            name="uq_semantic_calibration_artifacts_profile_report",
        ),
        sa.UniqueConstraint(
            "profile_sha256",
            "version",
            name="uq_semantic_calibration_artifacts_profile_version",
        ),
    )
    op.create_index(
        op.f("ix_semantic_calibration_artifacts_created_by_user_id"),
        "semantic_calibration_artifacts",
        ["created_by_user_id"],
    )
    op.create_index(
        "ix_semantic_calibration_artifacts_profile_created",
        "semantic_calibration_artifacts",
        ["profile_sha256", "created_at"],
    )
    _create_immutability_guards()


def downgrade() -> None:
    _drop_immutability_guards()
    op.drop_index(
        "ix_semantic_calibration_artifacts_profile_created",
        table_name="semantic_calibration_artifacts",
    )
    op.drop_index(
        op.f("ix_semantic_calibration_artifacts_created_by_user_id"),
        table_name="semantic_calibration_artifacts",
    )
    op.drop_table("semantic_calibration_artifacts")
    op.drop_index(
        "ix_semantic_anatomy_feedback_profile_created",
        table_name="semantic_anatomy_feedback",
    )
    op.drop_index(
        op.f("ix_semantic_anatomy_feedback_feedback_by_user_id"),
        table_name="semantic_anatomy_feedback",
    )
    op.drop_index(
        op.f("ix_semantic_anatomy_feedback_asset_id"),
        table_name="semantic_anatomy_feedback",
    )
    op.drop_table("semantic_anatomy_feedback")
    op.drop_index(
        "uq_semantic_assessments_feedback_identity",
        table_name="semantic_assessments",
    )


def _create_immutability_guards() -> None:
    dialect = op.get_bind().dialect.name
    tables = (
        ("semantic_anatomy_feedback", "semantic anatomy feedback"),
        ("semantic_calibration_artifacts", "semantic calibration artifacts"),
    )
    if dialect == "sqlite":
        for table, label in tables:
            op.execute(
                sa.text(
                    f"CREATE TRIGGER {table}_immutable_update BEFORE UPDATE ON {table} "
                    f"BEGIN SELECT RAISE(ABORT, '{label} are append-only'); END"
                )
            )
            op.execute(
                sa.text(
                    f"CREATE TRIGGER {table}_immutable_delete BEFORE DELETE ON {table} "
                    f"BEGIN SELECT RAISE(ABORT, '{label} are append-only'); END"
                )
            )
        return
    if dialect == "postgresql":
        for table, label in tables:
            op.execute(
                sa.text(
                    f"CREATE OR REPLACE FUNCTION gen_automation_guard_{table}_mutation() "
                    "RETURNS trigger AS $$ BEGIN "
                    f"RAISE EXCEPTION '{label} are append-only'; END; $$ LANGUAGE plpgsql"
                )
            )
            op.execute(
                sa.text(
                    f"CREATE TRIGGER {table}_guard_mutation "
                    f"BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW "
                    f"EXECUTE FUNCTION gen_automation_guard_{table}_mutation()"
                )
            )


def _drop_immutability_guards() -> None:
    dialect = op.get_bind().dialect.name
    tables = ("semantic_anatomy_feedback", "semantic_calibration_artifacts")
    if dialect == "sqlite":
        for table in tables:
            op.execute(sa.text(f"DROP TRIGGER IF EXISTS {table}_immutable_update"))
            op.execute(sa.text(f"DROP TRIGGER IF EXISTS {table}_immutable_delete"))
        return
    if dialect == "postgresql":
        for table in tables:
            op.execute(sa.text(f"DROP TRIGGER IF EXISTS {table}_guard_mutation ON {table}"))
            op.execute(sa.text(f"DROP FUNCTION IF EXISTS gen_automation_guard_{table}_mutation()"))
