from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from gen_automation.api.security import ReleaseManager, ReleaseReader
from gen_automation.db.session import get_session
from gen_automation.schemas import (
    GenerationPlanRead,
    ProjectCreate,
    ProjectRead,
    ReleaseCreate,
    ReleaseRead,
)
from gen_automation.services.generation import (
    GenerationPlanConflictError,
    GenerationPlanNotFoundError,
    approve_and_expand_generation_plan,
)
from gen_automation.services.releases import (
    ConflictError,
    NotFoundError,
    create_project,
    create_release,
    get_release,
)

router = APIRouter(tags=["releases"])
Session = Annotated[AsyncSession, Depends(get_session)]
IdempotencyKey = Annotated[
    str,
    Header(
        alias="Idempotency-Key",
        min_length=8,
        max_length=200,
        description="Stable unique key for this logical command",
    ),
]


@router.post("/projects", response_model=ProjectRead, status_code=status.HTTP_201_CREATED)
async def post_project(
    command: ProjectCreate,
    request: Request,
    session: Session,
    principal: ReleaseManager,
) -> ProjectRead:
    try:
        return await create_project(
            session,
            command,
            actor=str(principal.user_id),
            correlation_id=getattr(request.state, "request_id", None),
        )
    except IntegrityError as error:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="project slug already exists",
        ) from error


@router.post(
    "/projects/{project_id}/releases",
    response_model=ReleaseRead,
    status_code=status.HTTP_201_CREATED,
)
async def post_release(
    project_id: UUID,
    command: ReleaseCreate,
    session: Session,
    idempotency_key: IdempotencyKey,
    response: Response,
    principal: ReleaseManager,
) -> ReleaseRead:
    try:
        result = await create_release(
            session,
            project_id=project_id,
            command=command,
            idempotency_key=idempotency_key,
            actor=str(principal.user_id),
        )
    except NotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except ConflictError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    except IntegrityError as error:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="release conflicts with existing state",
        ) from error

    response.headers["Idempotency-Replayed"] = str(result.replayed).lower()
    return result.response


@router.get("/releases/{release_id}", response_model=ReleaseRead)
async def read_release(
    release_id: UUID,
    session: Session,
    _principal: ReleaseReader,
) -> ReleaseRead:
    try:
        return await get_release(session, release_id)
    except NotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error


@router.post(
    "/releases/{release_id}/generation-plan:approve",
    response_model=GenerationPlanRead,
)
async def approve_generation_plan(
    release_id: UUID,
    session: Session,
    idempotency_key: IdempotencyKey,
    response: Response,
    principal: ReleaseManager,
) -> GenerationPlanRead:
    try:
        result = await approve_and_expand_generation_plan(
            session,
            release_id=release_id,
            idempotency_key=idempotency_key,
            actor=str(principal.user_id),
        )
    except GenerationPlanNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except GenerationPlanConflictError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    except IntegrityError as error:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="generation plan conflicts with existing state",
        ) from error

    response.headers["Idempotency-Replayed"] = str(result.replayed).lower()
    return result.response
