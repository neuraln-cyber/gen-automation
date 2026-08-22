from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    DDL,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from gen_automation.db.base import (
    JSON_TYPE,
    Base,
    TimestampMixin,
    UuidPrimaryKeyMixin,
)
from gen_automation.domain.enums import (
    AdminEnrollmentState,
    AdminRole,
    ApprovalStatus,
    AssetKind,
    AssetScoreState,
    AssetState,
    BudgetState,
    ComplianceResult,
    DerivativeJobState,
    DesiredDeploymentState,
    ExperimentWarmLeaseState,
    FinishedSetArchiveState,
    GenerationAttemptState,
    GenerationModelFamily,
    GenerationState,
    InboxStatus,
    LoraImportJobState,
    LoraImportSource,
    ManagedLoraLifecycle,
    MegaDeliveryState,
    ModelArtifactKind,
    OutboxStatus,
    PublicationApprovalAction,
    PublicationAttemptState,
    PublicationIntentState,
    PublicationRetryClass,
    PublicationStepKind,
    PublicationStepState,
    PublicationTarget,
    RankingDisposition,
    ReleasePhase,
    ResourceHealth,
    ReviewDecisionValue,
    ReviewTaskState,
    SaladDeploymentPurpose,
    SaladDeploymentState,
    ScoringRunState,
    SemanticAssessmentState,
    SemanticFeedbackAgreement,
    SemanticGroundTruth,
    SemanticIssueCode,
    SemanticPromotionDecision,
    SemanticTrainingKind,
    SemanticTrainingState,
    SemanticVerdict,
    SpendEntryType,
)


def _lower_hex_check(column_name: str) -> str:
    expression = column_name
    for character in "0123456789abcdef":
        expression = f"replace({expression}, '{character}', '')"
    return (
        f"length({column_name}) = 64 AND {column_name} = lower({column_name}) "
        f"AND length({expression}) = 0"
    )


class Project(UuidPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "projects"

    slug: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)

    releases: Mapped[list["Release"]] = relationship(back_populates="project")


class Release(UuidPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "releases"
    __table_args__ = (
        UniqueConstraint("project_id", "slug"),
        CheckConstraint("desired_accepted_count > 0", name="positive_desired_count"),
    )

    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    slug: Mapped[str] = mapped_column(String(80), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    phase: Mapped[ReleasePhase] = mapped_column(
        Enum(
            ReleasePhase,
            name="release_phase",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda members: [member.value for member in members],
            length=16,
        ),
        nullable=False,
        default=ReleasePhase.DRAFT,
    )
    health: Mapped[ResourceHealth] = mapped_column(
        Enum(
            ResourceHealth,
            name="resource_health",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda members: [member.value for member in members],
            length=7,
        ),
        nullable=False,
        default=ResourceHealth.HEALTHY,
    )
    current_version_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    desired_accepted_count: Mapped[int] = mapped_column(Integer, nullable=False)
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lock_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    project: Mapped[Project] = relationship(back_populates="releases")
    versions: Mapped[list["ReleaseVersion"]] = relationship(back_populates="release")


class ReleaseVersion(UuidPrimaryKeyMixin, Base):
    __tablename__ = "release_versions"
    __table_args__ = (UniqueConstraint("release_id", "version_no"),)

    release_id: Mapped[UUID] = mapped_column(
        ForeignKey("releases.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    specification: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False)
    specification_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[str] = mapped_column(String(200), nullable=False, default="system")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )

    release: Mapped[Release] = relationship(back_populates="versions")
    generation_jobs: Mapped[list["GenerationJob"]] = relationship(back_populates="release_version")


class WildcardLibrary(UuidPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "wildcard_libraries"
    __table_args__ = (
        CheckConstraint("current_version_no > 0", name="positive_current_version"),
        CheckConstraint("lock_version > 0", name="positive_lock_version"),
    )

    name: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    current_version_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    lock_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_by: Mapped[str] = mapped_column(String(200), nullable=False)

    versions: Mapped[list["WildcardLibraryVersion"]] = relationship(
        back_populates="library",
        order_by="WildcardLibraryVersion.version_no",
    )


class WildcardLibraryVersion(UuidPrimaryKeyMixin, Base):
    __tablename__ = "wildcard_library_versions"
    __table_args__ = (
        UniqueConstraint("library_id", "version_no"),
        CheckConstraint("version_no > 0", name="positive_version"),
        CheckConstraint("entry_count > 0", name="positive_entry_count"),
        CheckConstraint(
            "entry_count <= 2000",
            name="entry_count_within_limit",
        ),
        Index(
            "ix_wildcard_library_versions_library_created",
            "library_id",
            "created_at",
        ),
    )

    library_id: Mapped[UUID] = mapped_column(
        ForeignKey("wildcard_libraries.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    entries: Mapped[list[str]] = mapped_column(JSON_TYPE, nullable=False)
    entry_count: Mapped[int] = mapped_column(Integer, nullable=False)
    entries_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    library: Mapped[WildcardLibrary] = relationship(back_populates="versions")


class ComplianceCheck(UuidPrimaryKeyMixin, Base):
    __tablename__ = "compliance_checks"

    release_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("release_versions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    check_type: Mapped[str] = mapped_column(String(100), nullable=False)
    result: Mapped[ComplianceResult] = mapped_column(
        Enum(
            ComplianceResult,
            name="compliance_result",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda members: [member.value for member in members],
            length=7,
        ),
        nullable=False,
    )
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False)
    checked_by: Mapped[str] = mapped_column(String(200), nullable=False)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class IdempotencyRecord(UuidPrimaryKeyMixin, Base):
    __tablename__ = "idempotency_records"
    __table_args__ = (UniqueConstraint("scope", "idempotency_key"),)

    scope: Mapped[str] = mapped_column(String(200), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    response_status: Mapped[int] = mapped_column(Integer, nullable=False)
    response_body: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditEvent(UuidPrimaryKeyMixin, Base):
    __tablename__ = "audit_events"

    actor: Mapped[str] = mapped_column(String(200), nullable=False)
    action: Mapped[str] = mapped_column(String(200), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(100), nullable=False)
    resource_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    correlation_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class OutboxEvent(UuidPrimaryKeyMixin, Base):
    __tablename__ = "outbox_events"
    __table_args__ = (
        CheckConstraint("attempts >= 0", name="nonnegative_attempts"),
        CheckConstraint("max_attempts > 0", name="positive_max_attempts"),
        CheckConstraint("attempts <= max_attempts", name="attempts_within_limit"),
        CheckConstraint(
            "(lease_owner IS NULL AND lease_expires_at IS NULL) OR "
            "(lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name="lease_pair",
        ),
        UniqueConstraint("topic", "dedupe_key"),
        Index(
            "ix_outbox_claim",
            "status",
            "available_at",
            "lease_expires_at",
            "created_at",
        ),
    )

    topic: Mapped[str] = mapped_column(String(200), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(200), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(200), nullable=False)
    aggregate_type: Mapped[str] = mapped_column(String(100), nullable=False)
    aggregate_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False)
    status: Mapped[OutboxStatus] = mapped_column(
        Enum(
            OutboxStatus,
            name="outbox_status",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda members: [member.value for member in members],
            length=11,
        ),
        nullable=False,
        default=OutboxStatus.PENDING,
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(200))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(100))
    last_error_detail: Mapped[str | None] = mapped_column(Text)


class GenerationJob(UuidPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "generation_jobs"
    __table_args__ = (
        UniqueConstraint("release_version_id", "logical_key"),
        CheckConstraint("expected_output_count > 0", name="positive_expected_outputs"),
        CheckConstraint("max_attempts > 0", name="positive_max_attempts"),
        CheckConstraint(
            "attempt_count >= 0 AND attempt_count <= max_attempts",
            name="valid_attempt_count",
        ),
        Index(
            "ix_generation_jobs_schedule",
            "state",
            "retry_at",
            "priority",
            "created_at",
        ),
    )

    release_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("release_versions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    logical_key: Mapped[str] = mapped_column(String(64), nullable=False)
    parameters: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False)
    parameters_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(50), nullable=False, default="salad")
    state: Mapped[GenerationState] = mapped_column(
        Enum(
            GenerationState,
            name="generation_state",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda members: [member.value for member in members],
            length=16,
        ),
        nullable=False,
        default=GenerationState.QUEUED,
    )
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    expected_output_count: Mapped[int] = mapped_column(Integer, nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    lock_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    lease_owner: Mapped[str | None] = mapped_column(String(200))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(100))
    last_error_detail: Mapped[str | None] = mapped_column(Text)

    release_version: Mapped[ReleaseVersion] = relationship(back_populates="generation_jobs")
    attempts: Mapped[list["GenerationAttempt"]] = relationship(back_populates="job")
    assets: Mapped[list["Asset"]] = relationship(back_populates="generation_job")


class GenerationAttempt(UuidPrimaryKeyMixin, Base):
    __tablename__ = "generation_attempts"
    __table_args__ = (
        UniqueConstraint("job_id", "attempt_no"),
        UniqueConstraint(
            "provider",
            "provider_external_id",
            name="uq_generation_attempts_provider_external_id",
        ),
        UniqueConstraint(
            "provider",
            "submission_key",
            name="uq_generation_attempts_submission_key",
        ),
        CheckConstraint("attempt_no > 0", name="positive_attempt_no"),
        CheckConstraint(
            "cost_reservation_microusd >= 0",
            name="nonnegative_cost_reservation",
        ),
        CheckConstraint(
            "state NOT IN "
            "('submitted', 'running', 'succeeded', 'cancel_requested', 'cancelled') "
            "OR provider_external_id IS NOT NULL",
            name="remote_state_has_provider_id",
        ),
        CheckConstraint(
            "state NOT IN ('succeeded', 'failed', 'cancelled') OR completed_at IS NOT NULL",
            name="terminal_attempt_is_completed",
        ),
        Index("ix_generation_attempts_observe", "state", "last_observed_at"),
    )

    job_id: Mapped[UUID] = mapped_column(
        ForeignKey("generation_jobs.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    salad_deployment_id: Mapped[UUID] = mapped_column(
        ForeignKey("salad_deployments.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    provider_external_id: Mapped[str | None] = mapped_column(String(200))
    submission_key: Mapped[str] = mapped_column(String(64), nullable=False)
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[GenerationAttemptState] = mapped_column(
        Enum(
            GenerationAttemptState,
            name="generation_attempt_state",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda members: [member.value for member in members],
            length=16,
        ),
        nullable=False,
        default=GenerationAttemptState.CREATED,
    )
    worker_image_digest: Mapped[str] = mapped_column(String(200), nullable=False)
    request_metadata: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False)
    response_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSON_TYPE)
    submit_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    unknown_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider_state: Mapped[str | None] = mapped_column(String(50))
    cost_reservation_microusd: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
    )
    reservation_released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lock_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    job: Mapped[GenerationJob] = relationship(back_populates="attempts")


class Asset(UuidPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "assets"
    __table_args__ = (
        UniqueConstraint("generation_job_id", "output_index", "kind"),
        UniqueConstraint(
            "storage_backend",
            "storage_bucket",
            "object_key",
            name="uq_assets_master_object",
        ),
        UniqueConstraint(
            "storage_backend",
            "storage_bucket",
            "staging_object_key",
            name="uq_assets_staging_object",
        ),
        CheckConstraint("output_index IS NULL OR output_index >= 0", name="valid_output_index"),
        CheckConstraint("byte_size IS NULL OR byte_size >= 0", name="valid_byte_size"),
        CheckConstraint("width IS NULL OR width > 0", name="valid_width"),
        CheckConstraint("height IS NULL OR height > 0", name="valid_height"),
        CheckConstraint(
            "kind <> 'raw_master' OR (generation_job_id IS NOT NULL AND output_index IS NOT NULL)",
            name="raw_master_has_output",
        ),
        CheckConstraint(
            "state NOT IN ('uploading', 'verifying') OR staging_object_key IS NOT NULL",
            name="active_upload_has_staging_key",
        ),
        CheckConstraint(
            "state <> 'available' OR "
            "(object_key IS NOT NULL AND sha256 IS NOT NULL "
            "AND content_type IS NOT NULL AND image_format IS NOT NULL "
            "AND width IS NOT NULL AND height IS NOT NULL AND byte_size IS NOT NULL)",
            name="available_asset_complete",
        ),
    )

    release_id: Mapped[UUID] = mapped_column(
        ForeignKey("releases.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    generation_job_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("generation_jobs.id", ondelete="RESTRICT"),
        index=True,
    )
    output_index: Mapped[int | None] = mapped_column(Integer)
    kind: Mapped[AssetKind] = mapped_column(
        Enum(
            AssetKind,
            name="asset_kind",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda members: [member.value for member in members],
            length=13,
        ),
        nullable=False,
    )
    state: Mapped[AssetState] = mapped_column(
        Enum(
            AssetState,
            name="asset_state",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda members: [member.value for member in members],
            length=13,
        ),
        nullable=False,
        default=AssetState.EXPECTED,
    )
    storage_backend: Mapped[str] = mapped_column(String(50), nullable=False)
    storage_bucket: Mapped[str] = mapped_column(String(255), nullable=False)
    staging_object_key: Mapped[str | None] = mapped_column(String(1024))
    object_key: Mapped[str | None] = mapped_column(String(1024))
    object_version_id: Mapped[str | None] = mapped_column(String(1024))
    sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    content_type: Mapped[str | None] = mapped_column(String(100))
    image_format: Mapped[str | None] = mapped_column(String(20))
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    byte_size: Mapped[int | None] = mapped_column(BigInteger)
    asset_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSON_TYPE,
        nullable=False,
        default=dict,
    )
    verification_error_code: Mapped[str | None] = mapped_column(String(100))
    verification_error_detail: Mapped[str | None] = mapped_column(Text)
    verification_lease_owner: Mapped[str | None] = mapped_column(String(200))
    verification_lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    available_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    purge_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    generation_job: Mapped[GenerationJob | None] = relationship(back_populates="assets")


class AssetLineage(UuidPrimaryKeyMixin, Base):
    __tablename__ = "asset_lineage"
    __table_args__ = (
        UniqueConstraint(
            "parent_asset_id",
            "child_asset_id",
            "relation",
            "recipe_version",
        ),
        CheckConstraint("parent_asset_id <> child_asset_id", name="not_self_referential"),
    )

    parent_asset_id: Mapped[UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    child_asset_id: Mapped[UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    relation: Mapped[str] = mapped_column(String(50), nullable=False)
    recipe_version: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class WebhookReceipt(UuidPrimaryKeyMixin, Base):
    __tablename__ = "webhook_receipts"
    __table_args__ = (
        UniqueConstraint("provider", "event_id"),
        CheckConstraint("attempts >= 0", name="nonnegative_attempts"),
        CheckConstraint("max_attempts > 0", name="positive_max_attempts"),
        CheckConstraint("attempts <= max_attempts", name="attempts_within_limit"),
        CheckConstraint(
            "(lease_owner IS NULL AND lease_expires_at IS NULL) OR "
            "(lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name="lease_pair",
        ),
        Index(
            "ix_webhook_receipts_claim",
            "status",
            "available_at",
            "lease_expires_at",
            "received_at",
        ),
    )

    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    event_id: Mapped[str] = mapped_column(String(512), nullable=False)
    event_type: Mapped[str | None] = mapped_column(String(100))
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    event_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSON_TYPE,
        nullable=False,
    )
    provider_external_job_id: Mapped[str | None] = mapped_column(String(200))
    generation_attempt_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("generation_attempts.id", ondelete="RESTRICT"),
        index=True,
    )
    status: Mapped[InboxStatus] = mapped_column(
        Enum(
            InboxStatus,
            name="inbox_status",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda members: [member.value for member in members],
            length=11,
        ),
        nullable=False,
        default=InboxStatus.RECEIVED,
    )
    signature_timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    verified_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    lease_owner: Mapped[str | None] = mapped_column(String(200))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(100))
    last_error_detail: Mapped[str | None] = mapped_column(Text)


class SaladDeployment(UuidPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "salad_deployments"
    __table_args__ = (
        CheckConstraint(
            "min_replicas >= 0 AND min_replicas <= max_replicas",
            name="valid_replica_range",
        ),
        CheckConstraint("version_no > 0", name="positive_version"),
        CheckConstraint(
            "desired_queue_length > 0",
            name="positive_desired_queue_length",
        ),
        CheckConstraint(
            "runtime_artifact_manifest_sha256 IS NULL OR ("
            + _lower_hex_check("runtime_artifact_manifest_sha256")
            + ")",
            name="valid_runtime_artifact_manifest_sha256",
        ),
        CheckConstraint(
            "max_replicas <= 1",
            name="single_creator_replica_limit",
        ),
        CheckConstraint(
            "observed_replicas IS NULL OR observed_replicas >= 0",
            name="nonnegative_observed_replicas",
        ),
        CheckConstraint(
            "ready_replicas IS NULL OR ready_replicas >= 0",
            name="nonnegative_ready_replicas",
        ),
        CheckConstraint(
            "ready_replicas IS NULL OR observed_replicas IS NULL "
            "OR ready_replicas <= observed_replicas",
            name="ready_not_above_observed",
        ),
        CheckConstraint(
            "max_hourly_cost_microusd > 0",
            name="positive_hourly_cost",
        ),
        CheckConstraint(
            "billing_accumulated_microseconds >= 0",
            name="nonnegative_billing_runtime",
        ),
        CheckConstraint(
            "(billing_active_instance_id IS NULL AND billing_active_started_at IS NULL) "
            "OR (billing_active_instance_id IS NOT NULL "
            "AND billing_active_started_at IS NOT NULL)",
            name="billing_active_pair",
        ),
        CheckConstraint(
            "billing_session_started_at IS NOT NULL "
            "OR (billing_session_ended_at IS NULL "
            "AND billing_active_instance_id IS NULL "
            "AND billing_active_started_at IS NULL "
            "AND billing_accumulated_microseconds = 0)",
            name="billing_session_required",
        ),
        CheckConstraint(
            "billing_session_ended_at IS NULL OR billing_active_instance_id IS NULL",
            name="ended_billing_not_active",
        ),
        CheckConstraint(
            "billing_active_started_at IS NULL "
            "OR billing_session_started_at <= billing_active_started_at",
            name="billing_active_after_session_start",
        ),
        CheckConstraint(
            "billing_session_ended_at IS NULL "
            "OR billing_session_started_at <= billing_session_ended_at",
            name="billing_end_after_session_start",
        ),
        CheckConstraint(
            "state NOT IN ('active', 'degraded', 'draining', 'stopped') "
            "OR (provider_queue_id IS NOT NULL "
            "AND provider_container_group_id IS NOT NULL)",
            name="remote_state_has_provider_resources",
        ),
        Index(
            "uq_salad_deployments_current_purpose",
            "purpose",
            unique=True,
            postgresql_where=text("is_current"),
            sqlite_where=text("is_current = 1"),
        ),
    )

    version_no: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        unique=True,
    )
    config_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    runtime_artifact_manifest_sha256: Mapped[str | None] = mapped_column(String(64))
    runtime_managed_lora_sha256s: Mapped[list[str] | None] = mapped_column(JSON_TYPE)
    provider_configuration: Mapped[dict[str, Any]] = mapped_column(
        JSON_TYPE,
        nullable=False,
        default=dict,
    )
    worker_image_digest: Mapped[str] = mapped_column(String(255), nullable=False)
    organization_name: Mapped[str] = mapped_column(String(200), nullable=False)
    project_name: Mapped[str] = mapped_column(String(200), nullable=False)
    queue_name: Mapped[str] = mapped_column(String(200), nullable=False)
    provider_queue_id: Mapped[str | None] = mapped_column(
        String(200),
        unique=True,
    )
    container_group_name: Mapped[str] = mapped_column(String(200), nullable=False)
    provider_container_group_id: Mapped[str | None] = mapped_column(
        String(200),
        unique=True,
    )
    purpose: Mapped[SaladDeploymentPurpose] = mapped_column(
        Enum(
            SaladDeploymentPurpose,
            name="salad_deployment_purpose",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda members: [member.value for member in members],
            length=5,
        ),
        nullable=False,
        default=SaladDeploymentPurpose.IMAGE,
        server_default=text("'image'"),
    )
    state: Mapped[SaladDeploymentState] = mapped_column(
        Enum(
            SaladDeploymentState,
            name="salad_deployment_state",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda members: [member.value for member in members],
            length=12,
        ),
        nullable=False,
        default=SaladDeploymentState.PLANNED,
    )
    desired_state: Mapped[DesiredDeploymentState] = mapped_column(
        Enum(
            DesiredDeploymentState,
            name="desired_deployment_state",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda members: [member.value for member in members],
            length=7,
        ),
        nullable=False,
        default=DesiredDeploymentState.ACTIVE,
    )
    administrative_stop_reason: Mapped[str | None] = mapped_column(String(100))
    is_current: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )
    min_replicas: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_replicas: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    desired_queue_length: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
    )
    observed_replicas: Mapped[int | None] = mapped_column(Integer)
    ready_replicas: Mapped[int | None] = mapped_column(Integer)
    max_hourly_cost_microusd: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
    )
    provider_status: Mapped[str | None] = mapped_column(String(100))
    last_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reconcile_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    unknown_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(100))
    last_error_detail: Mapped[str | None] = mapped_column(Text)
    lock_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    billing_session_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    billing_session_ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    billing_accumulated_microseconds: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
    )
    billing_active_instance_id: Mapped[str | None] = mapped_column(String(200))
    billing_active_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    billing_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    billing_observation_stale: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )
    billing_estimated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class ExperimentWarmLease(UuidPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "experiment_warm_leases"
    __table_args__ = (
        CheckConstraint(
            "started_at < expires_at AND expires_at <= hard_expires_at",
            name="valid_expiry_window",
        ),
        CheckConstraint(
            "max_cost_microusd > 0",
            name="positive_max_cost",
        ),
        CheckConstraint(
            "idle_ttl_seconds >= 60 AND idle_ttl_seconds <= 5400",
            name="valid_idle_ttl",
        ),
        CheckConstraint(
            "provider_version IS NULL OR provider_version > 0",
            name="positive_provider_version",
        ),
        CheckConstraint(
            "lock_version > 0",
            name="positive_lock_version",
        ),
        CheckConstraint(
            "(state IN ('starting', 'active', 'ending') AND ended_at IS NULL) "
            "OR (state IN ('ended', 'expired', 'failed') AND ended_at IS NOT NULL)",
            name="terminal_end_timestamp",
        ),
        Index(
            "uq_experiment_warm_leases_live_deployment",
            "salad_deployment_id",
            unique=True,
            postgresql_where=text("state IN ('starting', 'active', 'ending')"),
            sqlite_where=text("state IN ('starting', 'active', 'ending')"),
        ),
        Index(
            "ix_experiment_warm_leases_expiry",
            "state",
            "expires_at",
            "hard_expires_at",
        ),
    )

    salad_deployment_id: Mapped[UUID] = mapped_column(
        ForeignKey("salad_deployments.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    state: Mapped[ExperimentWarmLeaseState] = mapped_column(
        Enum(
            ExperimentWarmLeaseState,
            name="experiment_warm_lease_state",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda members: [member.value for member in members],
            length=8,
        ),
        nullable=False,
        default=ExperimentWarmLeaseState.STARTING,
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    hard_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_activity_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    idle_ttl_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    max_cost_microusd: Mapped[int] = mapped_column(BigInteger, nullable=False)
    provider_version: Mapped[int | None] = mapped_column(Integer)
    requested_checkpoint_sha256: Mapped[str | None] = mapped_column(String(64))
    requested_lora_sha256s: Mapped[list[str] | None] = mapped_column(JSON_TYPE)
    created_by: Mapped[str] = mapped_column(String(200), nullable=False)
    lock_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class ProviderBudgetGuard(UuidPrimaryKeyMixin, Base):
    __tablename__ = "provider_budget_guards"
    __table_args__ = (
        CheckConstraint("daily_limit_microusd > 0", name="positive_daily_limit"),
        CheckConstraint(
            "monthly_limit_microusd > 0",
            name="positive_monthly_limit",
        ),
        CheckConstraint(
            "daily_limit_microusd <= monthly_limit_microusd",
            name="daily_not_above_monthly",
        ),
        CheckConstraint("currency = 'USD'", name="usd_only"),
    )

    provider: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    daily_limit_microusd: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
    )
    monthly_limit_microusd: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
    )
    state: Mapped[BudgetState] = mapped_column(
        Enum(
            BudgetState,
            name="budget_state",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda members: [member.value for member in members],
            length=7,
        ),
        nullable=False,
        default=BudgetState.OPEN,
    )
    blocked_reason: Mapped[str | None] = mapped_column(String(200))
    blocked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lock_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )


class ProviderSpendEntry(UuidPrimaryKeyMixin, Base):
    __tablename__ = "provider_spend_entries"
    __table_args__ = (
        UniqueConstraint("budget_guard_id", "dedupe_key"),
        CheckConstraint("amount_microusd <> 0", name="nonzero_amount"),
        Index("ix_provider_spend_effective", "budget_guard_id", "effective_at"),
    )

    budget_guard_id: Mapped[UUID] = mapped_column(
        ForeignKey("provider_budget_guards.id", ondelete="RESTRICT"),
        nullable=False,
    )
    dedupe_key: Mapped[str] = mapped_column(String(200), nullable=False)
    entry_type: Mapped[SpendEntryType] = mapped_column(
        Enum(
            SpendEntryType,
            name="spend_entry_type",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda members: [member.value for member in members],
            length=10,
        ),
        nullable=False,
    )
    amount_microusd: Mapped[int] = mapped_column(BigInteger, nullable=False)
    effective_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    salad_deployment_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("salad_deployments.id", ondelete="RESTRICT"),
        index=True,
    )
    generation_attempt_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("generation_attempts.id", ondelete="RESTRICT"),
        index=True,
    )
    source_reference: Mapped[str | None] = mapped_column(String(200))
    detail: Mapped[dict[str, Any]] = mapped_column(
        JSON_TYPE,
        nullable=False,
        default=dict,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )


