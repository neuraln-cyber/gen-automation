from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import httpx2
import pytest

from gen_automation.i2v_worker.comfy import ComfyClient, ComfyMemoryError, ComfyUnhealthyError


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
async def test_readiness_requires_the_exact_face_fidelity_nodes() -> None:
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


@pytest.mark.asyncio
async def test_dead_prompt_thread_with_live_http_does_not_poll_forever(tmp_path, monkeypatch):
    monkeypatch.setattr("gen_automation.i2v_worker.comfy._HEALTH_INTERVAL_SECONDS", 0)
    probes = 0

    def handler(request):
        nonlocal probes
        if request.url.path == "/prompt":
            return httpx2.Response(200, json={"prompt_id": "stuck"})
        if request.url.path == "/system_stats":
            probes += 1
            return httpx2.Response(500, text="private CUDA details")
        assert request.url.path == "/history/stuck"
        return httpx2.Response(200, json={})

    client = await _client(handler)
    try:
        with pytest.raises(ComfyUnhealthyError, match="health repeatedly failed") as error:
            await client.execute({}, tmp_path)
        assert probes == 3
        assert "private" not in str(error.value)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_slow_sampling_and_transient_health_failures_can_still_complete(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("gen_automation.i2v_worker.comfy._HEALTH_INTERVAL_SECONDS", 0)
    health = iter([500, 500, 200, 500, 200, 200])
    polls = 0
    (tmp_path / "video.mp4").write_bytes(b"registered-only-after-completion")

    def handler(request):
        nonlocal polls
        if request.url.path == "/prompt":
            return httpx2.Response(200, json={"prompt_id": "slow"})
        if request.url.path == "/system_stats":
            return httpx2.Response(next(health))
        polls += 1
        if polls <= 6:
            return httpx2.Response(200, json={})
        return httpx2.Response(
            200,
            json={
                "slow": {
                    "status": {"completed": True},
                    "outputs": {"14": {"images": [{"filename": "video.mp4", "type": "output"}]}},
                }
            },
        )

    client = await _client(handler)
    try:
        assert await client.execute({}, tmp_path) == (tmp_path / "video.mp4",)
        assert polls == 7
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,detail",
    [
        ("torch.cuda.OutOfMemoryError", "private tensor"),
        ("torch.AcceleratorError", "CUDA error: out of memory; private details"),
    ],
)
async def test_memory_error_history_is_classified_without_leaking_details(tmp_path, kind, detail):
    def handler(request):
        if request.url.path == "/prompt":
            return httpx2.Response(200, json={"prompt_id": "oom"})
        return httpx2.Response(
            200,
            json={
                "oom": {
                    "status": {
                        "status_str": "error",
                        "messages": [
                            [
                                "execution_error",
                                {
                                    "exception_type": kind,
                                    "exception_message": detail,
                                },
                            ]
                        ],
                    }
                }
            },
        )

    client = await _client(handler)
    try:
        with pytest.raises(ComfyMemoryError, match="GPU memory exhausted") as error:
            await client.execute({}, Path(tmp_path))
        assert "private" not in str(error.value)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_unavailable_history_requests_recover_instead_of_leaving_worker_ready(tmp_path):
    def handler(request):
        if request.url.path == "/prompt":
            return httpx2.Response(200, json={"prompt_id": "lost"})
        return httpx2.Response(500)

    client = await _client(handler)
    try:
        with pytest.raises(ComfyUnhealthyError, match="history is unavailable"):
            await client.execute({}, tmp_path)
    finally:
        await client.close()
