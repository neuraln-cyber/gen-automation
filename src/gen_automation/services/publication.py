from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import Any, Literal, cast
from urllib.parse import urlsplit
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import ValidationError
from sqlalchemy import and_, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from gen_automation.db.models import (
    AdminUser,
    Asset,
    AuditEvent,
    DerivativeJob,
    DerivativeOutput,
    IdempotencyRecord,
    PublicationApproval,
    PublicationAttempt,
    PublicationInput,
    PublicationIntent,
    PublicationPackage,
    PublicationProviderGuard,
    PublicationReconciliation,
    PublicationStep,
    Release,
    ReleaseSelection,
    ReleaseVersion,
    ReviewTask,
    ReviewXSelection,
)
from gen_automation.domain.canonical import canonical_json_bytes, canonical_sha256
from gen_automation.domain.deliverability import (
    MAX_ACCEPTED_IMAGES_PER_RELEASE,
    PATREON_MAX_ARCHIVE_PARTS,
)
from gen_automation.domain.enums import (
    AdminRole,
    AssetKind,
    AssetState,
    DerivativeJobState,
    PublicationApprovalAction,
    PublicationAttemptState,
    PublicationIntentState,
    PublicationRetryClass,
    PublicationStepKind,
    PublicationStepState,
    PublicationTarget,
    ReleasePhase,
    ReviewTaskState,
)
from gen_automation.domain.ids import uuid7
from gen_automation.domain.release_spec import ReleaseSpecification
from gen_automation.integrations.patreon.handoff import (
    PATREON_MAX_BODY_BYTES,
    PATREON_MAX_DERIVATIVE_IMAGES,
    PATREON_MAX_IMAGE_BYTES,
    PATREON_MAX_TAG_BYTES,
    PATREON_MAX_TAGS,
    PATREON_MAX_TIER_BYTES,
    PATREON_MAX_TITLE_BYTES,
    PATREON_MAX_TOTAL_IMAGE_BYTES,
    PATREON_PUBLIC_PREVIEW_ATTESTATION,
)
from gen_automation.integrations.x.client import (
    X_MAX_MEDIA_PER_POST,
    X_MAX_POST_TEXT_BYTES,
    X_MAX_STATIC_IMAGE_BYTES,
)
from gen_automation.services.compliance import (
    ReleaseApprovalError,
    validate_release_approvals,
)
from gen_automation.storage.base import ObjectStore

PUBLICATION_EFFECT_APPROVAL_ATTESTATION = (
    "I approve this exact frozen publication intent and authorize its external "
    "effects until the recorded approval expiry."
)
PUBLICATION_REVOCATION_ATTESTATION = (
    "I revoke external-effect authorization for this exact publication intent."
)
PUBLICATION_CONFIRM_PRESENT_ATTESTATION = (
    "I manually verified that this exact publication exists at the recorded "
    "provider post ID and URL."
)
PUBLICATION_CONFIRM_ABSENT_ATTESTATION = (
    "I manually investigated the unknown X outcome and found evidence that the "
    "post was not created. I understand this confirmation does not publish or retry it."
)
PUBLICATION_CONFIRM_PATREON_ABSENT_ATTESTATION = (
    "I manually investigated the unknown Patreon outcome and found evidence that "
    "the post was not created. I understand this confirmation does not publish or "
    "retry it and authorizes access to the manual package handoff."
)

_PUBLISHER_ROLES = frozenset({AdminRole.OWNER, AdminRole.PUBLISHER})
_CREDENTIAL_REFERENCE = re.compile(
    r"^(?:env|vault|aws-secrets-manager|gcp-secret-manager|test)://"
    r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,450}$"
)
_SNOWFLAKE = re.compile(r"^[0-9]{1,19}$")
_PATREON_POST_ID = re.compile(r"(?:^|-)([0-9]{1,20})$")
_MAX_CONFIGURATION_BYTES = 256 * 1024
_MAX_EVIDENCE_BYTES = 64 * 1024
_MAX_SAFE_ERROR_BYTES = 500
_MAX_APPROVAL_SECONDS = 3_600
_MIN_APPROVAL_SECONDS = 60
_PLAN_IDEMPOTENCY_DAYS = 30
_PUBLISHABLE_RELEASE_PHASES = frozenset(
    {
        ReleasePhase.RENDERING,
        ReleasePhase.READY_TO_PUBLISH,
        ReleasePhase.PUBLISHING,
        ReleasePhase.PUBLISHED,
    }
)
_SUPERSEDABLE_INTENT_STATES = frozenset(
    {
        PublicationIntentState.FAILED,
        PublicationIntentState.CANCELLED,
    }
)

type PublicationInputRole = Literal[
    "x_teaser",
    "patreon_content",
    "patreon_preview",
]


class PublicationError(Exception):
    """Base error for publication control-plane operations."""


class PublicationInputError(PublicationError):
    """Caller input is malformed or violates a publication contract."""


class PublicationNotFoundError(PublicationError):
    """The requested durable publication resource does not exist."""


class PublicationConflictError(PublicationError):
    """Current durable state does not permit the requested transition."""


class PublicationDisabledError(PublicationConflictError):
    """The durable global publication guard is stopped."""


@dataclass(frozen=True, slots=True)
class PublicationIntentResult:
    intent_id: UUID
    release_id: UUID
    release_version_id: UUID
    target: PublicationTarget
    state: PublicationIntentState
    configuration_sha256: str
    input_manifest_sha256: str
    intent_digest: str
    input_count: int
    scheduled_at: datetime | None
    lock_version: int
    replayed: bool


@dataclass(frozen=True, slots=True)
class PublicationApprovalResult:
    intent_id: UUID
    approval_id: UUID
    attempt_id: UUID
    approval_revision: int
    intent_lock_version: int
    expires_at: datetime
    state: PublicationIntentState
    replayed: bool


@dataclass(frozen=True, slots=True)
class PublicationRevocationResult:
    intent_id: UUID
    approval_id: UUID
    approval_revision: int
    intent_lock_version: int
    state: PublicationIntentState
    replayed: bool


@dataclass(frozen=True, slots=True)
class PublicationGuardResult:
    enabled: bool
    epoch: int
    lock_version: int
    changed_at: datetime


@dataclass(frozen=True, slots=True)
class PublicationReconciliationResult:
    intent_id: UUID
    reconciliation_id: UUID
    outcome: str
    state: PublicationIntentState
    lock_version: int
    replayed: bool


@dataclass(frozen=True, slots=True)
class PatreonPackageDownloadResult:
    intent_id: UUID
    package_id: UUID
    url: str
    filename: str
    sha256: str
    manifest_sha256: str
    byte_size: int
    expires_at: datetime
    part_number: int
    part_count: int
    first_ordinal: int
    last_ordinal: int


@dataclass(frozen=True, slots=True)
class FinishedSetPackageDownloadResult:
    review_task_id: UUID
    intent_id: UUID
    package_id: UUID
    url: str
    filename: str
    sha256: str
    manifest_sha256: str
    byte_size: int
    expires_at: datetime
    part_number: int
    part_count: int
    first_ordinal: int
    last_ordinal: int


@dataclass(frozen=True, slots=True)
class _FrozenOutput:
    output: DerivativeOutput
    role: PublicationInputRole

    def manifest_record(self, ordinal: int) -> dict[str, object]:
        output = self.output
        return {
            "ordinal": ordinal,
            "role": self.role,
            "derivative_output_id": str(output.id),
            "derivative_recipe_id": str(output.derivative_recipe_id),
            "asset_id": str(output.asset_id),
            "derivative_target": output.target,
            "storage_backend": output.asset_storage_backend,
            "storage_bucket": output.asset_storage_bucket,
            "object_key": output.asset_object_key,
            "object_version_id": output.asset_object_version_id,
            "sha256": output.asset_sha256,
            "content_type": output.asset_content_type,
            "image_format": output.asset_image_format,
            "width": output.asset_width,
            "height": output.asset_height,
            "byte_size": output.asset_byte_size,
        }


