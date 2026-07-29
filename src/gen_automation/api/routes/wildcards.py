from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from gen_automation.api.security import ReleaseManager, ReleaseReader
from gen_automation.db.session import get_session
from gen_automation.domain.wildcards import (
    WildcardAppend,
    WildcardCreate,
    WildcardRead,
    WildcardReplace,
)
from gen_automation.services.wildcards import (
    WildcardConflictError,
    WildcardInputError,
    WildcardNotFoundError,
    append_wildcard_entries,
    create_wildcard_library,
    get_wildcard_library,
    list_wildcard_libraries,
    replace_wildcard_entries,
)

router = APIRouter(prefix="/wildcards", tags=["wildcards"])
Session = Annotated[AsyncSession, Depends(get_session)]
WildcardPath = Annotated[
    str,
    Path(
        min_length=1,
        max_length=80,
        pattern=r"^[a-z0-9]+(?:[._/-][a-z0-9]+)*$",
    ),
]


@router.get("", response_model=list[WildcardRead])
async def list_wildcards(
    session: Session,
    _principal: ReleaseReader,
) -> list[WildcardRead]:
    return list(await list_wildcard_libraries(session))


@router.get("/{name:path}", response_model=WildcardRead)
async def read_wildcard(
    name: WildcardPath,
    session: Session,
    _principal: ReleaseReader,
) -> WildcardRead:
    try:
        return await get_wildcard_library(session, name=name)
    except WildcardNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error


@router.post("", response_model=WildcardRead, status_code=status.HTTP_201_CREATED)
async def post_wildcard(
    command: WildcardCreate,
    session: Session,
    principal: ReleaseManager,
) -> WildcardRead:
    try:
        return await create_wildcard_library(
            session,
            command=command,
            actor=str(principal.user_id),
        )
    except WildcardConflictError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    except IntegrityError as error:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="wildcard library conflicts with existing state",
        ) from error


@router.put("/{name:path}", response_model=WildcardRead)
async def put_wildcard(
    name: WildcardPath,
    command: WildcardReplace,
    session: Session,
    principal: ReleaseManager,
) -> WildcardRead:
    try:
        return await replace_wildcard_entries(
            session,
            name=name,
            command=command,
            actor=str(principal.user_id),
        )
    except WildcardNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except WildcardInputError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error
    except WildcardConflictError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    except IntegrityError as error:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="wildcard library conflicts with existing state",
        ) from error


@router.post("/{name:path}/entries", response_model=WildcardRead)
async def post_wildcard_entries(
    name: WildcardPath,
    command: WildcardAppend,
    session: Session,
    principal: ReleaseManager,
) -> WildcardRead:
    try:
        return await append_wildcard_entries(
            session,
            name=name,
            command=command,
            actor=str(principal.user_id),
        )
    except WildcardNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except WildcardInputError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error
    except WildcardConflictError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    except IntegrityError as error:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="wildcard library conflicts with existing state",
        ) from error
