"""Add durable Salad orchestration, webhook inbox, and budget guards.

Revision ID: 20260728_0003
Revises: 20260728_0002
Create Date: 2026-07-28
"""

import hashlib
import json
from collections.abc import Sequence
from uuid import UUID, uuid5

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260728_0003"
down_revision: str | None = "20260728_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
inbox_status = sa.Enum(
    "received",
    "processing",
    "succeeded",
    "retry_wait",
    "dead_letter",
    name="inbox_status",
    native_enum=False,
    create_constraint=True,
)
salad_deployment_state = sa.Enum(
    "planned",
    "provisioning",
    "active",
    "degraded",
    "draining",
    "stopped",
    "unknown",
    "failed",
    name="salad_deployment_state",
    native_enum=False,
    create_constraint=True,
)
desired_deployment_state = sa.Enum(
    "active",
    "stopped",
    name="desired_deployment_state",
    native_enum=False,
    create_constraint=True,
)
budget_state = sa.Enum(
    "open",
    "blocked",
    name="budget_state",
    native_enum=False,
    create_constraint=True,
)
spend_entry_type = sa.Enum(
    "usage",
    "adjustment",
    name="spend_entry_type",
    native_enum=False,
    create_constraint=True,
)

_LEGACY_DEPLOYMENT_NAMESPACE = UUID("ed71d302-4781-4ddc-949b-3b0c1c75f95a")
_LEGACY_DEPLOYMENT_SCHEMA = "legacy-salad-deployment/v1"
_LEGACY_REQUEST_SCHEMA = "legacy-generation-request/v1"
_LEGACY_SUBMISSION_SCHEMA = "legacy-submission-key/v1"