async def plan_publication_intent(
    session: AsyncSession,
    *,
    release_version_id: UUID,
    target: PublicationTarget | str,
    configuration: Mapping[str, Any],
    derivative_output_ids: Sequence[UUID],
    planned_by_user_id: UUID,
    idempotency_key: str,
    scheduled_at: datetime | None = None,
    credential_reference: str | None = None,
    public_preview_output_id: UUID | None = None,
    public_preview_attester_name: str | None = None,
    public_preview_attested_at: datetime | None = None,
    public_preview_attestation_timezone: str | None = None,
    now: datetime | None = None,
) -> PublicationIntentResult:
    """Freeze one provider intent against the current ready release version."""

    planned_at = _utc_now(now)
    publication_target_value = _target(target)
    normalized_key = _bounded_text(idempotency_key, "idempotency key", 200)
    normalized_configuration = _normalize_configuration(
        publication_target_value,
        configuration,
    )
    normalized_schedule = _optional_future_datetime(
        scheduled_at,
        now=planned_at,
        label="scheduled_at",
    )
    normalized_outputs = _normalize_output_ids(derivative_output_ids)
    actor = await _require_publisher(session, planned_by_user_id)

    attestation = _normalize_preview_attestation(
        target=publication_target_value,
        preview_output_id=public_preview_output_id,
        attester_name=public_preview_attester_name,
        attested_at=public_preview_attested_at,
        timezone_name=public_preview_attestation_timezone,
        attester_user_id=actor.id,
        now=planned_at,
    )
    normalized_credential_reference = _normalize_credential_reference(
        publication_target_value,
        credential_reference,
    )

    release_row = (
        await session.execute(
            select(Release, ReleaseVersion)
            .join(ReleaseVersion, ReleaseVersion.release_id == Release.id)
            .where(ReleaseVersion.id == release_version_id)
            .with_for_update()
        )
    ).one_or_none()
    if release_row is None:
        raise PublicationNotFoundError("release version was not found")
    release, release_version = release_row
    _require_current_publishable_release(release, release_version)
    await _require_current_compliance_approvals(session, release_version)

    frozen_outputs = await _load_frozen_outputs(
        session,
        release_version_id=release_version.id,
        target=publication_target_value,
        derivative_output_ids=normalized_outputs,
        public_preview_output_id=public_preview_output_id,
    )
    input_manifest = [
        item.manifest_record(index) for index, item in enumerate(frozen_outputs, start=1)
    ]
    input_manifest_sha256 = canonical_sha256(
        {
            "schema": "publication-input-manifest/v1",
            "release_version_id": str(release_version.id),
            "target": publication_target_value.value,
            "inputs": input_manifest,
        }
    )
    configuration_sha256 = canonical_sha256(normalized_configuration)
    identity = {
        "schema": "publication-intent/v1",
        "release_id": str(release.id),
        "release_version_id": str(release_version.id),
        "release_version_no": release_version.version_no,
        "release_specification_sha256": release_version.specification_sha256,
        "target": publication_target_value.value,
        "configuration_sha256": configuration_sha256,
        "input_manifest_sha256": input_manifest_sha256,
        "input_count": len(frozen_outputs),
        "scheduled_at": _canonical_datetime(normalized_schedule),
        "credential_reference": normalized_credential_reference,
        "public_preview_attestation_sha256": (
            attestation["sha256"] if attestation is not None else None
        ),
    }
    intent_digest = canonical_sha256(identity)
    request_sha256 = canonical_sha256(
        {
            "schema": "publication-plan-request/v1",
            **identity,
            "intent_digest": intent_digest,
            "planned_by_user_id": str(planned_by_user_id),
        }
    )
    scope = (
        f"release-version:{release_version.id}:publication:{publication_target_value.value}:plan"
    )
    target_intents = tuple(
        (
            await session.scalars(
                select(PublicationIntent)
                .where(
                    PublicationIntent.release_id == release.id,
                    PublicationIntent.target == publication_target_value,
                )
                .order_by(PublicationIntent.planned_at.desc(), PublicationIntent.id.desc())
                .with_for_update()
            )
        ).all()
    )
    replay = await _intent_replay(
        session,
        scope=scope,
        idempotency_key=normalized_key,
        request_sha256=request_sha256,
    )
    if replay is not None:
        replay_intent = next(
            (intent for intent in target_intents if intent.id == replay.intent_id),
            None,
        )
        if replay_intent is None:
            raise PublicationConflictError("publication idempotency snapshot is unavailable")
        _require_single_target_owner(target_intents, owner_intent_id=replay_intent.id)
        return replay
    existing = next(
        (intent for intent in target_intents if intent.intent_digest == intent_digest),
        None,
    )
    if existing is not None:
        if (
            existing.intent_digest != intent_digest
            or existing.input_manifest_sha256 != input_manifest_sha256
            or existing.configuration != normalized_configuration
        ):
            raise PublicationConflictError(
                "the provider/configuration identity is already frozen with different inputs"
            )
        _require_single_target_owner(target_intents, owner_intent_id=existing.id)
        result = _intent_result(existing, replayed=True)
        session.add(
            _idempotency_record(
                scope=scope,
                key=normalized_key,
                request_sha256=request_sha256,
                status=200,
                response=_intent_response(result),
                now=planned_at,
            )
        )
        await session.commit()
        return result

    if any(intent.state not in _SUPERSEDABLE_INTENT_STATES for intent in target_intents):
        raise PublicationConflictError(
            "another publication intent already owns this release target"
        )

    intent = PublicationIntent(
        id=uuid7(),
        release_id=release.id,
        release_version_id=release_version.id,
        target=publication_target_value,
        state=PublicationIntentState.AWAITING_APPROVAL,
        configuration=normalized_configuration,
        configuration_sha256=configuration_sha256,
        input_manifest_sha256=input_manifest_sha256,
        intent_digest=intent_digest,
        input_count=len(frozen_outputs),
        credential_reference=normalized_credential_reference,
        scheduled_at=normalized_schedule,
        public_preview_attester_name=(
            str(attestation["attester_name"]) if attestation is not None else None
        ),
        public_preview_attester_user_id=(actor.id if attestation is not None else None),
        public_preview_attested_at=(
            attestation["attested_at"] if attestation is not None else None
        ),
        public_preview_attestation_timezone=(
            str(attestation["timezone"]) if attestation is not None else None
        ),
        public_preview_attestation_sha256=(
            str(attestation["sha256"]) if attestation is not None else None
        ),
        planned_by_user_id=actor.id,
        planned_at=planned_at,
        lock_version=1,
    )
    session.add(intent)
    try:
        await session.flush()
        for ordinal, frozen in enumerate(frozen_outputs, start=1):
            output = frozen.output
            session.add(
                PublicationInput(
                    id=uuid7(),
                    intent_id=intent.id,
                    ordinal=ordinal,
                    role=frozen.role,
                    derivative_output_id=output.id,
                    derivative_recipe_id=output.derivative_recipe_id,
                    asset_id=output.asset_id,
                    derivative_target=output.target,
                    asset_storage_backend=output.asset_storage_backend,
                    asset_storage_bucket=output.asset_storage_bucket,
                    asset_object_key=output.asset_object_key,
                    asset_object_version_id=output.asset_object_version_id,
                    asset_sha256=output.asset_sha256,
                    asset_content_type=output.asset_content_type,
                    asset_image_format=output.asset_image_format,
                    asset_width=output.asset_width,
                    asset_height=output.asset_height,
                    asset_byte_size=output.asset_byte_size,
                    frozen_at=planned_at,
                )
            )
        await session.flush()
    except IntegrityError as error:
        await session.rollback()
        raise PublicationConflictError(
            "publication intent was created concurrently or its snapshot became stale"
        ) from error

    result = _intent_result(intent, replayed=False)
    session.add(
        _audit(
            actor_id=actor.id,
            action="publication.intent_planned",
            resource_id=intent.id,
            correlation_id=normalized_key,
            detail={
                "target": publication_target_value.value,
                "release_id": str(release.id),
                "release_version_id": str(release_version.id),
                "configuration_sha256": configuration_sha256,
                "input_manifest_sha256": input_manifest_sha256,
                "intent_digest": intent_digest,
                "input_count": len(frozen_outputs),
                "has_schedule": normalized_schedule is not None,
                "has_public_preview_attestation": attestation is not None,
            },
            now=planned_at,
        )
    )
    session.add(
        _idempotency_record(
            scope=scope,
            key=normalized_key,
            request_sha256=request_sha256,
            status=201,
            response=_intent_response(result),
            now=planned_at,
        )
    )
    await session.commit()
    return result


async def approve_publication_intent(
    session: AsyncSession,
    *,
    intent_id: UUID,
    expected_intent_digest: str,
    expected_lock_version: int,
    actor_user_id: UUID,
    actor_role: AdminRole | str,
    attestation: str,
    approval_seconds: int,
    idempotency_key: str,
    max_attempts: int = 5,
    upload_max_retries: int = 3,
    package_max_retries: int = 3,
    now: datetime | None = None,
) -> PublicationApprovalResult:
    approved_at = _utc_now(now)
    normalized_key = _bounded_text(idempotency_key, "idempotency key", 200)
    normalized_digest = _sha256(expected_intent_digest, "intent digest")
    normalized_lock = _positive_int(expected_lock_version, "expected lock version", 1_000_000_000)
    normalized_approval_seconds = _bounded_int(
        approval_seconds,
        "approval seconds",
        _MIN_APPROVAL_SECONDS,
        _MAX_APPROVAL_SECONDS,
    )
    normalized_max_attempts = _bounded_int(max_attempts, "maximum attempts", 1, 100)
    normalized_upload_retries = _bounded_int(
        upload_max_retries,
        "upload maximum retries",
        0,
        20,
    )
    normalized_package_retries = _bounded_int(
        package_max_retries,
        "package maximum retries",
        0,
        20,
    )
    if attestation != PUBLICATION_EFFECT_APPROVAL_ATTESTATION:
        raise PublicationInputError("the exact publication approval attestation is required")
    actor = await _require_publisher(session, actor_user_id, asserted_role=actor_role)

    request_sha256 = canonical_sha256(
        {
            "schema": "publication-approval-request/v1",
            "intent_id": str(intent_id),
            "intent_digest": normalized_digest,
            "expected_lock_version": normalized_lock,
            "actor_user_id": str(actor.id),
            "actor_role": actor.role.value,
            "attestation_sha256": canonical_sha256(attestation),
            "approval_seconds": normalized_approval_seconds,
            "max_attempts": normalized_max_attempts,
            "upload_max_retries": normalized_upload_retries,
            "package_max_retries": normalized_package_retries,
        }
    )
    scope = f"publication-intent:{intent_id}:approve"
    replay = await _approval_replay(
        session,
        scope=scope,
        idempotency_key=normalized_key,
        request_sha256=request_sha256,
    )
    if replay is not None:
        return replay

    intent = await session.scalar(
        select(PublicationIntent).where(PublicationIntent.id == intent_id).with_for_update()
    )
    if intent is None:
        raise PublicationNotFoundError("publication intent was not found")
    if intent.intent_digest != normalized_digest or intent.lock_version != normalized_lock:
        raise PublicationConflictError("publication intent digest or lock is stale")
    if intent.state not in {
        PublicationIntentState.AWAITING_APPROVAL,
        PublicationIntentState.FAILED,
        PublicationIntentState.CANCELLED,
    }:
        raise PublicationConflictError("publication intent is not awaiting approval")

    release, release_version = await _load_intent_release(session, intent, lock=True)
    _require_current_publishable_release(release, release_version)
    await _require_current_compliance_approvals(session, release_version)
    inputs = tuple(
        (
            await session.scalars(
                select(PublicationInput)
                .where(PublicationInput.intent_id == intent.id)
                .order_by(PublicationInput.ordinal)
            )
        ).all()
    )
    _validate_frozen_input_set(intent, inputs)
    expires_at = approved_at + timedelta(seconds=normalized_approval_seconds)
    available_at = _initial_attempt_available_at(
        target=intent.target,
        scheduled_at=intent.scheduled_at,
        approved_at=approved_at,
    )
    if available_at >= expires_at:
        raise PublicationConflictError("approval would expire before the scheduled external effect")
    revision = (
        int(
            await session.scalar(
                select(func.max(PublicationApproval.revision)).where(
                    PublicationApproval.intent_id == intent.id
                )
            )
            or 0
        )
        + 1
    )
    approval = PublicationApproval(
        id=uuid7(),
        intent_id=intent.id,
        revision=revision,
        action=PublicationApprovalAction.APPROVE,
        intent_digest=intent.intent_digest,
        intent_lock_version=intent.lock_version + 1,
        actor_user_id=actor.id,
        actor_role=actor.role.value,
        recorded_at=approved_at,
        expires_at=expires_at,
        attestation_sha256=canonical_sha256(attestation),
    )
    session.add(approval)
    try:
        # PostgreSQL's insert guard validates the prospective post-approval lock
        # while the immutable intent still holds its pre-approval lock.
        await session.flush()
    except IntegrityError as error:
        await session.rollback()
        raise PublicationConflictError("publication approval snapshot is invalid") from error

    attempt_no = (
        int(
            await session.scalar(
                select(func.max(PublicationAttempt.attempt_no)).where(
                    PublicationAttempt.intent_id == intent.id
                )
            )
            or 0
        )
        + 1
    )
    attempt = PublicationAttempt(
        id=uuid7(),
        intent_id=intent.id,
        approval_id=approval.id,
        attempt_no=attempt_no,
        state=PublicationAttemptState.QUEUED,
        attempt_count=0,
        max_attempts=normalized_max_attempts,
        lock_version=1,
        available_at=available_at,
        created_at=approved_at,
    )
    session.add(attempt)
    await session.flush()
    if intent.target == PublicationTarget.X:
        for ordinal, publication_input in enumerate(inputs, start=1):
            session.add(
                PublicationStep(
                    id=uuid7(),
                    attempt_id=attempt.id,
                    ordinal=ordinal,
                    kind=PublicationStepKind.X_MEDIA_UPLOAD,
                    publication_input_id=publication_input.id,
                    state=PublicationStepState.PENDING,
                    retry_count=0,
                    max_retries=normalized_upload_retries,
                    lock_version=1,
                    updated_at=approved_at,
                )
            )
        session.add(
            PublicationStep(
                id=uuid7(),
                attempt_id=attempt.id,
                ordinal=len(inputs) + 1,
                kind=PublicationStepKind.X_CREATE_POST,
                state=PublicationStepState.PENDING,
                retry_count=0,
                max_retries=0,
                lock_version=1,
                updated_at=approved_at,
            )
        )
    else:
        session.add_all(
            [
                PublicationStep(
                    id=uuid7(),
                    attempt_id=attempt.id,
                    ordinal=1,
                    kind=PublicationStepKind.PATREON_PACKAGE,
                    state=PublicationStepState.PENDING,
                    retry_count=0,
                    max_retries=normalized_package_retries,
                    lock_version=1,
                    updated_at=approved_at,
                ),
                PublicationStep(
                    id=uuid7(),
                    attempt_id=attempt.id,
                    ordinal=2,
                    kind=PublicationStepKind.PATREON_HANDOFF,
                    state=PublicationStepState.PENDING,
                    retry_count=0,
                    max_retries=0,
                    lock_version=1,
                    updated_at=approved_at,
                ),
            ]
        )

    intent.state = PublicationIntentState.READY
    intent.lock_version += 1
    intent.completed_at = None
    intent.last_error_code = None
    intent.last_error_detail = None
    result = PublicationApprovalResult(
        intent_id=intent.id,
        approval_id=approval.id,
        attempt_id=attempt.id,
        approval_revision=approval.revision,
        intent_lock_version=intent.lock_version,
        expires_at=expires_at,
        state=intent.state,
        replayed=False,
    )
    session.add(
        _audit(
            actor_id=actor.id,
            action="publication.intent_approved",
            resource_id=intent.id,
            correlation_id=normalized_key,
            detail={
                "target": intent.target.value,
                "intent_digest": intent.intent_digest,
                "intent_lock_version": intent.lock_version,
                "approval_id": str(approval.id),
                "approval_revision": approval.revision,
                "attempt_id": str(attempt.id),
                "attempt_no": attempt.attempt_no,
                "expires_at": _canonical_datetime(expires_at),
            },
            now=approved_at,
        )
    )
    session.add(
        _idempotency_record(
            scope=scope,
            key=normalized_key,
            request_sha256=request_sha256,
            status=201,
            response=_approval_response(result),
            now=approved_at,
        )
    )
    try:
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        raise PublicationConflictError("publication approval was created concurrently") from error
    return result


