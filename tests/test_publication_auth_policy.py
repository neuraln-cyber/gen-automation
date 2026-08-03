from datetime import UTC, datetime
from typing import Any, cast, get_type_hints
from uuid import uuid4

import pytest
from fastapi import FastAPI, Response
from starlette.requests import Request

from gen_automation.api.routes import derivatives, publications
from gen_automation.api.security import (
    PublicationMutationOwner,
    PublicationMutationPrincipal,
    PublicationPrincipal,
)
from gen_automation.domain.enums import AdminRole
from gen_automation.domain.publication import PublicationGuardChange
from gen_automation.services.authentication import AuthenticatedPrincipal
from gen_automation.services.publication import PublicationGuardResult


def _owner() -> AuthenticatedPrincipal:
    now = datetime.now(UTC)
    return AuthenticatedPrincipal(
        session_id=uuid4(),
        user_id=uuid4(),
        username="owner",
        display_name="Owner",
        role=AdminRole.OWNER,
        csrf_sha256="a" * 64,
        expires_at=now,
        idle_expires_at=now,
        reauthenticated_at=now,
        mfa_verified_at=now,
    )


def test_publication_routes_apply_recent_auth_only_to_external_effects() -> None:
    mutation_routes = (
        publications.post_publication_intent,
        publications.post_publication_revocation,
        publications.post_patreon_package_download,
    )
    recent_routes = (
        publications.post_publication_approval,
        publications.post_publication_confirm_present,
        publications.post_publication_confirm_absent,
    )

    for route in mutation_routes:
        assert get_type_hints(route, include_extras=True)["principal"] == (
            PublicationMutationPrincipal
        )
    for route in recent_routes:
        assert get_type_hints(route, include_extras=True)["principal"] == (
            PublicationPrincipal
        )
    assert get_type_hints(
        publications.post_global_publication_guard,
        include_extras=True,
    )["principal"] == PublicationMutationOwner
    assert get_type_hints(derivatives.post_watermark, include_extras=True)[
        "principal"
    ] == PublicationMutationOwner


@pytest.mark.asyncio
@pytest.mark.parametrize(("enabled", "recent_auth_calls"), [(False, 0), (True, 1)])
async def test_publication_guard_requires_recent_auth_only_when_enabling(
    enabled: bool,
    recent_auth_calls: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal = _owner()
    calls = 0

    async def require_recent(
        _request: Request,
        candidate: AuthenticatedPrincipal,
    ) -> AuthenticatedPrincipal:
        nonlocal calls
        calls += 1
        return candidate

    async def change_guard(
        _session: object,
        **kwargs: object,
    ) -> PublicationGuardResult:
        return PublicationGuardResult(
            enabled=bool(kwargs["enabled"]),
            epoch=2,
            lock_version=2,
            changed_at=datetime.now(UTC),
        )

    monkeypatch.setattr(publications, "require_recent_principal", require_recent)
    monkeypatch.setattr(publications, "set_publication_guard", change_guard)
    app = FastAPI()
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/publication-guard",
            "headers": [],
            "app": app,
        }
    )

    result = await publications.post_global_publication_guard(
        PublicationGuardChange(
            enabled=enabled,
            expected_epoch=1,
            expected_lock_version=1,
            reason="Focused authentication policy test",
        ),
        request,
        cast(Any, object()),
        principal,
        "publication-guard-policy-test",
        Response(),
    )

    assert result.enabled is enabled
    assert calls == recent_auth_calls

