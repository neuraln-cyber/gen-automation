from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from gen_automation.api.security import (
    ClientAddressResolutionError,
    CurrentPrincipal,
    Session,
    authentication_service,
    require_same_origin,
    resolve_client_ip,
)
from gen_automation.api.session_cookies import (
    clear_session_cookies,
    set_session_cookies,
)
from gen_automation.config import Settings
from gen_automation.services.authentication import (
    AuthenticationFailedError,
    AuthenticationService,
    CsrfValidationError,
)

router = APIRouter(prefix="/auth", tags=["authentication"])
AuthService = Annotated[AuthenticationService, Depends(authentication_service)]


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=1, max_length=1024)
    totp_code: str | None = Field(default=None, pattern=r"^[0-9]{6}$")


class ReauthenticationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    password: str = Field(min_length=1, max_length=1024)
    totp_code: str | None = Field(default=None, pattern=r"^[0-9]{6}$")


class SessionResponse(BaseModel):
    user_id: str
    username: str
    display_name: str
    role: str
    expires_at: str
    idle_expires_at: str
    mfa_verified: bool


@router.post("/login", response_model=SessionResponse)
async def login(
    command: LoginRequest,
    request: Request,
    response: Response,
    session: Session,
    service: AuthService,
) -> SessionResponse:
    require_same_origin(request)
    try:
        client_context = resolve_client_ip(request)
    except ClientAddressResolutionError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="client address chain is invalid",
        ) from error
    correlation_id = getattr(request.state, "request_id", "unavailable")
    try:
        result = await service.login(
            session,
            username=command.username,
            password=command.password,
            totp_code=command.totp_code,
            client_context=client_context,
            user_agent=request.headers.get("user-agent"),
            correlation_id=correlation_id,
        )
    except AuthenticationFailedError as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="authentication failed",
        ) from error
    settings: Settings = request.app.state.settings
    set_session_cookies(
        response,
        settings=settings,
        session_token=result.session_token,
        csrf_token=result.csrf_token,
    )
    return SessionResponse(
        user_id=str(result.user_id),
        username=result.username,
        display_name=result.display_name,
        role=result.role.value,
        expires_at=result.expires_at.isoformat(),
        idle_expires_at=result.idle_expires_at.isoformat(),
        mfa_verified=result.mfa_verified,
    )


@router.get("/session", response_model=SessionResponse)
async def read_session(principal: CurrentPrincipal) -> SessionResponse:
    return SessionResponse(
        user_id=str(principal.user_id),
        username=principal.username,
        display_name=principal.display_name,
        role=principal.role.value,
        expires_at=principal.expires_at.isoformat(),
        idle_expires_at=principal.idle_expires_at.isoformat(),
        mfa_verified=principal.mfa_verified_at is not None,
    )


@router.post("/reauthenticate", response_model=SessionResponse)
async def reauthenticate(
    command: ReauthenticationRequest,
    request: Request,
    principal: CurrentPrincipal,
    session: Session,
    service: AuthService,
) -> SessionResponse:
    settings: Settings = request.app.state.settings
    require_same_origin(request)
    try:
        service.validate_csrf(
            principal,
            cookie_token=request.cookies.get(settings.auth_csrf_cookie_name),
            header_token=request.headers.get("x-csrf-token"),
        )
    except CsrfValidationError as error:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="CSRF validation failed",
        ) from error
    try:
        refreshed = await service.reauthenticate_session(
            session,
            principal=principal,
            password=command.password,
            totp_code=command.totp_code,
            correlation_id=getattr(request.state, "request_id", "unavailable"),
        )
    except AuthenticationFailedError as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="authentication failed",
        ) from error
    return SessionResponse(
        user_id=str(refreshed.user_id),
        username=refreshed.username,
        display_name=refreshed.display_name,
        role=refreshed.role.value,
        expires_at=refreshed.expires_at.isoformat(),
        idle_expires_at=refreshed.idle_expires_at.isoformat(),
        mfa_verified=refreshed.mfa_verified_at is not None,
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request,
    response: Response,
    principal: CurrentPrincipal,
    session: Session,
    service: AuthService,
) -> None:
    settings: Settings = request.app.state.settings
    require_same_origin(request)
    try:
        service.validate_csrf(
            principal,
            cookie_token=request.cookies.get(settings.auth_csrf_cookie_name),
            header_token=request.headers.get("x-csrf-token"),
        )
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="CSRF validation failed",
        ) from error
    await service.logout(
        session,
        principal=principal,
        correlation_id=getattr(request.state, "request_id", "unavailable"),
    )
    clear_session_cookies(response, settings)
