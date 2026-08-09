import hmac
from pathlib import Path
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from gen_automation.api.browser_new_set_forms import (
    BrowserNewSetForm,
    BrowserNewSetFormError,
    form_key_matches,
    generation_stop_form_key,
    new_set_csrf_token,
    new_set_form_key,
    read_generation_stop_form,
    read_new_set_form,
)
from gen_automation.api.security import (
    RawMasterReader,
    ReleaseReader,
    authentication_service,
    require_release_manager,
)
from gen_automation.config import Settings
from gen_automation.db.session import get_session
from gen_automation.domain.deliverability import MAX_ACCEPTED_IMAGES_PER_RELEASE
from gen_automation.domain.enums import AdminRole
from gen_automation.domain.generation_limits import MAX_OUTPUTS_PER_GENERATION_JOB
from gen_automation.middleware import content_security_policy
from gen_automation.services.authentication import (
    AuthenticatedPrincipal,
    CsrfValidationError,
)
from gen_automation.services.dashboard_previews import dashboard_preview_url
from gen_automation.services.generation import GenerationPlanConflictError
from gen_automation.services.generation_control import (
    GenerationControlConflictError,
    GenerationControlInputError,
    GenerationControlNotFoundError,
    request_generation_stop,
)
from gen_automation.services.new_sets import (
    NewSetInputError,
    NewSetNotFoundError,
    NewSetOptions,
    create_and_approve_new_set,
    list_new_set_options,
    load_new_set_status,
    new_set_progress_payload,
)
from gen_automation.services.progressive_assets import (
    ProgressiveAssetCursorError,
    ProgressiveAssetIntegrityError,
    ProgressiveAssetNotFoundError,
    list_available_raw_masters,
)
from gen_automation.services.releases import ConflictError, NotFoundError
from gen_automation.storage.base import ObjectStore

router = APIRouter(prefix="/dashboard", tags=["dashboard"], include_in_schema=False)
templates = Jinja2Templates(directory=str(Path(__file__).parents[2] / "templates"))
Session = Annotated[AsyncSession, Depends(get_session)]
_MANAGER_ROLES = frozenset({AdminRole.OWNER, AdminRole.ADMIN})


@router.get("/new-set", response_class=HTMLResponse, name="dashboard_new_set")
async def dashboard_new_set(
    request: Request,
    session: Session,
    principal: ReleaseReader,
) -> Response:
    if principal.role not in _MANAGER_ROLES:
        return _error_response(
            request,
            principal=principal,
            status_code=status.HTTP_403_FORBIDDEN,
            heading="New Set is unavailable",
            message="Your account cannot create releases.",
        )
    options = await list_new_set_options(session)
    try:
        return _new_set_response(
            request,
            principal=principal,
            options=options,
        )
    except (CsrfValidationError, HTTPException):
        return _error_response(
            request,
            principal=principal,
            status_code=status.HTTP_403_FORBIDDEN,
            heading="New Set could not be opened",
            message="The browser session could not be verified. Sign in again and retry.",
        )


