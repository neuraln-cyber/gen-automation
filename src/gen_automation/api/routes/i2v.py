"""Focused operator API for queued image-to-video generation."""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gen_automation.api.security import ReleaseManager, ReleaseReader
from gen_automation.config import Settings
from gen_automation.db.models import I2VJob, I2VPreset
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
from gen_automation.domain.i2v_loras import (
    I2VLoraPromptError,
    I2VLoraSelectionError,
    normalize_i2v_settings,
    selected_i2v_loras,
    validate_i2v_lora_prompt,
)
from gen_automation.i2v_worker.lora_catalog import (
    LORA_CATALOG,
    MAX_REVIEWED_LORA_SELECTIONS,
    MAX_REVIEWED_LORA_STRENGTH,
    MIN_REVIEWED_LORA_STRENGTH,
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
    I2V_RUNTIME_WORKER_ID,
    I2VConflictError,
    I2VInputError,
    I2VNotFoundError,
    bind_i2v_runpod_execution,
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
from gen_automation.services.i2v_runpod_claims import (
    I2VRunPodClaimError,
    verify_i2v_runpod_claim_token,
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


class RunPodExecutionClaim(_RequestModel):
    provider_job_id: str = Field(min_length=5, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


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


class ReviewedLoraRead(BaseModel):
    catalog_id: str
    display_name: str
    creator_name: str
    canonical_source_url: str = Field(
        pattern=r"^https://civitai[.]com/models/[0-9]+$",
    )
    canonical_version_urls: tuple[str, str]
    trigger_words: tuple[str, ...]
    automatic_trigger_words: tuple[str, ...]
    recommended_initial_strength: float
    strength_guidance: str
    usage_notes: str
    minimum_strength: float
    maximum_strength: float
    strength_step: float
    available: bool
    prompt_behavior: str
    credit_required: bool
    commercial_use: tuple[str, ...]
    derivatives_allowed: bool
    different_license_allowed: bool
    usage_recorded_at: str

    @field_validator("canonical_version_urls")
    @classmethod
    def validate_canonical_version_urls(
        cls,
        value: tuple[str, str],
    ) -> tuple[str, str]:
        pattern = re.compile(r"^https://civitai[.]com/models/[0-9]+[?]modelVersionId=[0-9]+$")
        if any(pattern.fullmatch(item) is None for item in value):
            raise ValueError("canonical version URLs must be exact Civitai versions")
        return value


class ReviewedLoraCatalogRead(BaseModel):
    profile_enabled: bool
    maximum_selections: int
    message: str
    loras: tuple[ReviewedLoraRead, ...]


@router.get("/loras", response_model=ReviewedLoraCatalogRead)
async def reviewed_loras(
    request: Request,
    _principal: ReleaseReader,
) -> ReviewedLoraCatalogRead:
    enabled = bool(request.app.state.settings.i2v_lora_profile_enabled)
    loras = tuple(
        ReviewedLoraRead(
            catalog_id=entry.catalog_id,
            display_name=entry.display_name,
            creator_name=entry.creator_name,
            canonical_source_url=entry.canonical_source_url,
            canonical_version_urls=(
                entry.high.canonical_version_url,
                entry.low.canonical_version_url,
            ),
            trigger_words=entry.trigger_words,
            automatic_trigger_words=entry.automatic_trigger_words,
            recommended_initial_strength=entry.recommended_initial_strength,
            strength_guidance=entry.strength_guidance,
            usage_notes=entry.usage_notes,
            minimum_strength=MIN_REVIEWED_LORA_STRENGTH,
            maximum_strength=MAX_REVIEWED_LORA_STRENGTH,
            strength_step=0.01,
            available=enabled,
            prompt_behavior=(
                "Trigger text is appended once to the effective positive prompt"
                if entry.automatic_trigger_words
                else (
                    "Include the one concept keyword that matches the requested action"
                    if entry.trigger_words
                    else "No trained trigger; describe the pose and action in the positive prompt"
                )
            ),
            credit_required=entry.source_usage.credit_required,
            commercial_use=entry.source_usage.commercial_use,
            derivatives_allowed=entry.source_usage.derivatives_allowed,
            different_license_allowed=entry.source_usage.different_license_allowed,
            usage_recorded_at=entry.source_usage.recorded_at,
        )
        for entry in LORA_CATALOG.values()
    )
    return ReviewedLoraCatalogRead(
        profile_enabled=enabled,
        maximum_selections=MAX_REVIEWED_LORA_SELECTIONS,
        message=(
            (
                "Reviewed LoRAs are installed and available on the matching worker. "
                f"Select up to {MAX_REVIEWED_LORA_SELECTIONS}."
            )
            if enabled
            else (
                "LoRAs remain unavailable until exact worker artifact and readiness "
                "verification passes."
            )
        ),
        loras=loras,
    )


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
    request: Request,
    session: Session,
    principal: ReleaseManager,
) -> I2VPresetSnapshot:
    try:
        normalized_settings = _settings_for_profile(
            payload.settings,
            enabled=request.app.state.settings.i2v_lora_profile_enabled,
        )
        _validate_lora_prompt(payload.positive_prompt, normalized_settings)
        return await create_i2v_preset(
            session,
            actor_user_id=principal.user_id,
            draft=_preset_draft(payload, settings=normalized_settings),
        )
    except HTTPException:
        raise
    except (I2VInputError, I2VConflictError) as error:
        raise _core_http_error(error) from error


@router.put("/presets/{preset_id}", response_model=I2VPresetSnapshot)
async def update_preset(
    preset_id: UUID,
    payload: PresetUpdate,
    request: Request,
    session: Session,
    principal: ReleaseManager,
) -> I2VPresetSnapshot:
    try:
        normalized_settings = _settings_for_profile(
            payload.settings,
            enabled=request.app.state.settings.i2v_lora_profile_enabled,
        )
        _validate_lora_prompt(payload.positive_prompt, normalized_settings)
        return await update_i2v_preset(
            session,
            actor_user_id=principal.user_id,
            preset_id=preset_id,
            draft=_preset_draft(payload, settings=normalized_settings),
            expected_lock_version=payload.expected_lock_version,
        )
    except HTTPException:
        raise
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
    _require_i2v_queue_writes_enabled(settings)
    normalized_settings = (
        _settings_for_profile(
            payload.settings,
            enabled=settings.i2v_lora_profile_enabled,
        )
        if payload.settings is not None
        else None
    )
    if (
        not settings.i2v_lora_profile_enabled
        and not _settings_override_disables_loras(normalized_settings)
        and payload.preset_id is not None
    ):
        preset_settings = await session.scalar(
            select(I2VPreset.settings).where(
                I2VPreset.id == payload.preset_id,
                I2VPreset.created_by_user_id == principal.user_id,
            )
        )
        if preset_settings is not None and _settings_have_loras(preset_settings):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="reviewed I2V LoRAs are paused until the matching worker rollout passes",
            )
    effective_prompt = payload.positive_prompt
    effective_settings = dict(normalized_settings or {})
    if payload.preset_id is not None:
        preset = await session.scalar(
            select(I2VPreset).where(
                I2VPreset.id == payload.preset_id,
                I2VPreset.created_by_user_id == principal.user_id,
            )
        )
        if preset is not None:
            if effective_prompt is None:
                effective_prompt = preset.positive_prompt
            merged_settings = dict(preset.settings)
            merged_settings.update(effective_settings)
            effective_settings = _settings_for_profile(
                merged_settings,
                enabled=settings.i2v_lora_profile_enabled,
            )
    _validate_lora_prompt(effective_prompt or "", effective_settings)
    created: list[I2VJobSnapshot] = []
    draft = I2VJobDraft(
        input_id=payload.input_id,
        preset_id=payload.preset_id,
        positive_prompt=payload.positive_prompt,
        negative_prompt=payload.negative_prompt,
        settings=normalized_settings,
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


@router.post("/runpod/claim", status_code=status.HTTP_204_NO_CONTENT)
async def claim_runpod_execution(
    payload: RunPodExecutionClaim,
    request: Request,
    session: Session,
) -> Response:
    settings: Settings = request.app.state.settings
    api_key = settings.i2v_runpod_api_key
    authorization = request.headers.get("authorization", "")
    scheme, separator, token = authorization.partition(" ")
    if (
        not settings.i2v_runpod_enabled
        or api_key is None
        or separator != " "
        or scheme.casefold() != "bearer"
        or not token
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid claim")
    try:
        identity = verify_i2v_runpod_claim_token(
            token,
            secret=api_key.get_secret_value(),
        )
        await bind_i2v_runpod_execution(
            session,
            job_id=identity.job_id,
            attempt_id=identity.attempt_id,
            request_sha256=identity.request_sha256,
            submission_key=identity.submission_key,
            provider_job_id=payload.provider_job_id,
            worker_id=I2V_RUNTIME_WORKER_ID,
            lease_duration=timedelta(seconds=settings.i2v_worker_lease_seconds),
        )
    except I2VRunPodClaimError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid claim",
        ) from None
    except (I2VNotFoundError, I2VConflictError):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="claim rejected") from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.patch("/queue", response_model=tuple[I2VJobSnapshot, ...])
async def move_queue_job(
    payload: QueueMove,
    request: Request,
    session: Session,
    principal: ReleaseManager,
) -> tuple[I2VJobSnapshot, ...]:
    _require_i2v_queue_writes_enabled(request.app.state.settings)
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
    request: Request,
    session: Session,
    principal: ReleaseManager,
) -> I2VJobSnapshot:
    _require_i2v_queue_writes_enabled(request.app.state.settings)
    frozen_job = (
        await session.execute(
            select(I2VJob.settings_snapshot, I2VJob.positive_prompt).where(
                I2VJob.id == job_id,
                I2VJob.created_by_user_id == principal.user_id,
            )
        )
    ).one_or_none()
    if frozen_job is not None:
        job_settings, positive_prompt = frozen_job
        try:
            normalized_job_settings = normalize_i2v_settings(job_settings)
            validate_i2v_lora_prompt(positive_prompt, normalized_job_settings)
        except I2VLoraSelectionError as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"the frozen I2V job has invalid LoRA settings: {error}",
            ) from error
        if not request.app.state.settings.i2v_lora_profile_enabled and _settings_have_loras(
            normalized_job_settings
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="reviewed I2V LoRAs are paused until the matching worker rollout passes",
            )
    try:
        return await retry_i2v_job(
            session,
            actor_user_id=principal.user_id,
            job_id=job_id,
        )
    except (I2VInputError, I2VNotFoundError, I2VConflictError) as error:
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


