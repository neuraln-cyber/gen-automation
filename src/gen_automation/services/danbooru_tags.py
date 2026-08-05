from __future__ import annotations

import re
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import StrEnum
from time import monotonic

from gen_automation.integrations.danbooru import (
    DanbooruAutocompleteClient,
    DanbooruAutocompleteError,
    DanbooruTagSuggestion,
)

DANBOORU_CACHE_FRESH_SECONDS = 10 * 60
# Mirrors Danbooru's public `max-age=600, stale-while-revalidate=86400` response.
DANBOORU_CACHE_STALE_SECONDS = 24 * 60 * 60
DANBOORU_CACHE_FAILURE_SECONDS = 15
DANBOORU_CACHE_MAX_ENTRIES = 512

_QUERY_PATTERN = re.compile(r"^[^,\x00-\x1f\x7f]{2,100}$")


class DanbooruCacheStatus(StrEnum):
    MISS = "miss"
    HIT = "hit"
    STALE = "stale"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class DanbooruAutocompleteResult:
    query: str
    available: bool
    cache_status: DanbooruCacheStatus
    suggestions: tuple[DanbooruTagSuggestion, ...]


@dataclass(frozen=True, slots=True)
class _CacheEntry:
    suggestions: tuple[DanbooruTagSuggestion, ...]
    available: bool
    cache_status: DanbooruCacheStatus
    fresh_until: float
    stale_until: float


def normalize_danbooru_query(query: str) -> str:
    if "," in query or any(not character.isprintable() for character in query):
        raise ValueError("Danbooru autocomplete query is invalid")
    normalized = " ".join(query.strip().split()).casefold()
    if _QUERY_PATTERN.fullmatch(normalized) is None:
        raise ValueError("Danbooru autocomplete query is invalid")
    return normalized


class DanbooruTagAutocompleteService:
    """A bounded process-local LRU cache around anonymous tag lookups."""

    def __init__(
        self,
        *,
        client: DanbooruAutocompleteClient,
        clock: Callable[[], float] = monotonic,
        max_entries: int = DANBOORU_CACHE_MAX_ENTRIES,
    ) -> None:
        if max_entries < 1:
            raise ValueError("Danbooru autocomplete cache size must be positive")
        self._client = client
        self._clock = clock
        self._max_entries = max_entries
        self._cache: OrderedDict[str, _CacheEntry] = OrderedDict()

    async def autocomplete(self, query: str, *, limit: int) -> DanbooruAutocompleteResult:
        normalized = normalize_danbooru_query(query)
        if not 1 <= limit <= 20:
            raise ValueError("Danbooru autocomplete limit is invalid")
        now = self._clock()
        cached = self._cache.get(normalized)
        if cached is not None and now < cached.fresh_until:
            self._cache.move_to_end(normalized)
            return self._result(normalized, cached, limit=limit)

        try:
            suggestions = await self._client.autocomplete(normalized)
        except DanbooruAutocompleteError:
            now = self._clock()
            cached = self._cache.get(normalized)
            if cached is not None and cached.available and now < cached.stale_until:
                stale = replace(
                    cached,
                    cache_status=DanbooruCacheStatus.STALE,
                    fresh_until=min(
                        now + DANBOORU_CACHE_FAILURE_SECONDS,
                        cached.stale_until,
                    ),
                )
                self._store(normalized, stale)
                return self._result(normalized, stale, limit=limit)
            unavailable = _CacheEntry(
                suggestions=(),
                available=False,
                cache_status=DanbooruCacheStatus.UNAVAILABLE,
                fresh_until=now + DANBOORU_CACHE_FAILURE_SECONDS,
                stale_until=now + DANBOORU_CACHE_FAILURE_SECONDS,
            )
            self._store(normalized, unavailable)
            return self._result(normalized, unavailable, limit=limit)

        now = self._clock()
        fresh = _CacheEntry(
            suggestions=suggestions,
            available=True,
            cache_status=DanbooruCacheStatus.HIT,
            fresh_until=now + DANBOORU_CACHE_FRESH_SECONDS,
            stale_until=(now + DANBOORU_CACHE_FRESH_SECONDS + DANBOORU_CACHE_STALE_SECONDS),
        )
        self._store(normalized, fresh)
        return DanbooruAutocompleteResult(
            query=normalized,
            available=True,
            cache_status=DanbooruCacheStatus.MISS,
            suggestions=suggestions[:limit],
        )

    def _store(self, query: str, entry: _CacheEntry) -> None:
        self._cache[query] = entry
        self._cache.move_to_end(query)
        while len(self._cache) > self._max_entries:
            self._cache.popitem(last=False)

    @staticmethod
    def _result(
        query: str,
        entry: _CacheEntry,
        *,
        limit: int,
    ) -> DanbooruAutocompleteResult:
        return DanbooruAutocompleteResult(
            query=query,
            available=entry.available,
            cache_status=entry.cache_status,
            suggestions=entry.suggestions[:limit],
        )