class ScoringRun(UuidPrimaryKeyMixin, Base):
    __tablename__ = "scoring_runs"
    __table_args__ = (
        UniqueConstraint(
            "release_version_id",
            "config_sha256",
            "scorer_version",
            name="uq_scoring_runs_identity",
        ),
        CheckConstraint("asset_count > 0", name="positive_asset_count"),
        CheckConstraint("max_attempts > 0", name="positive_max_attempts"),
        CheckConstraint(
            "(state = 'completed' AND completed_at IS NOT NULL "
            "AND ranking_manifest_sha256 IS NOT NULL "
            "AND length(ranking_manifest_sha256) = 64) "
            "OR (state <> 'completed' AND completed_at IS NULL "
            "AND ranking_manifest_sha256 IS NULL)",
            name="completion_snapshot",
        ),
    )

    release_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("release_versions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    configuration: Mapped[dict[str, Any]] = mapped_column(
        JSON_TYPE,
        nullable=False,
    )
    config_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    input_manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    ranking_manifest_sha256: Mapped[str | None] = mapped_column(String(64))
    scorer_version: Mapped[str] = mapped_column(String(100), nullable=False)
    pillow_version: Mapped[str] = mapped_column(String(50), nullable=False)
    state: Mapped[ScoringRunState] = mapped_column(
        Enum(
            ScoringRunState,
            name="scoring_run_state",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda members: [member.value for member in members],
            length=9,
        ),
        nullable=False,
        default=ScoringRunState.RUNNING,
    )
    asset_count: Mapped[int] = mapped_column(Integer, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AssetScore(UuidPrimaryKeyMixin, Base):
    __tablename__ = "asset_scores"
    __table_args__ = (
        UniqueConstraint(
            "scoring_run_id",
            "asset_id",
            name="uq_asset_scores_run_asset",
        ),
        CheckConstraint("attempts >= 0", name="nonnegative_attempts"),
        CheckConstraint("max_attempts > 0", name="positive_max_attempts"),
        CheckConstraint("attempts <= max_attempts", name="attempts_within_limit"),
        CheckConstraint("asset_byte_size > 0", name="positive_asset_byte_size"),
        CheckConstraint(
            "(state = 'processing' "
            "AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL) "
            "OR (state <> 'processing' "
            "AND lease_owner IS NULL AND lease_expires_at IS NULL)",
            name="lease_state",
        ),
        CheckConstraint(
            "state NOT IN "
            "('scored', 'flagged_blank', 'flagged_corrupt', 'dead_letter') "
            "OR completed_at IS NOT NULL",
            name="terminal_is_completed",
        ),
        CheckConstraint(
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
            name="scored_signal_complete",
        ),
        CheckConstraint(
            "luminance_mean_micros IS NULL OR luminance_mean_micros BETWEEN 0 AND 1000000",
            name="valid_luminance_mean",
        ),
        CheckConstraint(
            "luminance_std_micros IS NULL OR luminance_std_micros BETWEEN 0 AND 1000000",
            name="valid_luminance_std",
        ),
        CheckConstraint(
            "dynamic_range_micros IS NULL OR dynamic_range_micros BETWEEN 0 AND 1000000",
            name="valid_dynamic_range",
        ),
        CheckConstraint(
            "entropy_bits_micros IS NULL OR entropy_bits_micros BETWEEN 0 AND 8000000",
            name="valid_entropy_bits",
        ),
        CheckConstraint(
            "entropy_normalized_micros IS NULL OR entropy_normalized_micros BETWEEN 0 AND 1000000",
            name="valid_entropy_normalized",
        ),
        CheckConstraint(
            "sharpness_micros IS NULL OR sharpness_micros BETWEEN 0 AND 1000000",
            name="valid_sharpness",
        ),
        CheckConstraint(
            "aggregate_score_micros IS NULL OR aggregate_score_micros BETWEEN 0 AND 1000000",
            name="valid_aggregate_score",
        ),
        CheckConstraint(
            "dhash_hex IS NULL OR length(dhash_hex) = 16",
            name="valid_dhash_length",
        ),
        Index(
            "ix_asset_scores_claim",
            "state",
            "available_at",
            "lease_expires_at",
            "created_at",
        ),
    )

    scoring_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("scoring_runs.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    asset_id: Mapped[UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    asset_storage_backend: Mapped[str] = mapped_column(String(50), nullable=False)
    asset_storage_bucket: Mapped[str] = mapped_column(String(255), nullable=False)
    asset_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    asset_object_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    asset_object_version_id: Mapped[str] = mapped_column(String(1024), nullable=False)
    asset_byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    asset_image_format: Mapped[str] = mapped_column(String(20), nullable=False)
    asset_width: Mapped[int] = mapped_column(Integer, nullable=False)
    asset_height: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[AssetScoreState] = mapped_column(
        Enum(
            AssetScoreState,
            name="asset_score_state",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda members: [member.value for member in members],
            length=15,
        ),
        nullable=False,
        default=AssetScoreState.PENDING,
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(200))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    luminance_mean_micros: Mapped[int | None] = mapped_column(Integer)
    luminance_std_micros: Mapped[int | None] = mapped_column(Integer)
    dynamic_range_micros: Mapped[int | None] = mapped_column(Integer)
    entropy_bits_micros: Mapped[int | None] = mapped_column(Integer)
    entropy_normalized_micros: Mapped[int | None] = mapped_column(Integer)
    sharpness_micros: Mapped[int | None] = mapped_column(Integer)
    dhash_hex: Mapped[str | None] = mapped_column(String(16))
    aggregate_score_micros: Mapped[int | None] = mapped_column(Integer)
    score_breakdown: Mapped[dict[str, Any] | None] = mapped_column(JSON_TYPE)
    signal_detail: Mapped[dict[str, Any]] = mapped_column(
        JSON_TYPE,
        nullable=False,
        default=dict,
    )
    scorer_version: Mapped[str] = mapped_column(String(100), nullable=False)
    pillow_version: Mapped[str] = mapped_column(String(50), nullable=False)
    config_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    last_error_code: Mapped[str | None] = mapped_column(String(100))
    last_error_detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SemanticAssessment(UuidPrimaryKeyMixin, Base):
    __tablename__ = "semantic_assessments"
    __table_args__ = (
        UniqueConstraint(
            "scoring_run_id",
            "asset_id",
            "profile_sha256",
            name="uq_semantic_assessments_run_asset_profile",
        ),
        Index(
            "uq_semantic_assessments_feedback_identity",
            "id",
            "asset_id",
            "profile_sha256",
            unique=True,
        ),
        ForeignKeyConstraint(
            ["scoring_run_id", "asset_id"],
            ["asset_scores.scoring_run_id", "asset_scores.asset_id"],
            name="fk_semantic_assessments_score_snapshot",
            ondelete="RESTRICT",
        ),
        CheckConstraint("attempts >= 0", name="nonnegative_attempts"),
        CheckConstraint("max_attempts > 0", name="positive_max_attempts"),
        CheckConstraint("attempts <= max_attempts", name="attempts_within_limit"),
        CheckConstraint("asset_byte_size > 0", name="positive_asset_byte_size"),
        CheckConstraint("length(asset_sha256) = 64", name="valid_asset_sha256"),
        CheckConstraint("length(profile_sha256) = 64", name="valid_profile_sha256"),
        CheckConstraint("length(prompt_sha256) = 64", name="valid_prompt_sha256"),
        CheckConstraint("length(schema_sha256) = 64", name="valid_schema_sha256"),
        CheckConstraint(
            "(state = 'processing' "
            "AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL) "
            "OR (state <> 'processing' "
            "AND lease_owner IS NULL AND lease_expires_at IS NULL)",
            name="lease_state",
        ),
        CheckConstraint(
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
            name="result_state",
        ),
        CheckConstraint(
            "confidence_micros IS NULL OR confidence_micros BETWEEN 0 AND 1000000",
            name="valid_confidence",
        ),
        Index(
            "ix_semantic_assessments_claim",
            "state",
            "available_at",
            "lease_expires_at",
            "created_at",
        ),
    )

    scoring_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("scoring_runs.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    asset_score_id: Mapped[UUID] = mapped_column(
        ForeignKey("asset_scores.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    asset_id: Mapped[UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    asset_storage_backend: Mapped[str] = mapped_column(String(50), nullable=False)
    asset_storage_bucket: Mapped[str] = mapped_column(String(255), nullable=False)
    asset_object_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    asset_object_version_id: Mapped[str] = mapped_column(String(1024), nullable=False)
    asset_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    asset_content_type: Mapped[str] = mapped_column(String(100), nullable=False)
    asset_byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    profile_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    model_name: Mapped[str] = mapped_column(String(200), nullable=False)
    model_revision: Mapped[str] = mapped_column(String(200), nullable=False)
    prompt_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    schema_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[SemanticAssessmentState] = mapped_column(
        Enum(
            SemanticAssessmentState,
            name="semantic_assessment_state",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda members: [member.value for member in members],
            length=11,
        ),
        nullable=False,
        default=SemanticAssessmentState.PENDING,
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(200))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verdict: Mapped[SemanticVerdict | None] = mapped_column(
        Enum(
            SemanticVerdict,
            name="semantic_verdict",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda members: [member.value for member in members],
            length=6,
        )
    )
    confidence_micros: Mapped[int | None] = mapped_column(Integer)
    issues: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON_TYPE)
    response_sha256: Mapped[str | None] = mapped_column(String(64))
    last_error_code: Mapped[str | None] = mapped_column(String(100))
    last_error_detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SemanticAnatomyFeedback(UuidPrimaryKeyMixin, Base):
    __tablename__ = "semantic_anatomy_feedback"
    __table_args__ = (
        UniqueConstraint(
            "semantic_assessment_id",
            "feedback_by_user_id",
            name="uq_semantic_anatomy_feedback_assessment_user",
        ),
        ForeignKeyConstraint(
            ["semantic_assessment_id", "asset_id", "profile_sha256"],
            [
                "semantic_assessments.id",
                "semantic_assessments.asset_id",
                "semantic_assessments.profile_sha256",
            ],
            name="fk_semantic_anatomy_feedback_assessment_identity",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "ground_truth = 'anatomy_defect' OR issue_code IS NULL",
            name="issue_requires_defect",
        ),
        CheckConstraint(
            "(ground_truth = 'unjudgeable' AND agreement = 'unsure') "
            "OR (ground_truth <> 'unjudgeable' AND agreement <> 'unsure')",
            name="unjudgeable_agreement",
        ),
        CheckConstraint(
            "note IS NULL OR length(trim(note)) > 0",
            name="nonempty_note",
        ),
        Index(
            "ix_semantic_anatomy_feedback_profile_created",
            "profile_sha256",
            "created_at",
        ),
    )

    semantic_assessment_id: Mapped[UUID] = mapped_column(nullable=False)
    asset_id: Mapped[UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    profile_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    feedback_by_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("admin_users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    agreement: Mapped[SemanticFeedbackAgreement] = mapped_column(
        Enum(
            SemanticFeedbackAgreement,
            name="semantic_feedback_agreement",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda members: [member.value for member in members],
            length=9,
        ),
        nullable=False,
    )
    ground_truth: Mapped[SemanticGroundTruth] = mapped_column(
        Enum(
            SemanticGroundTruth,
            name="semantic_ground_truth",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda members: [member.value for member in members],
            length=14,
        ),
        nullable=False,
    )
    issue_code: Mapped[SemanticIssueCode | None] = mapped_column(
        Enum(
            SemanticIssueCode,
            name="semantic_issue_code",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda members: [member.value for member in members],
            length=23,
        )
    )
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SemanticCalibrationArtifact(UuidPrimaryKeyMixin, Base):
    __tablename__ = "semantic_calibration_artifacts"
    __table_args__ = (
        UniqueConstraint(
            "profile_sha256",
            "version",
            name="uq_semantic_calibration_artifacts_profile_version",
        ),
        UniqueConstraint(
            "profile_sha256",
            "report_sha256",
            name="uq_semantic_calibration_artifacts_profile_report",
        ),
        CheckConstraint("version > 0", name="positive_version"),
        CheckConstraint("sample_count >= 0", name="nonnegative_sample_count"),
        CheckConstraint("length(profile_sha256) = 64", name="valid_profile_sha256"),
        CheckConstraint("length(dataset_sha256) = 64", name="valid_dataset_sha256"),
        CheckConstraint("length(report_sha256) = 64", name="valid_report_sha256"),
        CheckConstraint(
            "recommended_threshold_micros IS NULL "
            "OR recommended_threshold_micros BETWEEN 0 AND 1000000",
            name="valid_recommended_threshold",
        ),
        Index(
            "ix_semantic_calibration_artifacts_profile_created",
            "profile_sha256",
            "created_at",
        ),
    )

    profile_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    calibration_schema_version: Mapped[str] = mapped_column(String(100), nullable=False)
    dataset_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False)
    recommended_threshold_micros: Mapped[int | None] = mapped_column(Integer)
    ready_for_enforcement: Mapped[bool] = mapped_column(Boolean, nullable=False)
    report: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False)
    report_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("admin_users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SemanticLearningPolicy(Base):
    __tablename__ = "semantic_learning_policies"
    __table_args__ = (
        CheckConstraint("minimum_new_labels_for_retrain > 0", name="positive_retrain_delta"),
        CheckConstraint("lock_version > 0", name="positive_lock_version"),
        CheckConstraint(
            "max_visual_run_microusd BETWEEN 1 AND 25000000",
            name="bounded_visual_run_cost",
        ),
    )

    owner_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("admin_users.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    learning_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    auto_train_meta: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    auto_train_visual: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    auto_promote_validated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    max_visual_run_microusd: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=10_000_000,
    )
    minimum_new_labels_for_retrain: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=50,
    )
    lock_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SemanticTrainingRun(UuidPrimaryKeyMixin, Base):
    __tablename__ = "semantic_training_runs"
    __table_args__ = (
        UniqueConstraint(
            "owner_user_id",
            "kind",
            "dataset_sha256",
            "training_config_sha256",
            name="uq_semantic_training_runs_dataset_config",
        ),
        CheckConstraint("length(profile_sha256) = 64", name="valid_profile_sha256"),
        CheckConstraint("length(dataset_sha256) = 64", name="valid_dataset_sha256"),
        CheckConstraint(
            "length(split_manifest_sha256) = 64",
            name="valid_split_manifest_sha256",
        ),
        CheckConstraint(
            "length(training_config_sha256) = 64",
            name="valid_training_config_sha256",
        ),
        CheckConstraint("attempts >= 0", name="nonnegative_attempts"),
        CheckConstraint("max_attempts > 0", name="positive_max_attempts"),
        CheckConstraint("attempts <= max_attempts", name="attempts_within_limit"),
        CheckConstraint(
            "estimated_cost_microusd IS NULL OR estimated_cost_microusd >= 0",
            name="nonnegative_estimated_cost",
        ),
        CheckConstraint(
            "actual_cost_microusd IS NULL OR actual_cost_microusd >= 0",
            name="nonnegative_actual_cost",
        ),
        CheckConstraint(
            "(state IN ('preparing', 'running') "
            "AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL) "
            "OR (state NOT IN ('preparing', 'running') "
            "AND lease_owner IS NULL AND lease_expires_at IS NULL)",
            name="lease_state",
        ),
        CheckConstraint(
            "(state = 'succeeded' AND artifact_sha256 IS NOT NULL "
            "AND evaluation_report IS NOT NULL AND evaluation_sha256 IS NOT NULL "
            "AND completed_at IS NOT NULL AND last_error_code IS NULL) "
            "OR (state IN ('failed', 'cancelled') AND completed_at IS NOT NULL) "
            "OR (state NOT IN ('succeeded', 'failed', 'cancelled') "
            "AND completed_at IS NULL)",
            name="terminal_result_state",
        ),
        Index(
            "ix_semantic_training_runs_claim",
            "state",
            "available_at",
            "lease_expires_at",
            "created_at",
        ),
        Index(
            "ix_semantic_training_runs_owner_profile",
            "owner_user_id",
            "profile_sha256",
            "created_at",
        ),
    )

    owner_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("admin_users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    kind: Mapped[SemanticTrainingKind] = mapped_column(
        Enum(
            SemanticTrainingKind,
            name="semantic_training_kind",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda members: [member.value for member in members],
            length=20,
        ),
        nullable=False,
    )
    state: Mapped[SemanticTrainingState] = mapped_column(
        Enum(
            SemanticTrainingState,
            name="semantic_training_state",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda members: [member.value for member in members],
            length=12,
        ),
        nullable=False,
        default=SemanticTrainingState.QUEUED,
    )
    profile_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    dataset_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    dataset_schema_version: Mapped[str] = mapped_column(String(100), nullable=False)
    split_manifest: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False)
    split_manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    training_config: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False)
    training_config_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(200))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider: Mapped[str | None] = mapped_column(String(50))
    provider_job_id: Mapped[str | None] = mapped_column(String(200))
    base_model: Mapped[str | None] = mapped_column(String(200))
    base_model_revision: Mapped[str | None] = mapped_column(String(200))
    trainer_image_digest: Mapped[str | None] = mapped_column(String(512))
    artifact_sha256: Mapped[str | None] = mapped_column(String(64))
    artifact_storage_bucket: Mapped[str | None] = mapped_column(String(255))
    artifact_object_key: Mapped[str | None] = mapped_column(String(1024))
    artifact_object_version_id: Mapped[str | None] = mapped_column(String(1024))
    artifact_size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    model_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON_TYPE)
    evaluation_report: Mapped[dict[str, Any] | None] = mapped_column(JSON_TYPE)
    evaluation_sha256: Mapped[str | None] = mapped_column(String(64))
    promotion_report: Mapped[dict[str, Any] | None] = mapped_column(JSON_TYPE)
    estimated_cost_microusd: Mapped[int | None] = mapped_column(BigInteger)
    actual_cost_microusd: Mapped[int | None] = mapped_column(BigInteger)
    last_error_code: Mapped[str | None] = mapped_column(String(100))
    last_error_detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SemanticModelPromotion(UuidPrimaryKeyMixin, Base):
    __tablename__ = "semantic_model_promotions"
    __table_args__ = (
        CheckConstraint("length(profile_sha256) = 64", name="valid_profile_sha256"),
        CheckConstraint("length(artifact_sha256) = 64", name="valid_artifact_sha256"),
        CheckConstraint("length(dataset_sha256) = 64", name="valid_dataset_sha256"),
        CheckConstraint("length(evaluation_sha256) = 64", name="valid_evaluation_sha256"),
        CheckConstraint(
            "keep_threshold_micros BETWEEN -1 AND 1000000",
            name="valid_keep_threshold",
        ),
        CheckConstraint(
            "reject_threshold_micros BETWEEN 0 AND 1000001",
            name="valid_reject_threshold",
        ),
        CheckConstraint(
            "keep_threshold_micros < reject_threshold_micros",
            name="ordered_thresholds",
        ),
        CheckConstraint("length(trim(reason)) > 0", name="nonempty_reason"),
        Index(
            "ix_semantic_model_promotions_owner_profile",
            "owner_user_id",
            "profile_sha256",
            "created_at",
        ),
    )

    owner_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("admin_users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    kind: Mapped[SemanticTrainingKind] = mapped_column(
        Enum(
            SemanticTrainingKind,
            name="semantic_promotion_training_kind",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda members: [member.value for member in members],
            length=20,
        ),
        nullable=False,
    )
    training_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("semantic_training_runs.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    previous_training_run_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("semantic_training_runs.id", ondelete="RESTRICT")
    )
    profile_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    artifact_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    dataset_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    evaluation_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    decision: Mapped[SemanticPromotionDecision] = mapped_column(
        Enum(
            SemanticPromotionDecision,
            name="semantic_promotion_decision",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda members: [member.value for member in members],
            length=11,
        ),
        nullable=False,
    )
    keep_threshold_micros: Mapped[int] = mapped_column(Integer, nullable=False)
    reject_threshold_micros: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_by_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("admin_users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AssetRanking(UuidPrimaryKeyMixin, Base):
    __tablename__ = "asset_rankings"
    __table_args__ = (
        UniqueConstraint(
            "scoring_run_id",
            "asset_id",
            name="uq_asset_rankings_run_asset",
        ),
        UniqueConstraint(
            "scoring_run_id",
            "rank",
            name="uq_asset_rankings_run_rank",
        ),
        UniqueConstraint("asset_score_id", name="uq_asset_rankings_asset_score"),
        CheckConstraint("rank > 0", name="positive_rank"),
        CheckConstraint(
            "aggregate_score_micros BETWEEN 0 AND 1000000",
            name="valid_aggregate_score",
        ),
        CheckConstraint(
            "(duplicate_cluster_id IS NULL "
            "AND duplicate_representative_asset_id IS NULL "
            "AND is_duplicate_representative = false) "
            "OR (duplicate_cluster_id IS NOT NULL "
            "AND duplicate_representative_asset_id IS NOT NULL)",
            name="duplicate_identity",
        ),
        CheckConstraint(
            "is_duplicate_representative = false OR duplicate_representative_asset_id = asset_id",
            name="representative_is_self",
        ),
        CheckConstraint(
            "disposition <> 'near_duplicate' "
            "OR (duplicate_cluster_id IS NOT NULL "
            "AND is_duplicate_representative = false)",
            name="near_duplicate_identity",
        ),
    )

    scoring_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("scoring_runs.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    asset_score_id: Mapped[UUID] = mapped_column(
        ForeignKey("asset_scores.id", ondelete="RESTRICT"),
        nullable=False,
    )
    asset_id: Mapped[UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    aggregate_score_micros: Mapped[int] = mapped_column(Integer, nullable=False)
    disposition: Mapped[RankingDisposition] = mapped_column(
        Enum(
            RankingDisposition,
            name="ranking_disposition",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda members: [member.value for member in members],
            length=16,
        ),
        nullable=False,
    )
    explanation: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False)
    duplicate_cluster_id: Mapped[str | None] = mapped_column(String(64))
    duplicate_representative_asset_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT"),
    )
    is_duplicate_representative: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )
    scorer_version: Mapped[str] = mapped_column(String(100), nullable=False)
    pillow_version: Mapped[str] = mapped_column(String(50), nullable=False)
    config_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    frozen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AdminUser(UuidPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "admin_users"
    __table_args__ = (
        CheckConstraint(
            "failed_login_count >= 0",
            name="nonnegative_failed_login_count",
        ),
        CheckConstraint(
            "(failed_login_count = 0 AND failed_login_window_started_at IS NULL) "
            "OR (failed_login_count > 0 AND failed_login_window_started_at IS NOT NULL)",
            name="login_failure_window_pair",
        ),
        CheckConstraint("lock_version > 0", name="positive_lock_version"),
        CheckConstraint("credential_version > 0", name="positive_credential_version"),
        CheckConstraint(
            "totp_confirmed_at IS NULL OR totp_secret_ciphertext IS NOT NULL",
            name="totp_confirmation_requires_secret",
        ),
        CheckConstraint(
            "last_totp_counter IS NULL OR "
            "(last_totp_counter >= 0 AND totp_confirmed_at IS NOT NULL)",
            name="totp_counter_requires_confirmation",
        ),
    )

    username_normalized: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
        unique=True,
    )
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[AdminRole] = mapped_column(
        Enum(
            AdminRole,
            name="admin_role",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda members: [member.value for member in members],
            length=9,
        ),
        nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    totp_secret_ciphertext: Mapped[str | None] = mapped_column(Text)
    totp_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_totp_counter: Mapped[int | None] = mapped_column(BigInteger)
    failed_login_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_login_window_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    password_changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    credential_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    lock_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class AdminEnrollment(UuidPrimaryKeyMixin, Base):
    __tablename__ = "admin_enrollments"
    __table_args__ = (
        UniqueConstraint(
            "token_sha256",
            name="uq_admin_enrollments_token_sha256",
        ),
        UniqueConstraint(
            "consumed_by_user_id",
            name="uq_admin_enrollments_consumed_by_user_id",
        ),
        Index(
            "uq_admin_enrollments_pending_username",
            "username_normalized",
            unique=True,
            postgresql_where=text("state = 'pending'"),
            sqlite_where=text("state = 'pending'"),
        ),
        Index(
            "ix_admin_enrollments_state_expires_at",
            "state",
            "expires_at",
        ),
        CheckConstraint(
            "length(token_sha256) = 64",
            name="valid_token_sha256",
        ),
        CheckConstraint(
            "expires_at > invited_at",
            name="expiry_after_invitation",
        ),
        CheckConstraint("lock_version > 0", name="positive_lock_version"),
        CheckConstraint(
            "(state = 'pending' "
            "AND totp_secret_ciphertext IS NOT NULL "
            "AND consumed_by_user_id IS NULL AND consumed_at IS NULL "
            "AND revoked_by_user_id IS NULL AND revoked_at IS NULL) "
            "OR (state = 'consumed' "
            "AND totp_secret_ciphertext IS NULL "
            "AND consumed_by_user_id IS NOT NULL AND consumed_at IS NOT NULL "
            "AND revoked_by_user_id IS NULL AND revoked_at IS NULL) "
            "OR (state = 'revoked' "
            "AND totp_secret_ciphertext IS NULL "
            "AND consumed_by_user_id IS NULL AND consumed_at IS NULL "
            "AND revoked_by_user_id IS NOT NULL AND revoked_at IS NOT NULL)",
            name="lifecycle_metadata",
        ),
        CheckConstraint(
            "consumed_at IS NULL OR consumed_at >= invited_at",
            name="valid_consumption_time",
        ),
        CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= invited_at",
            name="valid_revocation_time",
        ),
    )

    username_normalized: Mapped[str] = mapped_column(String(200), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    role: Mapped[AdminRole] = mapped_column(
        Enum(
            AdminRole,
            name="admin_enrollment_role",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda members: [member.value for member in members],
            length=9,
        ),
        nullable=False,
    )
    token_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    totp_secret_ciphertext: Mapped[str | None] = mapped_column(Text)
    state: Mapped[AdminEnrollmentState] = mapped_column(
        Enum(
            AdminEnrollmentState,
            name="admin_enrollment_state",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda members: [member.value for member in members],
            length=8,
        ),
        nullable=False,
        default=AdminEnrollmentState.PENDING,
    )
    invited_by_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("admin_users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    invited_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("admin_users.id", ondelete="RESTRICT"),
    )
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("admin_users.id", ondelete="RESTRICT"),
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lock_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class ReviewTask(UuidPrimaryKeyMixin, Base):
    __tablename__ = "review_tasks"
    __table_args__ = (
        UniqueConstraint(
            "scoring_run_id",
            name="uq_review_tasks_scoring_run",
        ),
        UniqueConstraint(
            "id",
            "scoring_run_id",
            name="uq_review_tasks_id_scoring_run",
        ),
        CheckConstraint(
            "release_version_no > 0",
            name="positive_release_version_no",
        ),
        CheckConstraint(
            "desired_accepted_count > 0",
            name="positive_desired_accepted_count",
        ),
        CheckConstraint(
            "ranked_asset_count > 0",
            name="positive_ranked_asset_count",
        ),
        CheckConstraint(
            "desired_accepted_count <= ranked_asset_count",
            name="desired_count_within_ranked_assets",
        ),
        CheckConstraint(
            "length(ranking_manifest_sha256) = 64",
            name="valid_ranking_manifest_sha256",
        ),
        CheckConstraint("lock_version > 0", name="positive_lock_version"),
        CheckConstraint(
            "(state = 'open' "
            "AND completed_by_user_id IS NULL AND completed_at IS NULL "
            "AND cancelled_by_user_id IS NULL AND cancelled_at IS NULL) "
            "OR (state = 'completed' "
            "AND completed_by_user_id IS NOT NULL AND completed_at IS NOT NULL "
            "AND cancelled_by_user_id IS NULL AND cancelled_at IS NULL) "
            "OR (state = 'cancelled' "
            "AND completed_by_user_id IS NULL AND completed_at IS NULL "
            "AND cancelled_by_user_id IS NOT NULL AND cancelled_at IS NOT NULL)",
            name="terminal_state_metadata",
        ),
        CheckConstraint(
            "completed_at IS NULL OR completed_at >= created_at",
            name="valid_completion_time",
        ),
        CheckConstraint(
            "cancelled_at IS NULL OR cancelled_at >= created_at",
            name="valid_cancellation_time",
        ),
        Index(
            "ix_review_tasks_state_created_at",
            "state",
            "created_at",
        ),
    )

    release_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("release_versions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    release_version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    release_specification_sha256: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    scoring_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("scoring_runs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    scoring_config_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    scoring_input_manifest_sha256: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    ranking_manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    desired_accepted_count: Mapped[int] = mapped_column(Integer, nullable=False)
    ranked_asset_count: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[ReviewTaskState] = mapped_column(
        Enum(
            ReviewTaskState,
            name="review_task_state",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda members: [member.value for member in members],
            length=9,
        ),
        nullable=False,
        default=ReviewTaskState.OPEN,
    )
    lock_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_by_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("admin_users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("admin_users.id", ondelete="RESTRICT"),
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("admin_users.id", ondelete="RESTRICT"),
    )
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ReviewDecision(UuidPrimaryKeyMixin, Base):
    __tablename__ = "review_decisions"
    __table_args__ = (
        UniqueConstraint(
            "review_task_id",
            "asset_id",
            "revision",
            name="uq_review_decisions_task_asset_revision",
        ),
        UniqueConstraint(
            "review_task_id",
            "asset_id",
            "revision",
            "id",
            name="uq_review_decisions_chain_target",
        ),
        UniqueConstraint(
            "supersedes_decision_id",
            name="uq_review_decisions_superseded_once",
        ),
        ForeignKeyConstraint(
            [
                "review_task_id",
                "scoring_run_id",
            ],
            [
                "review_tasks.id",
                "review_tasks.scoring_run_id",
            ],
            name="fk_review_decisions_task_scoring_run",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            [
                "scoring_run_id",
                "asset_id",
            ],
            [
                "asset_rankings.scoring_run_id",
                "asset_rankings.asset_id",
            ],
            name="fk_review_decisions_ranking_membership",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            [
                "review_task_id",
                "asset_id",
                "supersedes_revision",
                "supersedes_decision_id",
            ],
            [
                "review_decisions.review_task_id",
                "review_decisions.asset_id",
                "review_decisions.revision",
                "review_decisions.id",
            ],
            name="fk_review_decisions_prior_revision",
            ondelete="RESTRICT",
        ),
        CheckConstraint("revision > 0", name="positive_revision"),
        CheckConstraint(
            "(revision = 1 AND supersedes_revision IS NULL "
            "AND supersedes_decision_id IS NULL) "
            "OR (revision > 1 AND supersedes_revision = revision - 1 "
            "AND supersedes_decision_id IS NOT NULL)",
            name="linear_revision_chain",
        ),
        CheckConstraint(
            "reason_code IS NULL OR length(trim(reason_code)) > 0",
            name="nonempty_reason_code",
        ),
        CheckConstraint(
            "note IS NULL OR length(trim(note)) > 0",
            name="nonempty_note",
        ),
        Index(
            "ix_review_decisions_task_asset_revision",
            "review_task_id",
            "asset_id",
            "revision",
        ),
        Index(
            "ix_review_decisions_decided_by_user_id",
            "decided_by_user_id",
        ),
    )

    review_task_id: Mapped[UUID] = mapped_column(
        ForeignKey("review_tasks.id", ondelete="RESTRICT"),
        nullable=False,
    )
    scoring_run_id: Mapped[UUID] = mapped_column(nullable=False)
    asset_id: Mapped[UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT"),
        nullable=False,
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    decision: Mapped[ReviewDecisionValue] = mapped_column(
        Enum(
            ReviewDecisionValue,
            name="review_decision_value",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda members: [member.value for member in members],
            length=6,
        ),
        nullable=False,
    )
    reason_code: Mapped[str | None] = mapped_column(String(100))
    note: Mapped[str | None] = mapped_column(Text)
    decided_by_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("admin_users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    supersedes_revision: Mapped[int | None] = mapped_column(Integer)
    supersedes_decision_id: Mapped[UUID | None] = mapped_column(
        nullable=True,
    )


class ReviewAssetInspection(UuidPrimaryKeyMixin, Base):
    __tablename__ = "review_asset_inspections"
    __table_args__ = (
        UniqueConstraint(
            "review_task_id",
            "asset_id",
            "inspected_by_user_id",
            name="uq_review_asset_inspections_task_asset_user",
        ),
        ForeignKeyConstraint(
            ["review_task_id", "scoring_run_id"],
            ["review_tasks.id", "review_tasks.scoring_run_id"],
            name="fk_review_asset_inspections_task_scoring_run",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["scoring_run_id", "asset_id"],
            ["asset_rankings.scoring_run_id", "asset_rankings.asset_id"],
            name="fk_review_asset_inspections_ranking_membership",
            ondelete="RESTRICT",
        ),
        Index(
            "ix_review_asset_inspections_task_user",
            "review_task_id",
            "inspected_by_user_id",
        ),
    )

    review_task_id: Mapped[UUID] = mapped_column(nullable=False)
    scoring_run_id: Mapped[UUID] = mapped_column(nullable=False)
    asset_id: Mapped[UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT"),
        nullable=False,
    )
    inspected_by_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("admin_users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    inspected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ReviewXSelection(UuidPrimaryKeyMixin, Base):
    __tablename__ = "review_x_selections"
    __table_args__ = (
        UniqueConstraint(
            "review_task_id",
            "asset_id",
            name="uq_review_x_selections_task_asset",
        ),
        Index(
            "ix_review_x_selections_task_selected_at",
            "review_task_id",
            "selected_at",
        ),
    )

    review_task_id: Mapped[UUID] = mapped_column(
        ForeignKey("review_tasks.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    asset_id: Mapped[UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    selected_by_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("admin_users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    selected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ReleaseSelection(UuidPrimaryKeyMixin, Base):
    __tablename__ = "release_selections"
    __table_args__ = (
        UniqueConstraint(
            "review_task_id",
            "asset_id",
            name="uq_release_selections_task_asset",
        ),
        UniqueConstraint(
            "review_task_id",
            "display_order",
            name="uq_release_selections_task_display_order",
        ),
        Index(
            "uq_release_selections_task_generation_queue_position",
            "review_task_id",
            "source_generation_queue_position",
            unique=True,
        ),
        UniqueConstraint(
            "review_decision_id",
            name="uq_release_selections_review_decision",
        ),
        UniqueConstraint(
            "id",
            "asset_id",
            name="uq_release_selections_id_asset",
        ),
        UniqueConstraint(
            "id",
            "release_version_id",
            name="uq_release_selections_id_release_version",
        ),
        ForeignKeyConstraint(
            ["review_task_id", "scoring_run_id"],
            ["review_tasks.id", "review_tasks.scoring_run_id"],
            name="fk_release_selections_task_scoring_run",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            [
                "review_task_id",
                "asset_id",
                "decision_revision",
                "review_decision_id",
            ],
            [
                "review_decisions.review_task_id",
                "review_decisions.asset_id",
                "review_decisions.revision",
                "review_decisions.id",
            ],
            name="fk_release_selections_review_decision",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["scoring_run_id", "asset_id"],
            ["asset_rankings.scoring_run_id", "asset_rankings.asset_id"],
            name="fk_release_selections_ranking_membership",
            ondelete="RESTRICT",
        ),
        CheckConstraint("decision_revision > 0", name="positive_decision_revision"),
        CheckConstraint("ranking_rank > 0", name="positive_ranking_rank"),
        CheckConstraint("display_order > 0", name="positive_display_order"),
        CheckConstraint(
            "length(ranking_manifest_sha256) = 64",
            name="valid_ranking_manifest_sha256",
        ),
        CheckConstraint(
            "length(source_sha256) = 64",
            name="valid_source_sha256",
        ),
        CheckConstraint(
            "length(trim(source_storage_backend)) > 0 "
            "AND length(trim(source_storage_bucket)) > 0 "
            "AND length(trim(source_object_key)) > 0 "
            "AND length(trim(source_object_version_id)) > 0 "
            "AND length(trim(source_content_type)) > 0 "
            "AND length(trim(source_image_format)) > 0",
            name="complete_source_storage_identity",
        ),
        CheckConstraint(
            "source_width > 0 AND source_height > 0 AND source_byte_size > 0",
            name="positive_source_dimensions",
        ),
        CheckConstraint(
            "(source_generation_job_id IS NULL "
            "AND source_output_index IS NULL "
            "AND source_generation_ordinal IS NULL "
            "AND source_generation_queue_position IS NULL) OR "
            "(source_generation_job_id IS NOT NULL "
            "AND source_output_index >= 0 "
            "AND source_generation_ordinal >= 0 "
            "AND source_generation_queue_position > 0)",
            name="complete_source_generation_position",
        ),
        CheckConstraint(
            "frozen_at >= source_available_at",
            name="frozen_after_source_available",
        ),
        Index(
            "ix_release_selections_release_version_display",
            "release_version_id",
            "display_order",
        ),
    )

    review_task_id: Mapped[UUID] = mapped_column(
        ForeignKey("review_tasks.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    scoring_run_id: Mapped[UUID] = mapped_column(nullable=False)
    review_decision_id: Mapped[UUID] = mapped_column(
        ForeignKey("review_decisions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    decision_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    release_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("release_versions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    asset_id: Mapped[UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    ranking_rank: Mapped[int] = mapped_column(Integer, nullable=False)
    display_order: Mapped[int] = mapped_column(Integer, nullable=False)
    ranking_manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_storage_backend: Mapped[str] = mapped_column(String(50), nullable=False)
    source_storage_bucket: Mapped[str] = mapped_column(String(255), nullable=False)
    source_object_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    source_object_version_id: Mapped[str] = mapped_column(String(1024), nullable=False)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_content_type: Mapped[str] = mapped_column(String(100), nullable=False)
    source_image_format: Mapped[str] = mapped_column(String(20), nullable=False)
    source_width: Mapped[int] = mapped_column(Integer, nullable=False)
    source_height: Mapped[int] = mapped_column(Integer, nullable=False)
    source_byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_generation_job_id: Mapped[UUID | None] = mapped_column()
    source_output_index: Mapped[int | None] = mapped_column(Integer)
    source_generation_ordinal: Mapped[int | None] = mapped_column(Integer)
    source_generation_queue_position: Mapped[int | None] = mapped_column(Integer)
    source_available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    frozen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DerivativeRecipe(UuidPrimaryKeyMixin, Base):
    __tablename__ = "derivative_recipes"
    __table_args__ = (
        UniqueConstraint(
            "logical_key",
            name="uq_derivative_recipes_logical_key",
        ),
        UniqueConstraint(
            "id",
            "release_version_id",
            "expected_output_count",
            name="uq_derivative_recipes_id_release_outputs",
        ),
        CheckConstraint("recipe_version > 0", name="positive_recipe_version"),
        CheckConstraint(
            "expected_output_count > 0",
            name="positive_expected_output_count",
        ),
        CheckConstraint("length(logical_key) = 64", name="valid_logical_key"),
        CheckConstraint("length(config_sha256) = 64", name="valid_config_sha256"),
        CheckConstraint(
            "(watermark_asset_id IS NULL "
            "AND watermark_storage_backend IS NULL "
            "AND watermark_storage_bucket IS NULL "
            "AND watermark_object_key IS NULL "
            "AND watermark_object_version_id IS NULL "
            "AND watermark_sha256 IS NULL "
            "AND watermark_content_type IS NULL "
            "AND watermark_image_format IS NULL "
            "AND watermark_width IS NULL "
            "AND watermark_height IS NULL "
            "AND watermark_byte_size IS NULL) "
            "OR (watermark_asset_id IS NOT NULL "
            "AND watermark_storage_backend IS NOT NULL "
            "AND watermark_storage_bucket IS NOT NULL "
            "AND watermark_object_key IS NOT NULL "
            "AND watermark_object_version_id IS NOT NULL "
            "AND watermark_sha256 IS NOT NULL "
            "AND length(watermark_sha256) = 64 "
            "AND watermark_content_type IS NOT NULL "
            "AND watermark_image_format IS NOT NULL "
            "AND watermark_width > 0 "
            "AND watermark_height > 0 "
            "AND watermark_byte_size > 0)",
            name="watermark_snapshot_pair",
        ),
        CheckConstraint(
            "approved_at >= created_at",
            name="approval_after_creation",
        ),
        Index(
            "ix_derivative_recipes_release_created",
            "release_version_id",
            "created_at",
        ),
    )

    release_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("release_versions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    logical_key: Mapped[str] = mapped_column(String(64), nullable=False)
    recipe_version: Mapped[int] = mapped_column(Integer, nullable=False)
    configuration: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False)
    config_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    output_targets: Mapped[list[str]] = mapped_column(JSON_TYPE, nullable=False)
    expected_output_count: Mapped[int] = mapped_column(Integer, nullable=False)
    renderer_version: Mapped[str] = mapped_column(String(100), nullable=False)
    pillow_version: Mapped[str] = mapped_column(String(50), nullable=False)
    watermark_asset_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT"),
    )
    watermark_storage_backend: Mapped[str | None] = mapped_column(String(50))
    watermark_storage_bucket: Mapped[str | None] = mapped_column(String(255))
    watermark_object_key: Mapped[str | None] = mapped_column(String(1024))
    watermark_object_version_id: Mapped[str | None] = mapped_column(String(1024))
    watermark_sha256: Mapped[str | None] = mapped_column(String(64))
    watermark_content_type: Mapped[str | None] = mapped_column(String(100))
    watermark_image_format: Mapped[str | None] = mapped_column(String(20))
    watermark_width: Mapped[int | None] = mapped_column(Integer)
    watermark_height: Mapped[int | None] = mapped_column(Integer)
    watermark_byte_size: Mapped[int | None] = mapped_column(BigInteger)
    created_by_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("admin_users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    approved_by_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("admin_users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class XTeaserRevision(UuidPrimaryKeyMixin, Base):
    """Immutable desired X-teaser set; a mutable head chooses the visible revision."""

    __tablename__ = "x_teaser_revisions"
    __table_args__ = (
        UniqueConstraint(
            "review_task_id",
            "revision_no",
            name="uq_x_teaser_revisions_task_revision",
        ),
        UniqueConstraint(
            "id",
            "review_task_id",
            "release_version_id",
            name="uq_x_teaser_revisions_identity",
        ),
        CheckConstraint("revision_no > 0", name="positive_revision"),
        CheckConstraint("length(request_sha256) = 64", name="valid_request_sha256"),
        Index(
            "ix_x_teaser_revisions_task_created",
            "review_task_id",
            "created_at",
        ),
    )

    review_task_id: Mapped[UUID] = mapped_column(
        ForeignKey("review_tasks.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    release_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("release_versions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False)
    watermark_asset_id: Mapped[UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT"),
        nullable=False,
    )
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("admin_users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DerivativeJob(UuidPrimaryKeyMixin, Base):
    __tablename__ = "derivative_jobs"
    __table_args__ = (
        UniqueConstraint(
            "logical_key",
            name="uq_derivative_jobs_logical_key",
        ),
        Index(
            "uq_derivative_jobs_selection_recipe_legacy",
            "release_selection_id",
            "derivative_recipe_id",
            unique=True,
            sqlite_where=text("x_teaser_revision_id IS NULL"),
            postgresql_where=text("x_teaser_revision_id IS NULL"),
        ),
        Index(
            "uq_derivative_jobs_selection_recipe_revision",
            "release_selection_id",
            "derivative_recipe_id",
            "x_teaser_revision_id",
            unique=True,
            sqlite_where=text("x_teaser_revision_id IS NOT NULL"),
            postgresql_where=text("x_teaser_revision_id IS NOT NULL"),
        ),
        UniqueConstraint(
            "id",
            "release_selection_id",
            "derivative_recipe_id",
            name="uq_derivative_jobs_id_selection_recipe",
        ),
        ForeignKeyConstraint(
            ["release_selection_id", "release_version_id"],
            ["release_selections.id", "release_selections.release_version_id"],
            name="fk_derivative_jobs_selection_release_version",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            [
                "derivative_recipe_id",
                "release_version_id",
                "expected_output_count",
            ],
            [
                "derivative_recipes.id",
                "derivative_recipes.release_version_id",
                "derivative_recipes.expected_output_count",
            ],
            name="fk_derivative_jobs_recipe_release_outputs",
            ondelete="RESTRICT",
        ),
        CheckConstraint("length(logical_key) = 64", name="valid_logical_key"),
        CheckConstraint("length(request_sha256) = 64", name="valid_request_sha256"),
        CheckConstraint("attempt_count >= 0", name="nonnegative_attempt_count"),
        CheckConstraint("max_attempts > 0", name="positive_max_attempts"),
        CheckConstraint(
            "attempt_count <= max_attempts",
            name="attempts_within_limit",
        ),
        CheckConstraint("expected_output_count > 0", name="positive_output_count"),
        CheckConstraint("lock_version > 0", name="positive_lock_version"),
        CheckConstraint(
            "gates_release = true OR x_teaser_revision_id IS NOT NULL",
            name="nongating_job_requires_x_revision",
        ),
        CheckConstraint(
            "(state IN ('claimed', 'processing') "
            "AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL) "
            "OR (state NOT IN ('claimed', 'processing') "
            "AND lease_owner IS NULL AND lease_expires_at IS NULL)",
            name="lease_state",
        ),
        CheckConstraint(
            "(state = 'retry_wait' AND retry_at IS NOT NULL) "
            "OR (state <> 'retry_wait' AND retry_at IS NULL)",
            name="retry_state",
        ),
        CheckConstraint(
            "(state IN ('succeeded', 'failed', 'cancelled') "
            "AND completed_at IS NOT NULL) "
            "OR (state NOT IN ('succeeded', 'failed', 'cancelled') "
            "AND completed_at IS NULL)",
            name="terminal_state",
        ),
        CheckConstraint(
            "state <> 'requested' OR attempt_count = 0",
            name="requested_has_no_attempt",
        ),
        CheckConstraint(
            "state NOT IN ('claimed', 'processing') OR attempt_count > 0",
            name="active_has_attempt",
        ),
        CheckConstraint(
            "claimed_at IS NULL OR claimed_at >= requested_at",
            name="valid_claim_time",
        ),
        CheckConstraint(
            "processing_started_at IS NULL OR processing_started_at >= requested_at",
            name="valid_processing_time",
        ),
        CheckConstraint(
            "completed_at IS NULL OR completed_at >= requested_at",
            name="valid_completion_time",
        ),
        Index(
            "ix_derivative_jobs_claim",
            "state",
            "retry_at",
            "lease_expires_at",
            "priority",
            "requested_at",
        ),
    )

    release_selection_id: Mapped[UUID] = mapped_column(
        ForeignKey("release_selections.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    derivative_recipe_id: Mapped[UUID] = mapped_column(
        ForeignKey("derivative_recipes.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    x_teaser_revision_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("x_teaser_revisions.id", ondelete="RESTRICT"),
        index=True,
    )
    gates_release: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    release_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("release_versions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    logical_key: Mapped[str] = mapped_column(String(64), nullable=False)
    request_payload: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False)
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    expected_output_count: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[DerivativeJobState] = mapped_column(
        Enum(
            DerivativeJobState,
            name="derivative_job_state",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda members: [member.value for member in members],
            length=10,
        ),
        nullable=False,
        default=DerivativeJobState.REQUESTED,
    )
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    lock_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[str | None] = mapped_column(String(200))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processing_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(100))
    last_error_detail: Mapped[str | None] = mapped_column(Text)


class DerivativeOutput(UuidPrimaryKeyMixin, Base):
    __tablename__ = "derivative_outputs"
    __table_args__ = (
        UniqueConstraint(
            "derivative_job_id",
            "target",
            name="uq_derivative_outputs_job_target",
        ),
        UniqueConstraint(
            "asset_id",
            name="uq_derivative_outputs_asset",
        ),
        ForeignKeyConstraint(
            [
                "derivative_job_id",
                "release_selection_id",
                "derivative_recipe_id",
            ],
            [
                "derivative_jobs.id",
                "derivative_jobs.release_selection_id",
                "derivative_jobs.derivative_recipe_id",
            ],
            name="fk_derivative_outputs_job_identity",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["release_selection_id", "source_asset_id"],
            ["release_selections.id", "release_selections.asset_id"],
            name="fk_derivative_outputs_selection_source",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "length(trim(target)) > 0",
            name="nonempty_target",
        ),
        CheckConstraint(
            "length(asset_sha256) = 64",
            name="valid_asset_sha256",
        ),
        CheckConstraint(
            "length(trim(asset_storage_backend)) > 0 "
            "AND length(trim(asset_storage_bucket)) > 0 "
            "AND length(trim(asset_object_key)) > 0 "
            "AND length(trim(asset_object_version_id)) > 0 "
            "AND length(trim(asset_content_type)) > 0 "
            "AND length(trim(asset_image_format)) > 0",
            name="complete_asset_storage_identity",
        ),
        CheckConstraint(
            "asset_width > 0 AND asset_height > 0 AND asset_byte_size > 0",
            name="positive_asset_dimensions",
        ),
        CheckConstraint(
            "length(trim(lineage_relation)) > 0 AND length(trim(lineage_recipe_version)) > 0",
            name="complete_lineage_identity",
        ),
        Index(
            "ix_derivative_outputs_selection_target",
            "release_selection_id",
            "target",
        ),
    )

    derivative_job_id: Mapped[UUID] = mapped_column(
        ForeignKey("derivative_jobs.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    release_selection_id: Mapped[UUID] = mapped_column(nullable=False)
    derivative_recipe_id: Mapped[UUID] = mapped_column(nullable=False)
    target: Mapped[str] = mapped_column(String(50), nullable=False)
    asset_id: Mapped[UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    source_asset_id: Mapped[UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT"),
        nullable=False,
    )
    asset_lineage_id: Mapped[UUID] = mapped_column(
        ForeignKey("asset_lineage.id", ondelete="RESTRICT"),
        nullable=False,
    )
    asset_storage_backend: Mapped[str] = mapped_column(String(50), nullable=False)
    asset_storage_bucket: Mapped[str] = mapped_column(String(255), nullable=False)
    asset_object_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    asset_object_version_id: Mapped[str] = mapped_column(String(1024), nullable=False)
    asset_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    asset_content_type: Mapped[str] = mapped_column(String(100), nullable=False)
    asset_image_format: Mapped[str] = mapped_column(String(20), nullable=False)
    asset_width: Mapped[int] = mapped_column(Integer, nullable=False)
    asset_height: Mapped[int] = mapped_column(Integer, nullable=False)
    asset_byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    lineage_relation: Mapped[str] = mapped_column(String(50), nullable=False)
    lineage_recipe_version: Mapped[str] = mapped_column(String(100), nullable=False)
    recorded_by: Mapped[str] = mapped_column(String(200), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class XTeaserRevisionMember(UuidPrimaryKeyMixin, Base):
    """Immutable resolution of one selected image in an X-teaser revision."""

    __tablename__ = "x_teaser_revision_members"
    __table_args__ = (
        UniqueConstraint(
            "revision_id",
            "release_selection_id",
            name="uq_x_teaser_revision_members_selection",
        ),
        UniqueConstraint(
            "revision_id",
            "display_order",
            name="uq_x_teaser_revision_members_order",
        ),
        ForeignKeyConstraint(
            ["revision_id", "review_task_id", "release_version_id"],
            [
                "x_teaser_revisions.id",
                "x_teaser_revisions.review_task_id",
                "x_teaser_revisions.release_version_id",
            ],
            name="fk_x_teaser_revision_members_revision_identity",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["release_selection_id", "source_asset_id"],
            ["release_selections.id", "release_selections.asset_id"],
            name="fk_x_teaser_revision_members_selection_source",
            ondelete="RESTRICT",
        ),
        CheckConstraint("display_order > 0", name="positive_display_order"),
        CheckConstraint(
            "watermark_position IN ('top_left', 'top_right', 'bottom_left', 'bottom_right')",
            name="valid_watermark_position",
        ),
        CheckConstraint(
            "(derivative_job_id IS NOT NULL AND derivative_output_id IS NULL) OR "
            "(derivative_job_id IS NULL AND derivative_output_id IS NOT NULL)",
            name="job_or_reused_output",
        ),
        Index(
            "ix_x_teaser_revision_members_revision_order",
            "revision_id",
            "display_order",
        ),
    )

    revision_id: Mapped[UUID] = mapped_column(
        ForeignKey("x_teaser_revisions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    review_task_id: Mapped[UUID] = mapped_column(
        ForeignKey("review_tasks.id", ondelete="RESTRICT"),
        nullable=False,
    )
    release_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("release_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    release_selection_id: Mapped[UUID] = mapped_column(
        ForeignKey("release_selections.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    source_asset_id: Mapped[UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT"),
        nullable=False,
    )
    display_order: Mapped[int] = mapped_column(Integer, nullable=False)
    watermark_position: Mapped[str] = mapped_column(String(20), nullable=False)
    derivative_recipe_id: Mapped[UUID] = mapped_column(
        ForeignKey("derivative_recipes.id", ondelete="RESTRICT"),
        nullable=False,
    )
    derivative_job_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("derivative_jobs.id", ondelete="RESTRICT"),
        index=True,
    )
    derivative_output_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("derivative_outputs.id", ondelete="RESTRICT"),
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class XTeaserRevisionHead(UuidPrimaryKeyMixin, Base):
    """Narrow mutable pointer which atomically publishes one complete revision."""

    __tablename__ = "x_teaser_revision_heads"
    __table_args__ = (
        UniqueConstraint(
            "review_task_id",
            name="uq_x_teaser_revision_heads_review_task",
        ),
        CheckConstraint("lock_version > 0", name="positive_lock_version"),
        CheckConstraint(
            "active_revision_id IS NULL OR pending_revision_id IS NULL "
            "OR active_revision_id <> pending_revision_id",
            name="distinct_revision_pointers",
        ),
        ForeignKeyConstraint(
            ["active_revision_id", "review_task_id", "release_version_id"],
            [
                "x_teaser_revisions.id",
                "x_teaser_revisions.review_task_id",
                "x_teaser_revisions.release_version_id",
            ],
            name="fk_x_teaser_revision_heads_active_identity",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["pending_revision_id", "review_task_id", "release_version_id"],
            [
                "x_teaser_revisions.id",
                "x_teaser_revisions.review_task_id",
                "x_teaser_revisions.release_version_id",
            ],
            name="fk_x_teaser_revision_heads_pending_identity",
            ondelete="RESTRICT",
        ),
    )

    review_task_id: Mapped[UUID] = mapped_column(
        ForeignKey("review_tasks.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    release_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("release_versions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    active_revision_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("x_teaser_revisions.id", ondelete="RESTRICT"),
    )
    pending_revision_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("x_teaser_revisions.id", ondelete="RESTRICT"),
    )
    lock_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PublicationIntent(UuidPrimaryKeyMixin, Base):
    __tablename__ = "publication_intents"
    __table_args__ = (
        UniqueConstraint(
            "intent_digest",
            name="uq_publication_intents_digest",
        ),
        CheckConstraint("length(configuration_sha256) = 64", name="valid_config_sha256"),
        CheckConstraint("length(input_manifest_sha256) = 64", name="valid_input_manifest_sha256"),
        CheckConstraint("length(intent_digest) = 64", name="valid_intent_digest"),
        CheckConstraint("input_count > 0", name="positive_input_count"),
        CheckConstraint("lock_version > 0", name="positive_lock_version"),
        CheckConstraint(
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
            name="target_contract",
        ),
        CheckConstraint(
            "scheduled_at IS NULL OR scheduled_at >= planned_at",
            name="schedule_after_plan",
        ),
        CheckConstraint(
            "completed_at IS NULL OR completed_at >= planned_at",
            name="completion_after_plan",
        ),
        Index(
            "ix_publication_intents_state_schedule",
            "state",
            "scheduled_at",
            "planned_at",
        ),
        Index(
            "uq_publication_intents_release_target_canonical",
            "release_id",
            "target",
            unique=True,
            sqlite_where=text("state NOT IN ('failed', 'cancelled')"),
            postgresql_where=text("state NOT IN ('failed', 'cancelled')"),
        ),
    )

    release_id: Mapped[UUID] = mapped_column(
        ForeignKey("releases.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    release_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("release_versions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    target: Mapped[PublicationTarget] = mapped_column(
        Enum(
            PublicationTarget,
            name="publication_target",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda members: [member.value for member in members],
            length=7,
        ),
        nullable=False,
    )
    state: Mapped[PublicationIntentState] = mapped_column(
        Enum(
            PublicationIntentState,
            name="publication_intent_state",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda members: [member.value for member in members],
            length=17,
        ),
        nullable=False,
        default=PublicationIntentState.AWAITING_APPROVAL,
    )
    configuration: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False)
    configuration_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    input_manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    intent_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    input_count: Mapped[int] = mapped_column(Integer, nullable=False)
    credential_reference: Mapped[str | None] = mapped_column(String(500))
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    public_preview_attester_name: Mapped[str | None] = mapped_column(String(256))
    public_preview_attester_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("admin_users.id", ondelete="RESTRICT"),
    )
    public_preview_attested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
    )
    public_preview_attestation_timezone: Mapped[str | None] = mapped_column(String(100))
    public_preview_attestation_sha256: Mapped[str | None] = mapped_column(String(64))
    planned_by_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("admin_users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    planned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lock_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(100))
    last_error_detail: Mapped[str | None] = mapped_column(String(500))


class PublicationInput(UuidPrimaryKeyMixin, Base):
    __tablename__ = "publication_inputs"
    __table_args__ = (
        UniqueConstraint(
            "intent_id",
            "ordinal",
            name="uq_publication_inputs_intent_ordinal",
        ),
        UniqueConstraint(
            "intent_id",
            "role",
            "derivative_output_id",
            name="uq_publication_inputs_intent_role_output",
        ),
        CheckConstraint("ordinal > 0", name="positive_ordinal"),
        CheckConstraint(
            "role IN ('x_teaser', 'patreon_content', 'patreon_preview')",
            name="valid_role",
        ),
        CheckConstraint("length(asset_sha256) = 64", name="valid_asset_sha256"),
        CheckConstraint(
            "length(trim(derivative_target)) > 0 "
            "AND length(trim(asset_storage_backend)) > 0 "
            "AND length(trim(asset_storage_bucket)) > 0 "
            "AND length(trim(asset_object_key)) > 0 "
            "AND length(trim(asset_object_version_id)) > 0 "
            "AND length(trim(asset_content_type)) > 0 "
            "AND length(trim(asset_image_format)) > 0",
            name="complete_asset_identity",
        ),
        CheckConstraint(
            "asset_width > 0 AND asset_height > 0 AND asset_byte_size > 0",
            name="positive_asset_dimensions",
        ),
        CheckConstraint(
            "(role = 'x_teaser' AND derivative_target = 'x_teaser') "
            "OR (role IN ('patreon_content', 'patreon_preview') "
            "AND derivative_target = 'full')",
            name="role_target",
        ),
        Index(
            "ix_publication_inputs_intent_role",
            "intent_id",
            "role",
            "ordinal",
        ),
    )

    intent_id: Mapped[UUID] = mapped_column(
        ForeignKey("publication_intents.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    derivative_output_id: Mapped[UUID] = mapped_column(
        ForeignKey("derivative_outputs.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    derivative_recipe_id: Mapped[UUID] = mapped_column(
        ForeignKey("derivative_recipes.id", ondelete="RESTRICT"),
        nullable=False,
    )
    asset_id: Mapped[UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT"),
        nullable=False,
    )
    derivative_target: Mapped[str] = mapped_column(String(50), nullable=False)
    asset_storage_backend: Mapped[str] = mapped_column(String(50), nullable=False)
    asset_storage_bucket: Mapped[str] = mapped_column(String(255), nullable=False)
    asset_object_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    asset_object_version_id: Mapped[str] = mapped_column(String(1024), nullable=False)
    asset_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    asset_content_type: Mapped[str] = mapped_column(String(100), nullable=False)
    asset_image_format: Mapped[str] = mapped_column(String(20), nullable=False)
    asset_width: Mapped[int] = mapped_column(Integer, nullable=False)
    asset_height: Mapped[int] = mapped_column(Integer, nullable=False)
    asset_byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    frozen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PublicationApproval(UuidPrimaryKeyMixin, Base):
    __tablename__ = "publication_approvals"
    __table_args__ = (
        UniqueConstraint(
            "intent_id",
            "revision",
            name="uq_publication_approvals_intent_revision",
        ),
        CheckConstraint("revision > 0", name="positive_revision"),
        CheckConstraint("intent_lock_version > 0", name="positive_intent_lock_version"),
        CheckConstraint("length(intent_digest) = 64", name="valid_intent_digest"),
        CheckConstraint(
            "actor_role IN ('owner', 'publisher')",
            name="publisher_role",
        ),
        CheckConstraint(
            "(action = 'approve' AND expires_at IS NOT NULL AND expires_at > recorded_at) "
            "OR (action = 'revoke' AND expires_at IS NULL)",
            name="approval_expiry",
        ),
        Index(
            "ix_publication_approvals_intent_recorded",
            "intent_id",
            "recorded_at",
        ),
    )

    intent_id: Mapped[UUID] = mapped_column(
        ForeignKey("publication_intents.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[PublicationApprovalAction] = mapped_column(
        Enum(
            PublicationApprovalAction,
            name="publication_approval_action",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda members: [member.value for member in members],
            length=7,
        ),
        nullable=False,
    )
    intent_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    intent_lock_version: Mapped[int] = mapped_column(Integer, nullable=False)
    actor_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("admin_users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    actor_role: Mapped[str] = mapped_column(String(20), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attestation_sha256: Mapped[str | None] = mapped_column(String(64))


class PublicationReconciliation(UuidPrimaryKeyMixin, Base):
    __tablename__ = "publication_reconciliations"
    __table_args__ = (
        UniqueConstraint(
            "intent_id",
            "revision",
            name="uq_publication_reconciliations_intent_revision",
        ),
        CheckConstraint("revision > 0", name="positive_revision"),
        CheckConstraint("intent_lock_version > 0", name="positive_intent_lock_version"),
        CheckConstraint("length(intent_digest) = 64", name="valid_intent_digest"),
        CheckConstraint("length(evidence_sha256) = 64", name="valid_evidence_sha256"),
        CheckConstraint(
            "length(attestation_sha256) = 64",
            name="valid_attestation_sha256",
        ),
        CheckConstraint(
            "actor_role IN ('owner', 'publisher')",
            name="publisher_role",
        ),
        CheckConstraint(
            "(outcome = 'confirmed_present' "
            "AND remote_identifier IS NOT NULL AND remote_url IS NOT NULL) "
            "OR (outcome = 'confirmed_absent' "
            "AND remote_identifier IS NULL AND remote_url IS NULL)",
            name="outcome_contract",
        ),
        CheckConstraint(
            "outcome IN ('confirmed_present', 'confirmed_absent')",
            name="valid_outcome",
        ),
        Index(
            "ix_publication_reconciliations_intent_recorded",
            "intent_id",
            "recorded_at",
        ),
    )

    intent_id: Mapped[UUID] = mapped_column(
        ForeignKey("publication_intents.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    outcome: Mapped[str] = mapped_column(String(24), nullable=False)
    intent_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    intent_lock_version: Mapped[int] = mapped_column(Integer, nullable=False)
    actor_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("admin_users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    actor_role: Mapped[str] = mapped_column(String(20), nullable=False)
    evidence_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    attestation_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    remote_identifier: Mapped[str | None] = mapped_column(String(200))
    remote_url: Mapped[str | None] = mapped_column(String(2048))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PublicationAttempt(UuidPrimaryKeyMixin, Base):
    __tablename__ = "publication_attempts"
    __table_args__ = (
        UniqueConstraint(
            "intent_id",
            "attempt_no",
            name="uq_publication_attempts_intent_number",
        ),
        CheckConstraint("attempt_no > 0", name="positive_attempt_number"),
        CheckConstraint("attempt_count >= 0", name="nonnegative_attempt_count"),
        CheckConstraint("max_attempts > 0", name="positive_max_attempts"),
        CheckConstraint("attempt_count <= max_attempts", name="attempts_within_limit"),
        CheckConstraint("lock_version > 0", name="positive_lock_version"),
        CheckConstraint(
            "(state IN ('claimed', 'processing') "
            "AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL) "
            "OR (state NOT IN ('claimed', 'processing') "
            "AND lease_owner IS NULL AND lease_expires_at IS NULL)",
            name="lease_state",
        ),
        CheckConstraint(
            "(state = 'retry_wait' AND retry_at IS NOT NULL) "
            "OR (state <> 'retry_wait' AND retry_at IS NULL)",
            name="retry_state",
        ),
        CheckConstraint(
            "(state IN ('succeeded', 'failed', 'cancelled') "
            "AND completed_at IS NOT NULL) "
            "OR (state NOT IN ('succeeded', 'failed', 'cancelled') "
            "AND completed_at IS NULL)",
            name="terminal_state",
        ),
        Index(
            "ix_publication_attempts_claim",
            "state",
            "retry_at",
            "lease_expires_at",
            "available_at",
            "created_at",
        ),
    )

    intent_id: Mapped[UUID] = mapped_column(
        ForeignKey("publication_intents.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    approval_id: Mapped[UUID] = mapped_column(
        ForeignKey("publication_approvals.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[PublicationAttemptState] = mapped_column(
        Enum(
            PublicationAttemptState,
            name="publication_attempt_state",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda members: [member.value for member in members],
            length=14,
        ),
        nullable=False,
        default=PublicationAttemptState.QUEUED,
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    lock_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[str | None] = mapped_column(String(200))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processing_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(100))
    last_error_detail: Mapped[str | None] = mapped_column(String(500))


class PublicationStep(UuidPrimaryKeyMixin, Base):
    __tablename__ = "publication_steps"
    __table_args__ = (
        UniqueConstraint(
            "attempt_id",
            "ordinal",
            name="uq_publication_steps_attempt_ordinal",
        ),
        CheckConstraint("ordinal > 0", name="positive_ordinal"),
        CheckConstraint("retry_count >= 0", name="nonnegative_retry_count"),
        CheckConstraint("max_retries >= 0", name="nonnegative_max_retries"),
        CheckConstraint("retry_count <= max_retries", name="retries_within_limit"),
        CheckConstraint("lock_version > 0", name="positive_lock_version"),
        CheckConstraint(
            "(kind = 'x_media_upload' AND publication_input_id IS NOT NULL) "
            "OR (kind <> 'x_media_upload' AND publication_input_id IS NULL)",
            name="input_kind",
        ),
        CheckConstraint(
            "(state = 'retry_wait' AND retry_at IS NOT NULL) "
            "OR (state <> 'retry_wait' AND retry_at IS NULL)",
            name="retry_state",
        ),
        CheckConstraint(
            "effect_completed_at IS NULL OR effect_started_at IS NOT NULL",
            name="effect_time_pair",
        ),
        Index(
            "ix_publication_steps_attempt_state",
            "attempt_id",
            "state",
            "ordinal",
        ),
    )

    attempt_id: Mapped[UUID] = mapped_column(
        ForeignKey("publication_attempts.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[PublicationStepKind] = mapped_column(
        Enum(
            PublicationStepKind,
            name="publication_step_kind",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda members: [member.value for member in members],
            length=16,
        ),
        nullable=False,
    )
    publication_input_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("publication_inputs.id", ondelete="RESTRICT"),
    )
    state: Mapped[PublicationStepState] = mapped_column(
        Enum(
            PublicationStepState,
            name="publication_step_state",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda members: [member.value for member in members],
            length=14,
        ),
        nullable=False,
        default=PublicationStepState.PENDING,
    )
    retry_class: Mapped[PublicationRetryClass | None] = mapped_column(
        Enum(
            PublicationRetryClass,
            name="publication_retry_class",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda members: [member.value for member in members],
            length=10,
        ),
    )
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_retries: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lock_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    effect_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    effect_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    guard_epoch: Mapped[int | None] = mapped_column(Integer)
    remote_identifier: Mapped[str | None] = mapped_column(String(200))
    remote_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    remote_url: Mapped[str | None] = mapped_column(String(2048))
    package_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("publication_packages.id", ondelete="RESTRICT"),
    )
    last_error_code: Mapped[str | None] = mapped_column(String(100))
    last_error_detail: Mapped[str | None] = mapped_column(String(500))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PublicationEffectEvent(UuidPrimaryKeyMixin, Base):
    __tablename__ = "publication_effect_events"
    __table_args__ = (
        UniqueConstraint(
            "step_id",
            "request_no",
            "is_completion",
            name="uq_publication_effect_events_request_phase",
        ),
        CheckConstraint("request_no > 0", name="positive_request_number"),
        CheckConstraint("guard_epoch > 0", name="positive_guard_epoch"),
        CheckConstraint(
            "event_type IN ('started', 'succeeded', 'retryable', 'unknown', 'terminal')",
            name="valid_event_type",
        ),
        CheckConstraint(
            "(event_type = 'started' AND NOT is_completion "
            "AND remote_identifier IS NULL AND remote_expires_at IS NULL "
            "AND error_code IS NULL) "
            "OR (event_type = 'succeeded' AND is_completion "
            "AND remote_identifier IS NOT NULL AND error_code IS NULL) "
            "OR (event_type IN ('retryable', 'unknown', 'terminal') "
            "AND is_completion AND error_code IS NOT NULL)",
            name="event_contract",
        ),
        Index(
            "ix_publication_effect_events_step_request",
            "step_id",
            "request_no",
            "recorded_at",
        ),
    )

    step_id: Mapped[UUID] = mapped_column(
        ForeignKey("publication_steps.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    request_no: Mapped[int] = mapped_column(Integer, nullable=False)
    step_kind: Mapped[PublicationStepKind] = mapped_column(
        Enum(
            PublicationStepKind,
            name="publication_effect_step_kind",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda members: [member.value for member in members],
            length=16,
        ),
        nullable=False,
    )
    event_type: Mapped[str] = mapped_column(String(16), nullable=False)
    is_completion: Mapped[bool] = mapped_column(Boolean, nullable=False)
    guard_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    remote_identifier: Mapped[str | None] = mapped_column(String(200))
    remote_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(100))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PublicationPackage(UuidPrimaryKeyMixin, Base):
    __tablename__ = "publication_packages"
    __table_args__ = (
        UniqueConstraint(
            "intent_id",
            "part_number",
            name="uq_publication_packages_intent_part",
        ),
        UniqueConstraint(
            "storage_backend",
            "storage_bucket",
            "object_key",
            "object_version_id",
            name="uq_publication_packages_storage_version",
        ),
        CheckConstraint("length(sha256) = 64", name="valid_sha256"),
        CheckConstraint("length(manifest_sha256) = 64", name="valid_manifest_sha256"),
        CheckConstraint("byte_size > 0", name="positive_byte_size"),
        CheckConstraint(
            "part_number > 0 AND part_count > 0 AND part_number <= part_count",
            name="valid_part_identity",
        ),
        CheckConstraint(
            "first_ordinal > 0 AND last_ordinal >= first_ordinal",
            name="valid_ordinal_range",
        ),
        CheckConstraint(
            "length(trim(storage_backend)) > 0 "
            "AND length(trim(storage_bucket)) > 0 "
            "AND length(trim(object_key)) > 0 "
            "AND length(trim(object_version_id)) > 0 "
            "AND content_type = 'application/zip'",
            name="complete_storage_identity",
        ),
    )

    intent_id: Mapped[UUID] = mapped_column(
        ForeignKey("publication_intents.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    part_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    part_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    first_ordinal: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    last_ordinal: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    storage_backend: Mapped[str] = mapped_column(String(50), nullable=False)
    storage_bucket: Mapped[str] = mapped_column(String(255), nullable=False)
    object_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    object_version_id: Mapped[str] = mapped_column(String(1024), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content_type: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class FinishedSetArchive(UuidPrimaryKeyMixin, Base):
    """Durable, provider-independent archive of one completed ranked set."""

    __tablename__ = "finished_set_archives"
    __table_args__ = (
        UniqueConstraint(
            "review_task_id",
            "media_profile",
            name="uq_finished_set_archives_review_profile",
        ),
        CheckConstraint("length(trim(media_profile)) > 0", name="nonempty_media_profile"),
        CheckConstraint("selection_count > 0", name="positive_selection_count"),
        CheckConstraint("attempts >= 0", name="nonnegative_attempts"),
        CheckConstraint("max_attempts > 0", name="positive_max_attempts"),
        CheckConstraint("attempts <= max_attempts", name="attempts_within_limit"),
        CheckConstraint(
            "(lease_owner IS NULL AND lease_expires_at IS NULL) OR "
            "(lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name="lease_pair",
        ),
        CheckConstraint(
            "(state = 'processing' AND lease_owner IS NOT NULL) OR "
            "(state <> 'processing' AND lease_owner IS NULL)",
            name="state_lease_contract",
        ),
        CheckConstraint(
            "manifest_sha256 IS NULL OR length(manifest_sha256) = 64",
            name="valid_manifest_sha256",
        ),
        CheckConstraint(
            "(mega_requested_at IS NULL AND mega_requested_by_user_id IS NULL "
            "AND mega_requested_remote_root IS NULL) OR "
            "(mega_requested_at IS NOT NULL AND mega_requested_by_user_id IS NOT NULL "
            "AND mega_requested_remote_root IS NOT NULL)",
            name="mega_request_pair",
        ),
        CheckConstraint("part_count IS NULL OR part_count > 0", name="positive_part_count"),
        CheckConstraint(
            "(state = 'ready' AND manifest_sha256 IS NOT NULL AND part_count IS NOT NULL "
            "AND completed_at IS NOT NULL AND last_error_code IS NULL) OR "
            "(state = 'failed' AND completed_at IS NOT NULL AND last_error_code IS NOT NULL) OR "
            "(state NOT IN ('ready', 'failed') AND completed_at IS NULL)",
            name="terminal_result_contract",
        ),
        Index(
            "ix_finished_set_archives_claim",
            "state",
            "available_at",
            "lease_expires_at",
            "created_at",
        ),
        Index(
            "ix_finished_set_archives_mega_request",
            "mega_requested_at",
            "state",
            "completed_at",
        ),
    )

    review_task_id: Mapped[UUID] = mapped_column(
        ForeignKey("review_tasks.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    release_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("release_versions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    media_profile: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
    )
    requested_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("admin_users.id", ondelete="RESTRICT"),
    )
    mega_requested_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("admin_users.id", ondelete="RESTRICT"),
    )
    mega_requested_remote_root: Mapped[str | None] = mapped_column(String(1024))
    mega_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    state: Mapped[FinishedSetArchiveState] = mapped_column(
        Enum(
            FinishedSetArchiveState,
            name="finished_set_archive_state",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda members: [member.value for member in members],
            length=12,
        ),
        nullable=False,
        default=FinishedSetArchiveState.PENDING,
    )
    selection_count: Mapped[int] = mapped_column(Integer, nullable=False)
    manifest_sha256: Mapped[str | None] = mapped_column(String(64))
    part_count: Mapped[int | None] = mapped_column(Integer)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(200))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(100))
    last_error_detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class FinishedSetArchivePart(UuidPrimaryKeyMixin, Base):
    """One immutable deterministic part of a finished-set archive."""

    __tablename__ = "finished_set_archive_parts"
    __table_args__ = (
        UniqueConstraint(
            "archive_id",
            "part_number",
            name="uq_finished_set_archive_parts_archive_part",
        ),
        UniqueConstraint(
            "storage_backend",
            "storage_bucket",
            "object_key",
            "object_version_id",
            name="uq_finished_set_archive_parts_storage_version",
        ),
        CheckConstraint(
            "part_number > 0 AND part_count > 0 AND part_number <= part_count",
            name="valid_part_identity",
        ),
        CheckConstraint(
            "first_ordinal > 0 AND last_ordinal >= first_ordinal",
            name="valid_ordinal_range",
        ),
        CheckConstraint("length(sha256) = 64", name="valid_sha256"),
        CheckConstraint("length(manifest_sha256) = 64", name="valid_manifest_sha256"),
        CheckConstraint("byte_size > 0", name="positive_byte_size"),
        CheckConstraint(
            "length(trim(storage_backend)) > 0 "
            "AND length(trim(storage_bucket)) > 0 "
            "AND length(trim(object_key)) > 0 "
            "AND length(trim(object_version_id)) > 0 "
            "AND content_type = 'application/zip'",
            name="complete_storage_identity",
        ),
    )

    archive_id: Mapped[UUID] = mapped_column(
        ForeignKey("finished_set_archives.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    part_number: Mapped[int] = mapped_column(Integer, nullable=False)
    part_count: Mapped[int] = mapped_column(Integer, nullable=False)
    first_ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    last_ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    storage_backend: Mapped[str] = mapped_column(String(50), nullable=False)
    storage_bucket: Mapped[str] = mapped_column(String(255), nullable=False)
    object_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    object_version_id: Mapped[str] = mapped_column(String(1024), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content_type: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MegaDelivery(UuidPrimaryKeyMixin, TimestampMixin, Base):
    """Durable delivery of one immutable, clean Patreon package to MEGA."""

    __tablename__ = "mega_deliveries"
    __table_args__ = (
        UniqueConstraint(
            "publication_package_id",
            name="uq_mega_deliveries_publication_package",
        ),
        UniqueConstraint("remote_path", name="uq_mega_deliveries_remote_path"),
        CheckConstraint("length(sha256) = 64", name="valid_sha256"),
        CheckConstraint("byte_size > 0", name="positive_byte_size"),
        CheckConstraint("attempts >= 0", name="nonnegative_attempts"),
        CheckConstraint(
            "length(trim(remote_root)) > 0 AND length(trim(remote_path)) > 0",
            name="complete_remote_identity",
        ),
        CheckConstraint(
            "(lease_owner IS NULL AND lease_expires_at IS NULL) OR "
            "(lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name="lease_pair",
        ),
        CheckConstraint(
            "(state = 'claimed' AND lease_owner IS NOT NULL) OR "
            "(state <> 'claimed' AND lease_owner IS NULL)",
            name="state_lease_contract",
        ),
        CheckConstraint(
            "(state = 'succeeded' AND remote_node_handle IS NOT NULL "
            "AND verified_at IS NOT NULL AND completed_at IS NOT NULL) OR "
            "(state <> 'succeeded' AND remote_node_handle IS NULL "
            "AND verified_at IS NULL)",
            name="success_contract",
        ),
        Index(
            "ix_mega_deliveries_claim",
            "state",
            "available_at",
            "lease_expires_at",
            "created_at",
        ),
    )

    publication_package_id: Mapped[UUID] = mapped_column(
        ForeignKey("publication_packages.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    state: Mapped[MegaDeliveryState] = mapped_column(
        Enum(
            MegaDeliveryState,
            name="mega_delivery_state",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda members: [member.value for member in members],
            length=10,
        ),
        nullable=False,
        default=MegaDeliveryState.PENDING,
    )
    remote_root: Mapped[str] = mapped_column(String(1024), nullable=False)
    remote_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(200))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    remote_node_handle: Mapped[str | None] = mapped_column(String(80))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(100))
    last_error_detail: Mapped[str | None] = mapped_column(Text)


class MegaSetDelivery(UuidPrimaryKeyMixin, TimestampMixin, Base):
    """Durable extracted-folder delivery of one immutable finished set."""

    __tablename__ = "mega_set_deliveries"
    __table_args__ = (
        UniqueConstraint(
            "finished_set_archive_id",
            name="uq_mega_set_deliveries_finished_set_archive",
        ),
        CheckConstraint("length(manifest_sha256) = 64", name="valid_manifest_sha256"),
        CheckConstraint("total_item_count > 0", name="positive_total_item_count"),
        CheckConstraint(
            "uploaded_item_count >= 0 AND uploaded_item_count <= total_item_count",
            name="valid_uploaded_item_count",
        ),
        CheckConstraint(
            "total_byte_size IS NULL OR total_byte_size > 0",
            name="positive_optional_total_byte_size",
        ),
        CheckConstraint(
            "uploaded_byte_size >= 0 "
            "AND (total_byte_size IS NULL OR uploaded_byte_size <= total_byte_size)",
            name="valid_uploaded_byte_size",
        ),
        CheckConstraint(
            "(total_byte_size IS NULL AND source_manifest_json IS NULL "
            "AND planned_at IS NULL AND uploaded_byte_size = 0) OR "
            "(total_byte_size IS NOT NULL AND source_manifest_json IS NOT NULL "
            "AND length(source_manifest_json) > 0 AND planned_at IS NOT NULL)",
            name="planning_contract",
        ),
        CheckConstraint("attempts >= 0", name="nonnegative_attempts"),
        CheckConstraint(
            "length(trim(remote_root)) > 0 AND length(trim(remote_folder)) > 0",
            name="complete_remote_identity",
        ),
        CheckConstraint(
            "(lease_owner IS NULL AND lease_expires_at IS NULL) OR "
            "(lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name="lease_pair",
        ),
        CheckConstraint(
            "(state = 'claimed' AND lease_owner IS NOT NULL) OR "
            "(state <> 'claimed' AND lease_owner IS NULL)",
            name="state_lease_contract",
        ),
        CheckConstraint(
            "(state = 'succeeded' AND uploaded_item_count = total_item_count "
            "AND total_byte_size IS NOT NULL AND uploaded_byte_size = total_byte_size "
            "AND verified_at IS NOT NULL AND completed_at IS NOT NULL "
            "AND last_error_code IS NULL) OR "
            "(state = 'failed' AND completion_marker_node_handle IS NULL "
            "AND verified_at IS NULL AND completed_at IS NOT NULL "
            "AND last_error_code IS NOT NULL) OR "
            "(state IN ('pending', 'claimed', 'retry_wait') "
            "AND completion_marker_node_handle IS NULL "
            "AND verified_at IS NULL AND completed_at IS NULL)",
            name="terminal_result_contract",
        ),
        CheckConstraint(
            "started_at IS NULL OR started_at >= created_at",
            name="start_after_creation",
        ),
        CheckConstraint(
            "planned_at IS NULL OR planned_at >= created_at",
            name="plan_after_creation",
        ),
        CheckConstraint(
            "verified_at IS NULL OR verified_at >= created_at",
            name="verification_after_creation",
        ),
        CheckConstraint(
            "completed_at IS NULL OR completed_at >= created_at",
            name="completion_after_creation",
        ),
        Index(
            "ix_mega_set_deliveries_claim",
            "state",
            "available_at",
            "lease_expires_at",
            "created_at",
        ),
    )

    finished_set_archive_id: Mapped[UUID] = mapped_column(
        ForeignKey("finished_set_archives.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    state: Mapped[MegaDeliveryState] = mapped_column(
        Enum(
            MegaDeliveryState,
            name="mega_delivery_state",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda members: [member.value for member in members],
            length=10,
        ),
        nullable=False,
        default=MegaDeliveryState.PENDING,
    )
    remote_root: Mapped[str] = mapped_column(String(1024), nullable=False)
    remote_folder: Mapped[str] = mapped_column(String(1024), nullable=False)
    manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    total_item_count: Mapped[int] = mapped_column(Integer, nullable=False)
    uploaded_item_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_byte_size: Mapped[int | None] = mapped_column(BigInteger)
    source_manifest_json: Mapped[str | None] = mapped_column(Text)
    uploaded_byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(200))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completion_marker_node_handle: Mapped[str | None] = mapped_column(String(80))
    planned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(100))
    last_error_detail: Mapped[str | None] = mapped_column(Text)

    items: Mapped[list["MegaSetDeliveryItem"]] = relationship(
        back_populates="delivery",
        order_by="MegaSetDeliveryItem.ordinal",
    )


class MegaSetDeliveryItem(UuidPrimaryKeyMixin, TimestampMixin, Base):
    """One ordered, restart-safe image transfer within a MEGA set folder."""

    __tablename__ = "mega_set_delivery_items"
    __table_args__ = (
        UniqueConstraint(
            "delivery_id",
            "ordinal",
            name="uq_mega_set_delivery_items_delivery_ordinal",
        ),
        UniqueConstraint(
            "delivery_id",
            "source_asset_id",
            name="uq_mega_set_delivery_items_delivery_source",
        ),
        UniqueConstraint(
            "delivery_id",
            "readiness_derivative_output_id",
            name="uq_mega_set_delivery_items_delivery_readiness",
        ),
        UniqueConstraint("remote_path", name="uq_mega_set_delivery_items_remote_path"),
        CheckConstraint("ordinal > 0", name="positive_ordinal"),
        CheckConstraint("length(source_sha256) = 64", name="valid_source_sha256"),
        CheckConstraint("source_byte_size > 0", name="positive_source_byte_size"),
        CheckConstraint(
            "length(trim(source_content_type)) > 0 AND length(trim(remote_path)) > 0",
            name="complete_item_identity",
        ),
        CheckConstraint("attempts >= 0", name="nonnegative_attempts"),
        CheckConstraint(
            "(remote_node_handle IS NULL AND verified_at IS NULL) OR "
            "(remote_node_handle IS NOT NULL AND verified_at IS NOT NULL)",
            name="verified_node_pair",
        ),
        CheckConstraint(
            "(state = 'succeeded' AND uploaded_at IS NOT NULL "
            "AND completed_at IS NOT NULL AND last_error_code IS NULL) OR "
            "(state = 'failed' AND completed_at IS NOT NULL "
            "AND last_error_code IS NOT NULL) OR "
            "(state IN ('pending', 'claimed', 'retry_wait') AND completed_at IS NULL)",
            name="terminal_result_contract",
        ),
        CheckConstraint(
            "uploaded_at IS NULL OR uploaded_at >= created_at",
            name="upload_after_creation",
        ),
        CheckConstraint(
            "verified_at IS NULL OR verified_at >= created_at",
            name="verification_after_creation",
        ),
        CheckConstraint(
            "completed_at IS NULL OR completed_at >= created_at",
            name="completion_after_creation",
        ),
        Index(
            "ix_mega_set_delivery_items_progress",
            "delivery_id",
            "state",
            "available_at",
            "ordinal",
        ),
    )

    delivery_id: Mapped[UUID] = mapped_column(
        ForeignKey("mega_set_deliveries.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    source_asset_id: Mapped[UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    readiness_derivative_output_id: Mapped[UUID] = mapped_column(
        ForeignKey("derivative_outputs.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_content_type: Mapped[str] = mapped_column(String(100), nullable=False)
    remote_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    state: Mapped[MegaDeliveryState] = mapped_column(
        Enum(
            MegaDeliveryState,
            name="mega_delivery_state",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda members: [member.value for member in members],
            length=10,
        ),
        nullable=False,
        default=MegaDeliveryState.PENDING,
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    remote_node_handle: Mapped[str | None] = mapped_column(String(80))
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(100))
    last_error_detail: Mapped[str | None] = mapped_column(Text)

    delivery: Mapped[MegaSetDelivery] = relationship(back_populates="items")


class PublicationProviderGuard(UuidPrimaryKeyMixin, Base):
    __tablename__ = "publication_provider_guards"
    __table_args__ = (
        UniqueConstraint("provider", name="uq_publication_provider_guards_provider"),
        CheckConstraint("provider = 'global'", name="global_only"),
        CheckConstraint("epoch > 0", name="positive_epoch"),
        CheckConstraint("lock_version > 0", name="positive_lock_version"),
        CheckConstraint("length(trim(reason)) > 0", name="nonempty_reason"),
    )

    provider: Mapped[str] = mapped_column(String(20), nullable=False, default="global")
    enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    lock_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    reason: Mapped[str] = mapped_column(
        String(500),
        nullable=False,
        default="publication is stopped by default",
    )
    changed_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("admin_users.id", ondelete="RESTRICT"),
    )
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AdminSession(UuidPrimaryKeyMixin, Base):
    __tablename__ = "admin_sessions"
    __table_args__ = (
        UniqueConstraint("token_sha256", name="uq_admin_sessions_token_sha256"),
        CheckConstraint(
            "expires_at > created_at",
            name="expiry_after_creation",
        ),
        CheckConstraint(
            "idle_expires_at > created_at AND idle_expires_at <= expires_at",
            name="valid_idle_expiry",
        ),
        CheckConstraint(
            "last_seen_at >= created_at AND last_seen_at <= idle_expires_at",
            name="valid_last_seen_time",
        ),
        CheckConstraint(
            "reauthenticated_at >= created_at AND reauthenticated_at <= expires_at",
            name="valid_reauthentication_time",
        ),
        CheckConstraint(
            "mfa_verified_at IS NULL OR "
            "(mfa_verified_at >= created_at AND mfa_verified_at <= expires_at)",
            name="valid_mfa_time",
        ),
        CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= created_at",
            name="valid_revocation_time",
        ),
        CheckConstraint(
            "credential_version > 0",
            name="positive_credential_version",
        ),
        Index(
            "ix_admin_sessions_user_active",
            "user_id",
            "revoked_at",
            "expires_at",
        ),
        Index("ix_admin_sessions_expires_at", "expires_at"),
        Index("ix_admin_sessions_idle_expires_at", "idle_expires_at"),
    )

    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("admin_users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    token_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    csrf_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    credential_version: Mapped[int] = mapped_column(Integer, nullable=False)
    client_context_hmac: Mapped[str | None] = mapped_column(String(64))
    user_agent_hmac: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    idle_expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    reauthenticated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    mfa_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LoginThrottle(UuidPrimaryKeyMixin, Base):
    __tablename__ = "login_throttles"
    __table_args__ = (
        UniqueConstraint("key_sha256", name="uq_login_throttles_key_sha256"),
        CheckConstraint("failure_count >= 0", name="nonnegative_failure_count"),
        Index("ix_login_throttles_updated_at", "updated_at"),
        Index("ix_login_throttles_blocked_until", "blocked_until"),
    )

    key_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    window_started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    blocked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )


class SubjectApproval(UuidPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "subject_approvals"
    __table_args__ = (
        UniqueConstraint(
            "canonical_source_sha256",
            "approval_version",
            name="uq_subject_approvals_identity_version",
        ),
        UniqueConstraint(
            "slug",
            "approval_version",
            name="uq_subject_approvals_slug_version",
        ),
        Index(
            "uq_subject_approvals_current_source",
            "canonical_source_sha256",
            unique=True,
            postgresql_where=text("is_current"),
            sqlite_where=text("is_current = 1"),
        ),
        Index(
            "uq_subject_approvals_current_slug",
            "slug",
            unique=True,
            postgresql_where=text("is_current"),
            sqlite_where=text("is_current = 1"),
        ),
        CheckConstraint("canonical_age >= 18", name="adult_canonical_age"),
        CheckConstraint("clearly_adult = true", name="clearly_adult_required"),
        CheckConstraint("is_fictional = true", name="fictional_only"),
        CheckConstraint("is_aged_up_minor = false", name="aged_up_minor_forbidden"),
        CheckConstraint(
            "distribution_rights_approved = true",
            name="distribution_rights_required",
        ),
        CheckConstraint(
            "adult_derivative_rights_approved = true",
            name="adult_derivative_rights_required",
        ),
        CheckConstraint("approval_version > 0", name="positive_approval_version"),
        CheckConstraint(
            "(status = 'approved' AND revoked_at IS NULL "
            "AND revoked_by_user_id IS NULL) OR "
            "(status = 'revoked' AND revoked_at IS NOT NULL "
            "AND revoked_by_user_id IS NOT NULL)",
            name="revocation_state",
        ),
        CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= approved_at",
            name="valid_revocation_time",
        ),
    )

    slug: Mapped[str] = mapped_column(String(80), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    canonical_source_url: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    canonical_age: Mapped[int] = mapped_column(Integer, nullable=False)
    clearly_adult: Mapped[bool] = mapped_column(Boolean, nullable=False)
    is_fictional: Mapped[bool] = mapped_column(Boolean, nullable=False)
    is_aged_up_minor: Mapped[bool] = mapped_column(Boolean, nullable=False)
    distribution_rights_approved: Mapped[bool] = mapped_column(Boolean, nullable=False)
    adult_derivative_rights_approved: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
    )
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False)
    evidence_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[ApprovalStatus] = mapped_column(
        Enum(
            ApprovalStatus,
            name="approval_status",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda members: [member.value for member in members],
            length=8,
        ),
        nullable=False,
    )
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    approval_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    approved_by_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("admin_users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("admin_users.id", ondelete="RESTRICT"),
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ModelArtifactApproval(UuidPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "model_artifact_approvals"
    __table_args__ = (
        UniqueConstraint(
            "artifact_sha256",
            "approval_version",
            name="uq_model_artifact_approvals_identity_version",
        ),
        Index(
            "uq_model_artifact_approvals_current_artifact",
            "artifact_sha256",
            unique=True,
            postgresql_where=text("is_current"),
            sqlite_where=text("is_current = 1"),
        ),
        CheckConstraint(
            "commercial_use_approved = true OR experiment_only = true",
            name="approved_usage_scope",
        ),
        CheckConstraint("adult_use_approved = true", name="adult_use_required"),
        CheckConstraint("safetensors_verified = true", name="safetensors_required"),
        CheckConstraint("approval_version > 0", name="positive_approval_version"),
        CheckConstraint(
            "(status = 'approved' AND revoked_at IS NULL "
            "AND revoked_by_user_id IS NULL) OR "
            "(status = 'revoked' AND revoked_at IS NOT NULL "
            "AND revoked_by_user_id IS NOT NULL)",
            name="revocation_state",
        ),
        CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= approved_at",
            name="valid_revocation_time",
        ),
    )

    artifact_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[ModelArtifactKind] = mapped_column(
        Enum(
            ModelArtifactKind,
            name="model_artifact_kind",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda members: [member.value for member in members],
            length=10,
        ),
        nullable=False,
    )
    model_family: Mapped[GenerationModelFamily] = mapped_column(
        Enum(
            GenerationModelFamily,
            name="generation_model_family",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda members: [member.value for member in members],
            length=20,
        ),
        nullable=False,
        default=GenerationModelFamily.ILLUSTRIOUS,
    )
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    storage_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    license_url: Mapped[str] = mapped_column(Text, nullable=False)
    commercial_use_approved: Mapped[bool] = mapped_column(Boolean, nullable=False)
    experiment_only: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )
    adult_use_approved: Mapped[bool] = mapped_column(Boolean, nullable=False)
    safetensors_verified: Mapped[bool] = mapped_column(Boolean, nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False)
    evidence_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[ApprovalStatus] = mapped_column(
        Enum(
            ApprovalStatus,
            name="model_approval_status",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda members: [member.value for member in members],
            length=8,
        ),
        nullable=False,
    )
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    approval_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    approved_by_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("admin_users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("admin_users.id", ondelete="RESTRICT"),
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ManagedLoraArtifact(UuidPrimaryKeyMixin, TimestampMixin, Base):
    """One immutable, content-addressed LoRA and its deployment lifecycle."""

    __tablename__ = "managed_lora_artifacts"
    __table_args__ = (
        UniqueConstraint(
            "storage_bucket",
            "object_key",
            "object_version_id",
            name="uq_managed_lora_artifacts_storage_version",
        ),
        Index(
            "uq_managed_lora_artifacts_live_sha256",
            "artifact_sha256",
            unique=True,
            postgresql_where=text("lifecycle <> 'purged'"),
            sqlite_where=text("lifecycle <> 'purged'"),
        ),
        Index(
            "uq_managed_lora_artifacts_live_approval",
            "approval_id",
            unique=True,
            postgresql_where=text("lifecycle <> 'purged'"),
            sqlite_where=text("lifecycle <> 'purged'"),
        ),
        Index(
            "uq_managed_lora_artifacts_live_target_filename",
            "target_filename",
            unique=True,
            postgresql_where=text("lifecycle <> 'purged'"),
            sqlite_where=text("lifecycle <> 'purged'"),
        ),
        CheckConstraint(
            _lower_hex_check("artifact_sha256"),
            name="valid_artifact_sha256",
        ),
        CheckConstraint("byte_size > 0", name="positive_byte_size"),
        CheckConstraint("lock_version > 0", name="positive_lock_version"),
        CheckConstraint(
            "lifecycle_error_count >= 0",
            name="nonnegative_lifecycle_error_count",
        ),
        CheckConstraint(
            "(lifecycle_error_code IS NULL AND lifecycle_error_detail IS NULL) "
            "OR (lifecycle_error_code IS NOT NULL AND lifecycle_error_detail IS NOT NULL)",
            name="complete_lifecycle_error",
        ),
        CheckConstraint(
            "length(trim(display_name)) > 0 "
            "AND length(trim(canonical_source_url)) > 0 "
            "AND length(trim(license_url)) > 0 "
            "AND length(trim(storage_bucket)) > 0 "
            "AND length(trim(object_key)) > 0 "
            "AND length(trim(object_version_id)) > 0 "
            "AND length(trim(object_etag)) > 0 "
            "AND length(trim(target_filename)) > 0",
            name="complete_identity",
        ),
        CheckConstraint(
            "lower(target_filename) LIKE '%.safetensors'",
            name="safetensors_target",
        ),
        CheckConstraint(
            "object_key = 'worker/managed-loras/sha256/' || artifact_sha256 || '.safetensors'",
            name="content_addressed_object_key",
        ),
        CheckConstraint(
            "(lifecycle = 'active' AND activated_at IS NOT NULL) OR lifecycle <> 'active'",
            name="active_timestamp",
        ),
        CheckConstraint(
            "(lifecycle IN ('retiring', 'retired', 'purged') "
            "AND retirement_requested_at IS NOT NULL) "
            "OR lifecycle IN ('pending_activation', 'active')",
            name="retirement_timestamp",
        ),
        CheckConstraint(
            "(lifecycle = 'purged' AND purge_requested = true "
            "AND retired_at IS NOT NULL AND purged_at IS NOT NULL) "
            "OR (lifecycle <> 'purged' AND purged_at IS NULL)",
            name="purge_state",
        ),
        Index(
            "ix_managed_lora_artifacts_lifecycle_created",
            "lifecycle",
            "lifecycle_retry_at",
            "created_at",
        ),
    )

    artifact_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    source_type: Mapped[LoraImportSource] = mapped_column(
        Enum(
            LoraImportSource,
            name="lora_import_source",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda members: [member.value for member in members],
            length=7,
        ),
        nullable=False,
    )
    canonical_source_url: Mapped[str] = mapped_column(Text, nullable=False)
    license_url: Mapped[str] = mapped_column(Text, nullable=False)
    civitai_model_id: Mapped[int | None] = mapped_column(BigInteger)
    civitai_version_id: Mapped[int | None] = mapped_column(BigInteger)
    civitai_file_id: Mapped[int | None] = mapped_column(BigInteger)
    provenance: Mapped[dict[str, Any]] = mapped_column(
        JSON_TYPE,
        nullable=False,
        default=dict,
    )
    storage_bucket: Mapped[str] = mapped_column(String(255), nullable=False)
    object_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    object_version_id: Mapped[str] = mapped_column(String(1024), nullable=False)
    object_etag: Mapped[str] = mapped_column(String(80), nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    target_filename: Mapped[str] = mapped_column(String(236), nullable=False)
    approval_id: Mapped[UUID] = mapped_column(
        ForeignKey("model_artifact_approvals.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    trigger_words: Mapped[list[str]] = mapped_column(JSON_TYPE, nullable=False, default=list)
    lifecycle: Mapped[ManagedLoraLifecycle] = mapped_column(
        Enum(
            ManagedLoraLifecycle,
            name="managed_lora_lifecycle",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda members: [member.value for member in members],
            length=18,
        ),
        nullable=False,
        default=ManagedLoraLifecycle.PENDING_ACTIVATION,
    )
    purge_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    registered_by_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("admin_users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    retirement_requested_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("admin_users.id", ondelete="RESTRICT"),
    )
    restored_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("admin_users.id", ondelete="RESTRICT"),
    )
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retirement_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    restored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lifecycle_error_code: Mapped[str | None] = mapped_column(String(100))
    lifecycle_error_detail: Mapped[str | None] = mapped_column(Text)
    lifecycle_error_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    lifecycle_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lock_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class LoraImportJob(UuidPrimaryKeyMixin, TimestampMixin, Base):
    """Restart-safe manual or Civitai import request; rows are never deleted."""

    __tablename__ = "lora_import_jobs"
    __table_args__ = (
        UniqueConstraint(
            "staging_bucket",
            "staging_object_key",
            name="uq_lora_import_jobs_staging_object",
        ),
        CheckConstraint("attempts >= 0", name="nonnegative_attempts"),
        CheckConstraint("max_attempts > 0", name="positive_max_attempts"),
        CheckConstraint("attempts <= max_attempts", name="attempts_within_limit"),
        CheckConstraint(
            "commercial_use_attested = true AND adult_use_attested = true",
            name="rights_attested",
        ),
        CheckConstraint("lock_version > 0", name="positive_lock_version"),
        CheckConstraint("progress_bytes >= 0", name="nonnegative_progress"),
        CheckConstraint(
            "total_bytes IS NULL OR total_bytes > 0",
            name="positive_total_bytes",
        ),
        CheckConstraint(
            "total_bytes IS NULL OR progress_bytes <= total_bytes",
            name="progress_within_total",
        ),
        CheckConstraint(
            "expected_sha256 IS NULL OR (" + _lower_hex_check("expected_sha256") + ")",
            name="valid_expected_sha256",
        ),
        CheckConstraint(
            "expected_byte_size IS NULL OR expected_byte_size > 0",
            name="positive_expected_byte_size",
        ),
        CheckConstraint(
            "(source_type = 'manual' AND staging_bucket IS NOT NULL "
            "AND staging_object_key IS NOT NULL "
            "AND civitai_model_id IS NULL AND civitai_version_id IS NULL "
            "AND civitai_file_id IS NULL) "
            "OR (source_type = 'civitai' AND staging_bucket IS NULL "
            "AND staging_object_key IS NULL)",
            name="source_contract",
        ),
        CheckConstraint(
            "(staging_object_version_id IS NULL AND staging_object_etag IS NULL "
            "AND staging_byte_size IS NULL) OR "
            "(staging_object_version_id IS NOT NULL AND staging_object_etag IS NOT NULL "
            "AND staging_byte_size IS NOT NULL AND staging_byte_size > 0)",
            name="staging_version_tuple",
        ),
        CheckConstraint(
            "source_type = 'manual' OR staging_object_version_id IS NULL",
            name="manual_staging_version_only",
        ),
        CheckConstraint(
            "state <> 'awaiting_upload' OR (source_type = 'manual' "
            "AND staging_object_version_id IS NULL)",
            name="awaiting_manual_upload",
        ),
        CheckConstraint(
            "state NOT IN ('queued', 'claimed', 'retry_wait', 'failed') "
            "OR source_type = 'civitai' OR staging_object_version_id IS NOT NULL",
            name="manual_processing_has_version",
        ),
        CheckConstraint(
            "(state = 'claimed' AND lease_owner IS NOT NULL "
            "AND lease_expires_at IS NOT NULL) OR "
            "(state <> 'claimed' AND lease_owner IS NULL AND lease_expires_at IS NULL)",
            name="lease_state",
        ),
        CheckConstraint(
            "(state = 'completed' AND result_artifact_id IS NOT NULL "
            "AND completed_at IS NOT NULL AND last_error_code IS NULL) OR "
            "(state = 'duplicate' AND completed_at IS NOT NULL AND ("
            "(result_artifact_id IS NOT NULL AND last_error_code IS NULL) OR "
            "(result_artifact_id IS NULL "
            "AND last_error_code = 'already_available_static' "
            "AND last_error_detail IS NOT NULL))) OR "
            "(state = 'failed' AND result_artifact_id IS NULL "
            "AND completed_at IS NOT NULL AND last_error_code IS NOT NULL) OR "
            "(state = 'cancelled' AND result_artifact_id IS NULL "
            "AND completed_at IS NOT NULL) OR "
            "(state NOT IN ('completed', 'duplicate', 'failed', 'cancelled') "
            "AND result_artifact_id IS NULL AND completed_at IS NULL)",
            name="terminal_result",
        ),
        Index(
            "ix_lora_import_jobs_claim",
            "state",
            "available_at",
            "lease_expires_at",
            "created_at",
        ),
        Index(
            "ix_lora_import_jobs_requester_created",
            "requested_by_user_id",
            "created_at",
        ),
    )

    source_type: Mapped[LoraImportSource] = mapped_column(
        Enum(
            LoraImportSource,
            name="lora_import_source",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda members: [member.value for member in members],
            length=7,
        ),
        nullable=False,
    )
    state: Mapped[LoraImportJobState] = mapped_column(
        Enum(
            LoraImportJobState,
            name="lora_import_job_state",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda members: [member.value for member in members],
            length=15,
        ),
        nullable=False,
    )
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    canonical_source_url: Mapped[str] = mapped_column(Text, nullable=False)
    license_url: Mapped[str] = mapped_column(Text, nullable=False)
    commercial_use_attested: Mapped[bool] = mapped_column(Boolean, nullable=False)
    adult_use_attested: Mapped[bool] = mapped_column(Boolean, nullable=False)
    civitai_model_id: Mapped[int | None] = mapped_column(BigInteger)
    civitai_version_id: Mapped[int | None] = mapped_column(BigInteger)
    civitai_file_id: Mapped[int | None] = mapped_column(BigInteger)
    staging_bucket: Mapped[str | None] = mapped_column(String(255))
    staging_object_key: Mapped[str | None] = mapped_column(String(1024))
    staging_object_version_id: Mapped[str | None] = mapped_column(String(1024))
    staging_object_etag: Mapped[str | None] = mapped_column(String(80))
    staging_byte_size: Mapped[int | None] = mapped_column(BigInteger)
    target_filename: Mapped[str] = mapped_column(String(236), nullable=False)
    expected_sha256: Mapped[str | None] = mapped_column(String(64))
    expected_byte_size: Mapped[int | None] = mapped_column(BigInteger)
    expected_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSON_TYPE,
        nullable=False,
        default=dict,
    )
    trigger_words: Mapped[list[str]] = mapped_column(JSON_TYPE, nullable=False, default=list)
    progress_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    total_bytes: Mapped[int | None] = mapped_column(BigInteger)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(200))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(100))
    last_error_detail: Mapped[str | None] = mapped_column(Text)
    result_artifact_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("managed_lora_artifacts.id", ondelete="RESTRICT"),
        index=True,
    )
    requested_by_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("admin_users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    cancelled_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("admin_users.id", ondelete="RESTRICT"),
    )
    lock_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_progress_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WorkflowApproval(UuidPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "workflow_approvals"
    __table_args__ = (
        UniqueConstraint(
            "workflow_sha256",
            "approval_version",
            name="uq_workflow_approvals_identity_version",
        ),
        Index(
            "uq_workflow_approvals_current_workflow",
            "workflow_sha256",
            unique=True,
            postgresql_where=text("is_current"),
            sqlite_where=text("is_current = 1"),
        ),
        CheckConstraint("approval_version > 0", name="positive_approval_version"),
        CheckConstraint(
            "(status = 'approved' AND revoked_at IS NULL "
            "AND revoked_by_user_id IS NULL) OR "
            "(status = 'revoked' AND revoked_at IS NOT NULL "
            "AND revoked_by_user_id IS NOT NULL)",
            name="revocation_state",
        ),
        CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= approved_at",
            name="valid_revocation_time",
        ),
    )

    workflow_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    version: Mapped[str] = mapped_column(String(100), nullable=False)
    model_family: Mapped[GenerationModelFamily] = mapped_column(
        Enum(
            GenerationModelFamily,
            name="workflow_generation_model_family",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda members: [member.value for member in members],
            length=20,
        ),
        nullable=False,
        default=GenerationModelFamily.ILLUSTRIOUS,
    )
    object_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    reviewed_node_classes: Mapped[list[str]] = mapped_column(JSON_TYPE, nullable=False)
    capabilities: Mapped[list[str]] = mapped_column(JSON_TYPE, nullable=False, default=list)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False)
    evidence_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[ApprovalStatus] = mapped_column(
        Enum(
            ApprovalStatus,
            name="workflow_approval_status",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda members: [member.value for member in members],
            length=8,
        ),
        nullable=False,
    )
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    approval_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    approved_by_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("admin_users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("admin_users.id", ondelete="RESTRICT"),
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class I2VInput(UuidPrimaryKeyMixin, Base):
    """One immutable, verified source image for the isolated I2V queue."""

    __tablename__ = "i2v_inputs"
    __table_args__ = (
        CheckConstraint("source IN ('upload', 'generation')", name="known_source"),
        CheckConstraint(
            "source <> 'generation' OR asset_id IS NOT NULL",
            name="generation_has_asset",
        ),
        CheckConstraint("content_type LIKE 'image/%'", name="image_content"),
        CheckConstraint(
            "width > 0 AND height > 0 AND byte_size > 0",
            name="positive_dimensions_and_size",
        ),
        CheckConstraint(_lower_hex_check("sha256"), name="valid_sha256"),
        UniqueConstraint(
            "storage_backend",
            "storage_bucket",
            "object_key",
            "object_version_id",
            name="uq_i2v_inputs_frozen_object",
        ),
        Index(
            "uq_i2v_inputs_unversioned_object",
            "storage_backend",
            "storage_bucket",
            "object_key",
            unique=True,
            postgresql_where=text("object_version_id IS NULL"),
            sqlite_where=text("object_version_id IS NULL"),
        ),
        Index("ix_i2v_inputs_recent", "created_at", "id"),
    )

    created_by_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("admin_users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    source: Mapped[str] = mapped_column(String(10), nullable=False)
    asset_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT"), index=True
    )
    display_name: Mapped[str] = mapped_column(String(500), nullable=False)
    storage_backend: Mapped[str] = mapped_column(String(50), nullable=False)
    storage_bucket: Mapped[str] = mapped_column(String(255), nullable=False)
    object_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    object_version_id: Mapped[str | None] = mapped_column(String(1024))
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    content_type: Mapped[str] = mapped_column(String(100), nullable=False)
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    input_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSON_TYPE, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )


class I2VPreset(UuidPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "i2v_presets"
    __table_args__ = (
        CheckConstraint("lock_version > 0", name="positive_lock_version"),
        UniqueConstraint("created_by_user_id", "name"),
    )

    created_by_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("admin_users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    positive_prompt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    negative_prompt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    settings: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False, default=dict)
    lock_version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)


class I2VWorkerDeployment(UuidPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "i2v_worker_deployments"
    __table_args__ = (
        CheckConstraint(
            "state IN ('stopped', 'provisioning', 'starting', 'ready', "
            "'busy', 'draining', 'failed')",
            name="known_state",
        ),
        UniqueConstraint("provider", "provider_group_id"),
        Index("ix_i2v_worker_deployments_state", "state", "updated_at"),
    )

    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    provider_group_id: Mapped[str | None] = mapped_column(String(255))
    provider_instance_id: Mapped[str | None] = mapped_column(String(255))
    state: Mapped[str] = mapped_column(String(20), nullable=False)
    gpu_class: Mapped[str] = mapped_column(String(100), nullable=False)
    worker_image_digest: Mapped[str] = mapped_column(String(255), nullable=False)
    current_job_id: Mapped[UUID | None] = mapped_column()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deployment_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSON_TYPE, nullable=False, default=dict
    )


class I2VJob(UuidPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "i2v_jobs"
    __table_args__ = (
        CheckConstraint(
            "state IN ('queued', 'claimed', 'running', 'cancel_requested', "
            "'succeeded', 'failed', 'cancelled')",
            name="known_state",
        ),
        CheckConstraint(
            "(state = 'queued' AND queue_position IS NOT NULL AND queue_position > 0) "
            "OR (state <> 'queued' AND queue_position IS NULL)",
            name="queue_position_matches_state",
        ),
        CheckConstraint("attempt_count >= 0", name="nonnegative_attempt_count"),
        CheckConstraint(
            "(lease_owner IS NULL AND lease_expires_at IS NULL) OR "
            "(lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name="lease_pair",
        ),
        CheckConstraint(
            "(state IN ('succeeded', 'failed', 'cancelled') AND completed_at IS NOT NULL) "
            "OR (state NOT IN ('succeeded', 'failed', 'cancelled') AND completed_at IS NULL)",
            name="terminal_completion",
        ),
        CheckConstraint(_lower_hex_check("request_sha256"), name="valid_request_sha256"),
        Index(
            "uq_i2v_jobs_queue_position",
            "queue_position",
            unique=True,
            postgresql_where=text("queue_position IS NOT NULL"),
            sqlite_where=text("queue_position IS NOT NULL"),
        ),
        Index("ix_i2v_jobs_queue", "state", "queue_position", "created_at", "id"),
        Index("ix_i2v_jobs_recent", "created_by_user_id", "created_at", "id"),
    )

    created_by_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("admin_users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    input_id: Mapped[UUID] = mapped_column(
        ForeignKey("i2v_inputs.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    preset_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("i2v_presets.id", ondelete="SET NULL"), index=True
    )
    positive_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    negative_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    input_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False)
    preset_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False)
    settings_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False)
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False)
    queue_position: Mapped[int | None] = mapped_column(BigInteger)
    attempt_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    lease_owner: Mapped[str | None] = mapped_column(String(255))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(100))
    last_error_detail: Mapped[str | None] = mapped_column(Text)


class I2VAttempt(UuidPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "i2v_attempts"
    __table_args__ = (
        CheckConstraint("attempt_no > 0", name="positive_attempt_no"),
        CheckConstraint(
            "state IN ('created', 'running', 'succeeded', 'failed', 'cancelled')",
            name="known_state",
        ),
        CheckConstraint(
            "state NOT IN ('succeeded', 'failed', 'cancelled') OR completed_at IS NOT NULL",
            name="terminal_completion",
        ),
        UniqueConstraint("job_id", "attempt_no"),
        UniqueConstraint("id", "job_id", name="uq_i2v_attempts_id_job_id"),
        UniqueConstraint(
            "worker_deployment_id", "provider_job_id", name="uq_i2v_attempts_provider_job"
        ),
        Index("ix_i2v_attempts_state", "state", "updated_at"),
    )

    job_id: Mapped[UUID] = mapped_column(
        ForeignKey("i2v_jobs.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    worker_deployment_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("i2v_worker_deployments.id", ondelete="RESTRICT"), index=True
    )
    attempt_no: Mapped[int] = mapped_column(BigInteger, nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False)
    worker_id: Mapped[str | None] = mapped_column(String(255))
    worker_image_digest: Mapped[str | None] = mapped_column(String(255))
    provider_job_id: Mapped[str | None] = mapped_column(String(255))
    request_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSON_TYPE, nullable=False, default=dict
    )
    response_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSON_TYPE, nullable=False, default=dict
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_detail: Mapped[str | None] = mapped_column(Text)


class I2VOutput(UuidPrimaryKeyMixin, Base):
    __tablename__ = "i2v_outputs"
    __table_args__ = (
        CheckConstraint(_lower_hex_check("sha256"), name="valid_sha256"),
        CheckConstraint("content_type LIKE 'video/%'", name="video_content"),
        CheckConstraint(
            "width > 0 AND height > 0 AND frame_count > 0 AND fps > 0 "
            "AND duration_ms > 0 AND byte_size > 0",
            name="positive_media_values",
        ),
        ForeignKeyConstraint(
            ["attempt_id", "job_id"],
            ["i2v_attempts.id", "i2v_attempts.job_id"],
            name="fk_i2v_outputs_attempt_job",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("attempt_id"),
        UniqueConstraint(
            "storage_backend",
            "storage_bucket",
            "object_key",
            "object_version_id",
            name="uq_i2v_outputs_object",
        ),
        Index(
            "uq_i2v_outputs_unversioned_object",
            "storage_backend",
            "storage_bucket",
            "object_key",
            unique=True,
            postgresql_where=text("object_version_id IS NULL"),
            sqlite_where=text("object_version_id IS NULL"),
        ),
        Index("ix_i2v_outputs_recent", "created_at", "id"),
    )

    job_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    attempt_id: Mapped[UUID] = mapped_column(nullable=False)
    storage_backend: Mapped[str] = mapped_column(String(50), nullable=False)
    storage_bucket: Mapped[str] = mapped_column(String(255), nullable=False)
    object_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    object_version_id: Mapped[str | None] = mapped_column(String(1024))
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    content_type: Mapped[str] = mapped_column(String(100), nullable=False)
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)
    frame_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    fps: Mapped[float] = mapped_column(nullable=False)
    duration_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    output_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSON_TYPE, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )


event.listen(
    I2VJob.__table__,
    "after_create",
    DDL(  # type: ignore[no-untyped-call]
        "CREATE TRIGGER i2v_jobs_immutable_request BEFORE UPDATE ON i2v_jobs "
        "BEGIN SELECT CASE WHEN OLD.id IS NOT NEW.id "
        "OR OLD.created_by_user_id IS NOT NEW.created_by_user_id "
        "OR OLD.input_id IS NOT NEW.input_id "
        "OR OLD.positive_prompt IS NOT NEW.positive_prompt "
        "OR OLD.negative_prompt IS NOT NEW.negative_prompt "
        "OR OLD.input_snapshot IS NOT NEW.input_snapshot "
        "OR OLD.preset_snapshot IS NOT NEW.preset_snapshot "
        "OR OLD.settings_snapshot IS NOT NEW.settings_snapshot "
        "OR OLD.request_sha256 IS NOT NEW.request_sha256 "
        "OR OLD.created_at IS NOT NEW.created_at "
        "THEN RAISE(ABORT, 'i2v job request snapshots are immutable') END; END"
    ).execute_if(dialect="sqlite"),
)
event.listen(
    I2VJob.__table__,
    "after_create",
    DDL(  # type: ignore[no-untyped-call]
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
    ).execute_if(dialect="postgresql"),
)
event.listen(
    I2VJob.__table__,
    "after_create",
    DDL(  # type: ignore[no-untyped-call]
        "CREATE TRIGGER i2v_jobs_immutable_request BEFORE UPDATE ON i2v_jobs "
        "FOR EACH ROW EXECUTE FUNCTION guard_i2v_job_request()"
    ).execute_if(dialect="postgresql"),
)


def _ddl(statement: str) -> Any:
    return DDL(statement)  # type: ignore[no-untyped-call]


event.listen(
    ReviewDecision.__table__,
    "after_create",
    _ddl(
        "CREATE TRIGGER review_decisions_reject_update "
        "BEFORE UPDATE ON review_decisions "
        "BEGIN "
        "SELECT RAISE(ABORT, 'review_decisions are append-only'); "
        "END"
    ).execute_if(dialect="sqlite"),
)
event.listen(
    ReviewDecision.__table__,
    "after_create",
    _ddl(
        "CREATE TRIGGER review_decisions_reject_delete "
        "BEFORE DELETE ON review_decisions "
        "BEGIN "
        "SELECT RAISE(ABORT, 'review_decisions are append-only'); "
        "END"
    ).execute_if(dialect="sqlite"),
)
event.listen(
    ReviewDecision.__table__,
    "after_create",
    _ddl(
        "CREATE OR REPLACE FUNCTION "
        "gen_automation_reject_review_decision_mutation() "
        "RETURNS trigger AS $$ "
        "BEGIN "
        "RAISE EXCEPTION 'review_decisions are append-only'; "
        "END; "
        "$$ LANGUAGE plpgsql"
    ).execute_if(dialect="postgresql"),
)
event.listen(
    ReviewDecision.__table__,
    "after_create",
    _ddl(
        "CREATE TRIGGER review_decisions_reject_mutation "
        "BEFORE UPDATE OR DELETE ON review_decisions "
        "FOR EACH ROW EXECUTE FUNCTION "
        "gen_automation_reject_review_decision_mutation()"
    ).execute_if(dialect="postgresql"),
)
event.listen(
    ReviewDecision.__table__,
    "before_drop",
    _ddl("DROP TRIGGER IF EXISTS review_decisions_reject_mutation ON review_decisions").execute_if(
        dialect="postgresql"
    ),
)
event.listen(
    ReviewDecision.__table__,
    "after_drop",
    _ddl("DROP FUNCTION IF EXISTS gen_automation_reject_review_decision_mutation()").execute_if(
        dialect="postgresql"
    ),
)

# Quality rows are mutable only while their scoring run is open. These hooks
# keep ``metadata.create_all()`` (used by local/test databases) at parity with
# Alembic revision 0008.
event.listen(
    AssetRanking.__table__,
    "after_create",
    _ddl(
        "CREATE TRIGGER scoring_runs_guard_completed_update "
        "BEFORE UPDATE ON scoring_runs "
        "WHEN OLD.state = 'completed' "
        "BEGIN "
        "SELECT RAISE(ABORT, 'completed scoring runs are immutable'); "
        "END"
    ).execute_if(dialect="sqlite"),
)
event.listen(
    AssetRanking.__table__,
    "after_create",
    _ddl(
        "CREATE TRIGGER scoring_runs_guard_completed_delete "
        "BEFORE DELETE ON scoring_runs "
        "WHEN OLD.state = 'completed' "
        "BEGIN "
        "SELECT RAISE(ABORT, 'completed scoring runs are immutable'); "
        "END"
    ).execute_if(dialect="sqlite"),
)
event.listen(
    AssetRanking.__table__,
    "after_create",
    _ddl(
        "CREATE TRIGGER scoring_runs_validate_completion "
        "BEFORE UPDATE ON scoring_runs "
        "WHEN OLD.state = 'running' AND NEW.state = 'completed' "
        "BEGIN "
        "SELECT CASE WHEN "
        "(SELECT count(*) FROM asset_rankings "
        "WHERE scoring_run_id = NEW.id) <> NEW.asset_count "
        "OR (SELECT min(rank) FROM asset_rankings "
        "WHERE scoring_run_id = NEW.id) <> 1 "
        "OR (SELECT max(rank) FROM asset_rankings "
        "WHERE scoring_run_id = NEW.id) <> NEW.asset_count "
        "OR EXISTS ("
        "SELECT 1 FROM asset_rankings AS ranking "
        "LEFT JOIN asset_scores AS score "
        "ON score.id = ranking.asset_score_id "
        "AND score.scoring_run_id = ranking.scoring_run_id "
        "AND score.asset_id = ranking.asset_id "
        "WHERE ranking.scoring_run_id = NEW.id "
        "AND (score.id IS NULL "
        "OR score.state NOT IN ('scored', 'flagged_blank', 'flagged_corrupt') "
        "OR score.completed_at IS NULL "
        "OR score.aggregate_score_micros IS NULL "
        "OR score.aggregate_score_micros <> ranking.aggregate_score_micros "
        "OR score.scorer_version <> NEW.scorer_version "
        "OR ranking.scorer_version <> NEW.scorer_version "
        "OR score.pillow_version <> NEW.pillow_version "
        "OR ranking.pillow_version <> NEW.pillow_version "
        "OR score.config_sha256 <> NEW.config_sha256 "
        "OR ranking.config_sha256 <> NEW.config_sha256)"
        ") "
        "THEN RAISE(ABORT, 'scoring run completion snapshot is invalid') END; "
        "END"
    ).execute_if(dialect="sqlite"),
)
event.listen(
    AssetRanking.__table__,
    "after_create",
    _ddl(
        "CREATE TRIGGER asset_scores_guard_frozen_update "
        "BEFORE UPDATE ON asset_scores "
        "WHEN OLD.state IN ('scored', 'flagged_blank', 'flagged_corrupt', 'dead_letter') "
        "OR EXISTS (SELECT 1 FROM scoring_runs "
        "WHERE id = OLD.scoring_run_id AND state = 'completed') "
        "BEGIN "
        "SELECT RAISE(ABORT, 'terminal asset scores are immutable'); "
        "END"
    ).execute_if(dialect="sqlite"),
)
event.listen(
    AssetRanking.__table__,
    "after_create",
    _ddl(
        "CREATE TRIGGER asset_scores_guard_frozen_delete "
        "BEFORE DELETE ON asset_scores "
        "WHEN OLD.state IN ('scored', 'flagged_blank', 'flagged_corrupt', 'dead_letter') "
        "OR EXISTS (SELECT 1 FROM scoring_runs "
        "WHERE id = OLD.scoring_run_id AND state = 'completed') "
        "BEGIN "
        "SELECT RAISE(ABORT, 'terminal asset scores are immutable'); "
        "END"
    ).execute_if(dialect="sqlite"),
)
event.listen(
    AssetRanking.__table__,
    "after_create",
    _ddl(
        "CREATE TRIGGER asset_scores_guard_late_insert "
        "BEFORE INSERT ON asset_scores "
        "WHEN EXISTS (SELECT 1 FROM scoring_runs "
        "WHERE id = NEW.scoring_run_id AND state = 'completed') "
        "BEGIN "
        "SELECT RAISE(ABORT, 'completed scoring runs reject new scores'); "
        "END"
    ).execute_if(dialect="sqlite"),
)
event.listen(
    AssetRanking.__table__,
    "after_create",
    _ddl(
        "CREATE TRIGGER asset_rankings_reject_update "
        "BEFORE UPDATE ON asset_rankings "
        "BEGIN "
        "SELECT RAISE(ABORT, 'asset rankings are append-only'); "
        "END"
    ).execute_if(dialect="sqlite"),
)
event.listen(
    AssetRanking.__table__,
    "after_create",
    _ddl(
        "CREATE TRIGGER asset_rankings_reject_delete "
        "BEFORE DELETE ON asset_rankings "
        "BEGIN "
        "SELECT RAISE(ABORT, 'asset rankings are append-only'); "
        "END"
    ).execute_if(dialect="sqlite"),
)
event.listen(
    AssetRanking.__table__,
    "after_create",
    _ddl(
        "CREATE TRIGGER asset_rankings_guard_late_insert "
        "BEFORE INSERT ON asset_rankings "
        "WHEN EXISTS (SELECT 1 FROM scoring_runs "
        "WHERE id = NEW.scoring_run_id AND state = 'completed') "
        "BEGIN "
        "SELECT RAISE(ABORT, 'completed scoring runs reject new rankings'); "
        "END"
    ).execute_if(dialect="sqlite"),
)

event.listen(
    AssetRanking.__table__,
    "after_create",
    _ddl(
        "CREATE OR REPLACE FUNCTION gen_automation_guard_scoring_run_mutation() "
        "RETURNS trigger AS $$ "
        "DECLARE ranking_count integer; minimum_rank integer; maximum_rank integer; "
        "BEGIN "
        "IF TG_OP = 'DELETE' THEN "
        "IF OLD.state = 'completed' THEN "
        "RAISE EXCEPTION 'completed scoring runs are immutable'; "
        "END IF; "
        "RETURN OLD; "
        "END IF; "
        "IF OLD.state = 'completed' THEN "
        "RAISE EXCEPTION 'completed scoring runs are immutable'; "
        "END IF; "
        "IF OLD.state = 'running' AND NEW.state = 'completed' THEN "
        "SELECT count(*), min(rank), max(rank) "
        "INTO ranking_count, minimum_rank, maximum_rank "
        "FROM asset_rankings WHERE scoring_run_id = NEW.id; "
        "IF ranking_count <> NEW.asset_count OR minimum_rank <> 1 "
        "OR maximum_rank <> NEW.asset_count OR EXISTS ("
        "SELECT 1 FROM asset_rankings AS ranking "
        "LEFT JOIN asset_scores AS score "
        "ON score.id = ranking.asset_score_id "
        "AND score.scoring_run_id = ranking.scoring_run_id "
        "AND score.asset_id = ranking.asset_id "
        "WHERE ranking.scoring_run_id = NEW.id "
        "AND (score.id IS NULL "
        "OR score.state NOT IN ('scored', 'flagged_blank', 'flagged_corrupt') "
        "OR score.completed_at IS NULL "
        "OR score.aggregate_score_micros IS NULL "
        "OR score.aggregate_score_micros <> ranking.aggregate_score_micros "
        "OR score.scorer_version <> NEW.scorer_version "
        "OR ranking.scorer_version <> NEW.scorer_version "
        "OR score.pillow_version <> NEW.pillow_version "
        "OR ranking.pillow_version <> NEW.pillow_version "
        "OR score.config_sha256 <> NEW.config_sha256 "
        "OR ranking.config_sha256 <> NEW.config_sha256)"
        ") THEN "
        "RAISE EXCEPTION 'scoring run completion snapshot is invalid'; "
        "END IF; "
        "END IF; "
        "RETURN NEW; "
        "END; "
        "$$ LANGUAGE plpgsql"
    ).execute_if(dialect="postgresql"),
)
event.listen(
    AssetRanking.__table__,
    "after_create",
    _ddl(
        "CREATE TRIGGER scoring_runs_guard_mutation "
        "BEFORE UPDATE OR DELETE ON scoring_runs "
        "FOR EACH ROW EXECUTE FUNCTION gen_automation_guard_scoring_run_mutation()"
    ).execute_if(dialect="postgresql"),
)
event.listen(
    AssetRanking.__table__,
    "after_create",
    _ddl(
        "CREATE OR REPLACE FUNCTION gen_automation_guard_asset_score_mutation() "
        "RETURNS trigger AS $$ "
        "BEGIN "
        "IF TG_OP = 'INSERT' THEN "
        "IF EXISTS (SELECT 1 FROM scoring_runs "
        "WHERE id = NEW.scoring_run_id AND state = 'completed') THEN "
        "RAISE EXCEPTION 'completed scoring runs reject new scores'; "
        "END IF; "
        "RETURN NEW; "
        "END IF; "
        "IF OLD.state IN ('scored', 'flagged_blank', 'flagged_corrupt', 'dead_letter') "
        "OR EXISTS (SELECT 1 FROM scoring_runs "
        "WHERE id = OLD.scoring_run_id AND state = 'completed') THEN "
        "RAISE EXCEPTION 'terminal asset scores are immutable'; "
        "END IF; "
        "IF TG_OP = 'DELETE' THEN RETURN OLD; END IF; "
        "RETURN NEW; "
        "END; "
        "$$ LANGUAGE plpgsql"
    ).execute_if(dialect="postgresql"),
)
event.listen(
    AssetRanking.__table__,
    "after_create",
    _ddl(
        "CREATE TRIGGER asset_scores_guard_mutation "
        "BEFORE INSERT OR UPDATE OR DELETE ON asset_scores "
        "FOR EACH ROW EXECUTE FUNCTION gen_automation_guard_asset_score_mutation()"
    ).execute_if(dialect="postgresql"),
)
event.listen(
    AssetRanking.__table__,
    "after_create",
    _ddl(
        "CREATE OR REPLACE FUNCTION gen_automation_guard_asset_ranking_mutation() "
        "RETURNS trigger AS $$ "
        "BEGIN "
        "IF TG_OP = 'INSERT' THEN "
        "IF EXISTS (SELECT 1 FROM scoring_runs "
        "WHERE id = NEW.scoring_run_id AND state = 'completed') THEN "
        "RAISE EXCEPTION 'completed scoring runs reject new rankings'; "
        "END IF; "
        "RETURN NEW; "
        "END IF; "
        "RAISE EXCEPTION 'asset rankings are append-only'; "
        "END; "
        "$$ LANGUAGE plpgsql"
    ).execute_if(dialect="postgresql"),
)
event.listen(
    AssetRanking.__table__,
    "after_create",
    _ddl(
        "CREATE TRIGGER asset_rankings_guard_mutation "
        "BEFORE INSERT OR UPDATE OR DELETE ON asset_rankings "
        "FOR EACH ROW EXECUTE FUNCTION gen_automation_guard_asset_ranking_mutation()"
    ).execute_if(dialect="postgresql"),
)

# A review task's identity is immutable. An owner may expand an open task's
# target up to its frozen ranking count. Completion may atomically shrink that
# ceiling and must exactly match the final accepted-image count.
event.listen(
    ReviewDecision.__table__,
    "after_create",
    _ddl(
        "CREATE TRIGGER review_tasks_guard_update "
        "BEFORE UPDATE ON review_tasks "
        "BEGIN "
        "SELECT CASE WHEN "
        "OLD.state <> 'open' "
        "OR NEW.lock_version <> OLD.lock_version + 1 "
        "OR OLD.id IS NOT NEW.id "
        "OR OLD.release_version_id IS NOT NEW.release_version_id "
        "OR OLD.release_version_no IS NOT NEW.release_version_no "
        "OR OLD.release_specification_sha256 IS NOT NEW.release_specification_sha256 "
        "OR OLD.scoring_run_id IS NOT NEW.scoring_run_id "
        "OR OLD.scoring_config_sha256 IS NOT NEW.scoring_config_sha256 "
        "OR OLD.scoring_input_manifest_sha256 IS NOT NEW.scoring_input_manifest_sha256 "
        "OR OLD.ranking_manifest_sha256 IS NOT NEW.ranking_manifest_sha256 "
        "OR OLD.ranked_asset_count IS NOT NEW.ranked_asset_count "
        "OR OLD.created_by_user_id IS NOT NEW.created_by_user_id "
        "OR OLD.created_at IS NOT NEW.created_at "
        "THEN RAISE(ABORT, 'review task identity is immutable') END; "
        "SELECT CASE WHEN "
        "OLD.desired_accepted_count IS NOT NEW.desired_accepted_count "
        "AND NEW.state IS 'open' AND (NEW.desired_accepted_count IS NULL "
        "OR NEW.desired_accepted_count < OLD.desired_accepted_count "
        "OR NEW.desired_accepted_count > OLD.ranked_asset_count) "
        "THEN RAISE(ABORT, 'open review target expansion is invalid') END; "
        "SELECT CASE WHEN OLD.desired_accepted_count IS NOT "
        "NEW.desired_accepted_count AND NEW.state NOT IN ('open', 'completed') "
        "THEN RAISE(ABORT, 'review task acceptance target is immutable') END; "
        "SELECT CASE WHEN NEW.state = 'completed' AND ("
        "NEW.desired_accepted_count IS NULL "
        "OR NEW.desired_accepted_count <= 0 "
        "OR NEW.desired_accepted_count > OLD.desired_accepted_count"
        ") THEN RAISE(ABORT, 'review task acceptance target shrink is invalid') END; "
        "SELECT CASE WHEN NEW.state = 'completed' AND ("
        "SELECT count(*) FROM review_decisions AS decision "
        "WHERE decision.review_task_id = OLD.id "
        "AND decision.decision = 'accept' "
        "AND NOT EXISTS ("
        "SELECT 1 FROM review_decisions AS newer "
        "WHERE newer.review_task_id = decision.review_task_id "
        "AND newer.asset_id = decision.asset_id "
        "AND newer.revision > decision.revision"
        ")"
        ") <> NEW.desired_accepted_count "
        "THEN RAISE(ABORT, 'review task acceptance target is not satisfied') END; "
        "END"
    ).execute_if(dialect="sqlite"),
)
event.listen(
    ReviewDecision.__table__,
    "after_create",
    _ddl(
        "CREATE TRIGGER review_tasks_reject_delete "
        "BEFORE DELETE ON review_tasks "
        "BEGIN "
        "SELECT RAISE(ABORT, 'review tasks cannot be deleted'); "
        "END"
    ).execute_if(dialect="sqlite"),
)
event.listen(
    ReviewDecision.__table__,
    "after_create",
    _ddl(
        "CREATE OR REPLACE FUNCTION gen_automation_guard_review_task_mutation() "
        "RETURNS trigger AS $$ "
        "DECLARE accepted_count integer; "
        "BEGIN "
        "IF TG_OP = 'DELETE' THEN "
        "RAISE EXCEPTION 'review tasks cannot be deleted'; "
        "END IF; "
        "IF OLD.state <> 'open' OR NEW.lock_version <> OLD.lock_version + 1 "
        "OR OLD.id IS DISTINCT FROM NEW.id "
        "OR OLD.release_version_id IS DISTINCT FROM NEW.release_version_id "
        "OR OLD.release_version_no IS DISTINCT FROM NEW.release_version_no "
        "OR OLD.release_specification_sha256 IS DISTINCT FROM "
        "NEW.release_specification_sha256 "
        "OR OLD.scoring_run_id IS DISTINCT FROM NEW.scoring_run_id "
        "OR OLD.scoring_config_sha256 IS DISTINCT FROM NEW.scoring_config_sha256 "
        "OR OLD.scoring_input_manifest_sha256 IS DISTINCT FROM "
        "NEW.scoring_input_manifest_sha256 "
        "OR OLD.ranking_manifest_sha256 IS DISTINCT FROM NEW.ranking_manifest_sha256 "
        "OR OLD.ranked_asset_count IS DISTINCT FROM NEW.ranked_asset_count "
        "OR OLD.created_by_user_id IS DISTINCT FROM NEW.created_by_user_id "
        "OR OLD.created_at IS DISTINCT FROM NEW.created_at THEN "
        "RAISE EXCEPTION 'review task identity is immutable'; "
        "END IF; "
        "IF OLD.desired_accepted_count IS DISTINCT FROM NEW.desired_accepted_count "
        "AND NEW.state = 'open' AND (NEW.desired_accepted_count IS NULL "
        "OR NEW.desired_accepted_count < OLD.desired_accepted_count "
        "OR NEW.desired_accepted_count > OLD.ranked_asset_count) THEN "
        "RAISE EXCEPTION 'open review target expansion is invalid'; "
        "END IF; "
        "IF OLD.desired_accepted_count IS DISTINCT FROM NEW.desired_accepted_count "
        "AND NEW.state NOT IN ('open', 'completed') THEN "
        "RAISE EXCEPTION 'review task acceptance target is immutable'; "
        "END IF; "
        "IF NEW.state = 'completed' AND (NEW.desired_accepted_count IS NULL "
        "OR NEW.desired_accepted_count <= 0 "
        "OR NEW.desired_accepted_count > OLD.desired_accepted_count) THEN "
        "RAISE EXCEPTION 'review task acceptance target shrink is invalid'; "
        "END IF; "
        "IF NEW.state = 'completed' THEN "
        "SELECT count(*) INTO accepted_count "
        "FROM review_decisions AS decision "
        "WHERE decision.review_task_id = OLD.id "
        "AND decision.decision = 'accept' "
        "AND NOT EXISTS ("
        "SELECT 1 FROM review_decisions AS newer "
        "WHERE newer.review_task_id = decision.review_task_id "
        "AND newer.asset_id = decision.asset_id "
        "AND newer.revision > decision.revision"
        "); "
        "IF accepted_count <> NEW.desired_accepted_count THEN "
        "RAISE EXCEPTION 'review task acceptance target is not satisfied'; "
        "END IF; "
        "END IF; "
        "RETURN NEW; "
        "END; "
        "$$ LANGUAGE plpgsql"
    ).execute_if(dialect="postgresql"),
)
event.listen(
    ReviewDecision.__table__,
    "after_create",
    _ddl(
        "CREATE TRIGGER review_tasks_guard_mutation "
        "BEFORE UPDATE OR DELETE ON review_tasks "
        "FOR EACH ROW EXECUTE FUNCTION gen_automation_guard_review_task_mutation()"
    ).execute_if(dialect="postgresql"),
)

# Revision 0009 freezes accepted raw masters before review completion and keeps
# derivative planning/output records immutable. Attach these guards to the last
# dependent table so ``metadata.create_all()`` has created every referenced
# relation before the trigger bodies are installed.
for _statement in (
    "CREATE TRIGGER review_decisions_guard_late_insert "
    "BEFORE INSERT ON review_decisions "
    "WHEN NOT EXISTS (SELECT 1 FROM review_tasks "
    "WHERE id = NEW.review_task_id "
    "AND scoring_run_id = NEW.scoring_run_id AND state = 'open') "
    "BEGIN SELECT RAISE(ABORT, 'terminal review tasks reject new decisions'); END",
    "CREATE TRIGGER release_selections_guard_insert "
    "BEFORE INSERT ON release_selections BEGIN "
    "SELECT CASE WHEN NOT EXISTS ("
    "SELECT 1 FROM review_tasks AS task "
    "JOIN release_versions AS version ON version.id = task.release_version_id "
    "JOIN review_decisions AS decision "
    "ON decision.id = NEW.review_decision_id "
    "AND decision.review_task_id = task.id "
    "AND decision.asset_id = NEW.asset_id "
    "AND decision.revision = NEW.decision_revision "
    "AND decision.scoring_run_id = task.scoring_run_id "
    "JOIN asset_rankings AS ranking "
    "ON ranking.scoring_run_id = task.scoring_run_id "
    "AND ranking.asset_id = NEW.asset_id "
    "JOIN assets AS asset ON asset.id = NEW.asset_id "
    "WHERE task.id = NEW.review_task_id AND task.state = 'open' "
    "AND task.scoring_run_id = NEW.scoring_run_id "
    "AND task.release_version_id = NEW.release_version_id "
    "AND task.ranking_manifest_sha256 = NEW.ranking_manifest_sha256 "
    "AND decision.decision = 'accept' "
    "AND NOT EXISTS (SELECT 1 FROM review_decisions AS newer "
    "WHERE newer.review_task_id = decision.review_task_id "
    "AND newer.asset_id = decision.asset_id "
    "AND newer.revision > decision.revision) "
    "AND ranking.rank = NEW.ranking_rank "
    "AND asset.release_id = version.release_id "
    "AND asset.kind = 'raw_master' AND asset.state = 'available' "
    "AND asset.storage_backend = NEW.source_storage_backend "
    "AND asset.storage_bucket = NEW.source_storage_bucket "
    "AND asset.object_key = NEW.source_object_key "
    "AND asset.object_version_id = NEW.source_object_version_id "
    "AND asset.sha256 = NEW.source_sha256 "
    "AND asset.content_type = NEW.source_content_type "
    "AND asset.image_format = NEW.source_image_format "
    "AND asset.width = NEW.source_width AND asset.height = NEW.source_height "
    "AND asset.byte_size = NEW.source_byte_size "
    "AND asset.available_at = NEW.source_available_at"
    ") THEN RAISE(ABORT, 'release selection snapshot is invalid') END; END",
    "CREATE TRIGGER release_selections_reject_update "
    "BEFORE UPDATE ON release_selections "
    "BEGIN SELECT RAISE(ABORT, 'release selections are immutable'); END",
    "CREATE TRIGGER release_selections_reject_delete "
    "BEFORE DELETE ON release_selections "
    "BEGIN SELECT RAISE(ABORT, 'release selections are immutable'); END",
    "CREATE TRIGGER review_tasks_validate_selection_completion "
    "BEFORE UPDATE ON review_tasks "
    "WHEN OLD.state = 'open' AND NEW.state = 'completed' BEGIN "
    "SELECT CASE WHEN "
    "(SELECT count(*) FROM release_selections "
    "WHERE review_task_id = OLD.id) <> NEW.desired_accepted_count "
    "OR (SELECT min(display_order) FROM release_selections "
    "WHERE review_task_id = OLD.id) <> 1 "
    "OR (SELECT max(display_order) FROM release_selections "
    "WHERE review_task_id = OLD.id) <> NEW.desired_accepted_count "
    "OR NOT EXISTS (SELECT 1 FROM release_versions AS current_version "
    "JOIN releases AS current_release "
    "ON current_release.id = current_version.release_id "
    "WHERE current_version.id = OLD.release_version_id "
    "AND current_release.current_version_no = current_version.version_no "
    "AND current_release.phase = 'reviewing') "
    "OR EXISTS (SELECT 1 FROM release_selections AS selection "
    "JOIN review_decisions AS decision "
    "ON decision.id = selection.review_decision_id "
    "JOIN asset_rankings AS ranking "
    "ON ranking.scoring_run_id = selection.scoring_run_id "
    "AND ranking.asset_id = selection.asset_id "
    "JOIN release_versions AS version "
    "ON version.id = selection.release_version_id "
    "JOIN assets AS asset ON asset.id = selection.asset_id "
    "WHERE selection.review_task_id = OLD.id AND ("
    "selection.scoring_run_id IS NOT OLD.scoring_run_id "
    "OR selection.release_version_id IS NOT OLD.release_version_id "
    "OR selection.ranking_manifest_sha256 IS NOT OLD.ranking_manifest_sha256 "
    "OR selection.frozen_at IS NOT NEW.completed_at "
    "OR decision.review_task_id IS NOT OLD.id "
    "OR decision.asset_id IS NOT selection.asset_id "
    "OR decision.revision IS NOT selection.decision_revision "
    "OR decision.decision <> 'accept' "
    "OR EXISTS (SELECT 1 FROM review_decisions AS newer "
    "WHERE newer.review_task_id = decision.review_task_id "
    "AND newer.asset_id = decision.asset_id "
    "AND newer.revision > decision.revision) "
    "OR ranking.rank IS NOT selection.ranking_rank "
    "OR asset.release_id IS NOT version.release_id "
    "OR asset.kind <> 'raw_master' OR asset.state <> 'available' "
    "OR asset.storage_backend IS NOT selection.source_storage_backend "
    "OR asset.storage_bucket IS NOT selection.source_storage_bucket "
    "OR asset.object_key IS NOT selection.source_object_key "
    "OR asset.object_version_id IS NOT selection.source_object_version_id "
    "OR asset.sha256 IS NOT selection.source_sha256 "
    "OR asset.content_type IS NOT selection.source_content_type "
    "OR asset.image_format IS NOT selection.source_image_format "
    "OR asset.width IS NOT selection.source_width "
    "OR asset.height IS NOT selection.source_height "
    "OR asset.byte_size IS NOT selection.source_byte_size "
    "OR asset.available_at IS NOT selection.source_available_at)) "
    "OR EXISTS (SELECT 1 FROM review_decisions AS decision "
    "WHERE decision.review_task_id = OLD.id "
    "AND decision.decision = 'accept' "
    "AND NOT EXISTS (SELECT 1 FROM review_decisions AS newer "
    "WHERE newer.review_task_id = decision.review_task_id "
    "AND newer.asset_id = decision.asset_id "
    "AND newer.revision > decision.revision) "
    "AND NOT EXISTS (SELECT 1 FROM release_selections AS selection "
    "WHERE selection.review_task_id = OLD.id "
    "AND selection.review_decision_id = decision.id)) "
    "OR EXISTS (SELECT 1 FROM release_selections AS earlier "
    "JOIN release_selections AS later "
    "ON later.review_task_id = earlier.review_task_id "
    "WHERE earlier.review_task_id = OLD.id "
    "AND earlier.ranking_rank < later.ranking_rank "
    "AND earlier.display_order > later.display_order) "
    "THEN RAISE(ABORT, 'review completion selection snapshot is invalid') END; END",
    "CREATE TRIGGER review_tasks_promote_release_after_completion "
    "AFTER UPDATE ON review_tasks "
    "WHEN OLD.state = 'open' AND NEW.state = 'completed' BEGIN "
    "UPDATE releases SET phase = 'approved', lock_version = lock_version + 1 "
    "WHERE phase = 'reviewing' AND id = ("
    "SELECT version.release_id FROM release_versions AS version "
    "WHERE version.id = NEW.release_version_id "
    "AND version.version_no = releases.current_version_no); "
    "SELECT CASE WHEN changes() <> 1 "
    "THEN RAISE(ABORT, 'review release approval compare-and-swap failed') END; END",
    "CREATE TRIGGER derivative_recipes_guard_insert "
    "BEFORE INSERT ON derivative_recipes "
    "WHEN NEW.watermark_asset_id IS NOT NULL BEGIN "
    "SELECT CASE WHEN NOT EXISTS ("
    "SELECT 1 FROM assets AS asset "
    "WHERE asset.id = NEW.watermark_asset_id "
    "AND asset.kind = 'derivative' "
    "AND asset.state = 'available' "
    "AND asset.content_type = 'image/png' "
    "AND asset.image_format = 'PNG' "
    "AND json_extract(asset.metadata, '$.purpose') = 'watermark' "
    "AND asset.storage_backend = NEW.watermark_storage_backend "
    "AND asset.storage_bucket = NEW.watermark_storage_bucket "
    "AND asset.object_key = NEW.watermark_object_key "
    "AND asset.object_version_id = NEW.watermark_object_version_id "
    "AND asset.sha256 = NEW.watermark_sha256 "
    "AND asset.content_type = NEW.watermark_content_type "
    "AND asset.image_format = NEW.watermark_image_format "
    "AND asset.width = NEW.watermark_width "
    "AND asset.height = NEW.watermark_height "
    "AND asset.byte_size = NEW.watermark_byte_size"
    ") THEN RAISE(ABORT, 'derivative recipe watermark snapshot is invalid') END; END",
    "CREATE TRIGGER derivative_recipes_reject_update "
    "BEFORE UPDATE ON derivative_recipes "
    "BEGIN SELECT RAISE(ABORT, 'derivative recipes are immutable'); END",
    "CREATE TRIGGER derivative_recipes_reject_delete "
    "BEFORE DELETE ON derivative_recipes "
    "BEGIN SELECT RAISE(ABORT, 'derivative recipes are immutable'); END",
    "CREATE TRIGGER derivative_jobs_guard_insert "
    "BEFORE INSERT ON derivative_jobs BEGIN "
    "SELECT CASE WHEN NOT EXISTS ("
    "SELECT 1 FROM release_selections AS selection "
    "JOIN derivative_recipes AS recipe "
    "ON recipe.id = NEW.derivative_recipe_id "
    "JOIN release_versions AS version "
    "ON version.id = NEW.release_version_id "
    "JOIN releases AS release ON release.id = version.release_id "
    "WHERE selection.id = NEW.release_selection_id "
    "AND selection.release_version_id = NEW.release_version_id "
    "AND recipe.release_version_id = NEW.release_version_id "
    "AND recipe.expected_output_count = NEW.expected_output_count "
    "AND release.current_version_no = version.version_no "
    "AND ((NEW.gates_release = 1 AND release.phase = 'rendering') OR "
    "(NEW.gates_release = 0 AND release.phase IN "
    "('rendering', 'ready_to_publish', 'publishing', 'published') "
    "AND EXISTS (SELECT 1 FROM x_teaser_revision_heads AS head WHERE "
    "head.review_task_id = selection.review_task_id "
    "AND head.release_version_id = NEW.release_version_id "
    "AND (head.active_revision_id IS NOT NULL OR release.phase <> 'rendering') "
    "AND head.pending_revision_id = NEW.x_teaser_revision_id)))"
    ") THEN RAISE(ABORT, 'derivative job release snapshot is invalid') END; END",
    "CREATE TRIGGER derivative_jobs_guard_update "
    "BEFORE UPDATE ON derivative_jobs BEGIN "
    "SELECT CASE WHEN OLD.state IN ('succeeded', 'cancelled') "
    "OR NEW.lock_version <> OLD.lock_version + 1 "
    "OR OLD.id IS NOT NEW.id "
    "OR OLD.release_selection_id IS NOT NEW.release_selection_id "
    "OR OLD.derivative_recipe_id IS NOT NEW.derivative_recipe_id "
    "OR OLD.x_teaser_revision_id IS NOT NEW.x_teaser_revision_id "
    "OR OLD.gates_release IS NOT NEW.gates_release "
    "OR OLD.release_version_id IS NOT NEW.release_version_id "
    "OR OLD.logical_key IS NOT NEW.logical_key "
    "OR OLD.request_payload IS NOT NEW.request_payload "
    "OR OLD.request_sha256 IS NOT NEW.request_sha256 "
    "OR OLD.expected_output_count IS NOT NEW.expected_output_count "
    "OR OLD.priority IS NOT NEW.priority "
    "OR (OLD.max_attempts IS NOT NEW.max_attempts AND NOT ("
    "OLD.state = 'failed' AND NEW.state = 'retry_wait' "
    "AND NEW.max_attempts > OLD.max_attempts "
    "AND NEW.max_attempts <= 10 "
    "AND NEW.max_attempts >= OLD.attempt_count + 1)) "
    "OR OLD.available_at IS NOT NEW.available_at "
    "OR OLD.requested_at IS NOT NEW.requested_at "
    "THEN RAISE(ABORT, 'derivative job identity is immutable') END; "
    "SELECT CASE WHEN NOT ("
    "(OLD.state IN ('requested', 'retry_wait') AND NEW.state IN ('claimed', 'cancelled')) "
    "OR (OLD.state = 'claimed' "
    "AND NEW.state IN ('claimed', 'processing', 'retry_wait', 'failed', 'cancelled')) "
    "OR (OLD.state = 'processing' "
    "AND NEW.state IN ('claimed', 'retry_wait', 'succeeded', 'failed', 'cancelled')) "
    "OR (OLD.state = 'failed' AND NEW.state = 'retry_wait')"
    ") THEN RAISE(ABORT, 'derivative job state transition is invalid') END; "
    "SELECT CASE WHEN NEW.state = 'claimed' "
    "AND NEW.attempt_count <> OLD.attempt_count + 1 "
    "THEN RAISE(ABORT, 'derivative job claim attempt is invalid') END; "
    "SELECT CASE WHEN NEW.state <> 'claimed' "
    "AND NEW.attempt_count <> OLD.attempt_count "
    "THEN RAISE(ABORT, 'derivative job attempt count is immutable') END; "
    "SELECT CASE WHEN OLD.state = 'failed' AND ("
    "NEW.state <> 'retry_wait' "
    "OR OLD.last_error_code IS 'output_object_conflict' "
    "OR NEW.completed_at IS NOT NULL "
    "OR NEW.last_error_code IS NOT NULL OR NEW.last_error_detail IS NOT NULL "
    "OR NEW.lease_owner IS NOT NULL OR NEW.lease_expires_at IS NOT NULL "
    "OR NEW.retry_at IS NULL "
    "OR NEW.max_attempts <= OLD.max_attempts "
    "OR NEW.max_attempts > 10 "
    "OR NEW.max_attempts < OLD.attempt_count + 1 "
    "OR NOT EXISTS (SELECT 1 FROM derivative_recipes AS retry_recipe "
    "JOIN release_versions AS retry_version "
    "ON retry_version.id = OLD.release_version_id "
    "JOIN releases AS retry_release ON retry_release.id = retry_version.release_id "
    "WHERE retry_recipe.id = OLD.derivative_recipe_id "
    "AND retry_recipe.release_version_id = OLD.release_version_id "
    "AND json_array_length(retry_recipe.output_targets) = 1 "
    "AND json_extract(retry_recipe.output_targets, '$[0]') = 'full' "
    "AND retry_release.current_version_no = retry_version.version_no "
    "AND retry_release.phase = 'rendering')) "
    "THEN RAISE(ABORT, 'failed derivative job rearm is invalid') END; "
    "SELECT CASE WHEN OLD.state IN ('claimed', 'processing') "
    "AND NEW.state = 'claimed' "
    "AND (OLD.lease_expires_at IS NULL "
    "OR NEW.claimed_at < OLD.lease_expires_at) "
    "THEN RAISE(ABORT, 'active derivative job lease cannot be stolen') END; "
    "SELECT CASE WHEN NEW.state = 'succeeded' AND ("
    "SELECT count(*) FROM derivative_outputs "
    "WHERE derivative_job_id = OLD.id"
    ") <> OLD.expected_output_count "
    "THEN RAISE(ABORT, 'derivative job outputs are incomplete') END; "
    "SELECT CASE WHEN NEW.state = 'succeeded' AND NOT EXISTS ("
    "SELECT 1 FROM release_versions AS version "
    "JOIN releases AS release ON release.id = version.release_id "
    "JOIN release_selections AS selection ON selection.id = OLD.release_selection_id "
    "WHERE version.id = OLD.release_version_id "
    "AND release.current_version_no = version.version_no "
    "AND ((NEW.gates_release = 1 AND release.phase = 'rendering') OR "
    "(NEW.gates_release = 0 AND release.phase IN "
    "('rendering', 'ready_to_publish', 'publishing', 'published') "
    "AND EXISTS (SELECT 1 FROM x_teaser_revision_heads AS head WHERE "
    "head.review_task_id = selection.review_task_id "
    "AND head.release_version_id = NEW.release_version_id "
    "AND (head.active_revision_id IS NOT NULL OR release.phase <> 'rendering') "
    "AND head.pending_revision_id = NEW.x_teaser_revision_id)))) "
    "THEN RAISE(ABORT, 'derivative job release phase is invalid') END; END",
    "CREATE TRIGGER derivative_jobs_reject_delete "
    "BEFORE DELETE ON derivative_jobs "
    "BEGIN SELECT RAISE(ABORT, 'derivative jobs cannot be deleted'); END",
    "CREATE TRIGGER derivative_jobs_promote_release_after_success "
    "AFTER UPDATE ON derivative_jobs "
    "WHEN OLD.state <> 'succeeded' AND NEW.state = 'succeeded' "
    "AND NEW.gates_release = 1 "
    "AND NOT EXISTS (SELECT 1 FROM derivative_jobs AS pending "
    "WHERE pending.release_version_id = NEW.release_version_id "
    "AND pending.gates_release = 1 "
    "AND pending.state <> 'succeeded' "
    "AND ((pending.x_teaser_revision_id IS NULL AND ("
    "EXISTS (SELECT 1 FROM derivative_recipes AS pending_recipe "
    "WHERE pending_recipe.id = pending.derivative_recipe_id "
    "AND EXISTS (SELECT 1 FROM json_each(pending_recipe.output_targets) "
    "AS pending_target WHERE pending_target.value = 'full')) "
    "OR NOT EXISTS (SELECT 1 FROM release_selections AS pending_selection "
    "JOIN x_teaser_revision_heads AS pending_head "
    "ON pending_head.review_task_id = pending_selection.review_task_id "
    "WHERE pending_selection.id = pending.release_selection_id "
    "AND pending_head.release_version_id = pending.release_version_id))) "
    "OR (pending.x_teaser_revision_id IS NOT NULL AND EXISTS ("
    "SELECT 1 FROM release_selections AS pending_selection "
    "JOIN x_teaser_revision_heads AS pending_head "
    "ON pending_head.review_task_id = pending_selection.review_task_id "
    "WHERE pending_selection.id = pending.release_selection_id "
    "AND pending_head.release_version_id = pending.release_version_id "
    "AND (pending_head.active_revision_id = pending.x_teaser_revision_id "
    "OR pending_head.pending_revision_id = pending.x_teaser_revision_id))))) BEGIN "
    "UPDATE releases SET phase = 'ready_to_publish', "
    "lock_version = lock_version + 1 "
    "WHERE phase = 'rendering' AND id = ("
    "SELECT version.release_id FROM release_versions AS version "
    "WHERE version.id = NEW.release_version_id "
    "AND version.version_no = releases.current_version_no); "
    "SELECT CASE WHEN changes() <> 1 "
    "THEN RAISE(ABORT, 'release readiness compare-and-swap failed') END; END",
    "CREATE TRIGGER derivative_outputs_guard_insert "
    "BEFORE INSERT ON derivative_outputs BEGIN "
    "SELECT CASE WHEN NOT EXISTS ("
    "SELECT 1 FROM derivative_jobs AS job "
    "JOIN derivative_recipes AS recipe "
    "ON recipe.id = job.derivative_recipe_id "
    "JOIN release_selections AS selection "
    "ON selection.id = job.release_selection_id "
    "JOIN release_versions AS version "
    "ON version.id = job.release_version_id "
    "JOIN assets AS asset ON asset.id = NEW.asset_id "
    "JOIN asset_lineage AS lineage ON lineage.id = NEW.asset_lineage_id "
    "WHERE job.id = NEW.derivative_job_id "
    "AND job.release_selection_id = NEW.release_selection_id "
    "AND job.derivative_recipe_id = NEW.derivative_recipe_id "
    "AND job.state = 'processing' "
    "AND job.lease_expires_at > NEW.recorded_at "
    "AND EXISTS (SELECT 1 FROM json_each(recipe.output_targets) "
    "WHERE value = NEW.target) "
    "AND selection.asset_id = NEW.source_asset_id "
    "AND asset.release_id = version.release_id "
    "AND asset.kind = 'derivative' AND asset.state = 'available' "
    "AND asset.storage_backend = NEW.asset_storage_backend "
    "AND asset.storage_bucket = NEW.asset_storage_bucket "
    "AND asset.object_key = NEW.asset_object_key "
    "AND asset.object_version_id = NEW.asset_object_version_id "
    "AND asset.sha256 = NEW.asset_sha256 "
    "AND asset.content_type = NEW.asset_content_type "
    "AND asset.image_format = NEW.asset_image_format "
    "AND asset.width = NEW.asset_width AND asset.height = NEW.asset_height "
    "AND asset.byte_size = NEW.asset_byte_size "
    "AND lineage.parent_asset_id = selection.asset_id "
    "AND lineage.child_asset_id = asset.id "
    "AND lineage.relation = NEW.lineage_relation "
    "AND lineage.relation = 'derivative' "
    "AND lineage.recipe_version = NEW.lineage_recipe_version "
    "AND lineage.recipe_version = recipe.config_sha256"
    ") THEN RAISE(ABORT, 'derivative output snapshot is invalid') END; END",
    "CREATE TRIGGER derivative_outputs_reject_update "
    "BEFORE UPDATE ON derivative_outputs "
    "BEGIN SELECT RAISE(ABORT, 'derivative outputs are append-only'); END",
    "CREATE TRIGGER derivative_outputs_reject_delete "
    "BEFORE DELETE ON derivative_outputs "
    "BEGIN SELECT RAISE(ABORT, 'derivative outputs are append-only'); END",
    "CREATE TRIGGER asset_lineage_guard_derivative_update "
    "BEFORE UPDATE ON asset_lineage "
    "WHEN EXISTS (SELECT 1 FROM derivative_outputs "
    "WHERE asset_lineage_id = OLD.id) "
    "BEGIN SELECT RAISE(ABORT, 'recorded derivative lineage is immutable'); END",
    "CREATE TRIGGER asset_lineage_guard_derivative_delete "
    "BEFORE DELETE ON asset_lineage "
    "WHEN EXISTS (SELECT 1 FROM derivative_outputs "
    "WHERE asset_lineage_id = OLD.id) "
    "BEGIN SELECT RAISE(ABORT, 'recorded derivative lineage is immutable'); END",
):
    event.listen(
        DerivativeOutput.__table__,
        "after_create",
        _ddl(_statement).execute_if(dialect="sqlite"),
    )


for _table, _statements in (
    (
        XTeaserRevision.__table__,
        (
            "CREATE TRIGGER x_teaser_revisions_reject_update BEFORE UPDATE ON "
            "x_teaser_revisions BEGIN SELECT RAISE(ABORT, 'X teaser revisions are "
            "append-only'); END",
            "CREATE TRIGGER x_teaser_revisions_reject_delete BEFORE DELETE ON "
            "x_teaser_revisions BEGIN SELECT RAISE(ABORT, 'X teaser revisions are "
            "append-only'); END",
        ),
    ),
    (
        XTeaserRevisionMember.__table__,
        (
            "CREATE TRIGGER x_teaser_revision_members_guard_insert BEFORE INSERT ON "
            "x_teaser_revision_members BEGIN SELECT CASE WHEN NOT EXISTS (SELECT 1 "
            "FROM x_teaser_revisions AS revision JOIN release_selections AS selection "
            "ON selection.id = NEW.release_selection_id JOIN derivative_recipes AS recipe "
            "ON recipe.id = NEW.derivative_recipe_id WHERE revision.id = NEW.revision_id "
            "AND revision.review_task_id = NEW.review_task_id AND "
            "revision.release_version_id = NEW.release_version_id AND "
            "selection.review_task_id = NEW.review_task_id AND "
            "selection.release_version_id = NEW.release_version_id AND "
            "selection.asset_id = NEW.source_asset_id AND recipe.release_version_id = "
            "NEW.release_version_id AND recipe.watermark_asset_id = "
            "revision.watermark_asset_id AND json_extract(recipe.configuration, "
            "'$.watermark.position') = NEW.watermark_position AND ((NEW.derivative_job_id "
            "IS NOT NULL AND EXISTS (SELECT 1 FROM derivative_jobs AS job WHERE job.id = "
            "NEW.derivative_job_id AND job.release_selection_id = NEW.release_selection_id "
            "AND job.derivative_recipe_id = NEW.derivative_recipe_id AND "
            "job.x_teaser_revision_id = NEW.revision_id)) OR (NEW.derivative_output_id IS "
            "NOT NULL AND EXISTS (SELECT 1 FROM derivative_outputs AS output WHERE "
            "output.id = NEW.derivative_output_id AND output.release_selection_id = "
            "NEW.release_selection_id AND output.derivative_recipe_id = "
            "NEW.derivative_recipe_id AND output.target = 'x_teaser')))) THEN "
            "RAISE(ABORT, 'X teaser revision member is invalid') END; END",
            "CREATE TRIGGER x_teaser_revision_members_reject_update BEFORE UPDATE ON "
            "x_teaser_revision_members BEGIN SELECT RAISE(ABORT, 'X teaser revision "
            "members are append-only'); END",
            "CREATE TRIGGER x_teaser_revision_members_reject_delete BEFORE DELETE ON "
            "x_teaser_revision_members BEGIN SELECT RAISE(ABORT, 'X teaser revision "
            "members are append-only'); END",
        ),
    ),
    (
        XTeaserRevisionHead.__table__,
        (
            "CREATE TRIGGER x_teaser_revision_heads_guard_update BEFORE UPDATE ON "
            "x_teaser_revision_heads BEGIN SELECT CASE WHEN OLD.id IS NOT NEW.id OR "
            "OLD.review_task_id IS NOT NEW.review_task_id OR OLD.release_version_id IS "
            "NOT NEW.release_version_id OR NEW.lock_version <> OLD.lock_version + 1 OR "
            "NOT ((OLD.pending_revision_id IS NULL AND NEW.pending_revision_id IS NOT NULL "
            "AND NEW.active_revision_id IS OLD.active_revision_id) OR "
            "(OLD.pending_revision_id IS NOT NULL AND NEW.pending_revision_id IS NULL AND "
            "(NEW.active_revision_id IS OLD.pending_revision_id OR "
            "NEW.active_revision_id IS OLD.active_revision_id))) THEN RAISE(ABORT, "
            "'X teaser revision head transition is invalid') END; END",
            "CREATE TRIGGER x_teaser_revision_heads_reject_delete BEFORE DELETE ON "
            "x_teaser_revision_heads BEGIN SELECT RAISE(ABORT, 'X teaser revision head "
            "cannot be deleted'); END",
        ),
    ),
):
    for _statement in _statements:
        event.listen(
            _table,
            "after_create",
            _ddl(_statement).execute_if(dialect="sqlite"),
        )


for _table, _function, _trigger in (
    (
        XTeaserRevision.__table__,
        "CREATE OR REPLACE FUNCTION gen_automation_guard_x_teaser_revision_mutation() "
        "RETURNS trigger AS $$ BEGIN IF TG_OP <> 'INSERT' THEN RAISE EXCEPTION "
        "'X teaser revisions are append-only'; END IF; RETURN NEW; END; $$ LANGUAGE plpgsql",
        "CREATE TRIGGER x_teaser_revisions_guard BEFORE UPDATE OR DELETE ON "
        "x_teaser_revisions FOR EACH ROW EXECUTE FUNCTION "
        "gen_automation_guard_x_teaser_revision_mutation()",
    ),
    (
        XTeaserRevisionMember.__table__,
        "CREATE OR REPLACE FUNCTION gen_automation_guard_x_teaser_revision_member_mutation() "
        "RETURNS trigger AS $$ BEGIN IF TG_OP <> 'INSERT' THEN RAISE EXCEPTION "
        "'X teaser revision members are append-only'; END IF; IF NOT EXISTS (SELECT 1 "
        "FROM x_teaser_revisions AS revision JOIN release_selections AS selection ON "
        "selection.id = NEW.release_selection_id JOIN derivative_recipes AS recipe ON "
        "recipe.id = NEW.derivative_recipe_id WHERE revision.id = NEW.revision_id AND "
        "revision.review_task_id = NEW.review_task_id AND revision.release_version_id = "
        "NEW.release_version_id AND selection.review_task_id = NEW.review_task_id AND "
        "selection.release_version_id = NEW.release_version_id AND selection.asset_id = "
        "NEW.source_asset_id AND recipe.release_version_id = NEW.release_version_id AND "
        "recipe.watermark_asset_id = revision.watermark_asset_id AND "
        "recipe.configuration #>> '{watermark,position}' = NEW.watermark_position AND "
        "((NEW.derivative_job_id IS NOT NULL AND EXISTS (SELECT 1 FROM derivative_jobs "
        "AS job WHERE job.id = NEW.derivative_job_id AND job.release_selection_id = "
        "NEW.release_selection_id AND job.derivative_recipe_id = NEW.derivative_recipe_id "
        "AND job.x_teaser_revision_id = NEW.revision_id)) OR "
        "(NEW.derivative_output_id IS NOT NULL AND EXISTS (SELECT 1 FROM "
        "derivative_outputs AS output WHERE output.id = NEW.derivative_output_id AND "
        "output.release_selection_id = NEW.release_selection_id AND "
        "output.derivative_recipe_id = NEW.derivative_recipe_id AND output.target = "
        "'x_teaser')))) THEN RAISE EXCEPTION 'X teaser revision member is invalid'; END IF; "
        "RETURN NEW; END; $$ LANGUAGE plpgsql",
        "CREATE TRIGGER x_teaser_revision_members_guard BEFORE INSERT OR UPDATE OR DELETE ON "
        "x_teaser_revision_members FOR EACH ROW EXECUTE FUNCTION "
        "gen_automation_guard_x_teaser_revision_member_mutation()",
    ),
    (
        XTeaserRevisionHead.__table__,
        "CREATE OR REPLACE FUNCTION gen_automation_guard_x_teaser_revision_head_mutation() "
        "RETURNS trigger AS $$ BEGIN IF TG_OP = 'DELETE' THEN RAISE EXCEPTION "
        "'X teaser revision head cannot be deleted'; END IF; IF TG_OP = 'UPDATE' AND "
        "(OLD.id IS DISTINCT FROM NEW.id OR OLD.review_task_id IS DISTINCT FROM "
        "NEW.review_task_id OR OLD.release_version_id IS DISTINCT FROM "
        "NEW.release_version_id OR NEW.lock_version <> OLD.lock_version + 1 OR NOT "
        "((OLD.pending_revision_id IS NULL AND NEW.pending_revision_id IS NOT NULL AND "
        "NEW.active_revision_id IS NOT DISTINCT FROM OLD.active_revision_id) OR "
        "(OLD.pending_revision_id IS NOT NULL AND NEW.pending_revision_id IS NULL AND "
        "(NEW.active_revision_id IS NOT DISTINCT FROM OLD.pending_revision_id OR "
        "NEW.active_revision_id IS NOT DISTINCT FROM OLD.active_revision_id)))) THEN "
        "RAISE EXCEPTION 'X teaser revision head transition is invalid'; END IF; RETURN NEW; "
        "END; $$ LANGUAGE plpgsql",
        "CREATE TRIGGER x_teaser_revision_heads_guard BEFORE UPDATE OR DELETE ON "
        "x_teaser_revision_heads FOR EACH ROW EXECUTE FUNCTION "
        "gen_automation_guard_x_teaser_revision_head_mutation()",
    ),
):
    event.listen(
        _table,
        "after_create",
        _ddl(_function).execute_if(dialect="postgresql"),
    )
    event.listen(
        _table,
        "after_create",
        _ddl(_trigger).execute_if(dialect="postgresql"),
    )

for _statement in (
    "CREATE OR REPLACE FUNCTION gen_automation_guard_review_decision_insert() "
    "RETURNS trigger AS $$ BEGIN "
    "PERFORM 1 FROM review_tasks WHERE id = NEW.review_task_id "
    "AND scoring_run_id = NEW.scoring_run_id AND state = 'open' FOR UPDATE; "
    "IF NOT FOUND THEN "
    "RAISE EXCEPTION 'terminal review tasks reject new decisions'; END IF; "
    "RETURN NEW; END; $$ LANGUAGE plpgsql",
    "CREATE OR REPLACE FUNCTION gen_automation_guard_release_selection_mutation() "
    "RETURNS trigger AS $$ BEGIN "
    "IF TG_OP <> 'INSERT' THEN "
    "RAISE EXCEPTION 'release selections are immutable'; END IF; "
    "PERFORM 1 FROM review_tasks WHERE id = NEW.review_task_id "
    "AND state = 'open' FOR UPDATE; "
    "IF NOT FOUND OR NOT EXISTS ("
    "SELECT 1 FROM review_tasks AS task "
    "JOIN release_versions AS version ON version.id = task.release_version_id "
    "JOIN review_decisions AS decision "
    "ON decision.id = NEW.review_decision_id "
    "AND decision.review_task_id = task.id "
    "AND decision.asset_id = NEW.asset_id "
    "AND decision.revision = NEW.decision_revision "
    "AND decision.scoring_run_id = task.scoring_run_id "
    "JOIN asset_rankings AS ranking "
    "ON ranking.scoring_run_id = task.scoring_run_id "
    "AND ranking.asset_id = NEW.asset_id "
    "JOIN assets AS asset ON asset.id = NEW.asset_id "
    "WHERE task.id = NEW.review_task_id "
    "AND task.scoring_run_id = NEW.scoring_run_id "
    "AND task.release_version_id = NEW.release_version_id "
    "AND task.ranking_manifest_sha256 = NEW.ranking_manifest_sha256 "
    "AND decision.decision = 'accept' "
    "AND NOT EXISTS (SELECT 1 FROM review_decisions AS newer "
    "WHERE newer.review_task_id = decision.review_task_id "
    "AND newer.asset_id = decision.asset_id "
    "AND newer.revision > decision.revision) "
    "AND ranking.rank = NEW.ranking_rank "
    "AND asset.release_id = version.release_id "
    "AND asset.kind = 'raw_master' AND asset.state = 'available' "
    "AND asset.storage_backend = NEW.source_storage_backend "
    "AND asset.storage_bucket = NEW.source_storage_bucket "
    "AND asset.object_key = NEW.source_object_key "
    "AND asset.object_version_id = NEW.source_object_version_id "
    "AND asset.sha256 = NEW.source_sha256 "
    "AND asset.content_type = NEW.source_content_type "
    "AND asset.image_format = NEW.source_image_format "
    "AND asset.width = NEW.source_width AND asset.height = NEW.source_height "
    "AND asset.byte_size = NEW.source_byte_size "
    "AND asset.available_at = NEW.source_available_at"
    ") THEN RAISE EXCEPTION 'release selection snapshot is invalid'; END IF; "
    "RETURN NEW; END; $$ LANGUAGE plpgsql",
    "CREATE OR REPLACE FUNCTION gen_automation_validate_selection_completion() "
    "RETURNS trigger AS $$ BEGIN "
    "IF OLD.state = 'open' AND NEW.state = 'completed' AND ("
    "(SELECT count(*) FROM release_selections "
    "WHERE review_task_id = OLD.id) <> NEW.desired_accepted_count "
    "OR (SELECT min(display_order) FROM release_selections "
    "WHERE review_task_id = OLD.id) <> 1 "
    "OR (SELECT max(display_order) FROM release_selections "
    "WHERE review_task_id = OLD.id) <> NEW.desired_accepted_count "
    "OR NOT EXISTS (SELECT 1 FROM release_versions AS current_version "
    "JOIN releases AS current_release "
    "ON current_release.id = current_version.release_id "
    "WHERE current_version.id = OLD.release_version_id "
    "AND current_release.current_version_no = current_version.version_no "
    "AND current_release.phase = 'reviewing') "
    "OR EXISTS (SELECT 1 FROM release_selections AS selection "
    "JOIN review_decisions AS decision "
    "ON decision.id = selection.review_decision_id "
    "JOIN asset_rankings AS ranking "
    "ON ranking.scoring_run_id = selection.scoring_run_id "
    "AND ranking.asset_id = selection.asset_id "
    "JOIN release_versions AS version "
    "ON version.id = selection.release_version_id "
    "JOIN assets AS asset ON asset.id = selection.asset_id "
    "WHERE selection.review_task_id = OLD.id AND ("
    "selection.scoring_run_id IS DISTINCT FROM OLD.scoring_run_id "
    "OR selection.release_version_id IS DISTINCT FROM OLD.release_version_id "
    "OR selection.ranking_manifest_sha256 IS DISTINCT FROM "
    "OLD.ranking_manifest_sha256 "
    "OR selection.frozen_at IS DISTINCT FROM NEW.completed_at "
    "OR decision.review_task_id IS DISTINCT FROM OLD.id "
    "OR decision.asset_id IS DISTINCT FROM selection.asset_id "
    "OR decision.revision IS DISTINCT FROM selection.decision_revision "
    "OR decision.decision <> 'accept' "
    "OR EXISTS (SELECT 1 FROM review_decisions AS newer "
    "WHERE newer.review_task_id = decision.review_task_id "
    "AND newer.asset_id = decision.asset_id "
    "AND newer.revision > decision.revision) "
    "OR ranking.rank IS DISTINCT FROM selection.ranking_rank "
    "OR asset.release_id IS DISTINCT FROM version.release_id "
    "OR asset.kind <> 'raw_master' OR asset.state <> 'available' "
    "OR asset.storage_backend IS DISTINCT FROM selection.source_storage_backend "
    "OR asset.storage_bucket IS DISTINCT FROM selection.source_storage_bucket "
    "OR asset.object_key IS DISTINCT FROM selection.source_object_key "
    "OR asset.object_version_id IS DISTINCT FROM selection.source_object_version_id "
    "OR asset.sha256 IS DISTINCT FROM selection.source_sha256 "
    "OR asset.content_type IS DISTINCT FROM selection.source_content_type "
    "OR asset.image_format IS DISTINCT FROM selection.source_image_format "
    "OR asset.width IS DISTINCT FROM selection.source_width "
    "OR asset.height IS DISTINCT FROM selection.source_height "
    "OR asset.byte_size IS DISTINCT FROM selection.source_byte_size "
    "OR asset.available_at IS DISTINCT FROM selection.source_available_at)) "
    "OR EXISTS (SELECT 1 FROM review_decisions AS decision "
    "WHERE decision.review_task_id = OLD.id "
    "AND decision.decision = 'accept' "
    "AND NOT EXISTS (SELECT 1 FROM review_decisions AS newer "
    "WHERE newer.review_task_id = decision.review_task_id "
    "AND newer.asset_id = decision.asset_id "
    "AND newer.revision > decision.revision) "
    "AND NOT EXISTS (SELECT 1 FROM release_selections AS selection "
    "WHERE selection.review_task_id = OLD.id "
    "AND selection.review_decision_id = decision.id)) "
    "OR EXISTS (SELECT 1 FROM release_selections AS earlier "
    "JOIN release_selections AS later "
    "ON later.review_task_id = earlier.review_task_id "
    "WHERE earlier.review_task_id = OLD.id "
    "AND earlier.ranking_rank < later.ranking_rank "
    "AND earlier.display_order > later.display_order)"
    ") THEN "
    "RAISE EXCEPTION 'review completion selection snapshot is invalid'; END IF; "
    "RETURN NEW; END; $$ LANGUAGE plpgsql",
    "CREATE OR REPLACE FUNCTION gen_automation_promote_reviewed_release() "
    "RETURNS trigger AS $$ DECLARE changed_count integer; BEGIN "
    "IF OLD.state = 'open' AND NEW.state = 'completed' THEN "
    "UPDATE releases SET phase = 'approved', lock_version = lock_version + 1 "
    "WHERE phase = 'reviewing' AND id = ("
    "SELECT version.release_id FROM release_versions AS version "
    "WHERE version.id = NEW.release_version_id "
    "AND version.version_no = releases.current_version_no); "
    "GET DIAGNOSTICS changed_count = ROW_COUNT; "
    "IF changed_count <> 1 THEN "
    "RAISE EXCEPTION 'review release approval compare-and-swap failed'; END IF; "
    "END IF; RETURN NEW; END; $$ LANGUAGE plpgsql",
    "CREATE OR REPLACE FUNCTION gen_automation_guard_derivative_recipe_mutation() "
    "RETURNS trigger AS $$ BEGIN "
    "IF TG_OP <> 'INSERT' THEN "
    "RAISE EXCEPTION 'derivative recipes are immutable'; END IF; "
    "IF NEW.watermark_asset_id IS NOT NULL AND NOT EXISTS ("
    "SELECT 1 FROM assets AS asset "
    "WHERE asset.id = NEW.watermark_asset_id "
    "AND asset.kind = 'derivative' AND asset.state = 'available' "
    "AND asset.content_type = 'image/png' AND asset.image_format = 'PNG' "
    "AND asset.metadata ->> 'purpose' = 'watermark' "
    "AND asset.storage_backend = NEW.watermark_storage_backend "
    "AND asset.storage_bucket = NEW.watermark_storage_bucket "
    "AND asset.object_key = NEW.watermark_object_key "
    "AND asset.object_version_id = NEW.watermark_object_version_id "
    "AND asset.sha256 = NEW.watermark_sha256 "
    "AND asset.content_type = NEW.watermark_content_type "
    "AND asset.image_format = NEW.watermark_image_format "
    "AND asset.width = NEW.watermark_width "
    "AND asset.height = NEW.watermark_height "
    "AND asset.byte_size = NEW.watermark_byte_size"
    ") THEN "
    "RAISE EXCEPTION 'derivative recipe watermark snapshot is invalid'; END IF; "
    "RETURN NEW; END; $$ LANGUAGE plpgsql",
    "CREATE OR REPLACE FUNCTION gen_automation_guard_derivative_job_insert() "
    "RETURNS trigger AS $$ BEGIN "
    "IF NOT EXISTS ("
    "SELECT 1 FROM release_selections AS selection "
    "JOIN derivative_recipes AS recipe "
    "ON recipe.id = NEW.derivative_recipe_id "
    "JOIN release_versions AS version ON version.id = NEW.release_version_id "
    "JOIN releases AS release ON release.id = version.release_id "
    "WHERE selection.id = NEW.release_selection_id "
    "AND selection.release_version_id = NEW.release_version_id "
    "AND recipe.release_version_id = NEW.release_version_id "
    "AND recipe.expected_output_count = NEW.expected_output_count "
    "AND release.current_version_no = version.version_no "
    "AND ((NEW.gates_release AND release.phase = 'rendering') OR "
    "(NOT NEW.gates_release AND release.phase IN "
    "('rendering', 'ready_to_publish', 'publishing', 'published') "
    "AND EXISTS (SELECT 1 FROM x_teaser_revision_heads AS head WHERE "
    "head.review_task_id = selection.review_task_id "
    "AND head.release_version_id = NEW.release_version_id "
    "AND (head.active_revision_id IS NOT NULL OR release.phase <> 'rendering') "
    "AND head.pending_revision_id = NEW.x_teaser_revision_id)))"
    ") THEN RAISE EXCEPTION 'derivative job release snapshot is invalid'; END IF; "
    "RETURN NEW; END; $$ LANGUAGE plpgsql",
    "CREATE OR REPLACE FUNCTION gen_automation_guard_derivative_job_mutation() "
    "RETURNS trigger AS $$ BEGIN "
    "IF TG_OP = 'DELETE' THEN "
    "RAISE EXCEPTION 'derivative jobs cannot be deleted'; END IF; "
    "IF OLD.state IN ('succeeded', 'cancelled') "
    "OR NEW.lock_version <> OLD.lock_version + 1 "
    "OR OLD.id IS DISTINCT FROM NEW.id "
    "OR OLD.release_selection_id IS DISTINCT FROM NEW.release_selection_id "
    "OR OLD.derivative_recipe_id IS DISTINCT FROM NEW.derivative_recipe_id "
    "OR OLD.x_teaser_revision_id IS DISTINCT FROM NEW.x_teaser_revision_id "
    "OR OLD.gates_release IS DISTINCT FROM NEW.gates_release "
    "OR OLD.release_version_id IS DISTINCT FROM NEW.release_version_id "
    "OR OLD.logical_key IS DISTINCT FROM NEW.logical_key "
    "OR OLD.request_payload IS DISTINCT FROM NEW.request_payload "
    "OR OLD.request_sha256 IS DISTINCT FROM NEW.request_sha256 "
    "OR OLD.expected_output_count IS DISTINCT FROM NEW.expected_output_count "
    "OR OLD.priority IS DISTINCT FROM NEW.priority "
    "OR (OLD.max_attempts IS DISTINCT FROM NEW.max_attempts AND NOT ("
    "OLD.state = 'failed' AND NEW.state = 'retry_wait' "
    "AND NEW.max_attempts > OLD.max_attempts "
    "AND NEW.max_attempts <= 10 "
    "AND NEW.max_attempts >= OLD.attempt_count + 1)) "
    "OR OLD.available_at IS DISTINCT FROM NEW.available_at "
    "OR OLD.requested_at IS DISTINCT FROM NEW.requested_at THEN "
    "RAISE EXCEPTION 'derivative job identity is immutable'; END IF; "
    "IF NOT ("
    "(OLD.state IN ('requested', 'retry_wait') "
    "AND NEW.state IN ('claimed', 'cancelled')) "
    "OR (OLD.state = 'claimed' "
    "AND NEW.state IN ('claimed', 'processing', 'retry_wait', 'failed', 'cancelled')) "
    "OR (OLD.state = 'processing' "
    "AND NEW.state IN ('claimed', 'retry_wait', 'succeeded', 'failed', 'cancelled')) "
    "OR (OLD.state = 'failed' AND NEW.state = 'retry_wait')"
    ") THEN RAISE EXCEPTION 'derivative job state transition is invalid'; END IF; "
    "IF NEW.state = 'claimed' "
    "AND NEW.attempt_count <> OLD.attempt_count + 1 THEN "
    "RAISE EXCEPTION 'derivative job claim attempt is invalid'; END IF; "
    "IF NEW.state <> 'claimed' "
    "AND NEW.attempt_count <> OLD.attempt_count THEN "
    "RAISE EXCEPTION 'derivative job attempt count is immutable'; END IF; "
    "IF OLD.state = 'failed' AND ("
    "NEW.state <> 'retry_wait' "
    "OR OLD.last_error_code IS NOT DISTINCT FROM 'output_object_conflict' "
    "OR NEW.completed_at IS NOT NULL "
    "OR NEW.last_error_code IS NOT NULL OR NEW.last_error_detail IS NOT NULL "
    "OR NEW.lease_owner IS NOT NULL OR NEW.lease_expires_at IS NOT NULL "
    "OR NEW.retry_at IS NULL "
    "OR NEW.max_attempts <= OLD.max_attempts "
    "OR NEW.max_attempts > 10 "
    "OR NEW.max_attempts < OLD.attempt_count + 1 "
    "OR NOT EXISTS (SELECT 1 FROM derivative_recipes AS retry_recipe "
    "JOIN release_versions AS retry_version "
    "ON retry_version.id = OLD.release_version_id "
    "JOIN releases AS retry_release ON retry_release.id = retry_version.release_id "
    "WHERE retry_recipe.id = OLD.derivative_recipe_id "
    "AND retry_recipe.release_version_id = OLD.release_version_id "
    "AND retry_recipe.output_targets::jsonb = '[\"full\"]'::jsonb "
    "AND retry_release.current_version_no = retry_version.version_no "
    "AND retry_release.phase = 'rendering')) THEN "
    "RAISE EXCEPTION 'failed derivative job rearm is invalid'; END IF; "
    "IF OLD.state IN ('claimed', 'processing') AND NEW.state = 'claimed' "
    "AND (OLD.lease_expires_at IS NULL "
    "OR NEW.claimed_at < OLD.lease_expires_at) THEN "
    "RAISE EXCEPTION 'active derivative job lease cannot be stolen'; END IF; "
    "IF NEW.state = 'succeeded' AND (SELECT count(*) "
    "FROM derivative_outputs WHERE derivative_job_id = OLD.id) "
    "<> OLD.expected_output_count THEN "
    "RAISE EXCEPTION 'derivative job outputs are incomplete'; END IF; "
    "IF NEW.state = 'succeeded' AND NOT EXISTS ("
    "SELECT 1 FROM release_versions AS version "
    "JOIN releases AS release ON release.id = version.release_id "
    "JOIN release_selections AS selection ON selection.id = OLD.release_selection_id "
    "WHERE version.id = OLD.release_version_id "
    "AND release.current_version_no = version.version_no "
    "AND ((NEW.gates_release AND release.phase = 'rendering') OR "
    "(NOT NEW.gates_release AND release.phase IN "
    "('rendering', 'ready_to_publish', 'publishing', 'published') "
    "AND EXISTS (SELECT 1 FROM x_teaser_revision_heads AS head WHERE "
    "head.review_task_id = selection.review_task_id "
    "AND head.release_version_id = NEW.release_version_id "
    "AND (head.active_revision_id IS NOT NULL OR release.phase <> 'rendering') "
    "AND head.pending_revision_id = NEW.x_teaser_revision_id)))) THEN "
    "RAISE EXCEPTION 'derivative job release phase is invalid'; END IF; "
    "RETURN NEW; END; $$ LANGUAGE plpgsql",
    "CREATE OR REPLACE FUNCTION gen_automation_promote_rendered_release() "
    "RETURNS trigger AS $$ DECLARE changed_count integer; BEGIN "
    "IF OLD.state <> 'succeeded' AND NEW.state = 'succeeded' "
    "AND NEW.gates_release "
    "AND NOT EXISTS (SELECT 1 FROM derivative_jobs AS pending "
    "WHERE pending.release_version_id = NEW.release_version_id "
    "AND pending.gates_release "
    "AND pending.state <> 'succeeded' "
    "AND ((pending.x_teaser_revision_id IS NULL AND ("
    "EXISTS (SELECT 1 FROM derivative_recipes AS pending_recipe "
    "WHERE pending_recipe.id = pending.derivative_recipe_id "
    "AND pending_recipe.output_targets::jsonb ? 'full') "
    "OR NOT EXISTS (SELECT 1 FROM release_selections AS pending_selection "
    "JOIN x_teaser_revision_heads AS pending_head "
    "ON pending_head.review_task_id = pending_selection.review_task_id "
    "WHERE pending_selection.id = pending.release_selection_id "
    "AND pending_head.release_version_id = pending.release_version_id))) "
    "OR (pending.x_teaser_revision_id IS NOT NULL AND EXISTS ("
    "SELECT 1 FROM release_selections AS pending_selection "
    "JOIN x_teaser_revision_heads AS pending_head "
    "ON pending_head.review_task_id = pending_selection.review_task_id "
    "WHERE pending_selection.id = pending.release_selection_id "
    "AND pending_head.release_version_id = pending.release_version_id "
    "AND (pending_head.active_revision_id = pending.x_teaser_revision_id "
    "OR pending_head.pending_revision_id = pending.x_teaser_revision_id))))) THEN "
    "UPDATE releases SET phase = 'ready_to_publish', "
    "lock_version = lock_version + 1 "
    "WHERE phase = 'rendering' AND id = ("
    "SELECT version.release_id FROM release_versions AS version "
    "WHERE version.id = NEW.release_version_id "
    "AND version.version_no = releases.current_version_no); "
    "GET DIAGNOSTICS changed_count = ROW_COUNT; "
    "IF changed_count <> 1 THEN "
    "RAISE EXCEPTION 'release readiness compare-and-swap failed'; END IF; "
    "END IF; RETURN NEW; END; $$ LANGUAGE plpgsql",
    "CREATE OR REPLACE FUNCTION gen_automation_guard_derivative_output_mutation() "
    "RETURNS trigger AS $$ BEGIN "
    "IF TG_OP <> 'INSERT' THEN "
    "RAISE EXCEPTION 'derivative outputs are append-only'; END IF; "
    "PERFORM 1 FROM derivative_jobs WHERE id = NEW.derivative_job_id "
    "AND state = 'processing' FOR UPDATE; "
    "IF NOT FOUND OR NOT EXISTS ("
    "SELECT 1 FROM derivative_jobs AS job "
    "JOIN derivative_recipes AS recipe "
    "ON recipe.id = job.derivative_recipe_id "
    "JOIN release_selections AS selection "
    "ON selection.id = job.release_selection_id "
    "JOIN release_versions AS version ON version.id = job.release_version_id "
    "JOIN assets AS asset ON asset.id = NEW.asset_id "
    "JOIN asset_lineage AS lineage ON lineage.id = NEW.asset_lineage_id "
    "WHERE job.id = NEW.derivative_job_id "
    "AND job.release_selection_id = NEW.release_selection_id "
    "AND job.derivative_recipe_id = NEW.derivative_recipe_id "
    "AND job.state = 'processing' "
    "AND job.lease_expires_at > NEW.recorded_at "
    "AND recipe.output_targets ? NEW.target "
    "AND selection.asset_id = NEW.source_asset_id "
    "AND asset.release_id = version.release_id "
    "AND asset.kind = 'derivative' AND asset.state = 'available' "
    "AND asset.storage_backend = NEW.asset_storage_backend "
    "AND asset.storage_bucket = NEW.asset_storage_bucket "
    "AND asset.object_key = NEW.asset_object_key "
    "AND asset.object_version_id = NEW.asset_object_version_id "
    "AND asset.sha256 = NEW.asset_sha256 "
    "AND asset.content_type = NEW.asset_content_type "
    "AND asset.image_format = NEW.asset_image_format "
    "AND asset.width = NEW.asset_width AND asset.height = NEW.asset_height "
    "AND asset.byte_size = NEW.asset_byte_size "
    "AND lineage.parent_asset_id = selection.asset_id "
    "AND lineage.child_asset_id = asset.id "
    "AND lineage.relation = NEW.lineage_relation "
    "AND lineage.relation = 'derivative' "
    "AND lineage.recipe_version = NEW.lineage_recipe_version "
    "AND lineage.recipe_version = recipe.config_sha256"
    ") THEN RAISE EXCEPTION 'derivative output snapshot is invalid'; END IF; "
    "RETURN NEW; END; $$ LANGUAGE plpgsql",
    "CREATE OR REPLACE FUNCTION gen_automation_guard_recorded_lineage_mutation() "
    "RETURNS trigger AS $$ BEGIN "
    "IF EXISTS (SELECT 1 FROM derivative_outputs "
    "WHERE asset_lineage_id = OLD.id) THEN "
    "RAISE EXCEPTION 'recorded derivative lineage is immutable'; END IF; "
    "IF TG_OP = 'DELETE' THEN RETURN OLD; END IF; RETURN NEW; "
    "END; $$ LANGUAGE plpgsql",
):
    event.listen(
        DerivativeOutput.__table__,
        "after_create",
        _ddl(_statement).execute_if(dialect="postgresql"),
    )


# Managed LoRA provenance and import identities are durable compliance facts.
# Operational state may advance, but neither table may be hard-deleted and
# immutable identities may only acquire their one-time result/version fields.
for _statement in (
    "CREATE TRIGGER managed_lora_artifacts_guard_update "
    "BEFORE UPDATE ON managed_lora_artifacts BEGIN SELECT CASE WHEN "
    "OLD.id IS NOT NEW.id OR OLD.artifact_sha256 IS NOT NEW.artifact_sha256 "
    "OR OLD.display_name IS NOT NEW.display_name "
    "OR OLD.source_type IS NOT NEW.source_type "
    "OR OLD.canonical_source_url IS NOT NEW.canonical_source_url "
    "OR OLD.license_url IS NOT NEW.license_url "
    "OR OLD.civitai_model_id IS NOT NEW.civitai_model_id "
    "OR OLD.civitai_version_id IS NOT NEW.civitai_version_id "
    "OR OLD.civitai_file_id IS NOT NEW.civitai_file_id "
    "OR OLD.provenance IS NOT NEW.provenance "
    "OR OLD.storage_bucket IS NOT NEW.storage_bucket "
    "OR OLD.object_key IS NOT NEW.object_key "
    "OR OLD.object_version_id IS NOT NEW.object_version_id "
    "OR OLD.object_etag IS NOT NEW.object_etag "
    "OR OLD.byte_size IS NOT NEW.byte_size "
    "OR OLD.target_filename IS NOT NEW.target_filename "
    "OR OLD.approval_id IS NOT NEW.approval_id "
    "OR OLD.trigger_words IS NOT NEW.trigger_words "
    "OR OLD.registered_by_user_id IS NOT NEW.registered_by_user_id "
    "OR OLD.created_at IS NOT NEW.created_at "
    "THEN RAISE(ABORT, 'managed LoRA identity is immutable') END; END",
    "CREATE TRIGGER managed_lora_artifacts_reject_delete "
    "BEFORE DELETE ON managed_lora_artifacts BEGIN "
    "SELECT RAISE(ABORT, 'managed LoRAs cannot be deleted'); END",
    "CREATE TRIGGER lora_import_jobs_guard_update "
    "BEFORE UPDATE ON lora_import_jobs BEGIN SELECT CASE WHEN "
    "OLD.id IS NOT NEW.id OR OLD.source_type IS NOT NEW.source_type "
    "OR OLD.display_name IS NOT NEW.display_name "
    "OR OLD.canonical_source_url IS NOT NEW.canonical_source_url "
    "OR OLD.license_url IS NOT NEW.license_url "
    "OR OLD.commercial_use_attested IS NOT NEW.commercial_use_attested "
    "OR OLD.adult_use_attested IS NOT NEW.adult_use_attested "
    "OR OLD.civitai_model_id IS NOT NEW.civitai_model_id "
    "OR OLD.civitai_version_id IS NOT NEW.civitai_version_id "
    "OR OLD.civitai_file_id IS NOT NEW.civitai_file_id "
    "OR OLD.staging_bucket IS NOT NEW.staging_bucket "
    "OR OLD.staging_object_key IS NOT NEW.staging_object_key "
    "OR (OLD.staging_object_version_id IS NOT NEW.staging_object_version_id "
    "AND NOT (OLD.staging_object_version_id IS NULL "
    "AND NEW.staging_object_version_id IS NOT NULL)) "
    "OR (OLD.staging_object_etag IS NOT NEW.staging_object_etag "
    "AND NOT (OLD.staging_object_etag IS NULL "
    "AND NEW.staging_object_etag IS NOT NULL)) "
    "OR (OLD.staging_byte_size IS NOT NEW.staging_byte_size "
    "AND NOT (OLD.staging_byte_size IS NULL AND NEW.staging_byte_size IS NOT NULL)) "
    "OR OLD.target_filename IS NOT NEW.target_filename "
    "OR OLD.expected_sha256 IS NOT NEW.expected_sha256 "
    "OR OLD.expected_byte_size IS NOT NEW.expected_byte_size "
    "OR OLD.expected_metadata IS NOT NEW.expected_metadata "
    "OR OLD.trigger_words IS NOT NEW.trigger_words "
    "OR OLD.max_attempts IS NOT NEW.max_attempts "
    "OR OLD.requested_by_user_id IS NOT NEW.requested_by_user_id "
    "OR OLD.created_at IS NOT NEW.created_at "
    "OR (OLD.result_artifact_id IS NOT NEW.result_artifact_id "
    "AND NOT (OLD.result_artifact_id IS NULL AND NEW.result_artifact_id IS NOT NULL)) "
    "THEN RAISE(ABORT, 'LoRA import identity is immutable') END; END",
    "CREATE TRIGGER lora_import_jobs_reject_delete "
    "BEFORE DELETE ON lora_import_jobs BEGIN "
    "SELECT RAISE(ABORT, 'LoRA import jobs cannot be deleted'); END",
):
    event.listen(
        LoraImportJob.__table__,
        "after_create",
        _ddl(_statement).execute_if(dialect="sqlite"),
    )

for _statement in (
    "CREATE OR REPLACE FUNCTION gen_automation_guard_managed_lora_mutation() "
    "RETURNS trigger AS $$ BEGIN IF TG_OP = 'DELETE' THEN "
    "RAISE EXCEPTION 'managed LoRAs cannot be deleted'; END IF; "
    "IF OLD.id IS DISTINCT FROM NEW.id "
    "OR OLD.artifact_sha256 IS DISTINCT FROM NEW.artifact_sha256 "
    "OR OLD.display_name IS DISTINCT FROM NEW.display_name "
    "OR OLD.source_type IS DISTINCT FROM NEW.source_type "
    "OR OLD.canonical_source_url IS DISTINCT FROM NEW.canonical_source_url "
    "OR OLD.license_url IS DISTINCT FROM NEW.license_url "
    "OR OLD.civitai_model_id IS DISTINCT FROM NEW.civitai_model_id "
    "OR OLD.civitai_version_id IS DISTINCT FROM NEW.civitai_version_id "
    "OR OLD.civitai_file_id IS DISTINCT FROM NEW.civitai_file_id "
    "OR OLD.provenance IS DISTINCT FROM NEW.provenance "
    "OR OLD.storage_bucket IS DISTINCT FROM NEW.storage_bucket "
    "OR OLD.object_key IS DISTINCT FROM NEW.object_key "
    "OR OLD.object_version_id IS DISTINCT FROM NEW.object_version_id "
    "OR OLD.object_etag IS DISTINCT FROM NEW.object_etag "
    "OR OLD.byte_size IS DISTINCT FROM NEW.byte_size "
    "OR OLD.target_filename IS DISTINCT FROM NEW.target_filename "
    "OR OLD.approval_id IS DISTINCT FROM NEW.approval_id "
    "OR OLD.trigger_words IS DISTINCT FROM NEW.trigger_words "
    "OR OLD.registered_by_user_id IS DISTINCT FROM NEW.registered_by_user_id "
    "OR OLD.created_at IS DISTINCT FROM NEW.created_at THEN "
    "RAISE EXCEPTION 'managed LoRA identity is immutable'; END IF; "
    "RETURN NEW; END; $$ LANGUAGE plpgsql",
    "CREATE OR REPLACE FUNCTION gen_automation_guard_lora_import_mutation() "
    "RETURNS trigger AS $$ BEGIN IF TG_OP = 'DELETE' THEN "
    "RAISE EXCEPTION 'LoRA import jobs cannot be deleted'; END IF; "
    "IF OLD.id IS DISTINCT FROM NEW.id "
    "OR OLD.source_type IS DISTINCT FROM NEW.source_type "
    "OR OLD.display_name IS DISTINCT FROM NEW.display_name "
    "OR OLD.canonical_source_url IS DISTINCT FROM NEW.canonical_source_url "
    "OR OLD.license_url IS DISTINCT FROM NEW.license_url "
    "OR OLD.commercial_use_attested IS DISTINCT FROM NEW.commercial_use_attested "
    "OR OLD.adult_use_attested IS DISTINCT FROM NEW.adult_use_attested "
    "OR OLD.civitai_model_id IS DISTINCT FROM NEW.civitai_model_id "
    "OR OLD.civitai_version_id IS DISTINCT FROM NEW.civitai_version_id "
    "OR OLD.civitai_file_id IS DISTINCT FROM NEW.civitai_file_id "
    "OR OLD.staging_bucket IS DISTINCT FROM NEW.staging_bucket "
    "OR OLD.staging_object_key IS DISTINCT FROM NEW.staging_object_key "
    "OR (OLD.staging_object_version_id IS DISTINCT FROM NEW.staging_object_version_id "
    "AND NOT (OLD.staging_object_version_id IS NULL "
    "AND NEW.staging_object_version_id IS NOT NULL)) "
    "OR (OLD.staging_object_etag IS DISTINCT FROM NEW.staging_object_etag "
    "AND NOT (OLD.staging_object_etag IS NULL AND NEW.staging_object_etag IS NOT NULL)) "
    "OR (OLD.staging_byte_size IS DISTINCT FROM NEW.staging_byte_size "
    "AND NOT (OLD.staging_byte_size IS NULL AND NEW.staging_byte_size IS NOT NULL)) "
    "OR OLD.target_filename IS DISTINCT FROM NEW.target_filename "
    "OR OLD.expected_sha256 IS DISTINCT FROM NEW.expected_sha256 "
    "OR OLD.expected_byte_size IS DISTINCT FROM NEW.expected_byte_size "
    "OR OLD.expected_metadata IS DISTINCT FROM NEW.expected_metadata "
    "OR OLD.trigger_words IS DISTINCT FROM NEW.trigger_words "
    "OR OLD.max_attempts IS DISTINCT FROM NEW.max_attempts "
    "OR OLD.requested_by_user_id IS DISTINCT FROM NEW.requested_by_user_id "
    "OR OLD.created_at IS DISTINCT FROM NEW.created_at "
    "OR (OLD.result_artifact_id IS DISTINCT FROM NEW.result_artifact_id "
    "AND NOT (OLD.result_artifact_id IS NULL AND NEW.result_artifact_id IS NOT NULL)) THEN "
    "RAISE EXCEPTION 'LoRA import identity is immutable'; END IF; "
    "RETURN NEW; END; $$ LANGUAGE plpgsql",
):
    event.listen(
        LoraImportJob.__table__,
        "after_create",
        _ddl(_statement).execute_if(dialect="postgresql"),
    )

for _statement in (
    "CREATE TRIGGER managed_lora_artifacts_guard_mutation "
    "BEFORE UPDATE OR DELETE ON managed_lora_artifacts FOR EACH ROW "
    "EXECUTE FUNCTION gen_automation_guard_managed_lora_mutation()",
    "CREATE TRIGGER lora_import_jobs_guard_mutation "
    "BEFORE UPDATE OR DELETE ON lora_import_jobs FOR EACH ROW "
    "EXECUTE FUNCTION gen_automation_guard_lora_import_mutation()",
):
    event.listen(
        LoraImportJob.__table__,
        "after_create",
        _ddl(_statement).execute_if(dialect="postgresql"),
    )


# Extracted MEGA set records retain immutable source and destination identities
# while allowing only their operational state, counters, leases, and recovery
# references to advance. A delivery's byte plan may be frozen exactly once
# after its archive manifest has been expanded into item rows.
for _statement in (
    "CREATE TRIGGER mega_set_deliveries_guard_update "
    "BEFORE UPDATE ON mega_set_deliveries BEGIN "
    "SELECT CASE WHEN "
    "OLD.id IS NOT NEW.id "
    "OR OLD.finished_set_archive_id IS NOT NEW.finished_set_archive_id "
    "OR OLD.remote_root IS NOT NEW.remote_root "
    "OR OLD.remote_folder IS NOT NEW.remote_folder "
    "OR OLD.manifest_sha256 IS NOT NEW.manifest_sha256 "
    "OR OLD.total_item_count IS NOT NEW.total_item_count "
    "OR OLD.created_at IS NOT NEW.created_at "
    "OR (OLD.total_byte_size IS NOT NEW.total_byte_size "
    "AND NOT (OLD.total_byte_size IS NULL AND NEW.total_byte_size IS NOT NULL)) "
    "OR (OLD.source_manifest_json IS NOT NEW.source_manifest_json "
    "AND NOT (OLD.source_manifest_json IS NULL AND NEW.source_manifest_json IS NOT NULL)) "
    "OR (OLD.planned_at IS NOT NEW.planned_at "
    "AND NOT (OLD.planned_at IS NULL AND NEW.planned_at IS NOT NULL)) "
    "THEN RAISE(ABORT, 'MEGA set delivery identity is immutable') END; END",
    "CREATE TRIGGER mega_set_deliveries_reject_delete "
    "BEFORE DELETE ON mega_set_deliveries BEGIN "
    "SELECT RAISE(ABORT, 'MEGA set deliveries cannot be deleted'); END",
    "CREATE TRIGGER mega_set_delivery_items_guard_update "
    "BEFORE UPDATE ON mega_set_delivery_items BEGIN "
    "SELECT CASE WHEN "
    "OLD.id IS NOT NEW.id "
    "OR OLD.delivery_id IS NOT NEW.delivery_id "
    "OR OLD.ordinal IS NOT NEW.ordinal "
    "OR OLD.source_asset_id IS NOT NEW.source_asset_id "
    "OR OLD.readiness_derivative_output_id IS NOT NEW.readiness_derivative_output_id "
    "OR OLD.source_sha256 IS NOT NEW.source_sha256 "
    "OR OLD.source_byte_size IS NOT NEW.source_byte_size "
    "OR OLD.source_content_type IS NOT NEW.source_content_type "
    "OR OLD.remote_path IS NOT NEW.remote_path "
    "OR OLD.created_at IS NOT NEW.created_at "
    "THEN RAISE(ABORT, 'MEGA set delivery item identity is immutable') END; END",
    "CREATE TRIGGER mega_set_delivery_items_reject_delete "
    "BEFORE DELETE ON mega_set_delivery_items BEGIN "
    "SELECT RAISE(ABORT, 'MEGA set delivery items cannot be deleted'); END",
):
    event.listen(
        MegaSetDeliveryItem.__table__,
        "after_create",
        _ddl(_statement).execute_if(dialect="sqlite"),
    )

for _statement in (
    "CREATE OR REPLACE FUNCTION "
    "gen_automation_guard_mega_set_delivery_mutation() "
    "RETURNS trigger AS $$ BEGIN "
    "IF TG_OP = 'DELETE' THEN "
    "RAISE EXCEPTION 'MEGA set deliveries cannot be deleted'; END IF; "
    "IF OLD.id IS DISTINCT FROM NEW.id "
    "OR OLD.finished_set_archive_id IS DISTINCT FROM NEW.finished_set_archive_id "
    "OR OLD.remote_root IS DISTINCT FROM NEW.remote_root "
    "OR OLD.remote_folder IS DISTINCT FROM NEW.remote_folder "
    "OR OLD.manifest_sha256 IS DISTINCT FROM NEW.manifest_sha256 "
    "OR OLD.total_item_count IS DISTINCT FROM NEW.total_item_count "
    "OR OLD.created_at IS DISTINCT FROM NEW.created_at "
    "OR (OLD.total_byte_size IS DISTINCT FROM NEW.total_byte_size "
    "AND NOT (OLD.total_byte_size IS NULL AND NEW.total_byte_size IS NOT NULL)) "
    "OR (OLD.source_manifest_json IS DISTINCT FROM NEW.source_manifest_json "
    "AND NOT (OLD.source_manifest_json IS NULL AND NEW.source_manifest_json IS NOT NULL)) "
    "OR (OLD.planned_at IS DISTINCT FROM NEW.planned_at "
    "AND NOT (OLD.planned_at IS NULL AND NEW.planned_at IS NOT NULL)) THEN "
    "RAISE EXCEPTION 'MEGA set delivery identity is immutable'; END IF; "
    "RETURN NEW; END; $$ LANGUAGE plpgsql",
    "CREATE OR REPLACE FUNCTION "
    "gen_automation_guard_mega_set_delivery_item_mutation() "
    "RETURNS trigger AS $$ BEGIN "
    "IF TG_OP = 'DELETE' THEN "
    "RAISE EXCEPTION 'MEGA set delivery items cannot be deleted'; END IF; "
    "IF OLD.id IS DISTINCT FROM NEW.id "
    "OR OLD.delivery_id IS DISTINCT FROM NEW.delivery_id "
    "OR OLD.ordinal IS DISTINCT FROM NEW.ordinal "
    "OR OLD.source_asset_id IS DISTINCT FROM NEW.source_asset_id "
    "OR OLD.readiness_derivative_output_id "
    "IS DISTINCT FROM NEW.readiness_derivative_output_id "
    "OR OLD.source_sha256 IS DISTINCT FROM NEW.source_sha256 "
    "OR OLD.source_byte_size IS DISTINCT FROM NEW.source_byte_size "
    "OR OLD.source_content_type IS DISTINCT FROM NEW.source_content_type "
    "OR OLD.remote_path IS DISTINCT FROM NEW.remote_path "
    "OR OLD.created_at IS DISTINCT FROM NEW.created_at THEN "
    "RAISE EXCEPTION 'MEGA set delivery item identity is immutable'; END IF; "
    "RETURN NEW; END; $$ LANGUAGE plpgsql",
):
    event.listen(
        MegaSetDeliveryItem.__table__,
        "after_create",
        _ddl(_statement).execute_if(dialect="postgresql"),
    )

for _statement in (
    "CREATE TRIGGER mega_set_deliveries_guard_mutation "
    "BEFORE UPDATE OR DELETE ON mega_set_deliveries FOR EACH ROW "
    "EXECUTE FUNCTION gen_automation_guard_mega_set_delivery_mutation()",
    "CREATE TRIGGER mega_set_delivery_items_guard_mutation "
    "BEFORE UPDATE OR DELETE ON mega_set_delivery_items FOR EACH ROW "
    "EXECUTE FUNCTION gen_automation_guard_mega_set_delivery_item_mutation()",
):
    event.listen(
        MegaSetDeliveryItem.__table__,
        "after_create",
        _ddl(_statement).execute_if(dialect="postgresql"),
    )


event.listen(
    SemanticAssessment.__table__,
    "after_create",
    _ddl(
        "CREATE TRIGGER semantic_assessments_guard_terminal_update "
        "BEFORE UPDATE ON semantic_assessments "
        "WHEN OLD.state IN ('completed', 'unavailable') "
        "BEGIN SELECT RAISE(ABORT, 'terminal semantic assessments are immutable'); END"
    ).execute_if(dialect="sqlite"),
)
event.listen(
    SemanticAssessment.__table__,
    "after_create",
    _ddl(
        "CREATE TRIGGER semantic_assessments_guard_delete "
        "BEFORE DELETE ON semantic_assessments "
        "BEGIN SELECT RAISE(ABORT, 'semantic assessments cannot be deleted'); END"
    ).execute_if(dialect="sqlite"),
)
event.listen(
    SemanticAssessment.__table__,
    "after_create",
    _ddl(
        "CREATE OR REPLACE FUNCTION gen_automation_guard_semantic_assessment_mutation() "
        "RETURNS trigger AS $$ BEGIN "
        "IF TG_OP = 'DELETE' THEN "
        "RAISE EXCEPTION 'semantic assessments cannot be deleted'; END IF; "
        "IF OLD.state IN ('completed', 'unavailable') THEN "
        "RAISE EXCEPTION 'terminal semantic assessments are immutable'; END IF; "
        "RETURN NEW; END; $$ LANGUAGE plpgsql"
    ).execute_if(dialect="postgresql"),
)
event.listen(
    SemanticAssessment.__table__,
    "after_create",
    _ddl(
        "CREATE TRIGGER semantic_assessments_guard_mutation "
        "BEFORE UPDATE OR DELETE ON semantic_assessments FOR EACH ROW "
        "EXECUTE FUNCTION gen_automation_guard_semantic_assessment_mutation()"
    ).execute_if(dialect="postgresql"),
)


event.listen(
    SemanticTrainingRun.__table__,
    "after_create",
    _ddl(
        "CREATE TRIGGER semantic_training_runs_guard_terminal_update "
        "BEFORE UPDATE ON semantic_training_runs WHEN "
        "OLD.state IN ('succeeded', 'failed', 'cancelled') "
        "BEGIN SELECT RAISE(ABORT, 'terminal semantic training runs are immutable'); END"
    ).execute_if(dialect="sqlite"),
)
event.listen(
    SemanticTrainingRun.__table__,
    "after_create",
    _ddl(
        "CREATE TRIGGER semantic_training_runs_guard_delete "
        "BEFORE DELETE ON semantic_training_runs "
        "BEGIN SELECT RAISE(ABORT, 'semantic training runs cannot be deleted'); END"
    ).execute_if(dialect="sqlite"),
)
event.listen(
    SemanticTrainingRun.__table__,
    "after_create",
    _ddl(
        "CREATE OR REPLACE FUNCTION gen_automation_guard_semantic_training_run_mutation() "
        "RETURNS trigger AS $$ BEGIN "
        "IF TG_OP = 'DELETE' THEN "
        "RAISE EXCEPTION 'semantic training runs cannot be deleted'; END IF; "
        "IF OLD.state IN ('succeeded', 'failed', 'cancelled') THEN "
        "RAISE EXCEPTION 'terminal semantic training runs are immutable'; END IF; "
        "RETURN NEW; END; $$ LANGUAGE plpgsql"
    ).execute_if(dialect="postgresql"),
)
event.listen(
    SemanticTrainingRun.__table__,
    "after_create",
    _ddl(
        "CREATE TRIGGER semantic_training_runs_guard_mutation "
        "BEFORE UPDATE OR DELETE ON semantic_training_runs FOR EACH ROW "
        "EXECUTE FUNCTION gen_automation_guard_semantic_training_run_mutation()"
    ).execute_if(dialect="postgresql"),
)


event.listen(
    ReviewAssetInspection.__table__,
    "after_create",
    _ddl(
        "CREATE TRIGGER review_asset_inspections_guard_insert "
        "BEFORE INSERT ON review_asset_inspections BEGIN "
        "SELECT CASE WHEN NOT EXISTS ("
        "SELECT 1 FROM review_tasks AS task "
        "WHERE task.id = NEW.review_task_id AND task.state = 'open'"
        ") THEN RAISE(ABORT, 'review inspections require an open task') END; END"
    ).execute_if(dialect="sqlite"),
)
event.listen(
    ReviewAssetInspection.__table__,
    "after_create",
    _ddl(
        "CREATE OR REPLACE FUNCTION gen_automation_guard_review_asset_inspection_insert() "
        "RETURNS trigger AS $$ BEGIN "
        "IF NOT EXISTS (SELECT 1 FROM review_tasks AS task "
        "WHERE task.id = NEW.review_task_id AND task.state = 'open') THEN "
        "RAISE EXCEPTION 'review inspections require an open task'; END IF; "
        "RETURN NEW; END; $$ LANGUAGE plpgsql"
    ).execute_if(dialect="postgresql"),
)
event.listen(
    ReviewAssetInspection.__table__,
    "after_create",
    _ddl(
        "CREATE TRIGGER review_asset_inspections_guard_insert "
        "BEFORE INSERT ON review_asset_inspections FOR EACH ROW "
        "EXECUTE FUNCTION gen_automation_guard_review_asset_inspection_insert()"
    ).execute_if(dialect="postgresql"),
)


for _immutable_table, _table_name, _label in (
    (
        ReviewAssetInspection.__table__,
        "review_asset_inspections",
        "review asset inspections",
    ),
    (
        SemanticAnatomyFeedback.__table__,
        "semantic_anatomy_feedback",
        "semantic anatomy feedback",
    ),
    (
        SemanticCalibrationArtifact.__table__,
        "semantic_calibration_artifacts",
        "semantic calibration artifacts",
    ),
    (
        SemanticModelPromotion.__table__,
        "semantic_model_promotions",
        "semantic model promotions",
    ),
    (
        FinishedSetArchivePart.__table__,
        "finished_set_archive_parts",
        "finished set archive parts",
    ),
):
    event.listen(
        _immutable_table,
        "after_create",
        _ddl(
            f"CREATE TRIGGER {_table_name}_immutable_update "
            f"BEFORE UPDATE ON {_table_name} BEGIN "
            f"SELECT RAISE(ABORT, '{_label} are append-only'); END"
        ).execute_if(dialect="sqlite"),
    )
    event.listen(
        _immutable_table,
        "after_create",
        _ddl(
            f"CREATE TRIGGER {_table_name}_immutable_delete "
            f"BEFORE DELETE ON {_table_name} BEGIN "
            f"SELECT RAISE(ABORT, '{_label} are append-only'); END"
        ).execute_if(dialect="sqlite"),
    )
    event.listen(
        _immutable_table,
        "after_create",
        _ddl(
            f"CREATE OR REPLACE FUNCTION gen_automation_guard_{_table_name}_mutation() "
            "RETURNS trigger AS $$ BEGIN "
            f"RAISE EXCEPTION '{_label} are append-only'; END; $$ LANGUAGE plpgsql"
        ).execute_if(dialect="postgresql"),
    )
    event.listen(
        _immutable_table,
        "after_create",
        _ddl(
            f"CREATE TRIGGER {_table_name}_guard_mutation "
            f"BEFORE UPDATE OR DELETE ON {_table_name} FOR EACH ROW "
            f"EXECUTE FUNCTION gen_automation_guard_{_table_name}_mutation()"
        ).execute_if(dialect="postgresql"),
    )


# Wildcard contents are historical release inputs.  Even lightweight
# ``metadata.create_all`` databases preserve every version once written.
for _statement in (
    "CREATE TRIGGER wildcard_libraries_guard_update "
    "BEFORE UPDATE ON wildcard_libraries WHEN "
    "NEW.id <> OLD.id OR NEW.name <> OLD.name "
    "OR NEW.created_by <> OLD.created_by "
    "OR NEW.created_at <> OLD.created_at "
    "OR NEW.current_version_no <> OLD.current_version_no + 1 "
    "OR NEW.lock_version <> OLD.lock_version + 1 BEGIN "
    "SELECT RAISE(ABORT, 'wildcard library head transition is invalid'); END",
    "CREATE TRIGGER wildcard_libraries_guard_delete "
    "BEFORE DELETE ON wildcard_libraries BEGIN "
    "SELECT RAISE(ABORT, 'wildcard libraries cannot be deleted'); END",
):
    event.listen(
        WildcardLibrary.__table__,
        "after_create",
        _ddl(_statement).execute_if(dialect="sqlite"),
    )

event.listen(
    WildcardLibrary.__table__,
    "after_create",
    _ddl(
        "CREATE OR REPLACE FUNCTION "
        "gen_automation_guard_wildcard_library_mutation() "
        "RETURNS trigger AS $$ BEGIN "
        "IF TG_OP = 'DELETE' THEN "
        "RAISE EXCEPTION 'wildcard libraries cannot be deleted'; END IF; "
        "IF NEW.id IS DISTINCT FROM OLD.id "
        "OR NEW.name IS DISTINCT FROM OLD.name "
        "OR NEW.created_by IS DISTINCT FROM OLD.created_by "
        "OR NEW.created_at IS DISTINCT FROM OLD.created_at "
        "OR NEW.current_version_no <> OLD.current_version_no + 1 "
        "OR NEW.lock_version <> OLD.lock_version + 1 THEN "
        "RAISE EXCEPTION 'wildcard library head transition is invalid'; END IF; "
        "RETURN NEW; END; $$ LANGUAGE plpgsql"
    ).execute_if(dialect="postgresql"),
)
event.listen(
    WildcardLibrary.__table__,
    "after_create",
    _ddl(
        "CREATE TRIGGER wildcard_libraries_guard_mutation "
        "BEFORE UPDATE OR DELETE ON wildcard_libraries FOR EACH ROW "
        "EXECUTE FUNCTION gen_automation_guard_wildcard_library_mutation()"
    ).execute_if(dialect="postgresql"),
)

for _statement in (
    "CREATE TRIGGER wildcard_library_versions_immutable_update "
    "BEFORE UPDATE ON wildcard_library_versions BEGIN "
    "SELECT RAISE(ABORT, 'wildcard library versions are append-only'); END",
    "CREATE TRIGGER wildcard_library_versions_immutable_delete "
    "BEFORE DELETE ON wildcard_library_versions BEGIN "
    "SELECT RAISE(ABORT, 'wildcard library versions are append-only'); END",
):
    event.listen(
        WildcardLibraryVersion.__table__,
        "after_create",
        _ddl(_statement).execute_if(dialect="sqlite"),
    )

event.listen(
    WildcardLibraryVersion.__table__,
    "after_create",
    _ddl(
        "CREATE OR REPLACE FUNCTION "
        "gen_automation_guard_wildcard_library_version_mutation() "
        "RETURNS trigger AS $$ BEGIN "
        "RAISE EXCEPTION 'wildcard library versions are append-only'; "
        "END; $$ LANGUAGE plpgsql"
    ).execute_if(dialect="postgresql"),
)
event.listen(
    WildcardLibraryVersion.__table__,
    "after_create",
    _ddl(
        "CREATE TRIGGER wildcard_library_versions_guard_mutation "
        "BEFORE UPDATE OR DELETE ON wildcard_library_versions FOR EACH ROW "
        "EXECUTE FUNCTION gen_automation_guard_wildcard_library_version_mutation()"
    ).execute_if(dialect="postgresql"),
)


# Publication defaults fail closed for both migration-managed databases and
# lightweight ``metadata.create_all`` test/development databases.
event.listen(
    PublicationProviderGuard.__table__,
    "after_create",
    _ddl(
        "INSERT INTO publication_provider_guards "
        "(id, provider, enabled, epoch, lock_version, reason, "
        "changed_by_user_id, changed_at) VALUES "
        "('00000000-0000-0000-0000-000000000001', 'global', false, 1, 1, "
        "'publication is stopped by default', NULL, CURRENT_TIMESTAMP) "
        "ON CONFLICT (provider) DO NOTHING"
    ).execute_if(dialect="postgresql"),
)
event.listen(
    PublicationProviderGuard.__table__,
    "after_create",
    _ddl(
        "INSERT OR IGNORE INTO publication_provider_guards "
        "(id, provider, enabled, epoch, lock_version, reason, "
        "changed_by_user_id, changed_at) VALUES "
        "('00000000000000000000000000000001', 'global', 0, 1, 1, "
        "'publication is stopped by default', NULL, CURRENT_TIMESTAMP)"
    ).execute_if(dialect="sqlite"),
)

for _table, _name in (
    (PublicationInput.__table__, "publication_inputs"),
    (PublicationApproval.__table__, "publication_approvals"),
    (PublicationReconciliation.__table__, "publication_reconciliations"),
    (PublicationEffectEvent.__table__, "publication_effect_events"),
    (PublicationPackage.__table__, "publication_packages"),
):
    event.listen(
        _table,
        "after_create",
        _ddl(
            f"CREATE TRIGGER {_name}_immutable_update "
            f"BEFORE UPDATE ON {_name} BEGIN "
            f"SELECT RAISE(ABORT, '{_name} are append-only'); END"
        ).execute_if(dialect="sqlite"),
    )
    event.listen(
        _table,
        "after_create",
        _ddl(
            f"CREATE TRIGGER {_name}_immutable_delete "
            f"BEFORE DELETE ON {_name} BEGIN "
            f"SELECT RAISE(ABORT, '{_name} are append-only'); END"
        ).execute_if(dialect="sqlite"),
    )

event.listen(
    PublicationProviderGuard.__table__,
    "after_create",
    _ddl(
        "CREATE TRIGGER publication_provider_guards_fail_closed_insert "
        "BEFORE INSERT ON publication_provider_guards "
        "WHEN NEW.provider <> 'global' OR NEW.enabled <> 0 "
        "BEGIN SELECT RAISE(ABORT, 'publication guard must be globally stopped'); END"
    ).execute_if(dialect="sqlite"),
)
event.listen(
    PublicationProviderGuard.__table__,
    "after_create",
    _ddl(
        "CREATE TRIGGER publication_provider_guards_no_delete "
        "BEFORE DELETE ON publication_provider_guards "
        "BEGIN SELECT RAISE(ABORT, 'publication guard cannot be deleted'); END"
    ).execute_if(dialect="sqlite"),
)

for _statement in (
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
    "AND release.phase IN ('rendering', 'ready_to_publish', 'publishing', 'published') "
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
    "OR OLD.public_preview_attested_at IS DISTINCT FROM NEW.public_preview_attested_at "
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
    "AND release.phase IN ('rendering', 'ready_to_publish', 'publishing', 'published') "
    "AND (SELECT count(*) FROM publication_inputs AS input "
    "WHERE input.intent_id = intent.id) = intent.input_count "
    "AND ("
    "(intent.target = 'x' "
    "AND NOT EXISTS (SELECT 1 FROM publication_inputs AS input "
    "WHERE input.intent_id = intent.id AND input.role <> 'x_teaser')) "
    "OR (intent.target = 'patreon' "
    "AND (SELECT count(*) FROM publication_inputs AS input "
    "WHERE input.intent_id = intent.id AND input.role = 'patreon_preview') = 1 "
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
    "CREATE OR REPLACE FUNCTION gen_automation_guard_publication_reconciliation() "
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
):
    event.listen(
        Base.metadata,
        "after_create",
        _ddl(_statement).execute_if(dialect="postgresql"),
    )

for _statement in (
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
    "BEFORE INSERT OR UPDATE OR DELETE ON publication_reconciliations FOR EACH ROW "
    "EXECUTE FUNCTION gen_automation_guard_publication_reconciliation()",
    "CREATE TRIGGER publication_effect_events_guard_mutation "
    "BEFORE INSERT OR UPDATE OR DELETE ON publication_effect_events FOR EACH ROW "
    "EXECUTE FUNCTION gen_automation_guard_publication_effect_event()",
    "CREATE TRIGGER publication_provider_guards_guard_mutation "
    "BEFORE INSERT OR UPDATE OR DELETE ON publication_provider_guards FOR EACH ROW "
    "EXECUTE FUNCTION gen_automation_guard_publication_provider_guard()",
):
    event.listen(
        Base.metadata,
        "after_create",
        _ddl(_statement).execute_if(dialect="postgresql"),
    )

for _statement in (
    "CREATE TRIGGER review_decisions_guard_late_insert "
    "BEFORE INSERT ON review_decisions FOR EACH ROW "
    "EXECUTE FUNCTION gen_automation_guard_review_decision_insert()",
    "CREATE TRIGGER release_selections_guard_mutation "
    "BEFORE INSERT OR UPDATE OR DELETE ON release_selections FOR EACH ROW "
    "EXECUTE FUNCTION gen_automation_guard_release_selection_mutation()",
    "CREATE TRIGGER review_tasks_validate_selection_completion "
    "BEFORE UPDATE ON review_tasks FOR EACH ROW "
    "EXECUTE FUNCTION gen_automation_validate_selection_completion()",
    "CREATE TRIGGER review_tasks_promote_release_after_completion "
    "AFTER UPDATE ON review_tasks FOR EACH ROW "
    "EXECUTE FUNCTION gen_automation_promote_reviewed_release()",
    "CREATE TRIGGER derivative_recipes_guard_mutation "
    "BEFORE INSERT OR UPDATE OR DELETE ON derivative_recipes FOR EACH ROW "
    "EXECUTE FUNCTION gen_automation_guard_derivative_recipe_mutation()",
    "CREATE TRIGGER derivative_jobs_guard_insert "
    "BEFORE INSERT ON derivative_jobs FOR EACH ROW "
    "EXECUTE FUNCTION gen_automation_guard_derivative_job_insert()",
    "CREATE TRIGGER derivative_jobs_guard_mutation "
    "BEFORE UPDATE OR DELETE ON derivative_jobs FOR EACH ROW "
    "EXECUTE FUNCTION gen_automation_guard_derivative_job_mutation()",
    "CREATE TRIGGER derivative_jobs_promote_release_after_success "
    "AFTER UPDATE ON derivative_jobs FOR EACH ROW "
    "EXECUTE FUNCTION gen_automation_promote_rendered_release()",
    "CREATE TRIGGER derivative_outputs_guard_mutation "
    "BEFORE INSERT OR UPDATE OR DELETE ON derivative_outputs FOR EACH ROW "
    "EXECUTE FUNCTION gen_automation_guard_derivative_output_mutation()",
    "CREATE TRIGGER asset_lineage_guard_derivative_mutation "
    "BEFORE UPDATE OR DELETE ON asset_lineage FOR EACH ROW "
    "EXECUTE FUNCTION gen_automation_guard_recorded_lineage_mutation()",
):
    event.listen(
        DerivativeOutput.__table__,
        "after_create",
        _ddl(_statement).execute_if(dialect="postgresql"),
    )
