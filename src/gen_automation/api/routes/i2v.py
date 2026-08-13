"""Focused operator API for queued image-to-video generation."""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from gen_automation.api.security import ReleaseManager, ReleaseReader
from gen_automation.config import Settings
from gen_automation.db.session import get_session
from gen_automation.domain.enums import AdminRole
from gen_automation.domain.i2v import (
    I2VInputSnapshot,
    I2VJobDraft,
    I2VJobSnapshot,
    I2VJobState,
    I2VOutputSnapshot,
    I2VPresetDraft,
    I2VPresetSnapshot,
    I2VWorkerDeploymentSnapshot,
)
from gen_automation.services.dashboard_previews import (
    DASHBOARD_PREVIEW_CACHE_CONTROL,
    DASHBOARD_PREVIEW_CONTENT_TYPE,
    DashboardPreviewConflictError,
    DashboardPreviewNotFoundError,
    DashboardPreviewRenderError,
    DashboardPreviewStorageError,
    load_or_create_dashboard_preview,
)
from gen_automation.services.i2v import (
    I2VConflictError,
    I2VInputError,
    I2VNotFoundError,
    create_i2v_job,
    create_i2v_preset,
    delete_i2v_preset,
    get_i2v_worker_deployment,
    list_i2v_jobs,
    list_i2v_presets,
    list_recent_i2v_outputs,
    reorder_i2v_queue,
    request_i2v_job_cancellation,
    retry_i2v_job,
    update_i2v_preset,
)
from gen_automation.services.i2v_media import (
    I2VLibraryImage,
    I2VMediaConflictError,
    I2VMediaError,
    I2VMediaNotFoundError,
    I2VMediaStorageError,
    complete_i2v_upload,
    create_i2v_upload_intent,
    list_i2v_library_images,
    presign_i2v_output_download,
    register_i2v_generation_asset,
)
from gen_automation.storage.base import ObjectStore

router = APIRouter(prefix="/i2v", tags=["image-to-video"])
Session = Annotated[AsyncSession, Depends(get_session)]
_MANAGER_ROLES = frozenset({AdminRole.OWNER, AdminRole.ADMIN})


class _RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class UploadIntentCreate(_RequestModel):
    display_name: str = Field(min_length=1, max_length=255)
    content_type: str = Field(min_length=1, max_length=100)


class UploadGrantRead(BaseModel):
    upload_id: UUID
    display_name: str
    content_type: str
    max_bytes: int
    url: str
    method: str
    fields: dict[str, str]
    headers: dict[str, str]


class UploadComplete(_RequestModel):
    display_name: str = Field(min_length=1, max_length=255)


class GeneratedInputCreate(_RequestModel):
    asset_id: UUID