async def revoke_publication_intent(
    session: AsyncSession,
    *,
    intent_id: UUID,
    expected_intent_digest: str,
    expected_lock_version: int,
    actor_user_id: UUID,
    actor_role: AdminRole | str,
    attestation: str,
    idempotency_key: str,
    now: datetime | None = None,
) -> PublicationRevocationResult:
    revoked_at = _utc_now(now)
    normalized_key = _bounded_text(idempotency_key, "idempotency key", 200)
    normalized_digest = _sha256(expected_intent_digest, "intent digest")
    normalized_lock = _positive_int(expected_lock_version, "expected lock version", 1_000_000_000)
    if attestation != PUBLICATION_REVOCATION_ATTESTATION:
        raise PublicationInputError("the exact publication revocation attestation is required")
    actor = await _require_publisher(session, actor_user_id, asserted_role=actor_role)
    request_sha256 = canonical_sha256(
        {
            "schema": "publication-revocation-request/v1",
            "intent_id": str(intent_id),
            "intent_digest": normalized_digest,
            "expected_lock_version": normalized_lock,
            "actor_user_id": str(actor.id),
            "actor_role": actor.role.value,
            "attestation_sha256": canonical_sha256(attestation),
        }
    )
    scope = f"publication-intent:{intent_id}:revoke"
    replay = await _revocation_replay(
        session,
        scope=scope,
        idempotency_key=normalized_key,
        request_sha256=request_sha256,
    )
    if replay is not None:
        return replay

    intent = await session.scalar(
        select(PublicationIntent).where(PublicationIntent.id == intent_id).with_for_update()
    )
    if intent is None:
        raise PublicationNotFoundError("publication intent was not found")
    if intent.intent_digest != normalized_digest or intent.lock_version != normalized_lock:
        raise PublicationConflictError("publication intent digest or lock is stale")
    if intent.state in {
        PublicationIntentState.PUBLISHED,
        PublicationIntentState.CANCELLED,
    }:
        raise PublicationConflictError("publication intent can no longer be revoked")
    await _load_and_require_current_publishable_intent_release(session, intent)

    revision = (
        int(
            await session.scalar(
                select(func.max(PublicationApproval.revision)).where(
                    PublicationApproval.intent_id == intent.id
                )
            )
            or 0
        )
        + 1
    )
    revocation = PublicationApproval(
        id=uuid7(),
        intent_id=intent.id,
        revision=revision,
        action=PublicationApprovalAction.REVOKE,
        intent_digest=intent.intent_digest,
        intent_lock_version=intent.lock_version + 1,
        actor_user_id=actor.id,
        actor_role=actor.role.value,
        recorded_at=revoked_at,
        expires_at=None,
        attestation_sha256=canonical_sha256(attestation),
    )
    session.add(revocation)
    try:
        await session.flush()
    except IntegrityError as error:
        await session.rollback()
        raise PublicationConflictError("publication revocation snapshot is invalid") from error

    attempts = tuple(
        (
            await session.scalars(
                select(PublicationAttempt)
                .where(
                    PublicationAttempt.intent_id == intent.id,
                    PublicationAttempt.state.in_(
                        (
                            PublicationAttemptState.QUEUED,
                            PublicationAttemptState.CLAIMED,
                            PublicationAttemptState.PROCESSING,
                            PublicationAttemptState.RETRY_WAIT,
                        )
                    ),
                )
                .with_for_update()
            )
        ).all()
    )
    ambiguous = False
    for attempt in attempts:
        steps = tuple(
            (
                await session.scalars(
                    select(PublicationStep)
                    .where(PublicationStep.attempt_id == attempt.id)
                    .with_for_update()
                )
            ).all()
        )
        for step in steps:
            if step.effect_started_at is not None and step.effect_completed_at is None:
                step.state = PublicationStepState.UNKNOWN
                step.last_error_code = "revoked_during_effect"
                step.last_error_detail = "external effect outcome requires reconciliation"
                step.retry_at = None
                step.updated_at = revoked_at
                step.lock_version += 1
                ambiguous = True
            elif step.state in {
                PublicationStepState.PENDING,
                PublicationStepState.PROCESSING,
                PublicationStepState.RETRY_WAIT,
            }:
                step.state = PublicationStepState.CANCELLED
                step.retry_at = None
                step.updated_at = revoked_at
                step.lock_version += 1
        attempt.state = (
            PublicationAttemptState.UNKNOWN if ambiguous else PublicationAttemptState.CANCELLED
        )
        attempt.lease_owner = None
        attempt.lease_expires_at = None
        attempt.retry_at = None
        attempt.completed_at = None if ambiguous else revoked_at
        attempt.last_error_code = "revoked_during_effect" if ambiguous else "approval_revoked"
        attempt.last_error_detail = (
            "external effect outcome requires reconciliation"
            if ambiguous
            else "external-effect authorization was revoked"
        )
        attempt.lock_version += 1

    intent.state = (
        PublicationIntentState.UNKNOWN if ambiguous else PublicationIntentState.AWAITING_APPROVAL
    )
    intent.lock_version += 1
    intent.last_error_code = "revoked_during_effect" if ambiguous else None
    intent.last_error_detail = (
        "external effect outcome requires reconciliation" if ambiguous else None
    )
    result = PublicationRevocationResult(
        intent_id=intent.id,
        approval_id=revocation.id,
        approval_revision=revocation.revision,
        intent_lock_version=intent.lock_version,
        state=intent.state,
        replayed=False,
    )
    session.add(
        _audit(
            actor_id=actor.id,
            action="publication.authorization_revoked",
            resource_id=intent.id,
            correlation_id=normalized_key,
            detail={
                "target": intent.target.value,
                "intent_digest": intent.intent_digest,
                "intent_lock_version": intent.lock_version,
                "approval_id": str(revocation.id),
                "approval_revision": revocation.revision,
                "requires_reconciliation": ambiguous,
            },
            now=revoked_at,
        )
    )
    session.add(
        _idempotency_record(
            scope=scope,
            key=normalized_key,
            request_sha256=request_sha256,
            status=200,
            response=_revocation_response(result),
            now=revoked_at,
        )
    )
    await session.commit()
    return result


async def get_publication_guard(
    session: AsyncSession,
) -> PublicationGuardResult:
    guard = await session.scalar(
        select(PublicationProviderGuard).where(PublicationProviderGuard.provider == "global")
    )
    if guard is None:
        raise PublicationDisabledError("publication is stopped")
    return PublicationGuardResult(
        enabled=guard.enabled,
        epoch=guard.epoch,
        lock_version=guard.lock_version,
        changed_at=_as_utc(guard.changed_at),
    )


async def set_publication_guard(
    session: AsyncSession,
    *,
    enabled: bool,
    expected_epoch: int,
    expected_lock_version: int,
    reason: str,
    actor_user_id: UUID,
    actor_role: AdminRole | str,
    idempotency_key: str,
    now: datetime | None = None,
) -> PublicationGuardResult:
    changed_at = _utc_now(now)
    if not isinstance(enabled, bool):
        raise PublicationInputError("enabled must be a boolean")
    normalized_epoch = _positive_int(expected_epoch, "expected epoch", 1_000_000_000)
    normalized_lock = _positive_int(expected_lock_version, "expected lock version", 1_000_000_000)
    normalized_reason = _bounded_text(reason, "guard reason", 500)
    normalized_key = _bounded_text(idempotency_key, "idempotency key", 200)
    actor = await _require_owner(session, actor_user_id, asserted_role=actor_role)
    request_sha256 = canonical_sha256(
        {
            "schema": "publication-guard-change/v1",
            "enabled": enabled,
            "expected_epoch": normalized_epoch,
            "expected_lock_version": normalized_lock,
            "reason_sha256": canonical_sha256(normalized_reason),
            "actor_user_id": str(actor.id),
        }
    )
    scope = "publication-provider-guard:global:change"
    replay = await _guard_replay(
        session,
        scope=scope,
        idempotency_key=normalized_key,
        request_sha256=request_sha256,
    )
    if replay is not None:
        return replay

    guard = await session.scalar(
        select(PublicationProviderGuard)
        .where(PublicationProviderGuard.provider == "global")
        .with_for_update()
    )
    if guard is None:
        raise PublicationDisabledError("publication is stopped")
    if guard.epoch != normalized_epoch or guard.lock_version != normalized_lock:
        raise PublicationConflictError("publication guard epoch or lock is stale")
    if changed_at <= _as_utc(guard.changed_at):
        changed_at = _as_utc(guard.changed_at) + timedelta(microseconds=1)
    guard.enabled = enabled
    guard.epoch += 1
    guard.lock_version += 1
    guard.reason = normalized_reason
    guard.changed_by_user_id = actor.id
    guard.changed_at = changed_at
    result = PublicationGuardResult(
        enabled=guard.enabled,
        epoch=guard.epoch,
        lock_version=guard.lock_version,
        changed_at=changed_at,
    )
    session.add(
        _audit(
            actor_id=actor.id,
            action=("publication.guard_enabled" if enabled else "publication.guard_stopped"),
            resource_id=guard.id,
            correlation_id=normalized_key,
            detail={
                "enabled": enabled,
                "epoch": guard.epoch,
                "lock_version": guard.lock_version,
                "reason_sha256": canonical_sha256(normalized_reason),
            },
            now=changed_at,
        )
    )
    session.add(
        _idempotency_record(
            scope=scope,
            key=normalized_key,
            request_sha256=request_sha256,
            status=200,
            response=_guard_response(result),
            now=changed_at,
        )
    )
    try:
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        raise PublicationConflictError("publication guard was changed concurrently") from error
    return result


