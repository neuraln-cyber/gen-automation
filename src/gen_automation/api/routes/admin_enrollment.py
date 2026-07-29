from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from gen_automation.api.security import (
    Session,
    UserManager,
    require_same_origin,
)
from gen_automation.config import Settings
from gen_automation.domain.enums import AdminRole
from gen_automation.services.admin_enrollment import (
    AdminEnrollmentBusyError,
    AdminEnrollmentConflictError,
    AdminEnrollmentInputError,
    AdminEnrollmentInvalidError,
    AdminEnrollmentService,
)

router = APIRouter(prefix="/auth/admin-enrollments", tags=["authentication"])


def enrollment_service(request: Request) -> AdminEnrollmentService:
    settings: Settings = request.app.state.settings
    service: AdminEnrollmentService | None = request.app.state.admin_enrollment_service
    if not settings.auth_enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="authentication is disabled",
        )
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="administrator enrollment is unavailable",
        )
    return service


def _require_json(request: Request) -> None:
    content_type = request.headers.get("content-type", "")
    media_type = content_type.partition(";")[0].strip().casefold()
    if media_type != "application/json":
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="application/json is required",
        )


EnrollmentService = Annotated[
    AdminEnrollmentService,
    Depends(enrollment_service),
]
JsonRequest = Annotated[None, Depends(_require_json)]


class CreateAdminInvitationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=200)
    display_name: str = Field(min_length=1, max_length=200)
    role: AdminRole


class AdminInvitationResponse(BaseModel):
    enrollment_id: str
    username: str
    display_name: str
    role: AdminRole
    expires_at: str
    invite_token: str


class InspectAdminEnrollmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    invite_token: str = Field(min_length=1, max_length=128)


class AdminEnrollmentInspectionResponse(BaseModel):
    username: str
    display_name: str
    role: AdminRole
    expires_at: str
    totp_secret: str
    totp_provisioning_uri: str


class CompleteAdminEnrollmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    invite_token: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=1024)
    totp_code: str = Field(pattern=r"^[0-9]{6}$")


class AdminEnrollmentCompletionResponse(BaseModel):
    user_id: str
    username: str
    display_name: str
    role: AdminRole


@router.post(
    "/invitations",
    response_model=AdminInvitationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_admin_invitation(
    command: CreateAdminInvitationRequest,
    request: Request,
    principal: UserManager,
    session: Session,
    service: EnrollmentService,
    _json_request: JsonRequest,
) -> AdminInvitationResponse:
    settings: Settings = request.app.state.settings
    try:
        result = await service.create_invitation(
            session,
            username=command.username,
            display_name=command.display_name,
            role=command.role,
            invited_by_user_id=principal.user_id,
            correlation_id=getattr(request.state, "request_id", "unavailable"),
            ttl_seconds=settings.auth_enrollment_invite_ttl_seconds,
        )
    except AdminEnrollmentInputError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(error),
        ) from error
    except AdminEnrollmentConflictError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error
    return AdminInvitationResponse(
        enrollment_id=str(result.enrollment_id),
        username=result.username,
        display_name=result.display_name,
        role=result.role,
        expires_at=result.expires_at.isoformat(),
        invite_token=result.invite_token,
    )


@router.post(
    "/inspect",
    response_model=AdminEnrollmentInspectionResponse,
)
async def inspect_admin_enrollment(
    command: InspectAdminEnrollmentRequest,
    request: Request,
    session: Session,
    service: EnrollmentService,
    _json_request: JsonRequest,
) -> AdminEnrollmentInspectionResponse:
    require_same_origin(request)
    try:
        result = await service.inspect_capability(
            session,
            invite_token=command.invite_token,
        )
    except AdminEnrollmentInvalidError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(error),
        ) from error
    return AdminEnrollmentInspectionResponse(
        username=result.username,
        display_name=result.display_name,
        role=result.role,
        expires_at=result.expires_at.isoformat(),
        totp_secret=result.totp_secret,
        totp_provisioning_uri=result.totp_provisioning_uri,
    )


@router.post(
    "/complete",
    response_model=AdminEnrollmentCompletionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def complete_admin_enrollment(
    command: CompleteAdminEnrollmentRequest,
    request: Request,
    session: Session,
    service: EnrollmentService,
    _json_request: JsonRequest,
) -> AdminEnrollmentCompletionResponse:
    require_same_origin(request)
    try:
        result = await service.complete_enrollment(
            session,
            invite_token=command.invite_token,
            password=command.password,
            totp_code=command.totp_code,
            correlation_id=getattr(request.state, "request_id", "unavailable"),
        )
    except (AdminEnrollmentInputError, AdminEnrollmentInvalidError) as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(error),
        ) from error
    except AdminEnrollmentBusyError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(error),
        ) from error
    return AdminEnrollmentCompletionResponse(
        user_id=str(result.user_id),
        username=result.username,
        display_name=result.display_name,
        role=result.role,
    )