@router.post(
    "/new-set",
    response_class=HTMLResponse,
    response_model=None,
    name="dashboard_new_set_submit",
)
async def submit_dashboard_new_set(
    request: Request,
    session: Session,
    principal: ReleaseReader,
) -> Response:
    if principal.role not in _MANAGER_ROLES:
        return _error_response(
            request,
            principal=principal,
            status_code=status.HTTP_403_FORBIDDEN,
            heading="New Set was not created",
            message="Your account cannot create releases.",
        )
    try:
        form = await read_new_set_form(request)
    except BrowserNewSetFormError as error:
        options = await list_new_set_options(session)
        return _new_set_response(
            request,
            principal=principal,
            options=options,
            values=error.values,
            error_message=error.message,
            status_code=error.status_code,
        )

    try:
        manager = await require_release_manager(
            request,
            session,
            csrf_header=form.csrf_token,
        )
        settings: Settings = request.app.state.settings
        if not settings.auth_enabled and not hmac.compare_digest(
            form.csrf_token,
            new_set_csrf_token(settings, session_id=manager.session_id),
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="CSRF validation failed",
            )
        expected_key = new_set_form_key(
            settings,
            session_id=manager.session_id,
            submission_id=form.submission_id,
        )
        if not form_key_matches(form.idempotency_key, expected_key):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="form idempotency validation failed",
            )
        result = await create_and_approve_new_set(
            session,
            command=form.command,
            idempotency_key=form.idempotency_key,
            settings=settings,
            actor=str(manager.user_id),
        )
    except HTTPException as error:
        await session.rollback()
        return await _submission_error(
            request,
            session=session,
            principal=principal,
            form=form,
            status_code=error.status_code,
            message="The browser session or form could not be verified. Reload and try again.",
        )
    except NewSetInputError as error:
        await session.rollback()
        return await _submission_error(
            request,
            session=session,
            principal=principal,
            form=form,
            status_code=status.HTTP_409_CONFLICT,
            message=str(error).capitalize() + ".",
        )
    except ConflictError as error:
        await session.rollback()
        return await _submission_error(
            request,
            session=session,
            principal=principal,
            form=form,
            status_code=status.HTTP_409_CONFLICT,
            message=_release_conflict_message(error),
        )
    except GenerationPlanConflictError as error:
        await session.rollback()
        return await _submission_error(
            request,
            session=session,
            principal=principal,
            form=form,
            status_code=status.HTTP_409_CONFLICT,
            message=(
                "The release was frozen, but its generation jobs could not be approved: "
                f"{error}. Correct the registry or wildcard issue, then submit this form again."
            ),
        )
    except NotFoundError:
        await session.rollback()
        return await _submission_error(
            request,
            session=session,
            principal=principal,
            form=form,
            status_code=status.HTTP_409_CONFLICT,
            message="The default project changed while the set was being created. Try again.",
        )
    except (IntegrityError, ValidationError):
        await session.rollback()
        return await _submission_error(
            request,
            session=session,
            principal=principal,
            form=form,
            status_code=status.HTTP_409_CONFLICT,
            message=(
                "The selected approvals or set name conflict with current data. "
                "Reload the available choices and try again."
            ),
        )

    return _secure_response(
        request,
        RedirectResponse(
            url=(f"/dashboard/releases/{result.release.id}/status?draft={form.submission_id}"),
            status_code=status.HTTP_303_SEE_OTHER,
        ),
    )


@router.get(
    "/releases/{release_id}/status",
    response_class=HTMLResponse,
    name="dashboard_release_status",
)
async def dashboard_release_status(
    release_id: UUID,
    request: Request,
    session: Session,
    principal: ReleaseReader,
) -> Response:
    try:
        release_status = await load_new_set_status(session, release_id=release_id)
    except NewSetNotFoundError:
        return _error_response(
            request,
            principal=principal,
            status_code=status.HTTP_404_NOT_FOUND,
            heading="Release not found",
            message="The requested release does not exist.",
        )
    submitted_draft_id = request.query_params.get("draft", "")
    try:
        submitted_draft_id = str(UUID(submitted_draft_id))
    except ValueError:
        submitted_draft_id = ""
    return _secure_response(
        request,
        templates.TemplateResponse(
            request=request,
            name="dashboard/new_set_status.html",
            context={
                "page_title": release_status.title,
                "principal": principal,
                "release": release_status,
                "submitted_draft_id": submitted_draft_id,
                "can_manage_generation": principal.role in _MANAGER_ROLES,
                "stop_generation_csrf_token": (
                    _form_csrf_token(request, principal)
                    if principal.role in _MANAGER_ROLES and release_status.can_stop
                    else ""
                ),
                "stop_generation_idempotency_key": (
                    generation_stop_form_key(
                        request.app.state.settings,
                        session_id=principal.session_id,
                        release_id=release_id,
                    )
                    if principal.role in _MANAGER_ROLES and release_status.can_stop
                    else ""
                ),
            },
        ),
    )