async def reconcile_publication_present(
    session: AsyncSession,
    *,
    intent_id: UUID,
    expected_intent_digest: str,
    expected_lock_version: int,
    remote_identifier: str,
    remote_url: str,
    evidence: str,
    attestation: str,
    actor_user_id: UUID,
    actor_role: AdminRole | str,
    idempotency_key: str,
    now: datetime | None = None,
) -> PublicationReconciliationResult:
    recorded_at = _utc_now(now)
    normalized_key = _bounded_text(idempotency_key, "idempotency key", 200)
    normalized_digest = _sha256(expected_intent_digest, "intent digest")
    normalized_lock = _positive_int(expected_lock_version, "expected lock version", 1_000_000_000)
    normalized_evidence = _bounded_text(
        evidence,
        "reconciliation evidence",
        _MAX_EVIDENCE_BYTES,
        byte_limit=True,
    )
    if attestation != PUBLICATION_CONFIRM_PRESENT_ATTESTATION:
        raise PublicationInputError("the exact confirmed-present attestation is required")
    actor = await _require_publisher(session, actor_user_id, asserted_role=actor_role)

    intent = await session.scalar(
        select(PublicationIntent).where(PublicationIntent.id == intent_id).with_for_update()
    )
    if intent is None:
        raise PublicationNotFoundError("publication intent was not found")
    if intent.target == PublicationTarget.PATREON:
        normalized_identifier, normalized_url = validate_patreon_post_identity(
            remote_identifier,
            remote_url,
        )
    else:
        normalized_identifier, normalized_url = _validate_x_post_identity(
            remote_identifier,
            remote_url,
        )
    request_sha256 = canonical_sha256(
        {
            "schema": "publication-confirm-present/v1",
            "intent_id": str(intent.id),
            "intent_digest": normalized_digest,
            "expected_lock_version": normalized_lock,
            "remote_identifier": normalized_identifier,
            "remote_url_sha256": canonical_sha256(normalized_url),
            "evidence_sha256": canonical_sha256(normalized_evidence),
            "attestation_sha256": canonical_sha256(attestation),
            "actor_user_id": str(actor.id),
        }
    )
    scope = f"publication-intent:{intent.id}:reconcile-present"
    replay = await _reconciliation_replay(
        session,
        scope=scope,
        idempotency_key=normalized_key,
        request_sha256=request_sha256,
    )
    if replay is not None:
        return replay
    _require_expected_intent(intent, normalized_digest, normalized_lock)
    if intent.target == PublicationTarget.PATREON:
        if intent.state not in {
            PublicationIntentState.AWAITING_HUMAN,
            PublicationIntentState.UNKNOWN,
        }:
            raise PublicationConflictError("Patreon outcome is not awaiting confirmation")
    elif intent.state != PublicationIntentState.UNKNOWN:
        raise PublicationConflictError("X outcome is not awaiting reconciliation")

    reconciliation = await _append_reconciliation(
        session,
        intent=intent,
        outcome="confirmed_present",
        actor=actor,
        evidence=normalized_evidence,
        attestation=attestation,
        remote_identifier=normalized_identifier,
        remote_url=normalized_url,
        recorded_at=recorded_at,
    )
    await session.flush()
    await _mark_manual_confirmation_succeeded(
        session,
        intent=intent,
        remote_identifier=normalized_identifier,
        remote_url=normalized_url,
        now=recorded_at,
    )
    intent.state = PublicationIntentState.PUBLISHED
    intent.completed_at = recorded_at
    intent.last_error_code = None
    intent.last_error_detail = None
    intent.lock_version += 1
    result = PublicationReconciliationResult(
        intent_id=intent.id,
        reconciliation_id=reconciliation.id,
        outcome=reconciliation.outcome,
        state=intent.state,
        lock_version=intent.lock_version,
        replayed=False,
    )
    session.add(
        _audit(
            actor_id=actor.id,
            action="publication.outcome_confirmed_present",
            resource_id=intent.id,
            correlation_id=normalized_key,
            detail={
                "target": intent.target.value,
                "intent_digest": intent.intent_digest,
                "lock_version": intent.lock_version,
                "reconciliation_id": str(reconciliation.id),
                "remote_identifier_sha256": canonical_sha256(normalized_identifier),
                "remote_url_sha256": canonical_sha256(normalized_url),
                "evidence_sha256": reconciliation.evidence_sha256,
                "attestation_sha256": reconciliation.attestation_sha256,
            },
            now=recorded_at,
        )
    )
    session.add(
        _idempotency_record(
            scope=scope,
            key=normalized_key,
            request_sha256=request_sha256,
            status=200,
            response=_reconciliation_response(result),
            now=recorded_at,
        )
    )
    await session.commit()
    return result


async def reconcile_publication_absent(
    session: AsyncSession,
    *,
    intent_id: UUID,
    expected_intent_digest: str,
    expected_lock_version: int,
    evidence: str,
    attestation: str,
    actor_user_id: UUID,
    actor_role: AdminRole | str,
    idempotency_key: str,
    now: datetime | None = None,
) -> PublicationReconciliationResult:
    """Record absence evidence without retrying or creating a provider post."""

    recorded_at = _utc_now(now)
    normalized_key = _bounded_text(idempotency_key, "idempotency key", 200)
    normalized_digest = _sha256(expected_intent_digest, "intent digest")
    normalized_lock = _positive_int(expected_lock_version, "expected lock version", 1_000_000_000)
    normalized_evidence = _bounded_text(
        evidence,
        "reconciliation evidence",
        _MAX_EVIDENCE_BYTES,
        byte_limit=True,
    )
    actor = await _require_publisher(session, actor_user_id, asserted_role=actor_role)
    intent = await session.scalar(
        select(PublicationIntent).where(PublicationIntent.id == intent_id).with_for_update()
    )
    if intent is None:
        raise PublicationNotFoundError("publication intent was not found")
    expected_attestation = (
        PUBLICATION_CONFIRM_PATREON_ABSENT_ATTESTATION
        if intent.target == PublicationTarget.PATREON
        else PUBLICATION_CONFIRM_ABSENT_ATTESTATION
    )
    if attestation != expected_attestation:
        raise PublicationInputError("the exact confirmed-absent attestation is required")
    request_sha256 = canonical_sha256(
        {
            "schema": "publication-confirm-absent/v1",
            "intent_id": str(intent.id),
            "intent_digest": normalized_digest,
            "expected_lock_version": normalized_lock,
            "evidence_sha256": canonical_sha256(normalized_evidence),
            "attestation_sha256": canonical_sha256(attestation),
            "actor_user_id": str(actor.id),
        }
    )
    scope = f"publication-intent:{intent.id}:reconcile-absent"
    replay = await _reconciliation_replay(
        session,
        scope=scope,
        idempotency_key=normalized_key,
        request_sha256=request_sha256,
    )
    if replay is not None:
        return replay
    _require_expected_intent(intent, normalized_digest, normalized_lock)
    if intent.state != PublicationIntentState.UNKNOWN:
        raise PublicationConflictError(
            f"only an unknown {intent.target.value} outcome can be confirmed absent"
        )
    reconciliation = await _append_reconciliation(
        session,
        intent=intent,
        outcome="confirmed_absent",
        actor=actor,
        evidence=normalized_evidence,
        attestation=attestation,
        remote_identifier=None,
        remote_url=None,
        recorded_at=recorded_at,
    )
    await session.flush()
    # Deliberately create no attempt and invoke no provider. X requires a
    # separate, fresh approval for any later logical attempt. Patreon switches
    # the existing attempt to its immutable manual package fallback.
    if intent.target == PublicationTarget.PATREON:
        await _mark_patreon_confirmed_absent_handoff(
            session,
            intent=intent,
            now=recorded_at,
        )
        intent.state = PublicationIntentState.AWAITING_HUMAN
        intent.last_error_code = "patreon_outcome_confirmed_absent"
        intent.last_error_detail = "Confirmed absent; manual package handoff is available."
        audit_action = "publication.patreon_outcome_confirmed_absent"
    else:
        intent.state = PublicationIntentState.AWAITING_APPROVAL
        intent.last_error_code = None
        intent.last_error_detail = None
        audit_action = "publication.x_outcome_confirmed_absent"
    intent.lock_version += 1
    result = PublicationReconciliationResult(
        intent_id=intent.id,
        reconciliation_id=reconciliation.id,
        outcome=reconciliation.outcome,
        state=intent.state,
        lock_version=intent.lock_version,
        replayed=False,
    )
    session.add(
        _audit(
            actor_id=actor.id,
            action=audit_action,
            resource_id=intent.id,
            correlation_id=normalized_key,
            detail={
                "target": intent.target.value,
                "intent_digest": intent.intent_digest,
                "lock_version": intent.lock_version,
                "reconciliation_id": str(reconciliation.id),
                "evidence_sha256": reconciliation.evidence_sha256,
                "attestation_sha256": reconciliation.attestation_sha256,
                "provider_effect_performed": False,
                "attempt_created": False,
                "manual_package_available": intent.target == PublicationTarget.PATREON,
            },
            now=recorded_at,
        )
    )
    session.add(
        _idempotency_record(
            scope=scope,
            key=normalized_key,
            request_sha256=request_sha256,
            status=200,
            response=_reconciliation_response(result),
            now=recorded_at,
        )
    )
    await session.commit()
    return result


async def reconcile_x_publication_absent(
    session: AsyncSession,
    **kwargs: Any,
) -> PublicationReconciliationResult:
    """Backward-compatible alias for the target-aware reconciliation service."""

    return await reconcile_publication_absent(session, **kwargs)


async def presign_patreon_package_download(
    session: AsyncSession,
    store: ObjectStore,
    *,
    intent_id: UUID,
    expected_intent_digest: str,
    expected_lock_version: int,
    actor_user_id: UUID,
    actor_role: AdminRole | str,
    expires_in_seconds: int = 300,
    part_number: int = 1,
    now: datetime | None = None,
) -> PatreonPackageDownloadResult:
    requested_at = _utc_now(now)
    normalized_digest = _sha256(expected_intent_digest, "intent digest")
    normalized_lock = _positive_int(
        expected_lock_version,
        "expected lock version",
        1_000_000_000,
    )
    normalized_expiry = _bounded_int(
        expires_in_seconds,
        "download expiry seconds",
        30,
        900,
    )
    normalized_part_number = _bounded_int(
        part_number,
        "package part number",
        1,
        PATREON_MAX_ARCHIVE_PARTS,
    )
    actor = await _require_publisher(
        session,
        actor_user_id,
        asserted_role=actor_role,
    )
    intent = await session.scalar(
        select(PublicationIntent).where(PublicationIntent.id == intent_id).with_for_update()
    )
    if intent is None:
        raise PublicationNotFoundError("publication intent was not found")
    _require_expected_intent(intent, normalized_digest, normalized_lock)
    if (
        intent.target != PublicationTarget.PATREON
        or intent.state != PublicationIntentState.AWAITING_HUMAN
    ):
        raise PublicationConflictError("Patreon package is not awaiting human handoff")
    await _load_and_require_current_publishable_intent_release(session, intent)
    attempt = await session.scalar(
        select(PublicationAttempt)
        .where(PublicationAttempt.intent_id == intent.id)
        .order_by(PublicationAttempt.attempt_no.desc())
        .limit(1)
    )
    if attempt is None or attempt.state != PublicationAttemptState.AWAITING_HUMAN:
        raise PublicationConflictError("Patreon handoff attempt is unavailable")
    approval = await session.get(PublicationApproval, attempt.approval_id)
    latest_approval_id = await session.scalar(
        select(PublicationApproval.id)
        .where(PublicationApproval.intent_id == intent.id)
        .order_by(PublicationApproval.revision.desc())
        .limit(1)
    )
    approval_authorizes = not (
        approval is None
        or latest_approval_id != approval.id
        or approval.action != PublicationApprovalAction.APPROVE
        or approval.intent_digest != intent.intent_digest
        or approval.intent_lock_version != intent.lock_version
        or approval.expires_at is None
        or _as_utc(approval.expires_at) <= requested_at
    )
    reconciliation_authorizes = False
    if not approval_authorizes and approval is not None:
        reconciliation = await session.scalar(
            select(PublicationReconciliation)
            .where(PublicationReconciliation.intent_id == intent.id)
            .order_by(PublicationReconciliation.revision.desc())
            .limit(1)
        )
        reconciliation_authorizes = (
            latest_approval_id == approval.id
            and approval.action == PublicationApprovalAction.APPROVE
            and approval.intent_digest == intent.intent_digest
            and reconciliation is not None
            and reconciliation.outcome == "confirmed_absent"
            and reconciliation.intent_digest == intent.intent_digest
            and reconciliation.intent_lock_version + 1 == intent.lock_version
        )
    if not approval_authorizes and not reconciliation_authorizes:
        raise PublicationConflictError("fresh Patreon handoff approval is unavailable")
    guard = await session.scalar(
        select(PublicationProviderGuard)
        .where(PublicationProviderGuard.provider == "global")
        .with_for_update()
    )
    if guard is None or not guard.enabled:
        raise PublicationDisabledError("publication is stopped")
    package = await session.scalar(
        select(PublicationPackage).where(
            PublicationPackage.intent_id == intent.id,
            PublicationPackage.part_number == normalized_part_number,
        )
    )
    if package is None:
        raise PublicationConflictError("Patreon package is unavailable")
    if store.backend != package.storage_backend or store.bucket != package.storage_bucket:
        raise PublicationConflictError("Patreon package storage is unavailable")
    expires_at = requested_at + timedelta(seconds=normalized_expiry)
    session.add(
        _audit(
            actor_id=actor.id,
            action="publication.patreon_package_download_authorized",
            resource_id=intent.id,
            correlation_id=f"publication-package:{package.id}",
            detail={
                "target": intent.target.value,
                "intent_digest": intent.intent_digest,
                "package_id": str(package.id),
                "package_sha256": package.sha256,
                "manifest_sha256": package.manifest_sha256,
                "part_number": package.part_number,
                "part_count": package.part_count,
                "expires_at": _canonical_datetime(expires_at),
                "guard_epoch": guard.epoch,
                "authorization_basis": (
                    "active_approval" if approval_authorizes else "confirmed_absent_reconciliation"
                ),
            },
            now=requested_at,
        )
    )
    await session.commit()
    metadata = await store.head(package.object_key)
    if (
        metadata is None
        or metadata.version_id != package.object_version_id
        or metadata.byte_size != package.byte_size
        or metadata.content_type != package.content_type
        or metadata.metadata.get("sha256") != package.sha256
        or metadata.metadata.get("manifest-sha256") != package.manifest_sha256
        or metadata.metadata.get("intent-digest") != intent.intent_digest
    ):
        raise PublicationConflictError("Patreon package storage snapshot is invalid")
    url = await store.presign_download(
        key=package.object_key,
        version_id=package.object_version_id,
        expires_in=normalized_expiry,
        download_name=(
            f"patreon-handoff-{intent.id}-part-"
            f"{package.part_number:03d}-of-{package.part_count:03d}.zip"
        ),
    )
    return PatreonPackageDownloadResult(
        intent_id=intent.id,
        package_id=package.id,
        url=url,
        filename=(
            f"patreon-handoff-{intent.id}-part-"
            f"{package.part_number:03d}-of-{package.part_count:03d}.zip"
        ),
        sha256=package.sha256,
        manifest_sha256=package.manifest_sha256,
        byte_size=package.byte_size,
        expires_at=expires_at,
        part_number=package.part_number,
        part_count=package.part_count,
        first_ordinal=package.first_ordinal,
        last_ordinal=package.last_ordinal,
    )