def _canonical_sha256(value: object) -> str:
    serialized = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def _backfill_generation_attempts() -> None:
    connection = op.get_bind()
    attempts = sa.table(
        "generation_attempts",
        sa.column("id", sa.Uuid()),
        sa.column("job_id", sa.Uuid()),
        sa.column("provider", sa.String(length=50)),
        sa.column("worker_image_digest", sa.String(length=200)),
        sa.column("request_metadata", json_type),
        sa.column("salad_deployment_id", sa.Uuid()),
        sa.column("submission_key", sa.String(length=64)),
        sa.column("request_sha256", sa.String(length=64)),
    )
    jobs = sa.table(
        "generation_jobs",
        sa.column("id", sa.Uuid()),
        sa.column("release_version_id", sa.Uuid()),
        sa.column("parameters_sha256", sa.String(length=64)),
    )
    deployments = sa.table(
        "salad_deployments",
        sa.column("version_no", sa.Integer()),
        sa.column("config_sha256", sa.String(length=64)),
        sa.column("provider_configuration", json_type),
        sa.column("worker_image_digest", sa.String(length=255)),
        sa.column("organization_name", sa.String(length=200)),
        sa.column("project_name", sa.String(length=200)),
        sa.column("queue_name", sa.String(length=200)),
        sa.column("container_group_name", sa.String(length=200)),
        sa.column("state", salad_deployment_state),
        sa.column("desired_state", desired_deployment_state),
        sa.column("is_current", sa.Boolean()),
        sa.column("min_replicas", sa.Integer()),
        sa.column("max_replicas", sa.Integer()),
        sa.column("desired_queue_length", sa.Integer()),
        sa.column("max_hourly_cost_microusd", sa.BigInteger()),
        sa.column("provider_status", sa.String(length=100)),
        sa.column("last_error_code", sa.String(length=100)),
        sa.column("last_error_detail", sa.Text()),
        sa.column("lock_version", sa.Integer()),
        sa.column("id", sa.Uuid()),
    )

    historical_attempts = (
        connection.execute(
            sa.select(
                attempts.c.id,
                attempts.c.job_id,
                attempts.c.provider,
                attempts.c.worker_image_digest,
                attempts.c.request_metadata,
                jobs.c.release_version_id,
                jobs.c.parameters_sha256,
            )
            .select_from(attempts.join(jobs, jobs.c.id == attempts.c.job_id))
            .order_by(attempts.c.worker_image_digest, attempts.c.id)
        )
        .mappings()
        .all()
    )
    if not historical_attempts:
        return

    digests = sorted({row["worker_image_digest"] for row in historical_attempts})
    deployment_by_digest: dict[str, tuple[UUID, str]] = {}
    deployment_rows: list[dict[str, object]] = []
    for version_no, digest in enumerate(digests, start=1):
        provider_configuration = {
            "schema": _LEGACY_DEPLOYMENT_SCHEMA,
            "history_only": True,
            "worker_image_digest": digest,
            "max_hourly_cost_is_placeholder": True,
        }
        config_sha256 = _canonical_sha256(provider_configuration)
        deployment_id = uuid5(_LEGACY_DEPLOYMENT_NAMESPACE, config_sha256)
        name_suffix = config_sha256[:16]
        deployment_by_digest[digest] = (deployment_id, config_sha256)
        deployment_rows.append(
            {
                "version_no": version_no,
                "config_sha256": config_sha256,
                "provider_configuration": provider_configuration,
                "worker_image_digest": digest,
                "organization_name": "legacy-migration",
                "project_name": "legacy-migration",
                "queue_name": f"legacy-{name_suffix}-queue",
                "container_group_name": f"legacy-{name_suffix}-container",
                "state": "failed",
                "desired_state": "stopped",
                "is_current": False,
                "min_replicas": 0,
                "max_replicas": 1,
                "desired_queue_length": 1,
                "max_hourly_cost_microusd": 1,
                "provider_status": "legacy_history_only",
                "last_error_code": "legacy_history_only",
                "last_error_detail": (
                    "Created by migration 20260728_0003 to preserve historical "
                    "generation-attempt lineage; this deployment must not be reconciled."
                ),
                "lock_version": 1,
                "id": deployment_id,
            }
        )
    connection.execute(sa.insert(deployments), deployment_rows)

    update_attempt = (
        sa.update(attempts)
        .where(attempts.c.id == sa.bindparam("_attempt_id"))
        .values(
            salad_deployment_id=sa.bindparam("_salad_deployment_id"),
            submission_key=sa.bindparam("_submission_key"),
            request_sha256=sa.bindparam("_request_sha256"),
        )
    )
    attempt_rows: list[dict[str, object]] = []
    for row in historical_attempts:
        deployment_id, _ = deployment_by_digest[row["worker_image_digest"]]
        submission_key = _canonical_sha256(
            {
                "schema": _LEGACY_SUBMISSION_SCHEMA,
                "provider": row["provider"],
                "generation_attempt_id": str(row["id"]),
            }
        )
        request_sha256 = _canonical_sha256(
            {
                "schema": _LEGACY_REQUEST_SCHEMA,
                "provider": row["provider"],
                "generation_job_id": str(row["job_id"]),
                "release_version_id": str(row["release_version_id"]),
                "parameters_sha256": row["parameters_sha256"],
                "worker_image_digest": row["worker_image_digest"],
                "request_metadata": row["request_metadata"],
            }
        )
        attempt_rows.append(
            {
                "_attempt_id": row["id"],
                "_salad_deployment_id": deployment_id,
                "_submission_key": submission_key,
                "_request_sha256": request_sha256,
            }
        )
    connection.execute(update_attempt, attempt_rows)

    missing_values = connection.scalar(
        sa.select(sa.func.count())
        .select_from(attempts)
        .where(
            sa.or_(
                attempts.c.salad_deployment_id.is_(None),
                attempts.c.submission_key.is_(None),
                attempts.c.request_sha256.is_(None),
            )
        )
    )
    invalid_deployment_links = connection.scalar(
        sa.select(sa.func.count())
        .select_from(
            attempts.outerjoin(
                deployments,
                deployments.c.id == attempts.c.salad_deployment_id,
            )
        )
        .where(
            sa.or_(
                deployments.c.id.is_(None),
                deployments.c.worker_image_digest != attempts.c.worker_image_digest,
            )
        )
    )
    duplicate_submission = connection.execute(
        sa.select(attempts.c.provider, attempts.c.submission_key)
        .group_by(attempts.c.provider, attempts.c.submission_key)
        .having(sa.func.count() > 1)
        .limit(1)
    ).first()
    if missing_values or invalid_deployment_links or duplicate_submission is not None:
        raise RuntimeError("generation-attempt legacy backfill failed integrity verification")


