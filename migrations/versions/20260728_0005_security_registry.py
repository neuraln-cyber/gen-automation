"""Add administrative authentication and server-owned approval registries.

Revision ID: 20260728_0005
Revises: 20260728_0004
Create Date: 2026-07-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260728_0005"
down_revision: str | None = "20260728_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
admin_role = sa.Enum(
    "owner",
    "admin",
    "reviewer",
    "publisher",
    name="admin_role",
    native_enum=False,
    create_constraint=True,
)
approval_status = sa.Enum(
    "approved",
    "revoked",
    name="approval_status",
    native_enum=False,
    create_constraint=True,
)
model_artifact_kind = sa.Enum(
    "checkpoint",
    "lora",
    name="model_artifact_kind",
    native_enum=False,
    create_constraint=True,
)
model_approval_status = sa.Enum(
    "approved",
    "revoked",
    name="model_approval_status",
    native_enum=False,
    create_constraint=True,
)
workflow_approval_status = sa.Enum(
    "approved",
    "revoked",
    name="workflow_approval_status",
    native_enum=False,
    create_constraint=True,
)


def upgrade() -> None:
    op.create_table(
        "admin_users",
        sa.Column("username_normalized", sa.String(length=200), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("role", admin_role, nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("totp_secret_ciphertext", sa.Text(), nullable=True),
        sa.Column("totp_confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_totp_counter", sa.BigInteger(), nullable=True),
        sa.Column("failed_login_count", sa.Integer(), nullable=False),
        sa.Column(
            "failed_login_window_started_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("password_changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("credential_version", sa.Integer(), nullable=False),
        sa.Column("lock_version", sa.Integer(), nullable=False),
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
            "failed_login_count >= 0",
            name=op.f("ck_admin_users_nonnegative_failed_login_count"),
        ),
        sa.CheckConstraint(
            "(failed_login_count = 0 AND failed_login_window_started_at IS NULL) "
            "OR (failed_login_count > 0 AND failed_login_window_started_at IS NOT NULL)",
            name=op.f("ck_admin_users_login_failure_window_pair"),
        ),
        sa.CheckConstraint(
            "credential_version > 0",
            name=op.f("ck_admin_users_positive_credential_version"),
        ),
        sa.CheckConstraint(
            "totp_confirmed_at IS NULL OR totp_secret_ciphertext IS NOT NULL",
            name=op.f("ck_admin_users_totp_confirmation_requires_secret"),
        ),
        sa.CheckConstraint(
            "last_totp_counter IS NULL OR "
            "(last_totp_counter >= 0 AND totp_confirmed_at IS NOT NULL)",
            name=op.f("ck_admin_users_totp_counter_requires_confirmation"),
        ),
        sa.CheckConstraint(
            "lock_version > 0",
            name=op.f("ck_admin_users_positive_lock_version"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_admin_users")),
        sa.UniqueConstraint(
            "username_normalized",
            name=op.f("uq_admin_users_username_normalized"),
        ),
    )
    op.create_table(
        "admin_sessions",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("token_sha256", sa.String(length=64), nullable=False),
        sa.Column("csrf_sha256", sa.String(length=64), nullable=False),
        sa.Column("credential_version", sa.Integer(), nullable=False),
        sa.Column("client_context_hmac", sa.String(length=64), nullable=True),
        sa.Column("user_agent_hmac", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("idle_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reauthenticated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("mfa_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "expires_at > created_at",
            name=op.f("ck_admin_sessions_expiry_after_creation"),
        ),
        sa.CheckConstraint(
            "idle_expires_at > created_at AND idle_expires_at <= expires_at",
            name=op.f("ck_admin_sessions_valid_idle_expiry"),
        ),
        sa.CheckConstraint(
            "last_seen_at >= created_at AND last_seen_at <= idle_expires_at",
            name=op.f("ck_admin_sessions_valid_last_seen_time"),
        ),
        sa.CheckConstraint(
            "reauthenticated_at >= created_at AND reauthenticated_at <= expires_at",
            name=op.f("ck_admin_sessions_valid_reauthentication_time"),
        ),
        sa.CheckConstraint(
            "mfa_verified_at IS NULL OR "
            "(mfa_verified_at >= created_at AND mfa_verified_at <= expires_at)",
            name=op.f("ck_admin_sessions_valid_mfa_time"),
        ),
        sa.CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= created_at",
            name=op.f("ck_admin_sessions_valid_revocation_time"),
        ),
        sa.CheckConstraint(
            "credential_version > 0",
            name=op.f("ck_admin_sessions_positive_credential_version"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["admin_users.id"],
            name=op.f("fk_admin_sessions_user_id_admin_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_admin_sessions")),
        sa.UniqueConstraint(
            "token_sha256",
            name="uq_admin_sessions_token_sha256",
        ),
    )
    op.create_index(
        "ix_admin_sessions_user_active",
        "admin_sessions",
        ["user_id", "revoked_at", "expires_at"],
    )
    op.create_index(
        "ix_admin_sessions_expires_at",
        "admin_sessions",
        ["expires_at"],
    )
    op.create_index(
        "ix_admin_sessions_idle_expires_at",
        "admin_sessions",
        ["idle_expires_at"],
    )
    op.create_table(
        "login_throttles",
        sa.Column("key_sha256", sa.String(length=64), nullable=False),
        sa.Column("failure_count", sa.Integer(), nullable=False),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("blocked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "failure_count >= 0",
            name=op.f("ck_login_throttles_nonnegative_failure_count"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_login_throttles")),
        sa.UniqueConstraint(
            "key_sha256",
            name="uq_login_throttles_key_sha256",
        ),
    )
    op.create_index(
        "ix_login_throttles_updated_at",
        "login_throttles",
        ["updated_at"],
    )
    op.create_index(
        "ix_login_throttles_blocked_until",
        "login_throttles",
        ["blocked_until"],
    )
    op.create_table(
        "subject_approvals",
        sa.Column("slug", sa.String(length=80), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("canonical_source_url", sa.Text(), nullable=False),
        sa.Column("canonical_source_sha256", sa.String(length=64), nullable=False),
        sa.Column("canonical_age", sa.Integer(), nullable=False),
        sa.Column("clearly_adult", sa.Boolean(), nullable=False),
        sa.Column("is_fictional", sa.Boolean(), nullable=False),
        sa.Column("is_aged_up_minor", sa.Boolean(), nullable=False),
        sa.Column("distribution_rights_approved", sa.Boolean(), nullable=False),
        sa.Column("adult_derivative_rights_approved", sa.Boolean(), nullable=False),
        sa.Column("evidence", json_type, nullable=False),
        sa.Column("evidence_sha256", sa.String(length=64), nullable=False),
        sa.Column("status", approval_status, nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("approval_version", sa.Integer(), nullable=False),
        sa.Column("approved_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
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
            "canonical_age >= 18",
            name=op.f("ck_subject_approvals_adult_canonical_age"),
        ),
        sa.CheckConstraint(
            "clearly_adult = true",
            name=op.f("ck_subject_approvals_clearly_adult_required"),
        ),
        sa.CheckConstraint(
            "is_fictional = true",
            name=op.f("ck_subject_approvals_fictional_only"),
        ),
        sa.CheckConstraint(
            "is_aged_up_minor = false",
            name=op.f("ck_subject_approvals_aged_up_minor_forbidden"),
        ),
        sa.CheckConstraint(
            "distribution_rights_approved = true",
            name=op.f("ck_subject_approvals_distribution_rights_required"),
        ),
        sa.CheckConstraint(
            "adult_derivative_rights_approved = true",
            name=op.f("ck_subject_approvals_adult_derivative_rights_required"),
        ),
        sa.CheckConstraint(
            "approval_version > 0",
            name=op.f("ck_subject_approvals_positive_approval_version"),
        ),
        sa.CheckConstraint(
            "(status = 'approved' AND revoked_at IS NULL "
            "AND revoked_by_user_id IS NULL) OR "
            "(status = 'revoked' AND revoked_at IS NOT NULL "
            "AND revoked_by_user_id IS NOT NULL)",
            name=op.f("ck_subject_approvals_revocation_state"),
        ),
        sa.CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= approved_at",
            name=op.f("ck_subject_approvals_valid_revocation_time"),
        ),
        sa.ForeignKeyConstraint(
            ["approved_by_user_id"],
            ["admin_users.id"],
            name=op.f("fk_subject_approvals_approved_by_user_id_admin_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["revoked_by_user_id"],
            ["admin_users.id"],
            name=op.f("fk_subject_approvals_revoked_by_user_id_admin_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_subject_approvals")),
        sa.UniqueConstraint(
            "canonical_source_sha256",
            "approval_version",
            name="uq_subject_approvals_identity_version",
        ),
        sa.UniqueConstraint(
            "slug",
            "approval_version",
            name="uq_subject_approvals_slug_version",
        ),
    )
    op.create_index(
        "uq_subject_approvals_current_source",
        "subject_approvals",
        ["canonical_source_sha256"],
        unique=True,
        postgresql_where=sa.text("is_current"),
        sqlite_where=sa.text("is_current = 1"),
    )
    op.create_index(
        "uq_subject_approvals_current_slug",
        "subject_approvals",
        ["slug"],
        unique=True,
        postgresql_where=sa.text("is_current"),
        sqlite_where=sa.text("is_current = 1"),
    )
    op.create_table(
        "model_artifact_approvals",
        sa.Column("artifact_sha256", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("kind", model_artifact_kind, nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("storage_key", sa.String(length=1024), nullable=False),
        sa.Column("license_url", sa.Text(), nullable=False),
        sa.Column("commercial_use_approved", sa.Boolean(), nullable=False),
        sa.Column("adult_use_approved", sa.Boolean(), nullable=False),
        sa.Column("safetensors_verified", sa.Boolean(), nullable=False),
        sa.Column("evidence", json_type, nullable=False),
        sa.Column("evidence_sha256", sa.String(length=64), nullable=False),
        sa.Column("status", model_approval_status, nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("approval_version", sa.Integer(), nullable=False),
        sa.Column("approved_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
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
            "commercial_use_approved = true",
            name=op.f("ck_model_artifact_approvals_commercial_use_required"),
        ),
        sa.CheckConstraint(
            "adult_use_approved = true",
            name=op.f("ck_model_artifact_approvals_adult_use_required"),
        ),
        sa.CheckConstraint(
            "safetensors_verified = true",
            name=op.f("ck_model_artifact_approvals_safetensors_required"),
        ),
        sa.CheckConstraint(
            "approval_version > 0",
            name=op.f("ck_model_artifact_approvals_positive_approval_version"),
        ),
        sa.CheckConstraint(
            "(status = 'approved' AND revoked_at IS NULL "
            "AND revoked_by_user_id IS NULL) OR "
            "(status = 'revoked' AND revoked_at IS NOT NULL "
            "AND revoked_by_user_id IS NOT NULL)",
            name=op.f("ck_model_artifact_approvals_revocation_state"),
        ),
        sa.CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= approved_at",
            name=op.f("ck_model_artifact_approvals_valid_revocation_time"),
        ),
        sa.ForeignKeyConstraint(
            ["approved_by_user_id"],
            ["admin_users.id"],
            name=op.f("fk_model_artifact_approvals_approved_by_user_id_admin_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["revoked_by_user_id"],
            ["admin_users.id"],
            name=op.f("fk_model_artifact_approvals_revoked_by_user_id_admin_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "id",
            name=op.f("pk_model_artifact_approvals"),
        ),
        sa.UniqueConstraint(
            "artifact_sha256",
            "approval_version",
            name="uq_model_artifact_approvals_identity_version",
        ),
    )
    op.create_index(
        "uq_model_artifact_approvals_current_artifact",
        "model_artifact_approvals",
        ["artifact_sha256"],
        unique=True,
        postgresql_where=sa.text("is_current"),
        sqlite_where=sa.text("is_current = 1"),
    )
    op.create_table(
        "workflow_approvals",
        sa.Column("workflow_sha256", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("version", sa.String(length=100), nullable=False),
        sa.Column("object_key", sa.String(length=1024), nullable=False),
        sa.Column("reviewed_node_classes", json_type, nullable=False),
        sa.Column("evidence", json_type, nullable=False),
        sa.Column("evidence_sha256", sa.String(length=64), nullable=False),
        sa.Column("status", workflow_approval_status, nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("approval_version", sa.Integer(), nullable=False),
        sa.Column("approved_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
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
            "approval_version > 0",
            name=op.f("ck_workflow_approvals_positive_approval_version"),
        ),
        sa.CheckConstraint(
            "(status = 'approved' AND revoked_at IS NULL "
            "AND revoked_by_user_id IS NULL) OR "
            "(status = 'revoked' AND revoked_at IS NOT NULL "
            "AND revoked_by_user_id IS NOT NULL)",
            name=op.f("ck_workflow_approvals_revocation_state"),
        ),
        sa.CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= approved_at",
            name=op.f("ck_workflow_approvals_valid_revocation_time"),
        ),
        sa.ForeignKeyConstraint(
            ["approved_by_user_id"],
            ["admin_users.id"],
            name=op.f("fk_workflow_approvals_approved_by_user_id_admin_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["revoked_by_user_id"],
            ["admin_users.id"],
            name=op.f("fk_workflow_approvals_revoked_by_user_id_admin_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workflow_approvals")),
        sa.UniqueConstraint(
            "workflow_sha256",
            "approval_version",
            name="uq_workflow_approvals_identity_version",
        ),
    )
    op.create_index(
        "uq_workflow_approvals_current_workflow",
        "workflow_approvals",
        ["workflow_sha256"],
        unique=True,
        postgresql_where=sa.text("is_current"),
        sqlite_where=sa.text("is_current = 1"),
    )


def downgrade() -> None:
    op.drop_table("workflow_approvals")
    op.drop_table("model_artifact_approvals")
    op.drop_table("subject_approvals")
    op.drop_index(
        "ix_login_throttles_blocked_until",
        table_name="login_throttles",
    )
    op.drop_index(
        "ix_login_throttles_updated_at",
        table_name="login_throttles",
    )
    op.drop_table("login_throttles")
    op.drop_index(
        "ix_admin_sessions_idle_expires_at",
        table_name="admin_sessions",
    )
    op.drop_index(
        "ix_admin_sessions_expires_at",
        table_name="admin_sessions",
    )
    op.drop_index(
        "ix_admin_sessions_user_active",
        table_name="admin_sessions",
    )
    op.drop_table("admin_sessions")
    op.drop_table("admin_users")
