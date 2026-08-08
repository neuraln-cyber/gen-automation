import hmac
import json
import logging
import secrets
from pathlib import Path
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from gen_automation.api.browser_experiment_forms import (
    BrowserExperimentForm,
    BrowserExperimentFormError,
    read_experiment_form,
)
from gen_automation.api.browser_new_set_forms import (
    form_key_matches,
    new_set_csrf_token,
    new_set_form_key,
)
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
from gen_automation.services.experiment_support import (
    classify_experiment_model_readiness,
    estimate_experiment_session_cost_from_settings,
)
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
    ExperimentInputError,
    ExperimentNotFoundError,
    create_experiment,
    experiment_progress_payload,
    load_experiment_status,
)
from gen_automation.services.generation import GenerationPlanConflictError
from gen_automation.services.new_sets import (
    NewSetInputError,
    NewSetOptions,
    list_new_set_options,
)
from gen_automation.services.progressive_assets import (
    AvailableRawMaster,
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
_MAX_WARM_REQUEST_BYTES = 4 * 1024
logger = logging.getLogger(__name__)


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
        return _experiment_form_response(request, principal=principal, options=options)
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
    session: Session,
    principal: ReleaseReader,
) -> Response:
    if principal.role not in _MANAGER_ROLES:
        return _error_response(
            request,
            principal=principal,
            status_code=status.HTTP_403_FORBIDDEN,
            heading="Experiment was not created",
            message="Your account cannot create generation experiments.",
        )
    warm_failed = False
    try:
        form = await read_experiment_form(request)
    except BrowserExperimentFormError as error:
        options = await list_new_set_options(session)
        return _experiment_form_response(
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
            raise HTTPException(status_code=403, detail="CSRF validation failed")
        expected_key = new_set_form_key(
            settings,
            session_id=manager.session_id,
            submission_id=form.submission_id,
        )
        if not form_key_matches(form.idempotency_key, expected_key):
            raise HTTPException(status_code=400, detail="form idempotency validation failed")
        current_options = await list_new_set_options(session)
        readiness = classify_experiment_model_readiness(settings, current_options)
        warm_checkpoints = {
            item.option.approval_id for item in readiness.checkpoints if item.warm_ready
        }
        warm_loras = {item.option.approval_id for item in readiness.loras if item.warm_ready}
        restart_variants = [
            variant.label
            for variant in form.command.variants
            if variant.profile.checkpoint_approval_id not in warm_checkpoints
            or any(lora.approval_id not in warm_loras for lora in variant.profile.loras)
        ]
        if restart_variants:
            labels = ", ".join(restart_variants[:3])
            suffix = " and more" if len(restart_variants) > 3 else ""
            raise ExperimentInputError(
                f"{labels}{suffix}: onboard the checkpoint or LoRA into the worker manifest "
                "and redeploy before queueing"
            )
        if form.command.keep_warm:
            try:
                await ensure_experiment_warm_lease(
                    session,
                    actor=str(manager.user_id),
                    duration_seconds=DEFAULT_EXPERIMENT_WARM_LEASE_SECONDS,
                )
            except Exception:
                await session.rollback()
                warm_failed = True
                logger.exception(
                    "optional experiment warm lease could not be ensured; queueing normally"
                )
        await create_experiment(
            session,
            command=form.command,
            idempotency_key=form.idempotency_key,
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
    except (ExperimentInputError, NewSetInputError) as error:
        await session.rollback()
        return await _submission_error(
            request,
            session=session,
            principal=principal,
            form=form,
            status_code=status.HTTP_409_CONFLICT,
            message=str(error).capitalize() + ".",
        )
    except (ConflictError, GenerationPlanConflictError, NotFoundError) as error:
        await session.rollback()
        return await _submission_error(
            request,
            session=session,
            principal=principal,
            form=form,
            status_code=status.HTTP_409_CONFLICT,
            message=f"The experiment could not be queued: {error}.",
        )
    except (IntegrityError, ValidationError):
        await session.rollback()
        return await _submission_error(
            request,
            session=session,
            principal=principal,
            form=form,
            status_code=status.HTTP_409_CONFLICT,
            message="An approved input changed while the experiment was queued. Reload and retry.",
        )
    location = f"/dashboard/experiments/{form.command.group_slug}"
    if warm_failed:
        location += "?warm=unavailable"
    return _secure_response(
        request,
        RedirectResponse(
            url=location,
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


async def _submission_error(
    request: Request,
    *,
    session: AsyncSession,
    principal: AuthenticatedPrincipal,
    form: BrowserExperimentForm,
    status_code: int,
    message: str,
) -> Response:
    options = await list_new_set_options(session)
    return _experiment_form_response(
        request,
        principal=principal,
        options=options,
        values=form.values,
        submission_id=form.submission_id,
        idempotency_key=form.idempotency_key,
        error_message=message,
        status_code=status_code,
    )


def _experiment_form_response(
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
    defaults = _default_values(options)
    if values:
        defaults.update(
            {
                key: value
                for key, value in values.items()
                if key not in {"csrf_token", "submission_id", "idempotency_key"}
            }
        )
    readiness = classify_experiment_model_readiness(settings, options)
    cost_estimate = estimate_experiment_session_cost_from_settings(
        settings,
        idle_ttl_seconds=900,
        hard_max_duration_seconds=5400,
    )
    checkpoint_readiness = {str(item.option.approval_id): item for item in readiness.checkpoints}
    lora_readiness = {str(item.option.approval_id): item for item in readiness.loras}
    return _secure_response(
        request,
        templates.TemplateResponse(
            request=request,
            name="dashboard/experiment_new.html",
            context={
                "page_title": "Experiment Lab",
                "principal": principal,
                "options": options,
                "form_values": defaults,
                "csrf_token": _form_csrf_token(request, principal),
                "submission_id": resolved_submission_id,
                "idempotency_key": resolved_key,
                "error_message": error_message,
                "model_readiness": readiness,
                "checkpoint_readiness": checkpoint_readiness,
                "lora_readiness": lora_readiness,
                "warm_idle_cost_usd": (
                    f"{cost_estimate.initial_idle_commitment_microusd / 1_000_000:.2f}"
                ),
                "warm_session_max_usd": (f"{cost_estimate.session_max_microusd / 1_000_000:.2f}"),
                "salad_daily_budget_usd": f"{settings.salad_daily_budget_usd:.2f}",
                "salad_monthly_budget_usd": f"{settings.salad_monthly_budget_usd:.2f}",
            },
            status_code=status_code,
        ),
    )


def _default_values(options: NewSetOptions) -> dict[str, str]:
    preferred_workflow = next(
        (
            workflow
            for workflow in options.workflows
            if workflow.name.casefold() == "illustrious base detailer"
        ),
        options.workflows[0] if options.workflows else None,
    )
    values = {
        "group_slug": f"experiment-{secrets.token_hex(6)}",
        "experiment_title": "Style comparison",
        "outputs_per_variant": "2",
        "paired_seeds": "true",
        "keep_warm": "true",
        "base_seed": str(secrets.randbelow(2**63)),
        "variant_plan": "",
        "subject_id": str(options.subjects[0].approval_id) if options.subjects else "",
        "subject_2_id": "",
        "composition_mode": "single",
        "character_a_prompt": "",
        "character_b_prompt": "",
        "checkpoint_id": str(options.checkpoints[0].approval_id) if options.checkpoints else "",
        "workflow_id": str(preferred_workflow.approval_id) if preferred_workflow else "",
        "prompt": "",
        "negative_prompt": "",
        "detailer_prompt": "detailed face",
        "detailer_negative_prompt": "",
        "seed": str(secrets.randbelow(2**63)),
        "width": "1144",
        "height": "1480",
        "cfg": "6.0",
        "steps": "30",
        "sampler": "euler_ancestral",
        "scheduler": "karras",
        "clip_skip": "2",
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
    }
    for slot in range(1, 9):
        values[f"lora_{slot}_id"] = ""
        values[f"lora_{slot}_weight"] = ""
    return values


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
