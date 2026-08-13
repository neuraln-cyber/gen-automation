"""Browser shell for the focused image-to-video queue."""

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates

from gen_automation.api.security import ReleaseReader
from gen_automation.config import Settings
from gen_automation.domain.enums import AdminRole

router = APIRouter(
    prefix="/dashboard/animations",
    tags=["dashboard"],
    include_in_schema=False,
)
templates = Jinja2Templates(directory=str(Path(__file__).parents[2] / "templates"))


@router.get("", response_class=HTMLResponse, name="dashboard_i2v")
async def dashboard_i2v(request: Request, principal: ReleaseReader) -> Response:
    if principal.role not in {AdminRole.OWNER, AdminRole.ADMIN}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="image-to-video management permission required",
        )
    settings: Settings = request.app.state.settings
    csrf_token = (
        request.cookies.get(settings.auth_csrf_cookie_name, "")
        if settings.auth_enabled
        else "development"
    )
    response = templates.TemplateResponse(
        request=request,
        name="dashboard/i2v.html",
        context={
            "page_title": "Image to video",
            "principal": principal,
            "csrf_token": csrf_token,
            "can_manage": principal.role in {AdminRole.OWNER, AdminRole.ADMIN},
            "max_image_bytes": settings.storage_max_image_bytes,
            "hires_profile_enabled": settings.i2v_hires_profile_enabled,
            "lora_profile_enabled": settings.i2v_lora_profile_enabled,
        },
    )
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response
