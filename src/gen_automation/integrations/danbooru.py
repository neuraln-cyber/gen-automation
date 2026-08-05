from __future__ import annotations

import json
import re
from dataclasses import dataclass

import httpx2
from pydantic import BaseModel, ConfigDict, Field, ValidationError

DANBOORU_AUTOCOMPLETE_URL = "https://danbooru.donmai.us/autocomplete.json"
DANBOORU_AUTOCOMPLETE_LIMIT = 20
DANBOORU_MAX_RESPONSE_BYTES = 64 * 1024
DANBOORU_REQUEST_TIMEOUT = httpx2.Timeout(
    connect=3.0,
    read=5.0,
    write=5.0,
    pool=3.0,
)

_MAX_UPSTREAM_ITEMS = 100
_TAG_NAME_PATTERN = re.compile(r"^[^\s,\x00-\x1f\x7f]{1,255}$")
_CATEGORY_LABELS = {
    0: "general",
    1: "artist",
    3: "copyright",
    4: "character",
    5: "meta",
}


class DanbooruAutocompleteError(Exception):
    """Base error for the bounded Danbooru autocomplete integration."""


class DanbooruAutocompleteUnavailableError(DanbooruAutocompleteError):
    """The fixed upstream service could not return a usable response."""


class DanbooruAutocompleteProtocolError(DanbooruAutocompleteError):
    """The fixed upstream service returned data outside the expected contract."""


@dataclass(frozen=True, slots=True)
class DanbooruTagSuggestion:
    name: str
    category: int
    category_label: str
    post_count: int


class _UpstreamTag(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    name: str = Field(min_length=1, max_length=255)
    category: int
    post_count: int = Field(ge=0)
    is_deprecated: bool


class _UpstreamSuggestion(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    type: str = Field(min_length=1, max_length=100)
    value: str = Field(min_length=1, max_length=255)
    category: int
    post_count: int = Field(ge=0)
    tag: _UpstreamTag | None = None


def _content_type(response: httpx2.Response) -> str:
    value: object = response.headers.get("content-type", "")
    if not isinstance(value, str):
        return ""
    return value.split(";", 1)[0].strip().lower()


def _parse_suggestions(content: bytes) -> tuple[DanbooruTagSuggestion, ...]:
    try:
        payload: object = json.loads(content)
    except ValueError as error:
        raise DanbooruAutocompleteProtocolError(
            "Danbooru autocomplete returned malformed JSON"
        ) from error
    if not isinstance(payload, list) or len(payload) > _MAX_UPSTREAM_ITEMS:
        raise DanbooruAutocompleteProtocolError(
            "Danbooru autocomplete returned an invalid result collection"
        )

    suggestions: list[DanbooruTagSuggestion] = []
    seen: set[str] = set()
    for raw in payload:
        try:
            item = _UpstreamSuggestion.model_validate(raw, strict=True)
        except ValidationError:
            continue
        tag = item.tag
        if (
            item.type != "tag-word"
            or tag is None
            or tag.is_deprecated
            or tag.post_count <= 0
            or tag.category not in _CATEGORY_LABELS
            or item.category != tag.category
            or item.post_count != tag.post_count
            or item.value != tag.name
            or _TAG_NAME_PATTERN.fullmatch(tag.name) is None
            or tag.name in seen
        ):
            continue
        seen.add(tag.name)
        suggestions.append(
            DanbooruTagSuggestion(
                name=tag.name,
                category=tag.category,
                category_label=_CATEGORY_LABELS[tag.category],
                post_count=tag.post_count,
            )
        )
        if len(suggestions) >= DANBOORU_AUTOCOMPLETE_LIMIT:
            break
    return tuple(suggestions)


class DanbooruAutocompleteClient:
    """Typed client for Danbooru's fixed, anonymous autocomplete endpoint.

    The caller owns the HTTP client and its lifecycle. The endpoint cannot be
    configured, which prevents user-controlled input from becoming an SSRF URL.
    """

    def __init__(self, *, http_client: httpx2.AsyncClient) -> None:
        self._http_client = http_client

    async def autocomplete(self, query: str) -> tuple[DanbooruTagSuggestion, ...]:
        if not query or len(query) > 100:
            raise ValueError("Danbooru autocomplete query is invalid")
        try:
            response = await self._http_client.get(
                DANBOORU_AUTOCOMPLETE_URL,
                params={
                    "search[query]": query,
                    "search[type]": "tag",
                    "version": 1,
                    "limit": DANBOORU_AUTOCOMPLETE_LIMIT,
                },
                headers={
                    "Accept": "application/json",
                    "User-Agent": "gen-automation/0.1 (private tag autocomplete)",
                },
                timeout=DANBOORU_REQUEST_TIMEOUT,
            )
        except (httpx2.TimeoutException, httpx2.TransportError) as error:
            raise DanbooruAutocompleteUnavailableError(
                "Danbooru autocomplete request failed"
            ) from error
        if response.status_code != 200:
            raise DanbooruAutocompleteUnavailableError(
                f"Danbooru autocomplete returned HTTP {response.status_code}"
            )
        if (
            _content_type(response) != "application/json"
            or len(response.content) > DANBOORU_MAX_RESPONSE_BYTES
        ):
            raise DanbooruAutocompleteProtocolError(
                "Danbooru autocomplete response envelope is invalid"
            )
        return _parse_suggestions(response.content)
