from __future__ import annotations

from collections.abc import Callable

import httpx2
import pytest

from gen_automation.i2v_worker.comfy import ComfyClient


async def _client(
    handler: Callable[[httpx2.Request], httpx2.Response],
) -> ComfyClient:
    client = ComfyClient(
        base_url="http://127.0.0.1:8188",
        request_timeout_seconds=5,
        network_attempts=1,
        poll_seconds=0.01,
    )
    await client.client.aclose()
    client.client = httpx2.AsyncClient(
        base_url="http://127.0.0.1:8188",
        transport=httpx2.MockTransport(handler),
        follow_redirects=False,
        trust_env=False,
    )
    return client


@pytest.mark.asyncio
async def test_readiness_requires_the_exact_allowlisted_nag_node() -> None:
    paths: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        paths.append(request.url.path)
        if request.url.path == "/system_stats":
            return httpx2.Response(200, json={"system": {}, "devices": []})
        if request.url.path == "/object_info/KSamplerWithNAG (Advanced)":
            return httpx2.Response(
                200,
                json={"KSamplerWithNAG (Advanced)": {"input": {}}},
            )
        raise AssertionError(request.url.path)

    client = await _client(handler)
    try:
        assert await client.ready()
    finally:
        await client.close()

    assert paths == [
        "/system_stats",
        "/object_info/KSamplerWithNAG (Advanced)",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "node_payload",
    [
        {},
        {"KSamplerAdvanced": {"input": {}}},
        {
            "KSamplerWithNAG (Advanced)": {"input": {}},
            "unexpected": {},
        },
    ],
)
async def test_readiness_fails_closed_for_missing_or_ambiguous_node_contract(
    node_payload: dict[str, object],
) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/system_stats":
            return httpx2.Response(200, json={})
        return httpx2.Response(200, json=node_payload)

    client = await _client(handler)
    try:
        assert not await client.ready()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_readiness_does_not_follow_node_endpoint_redirects() -> None:
    requested_hosts: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requested_hosts.append(request.url.host)
        if request.url.path == "/system_stats":
            return httpx2.Response(200, json={})
        return httpx2.Response(
            307,
            headers={"location": "http://169.254.169.254/latest/meta-data"},
        )

    client = await _client(handler)
    try:
        assert not await client.ready()
    finally:
        await client.close()

    assert requested_hosts == ["127.0.0.1", "127.0.0.1"]