async def presign_finished_set_package_download(
    session: AsyncSession,
    store: ObjectStore,
    *,
    review_task_id: UUID,
    intent_id: UUID,
    expected_intent_digest: str,
    expected_lock_version: int,
    actor_user_id: UUID,
    actor_role: AdminRole | str,
    expires_in_seconds: int = 300,
    part_number: int = 1,
    now: datetime | None = None,
) -> FinishedSetPackageDownloadResult:
    """Presign one immutable, ranked finished-set archive part for its owner.

    The archive already exists as the clean Patreon/MEGA package.  This path is
    intentionally read-only: publication state, effect approval expiry, and the
    global publication guard do not govern an owner's access to their completed
    set.
    """

    requested_at = _utc_now(now)
    normalized_digest = _sha256(expected_intent_digest, "intent digest")
    normalized_lock = _positive_int(
        expected_lock_version,
        "expected lock version",
        1_000_000_000,
    )
    normalized_expiry = _bounded_int(
        expires_in_seconds,
        "download expiry seconds",
        30,
        900,
    )
    normalized_part_number = _bounded_int(
        part_number,
        "package part number",
        1,
        PATREON_MAX_ARCHIVE_PARTS,
    )
    actor = await _require_owner(
        session,
        actor_user_id,
        asserted_role=actor_role,
    )
    review_task = await session.scalar(
        select(ReviewTask).where(ReviewTask.id == review_task_id).with_for_update()
    )
    if review_task is None:
        raise PublicationNotFoundError("completed review task was not found")
    if review_task.state != ReviewTaskState.COMPLETED:
        raise PublicationConflictError("finished-set download requires a completed review")

    intent = await session.scalar(
        select(PublicationIntent).where(PublicationIntent.id == intent_id).with_for_update()
    )
    if intent is None:
        raise PublicationNotFoundError("publication intent was not found")
    _require_expected_intent(intent, normalized_digest, normalized_lock)
    if (
        intent.target != PublicationTarget.PATREON
        or intent.release_version_id != review_task.release_version_id
    ):
        raise PublicationConflictError(
            "finished-set package does not belong to the completed review"
        )
    release, release_version = await _load_intent_release(session, intent, lock=True)
    if (
        release_version.id != review_task.release_version_id
        or release_version.release_id != release.id
        or intent.release_id != release.id
    ):
        raise PublicationConflictError(
            "finished-set package release snapshot does not match the completed review"
        )

    review_selection_ids = tuple(
        (
            await session.scalars(
                select(ReleaseSelection.id)
                .where(ReleaseSelection.review_task_id == review_task.id)
                .order_by(ReleaseSelection.display_order)
            )
        ).all()
    )
    publication_selection_ids = tuple(
        (
            await session.scalars(
                select(DerivativeOutput.release_selection_id)
                .join(
                    PublicationInput,
                    PublicationInput.derivative_output_id == DerivativeOutput.id,
                )
                .where(
                    PublicationInput.intent_id == intent.id,
                    PublicationInput.role == "patreon_content",
                )
                .order_by(PublicationInput.ordinal)
            )
        ).all()
    )
    if not review_selection_ids or publication_selection_ids != review_selection_ids:
        raise PublicationConflictError(
            "finished-set package inputs do not match the completed ranked selection"
        )

    packages = tuple(
        (
            await session.scalars(
                select(PublicationPackage)
                .where(PublicationPackage.intent_id == intent.id)
                .order_by(PublicationPackage.part_number)
            )
        ).all()
    )
    if (
        not packages
        or len(packages) > PATREON_MAX_ARCHIVE_PARTS
        or tuple(package.part_number for package in packages) != tuple(range(1, len(packages) + 1))
        or any(package.part_count != len(packages) for package in packages)
        or len({package.manifest_sha256 for package in packages}) != 1
        or packages[0].first_ordinal != 1
        or packages[-1].last_ordinal != len(review_selection_ids)
        or any(
            current.last_ordinal + 1 != following.first_ordinal
            for current, following in pairwise(packages)
        )
        or normalized_part_number > len(packages)
    ):
        raise PublicationConflictError("finished-set package parts are incomplete")
    package = packages[normalized_part_number - 1]
    if store.backend != package.storage_backend or store.bucket != package.storage_bucket:
        raise PublicationConflictError("finished-set package storage is unavailable")

    expires_at = requested_at + timedelta(seconds=normalized_expiry)
    filename = (
        f"finished-ranked-set-{review_task.id}.zip"
        if package.part_count == 1
        else (
            f"finished-ranked-set-{review_task.id}-part-"
            f"{package.part_number:03d}-of-{package.part_count:03d}.zip"
        )
    )
    session.add(
        _audit(
            actor_id=actor.id,
            action="review.finished_set_package_download_authorized",
            resource_id=intent.id,
            correlation_id=f"finished-set-package:{review_task.id}:{package.id}",
            detail={
                "review_task_id": str(review_task.id),
                "intent_digest": intent.intent_digest,
                "package_id": str(package.id),
                "package_sha256": package.sha256,
                "manifest_sha256": package.manifest_sha256,
                "part_number": package.part_number,
                "part_count": package.part_count,
                "first_ordinal": package.first_ordinal,
                "last_ordinal": package.last_ordinal,
                "expires_at": _canonical_datetime(expires_at),
                "authorization_basis": "completed_review_owner",
            },
            now=requested_at,
        )
    )
    await session.commit()

    metadata = await store.head(package.object_key)
    expected_metadata = {
        "sha256": package.sha256,
        "intent-digest": intent.intent_digest,
        "manifest-sha256": package.manifest_sha256,
        "part-number": str(package.part_number),
        "part-count": str(package.part_count),
        "first-ordinal": str(package.first_ordinal),
        "last-ordinal": str(package.last_ordinal),
        "publication-intent-id": str(intent.id),
    }
    if (
        package.content_type != "application/zip"
        or metadata is None
        or metadata.key != package.object_key
        or metadata.version_id != package.object_version_id
        or metadata.byte_size != package.byte_size
        or metadata.content_type != package.content_type
        or any(metadata.metadata.get(key) != value for key, value in expected_metadata.items())
    ):
        raise PublicationConflictError("finished-set package storage snapshot is invalid")
    url = await store.presign_download(
        key=package.object_key,
        version_id=package.object_version_id,
        expires_in=normalized_expiry,
        download_name=filename,
    )
    return FinishedSetPackageDownloadResult(
        review_task_id=review_task.id,
        intent_id=intent.id,
        package_id=package.id,
        url=url,
        filename=filename,
        sha256=package.sha256,
        manifest_sha256=package.manifest_sha256,
        byte_size=package.byte_size,
        expires_at=expires_at,
        part_number=package.part_number,
        part_count=package.part_count,
        first_ordinal=package.first_ordinal,
        last_ordinal=package.last_ordinal,
    )


async def require_effect_authorization(
    session: AsyncSession,
    *,
    intent: PublicationIntent,
    approval_id: UUID,
    now: datetime,
) -> tuple[PublicationApproval, PublicationProviderGuard]:
    """Fail closed immediately before an external provider effect."""

    effective_at = _as_utc(now)
    release, release_version = await _load_intent_release(session, intent, lock=False)
    _require_current_publishable_release(release, release_version)
    await _require_current_compliance_approvals(session, release_version)
    if intent.state not in {
        PublicationIntentState.READY,
        PublicationIntentState.PROCESSING,
    }:
        raise PublicationConflictError("publication intent is not effect-ready")
    approval = await session.get(PublicationApproval, approval_id)
    if approval is None or approval.intent_id != intent.id:
        raise PublicationConflictError("publication approval is unavailable")
    latest = await session.scalar(
        select(PublicationApproval)
        .where(PublicationApproval.intent_id == intent.id)
        .order_by(PublicationApproval.revision.desc())
        .limit(1)
    )
    if (
        latest is None
        or latest.id != approval.id
        or approval.action != PublicationApprovalAction.APPROVE
        or approval.intent_digest != intent.intent_digest
        or approval.intent_lock_version != intent.lock_version
        or approval.expires_at is None
        or _as_utc(approval.expires_at) <= effective_at
    ):
        raise PublicationConflictError("publication approval is stale, revoked, or expired")
    guard = await session.scalar(
        select(PublicationProviderGuard)
        .where(PublicationProviderGuard.provider == "global")
        .with_for_update()
    )
    if guard is None or not guard.enabled:
        raise PublicationDisabledError("publication is stopped")
    return approval, guard


