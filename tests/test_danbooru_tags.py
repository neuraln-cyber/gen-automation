from typing import cast

import httpx2
import pytest

from gen_automation.integrations.danbooru import (
    DANBOORU_AUTOCOMPLETE_URL,
    DANBOORU_MAX_RESPONSE_BYTES,
    DanbooruAutocompleteClient,
    DanbooruAutocompleteProtocolError,
    DanbooruAutocompleteUnavailableError,
    DanbooruTagSuggestion,
)
from gen_automation.services.danbooru_tags import (
    DANBOORU_CACHE_FRESH_SECONDS,
    DANBOORU_CACHE_STALE_SECONDS,
    DanbooruCacheStatus,
    DanbooruTagAutocompleteService,
    normalize_danbooru_query,
)


def _item(
    name: str,
    *,
    category: int = 0,
    post_count: int = 100,
    deprecated: bool = False,
) -> dict[str, object]:
    return {
        "type": "tag-word",
        "label": name.replace("_", " "),
        "value": name,
        "category": category,
        "post_count": post_count,
        "tag": {
            "id": 123,
            "name": name,
            "post_count": post_count,
            "category": category,
            "is_deprecated": deprecated,
            "words": name.split("_"),
        },
    }


@pytest.mark.asyncio
async def test_client_uses_fixed_anonymous_endpoint_and_filters_invalid_results() -> None:
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        valid = _item("rio_(blue_archive)", category=4, post_count=14_450)
        valid["antecedent"] = "tsukatsuki_rio"
        mismatched = _item("mismatched", category=0)
        mismatched["post_count"] = 999
        return httpx2.Response(
            200,
            headers={"Content-Type": "application/json; charset=utf-8"},
            json=[
                valid,
                _item("old_tag", deprecated=True),
                _item("empty_tag", post_count=0),
                _item("unknown_category", category=2),
                _item("contains whitespace"),
                mismatched,
                valid,
                {"type": "tag-word", "value": 123},
            ],
        )

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as http_client:
        client = DanbooruAutocompleteClient(http_client=http_client)
        suggestions = await client.autocomplete("tsu")

    assert suggestions == (
        DanbooruTagSuggestion(
            name="rio_(blue_archive)",
            category=4,
            category_label="character",
            post_count=14_450,
        ),
    )
    assert len(requests) == 1
    request = requests[0]
    assert f"{request.url.scheme}://{request.url.host}{request.url.path}" == (
        DANBOORU_AUTOCOMPLETE_URL
    )
    assert request.url.params["search[query]"] == "tsu"
    assert request.url.params["search[type]"] == "tag"
    assert request.url.params["version"] == "1"
    assert request.url.params["limit"] == "20"
    assert "authorization" not in request.headers
    assert "cookie" not in request.headers


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        httpx2.Response(503, json={"error": "unavailable"}),
        httpx2.Response(200, headers={"Content-Type": "text/html"}, text="not json"),
    ],
)
async def test_client_maps_unavailable_and_invalid_envelopes(
    response: httpx2.Response,
) -> None:
    async with httpx2.AsyncClient(
        transport=httpx2.MockTransport(lambda _request: response)
    ) as http_client:
        client = DanbooruAutocompleteClient(http_client=http_client)
        error_type = (
            DanbooruAutocompleteUnavailableError
            if response.status_code != 200
            else DanbooruAutocompleteProtocolError
        )
        with pytest.raises(error_type):
            await client.autocomplete("face")


@pytest.mark.asyncio
async def test_client_rejects_oversized_or_malformed_json() -> None:
    responses = iter(
        (
            httpx2.Response(
                200,
                headers={"Content-Type": "application/json"},
                content=b"[" + b" " * DANBOORU_MAX_RESPONSE_BYTES + b"]",
            ),
            httpx2.Response(
                200,
                headers={"Content-Type": "application/json"},
                content=b"not-json",
            ),
        )
    )
    async with httpx2.AsyncClient(
        transport=httpx2.MockTransport(lambda _request: next(responses))
    ) as http_client:
        client = DanbooruAutocompleteClient(http_client=http_client)
        with pytest.raises(DanbooruAutocompleteProtocolError):
            await client.autocomplete("face")
        with pytest.raises(DanbooruAutocompleteProtocolError):
            await client.autocomplete("face")


class _StubClient:
    def __init__(self, results: list[object]) -> None:
        self.results = results
        self.queries: list[str] = []

    async def autocomplete(self, query: str) -> tuple[DanbooruTagSuggestion, ...]:
        self.queries.append(query)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return cast(tuple[DanbooruTagSuggestion, ...], result)


@pytest.mark.asyncio
async def test_service_caches_limits_and_serves_stale_results_during_outage() -> None:
    now = [100.0]
    suggestions = tuple(
        DanbooruTagSuggestion(
            name=f"tag_{index}",
            category=0,
            category_label="general",
            post_count=100 - index,
        )
        for index in range(4)
    )
    upstream = _StubClient(
        [
            suggestions,
            DanbooruAutocompleteUnavailableError("offline"),
            DanbooruAutocompleteUnavailableError("offline"),
        ]
    )
    service = DanbooruTagAutocompleteService(
        client=cast(DanbooruAutocompleteClient, upstream),
        clock=lambda: now[0],
    )

    first = await service.autocomplete("  FACE detail  ", limit=3)
    cached = await service.autocomplete("face detail", limit=1)

    assert first.cache_status == DanbooruCacheStatus.MISS
    assert first.query == "face detail"
    assert first.suggestions == suggestions[:3]
    assert cached.cache_status == DanbooruCacheStatus.HIT
    assert cached.suggestions == suggestions[:1]
    assert upstream.queries == ["face detail"]

    now[0] += DANBOORU_CACHE_FRESH_SECONDS + 1
    stale = await service.autocomplete("face detail", limit=4)
    suppressed_retry = await service.autocomplete("face detail", limit=2)

    assert stale.available
    assert stale.cache_status == DanbooruCacheStatus.STALE
    assert stale.suggestions == suggestions
    assert suppressed_retry.cache_status == DanbooruCacheStatus.STALE
    assert upstream.queries == ["face detail", "face detail"]

    now[0] += DANBOORU_CACHE_STALE_SECONDS + DANBOORU_CACHE_FRESH_SECONDS
    unavailable = await service.autocomplete("face detail", limit=4)
    assert not unavailable.available
    assert unavailable.cache_status == DanbooruCacheStatus.UNAVAILABLE
    assert unavailable.suggestions == ()


@pytest.mark.asyncio
async def test_service_evicts_least_recently_used_query() -> None:
    suggestion = (
        DanbooruTagSuggestion(
            name="face",
            category=0,
            category_label="general",
            post_count=100,
        ),
    )
    upstream = _StubClient([suggestion, suggestion, suggestion, suggestion])
    service = DanbooruTagAutocompleteService(
        client=cast(DanbooruAutocompleteClient, upstream),
        max_entries=2,
    )

    await service.autocomplete("face", limit=1)
    await service.autocomplete("eyes", limit=1)
    await service.autocomplete("face", limit=1)
    await service.autocomplete("hands", limit=1)
    await service.autocomplete("eyes", limit=1)

    assert upstream.queries == ["face", "eyes", "hands", "eyes"]


@pytest.mark.parametrize(
    "query",
    ["", "x", "tag,other", "line\nbreak", "zero\u200bwidth", "x" * 101],
)
def test_query_normalization_rejects_unbounded_or_non_fragment_input(query: str) -> None:
    with pytest.raises(ValueError):
        normalize_danbooru_query(query)