class PresetCreate(_RequestModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    positive_prompt: str = Field(default="", max_length=100_000)
    negative_prompt: str = Field(default="", max_length=100_000)
    settings: dict[str, Any] = Field(default_factory=dict)


class PresetUpdate(PresetCreate):
    expected_lock_version: int = Field(ge=1)


class JobCreate(_RequestModel):
    input_id: UUID
    preset_id: UUID | None = None
    positive_prompt: str | None = Field(default=None, max_length=100_000)
    negative_prompt: str | None = Field(default=None, max_length=100_000)
    settings: dict[str, Any] | None = None
    batch_count: int = Field(default=1, ge=1)


class JobBatchRead(BaseModel):
    jobs: tuple[I2VJobSnapshot, ...]


class QueueMove(_RequestModel):
    job_id: UUID
    before_job_id: UUID | None = None
    after_job_id: UUID | None = None


class OutputRead(BaseModel):
    output: I2VOutputSnapshot
    playback_url: str
    download_url: str


class WorkerRead(BaseModel):
    configured: bool
    status_available: bool
    message: str
    deployment: I2VWorkerDeploymentSnapshot | None


@router.get("/source-images", response_model=tuple[I2VLibraryImage, ...])
async def source_images(
    session: Session,
    principal: ReleaseReader,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> tuple[I2VLibraryImage, ...]:
    _require_manager_reader(principal.role)
    return await list_i2v_library_images(session, limit=limit)


@router.get(
    "/source-images/{asset_id}/preview/{source_token}.jpg",
    response_class=Response,
    response_model=None,
)
async def source_image_preview(
    asset_id: UUID,
    source_token: str,
    request: Request,
    session: Session,
    principal: ReleaseReader,
) -> Response:
    _require_manager_reader(principal.role)
    settings: Settings = request.app.state.settings
    try:
        preview = await load_or_create_dashboard_preview(
            session,
            _store(request),
            asset_id=asset_id,
            source_token=source_token,
            max_master_bytes=settings.storage_max_image_bytes,
        )
    except DashboardPreviewNotFoundError:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND, content={"detail": "image not found"}
        )
    except DashboardPreviewConflictError:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"detail": "image preview source is unavailable"},
        )
    except (DashboardPreviewStorageError, DashboardPreviewRenderError):
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"detail": "image preview is temporarily unavailable"},
        )
    headers = {
        "Cache-Control": DASHBOARD_PREVIEW_CACHE_CONTROL,
        "ETag": preview.etag,
        "Vary": "Cookie",
        "X-Content-Type-Options": "nosniff",
    }
    if request.headers.get("if-none-match") == preview.etag:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
    return Response(
        content=preview.data, media_type=DASHBOARD_PREVIEW_CONTENT_TYPE, headers=headers
    )


@router.post(
    "/inputs/uploads",
    response_model=UploadGrantRead,
    status_code=status.HTTP_201_CREATED,
)
async def begin_upload(
    payload: UploadIntentCreate,
    request: Request,
    _session: Session,
    principal: ReleaseManager,
) -> UploadGrantRead:
    settings: Settings = request.app.state.settings
    store = _store(request)
    try:
        intent = await create_i2v_upload_intent(
            store,
            actor_user_id=principal.user_id,
            display_name=payload.display_name,
            content_type=payload.content_type,
            max_bytes=settings.storage_max_image_bytes,
            expires_in=settings.storage_presign_ttl_seconds,
        )
    except I2VMediaError as error:
        raise _media_http_error(error) from error
    return UploadGrantRead(
        upload_id=intent.upload_id,
        display_name=intent.display_name,
        content_type=intent.content_type,
        max_bytes=intent.max_bytes,
        url=intent.grant.url,
        method=intent.grant.method,
        fields=intent.grant.fields,
        headers=intent.grant.headers,
    )


@router.post(
    "/inputs/uploads/{upload_id}:complete",
    response_model=I2VInputSnapshot,
    status_code=status.HTTP_201_CREATED,
)
async def finish_upload(
    upload_id: UUID,
    payload: UploadComplete,
    request: Request,
    session: Session,
    principal: ReleaseManager,
) -> I2VInputSnapshot:
    settings: Settings = request.app.state.settings
    try:
        return await complete_i2v_upload(
            session,
            _store(request),
            actor_user_id=principal.user_id,
            upload_id=upload_id,
            display_name=payload.display_name,
            max_bytes=settings.storage_max_image_bytes,
        )
    except I2VMediaError as error:
        raise _media_http_error(error) from error
    except (I2VInputError, I2VConflictError) as error:
        raise _core_http_error(error) from error


@router.post(
    "/inputs/from-asset",
    response_model=I2VInputSnapshot,
    status_code=status.HTTP_201_CREATED,
)
async def input_from_asset(
    payload: GeneratedInputCreate,
    request: Request,
    session: Session,
    principal: ReleaseManager,
) -> I2VInputSnapshot:
    try:
        return await register_i2v_generation_asset(
            session,
            _store(request),
            actor_user_id=principal.user_id,
            asset_id=payload.asset_id,
        )
    except I2VMediaError as error:
        raise _media_http_error(error) from error
    except (I2VInputError, I2VConflictError) as error:
        raise _core_http_error(error) from error