@router.get(
    "/releases/{release_id}/progress",
    response_class=JSONResponse,
    response_model=None,
    name="dashboard_release_progress",
)
async def dashboard_release_progress(
    release_id: UUID,
    request: Request,
    session: Session,
    _principal: ReleaseReader,
) -> Response:
    try:
        release_status = await load_new_set_status(session, release_id=release_id)
    except NewSetNotFoundError:
        return _secure_response(
            request,
            JSONResponse(
                status_code=status.HTTP_404_NOT_FOUND,
                content={"detail": "release not found"},
            ),
        )
    return _secure_response(
        request,
        JSONResponse(content=new_set_progress_payload(release_status)),
    )


@router.post(
    "/releases/{release_id}:stop-generation",
    response_class=JSONResponse,
    response_model=None,
    name="dashboard_stop_release_generation",
)
async def dashboard_stop_release_generation(
    release_id: UUID,
    request: Request,
    session: Session,
    principal: ReleaseReader,
) -> Response:
    if principal.role not in _MANAGER_ROLES:
        return _generation_stop_error_response(
            request,
            principal=principal,
            status_code=status.HTTP_403_FORBIDDEN,
            message="Your account cannot stop generation runs.",
        )
    try:
        form = await read_generation_stop_form(request)
        manager = await require_release_manager(
            request,
            session,
            csrf_header=form.csrf_token,
        )
        settings: Settings = request.app.state.settings
        if not settings.auth_enabled and not hmac.compare_digest(
            form.csrf_token,
            new_set_csrf_token(settings, session_id=manager.session_id),
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="CSRF validation failed",
            )
        expected_key = generation_stop_form_key(
            settings,
            session_id=manager.session_id,
            release_id=release_id,
        )
        if not form_key_matches(form.idempotency_key, expected_key):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="form idempotency validation failed",
            )
        await request_generation_stop(
            session,
            release_id=release_id,
            actor=f"admin:{manager.user_id}",
            correlation_id=form.idempotency_key,
        )
        release_status = await load_new_set_status(session, release_id=release_id)
    except BrowserNewSetFormError as error:
        await session.rollback()
        return _generation_stop_error_response(
            request,
            principal=principal,
            status_code=error.status_code,
            message=error.message,
        )
    except HTTPException as error:
        await session.rollback()
        return _generation_stop_error_response(
            request,
            principal=principal,
            status_code=error.status_code,
            message=(
                "The browser session or stop control could not be verified. Reload and try again."
            ),
        )
    except GenerationControlNotFoundError:
        await session.rollback()
        return _generation_stop_error_response(
            request,
            principal=principal,
            status_code=status.HTTP_404_NOT_FOUND,
            message="The generation run could not be found.",
        )
    except (GenerationControlInputError, GenerationControlConflictError) as error:
        await session.rollback()
        return _generation_stop_error_response(
            request,
            principal=principal,
            status_code=status.HTTP_409_CONFLICT,
            message=str(error).capitalize() + ".",
        )

    if _wants_json(request):
        return _secure_response(
            request,
            JSONResponse(content=new_set_progress_payload(release_status)),
        )
    return _secure_response(
        request,
        RedirectResponse(
            url=f"/dashboard/releases/{release_id}/status",
            status_code=status.HTTP_303_SEE_OTHER,
        ),
    )