async def _load_frozen_outputs(
    session: AsyncSession,
    *,
    release_version_id: UUID,
    target: PublicationTarget,
    derivative_output_ids: tuple[UUID, ...],
    public_preview_output_id: UUID | None,
) -> tuple[_FrozenOutput, ...]:
    requested_ids = list(derivative_output_ids)
    if public_preview_output_id is not None and public_preview_output_id not in requested_ids:
        requested_ids.append(public_preview_output_id)
    rows = (
        await session.execute(
            select(DerivativeOutput, DerivativeJob, Asset)
            .join(
                DerivativeJob,
                DerivativeJob.id == DerivativeOutput.derivative_job_id,
            )
            .join(Asset, Asset.id == DerivativeOutput.asset_id)
            .where(DerivativeOutput.id.in_(requested_ids))
        )
    ).all()
    by_id = {row[0].id: row for row in rows}
    if len(by_id) != len(requested_ids):
        raise PublicationNotFoundError("one or more derivative outputs were not found")

    def checked(output_id: UUID, expected_target: str) -> DerivativeOutput:
        raw_output, raw_job, raw_asset = by_id[output_id]
        output = cast(DerivativeOutput, raw_output)
        job = cast(DerivativeJob, raw_job)
        asset = cast(Asset, raw_asset)
        if (
            job.release_version_id != release_version_id
            or job.state != DerivativeJobState.SUCCEEDED
            or output.target != expected_target
            or asset.kind != AssetKind.DERIVATIVE
            or asset.state != AssetState.AVAILABLE
            or asset.storage_backend != output.asset_storage_backend
            or asset.storage_bucket != output.asset_storage_bucket
            or asset.object_key != output.asset_object_key
            or asset.object_version_id != output.asset_object_version_id
            or asset.sha256 != output.asset_sha256
            or asset.content_type != output.asset_content_type
            or asset.image_format != output.asset_image_format
            or asset.width != output.asset_width
            or asset.height != output.asset_height
            or asset.byte_size != output.asset_byte_size
        ):
            raise PublicationConflictError(
                "a derivative output is stale or not a verified available derivative"
            )
        if target == PublicationTarget.PATREON:
            if output.asset_content_type not in {
                "image/jpeg",
                "image/png",
            }:
                raise PublicationInputError(
                    "Patreon handoff inputs must be JPEG or PNG derivatives"
                )
            if output.asset_byte_size > PATREON_MAX_IMAGE_BYTES:
                raise PublicationInputError(
                    f"Patreon inputs must not exceed {PATREON_MAX_IMAGE_BYTES} bytes each"
                )
        if target == PublicationTarget.X and (
            output.asset_content_type not in {"image/jpeg", "image/png", "image/webp"}
            or output.asset_byte_size > X_MAX_STATIC_IMAGE_BYTES
        ):
            raise PublicationInputError(
                f"X teaser outputs must not exceed {X_MAX_STATIC_IMAGE_BYTES} bytes"
            )
        return output

    if target == PublicationTarget.X:
        if public_preview_output_id is not None:
            raise PublicationInputError("X intents do not accept a separate public preview")
        if not 1 <= len(derivative_output_ids) <= X_MAX_MEDIA_PER_POST:
            raise PublicationInputError(
                f"X intents require 1 to {X_MAX_MEDIA_PER_POST} teaser outputs"
            )
        selected_output_ids = tuple(
            (
                await session.scalars(
                    select(DerivativeOutput.id)
                    .join(
                        DerivativeJob,
                        DerivativeJob.id == DerivativeOutput.derivative_job_id,
                    )
                    .join(
                        ReleaseSelection,
                        ReleaseSelection.id == DerivativeJob.release_selection_id,
                    )
                    .join(
                        ReviewXSelection,
                        and_(
                            ReviewXSelection.review_task_id == ReleaseSelection.review_task_id,
                            ReviewXSelection.asset_id == ReleaseSelection.asset_id,
                        ),
                    )
                    .where(
                        DerivativeJob.release_version_id == release_version_id,
                        DerivativeJob.state == DerivativeJobState.SUCCEEDED,
                        DerivativeOutput.target == "x_teaser",
                    )
                    .order_by(ReleaseSelection.display_order)
                )
            ).all()
        )
        if derivative_output_ids != selected_output_ids:
            raise PublicationInputError(
                "X publication inputs must exactly match the owner-selected teasers"
            )
        return tuple(
            _FrozenOutput(
                output=checked(output_id, "x_teaser"),
                role="x_teaser",
            )
            for output_id in derivative_output_ids
        )

    if not derivative_output_ids:
        raise PublicationInputError("Patreon intents require at least one content output")
    if public_preview_output_id is None:
        raise PublicationInputError(
            "Patreon intents require one explicitly attested clean public preview"
        )
    exact_full_output_ids = await _load_exact_full_output_ids(
        session,
        release_version_id=release_version_id,
    )
    if derivative_output_ids != exact_full_output_ids:
        raise PublicationInputError(
            "Patreon content outputs must exactly match all accepted full outputs"
        )
    if len(derivative_output_ids) > MAX_ACCEPTED_IMAGES_PER_RELEASE:
        raise PublicationInputError(
            f"Patreon content must not exceed {MAX_ACCEPTED_IMAGES_PER_RELEASE} images"
        )
    content = tuple(
        _FrozenOutput(
            output=checked(output_id, "full"),
            role="patreon_content",
        )
        for output_id in derivative_output_ids
    )
    preview = _FrozenOutput(
        output=checked(public_preview_output_id, "full"),
        role="patreon_preview",
    )
    for offset in range(0, len(content), PATREON_MAX_DERIVATIVE_IMAGES):
        part = content[offset : offset + PATREON_MAX_DERIVATIVE_IMAGES]
        part_image_bytes = sum(item.output.asset_byte_size for item in part) + (
            preview.output.asset_byte_size
        )
        if part_image_bytes > PATREON_MAX_TOTAL_IMAGE_BYTES:
            raise PublicationInputError(
                "combined Patreon content and public preview exceed the "
                f"{PATREON_MAX_TOTAL_IMAGE_BYTES}-byte archive-part limit"
            )
    return (*content, preview)


async def _load_exact_full_output_ids(
    session: AsyncSession,
    *,
    release_version_id: UUID,
) -> tuple[UUID, ...]:
    selection_ids = tuple(
        (
            await session.scalars(
                select(ReleaseSelection.id)
                .where(ReleaseSelection.release_version_id == release_version_id)
                .order_by(ReleaseSelection.display_order)
            )
        ).all()
    )
    if not selection_ids:
        raise PublicationConflictError("accepted release selection snapshot is unavailable")
    rows = (
        await session.execute(
            select(
                DerivativeJob.release_selection_id,
                DerivativeOutput.id,
            )
            .join(
                DerivativeOutput,
                DerivativeOutput.derivative_job_id == DerivativeJob.id,
            )
            .where(
                DerivativeJob.release_version_id == release_version_id,
                DerivativeJob.release_selection_id.in_(selection_ids),
                DerivativeJob.state == DerivativeJobState.SUCCEEDED,
                DerivativeOutput.target == "full",
            )
        )
    ).all()
    by_selection: dict[UUID, UUID] = {}
    for selection_id, output_id in rows:
        if selection_id in by_selection:
            raise PublicationConflictError(
                "accepted release has more than one successful full output per image"
            )
        by_selection[selection_id] = output_id
    if set(by_selection) != set(selection_ids):
        raise PublicationConflictError(
            "accepted release does not have one successful full output per image"
        )
    return tuple(by_selection[selection_id] for selection_id in selection_ids)


