from __future__ import annotations

import httpx2
import pytest

from gen_automation.integrations.runpod.client import RunPodClient
from gen_automation.integrations.runpod.errors import (
    RunPodProtocolError,
    RunPodTimeoutError,
)
from gen_automation.integrations.runpod.models import RunPodJobStatus


def _client(handler: httpx2.MockTransport) -> tuple[RunPodClient, httpx2.AsyncClient]:
    http_client = httpx2.AsyncClient(transport=handler)
    return RunPodClient(
        http_client=http_client,
        api_key="rp-test-secret",
        endpoint_id="abc123endpoint",
    ), http_client


@pytest.mark.asyncio
async def test_submit_binds_endpoint_and_long_job_policy_without_retry() -> None:
    requests: list[httpx2.Request] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(
            200,
            json={"id": "job_12345", "status": "IN_QUEUE"},
        )

    client, http_client = _client(httpx2.MockTransport(respond))
    result = await client.submit(
        input_payload={"schema": "i2v-runpod-input/v1", "job": {"schema": "i2v-job/v2"}},
        execution_timeout_ms=7_200_000,
        ttl_ms=86_400_000,
    )

    assert result.id == "job_12345"
    assert result.status is RunPodJobStatus.IN_QUEUE
    assert len(requests) == 1
    assert requests[0].url == "https://api.runpod.ai/v2/abc123endpoint/run"
    assert requests[0].headers["authorization"] == "Bearer rp-test-secret"
    assert requests[0].read().decode() == (
        '{"input":{"schema":"i2v-runpod-input/v1","job":{"schema":"i2v-job/v2"}},'
        '"policy":{"executionTimeout":7200000,"lowPriority":false,"ttl":86400000}}'
    )
    await http_client.aclose()


@pytest.mark.asyncio
async def test_status_parses_terminal_output_and_metrics() -> None:
    transport = httpx2.MockTransport(
        lambda _request: httpx2.Response(
            200,
            json={
                "id": "job_12345",
                "status": "COMPLETED",
                "output": {"schema": "i2v-result/v2"},
                "delayTime": 321,
                "executionTime": 654,
                "workerId": "worker-1",
            },
        )
    )
    client, http_client = _client(transport)
    result = await client.get_job("job_12345")
    assert result.status is RunPodJobStatus.COMPLETED
    assert result.output == {"schema": "i2v-result/v2"}
    assert result.delay_time_ms == 321
    assert result.execution_time_ms == 654
    assert result.worker_id == "worker-1"
    await http_client.aclose()


@pytest.mark.asyncio
async def test_health_requires_the_documented_shape() -> None:
    transport = httpx2.MockTransport(
        lambda _request: httpx2.Response(
            200,
            json={
                "jobs": {
                    "completed": 2,
                    "failed": 0,
                    "inProgress": 1,
                    "inQueue": 3,
                    "retried": 0,
                },
                "workers": {"idle": 0, "running": 1},
            },
        )
    )
    client, http_client = _client(transport)
    result = await client.health()
    assert result.in_queue_jobs == 3
    assert result.in_progress_jobs == 1
    assert result.running_workers == 1
    await http_client.aclose()


@pytest.mark.asyncio
async def test_unknown_status_fails_closed() -> None:
    transport = httpx2.MockTransport(
        lambda _request: httpx2.Response(
            200,
            json={"id": "job_12345", "status": "MAYBE"},
        )
    )
    client, http_client = _client(transport)
    with pytest.raises(RunPodProtocolError, match="status"):
        await client.get_job("job_12345")
    await http_client.aclose()


@pytest.mark.asyncio
async def test_ambiguous_submission_timeout_is_not_retried_or_leaked() -> None:
    calls = 0

    def timeout(_request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        raise httpx2.ReadTimeout("contains rp-test-secret")

    client, http_client = _client(httpx2.MockTransport(timeout))
    with pytest.raises(RunPodTimeoutError) as captured:
        await client.submit(
            input_payload={"private": "do-not-leak"},
            execution_timeout_ms=7_200_000,
            ttl_ms=86_400_000,
        )
    assert calls == 1
    assert "rp-test-secret" not in str(captured.value)
    assert "do-not-leak" not in str(captured.value)
    await http_client.aclose()
