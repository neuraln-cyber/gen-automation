from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from gen_automation.api.security import (
    ClientAddressResolutionError,
    resolve_client_ip,
)
from gen_automation.app import create_app
from gen_automation.config import Environment, Settings

ROOT = Path(__file__).resolve().parents[1]


def _request(
    settings: Settings,
    *,
    direct_host: str | None,
    forwarded_for: tuple[str, ...] = (),
) -> Request:
    app = FastAPI()
    app.state.settings = settings
    headers = [(b"x-forwarded-for", value.encode("ascii")) for value in forwarded_for]
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/v1/auth/login",
        "raw_path": b"/api/v1/auth/login",
        "query_string": b"",
        "headers": headers,
        "client": (direct_host, 50000) if direct_host is not None else None,
        "server": ("127.0.0.1", 8000),
        "app": app,
    }
    return Request(scope)


def test_untrusted_direct_peer_cannot_influence_client_ip_with_forwarding_header() -> None:
    settings = Settings(
        environment=Environment.TEST,
        trusted_proxy_cidrs=("10.0.0.0/8",),
    )
    request = _request(
        settings,
        direct_host="192.0.2.20",
        forwarded_for=("malformed, attacker-controlled",),
    )

    assert resolve_client_ip(request) == "192.0.2.20"


def test_trusted_proxy_chain_is_walked_safely_from_right_to_left() -> None:
    settings = Settings(
        environment=Environment.TEST,
        trusted_proxy_cidrs=("10.0.0.0/8", "2001:db8:1::/48"),
    )
    ipv4_request = _request(
        settings,
        direct_host="10.1.0.10",
        forwarded_for=(
            "192.0.2.99, 198.51.100.40",
            "10.2.0.20, 10.3.0.30",
        ),
    )
    ipv6_request = _request(
        settings,
        direct_host="2001:db8:1::10",
        forwarded_for=("2001:db8:ffff::99, 2001:db8:1::20",),
    )

    assert resolve_client_ip(ipv4_request) == "198.51.100.40"
    assert resolve_client_ip(ipv6_request) == "2001:db8:ffff::99"


def test_all_trusted_forwarding_hops_return_the_leftmost_address() -> None:
    settings = Settings(
        environment=Environment.TEST,
        trusted_proxy_cidrs=("10.0.0.0/8",),
    )
    request = _request(
        settings,
        direct_host="10.1.0.10",
        forwarded_for=("10.4.0.40, 10.3.0.30, 10.2.0.20",),
    )

    assert resolve_client_ip(request) == "10.4.0.40"


def test_trusted_proxy_without_forwarded_chain_fails_closed() -> None:
    request = _request(
        Settings(
            environment=Environment.TEST,
            trusted_proxy_cidrs=("10.0.0.0/8",),
        ),
        direct_host="10.1.0.10",
    )

    with pytest.raises(ClientAddressResolutionError, match="required"):
        resolve_client_ip(request)


@pytest.mark.parametrize(
    "forwarded_for",
    [
        ("",),
        ("unknown",),
        ("192.0.2.10,,10.2.0.20",),
        ("192.0.2.10:443",),
        ("[2001:db8::1]",),
        ("fe80::1%eth0",),
        (",".join("192.0.2.1" for _ in range(33)),),
        ("1" * 4097,),
    ],
)
def test_trusted_proxy_malformed_or_unbounded_chain_fails_closed(
    forwarded_for: tuple[str, ...],
) -> None:
    settings = Settings(
        environment=Environment.TEST,
        trusted_proxy_cidrs=("10.0.0.0/8",),
    )
    request = _request(
        settings,
        direct_host="10.1.0.10",
        forwarded_for=forwarded_for,
    )

    with pytest.raises(ClientAddressResolutionError):
        resolve_client_ip(request)


@pytest.mark.parametrize("direct_host", [None, "testclient", "192.0.2.1:443", "fe80::1%eth0"])
def test_missing_or_non_ip_socket_peer_fails_closed(direct_host: str | None) -> None:
    request = _request(
        Settings(environment=Environment.TEST),
        direct_host=direct_host,
    )

    with pytest.raises(ClientAddressResolutionError):
        resolve_client_ip(request)


def test_synthetic_owner_requires_explicit_local_test_bypass(tmp_path: Path) -> None:
    disabled = Settings(
        environment=Environment.TEST,
        database_url=(f"sqlite+aiosqlite:///{(tmp_path / 'disabled-bypass.db').as_posix()}"),
        auto_create_schema=True,
    )
    enabled = Settings(
        environment=Environment.TEST,
        database_url=(f"sqlite+aiosqlite:///{(tmp_path / 'enabled-bypass.db').as_posix()}"),
        auto_create_schema=True,
        auth_development_bypass_enabled=True,
    )

    with TestClient(create_app(disabled)) as client:
        response = client.post(
            "/api/v1/projects",
            json={"slug": "blocked", "name": "Blocked"},
        )
        assert response.status_code == 503

    with TestClient(create_app(enabled)) as client:
        response = client.post(
            "/api/v1/projects",
            json={"slug": "local", "name": "Local"},
        )
        assert response.status_code == 201


def test_compose_exposes_only_loopback_and_makes_bypass_explicit() -> None:
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")

    assert 'GEN_AUTOMATION_AUTH_DEVELOPMENT_BYPASS_ENABLED: "true"' in compose
    assert '- "127.0.0.1:8000:8000"' in compose
    assert '- "8000:8000"' not in compose
    assert "--no-proxy-headers" in compose