def _normalize_configuration(
    target: PublicationTarget,
    configuration: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(configuration, Mapping):
        raise PublicationInputError("publication configuration must be an object")
    try:
        encoded = canonical_json_bytes(dict(configuration))
        normalized: object = json.loads(encoded)
    except (TypeError, ValueError, OverflowError) as error:
        raise PublicationInputError("publication configuration must be canonical JSON") from error
    if len(encoded) > _MAX_CONFIGURATION_BYTES or not isinstance(normalized, dict):
        raise PublicationInputError("publication configuration is too large")
    if not all(isinstance(key, str) for key in normalized):
        raise PublicationInputError("publication configuration keys must be strings")

    if target == PublicationTarget.X:
        if set(normalized) != {"text"}:
            raise PublicationInputError("X configuration must contain only text")
        text_value = normalized["text"]
        if not isinstance(text_value, str) or not text_value.strip():
            raise PublicationInputError("X post text must not be empty")
        if len(text_value.encode("utf-8")) > X_MAX_POST_TEXT_BYTES:
            raise PublicationInputError(
                f"X post text must not exceed {X_MAX_POST_TEXT_BYTES} UTF-8 bytes"
            )
        return {"text": text_value}

    if set(normalized) != {"title", "body", "tier", "tags"}:
        raise PublicationInputError(
            "Patreon configuration must contain title, body, tier, and tags"
        )
    title = _config_text(normalized["title"], "Patreon title", PATREON_MAX_TITLE_BYTES, False)
    body = _config_text(normalized["body"], "Patreon body", PATREON_MAX_BODY_BYTES, True)
    tier = _config_text(normalized["tier"], "Patreon tier", PATREON_MAX_TIER_BYTES, False)
    tags_value = normalized["tags"]
    if not isinstance(tags_value, list) or len(tags_value) > PATREON_MAX_TAGS:
        raise PublicationInputError(
            f"Patreon tags must be an array of at most {PATREON_MAX_TAGS} strings"
        )
    tags: list[str] = []
    for value in tags_value:
        tag = _config_text(value, "Patreon tag", PATREON_MAX_TAG_BYTES, False)
        if tag in tags:
            raise PublicationInputError("Patreon tags must be unique")
        tags.append(tag)
    return {"title": title, "body": body, "tier": tier, "tags": tags}


def _normalize_preview_attestation(
    *,
    target: PublicationTarget,
    preview_output_id: UUID | None,
    attester_name: str | None,
    attested_at: datetime | None,
    timezone_name: str | None,
    attester_user_id: UUID,
    now: datetime,
) -> dict[str, str | datetime] | None:
    values = (
        preview_output_id,
        attester_name,
        attested_at,
        timezone_name,
    )
    if target == PublicationTarget.X:
        if any(value is not None for value in values):
            raise PublicationInputError("public-preview attestation fields are Patreon-only")
        return None
    if any(value is None for value in values):
        raise PublicationInputError(
            "Patreon requires a named, timezone-aware public-preview attestation"
        )
    assert preview_output_id is not None
    assert attester_name is not None
    assert attested_at is not None
    assert timezone_name is not None
    normalized_name = _bounded_text(attester_name, "public preview attester", 256)
    normalized_timezone = _bounded_text(timezone_name, "attestation timezone", 100)
    if attested_at.tzinfo is None or attested_at.utcoffset() is None:
        raise PublicationInputError("public preview attestation time must include a timezone")
    try:
        zone = ZoneInfo(normalized_timezone)
    except ZoneInfoNotFoundError as error:
        raise PublicationInputError("attestation timezone must be an IANA timezone") from error
    instant = attested_at.astimezone(UTC)
    if attested_at.utcoffset() != instant.astimezone(zone).utcoffset():
        raise PublicationInputError("attestation time offset does not match the named timezone")
    if instant > now + timedelta(minutes=5) or instant < now - timedelta(hours=24):
        raise PublicationInputError(
            "public preview attestation must be recorded within the last 24 hours"
        )
    statement_record = {
        "schema": "patreon-public-preview-attestation/v1",
        "statement": PATREON_PUBLIC_PREVIEW_ATTESTATION,
        "safe_for_public": True,
        "prohibited_content": ["nudity", "sexually_explicit_content"],
        "preview_derivative_output_id": str(preview_output_id),
        "attester_name": normalized_name,
        "attester_user_id": str(attester_user_id),
        "attested_at": _canonical_datetime(instant),
        "timezone": normalized_timezone,
    }
    return {
        "attester_name": normalized_name,
        "attested_at": instant,
        "timezone": normalized_timezone,
        "sha256": canonical_sha256(statement_record),
    }


def _normalize_credential_reference(
    target: PublicationTarget,
    reference: str | None,
) -> str | None:
    if target == PublicationTarget.PATREON:
        if reference is not None:
            raise PublicationInputError(
                "Patreon handoff intents do not accept provider credentials"
            )
        return None
    if reference is None:
        raise PublicationInputError("X intents require a credential reference")
    normalized = _bounded_text(reference, "credential reference", 500)
    if (
        _CREDENTIAL_REFERENCE.fullmatch(normalized) is None
        or "?" in normalized
        or "#" in normalized
        or "@" in normalized
    ):
        raise PublicationInputError(
            "credential reference must be an approved secret-manager reference"
        )
    return normalized


async def _require_publisher(
    session: AsyncSession,
    user_id: UUID,
    *,
    asserted_role: AdminRole | str | None = None,
) -> AdminUser:
    user = await session.get(AdminUser, user_id)
    if user is None or not user.is_active or user.role not in _PUBLISHER_ROLES:
        raise PublicationConflictError("an active owner or publisher is required")
    if asserted_role is not None and user.role != _role(asserted_role):
        raise PublicationConflictError("publisher role assertion is stale")
    return user


async def _require_owner(
    session: AsyncSession,
    user_id: UUID,
    *,
    asserted_role: AdminRole | str,
) -> AdminUser:
    user = await session.get(AdminUser, user_id)
    if (
        user is None
        or not user.is_active
        or user.role != AdminRole.OWNER
        or _role(asserted_role) != AdminRole.OWNER
    ):
        raise PublicationConflictError("an active owner is required")
    return user


async def _load_intent_release(
    session: AsyncSession,
    intent: PublicationIntent,
    *,
    lock: bool,
) -> tuple[Release, ReleaseVersion]:
    query = (
        select(Release, ReleaseVersion)
        .join(ReleaseVersion, ReleaseVersion.release_id == Release.id)
        .where(
            Release.id == intent.release_id,
            ReleaseVersion.id == intent.release_version_id,
        )
    )
    if lock:
        query = query.with_for_update()
    row = (await session.execute(query)).one_or_none()
    if row is None:
        raise PublicationConflictError("publication release snapshot is unavailable")
    return row[0], row[1]


async def _load_and_require_current_publishable_intent_release(
    session: AsyncSession,
    intent: PublicationIntent,
) -> tuple[Release, ReleaseVersion]:
    release, version = await _load_intent_release(session, intent, lock=True)
    _require_current_publishable_release(release, version)
    await _require_current_compliance_approvals(session, version)
    return release, version


def _require_current_publishable_release(
    release: Release,
    version: ReleaseVersion,
) -> None:
    if (
        version.release_id != release.id
        or release.current_version_no != version.version_no
        or release.phase not in _PUBLISHABLE_RELEASE_PHASES
    ):
        raise PublicationConflictError(
            "publication requires the exact current publishable release version"
        )


def _require_single_target_owner(
    intents: tuple[PublicationIntent, ...],
    *,
    owner_intent_id: UUID,
) -> None:
    if any(
        intent.id != owner_intent_id and intent.state not in _SUPERSEDABLE_INTENT_STATES
        for intent in intents
    ):
        raise PublicationConflictError(
            "another publication intent already owns this release target"
        )


async def _require_current_compliance_approvals(
    session: AsyncSession,
    release_version: ReleaseVersion,
) -> None:
    try:
        specification = ReleaseSpecification.model_validate(release_version.specification)
        if canonical_sha256(specification) != release_version.specification_sha256:
            raise PublicationConflictError("frozen release specification digest is invalid")
        await validate_release_approvals(session, specification)
    except (ValidationError, ReleaseApprovalError) as error:
        raise PublicationConflictError(
            "current subject, model, LoRA, or workflow approvals are unavailable"
        ) from error


def _validate_frozen_input_set(
    intent: PublicationIntent,
    inputs: tuple[PublicationInput, ...],
) -> None:
    if len(inputs) != intent.input_count:
        raise PublicationConflictError("publication input snapshot is incomplete")
    if tuple(item.ordinal for item in inputs) != tuple(range(1, len(inputs) + 1)):
        raise PublicationConflictError("publication input ordering is invalid")
    if intent.target == PublicationTarget.X:
        if not 1 <= len(inputs) <= X_MAX_MEDIA_PER_POST or any(
            item.role != "x_teaser" or item.derivative_target != "x_teaser" for item in inputs
        ):
            raise PublicationConflictError("X publication input snapshot is invalid")
    else:
        previews = [item for item in inputs if item.role == "patreon_preview"]
        content = [item for item in inputs if item.role == "patreon_content"]
        if (
            len(previews) != 1
            or not content
            or previews[0].derivative_target != "full"
            or any(item.derivative_target != "full" for item in content)
        ):
            raise PublicationConflictError("Patreon publication input snapshot is invalid")
    manifest = [
        {
            "ordinal": item.ordinal,
            "role": item.role,
            "derivative_output_id": str(item.derivative_output_id),
            "derivative_recipe_id": str(item.derivative_recipe_id),
            "asset_id": str(item.asset_id),
            "derivative_target": item.derivative_target,
            "storage_backend": item.asset_storage_backend,
            "storage_bucket": item.asset_storage_bucket,
            "object_key": item.asset_object_key,
            "object_version_id": item.asset_object_version_id,
            "sha256": item.asset_sha256,
            "content_type": item.asset_content_type,
            "image_format": item.asset_image_format,
            "width": item.asset_width,
            "height": item.asset_height,
            "byte_size": item.asset_byte_size,
        }
        for item in inputs
    ]
    actual_sha256 = canonical_sha256(
        {
            "schema": "publication-input-manifest/v1",
            "release_version_id": str(intent.release_version_id),
            "target": intent.target.value,
            "inputs": manifest,
        }
    )
    if actual_sha256 != intent.input_manifest_sha256:
        raise PublicationConflictError("publication input manifest digest is invalid")


async def _append_reconciliation(
    session: AsyncSession,
    *,
    intent: PublicationIntent,
    outcome: str,
    actor: AdminUser,
    evidence: str,
    attestation: str,
    remote_identifier: str | None,
    remote_url: str | None,
    recorded_at: datetime,
) -> PublicationReconciliation:
    revision = (
        int(
            await session.scalar(
                select(func.max(PublicationReconciliation.revision)).where(
                    PublicationReconciliation.intent_id == intent.id
                )
            )
            or 0
        )
        + 1
    )
    reconciliation = PublicationReconciliation(
        id=uuid7(),
        intent_id=intent.id,
        revision=revision,
        outcome=outcome,
        intent_digest=intent.intent_digest,
        intent_lock_version=intent.lock_version,
        actor_user_id=actor.id,
        actor_role=actor.role.value,
        evidence_sha256=canonical_sha256(evidence),
        attestation_sha256=canonical_sha256(attestation),
        remote_identifier=remote_identifier,
        remote_url=remote_url,
        recorded_at=recorded_at,
    )
    session.add(reconciliation)
    return reconciliation


async def _mark_manual_confirmation_succeeded(
    session: AsyncSession,
    *,
    intent: PublicationIntent,
    remote_identifier: str,
    remote_url: str,
    now: datetime,
) -> None:
    attempt = await session.scalar(
        select(PublicationAttempt)
        .where(PublicationAttempt.intent_id == intent.id)
        .order_by(PublicationAttempt.attempt_no.desc())
        .limit(1)
        .with_for_update()
    )
    if attempt is None:
        raise PublicationConflictError("publication attempt is unavailable")
    kind = (
        PublicationStepKind.PATREON_HANDOFF
        if intent.target == PublicationTarget.PATREON
        else PublicationStepKind.X_CREATE_POST
    )
    step = await session.scalar(
        select(PublicationStep)
        .where(
            PublicationStep.attempt_id == attempt.id,
            PublicationStep.kind == kind,
        )
        .with_for_update()
    )
    if step is None:
        raise PublicationConflictError("publication reconciliation step is unavailable")
    step.state = PublicationStepState.SUCCEEDED
    step.remote_identifier = remote_identifier
    step.remote_url = remote_url
    step.effect_completed_at = step.effect_completed_at or now
    step.retry_at = None
    step.last_error_code = None
    step.last_error_detail = None
    step.updated_at = now
    step.lock_version += 1
    attempt.state = PublicationAttemptState.SUCCEEDED
    attempt.lease_owner = None
    attempt.lease_expires_at = None
    attempt.retry_at = None
    attempt.completed_at = now
    attempt.last_error_code = None
    attempt.last_error_detail = None
    attempt.lock_version += 1


async def _mark_patreon_confirmed_absent_handoff(
    session: AsyncSession,
    *,
    intent: PublicationIntent,
    now: datetime,
) -> None:
    package_id = await session.scalar(
        select(PublicationPackage.id).where(
            PublicationPackage.intent_id == intent.id,
            PublicationPackage.part_number == 1,
        )
    )
    if package_id is None:
        raise PublicationConflictError("Patreon manual package is unavailable")
    attempt = await session.scalar(
        select(PublicationAttempt)
        .where(PublicationAttempt.intent_id == intent.id)
        .order_by(PublicationAttempt.attempt_no.desc())
        .limit(1)
        .with_for_update()
    )
    if attempt is None or attempt.state != PublicationAttemptState.UNKNOWN:
        raise PublicationConflictError("unknown Patreon attempt is unavailable")
    step = await session.scalar(
        select(PublicationStep)
        .where(
            PublicationStep.attempt_id == attempt.id,
            PublicationStep.kind == PublicationStepKind.PATREON_HANDOFF,
        )
        .with_for_update()
    )
    if step is None or step.state != PublicationStepState.UNKNOWN or step.effect_started_at is None:
        raise PublicationConflictError("unknown Patreon handoff step is unavailable")
    step.state = PublicationStepState.AWAITING_HUMAN
    step.retry_class = PublicationRetryClass.TERMINAL
    step.retry_at = None
    step.effect_completed_at = now
    step.last_error_code = "patreon_outcome_confirmed_absent"
    step.last_error_detail = "Confirmed absent; manual package handoff is available."
    step.updated_at = now
    step.lock_version += 1
    attempt.state = PublicationAttemptState.AWAITING_HUMAN
    attempt.lease_owner = None
    attempt.lease_expires_at = None
    attempt.retry_at = None
    attempt.completed_at = None
    attempt.last_error_code = "patreon_outcome_confirmed_absent"
    attempt.last_error_detail = "Confirmed absent; manual package handoff is available."
    attempt.lock_version += 1


def validate_patreon_post_identity(
    remote_identifier: str,
    remote_url: str,
) -> tuple[str, str]:
    identifier = _bounded_text(remote_identifier, "Patreon post ID", 20)
    if not identifier.isdecimal() or len(identifier) > 20:
        raise PublicationInputError("Patreon post ID must contain decimal digits")
    normalized_url = _bounded_text(remote_url, "Patreon post URL", 2048)
    parsed = urlsplit(normalized_url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"patreon.com", "www.patreon.com"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 443}
        or parsed.query
        or parsed.fragment
    ):
        raise PublicationInputError("Patreon post URL must be an exact HTTPS Patreon URL")
    parts = tuple(part for part in parsed.path.split("/") if part)
    if len(parts) != 2 or parts[0] != "posts":
        raise PublicationInputError("Patreon post URL path is invalid")
    match = _PATREON_POST_ID.search(parts[1])
    if match is None or match.group(1) != identifier:
        raise PublicationInputError("Patreon post URL does not match the post ID")
    return identifier, normalized_url


def _validate_x_post_identity(
    remote_identifier: str,
    remote_url: str,
) -> tuple[str, str]:
    identifier = _bounded_text(remote_identifier, "X post ID", 19)
    if _SNOWFLAKE.fullmatch(identifier) is None:
        raise PublicationInputError("X post ID must contain 1 to 19 decimal digits")
    normalized_url = _bounded_text(remote_url, "X post URL", 2048)
    parsed = urlsplit(normalized_url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"x.com", "www.x.com"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 443}
        or parsed.query
        or parsed.fragment
    ):
        raise PublicationInputError("X post URL must be an exact HTTPS X URL")
    parts = tuple(part for part in parsed.path.split("/") if part)
    if len(parts) != 3 or parts[1] != "status" or parts[2] != identifier:
        raise PublicationInputError("X post URL does not match the post ID")
    return identifier, normalized_url


def _require_expected_intent(
    intent: PublicationIntent,
    digest: str,
    lock_version: int,
) -> None:
    if intent.intent_digest != digest or intent.lock_version != lock_version:
        raise PublicationConflictError("publication intent digest or lock is stale")


