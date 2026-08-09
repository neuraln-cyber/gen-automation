from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
)

from gen_automation.domain.deliverability import (
    MAX_ACCEPTED_IMAGES_PER_RELEASE,
    PATREON_MAX_ARCHIVE_PARTS,
)
from gen_automation.domain.enums import (
    PublicationAttemptState,
    PublicationIntentState,
    PublicationStepKind,
    PublicationStepState,
    PublicationTarget,
)

Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class StrictPublicationModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PublicationIntentCreate(StrictPublicationModel):
    release_version_id: UUID
    target: PublicationTarget
    configuration: dict[str, Any]
    derivative_output_ids: list[UUID] = Field(
        min_length=1,
        max_length=MAX_ACCEPTED_IMAGES_PER_RELEASE,
    )
    scheduled_at: datetime | None = None
    credential_reference: str | None = Field(default=None, max_length=500)
    public_preview_output_id: UUID | None = None
    public_preview_attester_name: str | None = Field(
        default=None,
        max_length=256,
    )
    public_preview_attested_at: datetime | None = None
    public_preview_attestation_timezone: str | None = Field(
        default=None,
        max_length=100,
    )


class PublicationApprovalCreate(StrictPublicationModel):
    expected_intent_digest: Sha256
    expected_lock_version: int = Field(ge=1)
    attestation: str = Field(min_length=1, max_length=500)
    approval_seconds: int = Field(default=900, ge=60, le=3600)


class PublicationRevocationCreate(StrictPublicationModel):
    expected_intent_digest: Sha256
    expected_lock_version: int = Field(ge=1)
    attestation: str = Field(min_length=1, max_length=500)


class PublicationCancellationCreate(StrictPublicationModel):
    expected_intent_digest: Sha256
    expected_lock_version: int = Field(ge=1)
    attestation: str = Field(min_length=1, max_length=500)


class PublicationGuardChange(StrictPublicationModel):
    enabled: bool
    expected_epoch: int = Field(ge=1)
    expected_lock_version: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=500)


class PublicationConfirmPresent(StrictPublicationModel):
    expected_intent_digest: Sha256
    expected_lock_version: int = Field(ge=1)
    remote_identifier: str = Field(min_length=1, max_length=200)
    remote_url: str = Field(min_length=1, max_length=2048)
    evidence: str = Field(min_length=1, max_length=65536)
    attestation: str = Field(min_length=1, max_length=500)


class PublicationConfirmAbsent(StrictPublicationModel):
    expected_intent_digest: Sha256
    expected_lock_version: int = Field(ge=1)
    evidence: str = Field(min_length=1, max_length=65536)
    attestation: str = Field(min_length=1, max_length=500)


class PatreonPackageDownloadCreate(StrictPublicationModel):
    expected_intent_digest: Sha256
    expected_lock_version: int = Field(ge=1)
    expires_in_seconds: int = Field(default=300, ge=30, le=900)
    part_number: int = Field(default=1, ge=1, le=PATREON_MAX_ARCHIVE_PARTS)


class PublicationIntentMutationRead(StrictPublicationModel):
    intent_id: UUID
    release_id: UUID
    release_version_id: UUID
    target: PublicationTarget
    state: PublicationIntentState
    configuration_sha256: Sha256
    input_manifest_sha256: Sha256
    intent_digest: Sha256
    input_count: int
    scheduled_at: datetime | None
    lock_version: int
    replayed: bool


class PublicationApprovalRead(StrictPublicationModel):
    intent_id: UUID
    approval_id: UUID
    attempt_id: UUID
    approval_revision: int
    intent_lock_version: int
    expires_at: datetime
    state: PublicationIntentState
    replayed: bool


class PublicationRevocationRead(StrictPublicationModel):
    intent_id: UUID
    approval_id: UUID
    approval_revision: int
    intent_lock_version: int
    state: PublicationIntentState
    replayed: bool


class PublicationCancellationRead(StrictPublicationModel):
    intent_id: UUID
    intent_lock_version: int
    state: PublicationIntentState
    replayed: bool


class PublicationGuardRead(StrictPublicationModel):
    enabled: bool
    epoch: int
    lock_version: int
    changed_at: datetime


class PublicationReconciliationRead(StrictPublicationModel):
    intent_id: UUID
    reconciliation_id: UUID
    outcome: str
    state: PublicationIntentState
    lock_version: int
    replayed: bool


class PatreonPackageDownloadRead(StrictPublicationModel):
    intent_id: UUID
    package_id: UUID
    url: str
    filename: str
    sha256: Sha256
    manifest_sha256: Sha256
    byte_size: int
    expires_at: datetime
    part_number: int
    part_count: int
    first_ordinal: int
    last_ordinal: int


class PublicationInputRead(StrictPublicationModel):
    id: UUID
    ordinal: int
    role: str
    derivative_output_id: UUID
    derivative_target: str
    asset_id: UUID
    asset_sha256: Sha256
    content_type: str
    width: int
    height: int
    byte_size: int


class PublicationStepRead(StrictPublicationModel):
    id: UUID
    ordinal: int
    kind: PublicationStepKind
    state: PublicationStepState
    retry_count: int
    max_retries: int
    retry_at: datetime | None
    effect_started_at: datetime | None
    effect_completed_at: datetime | None
    remote_identifier: str | None
    remote_expires_at: datetime | None
    package_id: UUID | None
    last_error_code: str | None
    last_error_detail: str | None


class PublicationAttemptRead(StrictPublicationModel):
    id: UUID
    attempt_no: int
    approval_id: UUID
    state: PublicationAttemptState
    attempt_count: int
    max_attempts: int
    available_at: datetime
    retry_at: datetime | None
    created_at: datetime
    completed_at: datetime | None
    last_error_code: str | None
    last_error_detail: str | None
    steps: list[PublicationStepRead]


class PublicationPackageRead(StrictPublicationModel):
    id: UUID
    part_number: int
    part_count: int
    first_ordinal: int
    last_ordinal: int
    sha256: Sha256
    manifest_sha256: Sha256
    byte_size: int
    content_type: str
    created_at: datetime


class PublicationIntentRead(StrictPublicationModel):
    id: UUID
    release_id: UUID
    release_version_id: UUID
    target: PublicationTarget
    state: PublicationIntentState
    configuration: dict[str, Any]
    configuration_sha256: Sha256
    input_manifest_sha256: Sha256
    intent_digest: Sha256
    input_count: int
    scheduled_at: datetime | None
    public_preview_attester_name: str | None
    public_preview_attested_at: datetime | None
    public_preview_attestation_timezone: str | None
    planned_at: datetime
    lock_version: int
    completed_at: datetime | None
    last_error_code: str | None
    last_error_detail: str | None
    inputs: list[PublicationInputRead]
    attempts: list[PublicationAttemptRead]
    package: PublicationPackageRead | None
    packages: list[PublicationPackageRead]
