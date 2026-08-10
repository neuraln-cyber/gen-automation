import hmac
import json
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gen_automation.api.browser_new_set_forms import new_set_csrf_token
from gen_automation.api.routes.new_set_dashboard import new_set_form_response
from gen_automation.api.security import (
    RawMasterReader,
    ReleaseReader,
    authentication_service,
    require_release_manager,
)
from gen_automation.config import Settings
from gen_automation.db.models import SaladDeployment
from gen_automation.db.session import get_session
from gen_automation.domain.enums import (
    AdminRole,
    DesiredDeploymentState,
    ExperimentWarmLeaseState,
    SaladDeploymentState,
)
from gen_automation.middleware import content_security_policy
from gen_automation.services.authentication import AuthenticatedPrincipal, CsrfValidationError
from gen_automation.services.dashboard_previews import dashboard_preview_url
from gen_automation.services.experiment_warm_leases import (
    DEFAULT_EXPERIMENT_WARM_LEASE_SECONDS,
    ExperimentWarmLeaseBudgetError,
    ExperimentWarmLeaseConflictError,
    ExperimentWarmLeaseNotFoundError,
    ExperimentWarmLeaseStatus,
    end_experiment_warm_lease,
    ensure_experiment_warm_lease,
    extend_experiment_warm_lease,
    get_current_experiment_warm_lease_status,
)
from gen_automation.services.experiments import (
    ExperimentNotFoundError,
    experiment_progress_payload,
    load_experiment_status,
)
from gen_automation.services.new_sets import list_new_set_options
from gen_automation.services.progressive_assets import (
    AvailableRawMaster,
    ProgressiveAssetIntegrityError,
    ProgressiveAssetNotFoundError,
    list_available_raw_masters,
)
from gen_automation.storage.base import ObjectStore

router = APIRouter(prefix="/dashboard", tags=["dashboard"], include_in_schema=False)
templates = Jinja2Templates(directory=str(Path(__file__).parents[2] / "templates"))
Session = Annotated[AsyncSession, Depends(get_session)]
_MANAGER_ROLES = frozenset({AdminRole.OWNER, AdminRole.ADMIN})
_MAX_WARM_REQUEST_BYTES = 4 * 1024


@router.get(
    "/experiments/new",
    response_class=HTMLResponse,
    name="dashboard_new_experiment",
)
async def dashboard_new_experiment(
    request: Request,
    session: Session,
    principal: ReleaseReader,
) -> Response:
    if principal.role not in _MANAGER_ROLES:
        return _error_response(
            request,
            principal=principal,
            status_code=status.HTTP_403_FORBIDDEN,
            heading="Experiment Lab is unavailable",
            message="Your account cannot create generation experiments.",
        )
    options = await list_new_set_options(session)
    try:
        return new_set_form_response(
            request,
            principal=principal,
            options=options,
            experiment_mode=True,
        )
    except (CsrfValidationError, HTTPException):
        return _error_response(
            request,
            principal=principal,
            status_code=status.HTTP_403_FORBIDDEN,
            heading="Experiment Lab could not be opened",
            message="The browser session could not be verified. Sign in again and retry.",
        )


@router.post(
    "/experiments/new",
    response_class=HTMLResponse,
    response_model=None,
    name="dashboard_new_experiment_submit",
)
async def submit_dashboard_new_experiment(
    request: Request,
    _session: Session,
    principal: ReleaseReader,
) -> Response:
    if principal.role not in _MANAGER_ROLES:
        return _error_response(
            request,
            principal=principal,
            status_code=status.HTTP_403_FORBIDDEN,
            heading="Experiment Lab is unavailable",
            message="Your account cannot create releases.",
        )
    return _secure_response(
        request,
        RedirectResponse(
            url="/dashboard/experiments/new",
            status_code=status.HTTP_303_SEE_OTHER,
        ),
    )