@router.get("/presets", response_model=tuple[I2VPresetSnapshot, ...])
async def presets(session: Session, principal: ReleaseReader) -> tuple[I2VPresetSnapshot, ...]:
    return await list_i2v_presets(session, actor_user_id=principal.user_id)


@router.post("/presets", response_model=I2VPresetSnapshot, status_code=status.HTTP_201_CREATED)
async def create_preset(
    payload: PresetCreate,
    session: Session,
    principal: ReleaseManager,
) -> I2VPresetSnapshot:
    try:
        return await create_i2v_preset(
            session,
            actor_user_id=principal.user_id,
            draft=_preset_draft(payload),
        )
    except (I2VInputError, I2VConflictError) as error:
        raise _core_http_error(error) from error


@router.put("/presets/{preset_id}", response_model=I2VPresetSnapshot)
async def update_preset(
    preset_id: UUID,
    payload: PresetUpdate,
    session: Session,
    principal: ReleaseManager,
) -> I2VPresetSnapshot:
    try:
        return await update_i2v_preset(
            session,
            actor_user_id=principal.user_id,
            preset_id=preset_id,
            draft=_preset_draft(payload),
            expected_lock_version=payload.expected_lock_version,
        )
    except (I2VInputError, I2VNotFoundError, I2VConflictError) as error:
        raise _core_http_error(error) from error


@router.delete("/presets/{preset_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_preset(
    preset_id: UUID,
    session: Session,
    principal: ReleaseManager,
) -> None:
    try:
        await delete_i2v_preset(
            session,
            actor_user_id=principal.user_id,
            preset_id=preset_id,
        )
    except (I2VNotFoundError, I2VConflictError) as error:
        raise _core_http_error(error) from error


@router.get("/jobs", response_model=tuple[I2VJobSnapshot, ...])
async def jobs(
    session: Session,
    principal: ReleaseReader,
    state_filter: Annotated[list[I2VJobState] | None, Query(alias="state")] = None,
) -> tuple[I2VJobSnapshot, ...]:
    return await list_i2v_jobs(
        session,
        actor_user_id=principal.user_id,
        states=state_filter,
        limit=None,
    )


@router.post("/jobs", response_model=JobBatchRead, status_code=status.HTTP_201_CREATED)
async def enqueue_jobs(
    payload: JobCreate,
    request: Request,
    session: Session,
    principal: ReleaseManager,
) -> JobBatchRead:
    settings: Settings = request.app.state.settings
    if not settings.i2v_hires_profile_enabled:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="image-to-video submissions are paused for the coordinated worker rollout",
        )
    created: list[I2VJobSnapshot] = []
    draft = I2VJobDraft(
        input_id=payload.input_id,
        preset_id=payload.preset_id,
        positive_prompt=payload.positive_prompt,
        negative_prompt=payload.negative_prompt,
        settings=payload.settings,
    )
    try:
        for _ in range(payload.batch_count):
            created.append(
                await create_i2v_job(
                    session,
                    actor_user_id=principal.user_id,
                    draft=draft,
                )
            )
    except (I2VInputError, I2VNotFoundError, I2VConflictError) as error:
        raise _core_http_error(error) from error
    return JobBatchRead(jobs=tuple(created))


@router.patch("/queue", response_model=tuple[I2VJobSnapshot, ...])
async def move_queue_job(
    payload: QueueMove,
    session: Session,
    principal: ReleaseManager,
) -> tuple[I2VJobSnapshot, ...]:
    if payload.before_job_id is not None and payload.after_job_id is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="provide at most one of before_job_id or after_job_id",
        )
    try:
        return await reorder_i2v_queue(
            session,
            actor_user_id=principal.user_id,
            job_id=payload.job_id,
            before_job_id=payload.before_job_id,
            after_job_id=payload.after_job_id,
        )
    except (I2VInputError, I2VNotFoundError, I2VConflictError) as error:
        raise _core_http_error(error) from error


@router.post("/jobs/{job_id}:cancel", response_model=I2VJobSnapshot)
async def cancel_job(
    job_id: UUID,
    session: Session,
    principal: ReleaseManager,
) -> I2VJobSnapshot:
    try:
        return await request_i2v_job_cancellation(
            session,
            actor_user_id=principal.user_id,
            job_id=job_id,
        )
    except (I2VNotFoundError, I2VConflictError) as error:
        raise _core_http_error(error) from error


