from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from gen_automation.api.security import (
    ReleaseReader,
    require_release_manager,
)
from gen_automation.config import Settings
from gen_automation.db.session import get_session
from gen_automation.domain.enums import AdminRole
from gen_automation.domain.wildcards import (
    WildcardAppend,
    WildcardCreate,
    WildcardReplace,
)
from gen_automation.services.authentication import AuthenticatedPrincipal
from gen_automation.services.wildcards import (
    WildcardConflictError,
    WildcardError,
    WildcardInputError,
    WildcardNotFoundError,
    append_wildcard_entries,
    create_wildcard_library,
    list_wildcard_libraries,
    replace_wildcard_entries,
)

router = APIRouter(
    prefix="/dashboard/wildcards",
    tags=["dashboard"],
    include_in_schema=False,
)
templates = Jinja2Templates(directory=str(Path(__file__).parents[2] / "templates"))
Session = Annotated[AsyncSession, Depends(get_session)]
MAX_FORM_BYTES = 1024 * 1024


@router.get("", response_class=HTMLResponse, name="dashboard_wildcards")
async def dashboard_wildcards(
    request: Request,
    session: Session,
    principal: ReleaseReader,
) -> Response:
    libraries = await list_wildcard_libraries(session)
    settings: Settings = request.app.state.settings
    csrf_token = (
        request.cookies.get(settings.auth_csrf_cookie_name, "")
        if settings.auth_enabled
        else "development"
    )
    return _secure(
        templates.TemplateResponse(
            request=request,
            name="dashboard/wildcards.html",
            context={
                "page_title": "Prompt wildcards",
                "principal": principal,
                "libraries": libraries,
                "csrf_token": csrf_token,
                "can_manage": principal.role in {AdminRole.OWNER, AdminRole.ADMIN},
            },
        )
    )


@router.post("", response_class=HTMLResponse, response_model=None)
async def mutate_dashboard_wildcard(
    request: Request,
    session: Session,
) -> Response:
    principal: AuthenticatedPrincipal | None = None
    try:
        form = await _read_form(request)
        csrf_token = _form_value(form, "csrf_token", max_length=100)
        principal = await require_release_manager(
            request,
            session,
            csrf_header=csrf_token,
        )
        action = _form_value(form, "action", max_length=20)
        entries = _entry_lines(_form_value(form, "entries", max_length=MAX_FORM_BYTES))
        if action == "create":
            command = WildcardCreate(
                name=_form_value(form, "name", max_length=80),
                entries=entries,
            )
            await create_wildcard_library(
                session,
                command=command,
                actor=str(principal.user_id),
            )
        elif action in {"replace", "append"}:
            name = _form_value(form, "name", max_length=80)
            expected_version_no = int(_form_value(form, "expected_version_no", max_length=12))
            if action == "replace":
                await replace_wildcard_entries(
                    session,
                    name=name,
                    command=WildcardReplace(
                        expected_version_no=expected_version_no,
                        entries=entries,
                    ),
                    actor=str(principal.user_id),
                )
            else:
                await append_wildcard_entries(
                    session,
                    name=name,
                    command=WildcardAppend(
                        expected_version_no=expected_version_no,
                        entries=entries,
                    ),
                    actor=str(principal.user_id),
                )
        else:
            raise ValueError("unsupported wildcard action")
    except HTTPException as error:
        return _error(
            request,
            principal=None,
            status_code=error.status_code,
            message="The wildcard change could not be authorized.",
        )
    except (ValidationError, ValueError, WildcardInputError):
        await session.rollback()
        return _error(
            request,
            principal=principal,
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            message="The submitted wildcard name, version, or entries were invalid.",
        )
    except WildcardNotFoundError:
        await session.rollback()
        return _error(
            request,
            principal=principal,
            status_code=status.HTTP_404_NOT_FOUND,
            message="The wildcard library no longer exists.",
        )
    except (WildcardConflictError, IntegrityError):
        await session.rollback()
        return _error(
            request,
            principal=principal,
            status_code=status.HTTP_409_CONFLICT,
            message="The wildcard library changed. Reload the page and try again.",
        )
    except WildcardError:
        await session.rollback()
        return _error(
            request,
            principal=principal,
            status_code=status.HTTP_409_CONFLICT,
            message="The wildcard change could not be saved.",
        )

    return _secure(
        RedirectResponse(
            url="/dashboard/wildcards",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    )


async def _read_form(request: Request) -> dict[str, str]:
    content_length = request.headers.get("content-length")
    if content_length is None or not content_length.isdecimal():
        raise ValueError("form length is required")
    if int(content_length) > MAX_FORM_BYTES:
        raise ValueError("form is too large")
    content_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
    if content_type != "application/x-www-form-urlencoded":
        raise ValueError("form type is unsupported")
    submitted = await request.form(max_files=0, max_fields=8, max_part_size=MAX_FORM_BYTES)
    result: dict[str, str] = {}
    for key, value in submitted.multi_items():
        if key in result or not isinstance(value, str):
            raise ValueError("form contains repeated or invalid fields")
        result[key] = value
    return result


def _form_value(form: dict[str, str], key: str, *, max_length: int) -> str:
    value = form.get(key)
    if value is None or not value or len(value) > max_length:
        raise ValueError("form field is invalid")
    return value


def _entry_lines(value: str) -> list[str]:
    return [line for line in value.splitlines() if line.strip()]


def _secure(response: Response) -> Response:
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def _error(
    request: Request,
    *,
    principal: AuthenticatedPrincipal | None,
    status_code: int,
    message: str,
) -> Response:
    if principal is None:
        return _secure(
            HTMLResponse(
                "<h1>Wildcard change was not saved</h1>"
                "<p>Sign in again, then reload the wildcard page.</p>",
                status_code=status_code,
            )
        )
    return _secure(
        templates.TemplateResponse(
            request=request,
            name="dashboard/error.html",
            context={
                "page_title": "Wildcard change was not saved",
                "principal": principal,
                "heading": "Wildcard change was not saved",
                "message": message,
            },
            status_code=status_code,
        )
    )