def _preset_draft(
    payload: PresetCreate | PresetUpdate,
    *,
    settings: dict[str, Any],
) -> I2VPresetDraft:
    return I2VPresetDraft(
        name=payload.name,
        description=payload.description,
        positive_prompt=payload.positive_prompt,
        negative_prompt=payload.negative_prompt,
        settings=settings,
    )


def _settings_for_profile(value: dict[str, Any], *, enabled: bool) -> dict[str, Any]:
    try:
        normalized = normalize_i2v_settings(value)
    except I2VLoraSelectionError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error
    if selected_i2v_loras(normalized) and not enabled:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="reviewed I2V LoRAs are paused until the matching worker rollout passes",
        )
    return normalized


def _require_i2v_queue_writes_enabled(settings: Settings) -> None:
    if settings.i2v_hires_profile_enabled:
        return
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="image-to-video queue writes are paused for the coordinated worker rollout",
    )


def _settings_override_disables_loras(value: dict[str, Any] | None) -> bool:
    return value is not None and "loras" in value and not value["loras"]


def _settings_have_loras(value: dict[str, Any]) -> bool:
    """Fail closed for legacy or malformed frozen LoRA settings."""

    return "loras" in value and value["loras"] != []


def _validate_lora_prompt(positive_prompt: str, settings: dict[str, Any]) -> None:
    try:
        validate_i2v_lora_prompt(positive_prompt, settings)
    except I2VLoraPromptError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error


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
