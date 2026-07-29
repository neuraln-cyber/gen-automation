"""Boundary for a future signed-in Patreon browser publisher.

Patreon post creation is deliberately not represented as a public-API call.
An implementation of this protocol owns a persistent browser profile, consumes
the already-verified handoff ZIP, and returns a bounded outcome for durable
reconciliation. The current runtime keeps using the manual package fallback.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol
from uuid import UUID


class PatreonDriverOutcome(StrEnum):
    PUBLISHED = "published"
    NEEDS_OPERATOR = "needs_operator"
    UNKNOWN = "unknown"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class PatreonDriverRequest:
    intent_id: UUID
    intent_digest: str
    package_id: UUID
    package_path: Path
    package_sha256: str
    browser_profile_reference: str


@dataclass(frozen=True, slots=True)
class PatreonDriverResult:
    outcome: PatreonDriverOutcome
    remote_identifier: str | None = None
    remote_url: str | None = None
    detail_code: str | None = None


class PatreonPublicationDriver(Protocol):
    """Publish through Patreon's official UI using an existing signed-in profile."""

    async def publish(self, request: PatreonDriverRequest) -> PatreonDriverResult: ...