def upgrade() -> None:
    with op.batch_alter_table("outbox_events") as batch_op:
        batch_op.drop_index("ix_outbox_claim")
        batch_op.add_column(sa.Column("dedupe_key", sa.String(length=200), nullable=True))
        batch_op.add_column(sa.Column("correlation_id", sa.String(length=200), nullable=True))
        batch_op.add_column(
            sa.Column(
                "max_attempts",
                sa.Integer(),
                server_default=sa.text("10"),
                nullable=False,
            )
        )
        batch_op.add_column(sa.Column("lease_owner", sa.String(length=200), nullable=True))
        batch_op.add_column(
            sa.Column(
                "lease_expires_at",
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )

    op.execute(
        sa.text(
            "UPDATE outbox_events "
            "SET dedupe_key = CAST(id AS VARCHAR), "
            "correlation_id = CAST(id AS VARCHAR)"
        )
    )

    with op.batch_alter_table("outbox_events") as batch_op:
        batch_op.alter_column(
            "dedupe_key",
            existing_type=sa.String(length=200),
            nullable=False,
        )
        batch_op.alter_column(
            "correlation_id",
            existing_type=sa.String(length=200),
            nullable=False,
        )
        batch_op.alter_column(
            "max_attempts",
            existing_type=sa.Integer(),
            server_default=None,
        )
        batch_op.create_check_constraint(
            "ck_outbox_events_nonnegative_attempts",
            "attempts >= 0",
        )
        batch_op.create_check_constraint(
            "ck_outbox_events_positive_max_attempts",
            "max_attempts > 0",
        )
        batch_op.create_check_constraint(
            "ck_outbox_events_attempts_within_limit",
            "attempts <= max_attempts",
        )
        batch_op.create_check_constraint(
            "ck_outbox_events_lease_pair",
            "(lease_owner IS NULL AND lease_expires_at IS NULL) OR "
            "(lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)",
        )
        batch_op.create_unique_constraint(
            "uq_outbox_events_topic",
            ["topic", "dedupe_key"],
        )
        batch_op.create_index(
            "ix_outbox_claim",
            [
                "status",
                "available_at",
                "lease_expires_at",
                "created_at",
            ],
            unique=False,
        )

    with op.batch_alter_table("generation_jobs") as batch_op:
        batch_op.add_column(
            sa.Column(
                "lock_version",
                sa.Integer(),
                server_default=sa.text("1"),
                nullable=False,
            )
        )

    with op.batch_alter_table("generation_jobs") as batch_op:
        batch_op.alter_column(
            "lock_version",
            existing_type=sa.Integer(),
            server_default=None,
        )
        batch_op.create_check_constraint(
            "ck_generation_jobs_valid_attempt_count",
            "attempt_count >= 0 AND attempt_count <= max_attempts",
        )
        batch_op.create_index(
            "ix_generation_jobs_schedule",
            ["state", "retry_at", "priority", "created_at"],
            unique=False,
        )

    op.create_table(
        "salad_deployments",
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("config_sha256", sa.String(length=64), nullable=False),
        sa.Column("provider_configuration", json_type, nullable=False),
        sa.Column(
            "worker_image_digest",
            sa.String(length=255),
            nullable=False,
        ),
        sa.Column("organization_name", sa.String(length=200), nullable=False),
        sa.Column("project_name", sa.String(length=200), nullable=False),
        sa.Column("queue_name", sa.String(length=200), nullable=False),
        sa.Column("provider_queue_id", sa.String(length=200), nullable=True),
        sa.Column(
            "container_group_name",
            sa.String(length=200),
            nullable=False,
        ),
        sa.Column(
            "provider_container_group_id",
            sa.String(length=200),
            nullable=True,
        ),
        sa.Column("state", salad_deployment_state, nullable=False),
        sa.Column(
            "desired_state",
            desired_deployment_state,
            nullable=False,
        ),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("min_replicas", sa.Integer(), nullable=False),
        sa.Column("max_replicas", sa.Integer(), nullable=False),
        sa.Column("desired_queue_length", sa.Integer(), nullable=False),
        sa.Column("observed_replicas", sa.Integer(), nullable=True),
        sa.Column("ready_replicas", sa.Integer(), nullable=True),
        sa.Column(
            "max_hourly_cost_microusd",
            sa.BigInteger(),
            nullable=False,
        ),
        sa.Column("provider_status", sa.String(length=100), nullable=True),
        sa.Column(
            "last_observed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "reconcile_after",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "unknown_since",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("last_error_code", sa.String(length=100), nullable=True),
        sa.Column("last_error_detail", sa.Text(), nullable=True),
        sa.Column("lock_version", sa.Integer(), nullable=False),
        sa.Column(
            "activated_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "stopped_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
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
            "min_replicas >= 0 AND min_replicas <= max_replicas",
            name=op.f("ck_salad_deployments_valid_replica_range"),
        ),
        sa.CheckConstraint(
            "version_no > 0",
            name=op.f("ck_salad_deployments_positive_version"),
        ),
        sa.CheckConstraint(
            "desired_queue_length > 0",
            name=op.f("ck_salad_deployments_positive_desired_queue_length"),
        ),
        sa.CheckConstraint(
            "max_replicas <= 1",
            name=op.f("ck_salad_deployments_single_creator_replica_limit"),
        ),
        sa.CheckConstraint(
            "observed_replicas IS NULL OR observed_replicas >= 0",
            name=op.f("ck_salad_deployments_nonnegative_observed_replicas"),
        ),
        sa.CheckConstraint(
            "ready_replicas IS NULL OR ready_replicas >= 0",
            name=op.f("ck_salad_deployments_nonnegative_ready_replicas"),
        ),
        sa.CheckConstraint(
            "ready_replicas IS NULL OR observed_replicas IS NULL "
            "OR ready_replicas <= observed_replicas",
            name=op.f("ck_salad_deployments_ready_not_above_observed"),
        ),
        sa.CheckConstraint(
            "max_hourly_cost_microusd > 0",
            name=op.f("ck_salad_deployments_positive_hourly_cost"),
        ),
        sa.CheckConstraint(
            "state NOT IN ('active', 'degraded', 'draining', 'stopped') "
            "OR (provider_queue_id IS NOT NULL "
            "AND provider_container_group_id IS NOT NULL)",
            name=op.f("ck_salad_deployments_remote_state_has_provider_resources"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_salad_deployments")),
        sa.UniqueConstraint(
            "provider_container_group_id",
            name=op.f("uq_salad_deployments_provider_container_group_id"),
        ),
        sa.UniqueConstraint(
            "provider_queue_id",
            name=op.f("uq_salad_deployments_provider_queue_id"),
        ),
        sa.UniqueConstraint(
            "version_no",
            name=op.f("uq_salad_deployments_version_no"),
        ),
    )
    op.create_index(
        "uq_salad_deployments_current",
        "salad_deployments",
        ["is_current"],
        unique=True,
        postgresql_where=sa.text("is_current"),
        sqlite_where=sa.text("is_current = 1"),
    )

    with op.batch_alter_table("generation_attempts") as batch_op:
        batch_op.add_column(sa.Column("salad_deployment_id", sa.Uuid(), nullable=True))
        batch_op.add_column(sa.Column("submission_key", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("request_sha256", sa.String(length=64), nullable=True))
        batch_op.add_column(
            sa.Column(
                "submit_started_at",
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "submitted_at",
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "last_observed_at",
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "unknown_since",
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )
        batch_op.add_column(sa.Column("provider_state", sa.String(length=50), nullable=True))
        batch_op.add_column(
            sa.Column(
                "cost_reservation_microusd",
                sa.BigInteger(),
                server_default=sa.text("0"),
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column(
                "reservation_released_at",
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "lock_version",
                sa.Integer(),
                server_default=sa.text("1"),
                nullable=False,
            )
        )

    _backfill_generation_attempts()

    with op.batch_alter_table("generation_attempts") as batch_op:
        batch_op.alter_column(
            "salad_deployment_id",
            existing_type=sa.Uuid(),
            nullable=False,
        )
        batch_op.alter_column(
            "submission_key",
            existing_type=sa.String(length=64),
            nullable=False,
        )
        batch_op.alter_column(
            "request_sha256",
            existing_type=sa.String(length=64),
            nullable=False,
        )
        batch_op.create_foreign_key(
            "fk_generation_attempts_salad_deployment_id_salad_deployments",
            "salad_deployments",
            ["salad_deployment_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_unique_constraint(
            "uq_generation_attempts_submission_key",
            ["provider", "submission_key"],
        )
        batch_op.create_check_constraint(
            "ck_generation_attempts_positive_attempt_no",
            "attempt_no > 0",
        )
        batch_op.create_check_constraint(
            "ck_generation_attempts_nonnegative_cost_reservation",
            "cost_reservation_microusd >= 0",
        )
        batch_op.create_check_constraint(
            "ck_generation_attempts_remote_state_has_provider_id",
            "state NOT IN "
            "('submitted', 'running', 'succeeded', "
            "'cancel_requested', 'cancelled') "
            "OR provider_external_id IS NOT NULL",
        )
        batch_op.create_check_constraint(
            "ck_generation_attempts_terminal_attempt_is_completed",
            "state NOT IN ('succeeded', 'failed', 'cancelled') OR completed_at IS NOT NULL",
        )
        batch_op.create_index(
            "ix_generation_attempts_salad_deployment_id",
            ["salad_deployment_id"],
            unique=False,
        )
        batch_op.create_index(
            "ix_generation_attempts_observe",
            ["state", "last_observed_at"],
            unique=False,
        )
        batch_op.alter_column(
            "cost_reservation_microusd",
            existing_type=sa.BigInteger(),
            server_default=None,
        )
        batch_op.alter_column(
            "lock_version",
            existing_type=sa.Integer(),
            server_default=None,
        )

    op.create_table(
        "webhook_receipts",
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("event_id", sa.String(length=512), nullable=False),
        sa.Column("event_type", sa.String(length=100), nullable=True),
        sa.Column("payload_sha256", sa.String(length=64), nullable=False),
        sa.Column("event_metadata", json_type, nullable=False),
        sa.Column(
            "provider_external_job_id",
            sa.String(length=200),
            nullable=True,
        ),
        sa.Column("generation_attempt_id", sa.Uuid(), nullable=True),
        sa.Column("status", inbox_status, nullable=False),
        sa.Column(
            "signature_timestamp",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "verified_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("lease_owner", sa.String(length=200), nullable=True),
        sa.Column(
            "lease_expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "processed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("last_error_code", sa.String(length=100), nullable=True),
        sa.Column("last_error_detail", sa.Text(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "attempts >= 0",
            name=op.f("ck_webhook_receipts_nonnegative_attempts"),
        ),
        sa.CheckConstraint(
            "max_attempts > 0",
            name=op.f("ck_webhook_receipts_positive_max_attempts"),
        ),
        sa.CheckConstraint(
            "attempts <= max_attempts",
            name=op.f("ck_webhook_receipts_attempts_within_limit"),
        ),
        sa.CheckConstraint(
            "(lease_owner IS NULL AND lease_expires_at IS NULL) OR "
            "(lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name=op.f("ck_webhook_receipts_lease_pair"),
        ),
        sa.ForeignKeyConstraint(
            ["generation_attempt_id"],
            ["generation_attempts.id"],
            name=op.f("fk_webhook_receipts_generation_attempt_id_generation_attempts"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_webhook_receipts")),
        sa.UniqueConstraint(
            "provider",
            "event_id",
            name=op.f("uq_webhook_receipts_provider"),
        ),
    )
    op.create_index(
        "ix_webhook_receipts_claim",
        "webhook_receipts",
        ["status", "available_at", "lease_expires_at", "received_at"],
    )
    op.create_index(
        op.f("ix_webhook_receipts_generation_attempt_id"),
        "webhook_receipts",
        ["generation_attempt_id"],
    )

    op.create_table(
        "provider_budget_guards",
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("daily_limit_microusd", sa.BigInteger(), nullable=False),
        sa.Column("monthly_limit_microusd", sa.BigInteger(), nullable=False),
        sa.Column("state", budget_state, nullable=False),
        sa.Column("blocked_reason", sa.String(length=200), nullable=True),
        sa.Column(
            "blocked_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("lock_version", sa.Integer(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "daily_limit_microusd > 0",
            name=op.f("ck_provider_budget_guards_positive_daily_limit"),
        ),
        sa.CheckConstraint(
            "monthly_limit_microusd > 0",
            name=op.f("ck_provider_budget_guards_positive_monthly_limit"),
        ),
        sa.CheckConstraint(
            "daily_limit_microusd <= monthly_limit_microusd",
            name=op.f("ck_provider_budget_guards_daily_not_above_monthly"),
        ),
        sa.CheckConstraint(
            "currency = 'USD'",
            name=op.f("ck_provider_budget_guards_usd_only"),
        ),
        sa.PrimaryKeyConstraint(
            "id",
            name=op.f("pk_provider_budget_guards"),
        ),
        sa.UniqueConstraint(
            "provider",
            name=op.f("uq_provider_budget_guards_provider"),
        ),
    )

    op.create_table(
        "provider_spend_entries",
        sa.Column("budget_guard_id", sa.Uuid(), nullable=False),
        sa.Column("dedupe_key", sa.String(length=200), nullable=False),
        sa.Column("entry_type", spend_entry_type, nullable=False),
        sa.Column("amount_microusd", sa.BigInteger(), nullable=False),
        sa.Column(
            "effective_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("salad_deployment_id", sa.Uuid(), nullable=True),
        sa.Column("generation_attempt_id", sa.Uuid(), nullable=True),
        sa.Column("source_reference", sa.String(length=200), nullable=True),
        sa.Column("detail", json_type, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "amount_microusd <> 0",
            name=op.f("ck_provider_spend_entries_nonzero_amount"),
        ),
        sa.ForeignKeyConstraint(
            ["budget_guard_id"],
            ["provider_budget_guards.id"],
            name=op.f("fk_provider_spend_entries_budget_guard_id_provider_budget_guards"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["generation_attempt_id"],
            ["generation_attempts.id"],
            name=op.f("fk_provider_spend_entries_generation_attempt_id_generation_attempts"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["salad_deployment_id"],
            ["salad_deployments.id"],
            name=op.f("fk_provider_spend_entries_salad_deployment_id_salad_deployments"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "id",
            name=op.f("pk_provider_spend_entries"),
        ),
        sa.UniqueConstraint(
            "budget_guard_id",
            "dedupe_key",
            name=op.f("uq_provider_spend_entries_budget_guard_id"),
        ),
    )
    op.create_index(
        "ix_provider_spend_effective",
        "provider_spend_entries",
        ["budget_guard_id", "effective_at"],
    )
    op.create_index(
        op.f("ix_provider_spend_entries_generation_attempt_id"),
        "provider_spend_entries",
        ["generation_attempt_id"],
    )
    op.create_index(
        op.f("ix_provider_spend_entries_salad_deployment_id"),
        "provider_spend_entries",
        ["salad_deployment_id"],
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_provider_spend_entries_salad_deployment_id"),
        table_name="provider_spend_entries",
    )
    op.drop_index(
        op.f("ix_provider_spend_entries_generation_attempt_id"),
        table_name="provider_spend_entries",
    )
    op.drop_index(
        "ix_provider_spend_effective",
        table_name="provider_spend_entries",
    )
    op.drop_table("provider_spend_entries")
    op.drop_table("provider_budget_guards")
    op.drop_index(
        op.f("ix_webhook_receipts_generation_attempt_id"),
        table_name="webhook_receipts",
    )
    op.drop_index(
        "ix_webhook_receipts_claim",
        table_name="webhook_receipts",
    )
    op.drop_table("webhook_receipts")

    with op.batch_alter_table("generation_attempts") as batch_op:
        batch_op.drop_index("ix_generation_attempts_observe")
        batch_op.drop_index("ix_generation_attempts_salad_deployment_id")
        batch_op.drop_constraint(
            "ck_generation_attempts_terminal_attempt_is_completed",
            type_="check",
        )
        batch_op.drop_constraint(
            "ck_generation_attempts_remote_state_has_provider_id",
            type_="check",
        )
        batch_op.drop_constraint(
            "ck_generation_attempts_nonnegative_cost_reservation",
            type_="check",
        )
        batch_op.drop_constraint(
            "ck_generation_attempts_positive_attempt_no",
            type_="check",
        )
        batch_op.drop_constraint(
            "uq_generation_attempts_submission_key",
            type_="unique",
        )
        batch_op.drop_constraint(
            "fk_generation_attempts_salad_deployment_id_salad_deployments",
            type_="foreignkey",
        )
        batch_op.drop_column("lock_version")
        batch_op.drop_column("reservation_released_at")
        batch_op.drop_column("cost_reservation_microusd")
        batch_op.drop_column("provider_state")
        batch_op.drop_column("unknown_since")
        batch_op.drop_column("last_observed_at")
        batch_op.drop_column("submitted_at")
        batch_op.drop_column("submit_started_at")
        batch_op.drop_column("request_sha256")
        batch_op.drop_column("submission_key")
        batch_op.drop_column("salad_deployment_id")

    op.drop_index(
        "uq_salad_deployments_current",
        table_name="salad_deployments",
    )
    op.drop_table("salad_deployments")

    with op.batch_alter_table("generation_jobs") as batch_op:
        batch_op.drop_index("ix_generation_jobs_schedule")
        batch_op.drop_constraint(
            "ck_generation_jobs_valid_attempt_count",
            type_="check",
        )
        batch_op.drop_column("lock_version")

    with op.batch_alter_table("outbox_events") as batch_op:
        batch_op.drop_index("ix_outbox_claim")
        batch_op.drop_constraint(
            "uq_outbox_events_topic",
            type_="unique",
        )
        batch_op.drop_constraint(
            "ck_outbox_events_lease_pair",
            type_="check",
        )
        batch_op.drop_constraint(
            "ck_outbox_events_attempts_within_limit",
            type_="check",
        )
        batch_op.drop_constraint(
            "ck_outbox_events_positive_max_attempts",
            type_="check",
        )
        batch_op.drop_constraint(
            "ck_outbox_events_nonnegative_attempts",
            type_="check",
        )
        batch_op.drop_column("lease_expires_at")
        batch_op.drop_column("lease_owner")
        batch_op.drop_column("max_attempts")
        batch_op.drop_column("correlation_id")
        batch_op.drop_column("dedupe_key")
        batch_op.create_index(
            "ix_outbox_claim",
            ["status", "available_at", "created_at"],
            unique=False,
        )
