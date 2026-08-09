import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, cast
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from gen_automation.db.models import (
    AdminUser,
    AuditEvent,
    IdempotencyRecord,
    ManagedLoraArtifact,
    ModelArtifactApproval,
    SubjectApproval,
    WorkflowApproval,
)
from gen_automation.domain.canonical import canonical_sha256
from gen_automation.domain.compliance_registry import (
    ApprovalEvidence,
    ApprovalRevoke,
    ModelArtifactApprovalCreate,
    SubjectApprovalCreate,
    WorkflowApprovalCreate,
)
from gen_automation.domain.enums import AdminRole, ApprovalStatus, ManagedLoraLifecycle
from gen_automation.services.compliance import canonical_source_sha256

RegistryName = Literal["subject", "model_artifact", "workflow"]
ApprovalModel = SubjectApproval | ModelArtifactApproval | WorkflowApproval
ApprovalModelType = type[SubjectApproval] | type[ModelArtifactApproval] | type[WorkflowApproval]

_IDEMPOTENCY_KEY_MAX_LENGTH = 200
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_MUTATING_ROLES = frozenset({AdminRole.OWNER, AdminRole.ADMIN})


class ComplianceRegistryError(Exception):
    """Base error for server-owned compliance registry mutations."""


class ComplianceRegistryNotFoundError(ComplianceRegistryError):
    pass


class ComplianceRegistryConflictError(ComplianceRegistryError):
    pass


