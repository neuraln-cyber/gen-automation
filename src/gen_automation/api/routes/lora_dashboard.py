"""Browser page for owner/admin managed LoRA onboarding."""

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from gen_automation.api.routes.loras import _import_read, _library_entries
from gen_automation.api.security import ReleaseReader
from gen_automation.config import Settings
from gen_automation.db.session import get_session
from gen_automation.domain.enums import AdminRole
from gen_automation.services.lora_catalog import (
    list_lora_import_jobs,
    list_managed_loras,
)

router = APIRouter(
    prefix="/dashboard/loras",
    tags=["dashboard"],
    include_in_schema=False,
)
templates = Jinja2Templates(directory=str(Path(__file__).parents[2] / "templates"))
Session = Annotated[AsyncSession, Depends(get_session)]
_MANAGER_ROLES = frozenset({AdminRole.OWNER, AdminRole.ADMIN})


@router.get("", response_class=HTMLResponse, name="dashboard_loras")
async def dashboard_loras(
    request: Request,
    session: Session,
    principal: ReleaseReader,
) -> Response:
    if principal.role not in _MANAGER_ROLES:
        return _secure(
            templates.TemplateResponse(
                request=request,
                name="dashboard/error.html",
                context={
                    "page_title": "LoRA manager is unavailable",
                    "principal": principal,
                    "heading": "LoRA manager is unavailable",
                    "message": "Your account cannot manage model artifacts.",
                },
                status_code=status.HTTP_403_FORBIDDEN,
            )
        )
    settings: Settings = request.app.state.settings
    if not settings.lora_manager_enabled:
        return _secure(
            templates.TemplateResponse(
                request=request,
                name="dashboard/error.html",
                context={
                    "page_title": "LoRA manager is unavailable",
                    "principal": principal,
                    "heading": "LoRA manager is not enabled",
                    "message": "Finish the private-storage configuration, then reload.",
                },
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        )
    managed = await list_managed_loras(
        session,
        actor_user_id=principal.user_id,
        limit=None,
    )
    imports = await list_lora_import_jobs(
        session,
        actor_user_id=principal.user_id,
        limit=100,
    )
    entries = await _library_entries(session, managed)
    csrf_token = (
        request.cookies.get(settings.auth_csrf_cookie_name, "")
        if settings.auth_enabled
        else "development"
    )
    return _secure(
        templates.TemplateResponse(
            request=request,
            name="dashboard/loras.html",
            context={
                "page_title": "LoRA manager",
                "principal": principal,
                "entries": entries,
                "imports": [_import_read(item) for item in imports],
                "can_manage": True,
                "csrf_token": csrf_token,
            },
        )
    )


def _secure(response: Response) -> Response:
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response