@router.get(
    "/experiments/warm-session",
    response_class=JSONResponse,
    response_model=None,
    name="dashboard_experiment_warm_session",
)
async def dashboard_experiment_warm_session(
    request: Request,
    session: Session,
    _principal: ReleaseReader,
) -> Response:
    current = await get_current_experiment_warm_lease_status(session)
    available = current is not None or await _warm_deployment_available(session)
    return _secure_response(
        request,
        JSONResponse(
            content=_warm_status_payload(
                request.app.state.settings,
                current=current,
                available=available,
            )
        ),
    )


@router.post(
    "/experiments/warm-session/start",
    response_class=JSONResponse,
    response_model=None,
    name="dashboard_experiment_warm_session_start",
)
async def start_dashboard_experiment_warm_session(
    request: Request,
    session: Session,
) -> Response:
    try:
        manager = await _require_warm_manager(request, session)
        duration_seconds = await _read_warm_request(request, duration_required=True)
        current = await ensure_experiment_warm_lease(
            session,
            actor=str(manager.user_id),
            duration_seconds=duration_seconds,
        )
        await session.commit()
    except HTTPException as error:
        await session.rollback()
        return _warm_error(request, status_code=error.status_code, message="Request rejected.")
    except (
        ExperimentWarmLeaseBudgetError,
        ExperimentWarmLeaseConflictError,
        ExperimentWarmLeaseNotFoundError,
    ) as error:
        await session.rollback()
        return _warm_error(request, status_code=409, message=str(error).capitalize() + ".")
    return _secure_response(
        request,
        JSONResponse(
            content=_warm_status_payload(
                request.app.state.settings,
                current=current,
                available=True,
            )
        ),
    )


@router.post(
    "/experiments/warm-session/extend",
    response_class=JSONResponse,
    response_model=None,
    name="dashboard_experiment_warm_session_extend",
)
async def extend_dashboard_experiment_warm_session(
    request: Request,
    session: Session,
) -> Response:
    try:
        manager = await _require_warm_manager(request, session)
        extension_seconds = await _read_warm_request(request, duration_required=True)
        current = await get_current_experiment_warm_lease_status(session)
        if current is None:
            raise ExperimentWarmLeaseNotFoundError("experiment warm session was not found")
        current = await extend_experiment_warm_lease(
            session,
            lease_id=current.lease_id,
            actor=str(manager.user_id),
            extension_seconds=extension_seconds,
        )
        await session.commit()
    except HTTPException as error:
        await session.rollback()
        return _warm_error(request, status_code=error.status_code, message="Request rejected.")
    except (
        ExperimentWarmLeaseBudgetError,
        ExperimentWarmLeaseConflictError,
        ExperimentWarmLeaseNotFoundError,
    ) as error:
        await session.rollback()
        return _warm_error(request, status_code=409, message=str(error).capitalize() + ".")
    return _secure_response(
        request,
        JSONResponse(
            content=_warm_status_payload(
                request.app.state.settings,
                current=current,
                available=True,
            )
        ),
    )


@router.post(
    "/experiments/warm-session/end",
    response_class=JSONResponse,
    response_model=None,
    name="dashboard_experiment_warm_session_end",
)
async def end_dashboard_experiment_warm_session(
    request: Request,
    session: Session,
) -> Response:
    try:
        manager = await _require_warm_manager(request, session)
        await _read_warm_request(request, duration_required=False)
        current = await get_current_experiment_warm_lease_status(session)
        if current is None:
            raise ExperimentWarmLeaseNotFoundError("experiment warm session was not found")
        current = await end_experiment_warm_lease(
            session,
            lease_id=current.lease_id,
            actor=str(manager.user_id),
        )
        await session.commit()
    except HTTPException as error:
        await session.rollback()
        return _warm_error(request, status_code=error.status_code, message="Request rejected.")
    except (
        ExperimentWarmLeaseConflictError,
        ExperimentWarmLeaseNotFoundError,
    ) as error:
        await session.rollback()
        return _warm_error(request, status_code=409, message=str(error).capitalize() + ".")
    return _secure_response(
        request,
        JSONResponse(
            content=_warm_status_payload(
                request.app.state.settings,
                current=current,
                available=True,
            )
        ),
    )