@router.get(
    "/releases/{release_id}/generated-assets",
    response_class=JSONResponse,
    response_model=None,
    name="dashboard_release_generated_assets",
)
async def dashboard_release_generated_assets(
    release_id: UUID,
    request: Request,
    session: Session,
    _principal: RawMasterReader,
    cursor: Annotated[str | None, Query(max_length=256)] = None,
    limit: Annotated[int, Query(ge=1, le=64)] = 32,
) -> Response:
    store: ObjectStore | None = request.app.state.object_store
    if store is None:
        return _secure_response(
            request,
            JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"detail": "private storage is unavailable"},
            ),
        )
    try:
        page = await list_available_raw_masters(
            session,
            store=store,
            release_id=release_id,
            cursor=cursor,
            limit=limit,
        )
    except ProgressiveAssetCursorError:
        return _secure_response(
            request,
            JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"detail": "available-master cursor is invalid"},
            ),
        )
    except ProgressiveAssetNotFoundError:
        return _secure_response(
            request,
            JSONResponse(
                status_code=status.HTTP_404_NOT_FOUND,
                content={"detail": "release not found"},
            ),
        )
    except ProgressiveAssetIntegrityError:
        return _secure_response(
            request,
            JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"detail": "available raw masters are unavailable"},
            ),
        )

    assets = [
        {
            "asset_id": str(asset.asset_id),
            "available_at": asset.available_at.isoformat().replace("+00:00", "Z"),
            "width": asset.width,
            "height": asset.height,
            "image_format": asset.image_format,
            "byte_size": asset.byte_size,
            "checksum_prefix": asset.checksum_prefix,
            "ordinal": asset.ordinal,
            "output_index": asset.output_index,
            "queue_position": asset.queue_position,
            "batch_index": asset.batch_index,
            "batch_name": asset.batch_name,
            "batch_image_number": asset.batch_image_number,
            "preview_url": dashboard_preview_url(
                asset_id=asset.asset_id,
                source_sha256=asset.source_sha256,
            ),
            "view_url": f"/dashboard/assets/{asset.asset_id}/view",
            "download_url": f"/dashboard/assets/{asset.asset_id}/download",
            "generation_details_url": (f"/dashboard/assets/{asset.asset_id}/generation-details"),
        }
        for asset in page.assets
    ]
    return _secure_response(
        request,
        JSONResponse(
            content={
                "schema_version": 1,
                "release_id": str(page.release_id),
                "assets": assets,
                "next_cursor": page.next_cursor,
                "has_more": page.has_more,
            }
        ),
    )


async def _submission_error(
    request: Request,
    *,
    session: AsyncSession,
    principal: AuthenticatedPrincipal,
    form: BrowserNewSetForm,
    status_code: int,
    message: str,
) -> Response:
    options = await list_new_set_options(session)
    return _new_set_response(
        request,
        principal=principal,
        options=options,
        values=form.values,
        submission_id=form.submission_id,
        idempotency_key=form.idempotency_key,
        error_message=message,
        status_code=status_code,
    )


def _new_set_response(
    request: Request,
    *,
    principal: AuthenticatedPrincipal,
    options: NewSetOptions,
    values: dict[str, str] | None = None,
    submission_id: UUID | None = None,
    idempotency_key: str | None = None,
    error_message: str | None = None,
    status_code: int = status.HTTP_200_OK,
) -> Response:
    settings: Settings = request.app.state.settings
    resolved_submission_id = submission_id or uuid4()
    resolved_key = idempotency_key or new_set_form_key(
        settings,
        session_id=principal.session_id,
        submission_id=resolved_submission_id,
    )
    form_values = _default_values(options)
    if values is not None:
        form_values.update(
            {
                key: value
                for key, value in values.items()
                if key not in {"csrf_token", "submission_id", "idempotency_key"}
            }
        )
    return _secure_response(
        request,
        templates.TemplateResponse(
            request=request,
            name="dashboard/new_set.html",
            context={
                "page_title": "New Set",
                "principal": principal,
                "options": options,
                "form_values": form_values,
                "csrf_token": _form_csrf_token(request, principal),
                "submission_id": resolved_submission_id,
                "idempotency_key": resolved_key,
                "error_message": error_message,
                "max_outputs_per_generation_job": MAX_OUTPUTS_PER_GENERATION_JOB,
                "max_accepted_images_per_release": MAX_ACCEPTED_IMAGES_PER_RELEASE,
            },
            status_code=status_code,
        ),
    )