@router.post("/jobs/{job_id}:retry", response_model=I2VJobSnapshot)
async def retry_job(
    job_id: UUID,
    session: Session,
    principal: ReleaseManager,
) -> I2VJobSnapshot:
    try:
        return await retry_i2v_job(
            session,
            actor_user_id=principal.user_id,
            job_id=job_id,
        )
    except (I2VNotFoundError, I2VConflictError) as error:
        raise _core_http_error(error) from error


@router.get("/outputs/recent", response_model=tuple[OutputRead, ...])
async def recent_outputs(
    session: Session,
    principal: ReleaseReader,
    limit: Annotated[int, Query(ge=1, le=100)] = 24,
) -> tuple[OutputRead, ...]:
    outputs = await list_recent_i2v_outputs(
        session,
        actor_user_id=principal.user_id,
        limit=limit,
    )
    return tuple(
        OutputRead(
            output=item,
            playback_url=f"/api/v1/i2v/outputs/{item.output_id}/download",
            download_url=f"/api/v1/i2v/outputs/{item.output_id}/download?attachment=true",
        )
        for item in outputs
    )


@router.get("/outputs/{output_id}/download", response_model=None)
async def download_output(
    output_id: UUID,
    request: Request,
    session: Session,
    principal: ReleaseReader,
    attachment: bool = False,
) -> RedirectResponse:
    outputs = await list_recent_i2v_outputs(
        session,
        actor_user_id=principal.user_id,
        limit=None,
    )
    output = next((item for item in outputs if item.output_id == output_id), None)
    if output is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="video not found")
    settings: Settings = request.app.state.settings
    try:
        signed = await presign_i2v_output_download(
            _store(request),
            output=output,
            expires_in=min(settings.storage_presign_ttl_seconds, 900),
            attachment=attachment,
        )
    except I2VMediaError as error:
        raise _media_http_error(error) from error
    response = RedirectResponse(signed, status_code=status.HTTP_307_TEMPORARY_REDIRECT)
    response.headers["Cache-Control"] = "private, no-store"
    return response


@router.get("/worker", response_model=WorkerRead)
async def worker_status(
    request: Request,
    session: Session,
    _principal: ReleaseReader,
) -> WorkerRead:
    deployment = await get_i2v_worker_deployment(session, deployment_id=None)
    configured = bool(getattr(request.app.state.settings, "i2v_enabled", False))
    if deployment is None:
        return WorkerRead(
            configured=configured,
            status_available=False,
            message=(
                "No worker has been provisioned yet. Queued jobs will wait for a worker."
                if configured
                else "Image-to-video worker provisioning is not configured."
            ),
            deployment=None,
        )
    return WorkerRead(
        configured=configured,
        status_available=True,
        message=f"Provider reports {deployment.state.value}.",
        deployment=deployment,
    )


def _preset_draft(payload: PresetCreate | PresetUpdate) -> I2VPresetDraft:
    return I2VPresetDraft(
        name=payload.name,
        description=payload.description,
        positive_prompt=payload.positive_prompt,
        negative_prompt=payload.negative_prompt,
        settings=payload.settings,
    )


def _require_manager_reader(role: AdminRole) -> None:
    if role not in _MANAGER_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="image-to-video management permission required",
        )


def _store(request: Request) -> ObjectStore:
    store: ObjectStore | None = getattr(request.app.state, "object_store", None)
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="private storage is unavailable",
        )
    return store


def _media_http_error(error: I2VMediaError) -> HTTPException:
    if isinstance(error, I2VMediaNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error))
    if isinstance(error, I2VMediaConflictError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error))
    if isinstance(error, I2VMediaStorageError):
        return HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error))
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error))


def _core_http_error(error: Exception) -> HTTPException:
    if isinstance(error, I2VNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error))
    if isinstance(error, I2VConflictError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error))
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(error))
