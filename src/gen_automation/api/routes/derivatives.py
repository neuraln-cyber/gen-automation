from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import (
    APIRouter,
    File,
    Form,
    Header,
    HTTPException,
    Request,
    Response,
    UploadFile,
    status,
)
from pydantic import BaseModel, ConfigDict, Field

from gen_automation.api.security import (
    PublicationMutationOwner,
    PublicationReader,
    ReviewPrincipal,
    Session,
)
from gen_automation.services.derivative_pipeline import (
    DerivativePipelineConflictError,
    DerivativePipelineInputError,
    DerivativePipelineNotFoundError,
)
from gen_automation.services.derivatives import WatermarkPosition
from gen_automation.services.review_derivatives import (
    prepare_completed_review_derivatives,
)
from gen_automation.services.watermarks import (
    MAX_WATERMARK_BYTES,
    WatermarkConflictError,
    WatermarkInputError,
    WatermarkNotFoundError,
    WatermarkStorageError,
    list_registered_watermarks,
    register_watermark,
)
from gen_automation.storage.base import ObjectStore

router = APIRouter(tags=["derivatives"])
IdempotencyKey = Annotated[
    str,
    Header(
        alias="Idempotency-Key",
        min_length=8,
        max_length=200,
        description="Stable key for this exact derivative command",
    ),
]


class _StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PrepareDerivativesRequest(_StrictRequest):
    watermark_asset_id: UUID | None = None
    watermark_position: WatermarkPosition = WatermarkPosition.BOTTOM_RIGHT
    watermark_positions_by_asset_id: dict[UUID, WatermarkPosition] = Field(
        default_factory=dict,
        max_length=4,
    )
    max_attempts: int = Field(default=3, ge=1, le=10)


class DerivativePlanResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    review_task_id: UUID
    recipe_id: UUID
    release_version_id: UUID
    job_ids: tuple[UUID, ...]
    jobs_created: int
    total_jobs: int
    replayed: bool


class WatermarkResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    asset_id: UUID
    display_name: str
    sha256: str
    storage_backend: str
    storage_bucket: str
    object_key: str
    object_version_id: str
    width: int
    height: int
    byte_size: int
    registered_at: datetime
    replayed: bool


@router.post(
    "/watermarks",
    response_model=WatermarkResponse,
    status_code=status.HTTP_201_CREATED,
)
async def post_watermark(
    request: Request,
    release_id: Annotated[UUID, Form()],
    display_name: Annotated[str, Form(min_length=1, max_length=100)],
    file: Annotated[UploadFile, File()],
    session: Session,
    principal: PublicationMutationOwner,
    idempotency_key: IdempotencyKey,
    response: Response,
) -> WatermarkResponse:
    store = _object_store(request)
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="private object storage is unavailable",
        )
    if file.content_type != "image/png":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="watermark must be uploaded as image/png",
        )
    payload = await file.read(MAX_WATERMARK_BYTES + 1)
    await file.close()
    try:
        result = await register_watermark(
            session,
            store,
            release_id=release_id,
            display_name=display_name,
            png_bytes=payload,
            registered_by_user_id=principal.user_id,
            idempotency_key=idempotency_key,
        )
    except (
        WatermarkInputError,
        WatermarkNotFoundError,
        WatermarkConflictError,
        WatermarkStorageError,
    ) as error:
        raise _watermark_http_error(error) from error
    response.headers["Idempotency-Replayed"] = str(result.replayed).lower()
    if result.replayed:
        response.status_code = status.HTTP_200_OK
    return WatermarkResponse.model_validate(result)


@router.get("/watermarks", response_model=tuple[WatermarkResponse, ...])
async def get_watermarks(
    session: Session,
    _principal: PublicationReader,
) -> tuple[WatermarkResponse, ...]:
    results = await list_registered_watermarks(session)
    return tuple(WatermarkResponse.model_validate(result) for result in results)


@router.post(
    "/review-tasks/{review_task_id}:prepare-derivatives",
    response_model=DerivativePlanResponse,
    status_code=status.HTTP_201_CREATED,
)
async def post_review_derivative_plan(
    review_task_id: UUID,
    command: PrepareDerivativesRequest,
    session: Session,
    principal: ReviewPrincipal,
    idempotency_key: IdempotencyKey,
    response: Response,
) -> DerivativePlanResponse:
    try:
        result = await prepare_completed_review_derivatives(
            session,
            review_task_id=review_task_id,
            actor_user_id=principal.user_id,
            idempotency_key=idempotency_key,
            watermark_asset_id=command.watermark_asset_id,
            watermark_position=command.watermark_position,
            watermark_positions_by_asset_id=command.watermark_positions_by_asset_id,
            max_attempts=command.max_attempts,
        )
    except (
        DerivativePipelineInputError,
        DerivativePipelineNotFoundError,
        DerivativePipelineConflictError,
    ) as error:
        raise _derivative_http_error(error) from error
    response.headers["Idempotency-Replayed"] = str(result.replayed).lower()
    if result.replayed:
        response.status_code = status.HTTP_200_OK
    return DerivativePlanResponse.model_validate(result)


def _object_store(request: Request) -> ObjectStore | None:
    return getattr(request.app.state, "object_store", None)


def _watermark_http_error(error: Exception) -> HTTPException:
    if isinstance(error, WatermarkInputError):
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="watermark registration is invalid",
        )
    if isinstance(error, WatermarkNotFoundError):
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="watermark registration resource was not found",
        )
    if isinstance(error, WatermarkStorageError):
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="watermark storage is unavailable",
        )
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="watermark registration conflicts with current state",
    )


def _derivative_http_error(error: Exception) -> HTTPException:
    if isinstance(error, DerivativePipelineInputError):
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="derivative preparation is invalid",
        )
    if isinstance(error, DerivativePipelineNotFoundError):
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="derivative preparation resource was not found",
        )
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="derivative preparation conflicts with current state",
    )
