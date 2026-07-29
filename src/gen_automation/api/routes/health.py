from typing import Literal

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError

from gen_automation import __version__
from gen_automation.config import Settings
from gen_automation.controller.runtime import ControllerRuntime
from gen_automation.db.session import Database
from gen_automation.storage.base import ObjectStore, ObjectStoreError

router = APIRouter(prefix="/health", tags=["health"])


class HealthResponse(BaseModel):
    status: Literal["ok", "not_ready"]
    service: str
    version: str
    environment: str
    controller_runtime: Literal[
        "disabled",
        "starting",
        "healthy",
        "degraded",
        "stopping",
        "stopped",
    ]
    controller_failed_loops: list[str]


def _runtime_health(request: Request) -> tuple[str, list[str], bool, bool]:
    settings: Settings = request.app.state.settings
    runtime: ControllerRuntime | None = request.app.state.controller_runtime
    if not settings.background_runtime_enabled:
        return "disabled", [], True, True
    if runtime is None:
        return "stopped", [], False, False
    snapshot = runtime.snapshot()
    return (
        snapshot.status.value,
        list(snapshot.failed_loops),
        snapshot.ready,
        snapshot.live,
    )


@router.get("/live", response_model=HealthResponse)
async def live(request: Request) -> HealthResponse:
    settings: Settings = request.app.state.settings
    runtime_status, failed_loops, _, runtime_live = _runtime_health(request)
    if not runtime_live:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="background controller runtime is not live",
        )
    return HealthResponse(
        status="ok",
        service=settings.app_name,
        version=__version__,
        environment=settings.environment,
        controller_runtime=runtime_status,
        controller_failed_loops=failed_loops,
    )


@router.get("/ready", response_model=HealthResponse)
async def ready(request: Request) -> HealthResponse:
    settings: Settings = request.app.state.settings
    runtime_status, failed_loops, runtime_ready, _ = _runtime_health(request)
    if not runtime_ready:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="background controller runtime is not ready",
        )
    database: Database = request.app.state.database
    try:
        await database.ping()
    except SQLAlchemyError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="database is not ready",
        ) from error

    object_store: ObjectStore | None = request.app.state.object_store
    if settings.storage_enabled:
        if object_store is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="object storage is not configured",
            )
        try:
            await object_store.ping()
        except ObjectStoreError as error:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="object storage is not ready",
            ) from error

    return HealthResponse(
        status="ok",
        service=settings.app_name,
        version=__version__,
        environment=settings.environment,
        controller_runtime=runtime_status,
        controller_failed_loops=failed_loops,
    )
