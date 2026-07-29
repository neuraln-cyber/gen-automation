from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Response, status

from gen_automation.api.security import (
    ComplianceManager,
    ComplianceReader,
    Session,
)
from gen_automation.domain.compliance_registry import (
    ApprovalRead,
    ApprovalRevoke,
    ModelArtifactApprovalCreate,
    SubjectApprovalCreate,
    WorkflowApprovalCreate,
)
from gen_automation.services.compliance_registry import (
    ApprovalMutationResult,
    ComplianceRegistryConflictError,
    ComplianceRegistryInputError,
    ComplianceRegistryNotFoundError,
    approve_model_artifact,
    approve_subject,
    approve_workflow,
    list_current_approvals,
    revoke_model_artifact,
    revoke_subject,
    revoke_workflow,
)

router = APIRouter(prefix="/compliance", tags=["compliance"])
IdempotencyKey = Annotated[
    str,
    Header(
        alias="Idempotency-Key",
        min_length=1,
        max_length=200,
        description="Stable unique key for this logical compliance mutation",
    ),
]
RegistryPath = Literal["subject", "model_artifact", "workflow"]


@router.get("/{registry}", response_model=list[ApprovalRead])
async def get_current_approvals(
    registry: RegistryPath,
    session: Session,
    _principal: ComplianceReader,
) -> list[ApprovalRead]:
    try:
        results = await list_current_approvals(session, registry=registry)
    except ComplianceRegistryInputError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="compliance registry request is invalid",
        ) from error
    return [_read(result) for result in results]


@router.post(
    "/subjects",
    response_model=ApprovalRead,
    status_code=status.HTTP_201_CREATED,
)
async def post_subject_approval(
    command: SubjectApprovalCreate,
    session: Session,
    principal: ComplianceManager,
    idempotency_key: IdempotencyKey,
    response: Response,
) -> ApprovalRead:
    try:
        result = await approve_subject(
            session,
            command=command,
            actor_user_id=principal.user_id,
            idempotency_key=idempotency_key,
        )
    except (
        ComplianceRegistryInputError,
        ComplianceRegistryNotFoundError,
        ComplianceRegistryConflictError,
    ) as error:
        raise _http_error(error) from error
    response.headers["Idempotency-Replayed"] = str(result.replayed).lower()
    return _read(result)


@router.post(
    "/model-artifacts",
    response_model=ApprovalRead,
    status_code=status.HTTP_201_CREATED,
)
async def post_model_artifact_approval(
    command: ModelArtifactApprovalCreate,
    session: Session,
    principal: ComplianceManager,
    idempotency_key: IdempotencyKey,
    response: Response,
) -> ApprovalRead:
    try:
        result = await approve_model_artifact(
            session,
            command=command,
            actor_user_id=principal.user_id,
            idempotency_key=idempotency_key,
        )
    except (
        ComplianceRegistryInputError,
        ComplianceRegistryNotFoundError,
        ComplianceRegistryConflictError,
    ) as error:
        raise _http_error(error) from error
    response.headers["Idempotency-Replayed"] = str(result.replayed).lower()
    return _read(result)


@router.post(
    "/workflows",
    response_model=ApprovalRead,
    status_code=status.HTTP_201_CREATED,
)
async def post_workflow_approval(
    command: WorkflowApprovalCreate,
    session: Session,
    principal: ComplianceManager,
    idempotency_key: IdempotencyKey,
    response: Response,
) -> ApprovalRead:
    try:
        result = await approve_workflow(
            session,
            command=command,
            actor_user_id=principal.user_id,
            idempotency_key=idempotency_key,
        )
    except (
        ComplianceRegistryInputError,
        ComplianceRegistryNotFoundError,
        ComplianceRegistryConflictError,
    ) as error:
        raise _http_error(error) from error
    response.headers["Idempotency-Replayed"] = str(result.replayed).lower()
    return _read(result)


@router.post("/subjects/{approval_id}:revoke", response_model=ApprovalRead)
async def post_subject_revocation(
    approval_id: UUID,
    command: ApprovalRevoke,
    session: Session,
    principal: ComplianceManager,
    idempotency_key: IdempotencyKey,
    response: Response,
) -> ApprovalRead:
    try:
        result = await revoke_subject(
            session,
            approval_id=approval_id,
            command=command,
            actor_user_id=principal.user_id,
            idempotency_key=idempotency_key,
        )
    except (
        ComplianceRegistryInputError,
        ComplianceRegistryNotFoundError,
        ComplianceRegistryConflictError,
    ) as error:
        raise _http_error(error) from error
    response.headers["Idempotency-Replayed"] = str(result.replayed).lower()
    return _read(result)


@router.post(
    "/model-artifacts/{approval_id}:revoke",
    response_model=ApprovalRead,
)
async def post_model_artifact_revocation(
    approval_id: UUID,
    command: ApprovalRevoke,
    session: Session,
    principal: ComplianceManager,
    idempotency_key: IdempotencyKey,
    response: Response,
) -> ApprovalRead:
    try:
        result = await revoke_model_artifact(
            session,
            approval_id=approval_id,
            command=command,
            actor_user_id=principal.user_id,
            idempotency_key=idempotency_key,
        )
    except (
        ComplianceRegistryInputError,
        ComplianceRegistryNotFoundError,
        ComplianceRegistryConflictError,
    ) as error:
        raise _http_error(error) from error
    response.headers["Idempotency-Replayed"] = str(result.replayed).lower()
    return _read(result)


@router.post("/workflows/{approval_id}:revoke", response_model=ApprovalRead)
async def post_workflow_revocation(
    approval_id: UUID,
    command: ApprovalRevoke,
    session: Session,
    principal: ComplianceManager,
    idempotency_key: IdempotencyKey,
    response: Response,
) -> ApprovalRead:
    try:
        result = await revoke_workflow(
            session,
            approval_id=approval_id,
            command=command,
            actor_user_id=principal.user_id,
            idempotency_key=idempotency_key,
        )
    except (
        ComplianceRegistryInputError,
        ComplianceRegistryNotFoundError,
        ComplianceRegistryConflictError,
    ) as error:
        raise _http_error(error) from error
    response.headers["Idempotency-Replayed"] = str(result.replayed).lower()
    return _read(result)


def _read(result: ApprovalMutationResult) -> ApprovalRead:
    return ApprovalRead(
        registry=result.registry,
        approval_id=str(result.approval_id),
        identity_sha256=result.identity_sha256,
        name=result.name,
        approval_version=result.approval_version,
        status=result.status,
        is_current=result.is_current,
        evidence_sha256=result.evidence_sha256,
        approved_by_user_id=str(result.approved_by_user_id),
        approved_at=result.approved_at.isoformat(),
        revoked_by_user_id=(
            str(result.revoked_by_user_id) if result.revoked_by_user_id is not None else None
        ),
        revoked_at=(result.revoked_at.isoformat() if result.revoked_at is not None else None),
        replayed=result.replayed,
    )


def _http_error(error: Exception) -> HTTPException:
    if isinstance(error, ComplianceRegistryNotFoundError):
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="compliance approval was not found",
        )
    if isinstance(error, ComplianceRegistryConflictError):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="compliance request conflicts with current registry state",
        )
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="compliance request is invalid",
    )