@router.get(
    "/experiments/{group_slug}",
    response_class=HTMLResponse,
    name="dashboard_experiment",
)
async def dashboard_experiment(
    group_slug: str,
    request: Request,
    session: Session,
    principal: ReleaseReader,
) -> Response:
    try:
        experiment = await load_experiment_status(session, group_slug=group_slug)
    except ExperimentNotFoundError:
        return _error_response(
            request,
            principal=principal,
            status_code=status.HTTP_404_NOT_FOUND,
            heading="Experiment not found",
            message="The requested comparison does not exist.",
        )
    return _secure_response(
        request,
        templates.TemplateResponse(
            request=request,
            name="dashboard/experiment_detail.html",
            context={
                "page_title": experiment.title,
                "principal": principal,
                "experiment": experiment,
                "csrf_token": _form_csrf_token(request, principal),
                "warm_notice": request.query_params.get("warm") == "unavailable",
            },
        ),
    )


@router.get(
    "/experiments/{group_slug}/progress",
    response_class=JSONResponse,
    response_model=None,
    name="dashboard_experiment_progress",
)
async def dashboard_experiment_progress(
    group_slug: str,
    request: Request,
    session: Session,
    _principal: ReleaseReader,
) -> Response:
    try:
        experiment = await load_experiment_status(session, group_slug=group_slug)
    except ExperimentNotFoundError:
        return _secure_response(
            request,
            JSONResponse(status_code=404, content={"detail": "experiment not found"}),
        )
    return _secure_response(
        request,
        JSONResponse(content=experiment_progress_payload(experiment)),
    )


@router.get(
    "/experiments/{group_slug}/generated-assets",
    response_class=JSONResponse,
    response_model=None,
    name="dashboard_experiment_generated_assets",
)
async def dashboard_experiment_generated_assets(
    group_slug: str,
    request: Request,
    session: Session,
    _principal: RawMasterReader,
) -> Response:
    store: ObjectStore | None = request.app.state.object_store
    if store is None:
        return _secure_response(
            request,
            JSONResponse(status_code=503, content={"detail": "private storage is unavailable"}),
        )
    try:
        experiment = await load_experiment_status(session, group_slug=group_slug)
        variants = []
        for variant in experiment.variants:
            page = await list_available_raw_masters(
                session,
                store=store,
                release_id=variant.release_id,
                cursor=None,
                limit=4,
            )
            variants.append(
                {
                    "index": variant.index,
                    "label": variant.label,
                    "release_id": str(variant.release_id),
                    "assets": [_asset_payload(asset) for asset in page.assets],
                }
            )
    except (ExperimentNotFoundError, ProgressiveAssetNotFoundError):
        return _secure_response(
            request,
            JSONResponse(status_code=404, content={"detail": "experiment not found"}),
        )
    except ProgressiveAssetIntegrityError:
        return _secure_response(
            request,
            JSONResponse(
                status_code=503,
                content={"detail": "available raw masters are unavailable"},
            ),
        )
    return _secure_response(
        request,
        JSONResponse(
            content={
                "schema_version": 1,
                "group_slug": experiment.group_slug,
                "variants": variants,
            }
        ),
    )


def _asset_payload(asset: AvailableRawMaster) -> dict[str, object]:
    return {
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
        "preview_url": dashboard_preview_url(
            asset_id=asset.asset_id,
            source_sha256=asset.source_sha256,
        ),
        "view_url": f"/dashboard/assets/{asset.asset_id}/view",
        "download_url": f"/dashboard/assets/{asset.asset_id}/download",
        "generation_details_url": f"/dashboard/assets/{asset.asset_id}/generation-details",
    }


async def _require_warm_manager(
    request: Request,
    session: AsyncSession,
) -> AuthenticatedPrincipal:
    csrf_token = request.headers.get("X-CSRF-Token", "")
    if not csrf_token or len(csrf_token) > 200:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="CSRF failed")
    manager = await require_release_manager(request, session, csrf_header=csrf_token)
    settings: Settings = request.app.state.settings
    if not settings.auth_enabled and not hmac.compare_digest(
        csrf_token,
        new_set_csrf_token(settings, session_id=manager.session_id),
    ):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="CSRF failed")
    return manager


