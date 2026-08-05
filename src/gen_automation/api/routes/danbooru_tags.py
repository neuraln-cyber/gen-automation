from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field

from gen_automation.api.security import ReleaseReader
from gen_automation.services.danbooru_tags import (
    DanbooruCacheStatus,
    DanbooruTagAutocompleteService,
)

router = APIRouter(prefix="/danbooru-tags", tags=["danbooru-tags"])

DanbooruQuery = Annotated[
    str,
    Query(
        min_length=2,
        max_length=100,
        pattern=r"^[^,\x00-\x1f\x7f]{2,100}$",
    ),
]
DanbooruLimit = Annotated[int, Query(ge=1, le=20)]


class DanbooruTagSuggestionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: str = Field(min_length=1, max_length=255)
    category: int = Field(ge=0, le=5)
    category_label: str = Field(min_length=1, max_length=20)
    post_count: int = Field(gt=0)


class DanbooruAutocompleteResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[1] = 1
    query: str = Field(min_length=2, max_length=100)
    available: bool
    cache_status: DanbooruCacheStatus
    suggestions: list[DanbooruTagSuggestionResponse] = Field(max_length=20)


@router.get("/autocomplete", response_model=DanbooruAutocompleteResponse)
async def autocomplete_danbooru_tags(
    request: Request,
    _principal: ReleaseReader,
    q: DanbooruQuery,
    limit: DanbooruLimit = 12,
) -> DanbooruAutocompleteResponse:
    service: DanbooruTagAutocompleteService | None = (
        request.app.state.danbooru_tag_autocomplete_service
    )
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Danbooru tag autocomplete is unavailable",
        )
    try:
        result = await service.autocomplete(q, limit=limit)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Danbooru autocomplete query is invalid",
        ) from error
    return DanbooruAutocompleteResponse(
        query=result.query,
        available=result.available,
        cache_status=result.cache_status,
        suggestions=[
            DanbooruTagSuggestionResponse(
                name=suggestion.name,
                category=suggestion.category,
                category_label=suggestion.category_label,
                post_count=suggestion.post_count,
            )
            for suggestion in result.suggestions
        ],
    )