def _default_values(options: NewSetOptions) -> dict[str, str]:
    values = {
        "slug": "",
        "title": "",
        "subject_id": str(options.subjects[0].approval_id) if options.subjects else "",
        "subject_2_id": "",
        "composition_mode": "single",
        "character_a_prompt": "",
        "character_b_prompt": "",
        "checkpoint_id": (str(options.checkpoints[0].approval_id) if options.checkpoints else ""),
        "workflow_id": _preferred_workflow_id(options),
        "prompt": "",
        "negative_prompt": "",
        "detailer_prompt": "sexy, expressive, ",
        "detailer_negative_prompt": "closed eyes, ",
        "batch_plan": "",
        "seed": "-1",
        "width": "1144",
        "height": "1480",
        "cfg": "6.0",
        "steps": "30",
        "sampler": "euler_ancestral",
        "scheduler": "karras",
        "clip_skip": "2",
        "outputs_per_job": "4",
        "hires_scale": "1.5",
        "hires_denoise": "0.35",
        "hires_upscale_method": "bislerp",
        "detailer_guide_size": "768",
        "detailer_max_size": "1536",
        "detailer_denoise": "0.4",
        "detailer_bbox_threshold": "0.3",
        "detailer_bbox_dilation": "4",
        "detailer_bbox_crop_factor": "1.5",
        "detailer_feather": "4",
        "planned_job_count": "1",
        "desired_accepted_count": "4",
    }
    for slot in range(1, 9):
        values[f"lora_{slot}_id"] = ""
        values[f"lora_{slot}_weight"] = ""
    return values


def _preferred_workflow_id(options: NewSetOptions) -> str:
    if not options.workflows:
        return ""
    preferred = next(
        (
            workflow
            for workflow in options.workflows
            if workflow.name.casefold() == "illustrious base detailer"
        ),
        options.workflows[0],
    )
    return str(preferred.approval_id)


def _form_csrf_token(
    request: Request,
    principal: AuthenticatedPrincipal,
) -> str:
    settings: Settings = request.app.state.settings
    if not settings.auth_enabled:
        return new_set_csrf_token(settings, session_id=principal.session_id)
    cookie_token = request.cookies.get(settings.auth_csrf_cookie_name)
    if cookie_token is None:
        raise CsrfValidationError("CSRF cookie is unavailable")
    authentication_service(request).validate_csrf(
        principal,
        cookie_token=cookie_token,
        header_token=cookie_token,
    )
    return cookie_token


def _release_conflict_message(error: ConflictError) -> str:
    detail = str(error)
    if "wildcard" in detail.casefold():
        return (
            "A prompt wildcard is missing, invalid, or changed. "
            "Update Prompt wildcards, then submit again."
        )
    if "slug" in detail.casefold():
        return "That set slug is already used in this project. Choose another slug."
    if "idempotency" in detail.casefold():
        return "This form was already used with different values. Reload New Set and try again."
    return "The release conflicts with current project data. Reload New Set and try again."


def _wants_json(request: Request) -> bool:
    return "application/json" in request.headers.get("accept", "").casefold()


def _generation_stop_error_response(
    request: Request,
    *,
    principal: AuthenticatedPrincipal,
    status_code: int,
    message: str,
) -> Response:
    if _wants_json(request):
        return _secure_response(
            request,
            JSONResponse(status_code=status_code, content={"detail": message}),
        )
    return _error_response(
        request,
        principal=principal,
        status_code=status_code,
        heading="Generation could not be stopped",
        message=message,
    )


def _secure_response(request: Request, response: Response) -> Response:
    settings: Settings = request.app.state.settings
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = content_security_policy(settings.environment)
    return response


def _error_response(
    request: Request,
    *,
    principal: AuthenticatedPrincipal,
    status_code: int,
    heading: str,
    message: str,
) -> Response:
    return _secure_response(
        request,
        templates.TemplateResponse(
            request=request,
            name="dashboard/error.html",
            context={
                "page_title": heading,
                "principal": principal,
                "heading": heading,
                "message": message,
            },
            status_code=status_code,
        ),
    )
