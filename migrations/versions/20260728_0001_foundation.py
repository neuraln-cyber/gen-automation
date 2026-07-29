"""Create the control-plane foundation tables.

Revision ID: 20260728_0001
Revises:
Create Date: 2026-07-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260728_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
release_phase = sa.Enum(
    "draft",
    "validating",
    "ready",
    "generating",
    "reviewing",
    "approved",
    "rendering",
    "ready_to_publish",
    "publishing",
    "published",
    "paused",
    "cancelled",
    name="release_phase",
    native_enum=False,
    create_constraint=True,
)
resource_health = sa.Enum(
    "healthy",
    "warning",
    "blocked",
    name="resource_health",
    native_enum=False,
    create_constraint=True,
)
compliance_result = sa.Enum(
    "pending",
    "passed",
    "failed",
    "waived",
    name="compliance_result",
    native_enum=False,
    create_constraint=True,
)
outbox_status = sa.Enum(
    "pending",
    "processing",
    "succeeded",
    "dead_letter",
    name="outbox_status",
    native_enum=False,
    create_constraint=True,
)


def upgrade() -> None:
    op.create_table(
        "projects",
        sa.Column("slug", sa.String(length=80), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_projects")),
        sa.UniqueConstraint("slug", name=op.f("uq_projects_slug")),
    )
    op.create_table(
        "releases",
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("slug", sa.String(length=80), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("phase", release_phase, nullable=False),
        sa.Column("health", resource_health, nullable=False),
        sa.Column("current_version_no", sa.Integer(), nullable=False),
        sa.Column("desired_accepted_count", sa.Integer(), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True),
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
            "desired_accepted_count > 0",
            name=op.f("ck_releases_positive_desired_count"),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_releases_project_id_projects"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_releases")),
        sa.UniqueConstraint("project_id", "slug", name=op.f("uq_releases_project_id")),
    )
    op.create_index(op.f("ix_releases_project_id"), "releases", ["project_id"])
    op.create_table(
        "release_versions",
        sa.Column("release_id", sa.Uuid(), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("specification", json_type, nullable=False),
        sa.Column("specification_sha256", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["release_id"],
            ["releases.id"],
            name=op.f("fk_release_versions_release_id_releases"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_release_versions")),
        sa.UniqueConstraint(
            "release_id",
            "version_no",
            name=op.f("uq_release_versions_release_id"),
        ),
    )
    op.create_index(
        op.f("ix_release_versions_release_id"),
        "release_versions",
        ["release_id"],
    )
    op.create_table(
        "compliance_checks",
        sa.Column("release_version_id", sa.Uuid(), nullable=False),
        sa.Column("check_type", sa.String(length=100), nullable=False),
        sa.Column("result", compliance_result, nullable=False),
        sa.Column("evidence", json_type, nullable=False),
        sa.Column("checked_by", sa.String(length=200), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["release_version_id"],
            ["release_versions.id"],
            name=op.f("fk_compliance_checks_release_version_id_release_versions"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_compliance_checks")),
    )
    op.create_index(
        op.f("ix_compliance_checks_release_version_id"),
        "compliance_checks",
        ["release_version_id"],
    )
    op.create_table(
        "idempotency_records",
        sa.Column("scope", sa.String(length=200), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("request_sha256", sa.String(length=64), nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=False),
        sa.Column("response_body", json_type, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_idempotency_records")),
        sa.UniqueConstraint(
            "scope",
            "idempotency_key",
            name=op.f("uq_idempotency_records_scope"),
        ),
    )
    op.create_table(
        "audit_events",
        sa.Column("actor", sa.String(length=200), nullable=False),
        sa.Column("action", sa.String(length=200), nullable=False),
        sa.Column("resource_type", sa.String(length=100), nullable=False),
        sa.Column("resource_id", sa.Uuid(), nullable=False),
        sa.Column("correlation_id", sa.String(length=200), nullable=False),
        sa.Column("detail", json_type, nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_events")),
    )
    op.create_index(
        op.f("ix_audit_events_correlation_id"),
        "audit_events",
        ["correlation_id"],
    )
    op.create_index(
        op.f("ix_audit_events_resource_id"),
        "audit_events",
        ["resource_id"],
    )
    op.create_table(
        "outbox_events",
        sa.Column("topic", sa.String(length=200), nullable=False),
        sa.Column("aggregate_type", sa.String(length=100), nullable=False),
        sa.Column("aggregate_id", sa.Uuid(), nullable=False),
        sa.Column("payload", json_type, nullable=False),
        sa.Column("status", outbox_status, nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=100), nullable=True),
        sa.Column("last_error_detail", sa.Text(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_outbox_events")),
    )
    op.create_index(
        op.f("ix_outbox_events_aggregate_id"),
        "outbox_events",
        ["aggregate_id"],
    )
    op.create_index(
        "ix_outbox_claim",
        "outbox_events",
        ["status", "available_at", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_outbox_claim", table_name="outbox_events")
    op.drop_index(op.f("ix_outbox_events_aggregate_id"), table_name="outbox_events")
    op.drop_table("outbox_events")
    op.drop_index(op.f("ix_audit_events_resource_id"), table_name="audit_events")
    op.drop_index(op.f("ix_audit_events_correlation_id"), table_name="audit_events")
    op.drop_table("audit_events")
    op.drop_table("idempotency_records")
    op.drop_index(
        op.f("ix_compliance_checks_release_version_id"),
        table_name="compliance_checks",
    )
    op.drop_table("compliance_checks")
    op.drop_index(
        op.f("ix_release_versions_release_id"),
        table_name="release_versions",
    )
    op.drop_table("release_versions")
    op.drop_index(op.f("ix_releases_project_id"), table_name="releases")
    op.drop_table("releases")
    op.drop_table("projects")