class ComplianceRegistryInputError(ComplianceRegistryError, ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ApprovalMutationResult:
    registry: RegistryName
    approval_id: UUID
    identity_sha256: str
    name: str
    approval_version: int
    status: ApprovalStatus
    is_current: bool
    evidence_sha256: str
    approved_by_user_id: UUID
    approved_at: datetime
    revoked_by_user_id: UUID | None
    revoked_at: datetime | None
    replayed: bool


async def approve_subject(
    session: AsyncSession,
    *,
    command: SubjectApprovalCreate,
    actor_user_id: UUID,
    idempotency_key: str,
    now: datetime | None = None,
) -> ApprovalMutationResult:
    approved_at = _as_utc(now or datetime.now(UTC))
    key = _idempotency_key(idempotency_key)
    request_sha256 = _request_sha256(
        registry="subject",
        action="approve",
        actor_user_id=actor_user_id,
        command=command.model_dump(mode="json"),
    )
    replay = await _idempotency_replay(
        session,
        scope=_scope("subject", "approve"),
        key=key,
        request_sha256=request_sha256,
    )
    if replay is not None:
        return replay

    await _require_actor(session, actor_user_id)
    source_url = str(command.canonical_source_url)
    identity_sha256 = canonical_source_sha256(source_url)
    evidence = command.evidence.model_dump(mode="json")
    evidence_sha256 = canonical_sha256(evidence)
    rows = list(
        (
            await session.scalars(
                select(SubjectApproval)
                .where(
                    or_(
                        SubjectApproval.canonical_source_sha256 == identity_sha256,
                        SubjectApproval.slug == command.slug,
                    )
                )
                .order_by(SubjectApproval.approval_version, SubjectApproval.id)
                .with_for_update()
            )
        ).all()
    )
    for row in rows:
        if row.is_current:
            _validate_current_approval(row, registry="subject")
    current_source = next(
        (row for row in rows if row.is_current and row.canonical_source_sha256 == identity_sha256),
        None,
    )
    slug_collision = next(
        (
            row
            for row in rows
            if row.is_current
            and row.slug == command.slug
            and row.canonical_source_sha256 != identity_sha256
        ),
        None,
    )
    if slug_collision is not None:
        raise ComplianceRegistryConflictError("subject slug belongs to another current approval")
    if current_source is not None and _subject_matches(
        current_source,
        command=command,
        source_url=source_url,
        evidence=evidence,
        evidence_sha256=evidence_sha256,
    ):
        return await _record_reaffirmation(
            session,
            current_source,
            registry="subject",
            identity_sha256=identity_sha256,
            actor_user_id=actor_user_id,
            scope=_scope("subject", "approve"),
            key=key,
            request_sha256=request_sha256,
            now=approved_at,
        )

    version = max((row.approval_version for row in rows), default=0) + 1
    if current_source is not None:
        _supersede(current_source, actor_user_id=actor_user_id, now=approved_at)
        await session.flush()
    approval = SubjectApproval(
        slug=command.slug,
        display_name=command.display_name,
        canonical_source_url=source_url,
        canonical_source_sha256=identity_sha256,
        canonical_age=command.canonical_age,
        clearly_adult=True,
        is_fictional=True,
        is_aged_up_minor=False,
        distribution_rights_approved=True,
        adult_derivative_rights_approved=True,
        evidence=evidence,
        evidence_sha256=evidence_sha256,
        status=ApprovalStatus.APPROVED,
        is_current=True,
        approval_version=version,
        approved_by_user_id=actor_user_id,
        approved_at=approved_at,
        revoked_by_user_id=None,
        revoked_at=None,
    )
    return await _insert_approval(
        session,
        approval,
        registry="subject",
        identity_sha256=identity_sha256,
        actor_user_id=actor_user_id,
        scope=_scope("subject", "approve"),
        key=key,
        request_sha256=request_sha256,
        now=approved_at,
    )


async def approve_model_artifact(
    session: AsyncSession,
    *,
    command: ModelArtifactApprovalCreate,
    actor_user_id: UUID,
    idempotency_key: str,
    allow_managed_registration: bool = False,
    now: datetime | None = None,
) -> ApprovalMutationResult:
    approved_at = _as_utc(now or datetime.now(UTC))
    key = _idempotency_key(idempotency_key)
    request_sha256 = _request_sha256(
        registry="model_artifact",
        action="approve",
        actor_user_id=actor_user_id,
        command=command.model_dump(mode="json"),
    )
    replay = await _idempotency_replay(
        session,
        scope=_scope("model_artifact", "approve"),
        key=key,
        request_sha256=request_sha256,
    )
    if replay is not None:
        return replay

    await _require_actor(session, actor_user_id)
    managed_lifecycles = list(
        (
            await session.scalars(
                select(ManagedLoraArtifact.lifecycle)
                .where(ManagedLoraArtifact.artifact_sha256 == command.artifact_sha256)
                .with_for_update()
            )
        ).all()
    )
    if managed_lifecycles and (
        not allow_managed_registration
        or any(lifecycle != ManagedLoraLifecycle.PURGED for lifecycle in managed_lifecycles)
    ):
        raise ComplianceRegistryConflictError(
            "managed LoRA approvals can only change through the LoRA manager"
        )
    managed_storage = command.storage_key.startswith("worker/managed-loras/sha256/")
    if managed_storage and not allow_managed_registration:
        raise ComplianceRegistryConflictError(
            "managed LoRA approvals can only be created by the onboarding runtime"
        )
    evidence = command.evidence.model_dump(mode="json")
    evidence_sha256 = canonical_sha256(evidence)
    rows = list(
        (
            await session.scalars(
                select(ModelArtifactApproval)
                .where(ModelArtifactApproval.artifact_sha256 == command.artifact_sha256)
                .order_by(ModelArtifactApproval.approval_version)
                .with_for_update()
            )
        ).all()
    )
    for row in rows:
        if row.is_current:
            _validate_current_approval(row, registry="model_artifact")
    current = next((row for row in rows if row.is_current), None)
    if (
        allow_managed_registration
        and current is not None
        and not current.storage_key.startswith("worker/managed-loras/sha256/")
    ):
        raise ComplianceRegistryConflictError(
            "a protected static LoRA approval already owns this artifact"
        )
    if current is not None and _artifact_matches(
        current,
        command=command,
        evidence=evidence,
        evidence_sha256=evidence_sha256,
    ):
        return await _record_reaffirmation(
            session,
            current,
            registry="model_artifact",
            identity_sha256=command.artifact_sha256,
            actor_user_id=actor_user_id,
            scope=_scope("model_artifact", "approve"),
            key=key,
            request_sha256=request_sha256,
            now=approved_at,
        )

    version = max((row.approval_version for row in rows), default=0) + 1
    if current is not None:
        _supersede(current, actor_user_id=actor_user_id, now=approved_at)
        await session.flush()
    approval = ModelArtifactApproval(
        artifact_sha256=command.artifact_sha256,
        name=command.name,
        kind=command.kind,
        source_url=str(command.source_url),
        storage_key=command.storage_key,
        license_url=str(command.license_url),
        commercial_use_approved=True,
        adult_use_approved=True,
        safetensors_verified=True,
        evidence=evidence,
        evidence_sha256=evidence_sha256,
        status=ApprovalStatus.APPROVED,
        is_current=True,
        approval_version=version,
        approved_by_user_id=actor_user_id,
        approved_at=approved_at,
        revoked_by_user_id=None,
        revoked_at=None,
    )
    return await _insert_approval(
        session,
        approval,
        registry="model_artifact",
        identity_sha256=command.artifact_sha256,
        actor_user_id=actor_user_id,
        scope=_scope("model_artifact", "approve"),
        key=key,
        request_sha256=request_sha256,
        now=approved_at,
    )


async def approve_workflow(
    session: AsyncSession,
    *,
    command: WorkflowApprovalCreate,
    actor_user_id: UUID,
    idempotency_key: str,
    now: datetime | None = None,
) -> ApprovalMutationResult:
    approved_at = _as_utc(now or datetime.now(UTC))
    key = _idempotency_key(idempotency_key)
    request_sha256 = _request_sha256(
        registry="workflow",
        action="approve",
        actor_user_id=actor_user_id,
        command=command.model_dump(mode="json"),
    )
    replay = await _idempotency_replay(
        session,
        scope=_scope("workflow", "approve"),
        key=key,
        request_sha256=request_sha256,
    )
    if replay is not None:
        return replay

    await _require_actor(session, actor_user_id)
    evidence = command.evidence.model_dump(mode="json")
    evidence_sha256 = canonical_sha256(evidence)
    rows = list(
        (
            await session.scalars(
                select(WorkflowApproval)
                .where(WorkflowApproval.workflow_sha256 == command.workflow_sha256)
                .order_by(WorkflowApproval.approval_version)
                .with_for_update()
            )
        ).all()
    )
    for row in rows:
        if row.is_current:
            _validate_current_approval(row, registry="workflow")
    current = next((row for row in rows if row.is_current), None)
    if current is not None and _workflow_matches(
        current,
        command=command,
        evidence=evidence,
        evidence_sha256=evidence_sha256,
    ):
        return await _record_reaffirmation(
            session,
            current,
            registry="workflow",
            identity_sha256=command.workflow_sha256,
            actor_user_id=actor_user_id,
            scope=_scope("workflow", "approve"),
            key=key,
            request_sha256=request_sha256,
            now=approved_at,
        )

    version = max((row.approval_version for row in rows), default=0) + 1
    if current is not None:
        _supersede(current, actor_user_id=actor_user_id, now=approved_at)
        await session.flush()
    approval = WorkflowApproval(
        workflow_sha256=command.workflow_sha256,
        name=command.name,
        version=command.version,
        object_key=command.object_key,
        reviewed_node_classes=command.reviewed_node_classes,
        capabilities=[str(item) for item in command.capabilities],
        evidence=evidence,
        evidence_sha256=evidence_sha256,
        status=ApprovalStatus.APPROVED,
        is_current=True,
        approval_version=version,
        approved_by_user_id=actor_user_id,
        approved_at=approved_at,
        revoked_by_user_id=None,
        revoked_at=None,
    )
    return await _insert_approval(
        session,
        approval,
        registry="workflow",
        identity_sha256=command.workflow_sha256,
        actor_user_id=actor_user_id,
        scope=_scope("workflow", "approve"),
        key=key,
        request_sha256=request_sha256,
        now=approved_at,
    )


async def revoke_subject(
    session: AsyncSession,
    *,
    approval_id: UUID,
    command: ApprovalRevoke,
    actor_user_id: UUID,
    idempotency_key: str,
    now: datetime | None = None,
) -> ApprovalMutationResult:
    return await _revoke(
        session,
        model=SubjectApproval,
        registry="subject",
        approval_id=approval_id,
        command=command,
        actor_user_id=actor_user_id,
        idempotency_key=idempotency_key,
        now=now,
    )


async def revoke_model_artifact(
    session: AsyncSession,
    *,
    approval_id: UUID,
    command: ApprovalRevoke,
    actor_user_id: UUID,
    idempotency_key: str,
    now: datetime | None = None,
) -> ApprovalMutationResult:
    managed = await session.scalar(
        select(ManagedLoraArtifact.id)
        .where(ManagedLoraArtifact.approval_id == approval_id)
        .limit(1)
    )
    if managed is not None:
        raise ComplianceRegistryConflictError("managed LoRAs must be deleted from the LoRA manager")
    return await _revoke(
        session,
        model=ModelArtifactApproval,
        registry="model_artifact",
        approval_id=approval_id,
        command=command,
        actor_user_id=actor_user_id,
        idempotency_key=idempotency_key,
        now=now,
    )


async def revoke_workflow(
    session: AsyncSession,
    *,
    approval_id: UUID,
    command: ApprovalRevoke,
    actor_user_id: UUID,
    idempotency_key: str,
    now: datetime | None = None,
) -> ApprovalMutationResult:
    return await _revoke(
        session,
        model=WorkflowApproval,
        registry="workflow",
        approval_id=approval_id,
        command=command,
        actor_user_id=actor_user_id,
        idempotency_key=idempotency_key,
        now=now,
    )


async def list_current_approvals(
    session: AsyncSession,
    *,
    registry: RegistryName,
) -> tuple[ApprovalMutationResult, ...]:
    if registry == "subject":
        rows: list[ApprovalModel] = list(
            (
                await session.scalars(
                    select(SubjectApproval)
                    .where(SubjectApproval.is_current.is_(True))
                    .order_by(SubjectApproval.slug)
                )
            ).all()
        )
    elif registry == "model_artifact":
        rows = list(
            (
                await session.scalars(
                    select(ModelArtifactApproval)
                    .where(ModelArtifactApproval.is_current.is_(True))
                    .order_by(ModelArtifactApproval.kind, ModelArtifactApproval.name)
                )
            ).all()
        )
    elif registry == "workflow":
        rows = list(
            (
                await session.scalars(
                    select(WorkflowApproval)
                    .where(WorkflowApproval.is_current.is_(True))
                    .order_by(WorkflowApproval.name, WorkflowApproval.version)
                )
            ).all()
        )
    else:
        raise ComplianceRegistryInputError("registry is invalid")
    for row in rows:
        _validate_current_approval(row, registry=registry)
    return tuple(_result(row, registry=registry, replayed=False) for row in rows)


async def _revoke(
    session: AsyncSession,
    *,
    model: ApprovalModelType,
    registry: RegistryName,
    approval_id: UUID,
    command: ApprovalRevoke,
    actor_user_id: UUID,
    idempotency_key: str,
    now: datetime | None,
) -> ApprovalMutationResult:
    revoked_at = _as_utc(now or datetime.now(UTC))
    key = _idempotency_key(idempotency_key)
    request_sha256 = _request_sha256(
        registry=registry,
        action="revoke",
        actor_user_id=actor_user_id,
        command={
            "approval_id": str(approval_id),
            **command.model_dump(mode="json"),
        },
    )
    scope = _scope(registry, "revoke")
    replay = await _idempotency_replay(
        session,
        scope=scope,
        key=key,
        request_sha256=request_sha256,
    )
    if replay is not None:
        return replay
    await _require_actor(session, actor_user_id)
    approval = cast(
        ApprovalModel | None,
        await session.scalar(select(model).where(model.id == approval_id).with_for_update()),
    )
    if approval is None:
        raise ComplianceRegistryNotFoundError("approval was not found")
    if approval.approval_version != command.expected_approval_version:
        raise ComplianceRegistryConflictError("approval version is stale")
    if approval.status != ApprovalStatus.APPROVED or not approval.is_current:
        raise ComplianceRegistryConflictError("approval is not current")

    approval.status = ApprovalStatus.REVOKED
    approval.is_current = False
    approval.revoked_by_user_id = actor_user_id
    approval.revoked_at = revoked_at
    result = _result(approval, registry=registry, replayed=False)
    body = _response_body(result)
    session.add(
        IdempotencyRecord(
            scope=scope,
            idempotency_key=key,
            request_sha256=request_sha256,
            response_status=200,
            response_body=body,
            created_at=revoked_at,
            expires_at=None,
        )
    )
    session.add(
        AuditEvent(
            actor=str(actor_user_id),
            action=f"compliance.{registry}.revoked",
            resource_type=f"{registry}_approval",
            resource_id=approval.id,
            correlation_id=key,
            detail={
                "approval_version": approval.approval_version,
                "identity_sha256": result.identity_sha256,
                "evidence_sha256": approval.evidence_sha256,
                "reason_code": command.reason_code,
                "has_note": command.note is not None,
            },
            occurred_at=revoked_at,
        )
    )
    try:
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        replay = await _idempotency_replay(
            session,
            scope=scope,
            key=key,
            request_sha256=request_sha256,
        )
        if replay is not None:
            return replay
        raise ComplianceRegistryConflictError(
            "approval revocation conflicts with current state"
        ) from error
    return result


async def _insert_approval(
    session: AsyncSession,
    approval: ApprovalModel,
    *,
    registry: RegistryName,
    identity_sha256: str,
    actor_user_id: UUID,
    scope: str,
    key: str,
    request_sha256: str,
    now: datetime,
) -> ApprovalMutationResult:
    session.add(approval)
    try:
        await session.flush()
        result = _result(approval, registry=registry, replayed=False)
        session.add(
            IdempotencyRecord(
                scope=scope,
                idempotency_key=key,
                request_sha256=request_sha256,
                response_status=201,
                response_body=_response_body(result),
                created_at=now,
                expires_at=None,
            )
        )
        session.add(
            AuditEvent(
                actor=str(actor_user_id),
                action=f"compliance.{registry}.approved",
                resource_type=f"{registry}_approval",
                resource_id=approval.id,
                correlation_id=key,
                detail={
                    "approval_version": approval.approval_version,
                    "identity_sha256": identity_sha256,
                    "evidence_sha256": approval.evidence_sha256,
                },
                occurred_at=now,
            )
        )
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        replay = await _idempotency_replay(
            session,
            scope=scope,
            key=key,
            request_sha256=request_sha256,
        )
        if replay is not None:
            return replay
        raise ComplianceRegistryConflictError(
            "approval conflicts with current registry state"
        ) from error
    return result


async def _record_reaffirmation(
    session: AsyncSession,
    approval: ApprovalModel,
    *,
    registry: RegistryName,
    identity_sha256: str,
    actor_user_id: UUID,
    scope: str,
    key: str,
    request_sha256: str,
    now: datetime,
) -> ApprovalMutationResult:
    result = _result(approval, registry=registry, replayed=False)
    session.add(
        IdempotencyRecord(
            scope=scope,
            idempotency_key=key,
            request_sha256=request_sha256,
            response_status=200,
            response_body=_response_body(result),
            created_at=now,
            expires_at=None,
        )
    )
    session.add(
        AuditEvent(
            actor=str(actor_user_id),
            action=f"compliance.{registry}.reaffirmed",
            resource_type=f"{registry}_approval",
            resource_id=approval.id,
            correlation_id=key,
            detail={
                "approval_version": approval.approval_version,
                "identity_sha256": identity_sha256,
                "evidence_sha256": approval.evidence_sha256,
            },
            occurred_at=now,
        )
    )
    try:
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        replay = await _idempotency_replay(
            session,
            scope=scope,
            key=key,
            request_sha256=request_sha256,
        )
        if replay is not None:
            return replay
        raise ComplianceRegistryConflictError(
            "approval reaffirmation conflicts with current registry state"
        ) from error
    return result


async def _require_actor(session: AsyncSession, actor_user_id: UUID) -> None:
    actor = await session.scalar(
        select(AdminUser)
        .where(
            AdminUser.id == actor_user_id,
            AdminUser.is_active.is_(True),
        )
        .with_for_update(read=True)
    )
    if actor is None or actor.role not in _MUTATING_ROLES:
        raise ComplianceRegistryConflictError("compliance actor is not an active administrator")


def _supersede(
    approval: ApprovalModel,
    *,
    actor_user_id: UUID,
    now: datetime,
) -> None:
    approval.status = ApprovalStatus.REVOKED
    approval.is_current = False
    approval.revoked_by_user_id = actor_user_id
    approval.revoked_at = now


def _subject_matches(
    approval: SubjectApproval,
    *,
    command: SubjectApprovalCreate,
    source_url: str,
    evidence: dict[str, Any],
    evidence_sha256: str,
) -> bool:
    return (
        approval.status == ApprovalStatus.APPROVED
        and approval.is_current
        and approval.slug == command.slug
        and approval.display_name == command.display_name
        and approval.canonical_source_url == source_url
        and approval.canonical_age == command.canonical_age
        and approval.clearly_adult
        and approval.is_fictional
        and not approval.is_aged_up_minor
        and approval.distribution_rights_approved
        and approval.adult_derivative_rights_approved
        and approval.evidence == evidence
        and approval.evidence_sha256 == evidence_sha256
    )


def _artifact_matches(
    approval: ModelArtifactApproval,
    *,
    command: ModelArtifactApprovalCreate,
    evidence: dict[str, Any],
    evidence_sha256: str,
) -> bool:
    return (
        approval.status == ApprovalStatus.APPROVED
        and approval.is_current
        and approval.name == command.name
        and approval.kind == command.kind
        and approval.source_url == str(command.source_url)
        and approval.storage_key == command.storage_key
        and approval.license_url == str(command.license_url)
        and approval.commercial_use_approved
        and approval.adult_use_approved
        and approval.safetensors_verified
        and approval.evidence == evidence
        and approval.evidence_sha256 == evidence_sha256
    )


def _workflow_matches(
    approval: WorkflowApproval,
    *,
    command: WorkflowApprovalCreate,
    evidence: dict[str, Any],
    evidence_sha256: str,
) -> bool:
    return (
        approval.status == ApprovalStatus.APPROVED
        and approval.is_current
        and approval.name == command.name
        and approval.version == command.version
        and approval.object_key == command.object_key
        and approval.reviewed_node_classes == command.reviewed_node_classes
        and approval.capabilities == [str(item) for item in command.capabilities]
        and approval.evidence == evidence
        and approval.evidence_sha256 == evidence_sha256
    )


def _validate_current_approval(
    approval: ApprovalModel,
    *,
    registry: RegistryName,
) -> None:
    try:
        if (
            not approval.is_current
            or approval.status != ApprovalStatus.APPROVED
            or approval.revoked_by_user_id is not None
            or approval.revoked_at is not None
            or approval.approval_version <= 0
            or _SHA256_PATTERN.fullmatch(approval.evidence_sha256) is None
            or canonical_sha256(approval.evidence) != approval.evidence_sha256
        ):
            raise ValueError
        evidence = ApprovalEvidence.model_validate(approval.evidence)
        if isinstance(approval, SubjectApproval):
            if registry != "subject":
                raise ValueError
            command = SubjectApprovalCreate(
                slug=approval.slug,
                display_name=approval.display_name,
                canonical_source_url=approval.canonical_source_url,
                canonical_age=approval.canonical_age,
                clearly_adult=approval.clearly_adult,
                is_fictional=approval.is_fictional,
                is_aged_up_minor=approval.is_aged_up_minor,
                distribution_rights_approved=approval.distribution_rights_approved,
                adult_derivative_rights_approved=approval.adult_derivative_rights_approved,
                evidence=evidence,
            )
            if (
                canonical_source_sha256(str(command.canonical_source_url))
                != approval.canonical_source_sha256
            ):
                raise ValueError
        elif isinstance(approval, ModelArtifactApproval):
            if registry != "model_artifact":
                raise ValueError
            ModelArtifactApprovalCreate(
                artifact_sha256=approval.artifact_sha256,
                name=approval.name,
                kind=approval.kind,
                source_url=approval.source_url,
                storage_key=approval.storage_key,
                license_url=approval.license_url,
                commercial_use_approved=approval.commercial_use_approved,
                adult_use_approved=approval.adult_use_approved,
                safetensors_verified=approval.safetensors_verified,
                evidence=evidence,
            )
        elif isinstance(approval, WorkflowApproval):
            if registry != "workflow":
                raise ValueError
            WorkflowApprovalCreate(
                workflow_sha256=approval.workflow_sha256,
                name=approval.name,
                version=approval.version,
                object_key=approval.object_key,
                reviewed_node_classes=approval.reviewed_node_classes,
                capabilities=approval.capabilities,
                evidence=evidence,
            )
        else:
            raise ValueError
    except (TypeError, ValueError):
        raise ComplianceRegistryConflictError(
            "current compliance approval failed integrity validation"
        ) from None


def _result(
    approval: ApprovalModel,
    *,
    registry: RegistryName,
    replayed: bool,
) -> ApprovalMutationResult:
    if isinstance(approval, SubjectApproval):
        identity_sha256 = approval.canonical_source_sha256
        name = approval.display_name
    elif isinstance(approval, ModelArtifactApproval):
        identity_sha256 = approval.artifact_sha256
        name = approval.name
    else:
        identity_sha256 = approval.workflow_sha256
        name = approval.name
    return ApprovalMutationResult(
        registry=registry,
        approval_id=approval.id,
        identity_sha256=identity_sha256,
        name=name,
        approval_version=approval.approval_version,
        status=approval.status,
        is_current=approval.is_current,
        evidence_sha256=approval.evidence_sha256,
        approved_by_user_id=approval.approved_by_user_id,
        approved_at=_as_utc(approval.approved_at),
        revoked_by_user_id=approval.revoked_by_user_id,
        revoked_at=(_as_utc(approval.revoked_at) if approval.revoked_at is not None else None),
        replayed=replayed,
    )


def _response_body(result: ApprovalMutationResult) -> dict[str, Any]:
    return {
        "schema": "compliance-approval-result/v1",
        "registry": result.registry,
        "approval_id": str(result.approval_id),
        "identity_sha256": result.identity_sha256,
        "name": result.name,
        "approval_version": result.approval_version,
        "status": result.status.value,
        "is_current": result.is_current,
        "evidence_sha256": result.evidence_sha256,
        "approved_by_user_id": str(result.approved_by_user_id),
        "approved_at": result.approved_at.isoformat(),
        "revoked_by_user_id": (
            str(result.revoked_by_user_id) if result.revoked_by_user_id is not None else None
        ),
        "revoked_at": (result.revoked_at.isoformat() if result.revoked_at is not None else None),
    }


async def _idempotency_replay(
    session: AsyncSession,
    *,
    scope: str,
    key: str,
    request_sha256: str,
) -> ApprovalMutationResult | None:
    record = await session.scalar(
        select(IdempotencyRecord).where(
            IdempotencyRecord.scope == scope,
            IdempotencyRecord.idempotency_key == key,
        )
    )
    if record is None:
        return None
    if record.request_sha256 != request_sha256:
        raise ComplianceRegistryConflictError(
            "idempotency key was already used for another request"
        )
    return _result_from_response(record.response_body)


def _result_from_response(body: object) -> ApprovalMutationResult:
    try:
        if not isinstance(body, dict) or set(body) != {
            "schema",
            "registry",
            "approval_id",
            "identity_sha256",
            "name",
            "approval_version",
            "status",
            "is_current",
            "evidence_sha256",
            "approved_by_user_id",
            "approved_at",
            "revoked_by_user_id",
            "revoked_at",
        }:
            raise ValueError
        if body.get("schema") != "compliance-approval-result/v1":
            raise ValueError
        registry = body["registry"]
        if registry not in {"subject", "model_artifact", "workflow"}:
            raise ValueError
        identity_sha256 = body["identity_sha256"]
        evidence_sha256 = body["evidence_sha256"]
        name = body["name"]
        version = body["approval_version"]
        is_current = body["is_current"]
        revoked_id = body["revoked_by_user_id"]
        revoked_at = body["revoked_at"]
        if (
            not isinstance(identity_sha256, str)
            or _SHA256_PATTERN.fullmatch(identity_sha256) is None
            or not isinstance(evidence_sha256, str)
            or _SHA256_PATTERN.fullmatch(evidence_sha256) is None
            or not isinstance(name, str)
            or not name
            or len(name) > 200
            or name != name.strip()
            or any(ord(character) < 32 for character in name)
            or isinstance(version, bool)
            or not isinstance(version, int)
            or version <= 0
            or not isinstance(is_current, bool)
        ):
            raise ValueError
        approval_id = _canonical_uuid(body["approval_id"])
        approved_by_user_id = _canonical_uuid(body["approved_by_user_id"])
        approved_at = _aware_timestamp(body["approved_at"])
        parsed_revoked_at = _aware_timestamp(revoked_at) if isinstance(revoked_at, str) else None
        status = ApprovalStatus(body["status"])
        if (
            (
                status == ApprovalStatus.APPROVED
                and (not is_current or revoked_id is not None or parsed_revoked_at is not None)
            )
            or (
                status == ApprovalStatus.REVOKED
                and (is_current or not isinstance(revoked_id, str) or parsed_revoked_at is None)
            )
            or (parsed_revoked_at is not None and parsed_revoked_at < approved_at)
        ):
            raise ValueError
        return ApprovalMutationResult(
            registry=cast(RegistryName, registry),
            approval_id=approval_id,
            identity_sha256=identity_sha256,
            name=name,
            approval_version=version,
            status=status,
            is_current=is_current,
            evidence_sha256=evidence_sha256,
            approved_by_user_id=approved_by_user_id,
            approved_at=approved_at,
            revoked_by_user_id=(
                _canonical_uuid(revoked_id) if isinstance(revoked_id, str) else None
            ),
            revoked_at=parsed_revoked_at,
            replayed=True,
        )
    except (KeyError, TypeError, ValueError):
        raise ComplianceRegistryConflictError("compliance idempotency record is invalid") from None


def _request_sha256(
    *,
    registry: RegistryName,
    action: Literal["approve", "revoke"],
    actor_user_id: UUID,
    command: dict[str, Any],
) -> str:
    return canonical_sha256(
        {
            "schema": "compliance-registry-command/v1",
            "registry": registry,
            "action": action,
            "actor_user_id": str(actor_user_id),
            "command": command,
        }
    )


def _scope(registry: RegistryName, action: Literal["approve", "revoke"]) -> str:
    return f"compliance:{registry}:{action}:v1"


def _idempotency_key(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > _IDEMPOTENCY_KEY_MAX_LENGTH
        or any(ord(character) < 32 for character in value)
    ):
        raise ComplianceRegistryInputError("idempotency key must be 1 to 200 visible characters")
    return value


def _canonical_uuid(value: object) -> UUID:
    if not isinstance(value, str):
        raise ValueError
    parsed = UUID(value)
    if str(parsed) != value:
        raise ValueError
    return parsed


def _aware_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError
    return parsed.astimezone(UTC)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