async def _read_warm_request(request: Request, *, duration_required: bool) -> int:
    content_type = request.headers.get("content-type", "")
    if content_type.partition(";")[0].strip().lower() != "application/json":
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="JSON is required",
        )
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid body length") from None
        if declared_length < 0 or declared_length > _MAX_WARM_REQUEST_BYTES:
            raise HTTPException(status_code=413, detail="request body is too large")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > _MAX_WARM_REQUEST_BYTES:
            raise HTTPException(status_code=413, detail="request body is too large")
        body.extend(chunk)

    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    def reject_constant(_value: str) -> None:
        raise ValueError("non-finite number")

    try:
        payload = json.loads(
            bytes(body).decode("utf-8", errors="strict"),
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, ValueError):
        raise HTTPException(status_code=400, detail="invalid JSON body") from None
    expected = {"duration_minutes"} if duration_required else set()
    if not isinstance(payload, dict) or set(payload) != expected:
        raise HTTPException(status_code=400, detail="invalid JSON fields")
    if not duration_required:
        return 0
    duration_minutes: object = payload["duration_minutes"]
    if (
        isinstance(duration_minutes, bool)
        or not isinstance(duration_minutes, int)
        or not 1 <= duration_minutes <= 90
    ):
        raise HTTPException(status_code=422, detail="invalid warm duration")
    return duration_minutes * 60


async def _warm_deployment_available(session: AsyncSession) -> bool:
    deployment = await session.scalar(
        select(SaladDeployment).where(SaladDeployment.is_current.is_(True)).limit(1)
    )
    return bool(
        deployment is not None
        and deployment.state == SaladDeploymentState.ACTIVE
        and deployment.desired_state == DesiredDeploymentState.ACTIVE
        and deployment.provider_queue_id
        and deployment.provider_container_group_id
        and deployment.min_replicas == 0
        and deployment.max_replicas == 1
        and deployment.desired_queue_length == 1
    )


def _warm_status_payload(
    settings: Settings,
    *,
    current: ExperimentWarmLeaseStatus | None,
    available: bool,
) -> dict[str, object]:
    if current is None:
        state = "off"
        expires_at = None
        hard_expires_at = None
        ready = False
        usable = False
        remaining_seconds = 0
        hard_remaining_seconds = 0
        idle_ttl_seconds = DEFAULT_EXPERIMENT_WARM_LEASE_SECONDS
        max_cost_usd = None
    else:
        state = {
            ExperimentWarmLeaseState.STARTING: "starting",
            ExperimentWarmLeaseState.ACTIVE: "warm",
            ExperimentWarmLeaseState.ENDING: "ending",
        }.get(current.state, "off")
        expires_at = current.expires_at.isoformat().replace("+00:00", "Z")
        hard_expires_at = current.hard_expires_at.isoformat().replace("+00:00", "Z")
        ready = current.ready
        usable = current.usable
        remaining_seconds = current.remaining_seconds
        hard_remaining_seconds = current.hard_remaining_seconds
        idle_ttl_seconds = current.idle_ttl_seconds
        max_cost_usd = f"{current.max_cost_microusd / 1_000_000:.2f}"
    return {
        "schema_version": 1,
        "available": available,
        "state": state,
        "expires_at": expires_at,
        "hard_expires_at": hard_expires_at,
        "ready": ready,
        "usable": usable,
        "remaining_seconds": remaining_seconds,
        "hard_remaining_seconds": hard_remaining_seconds,
        "idle_ttl_seconds": idle_ttl_seconds,
        "hourly_rate_usd": f"{settings.salad_max_hourly_cost_usd:.2f}",
        "max_cost_usd": max_cost_usd,
        "controller_auto_stop_minutes": 90,
    }


def _warm_error(request: Request, *, status_code: int, message: str) -> Response:
    return _secure_response(
        request,
        JSONResponse(
            status_code=status_code,
            content={"schema_version": 1, "detail": message[:300]},
        ),
    )


def _form_csrf_token(request: Request, principal: AuthenticatedPrincipal) -> str:
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
