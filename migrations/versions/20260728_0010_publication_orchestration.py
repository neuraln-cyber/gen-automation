"""Add durable, fail-closed publication orchestration.

Revision ID: 20260728_0010
Revises: 20260728_0009
Create Date: 2026-07-28
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260728_0010"
down_revision: str | None = "20260728_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
publication_target = sa.Enum(
    "x",
    "patreon",
    name="publication_target",
    native_enum=False,
    create_constraint=True,
)
publication_intent_state = sa.Enum(
    "awaiting_approval",
    "ready",
    "processing",
    "awaiting_human",
    "unknown",
    "published",
    "failed",
    "cancelled",
    name="publication_intent_state",
    native_enum=False,
    create_constraint=True,
)
publication_approval_action = sa.Enum(
    "approve",
    "revoke",
    name="publication_approval_action",
    native_enum=False,
    create_constraint=True,
)
publication_attempt_state = sa.Enum(
    "queued",
    "claimed",
    "processing",
    "retry_wait",
    "awaiting_human",
    "unknown",
    "succeeded",
    "failed",
    "cancelled",
    name="publication_attempt_state",
    native_enum=False,
    create_constraint=True,
)
publication_step_kind = sa.Enum(
    "x_media_upload",
    "x_create_post",
    "patreon_package",
    "patreon_handoff",
    name="publication_step_kind",
    native_enum=False,
    create_constraint=True,
    length=16,
)
publication_effect_step_kind = sa.Enum(
    "x_media_upload",
    "x_create_post",
    "patreon_package",
    "patreon_handoff",
    name="publication_effect_step_kind",
    native_enum=False,
    create_constraint=True,
    length=16,
)
publication_step_state = sa.Enum(
    "pending",
    "processing",
    "retry_wait",
    "awaiting_human",
    "unknown",
    "succeeded",
    "failed",
    "cancelled",
    name="publication_step_state",
    native_enum=False,
    create_constraint=True,
)
publication_retry_class = sa.Enum(
    "safe_retry",
    "terminal",
    "unknown",
    name="publication_retry_class",
    native_enum=False,
    create_constraint=True,
)

_INITIAL_GUARD_ID = UUID("00000000-0000-0000-0000-000000000001")


def upgrade() -> None:
    _create_publication_provider_guards()
    _create_publication_intents()
    _create_publication_inputs()
    _create_publication_approvals()
    _create_publication_reconciliations()
    _create_publication_packages()
    _create_publication_attempts()
    _create_publication_steps()
    _create_publication_effect_events()
    _insert_stopped_guard(op.get_bind())
    _create_guards(op.get_bind().dialect.name)


def downgrade() -> None:
    _drop_guards(op.get_bind().dialect.name)

    op.drop_index(
        "ix_publication_effect_events_step_request",
        table_name="publication_effect_events",
    )
    op.drop_index(
        op.f("ix_publication_effect_events_step_id"),
        table_name="publication_effect_events",
    )
    op.drop_table("publication_effect_events")

    op.drop_index(
        "ix_publication_steps_attempt_state",
        table_name="publication_steps",
    )
    op.drop_index(
        op.f("ix_publication_steps_attempt_id"),
        table_name="publication_steps",
    )
    op.drop_table("publication_steps")

    op.drop_index(
        "ix_publication_attempts_claim",
        table_name="publication_attempts",
    )
    op.drop_index(
        op.f("ix_publication_attempts_approval_id"),
        table_name="publication_attempts",
    )
    op.drop_index(
        op.f("ix_publication_attempts_intent_id"),
        table_name="publication_attempts",
    )
    op.drop_table("publication_attempts")

    op.drop_index(
        op.f("ix_publication_packages_intent_id"),
        table_name="publication_packages",
    )
    op.drop_table("publication_packages")

    op.drop_index(
        "ix_publication_reconciliations_intent_recorded",
        table_name="publication_reconciliations",
    )
    op.drop_index(
        op.f("ix_publication_reconciliations_intent_id"),
        table_name="publication_reconciliations",
    )
    op.drop_table("publication_reconciliations")

    op.drop_index(
        "ix_publication_approvals_intent_recorded",
        table_name="publication_approvals",
    )
    op.drop_index(
        op.f("ix_publication_approvals_intent_id"),
        table_name="publication_approvals",
    )
    op.drop_table("publication_approvals")

    op.drop_index(
        "ix_publication_inputs_intent_role",
        table_name="publication_inputs",
    )
    op.drop_index(
        op.f("ix_publication_inputs_derivative_output_id"),
        table_name="publication_inputs",
    )
    op.drop_index(
        op.f("ix_publication_inputs_intent_id"),
        table_name="publication_inputs",
    )
    op.drop_table("publication_inputs")

    op.drop_index(
        "ix_publication_intents_state_schedule",
        table_name="publication_intents",
    )
    op.drop_index(
        op.f("ix_publication_intents_release_version_id"),
        table_name="publication_intents",
    )
    op.drop_index(
        op.f("ix_publication_intents_release_id"),
        table_name="publication_intents",
    )
    op.drop_table("publication_intents")
    op.drop_table("publication_provider_guards")


def _create_publication_provider_guards() -> None:
    op.create_table(
        "publication_provider_guards",
        sa.Column("provider", sa.String(length=20), nullable=False),
        sa.Column(
            "enabled",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("epoch", sa.Integer(), nullable=False),
        sa.Column("lock_version", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column("changed_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "provider = 'global'",
            name=op.f("ck_publication_provider_guards_global_only"),
        ),
        sa.CheckConstraint(
            "epoch > 0",
            name=op.f("ck_publication_provider_guards_positive_epoch"),
        ),
        sa.CheckConstraint(
            "lock_version > 0",
            name=op.f("ck_publication_provider_guards_positive_lock_version"),
        ),
        sa.CheckConstraint(
            "length(trim(reason)) > 0",
            name=op.f("ck_publication_provider_guards_nonempty_reason"),
        ),
        sa.ForeignKeyConstraint(
            ["changed_by_user_id"],
            ["admin_users.id"],
            name=op.f("fk_publication_provider_guards_changed_by_user_id_admin_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "id",
            name=op.f("pk_publication_provider_guards"),
        ),
        sa.UniqueConstraint(
            "provider",
            name="uq_publication_provider_guards_provider",
        ),
    )


def _create_publication_intents() -> None:
    op.create_table(
        "publication_intents",
        sa.Column("release_id", sa.Uuid(), nullable=False),
        sa.Column("release_version_id", sa.Uuid(), nullable=False),
        sa.Column("target", publication_target, nullable=False),
        sa.Column("state", publication_intent_state, nullable=False),
        sa.Column("configuration", json_type, nullable=False),
        sa.Column("configuration_sha256", sa.String(length=64), nullable=False),
        sa.Column("input_manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column("intent_digest", sa.String(length=64), nullable=False),
        sa.Column("input_count", sa.Integer(), nullable=False),
        sa.Column("credential_reference", sa.String(length=500), nullable=True),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "public_preview_attester_name",
            sa.String(length=256),
            nullable=True,
        ),
        sa.Column("public_preview_attester_user_id", sa.Uuid(), nullable=True),
        sa.Column(
            "public_preview_attested_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "public_preview_attestation_timezone",
            sa.String(length=100),
            nullable=True,
        ),
        sa.Column(
            "public_preview_attestation_sha256",
            sa.String(length=64),
            nullable=True,
        ),
        sa.Column("planned_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("planned_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lock_version", sa.Integer(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=100), nullable=True),
        sa.Column("last_error_detail", sa.String(length=500), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "length(configuration_sha256) = 64",
            name=op.f("ck_publication_intents_valid_config_sha256"),
        ),
        sa.CheckConstraint(
            "length(input_manifest_sha256) = 64",
            name=op.f("ck_publication_intents_valid_input_manifest_sha256"),
        ),
        sa.CheckConstraint(
            "length(intent_digest) = 64",
            name=op.f("ck_publication_intents_valid_intent_digest"),
        ),
        sa.CheckConstraint(
            "input_count > 0",
            name=op.f("ck_publication_intents_positive_input_count"),
        ),
        sa.CheckConstraint(
            "lock_version > 0",
            name=op.f("ck_publication_intents_positive_lock_version"),
        ),
        sa.CheckConstraint(
            "(target = 'x' "
            "AND input_count BETWEEN 1 AND 4 "
            "AND credential_reference IS NOT NULL "
            "AND public_preview_attester_name IS NULL "
            "AND public_preview_attester_user_id IS NULL "
            "AND public_preview_attested_at IS NULL "
            "AND public_preview_attestation_timezone IS NULL "
            "AND public_preview_attestation_sha256 IS NULL) "
            "OR (target = 'patreon' "
            "AND credential_reference IS NULL "
            "AND public_preview_attester_name IS NOT NULL "
            "AND public_preview_attester_user_id IS NOT NULL "
            "AND public_preview_attested_at IS NOT NULL "
            "AND public_preview_attestation_timezone IS NOT NULL "
            "AND public_preview_attestation_sha256 IS NOT NULL "
            "AND length(public_preview_attestation_sha256) = 64)",
            name=op.f("ck_publication_intents_target_contract"),
        ),
        sa.CheckConstraint(
            "scheduled_at IS NULL OR scheduled_at >= planned_at",
            name=op.f("ck_publication_intents_schedule_after_plan"),
        ),
        sa.CheckConstraint(
            "completed_at IS NULL OR completed_at >= planned_at",
            name=op.f("ck_publication_intents_completion_after_plan"),
        ),
        sa.ForeignKeyConstraint(
            ["planned_by_user_id"],
            ["admin_users.id"],
            name=op.f("fk_publication_intents_planned_by_user_id_admin_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["public_preview_attester_user_id"],
            ["admin_users.id"],
            name=op.f("fk_publication_intents_public_preview_attester_user_id_admin_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["release_id"],
            ["releases.id"],
            name=op.f("fk_publication_intents_release_id_releases"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["release_version_id"],
            ["release_versions.id"],
            name=op.f("fk_publication_intents_release_version_id_release_versions"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_publication_intents")),
        sa.UniqueConstraint(
            "release_version_id",
            "target",
            "configuration_sha256",
            name="uq_publication_intents_version_target_config",
        ),
        sa.UniqueConstraint(
            "intent_digest",
            name="uq_publication_intents_digest",
        ),
    )
    op.create_index(
        op.f("ix_publication_intents_release_id"),
        "publication_intents",
        ["release_id"],
    )
    op.create_index(
        op.f("ix_publication_intents_release_version_id"),
        "publication_intents",
        ["release_version_id"],
    )
    op.create_index(
        "ix_publication_intents_state_schedule",
        "publication_intents",
        ["state", "scheduled_at", "planned_at"],
    )


def _create_publication_inputs() -> None:
    op.create_table(
        "publication_inputs",
        sa.Column("intent_id", sa.Uuid(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("derivative_output_id", sa.Uuid(), nullable=False),
        sa.Column("derivative_recipe_id", sa.Uuid(), nullable=False),
        sa.Column("asset_id", sa.Uuid(), nullable=False),
        sa.Column("derivative_target", sa.String(length=50), nullable=False),
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
        sa.Column("frozen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "ordinal > 0",
            name=op.f("ck_publication_inputs_positive_ordinal"),
        ),
        sa.CheckConstraint(
            "role IN ('x_teaser', 'patreon_content', 'patreon_preview')",
            name=op.f("ck_publication_inputs_valid_role"),
        ),
        sa.CheckConstraint(
            "length(asset_sha256) = 64",
            name=op.f("ck_publication_inputs_valid_asset_sha256"),
        ),
        sa.CheckConstraint(
            "length(trim(derivative_target)) > 0 "
            "AND length(trim(asset_storage_backend)) > 0 "
            "AND length(trim(asset_storage_bucket)) > 0 "
            "AND length(trim(asset_object_key)) > 0 "
            "AND length(trim(asset_object_version_id)) > 0 "
            "AND length(trim(asset_content_type)) > 0 "
            "AND length(trim(asset_image_format)) > 0",
            name=op.f("ck_publication_inputs_complete_asset_identity"),
        ),
        sa.CheckConstraint(
            "asset_width > 0 AND asset_height > 0 AND asset_byte_size > 0",
            name=op.f("ck_publication_inputs_positive_asset_dimensions"),
        ),
        sa.CheckConstraint(
            "(role IN ('x_teaser', 'patreon_preview') "
            "AND derivative_target = 'x_teaser') "
            "OR (role = 'patreon_content' AND derivative_target = 'full')",
            name=op.f("ck_publication_inputs_role_target"),
        ),
        sa.ForeignKeyConstraint(
            ["asset_id"],
            ["assets.id"],
            name=op.f("fk_publication_inputs_asset_id_assets"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["derivative_output_id"],
            ["derivative_outputs.id"],
            name=op.f("fk_publication_inputs_derivative_output_id_derivative_outputs"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["derivative_recipe_id"],
            ["derivative_recipes.id"],
            name=op.f("fk_publication_inputs_derivative_recipe_id_derivative_recipes"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["intent_id"],
            ["publication_intents.id"],
            name=op.f("fk_publication_inputs_intent_id_publication_intents"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_publication_inputs")),
        sa.UniqueConstraint(
            "intent_id",
            "ordinal",
            name="uq_publication_inputs_intent_ordinal",
        ),
        sa.UniqueConstraint(
            "intent_id",
            "role",
            "derivative_output_id",
            name="uq_publication_inputs_intent_role_output",
        ),
    )
    op.create_index(
        op.f("ix_publication_inputs_intent_id"),
        "publication_inputs",
        ["intent_id"],
    )
    op.create_index(
        op.f("ix_publication_inputs_derivative_output_id"),
        "publication_inputs",
        ["derivative_output_id"],
    )
    op.create_index(
        "ix_publication_inputs_intent_role",
        "publication_inputs",
        ["intent_id", "role", "ordinal"],
    )


def _create_publication_approvals() -> None:
    op.create_table(
        "publication_approvals",
        sa.Column("intent_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("action", publication_approval_action, nullable=False),
        sa.Column("intent_digest", sa.String(length=64), nullable=False),
        sa.Column("intent_lock_version", sa.Integer(), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=False),
        sa.Column("actor_role", sa.String(length=20), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attestation_sha256", sa.String(length=64), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "revision > 0",
            name=op.f("ck_publication_approvals_positive_revision"),
        ),
        sa.CheckConstraint(
            "intent_lock_version > 0",
            name=op.f("ck_publication_approvals_positive_intent_lock_version"),
        ),
        sa.CheckConstraint(
            "length(intent_digest) = 64",
            name=op.f("ck_publication_approvals_valid_intent_digest"),
        ),
        sa.CheckConstraint(
            "actor_role IN ('owner', 'publisher')",
            name=op.f("ck_publication_approvals_publisher_role"),
        ),
        sa.CheckConstraint(
            "(action = 'approve' AND expires_at IS NOT NULL "
            "AND expires_at > recorded_at) "
            "OR (action = 'revoke' AND expires_at IS NULL)",
            name=op.f("ck_publication_approvals_approval_expiry"),
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["admin_users.id"],
            name=op.f("fk_publication_approvals_actor_user_id_admin_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["intent_id"],
            ["publication_intents.id"],
            name=op.f("fk_publication_approvals_intent_id_publication_intents"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_publication_approvals")),
        sa.UniqueConstraint(
            "intent_id",
            "revision",
            name="uq_publication_approvals_intent_revision",
        ),
    )
    op.create_index(
        op.f("ix_publication_approvals_intent_id"),
        "publication_approvals",
        ["intent_id"],
    )
    op.create_index(
        "ix_publication_approvals_intent_recorded",
        "publication_approvals",
        ["intent_id", "recorded_at"],
    )


def _create_publication_reconciliations() -> None:
    op.create_table(
        "publication_reconciliations",
        sa.Column("intent_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.String(length=24), nullable=False),
        sa.Column("intent_digest", sa.String(length=64), nullable=False),
        sa.Column("intent_lock_version", sa.Integer(), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=False),
        sa.Column("actor_role", sa.String(length=20), nullable=False),
        sa.Column("evidence_sha256", sa.String(length=64), nullable=False),
        sa.Column("attestation_sha256", sa.String(length=64), nullable=False),
        sa.Column("remote_identifier", sa.String(length=200), nullable=True),
        sa.Column("remote_url", sa.String(length=2048), nullable=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "revision > 0",
            name=op.f("ck_publication_reconciliations_positive_revision"),
        ),
        sa.CheckConstraint(
            "intent_lock_version > 0",
            name=op.f("ck_publication_reconciliations_positive_intent_lock_version"),
        ),
        sa.CheckConstraint(
            "length(intent_digest) = 64",
            name=op.f("ck_publication_reconciliations_valid_intent_digest"),
        ),
        sa.CheckConstraint(
            "length(evidence_sha256) = 64",
            name=op.f("ck_publication_reconciliations_valid_evidence_sha256"),
        ),
        sa.CheckConstraint(
            "length(attestation_sha256) = 64",
            name=op.f("ck_publication_reconciliations_valid_attestation_sha256"),
        ),
        sa.CheckConstraint(
            "actor_role IN ('owner', 'publisher')",
            name=op.f("ck_publication_reconciliations_publisher_role"),
        ),
        sa.CheckConstraint(
            "(outcome = 'confirmed_present' "
            "AND remote_identifier IS NOT NULL AND remote_url IS NOT NULL) "
            "OR (outcome = 'confirmed_absent' "
            "AND remote_identifier IS NULL AND remote_url IS NULL)",
            name=op.f("ck_publication_reconciliations_outcome_contract"),
        ),
        sa.CheckConstraint(
            "outcome IN ('confirmed_present', 'confirmed_absent')",
            name=op.f("ck_publication_reconciliations_valid_outcome"),
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["admin_users.id"],
            name=op.f("fk_publication_reconciliations_actor_user_id_admin_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["intent_id"],
            ["publication_intents.id"],
            name=op.f("fk_publication_reconciliations_intent_id_publication_intents"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "id",
            name=op.f("pk_publication_reconciliations"),
        ),
        sa.UniqueConstraint(
            "intent_id",
            "revision",
            name="uq_publication_reconciliations_intent_revision",
        ),
    )
    op.create_index(
        op.f("ix_publication_reconciliations_intent_id"),
        "publication_reconciliations",
        ["intent_id"],
    )
    op.create_index(
        "ix_publication_reconciliations_intent_recorded",
        "publication_reconciliations",
        ["intent_id", "recorded_at"],
    )


def _create_publication_packages() -> None:
    op.create_table(
        "publication_packages",
        sa.Column("intent_id", sa.Uuid(), nullable=False),
        sa.Column("storage_backend", sa.String(length=50), nullable=False),
        sa.Column("storage_bucket", sa.String(length=255), nullable=False),
        sa.Column("object_key", sa.String(length=1024), nullable=False),
        sa.Column("object_version_id", sa.String(length=1024), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("content_type", sa.String(length=100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "length(sha256) = 64",
            name=op.f("ck_publication_packages_valid_sha256"),
        ),
        sa.CheckConstraint(
            "length(manifest_sha256) = 64",
            name=op.f("ck_publication_packages_valid_manifest_sha256"),
        ),
        sa.CheckConstraint(
            "byte_size > 0",
            name=op.f("ck_publication_packages_positive_byte_size"),
        ),
        sa.CheckConstraint(
            "length(trim(storage_backend)) > 0 "
            "AND length(trim(storage_bucket)) > 0 "
            "AND length(trim(object_key)) > 0 "
            "AND length(trim(object_version_id)) > 0 "
            "AND content_type = 'application/zip'",
            name=op.f("ck_publication_packages_complete_storage_identity"),
        ),
        sa.ForeignKeyConstraint(
            ["intent_id"],
            ["publication_intents.id"],
            name=op.f("fk_publication_packages_intent_id_publication_intents"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_publication_packages")),
        sa.UniqueConstraint(
            "intent_id",
            name="uq_publication_packages_intent",
        ),
        sa.UniqueConstraint(
            "storage_backend",
            "storage_bucket",
            "object_key",
            "object_version_id",
            name="uq_publication_packages_storage_version",
        ),
    )
    op.create_index(
        op.f("ix_publication_packages_intent_id"),
        "publication_packages",
        ["intent_id"],
    )


def _create_publication_attempts() -> None:
    op.create_table(
        "publication_attempts",
        sa.Column("intent_id", sa.Uuid(), nullable=False),
        sa.Column("approval_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("state", publication_attempt_state, nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("lock_version", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_owner", sa.String(length=200), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "processing_started_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=100), nullable=True),
        sa.Column("last_error_detail", sa.String(length=500), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "attempt_no > 0",
            name=op.f("ck_publication_attempts_positive_attempt_number"),
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name=op.f("ck_publication_attempts_nonnegative_attempt_count"),
        ),
        sa.CheckConstraint(
            "max_attempts > 0",
            name=op.f("ck_publication_attempts_positive_max_attempts"),
        ),
        sa.CheckConstraint(
            "attempt_count <= max_attempts",
            name=op.f("ck_publication_attempts_attempts_within_limit"),
        ),
        sa.CheckConstraint(
            "lock_version > 0",
            name=op.f("ck_publication_attempts_positive_lock_version"),
        ),
        sa.CheckConstraint(
            "(state IN ('claimed', 'processing') "
            "AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL) "
            "OR (state NOT IN ('claimed', 'processing') "
            "AND lease_owner IS NULL AND lease_expires_at IS NULL)",
            name=op.f("ck_publication_attempts_lease_state"),
        ),
        sa.CheckConstraint(
            "(state = 'retry_wait' AND retry_at IS NOT NULL) "
            "OR (state <> 'retry_wait' AND retry_at IS NULL)",
            name=op.f("ck_publication_attempts_retry_state"),
        ),
        sa.CheckConstraint(
            "(state IN ('succeeded', 'failed', 'cancelled') "
            "AND completed_at IS NOT NULL) "
            "OR (state NOT IN ('succeeded', 'failed', 'cancelled') "
            "AND completed_at IS NULL)",
            name=op.f("ck_publication_attempts_terminal_state"),
        ),
        sa.ForeignKeyConstraint(
            ["approval_id"],
            ["publication_approvals.id"],
            name=op.f("fk_publication_attempts_approval_id_publication_approvals"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["intent_id"],
            ["publication_intents.id"],
            name=op.f("fk_publication_attempts_intent_id_publication_intents"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_publication_attempts")),
        sa.UniqueConstraint(
            "intent_id",
            "attempt_no",
            name="uq_publication_attempts_intent_number",
        ),
    )
    op.create_index(
        op.f("ix_publication_attempts_intent_id"),
        "publication_attempts",
        ["intent_id"],
    )
    op.create_index(
        op.f("ix_publication_attempts_approval_id"),
        "publication_attempts",
        ["approval_id"],
    )
    op.create_index(
        "ix_publication_attempts_claim",
        "publication_attempts",
        [
            "state",
            "retry_at",
            "lease_expires_at",
            "available_at",
            "created_at",
        ],
    )


def _create_publication_steps() -> None:
    op.create_table(
        "publication_steps",
        sa.Column("attempt_id", sa.Uuid(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("kind", publication_step_kind, nullable=False),
        sa.Column("publication_input_id", sa.Uuid(), nullable=True),
        sa.Column("state", publication_step_state, nullable=False),
        sa.Column("retry_class", publication_retry_class, nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False),
        sa.Column("max_retries", sa.Integer(), nullable=False),
        sa.Column("retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lock_version", sa.Integer(), nullable=False),
        sa.Column("effect_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("effect_completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("guard_epoch", sa.Integer(), nullable=True),
        sa.Column("remote_identifier", sa.String(length=200), nullable=True),
        sa.Column(
            "remote_expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("remote_url", sa.String(length=2048), nullable=True),
        sa.Column("package_id", sa.Uuid(), nullable=True),
        sa.Column("last_error_code", sa.String(length=100), nullable=True),
        sa.Column("last_error_detail", sa.String(length=500), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "ordinal > 0",
            name=op.f("ck_publication_steps_positive_ordinal"),
        ),
        sa.CheckConstraint(
            "retry_count >= 0",
            name=op.f("ck_publication_steps_nonnegative_retry_count"),
        ),
        sa.CheckConstraint(
            "max_retries >= 0",
            name=op.f("ck_publication_steps_nonnegative_max_retries"),
        ),
        sa.CheckConstraint(
            "retry_count <= max_retries",
            name=op.f("ck_publication_steps_retries_within_limit"),
        ),
        sa.CheckConstraint(
            "lock_version > 0",
            name=op.f("ck_publication_steps_positive_lock_version"),
        ),
        sa.CheckConstraint(
            "(kind = 'x_media_upload' AND publication_input_id IS NOT NULL) "
            "OR (kind <> 'x_media_upload' AND publication_input_id IS NULL)",
            name=op.f("ck_publication_steps_input_kind"),
        ),
        sa.CheckConstraint(
            "(state = 'retry_wait' AND retry_at IS NOT NULL) "
            "OR (state <> 'retry_wait' AND retry_at IS NULL)",
            name=op.f("ck_publication_steps_retry_state"),
        ),
        sa.CheckConstraint(
            "effect_completed_at IS NULL OR effect_started_at IS NOT NULL",
            name=op.f("ck_publication_steps_effect_time_pair"),
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id"],
            ["publication_attempts.id"],
            name=op.f("fk_publication_steps_attempt_id_publication_attempts"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["package_id"],
            ["publication_packages.id"],
            name=op.f("fk_publication_steps_package_id_publication_packages"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["publication_input_id"],
            ["publication_inputs.id"],
            name=op.f("fk_publication_steps_publication_input_id_publication_inputs"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_publication_steps")),
        sa.UniqueConstraint(
            "attempt_id",
            "ordinal",
            name="uq_publication_steps_attempt_ordinal",
        ),
    )
    op.create_index(
        op.f("ix_publication_steps_attempt_id"),
        "publication_steps",
        ["attempt_id"],
    )
    op.create_index(
        "ix_publication_steps_attempt_state",
        "publication_steps",
        ["attempt_id", "state", "ordinal"],
    )


def _create_publication_effect_events() -> None:
    op.create_table(
        "publication_effect_events",
        sa.Column("step_id", sa.Uuid(), nullable=False),
        sa.Column("request_no", sa.Integer(), nullable=False),
        sa.Column("step_kind", publication_effect_step_kind, nullable=False),
        sa.Column("event_type", sa.String(length=16), nullable=False),
        sa.Column("is_completion", sa.Boolean(), nullable=False),
        sa.Column("guard_epoch", sa.Integer(), nullable=False),
        sa.Column("remote_identifier", sa.String(length=200), nullable=True),
        sa.Column(
            "remote_expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "request_no > 0",
            name=op.f("ck_publication_effect_events_positive_request_number"),
        ),
        sa.CheckConstraint(
            "guard_epoch > 0",
            name=op.f("ck_publication_effect_events_positive_guard_epoch"),
        ),
        sa.CheckConstraint(
            "event_type IN ('started', 'succeeded', 'retryable', 'unknown', 'terminal')",
            name=op.f("ck_publication_effect_events_valid_event_type"),
        ),
        sa.CheckConstraint(
            "(event_type = 'started' AND NOT is_completion "
            "AND remote_identifier IS NULL AND remote_expires_at IS NULL "
            "AND error_code IS NULL) "
            "OR (event_type = 'succeeded' AND is_completion "
            "AND remote_identifier IS NOT NULL AND error_code IS NULL) "
            "OR (event_type IN ('retryable', 'unknown', 'terminal') "
            "AND is_completion AND error_code IS NOT NULL)",
            name=op.f("ck_publication_effect_events_event_contract"),
        ),
        sa.ForeignKeyConstraint(
            ["step_id"],
            ["publication_steps.id"],
            name=op.f("fk_publication_effect_events_step_id_publication_steps"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "id",
            name=op.f("pk_publication_effect_events"),
        ),
        sa.UniqueConstraint(
            "step_id",
            "request_no",
            "is_completion",
            name="uq_publication_effect_events_request_phase",
        ),
    )
    op.create_index(
        op.f("ix_publication_effect_events_step_id"),
        "publication_effect_events",
        ["step_id"],
    )
    op.create_index(
        "ix_publication_effect_events_step_request",
        "publication_effect_events",
        ["step_id", "request_no", "recorded_at"],
    )


def _insert_stopped_guard(connection: Any) -> None:
    guard = sa.table(
        "publication_provider_guards",
        sa.column("id", sa.Uuid()),
        sa.column("provider", sa.String()),
        sa.column("enabled", sa.Boolean()),
        sa.column("epoch", sa.Integer()),
        sa.column("lock_version", sa.Integer()),
        sa.column("reason", sa.String()),
        sa.column("changed_by_user_id", sa.Uuid()),
        sa.column("changed_at", sa.DateTime(timezone=True)),
    )
    connection.execute(
        guard.insert().values(
            id=_INITIAL_GUARD_ID,
            provider="global",
            enabled=False,
            epoch=1,
            lock_version=1,
            reason="publication is stopped by default",
            changed_by_user_id=None,
            changed_at=datetime.now(UTC),
        )
    )


def _create_guards(dialect_name: str) -> None:
    if dialect_name == "postgresql":
        _create_postgresql_guards()
    elif dialect_name == "sqlite":
        _create_sqlite_guards()


def _drop_guards(dialect_name: str) -> None:
    if dialect_name == "postgresql":
        for trigger, table in (
            ("publication_provider_guards_guard_mutation", "publication_provider_guards"),
            ("publication_packages_guard_mutation", "publication_packages"),
            (
                "publication_reconciliations_guard_mutation",
                "publication_reconciliations",
            ),
            (
                "publication_effect_events_guard_mutation",
                "publication_effect_events",
            ),
            ("publication_approvals_guard_mutation", "publication_approvals"),
            ("publication_intents_guard_update", "publication_intents"),
            ("publication_inputs_guard_mutation", "publication_inputs"),
        ):
            op.execute(sa.text(f"DROP TRIGGER IF EXISTS {trigger} ON {table}"))
        for function in (
            "gen_automation_guard_publication_provider_guard",
            "gen_automation_guard_publication_package",
            "gen_automation_guard_publication_reconciliation",
            "gen_automation_guard_publication_effect_event",
            "gen_automation_guard_publication_approval",
            "gen_automation_guard_publication_intent_update",
            "gen_automation_guard_publication_input",
        ):
            op.execute(sa.text(f"DROP FUNCTION IF EXISTS {function}()"))
    elif dialect_name == "sqlite":
        for trigger in (
            "publication_provider_guards_no_delete",
            "publication_provider_guards_fail_closed_insert",
            "publication_packages_immutable_delete",
            "publication_packages_immutable_update",
            "publication_reconciliations_immutable_delete",
            "publication_reconciliations_immutable_update",
            "publication_effect_events_immutable_delete",
            "publication_effect_events_immutable_update",
            "publication_approvals_immutable_delete",
            "publication_approvals_immutable_update",
            "publication_inputs_immutable_delete",
            "publication_inputs_immutable_update",
        ):
            op.execute(sa.text(f"DROP TRIGGER IF EXISTS {trigger}"))


def _create_sqlite_guards() -> None:
    for table in (
        "publication_inputs",
        "publication_approvals",
        "publication_reconciliations",
        "publication_effect_events",
        "publication_packages",
    ):
        op.execute(
            sa.text(
                f"CREATE TRIGGER {table}_immutable_update "
                f"BEFORE UPDATE ON {table} BEGIN "
                f"SELECT RAISE(ABORT, '{table} are append-only'); END"
            )
        )
        op.execute(
            sa.text(
                f"CREATE TRIGGER {table}_immutable_delete "
                f"BEFORE DELETE ON {table} BEGIN "
                f"SELECT RAISE(ABORT, '{table} are append-only'); END"
            )
        )
    op.execute(
        sa.text(
            "CREATE TRIGGER publication_provider_guards_fail_closed_insert "
            "BEFORE INSERT ON publication_provider_guards "
            "WHEN NEW.provider <> 'global' OR NEW.enabled <> 0 "
            "BEGIN SELECT RAISE(ABORT, "
            "'publication guard must be globally stopped'); END"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER publication_provider_guards_no_delete "
            "BEFORE DELETE ON publication_provider_guards "
            "BEGIN SELECT RAISE(ABORT, 'publication guard cannot be deleted'); END"
        )
    )


def _create_postgresql_guards() -> None:
    statements = (
        "CREATE OR REPLACE FUNCTION gen_automation_guard_publication_input() "
        "RETURNS trigger AS $$ BEGIN "
        "IF TG_OP <> 'INSERT' THEN "
        "RAISE EXCEPTION 'publication inputs are append-only'; END IF; "
        "IF NOT EXISTS ("
        "SELECT 1 FROM publication_intents AS intent "
        "JOIN release_versions AS version ON version.id = intent.release_version_id "
        "JOIN releases AS release ON release.id = intent.release_id "
        "JOIN derivative_outputs AS output ON output.id = NEW.derivative_output_id "
        "JOIN derivative_jobs AS job ON job.id = output.derivative_job_id "
        "JOIN assets AS asset ON asset.id = output.asset_id "
        "WHERE intent.id = NEW.intent_id "
        "AND intent.state = 'awaiting_approval' "
        "AND version.release_id = release.id "
        "AND release.current_version_no = version.version_no "
        "AND release.phase = 'ready_to_publish' "
        "AND job.release_version_id = version.id "
        "AND output.derivative_recipe_id = NEW.derivative_recipe_id "
        "AND output.asset_id = NEW.asset_id "
        "AND output.target = NEW.derivative_target "
        "AND output.asset_storage_backend = NEW.asset_storage_backend "
        "AND output.asset_storage_bucket = NEW.asset_storage_bucket "
        "AND output.asset_object_key = NEW.asset_object_key "
        "AND output.asset_object_version_id = NEW.asset_object_version_id "
        "AND output.asset_sha256 = NEW.asset_sha256 "
        "AND output.asset_content_type = NEW.asset_content_type "
        "AND output.asset_image_format = NEW.asset_image_format "
        "AND output.asset_width = NEW.asset_width "
        "AND output.asset_height = NEW.asset_height "
        "AND output.asset_byte_size = NEW.asset_byte_size "
        "AND asset.state = 'available' AND asset.kind = 'derivative'"
        ") THEN RAISE EXCEPTION 'publication input snapshot is invalid'; END IF; "
        "RETURN NEW; END; $$ LANGUAGE plpgsql",
        "CREATE OR REPLACE FUNCTION gen_automation_guard_publication_intent_update() "
        "RETURNS trigger AS $$ BEGIN "
        "IF TG_OP = 'DELETE' THEN "
        "RAISE EXCEPTION 'publication intents cannot be deleted'; END IF; "
        "IF OLD.release_id IS DISTINCT FROM NEW.release_id "
        "OR OLD.release_version_id IS DISTINCT FROM NEW.release_version_id "
        "OR OLD.target IS DISTINCT FROM NEW.target "
        "OR OLD.configuration IS DISTINCT FROM NEW.configuration "
        "OR OLD.configuration_sha256 IS DISTINCT FROM NEW.configuration_sha256 "
        "OR OLD.input_manifest_sha256 IS DISTINCT FROM NEW.input_manifest_sha256 "
        "OR OLD.intent_digest IS DISTINCT FROM NEW.intent_digest "
        "OR OLD.input_count IS DISTINCT FROM NEW.input_count "
        "OR OLD.credential_reference IS DISTINCT FROM NEW.credential_reference "
        "OR OLD.scheduled_at IS DISTINCT FROM NEW.scheduled_at "
        "OR OLD.public_preview_attester_name "
        "IS DISTINCT FROM NEW.public_preview_attester_name "
        "OR OLD.public_preview_attester_user_id "
        "IS DISTINCT FROM NEW.public_preview_attester_user_id "
        "OR OLD.public_preview_attested_at "
        "IS DISTINCT FROM NEW.public_preview_attested_at "
        "OR OLD.public_preview_attestation_timezone "
        "IS DISTINCT FROM NEW.public_preview_attestation_timezone "
        "OR OLD.public_preview_attestation_sha256 "
        "IS DISTINCT FROM NEW.public_preview_attestation_sha256 "
        "OR OLD.planned_by_user_id IS DISTINCT FROM NEW.planned_by_user_id "
        "OR OLD.planned_at IS DISTINCT FROM NEW.planned_at THEN "
        "RAISE EXCEPTION 'publication intent identity is immutable'; END IF; "
        "RETURN NEW; END; $$ LANGUAGE plpgsql",
        "CREATE OR REPLACE FUNCTION gen_automation_guard_publication_approval() "
        "RETURNS trigger AS $$ BEGIN "
        "IF TG_OP <> 'INSERT' THEN "
        "RAISE EXCEPTION 'publication approvals are append-only'; END IF; "
        "IF NOT EXISTS ("
        "SELECT 1 FROM publication_intents AS intent "
        "JOIN release_versions AS version ON version.id = intent.release_version_id "
        "JOIN releases AS release ON release.id = intent.release_id "
        "JOIN admin_users AS actor ON actor.id = NEW.actor_user_id "
        "WHERE intent.id = NEW.intent_id "
        "AND intent.intent_digest = NEW.intent_digest "
        "AND NEW.intent_lock_version = intent.lock_version + 1 "
        "AND actor.is_active AND actor.role IN ('owner', 'publisher') "
        "AND actor.role = NEW.actor_role "
        "AND version.release_id = release.id "
        "AND release.current_version_no = version.version_no "
        "AND release.phase = 'ready_to_publish' "
        "AND (SELECT count(*) FROM publication_inputs AS input "
        "WHERE input.intent_id = intent.id) = intent.input_count "
        "AND ("
        "(intent.target = 'x' "
        "AND NOT EXISTS (SELECT 1 FROM publication_inputs AS input "
        "WHERE input.intent_id = intent.id AND input.role <> 'x_teaser')) "
        "OR (intent.target = 'patreon' "
        "AND (SELECT count(*) FROM publication_inputs AS input "
        "WHERE input.intent_id = intent.id "
        "AND input.role = 'patreon_preview') = 1 "
        "AND EXISTS (SELECT 1 FROM publication_inputs AS input "
        "WHERE input.intent_id = intent.id AND input.role = 'patreon_content'))"
        ")"
        ") THEN RAISE EXCEPTION 'publication approval snapshot is invalid'; END IF; "
        "RETURN NEW; END; $$ LANGUAGE plpgsql",
        "CREATE OR REPLACE FUNCTION gen_automation_guard_publication_package() "
        "RETURNS trigger AS $$ BEGIN "
        "IF TG_OP <> 'INSERT' THEN "
        "RAISE EXCEPTION 'publication packages are append-only'; END IF; "
        "IF NOT EXISTS (SELECT 1 FROM publication_intents "
        "WHERE id = NEW.intent_id AND target = 'patreon') THEN "
        "RAISE EXCEPTION 'publication package target is invalid'; END IF; "
        "RETURN NEW; END; $$ LANGUAGE plpgsql",
        "CREATE OR REPLACE FUNCTION "
        "gen_automation_guard_publication_reconciliation() "
        "RETURNS trigger AS $$ BEGIN "
        "IF TG_OP <> 'INSERT' THEN "
        "RAISE EXCEPTION 'publication reconciliations are append-only'; END IF; "
        "IF NOT EXISTS ("
        "SELECT 1 FROM publication_intents AS intent "
        "JOIN admin_users AS actor ON actor.id = NEW.actor_user_id "
        "WHERE intent.id = NEW.intent_id "
        "AND intent.intent_digest = NEW.intent_digest "
        "AND intent.lock_version = NEW.intent_lock_version "
        "AND actor.is_active AND actor.role IN ('owner', 'publisher') "
        "AND actor.role = NEW.actor_role"
        ") THEN RAISE EXCEPTION 'publication reconciliation is invalid'; END IF; "
        "RETURN NEW; END; $$ LANGUAGE plpgsql",
        "CREATE OR REPLACE FUNCTION gen_automation_guard_publication_effect_event() "
        "RETURNS trigger AS $$ BEGIN "
        "IF TG_OP <> 'INSERT' THEN "
        "RAISE EXCEPTION 'publication effect events are append-only'; END IF; "
        "IF NOT EXISTS (SELECT 1 FROM publication_steps AS step "
        "WHERE step.id = NEW.step_id AND step.kind = NEW.step_kind "
        "AND step.kind IN ('x_media_upload', 'x_create_post')) THEN "
        "RAISE EXCEPTION 'publication effect event step is invalid'; END IF; "
        "IF NEW.event_type = 'started' THEN "
        "IF NEW.request_no <> COALESCE((SELECT max(event.request_no) + 1 "
        "FROM publication_effect_events AS event "
        "WHERE event.step_id = NEW.step_id AND event.event_type = 'started'), 1) "
        "THEN RAISE EXCEPTION 'publication request sequence is invalid'; END IF; "
        "ELSIF NOT EXISTS (SELECT 1 FROM publication_effect_events AS started "
        "WHERE started.step_id = NEW.step_id "
        "AND started.request_no = NEW.request_no "
        "AND started.event_type = 'started' "
        "AND started.guard_epoch = NEW.guard_epoch) THEN "
        "RAISE EXCEPTION 'publication request completion has no start'; END IF; "
        "END IF; RETURN NEW; END; $$ LANGUAGE plpgsql",
        "CREATE OR REPLACE FUNCTION gen_automation_guard_publication_provider_guard() "
        "RETURNS trigger AS $$ BEGIN "
        "IF TG_OP = 'DELETE' THEN "
        "RAISE EXCEPTION 'publication guard cannot be deleted'; END IF; "
        "IF TG_OP = 'INSERT' THEN "
        "IF NEW.provider <> 'global' OR NEW.enabled THEN "
        "RAISE EXCEPTION 'publication guard must be globally stopped'; END IF; "
        "RETURN NEW; END IF; "
        "IF NEW.provider IS DISTINCT FROM OLD.provider "
        "OR NEW.epoch <> OLD.epoch + 1 "
        "OR NEW.lock_version <> OLD.lock_version + 1 "
        "OR NEW.changed_at <= OLD.changed_at "
        "OR NEW.changed_by_user_id IS NULL "
        "OR NOT EXISTS (SELECT 1 FROM admin_users AS actor "
        "WHERE actor.id = NEW.changed_by_user_id "
        "AND actor.is_active AND actor.role = 'owner') THEN "
        "RAISE EXCEPTION 'publication guard transition is invalid'; END IF; "
        "RETURN NEW; END; $$ LANGUAGE plpgsql",
    )
    for statement in statements:
        op.execute(sa.text(statement))

    for statement in (
        "CREATE TRIGGER publication_inputs_guard_mutation "
        "BEFORE INSERT OR UPDATE OR DELETE ON publication_inputs FOR EACH ROW "
        "EXECUTE FUNCTION gen_automation_guard_publication_input()",
        "CREATE TRIGGER publication_intents_guard_update "
        "BEFORE UPDATE OR DELETE ON publication_intents FOR EACH ROW "
        "EXECUTE FUNCTION gen_automation_guard_publication_intent_update()",
        "CREATE TRIGGER publication_approvals_guard_mutation "
        "BEFORE INSERT OR UPDATE OR DELETE ON publication_approvals FOR EACH ROW "
        "EXECUTE FUNCTION gen_automation_guard_publication_approval()",
        "CREATE TRIGGER publication_packages_guard_mutation "
        "BEFORE INSERT OR UPDATE OR DELETE ON publication_packages FOR EACH ROW "
        "EXECUTE FUNCTION gen_automation_guard_publication_package()",
        "CREATE TRIGGER publication_reconciliations_guard_mutation "
        "BEFORE INSERT OR UPDATE OR DELETE ON publication_reconciliations "
        "FOR EACH ROW "
        "EXECUTE FUNCTION gen_automation_guard_publication_reconciliation()",
        "CREATE TRIGGER publication_effect_events_guard_mutation "
        "BEFORE INSERT OR UPDATE OR DELETE ON publication_effect_events "
        "FOR EACH ROW "
        "EXECUTE FUNCTION gen_automation_guard_publication_effect_event()",
        "CREATE TRIGGER publication_provider_guards_guard_mutation "
        "BEFORE INSERT OR UPDATE OR DELETE ON publication_provider_guards "
        "FOR EACH ROW "
        "EXECUTE FUNCTION gen_automation_guard_publication_provider_guard()",
    ):
        op.execute(sa.text(statement))
