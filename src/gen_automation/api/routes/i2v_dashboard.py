"""Browser shell for the focused image-to-video queue."""

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates

from gen_automation.api.security import ReleaseReader
from gen_automation.config import Settings
from gen_automation.domain.enums import AdminRole
from gen_automation.i2v_worker.h3_sampling import H3_EROS_SCHEDULERS, H3_SAMPLERS, H3_SCHEDULERS

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
            "lora_profile_enabled": (
                settings.i2v_h3_loras_enabled
                if settings.i2v_profile == "minimax_h3"
                else settings.i2v_lora_profile_enabled
            ),
            "video_profile": settings.i2v_profile,
            "h3_model_variant": settings.i2v_h3_model_variant,
            "h3_advanced_sampling_enabled": settings.i2v_h3_advanced_sampling_enabled,
            "h3_samplers": H3_SAMPLERS,
            "h3_schedulers": (
                H3_EROS_SCHEDULERS
                if settings.i2v_h3_model_variant == "eros_beta5"
                and settings.i2v_h3_eros_author_recipe_enabled
                else H3_SCHEDULERS
            ),
            "eros_author_recipe": settings.i2v_h3_model_variant == "eros_beta5"
            and settings.i2v_h3_eros_author_recipe_enabled,
            "source_resolution_enabled": settings.i2v_h3_source_resolution_enabled,
            "h3_diagnostics_enabled": settings.i2v_h3_diagnostics_enabled,
            "h3_first_frame_enabled": settings.i2v_h3_first_frame_enabled,
            "h3_latent_contrast_enabled": settings.i2v_h3_latent_contrast_enabled,
            "video_provider": "RunPod" if settings.i2v_runpod_enabled else "Salad",
        },
    )
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response