async def _intent_replay(
    session: AsyncSession,
    *,
    scope: str,
    idempotency_key: str,
    request_sha256: str,
) -> PublicationIntentResult | None:
    body = await _idempotency_body(
        session,
        scope=scope,
        key=idempotency_key,
        request_sha256=request_sha256,
    )
    if body is None:
        return None
    try:
        return PublicationIntentResult(
            intent_id=UUID(str(body["intent_id"])),
            release_id=UUID(str(body["release_id"])),
            release_version_id=UUID(str(body["release_version_id"])),
            target=PublicationTarget(str(body["target"])),
            state=PublicationIntentState(str(body["state"])),
            configuration_sha256=str(body["configuration_sha256"]),
            input_manifest_sha256=str(body["input_manifest_sha256"]),
            intent_digest=str(body["intent_digest"]),
            input_count=int(body["input_count"]),
            scheduled_at=_parse_datetime(body.get("scheduled_at")),
            lock_version=int(body["lock_version"]),
            replayed=True,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise PublicationConflictError("publication idempotency record is invalid") from error


async def _approval_replay(
    session: AsyncSession,
    *,
    scope: str,
    idempotency_key: str,
    request_sha256: str,
) -> PublicationApprovalResult | None:
    body = await _idempotency_body(
        session,
        scope=scope,
        key=idempotency_key,
        request_sha256=request_sha256,
    )
    if body is None:
        return None
    try:
        return PublicationApprovalResult(
            intent_id=UUID(str(body["intent_id"])),
            approval_id=UUID(str(body["approval_id"])),
            attempt_id=UUID(str(body["attempt_id"])),
            approval_revision=int(body["approval_revision"]),
            intent_lock_version=int(body["intent_lock_version"]),
            expires_at=_required_datetime(body["expires_at"]),
            state=PublicationIntentState(str(body["state"])),
            replayed=True,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise PublicationConflictError("publication idempotency record is invalid") from error


async def _revocation_replay(
    session: AsyncSession,
    *,
    scope: str,
    idempotency_key: str,
    request_sha256: str,
) -> PublicationRevocationResult | None:
    body = await _idempotency_body(
        session,
        scope=scope,
        key=idempotency_key,
        request_sha256=request_sha256,
    )
    if body is None:
        return None
    try:
        return PublicationRevocationResult(
            intent_id=UUID(str(body["intent_id"])),
            approval_id=UUID(str(body["approval_id"])),
            approval_revision=int(body["approval_revision"]),
            intent_lock_version=int(body["intent_lock_version"]),
            state=PublicationIntentState(str(body["state"])),
            replayed=True,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise PublicationConflictError("publication idempotency record is invalid") from error


async def _guard_replay(
    session: AsyncSession,
    *,
    scope: str,
    idempotency_key: str,
    request_sha256: str,
) -> PublicationGuardResult | None:
    body = await _idempotency_body(
        session,
        scope=scope,
        key=idempotency_key,
        request_sha256=request_sha256,
    )
    if body is None:
        return None
    try:
        return PublicationGuardResult(
            enabled=bool(body["enabled"]),
            epoch=int(body["epoch"]),
            lock_version=int(body["lock_version"]),
            changed_at=_required_datetime(body["changed_at"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise PublicationConflictError("publication idempotency record is invalid") from error


async def _reconciliation_replay(
    session: AsyncSession,
    *,
    scope: str,
    idempotency_key: str,
    request_sha256: str,
) -> PublicationReconciliationResult | None:
    body = await _idempotency_body(
        session,
        scope=scope,
        key=idempotency_key,
        request_sha256=request_sha256,
    )
    if body is None:
        return None
    try:
        return PublicationReconciliationResult(
            intent_id=UUID(str(body["intent_id"])),
            reconciliation_id=UUID(str(body["reconciliation_id"])),
            outcome=str(body["outcome"]),
            state=PublicationIntentState(str(body["state"])),
            lock_version=int(body["lock_version"]),
            replayed=True,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise PublicationConflictError("publication idempotency record is invalid") from error


async def _idempotency_body(
    session: AsyncSession,
    *,
    scope: str,
    key: str,
    request_sha256: str,
) -> dict[str, Any] | None:
    record = await session.scalar(
        select(IdempotencyRecord).where(
            IdempotencyRecord.scope == scope,
            IdempotencyRecord.idempotency_key == key,
        )
    )
    if record is None:
        return None
    if record.request_sha256 != request_sha256:
        raise PublicationConflictError(
            "idempotency key was already used for a different publication request"
        )
    return record.response_body


def _idempotency_record(
    *,
    scope: str,
    key: str,
    request_sha256: str,
    status: int,
    response: dict[str, Any],
    now: datetime,
) -> IdempotencyRecord:
    return IdempotencyRecord(
        id=uuid7(),
        scope=scope,
        idempotency_key=key,
        request_sha256=request_sha256,
        response_status=status,
        response_body=response,
        created_at=now,
        expires_at=now + timedelta(days=_PLAN_IDEMPOTENCY_DAYS),
    )


def _intent_result(
    intent: PublicationIntent,
    *,
    replayed: bool,
) -> PublicationIntentResult:
    return PublicationIntentResult(
        intent_id=intent.id,
        release_id=intent.release_id,
        release_version_id=intent.release_version_id,
        target=intent.target,
        state=intent.state,
        configuration_sha256=intent.configuration_sha256,
        input_manifest_sha256=intent.input_manifest_sha256,
        intent_digest=intent.intent_digest,
        input_count=intent.input_count,
        scheduled_at=(_as_utc(intent.scheduled_at) if intent.scheduled_at is not None else None),
        lock_version=intent.lock_version,
        replayed=replayed,
    )


def _intent_response(result: PublicationIntentResult) -> dict[str, Any]:
    return {
        "intent_id": str(result.intent_id),
        "release_id": str(result.release_id),
        "release_version_id": str(result.release_version_id),
        "target": result.target.value,
        "state": result.state.value,
        "configuration_sha256": result.configuration_sha256,
        "input_manifest_sha256": result.input_manifest_sha256,
        "intent_digest": result.intent_digest,
        "input_count": result.input_count,
        "scheduled_at": _canonical_datetime(result.scheduled_at),
        "lock_version": result.lock_version,
    }


def _approval_response(result: PublicationApprovalResult) -> dict[str, Any]:
    return {
        "intent_id": str(result.intent_id),
        "approval_id": str(result.approval_id),
        "attempt_id": str(result.attempt_id),
        "approval_revision": result.approval_revision,
        "intent_lock_version": result.intent_lock_version,
        "expires_at": _canonical_datetime(result.expires_at),
        "state": result.state.value,
    }


def _revocation_response(result: PublicationRevocationResult) -> dict[str, Any]:
    return {
        "intent_id": str(result.intent_id),
        "approval_id": str(result.approval_id),
        "approval_revision": result.approval_revision,
        "intent_lock_version": result.intent_lock_version,
        "state": result.state.value,
    }


def _guard_response(result: PublicationGuardResult) -> dict[str, Any]:
    return {
        "enabled": result.enabled,
        "epoch": result.epoch,
        "lock_version": result.lock_version,
        "changed_at": _canonical_datetime(result.changed_at),
    }


def _reconciliation_response(
    result: PublicationReconciliationResult,
) -> dict[str, Any]:
    return {
        "intent_id": str(result.intent_id),
        "reconciliation_id": str(result.reconciliation_id),
        "outcome": result.outcome,
        "state": result.state.value,
        "lock_version": result.lock_version,
    }


def _audit(
    *,
    actor_id: UUID,
    action: str,
    resource_id: UUID,
    correlation_id: str,
    detail: dict[str, Any],
    now: datetime,
) -> AuditEvent:
    return AuditEvent(
        id=uuid7(),
        actor=f"admin:{actor_id}",
        action=action,
        resource_type="publication_intent",
        resource_id=resource_id,
        correlation_id=correlation_id,
        detail=detail,
        occurred_at=now,
    )


def safe_publication_error(
    *,
    code: str,
    detail: str,
) -> tuple[str, str]:
    """Return bounded constant-style errors suitable for durable state and logs."""

    normalized_code = re.sub(r"[^a-z0-9_]", "_", code.lower())[:100] or "error"
    normalized_detail = " ".join(detail.split())[:_MAX_SAFE_ERROR_BYTES]
    return normalized_code, normalized_detail


def _target(value: PublicationTarget | str) -> PublicationTarget:
    try:
        return value if isinstance(value, PublicationTarget) else PublicationTarget(value)
    except ValueError as error:
        raise PublicationInputError("publication target must be x or patreon") from error


def _role(value: AdminRole | str) -> AdminRole:
    try:
        return value if isinstance(value, AdminRole) else AdminRole(value)
    except ValueError as error:
        raise PublicationInputError("publisher role is invalid") from error


def _normalize_output_ids(values: Sequence[UUID]) -> tuple[UUID, ...]:
    if isinstance(values, (str, bytes)):
        raise PublicationInputError("derivative output IDs must be a sequence")
    normalized = tuple(values)
    if not normalized or any(not isinstance(value, UUID) for value in normalized):
        raise PublicationInputError("derivative output IDs must contain UUIDs")
    if len(set(normalized)) != len(normalized):
        raise PublicationInputError("derivative output IDs must be unique")
    if len(normalized) > 100:
        raise PublicationInputError("too many derivative output IDs")
    return normalized


def _config_text(value: object, label: str, max_bytes: int, allow_empty: bool) -> str:
    if not isinstance(value, str):
        raise PublicationInputError(f"{label} must be a string")
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    if not allow_empty and not normalized.strip():
        raise PublicationInputError(f"{label} must not be empty")
    if len(normalized.encode("utf-8")) > max_bytes:
        raise PublicationInputError(f"{label} is too large")
    if any(
        ord(character) == 0
        or ord(character) == 127
        or (ord(character) < 32 and character not in "\n\t")
        for character in normalized
    ):
        raise PublicationInputError(f"{label} contains a prohibited control character")
    return normalized


def _bounded_text(
    value: str,
    label: str,
    maximum: int,
    *,
    byte_limit: bool = False,
) -> str:
    if not isinstance(value, str):
        raise PublicationInputError(f"{label} must be a string")
    normalized = value.strip()
    measured = len(normalized.encode("utf-8")) if byte_limit else len(normalized)
    if (
        not normalized
        or measured > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in normalized)
    ):
        raise PublicationInputError(f"{label} must be nonempty and within its limit")
    return normalized


def _sha256(value: str, label: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise PublicationInputError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _positive_int(value: int, label: str, maximum: int) -> int:
    return _bounded_int(value, label, 1, maximum)


def _bounded_int(value: int, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise PublicationInputError(f"{label} must be between {minimum} and {maximum}")
    return value


def _optional_future_datetime(
    value: datetime | None,
    *,
    now: datetime,
    label: str,
) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise PublicationInputError(f"{label} must include a timezone")
    normalized = value.astimezone(UTC)
    if normalized < now:
        raise PublicationInputError(f"{label} must not be in the past")
    return normalized


def _initial_attempt_available_at(
    *,
    target: PublicationTarget,
    scheduled_at: datetime | None,
    approved_at: datetime,
) -> datetime:
    """Return when the provider-creation attempt may start.

    Patreon owns the future schedule after its post is created, so the browser
    handoff starts immediately after approval while retaining ``scheduled_at``
    in the frozen package. X has no equivalent provider-side scheduler in this
    workflow and therefore remains queued until its requested publication time.
    """

    normalized_approval = _as_utc(approved_at)
    if target == PublicationTarget.PATREON:
        return normalized_approval
    if scheduled_at is None:
        return normalized_approval
    return max(normalized_approval, _as_utc(scheduled_at))


def _utc_now(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if value.tzinfo is None or value.utcoffset() is None:
        raise PublicationInputError("operation time must include a timezone")
    return value.astimezone(UTC)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _canonical_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _as_utc(value).isoformat().replace("+00:00", "Z")


def _required_datetime(value: object) -> datetime:
    result = _parse_datetime(value)
    if result is None:
        raise ValueError("datetime is required")
    return result


def _parse_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("datetime must be a string")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        raise ValueError("datetime must include a timezone")
    return parsed.astimezone(UTC)
