from pathlib import Path
from typing import cast

from fastapi.testclient import TestClient

from gen_automation.app import create_app
from gen_automation.config import Environment, Settings
from gen_automation.integrations.danbooru import DanbooruTagSuggestion
from gen_automation.services.danbooru_tags import (
    DanbooruAutocompleteResult,
    DanbooruCacheStatus,
    DanbooruTagAutocompleteService,
)


class _StubService:
    def __init__(self, result: DanbooruAutocompleteResult) -> None:
        self.result = result
        self.calls: list[tuple[str, int]] = []

    async def autocomplete(self, query: str, *, limit: int) -> DanbooruAutocompleteResult:
        self.calls.append((query, limit))
        return self.result


def test_authenticated_endpoint_returns_bounded_typed_suggestions(client: TestClient) -> None:
    result = DanbooruAutocompleteResult(
        query="tsu",
        available=True,
        cache_status=DanbooruCacheStatus.HIT,
        suggestions=(
            DanbooruTagSuggestion(
                name="tsunade_(naruto)",
                category=4,
                category_label="character",
                post_count=9_001,
            ),
        ),
    )
    service = _StubService(result)
    client.app.state.danbooru_tag_autocomplete_service = cast(
        DanbooruTagAutocompleteService,
        service,
    )

    response = client.get("/api/v1/danbooru-tags/autocomplete", params={"q": "tsu"})

    assert response.status_code == 200
    assert response.json() == {
        "schema_version": 1,
        "query": "tsu",
        "available": True,
        "cache_status": "hit",
        "suggestions": [
            {
                "name": "tsunade_(naruto)",
                "category": 4,
                "category_label": "character",
                "post_count": 9_001,
            }
        ],
    }
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert service.calls == [("tsu", 12)]


def test_endpoint_bounds_query_and_result_limit(client: TestClient) -> None:
    assert client.get("/api/v1/danbooru-tags/autocomplete", params={"q": "x"}).status_code == 422
    assert (
        client.get("/api/v1/danbooru-tags/autocomplete", params={"q": "tag,other"}).status_code
        == 422
    )
    assert client.get("/api/v1/danbooru-tags/autocomplete", params={"q": "  "}).status_code == 422
    assert (
        client.get(
            "/api/v1/danbooru-tags/autocomplete",
            params={"q": "face", "limit": 21},
        ).status_code
        == 422
    )


def test_endpoint_requires_authentication_or_explicit_local_bypass(tmp_path: Path) -> None:
    settings = Settings(
        environment=Environment.TEST,
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'tags-auth.db').as_posix()}",
        auto_create_schema=True,
    )
    with TestClient(create_app(settings)) as client:
        response = client.get(
            "/api/v1/danbooru-tags/autocomplete",
            params={"q": "face"},
        )

    assert response.status_code == 503
    assert response.json() == {"detail": "authentication is required"}


def test_endpoint_reports_missing_runtime_service_without_leaking_details(
    client: TestClient,
) -> None:
    client.app.state.danbooru_tag_autocomplete_service = None

    response = client.get(
        "/api/v1/danbooru-tags/autocomplete",
        params={"q": "face"},
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "Danbooru tag autocomplete is unavailable"}
