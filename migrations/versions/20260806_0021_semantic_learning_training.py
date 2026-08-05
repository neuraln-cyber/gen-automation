"""Add autonomous semantic learning policy and training records.

Revision ID: 20260806_0021
Revises: 20260806_0020
Create Date: 2026-08-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from gen_automation.db.base import JSON_TYPE

revision: str = "20260806_0021"
down_revision: str | None = "20260806_0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "semantic_learning_policies",
        sa.Column("owner_user_id", sa.Uuid(), nullable=False),
        sa.Column("learning_enabled", sa.Boolean(), nullable=False),
        sa.Column("auto_train_meta", sa.Boolean(), nullable=False),
        sa.Column("auto_train_visual", sa.Boolean(), nullable=False),
        sa.Column("auto_promote_validated", sa.Boolean(), nullable=False),
        sa.Column("max_visual_run_microusd", sa.BigInteger(), nullable=False),
        sa.Column("minimum_new_labels_for_retrain", sa.Integer(), nullable=False),
        sa.Column("lock_version", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "max_visual_run_microusd BETWEEN 1 AND 25000000",
            name=op.f("ck_semantic_learning_policies_bounded_visual_run_cost"),
        ),
        sa.CheckConstraint(
            "lock_version > 0",
            name=op.f("ck_semantic_learning_policies_positive_lock_version"),
        ),
        sa.CheckConstraint(
            "minimum_new_labels_for_retrain > 0",
            name=op.f("ck_semantic_learning_policies_positive_retrain_delta"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["admin_users.id"],
            name=op.f("fk_semantic_learning_policies_owner_user_id_admin_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "owner_user_id",
            name=op.f("pk_semantic_learning_policies"),
        ),
    )
    op.create_table(
        "semantic_training_runs",
        sa.Column("owner_user_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("state", sa.String(length=12), nullable=False),
        sa.Column("profile_sha256", sa.String(length=64), nullable=False),
        sa.Column("dataset_sha256", sa.String(length=64), nullable=False),
        sa.Column("dataset_schema_version", sa.String(length=100), nullable=False),
        sa.Column("split_manifest", JSON_TYPE, nullable=False),
        sa.Column("split_manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column("training_config", JSON_TYPE, nullable=False),
        sa.Column("training_config_sha256", sa.String(length=64), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_owner", sa.String(length=200), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider", sa.String(length=50), nullable=True),
        sa.Column("provider_job_id", sa.String(length=200), nullable=True),
        sa.Column("base_model", sa.String(length=200), nullable=True),
        sa.Column("base_model_revision", sa.String(length=200), nullable=True),
        sa.Column("trainer_image_digest", sa.String(length=512), nullable=True),
        sa.Column("artifact_sha256", sa.String(length=64), nullable=True),
        sa.Column("artifact_storage_bucket", sa.String(length=255), nullable=True),
        sa.Column("artifact_object_key", sa.String(length=1024), nullable=True),
        sa.Column("artifact_object_version_id", sa.String(length=1024), nullable=True),
        sa.Column("artifact_size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("model_payload", JSON_TYPE, nullable=True),
        sa.Column("evaluation_report", JSON_TYPE, nullable=True),
        sa.Column("evaluation_sha256", sa.String(length=64), nullable=True),
        sa.Column("promotion_report", JSON_TYPE, nullable=True),
        sa.Column("estimated_cost_microusd", sa.BigInteger(), nullable=True),
        sa.Column("actual_cost_microusd", sa.BigInteger(), nullable=True),
        sa.Column("last_error_code", sa.String(length=100), nullable=True),
        sa.Column("last_error_detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "actual_cost_microusd IS NULL OR actual_cost_microusd >= 0",
            name=op.f("ck_semantic_training_runs_nonnegative_actual_cost"),
        ),
        sa.CheckConstraint(
            "attempts <= max_attempts",
            name=op.f("ck_semantic_training_runs_attempts_within_limit"),
        ),
        sa.CheckConstraint(
            "estimated_cost_microusd IS NULL OR estimated_cost_microusd >= 0",
            name=op.f("ck_semantic_training_runs_nonnegative_estimated_cost"),
        ),
        sa.CheckConstraint(
            "length(dataset_sha256) = 64",
            name=op.f("ck_semantic_training_runs_valid_dataset_sha256"),
        ),
        sa.CheckConstraint(
            "length(profile_sha256) = 64",
            name=op.f("ck_semantic_training_runs_valid_profile_sha256"),
        ),
        sa.CheckConstraint(
            "length(split_manifest_sha256) = 64",
            name=op.f("ck_semantic_training_runs_valid_split_manifest_sha256"),
        ),
        sa.CheckConstraint(
            "length(training_config_sha256) = 64",
            name=op.f("ck_semantic_training_runs_valid_training_config_sha256"),
        ),
        sa.CheckConstraint(
            "(state IN ('preparing', 'running') "
            "AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL) "
            "OR (state NOT IN ('preparing', 'running') "
            "AND lease_owner IS NULL AND lease_expires_at IS NULL)",
            name=op.f("ck_semantic_training_runs_lease_state"),
        ),
        sa.CheckConstraint(
            "attempts >= 0",
            name=op.f("ck_semantic_training_runs_nonnegative_attempts"),
        ),
        sa.CheckConstraint(
            "max_attempts > 0",
            name=op.f("ck_semantic_training_runs_positive_max_attempts"),
        ),
        sa.CheckConstraint(
            "(state = 'succeeded' AND artifact_sha256 IS NOT NULL "
            "AND evaluation_report IS NOT NULL AND evaluation_sha256 IS NOT NULL "
            "AND completed_at IS NOT NULL AND last_error_code IS NULL) "
            "OR (state IN ('failed', 'cancelled') AND completed_at IS NOT NULL) "
            "OR (state NOT IN ('succeeded', 'failed', 'cancelled') "
            "AND completed_at IS NULL)",
            name=op.f("ck_semantic_training_runs_terminal_result_state"),
        ),
        sa.CheckConstraint(
            "kind IN ('meta_classifier', 'visual_lora')",
            name=op.f("ck_semantic_training_runs_semantic_training_kind"),
        ),
        sa.CheckConstraint(
            "state IN ('planned', 'queued', 'preparing', 'submitted', 'running', "
            "'succeeded', 'failed', 'cancelled')",
            name=op.f("ck_semantic_training_runs_semantic_training_state"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["admin_users.id"],
            name=op.f("fk_semantic_training_runs_owner_user_id_admin_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_semantic_training_runs")),
        sa.UniqueConstraint(
            "owner_user_id",
            "kind",
            "dataset_sha256",
            "training_config_sha256",
            name="uq_semantic_training_runs_dataset_config",
        ),
    )
    op.create_index(
        op.f("ix_semantic_training_runs_owner_user_id"),
        "semantic_training_runs",
        ["owner_user_id"],
    )
    op.create_index(
        "ix_semantic_training_runs_claim",
        "semantic_training_runs",
        ["state", "available_at", "lease_expires_at", "created_at"],
    )
    op.create_index(
        "ix_semantic_training_runs_owner_profile",
        "semantic_training_runs",
        ["owner_user_id", "profile_sha256", "created_at"],
    )
    op.create_table(
        "semantic_model_promotions",
        sa.Column("owner_user_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("training_run_id", sa.Uuid(), nullable=False),
        sa.Column("previous_training_run_id", sa.Uuid(), nullable=True),
        sa.Column("profile_sha256", sa.String(length=64), nullable=False),
        sa.Column("artifact_sha256", sa.String(length=64), nullable=False),
        sa.Column("dataset_sha256", sa.String(length=64), nullable=False),
        sa.Column("evaluation_sha256", sa.String(length=64), nullable=False),
        sa.Column("decision", sa.String(length=11), nullable=False),
        sa.Column("keep_threshold_micros", sa.Integer(), nullable=False),
        sa.Column("reject_threshold_micros", sa.Integer(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "decision IN ('promoted', 'rejected', 'rolled_back')",
            name=op.f("ck_semantic_model_promotions_semantic_promotion_decision"),
        ),
        sa.CheckConstraint(
            "keep_threshold_micros BETWEEN -1 AND 1000000",
            name=op.f("ck_semantic_model_promotions_valid_keep_threshold"),
        ),
        sa.CheckConstraint(
            "keep_threshold_micros < reject_threshold_micros",
            name=op.f("ck_semantic_model_promotions_ordered_thresholds"),
        ),
        sa.CheckConstraint(
            "kind IN ('meta_classifier', 'visual_lora')",
            name=op.f("ck_semantic_model_promotions_semantic_promotion_training_kind"),
        ),
        sa.CheckConstraint(
            "length(artifact_sha256) = 64",
            name=op.f("ck_semantic_model_promotions_valid_artifact_sha256"),
        ),
        sa.CheckConstraint(
            "length(dataset_sha256) = 64",
            name=op.f("ck_semantic_model_promotions_valid_dataset_sha256"),
        ),
        sa.CheckConstraint(
            "length(evaluation_sha256) = 64",
            name=op.f("ck_semantic_model_promotions_valid_evaluation_sha256"),
        ),
        sa.CheckConstraint(
            "length(profile_sha256) = 64",
            name=op.f("ck_semantic_model_promotions_valid_profile_sha256"),
        ),
        sa.CheckConstraint(
            "length(trim(reason)) > 0",
            name=op.f("ck_semantic_model_promotions_nonempty_reason"),
        ),
        sa.CheckConstraint(
            "reject_threshold_micros BETWEEN 0 AND 1000001",
            name=op.f("ck_semantic_model_promotions_valid_reject_threshold"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["admin_users.id"],
            name=op.f("fk_semantic_model_promotions_created_by_user_id_admin_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["admin_users.id"],
            name=op.f("fk_semantic_model_promotions_owner_user_id_admin_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["previous_training_run_id"],
            ["semantic_training_runs.id"],
            name=op.f(
                "fk_semantic_model_promotions_previous_training_run_id_semantic_training_runs"
            ),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["training_run_id"],
            ["semantic_training_runs.id"],
            name=op.f(
                "fk_semantic_model_promotions_training_run_id_semantic_training_runs"
            ),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_semantic_model_promotions")),
    )
    op.create_index(
        op.f("ix_semantic_model_promotions_owner_user_id"),
        "semantic_model_promotions",
        ["owner_user_id"],
    )
    op.create_index(
        op.f("ix_semantic_model_promotions_training_run_id"),
        "semantic_model_promotions",
        ["training_run_id"],
    )
    op.create_index(
        "ix_semantic_model_promotions_owner_profile",
        "semantic_model_promotions",
        ["owner_user_id", "profile_sha256", "created_at"],
    )
    _create_guards()


def downgrade() -> None:
    _drop_guards()
    op.drop_index(
        "ix_semantic_model_promotions_owner_profile",
        table_name="semantic_model_promotions",
    )
    op.drop_index(
        op.f("ix_semantic_model_promotions_training_run_id"),
        table_name="semantic_model_promotions",
    )
    op.drop_index(
        op.f("ix_semantic_model_promotions_owner_user_id"),
        table_name="semantic_model_promotions",
    )
    op.drop_table("semantic_model_promotions")
    op.drop_index(
        "ix_semantic_training_runs_owner_profile",
        table_name="semantic_training_runs",
    )
    op.drop_index("ix_semantic_training_runs_claim", table_name="semantic_training_runs")
    op.drop_index(
        op.f("ix_semantic_training_runs_owner_user_id"),
        table_name="semantic_training_runs",
    )
    op.drop_table("semantic_training_runs")
    op.drop_table("semantic_learning_policies")


def _create_guards() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        op.execute(
            "CREATE TRIGGER semantic_training_runs_guard_terminal_update "
            "BEFORE UPDATE ON semantic_training_runs WHEN "
            "OLD.state IN ('succeeded', 'failed', 'cancelled') BEGIN "
            "SELECT RAISE(ABORT, 'terminal semantic training runs are immutable'); END"
        )
        op.execute(
            "CREATE TRIGGER semantic_training_runs_guard_delete "
            "BEFORE DELETE ON semantic_training_runs BEGIN "
            "SELECT RAISE(ABORT, 'semantic training runs cannot be deleted'); END"
        )
        op.execute(
            "CREATE TRIGGER semantic_model_promotions_immutable_update "
            "BEFORE UPDATE ON semantic_model_promotions BEGIN "
            "SELECT RAISE(ABORT, 'semantic model promotions are append-only'); END"
        )
        op.execute(
            "CREATE TRIGGER semantic_model_promotions_immutable_delete "
            "BEFORE DELETE ON semantic_model_promotions BEGIN "
            "SELECT RAISE(ABORT, 'semantic model promotions are append-only'); END"
        )
        return
    op.execute(
        "CREATE OR REPLACE FUNCTION gen_automation_guard_semantic_training_run_mutation() "
        "RETURNS trigger AS $$ BEGIN "
        "IF TG_OP = 'DELETE' THEN "
        "RAISE EXCEPTION 'semantic training runs cannot be deleted'; END IF; "
        "IF OLD.state IN ('succeeded', 'failed', 'cancelled') THEN "
        "RAISE EXCEPTION 'terminal semantic training runs are immutable'; END IF; "
        "RETURN NEW; END; $$ LANGUAGE plpgsql"
    )
    op.execute(
        "CREATE TRIGGER semantic_training_runs_guard_mutation "
        "BEFORE UPDATE OR DELETE ON semantic_training_runs FOR EACH ROW "
        "EXECUTE FUNCTION gen_automation_guard_semantic_training_run_mutation()"
    )
    op.execute(
        "CREATE OR REPLACE FUNCTION gen_automation_guard_semantic_model_promotions_mutation() "
        "RETURNS trigger AS $$ BEGIN "
        "RAISE EXCEPTION 'semantic model promotions are append-only'; END; $$ LANGUAGE plpgsql"
    )
    op.execute(
        "CREATE TRIGGER semantic_model_promotions_guard_mutation "
        "BEFORE UPDATE OR DELETE ON semantic_model_promotions FOR EACH ROW "
        "EXECUTE FUNCTION gen_automation_guard_semantic_model_promotions_mutation()"
    )


def _drop_guards() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS semantic_model_promotions_immutable_delete")
        op.execute("DROP TRIGGER IF EXISTS semantic_model_promotions_immutable_update")
        op.execute("DROP TRIGGER IF EXISTS semantic_training_runs_guard_delete")
        op.execute("DROP TRIGGER IF EXISTS semantic_training_runs_guard_terminal_update")
        return
    op.execute(
        "DROP TRIGGER IF EXISTS semantic_model_promotions_guard_mutation "
        "ON semantic_model_promotions"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS gen_automation_guard_semantic_model_promotions_mutation()"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS semantic_training_runs_guard_mutation "
        "ON semantic_training_runs"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS gen_automation_guard_semantic_training_run_mutation()"
    )
