from pathlib import Path
from typing import Annotated
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from gen_automation.api.security import (
    ClientAddressResolutionError,
    Session,
    authentication_service,
    require_same_origin,
    resolve_client_ip,
)
from gen_automation.api.session_cookies import set_session_cookies
from gen_automation.config import Settings
from gen_automation.services.authentication import (
    AuthenticationFailedError,
    AuthenticationService,
)

router = APIRouter(tags=["browser authentication"], include_in_schema=False)
templates = Jinja2Templates(directory=str(Path(__file__).parents[2] / "templates"))
AuthService = Annotated[AuthenticationService, Depends(authentication_service)]

_FORM_CONTENT_TYPE = "application/x-www-form-urlencoded"
_MAX_FORM_BODY_BYTES = 16 * 1024
_FORM_FIELDS = frozenset({"username", "password", "totp_code"})


class BrowserLoginFormError(ValueError):
    """The browser login body did not match the bounded form contract."""


@router.get("/login", response_class=HTMLResponse, name="browser_login")
async def browser_login(request: Request) -> HTMLResponse:
    settings: Settings = request.app.state.settings
    if not settings.auth_enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="authentication is disabled",
        )
    return _login_page(request)


@router.post(
    "/login",
    response_class=HTMLResponse,
    response_model=None,
    name="browser_login_submit",
)
async def browser_login_submit(
    request: Request,
    session: Session,
    service: AuthService,
) -> Response:
    require_same_origin(request)
    content_type = request.headers.get("content-type", "")
    if content_type.partition(";")[0].strip().lower() != _FORM_CONTENT_TYPE:
        return _login_page(
            request,
            failed=True,
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
        )
    try:
        client_context = resolve_client_ip(request)
    except ClientAddressResolutionError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="client address chain is invalid",
        ) from error

    try:
        username, password, totp_code = await _read_login_form(request)
        result = await service.login(
            session,
            username=username,
            password=password,
            totp_code=totp_code,
            client_context=client_context,
            user_agent=request.headers.get("user-agent"),
            correlation_id=getattr(request.state, "request_id", "unavailable"),
        )
    except (AuthenticationFailedError, BrowserLoginFormError):
        return _login_page(
            request,
            failed=True,
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    response = RedirectResponse(
        url="/dashboard",
        status_code=status.HTTP_303_SEE_OTHER,
    )
    settings: Settings = request.app.state.settings
    set_session_cookies(
        response,
        settings=settings,
        session_token=result.session_token,
        csrf_token=result.csrf_token,
    )
    return response


async def _read_login_form(request: Request) -> tuple[str, str, str]:
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > _MAX_FORM_BODY_BYTES:
            raise BrowserLoginFormError("browser login form is invalid")
        body.extend(chunk)
    try:
        encoded = bytes(body).decode("utf-8", errors="strict")
        parsed = parse_qs(
            encoded,
            keep_blank_values=True,
            strict_parsing=True,
            encoding="utf-8",
            errors="strict",
            max_num_fields=len(_FORM_FIELDS),
        )
    except (UnicodeDecodeError, ValueError):
        raise BrowserLoginFormError("browser login form is invalid") from None
    if set(parsed) != _FORM_FIELDS or any(len(parsed[field]) != 1 for field in _FORM_FIELDS):
        raise BrowserLoginFormError("browser login form is invalid")
    username = parsed["username"][0]
    password = parsed["password"][0]
    totp_code = parsed["totp_code"][0]
    if (
        not 1 <= len(username) <= 200
        or not 1 <= len(password) <= 1024
        or len(totp_code) != 6
        or not totp_code.isascii()
        or not totp_code.isdecimal()
    ):
        raise BrowserLoginFormError("browser login form is invalid")
    return username, password, totp_code


def _login_page(
    request: Request,
    *,
    failed: bool = False,
    status_code: int = status.HTTP_200_OK,
) -> HTMLResponse:
    response = templates.TemplateResponse(
        request=request,
        name="auth/login.html",
        context={
            "page_title": "Operator sign in",
            "failed": failed,
        },
        status_code=status_code,
    )
    response.headers["Cache-Control"] = "no-store"
    return response
