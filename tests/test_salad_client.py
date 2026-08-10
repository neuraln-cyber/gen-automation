import json
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager
from uuid import UUID

import httpx2
import pytest

from gen_automation.integrations.salad import (
    SALAD_API_BASE_URL,
    SALAD_QUEUE_JOB_PAGE_SIZE,
    SALAD_REQUESTS_PER_MINUTE,
    JSONObject,
    SaladAPIError,
    SaladClient,
    SaladContainerGroupInstanceState,
    SaladJobStatus,
    SaladProtocolError,
    SaladRateLimitError,
    SaladTimeoutError,
)

API_KEY = "salad-test-token"
JOB_ID = UUID("50150edd-e182-47b5-a754-2d2a04d6ee31")
GPU_ID = UUID("3c90c3cc-0d44-4b50-8888-8dd25736052a")
LIVE_GPU_ID = UUID("a5db5c50-cbcb-4596-ae80-6a0c8090d80f")
GROUP_ID = UUID("ab3a4591-efc3-46c0-b06a-3d820c0ec100")
QUEUE_ID = UUID("7dcd6922-50e9-4d56-89b5-91cde26f0211")


def job_payload(
    status: SaladJobStatus = SaladJobStatus.PENDING,
    *,
    job_id: UUID = JOB_ID,
) -> JSONObject:
    return {
        "id": str(job_id),
        "input": {"prompt": "portrait"},
        "status": status.value,
        "events": [
            {
                "action": "created",
                "time": "2026-07-28T10:00:00Z",
            }
        ],
        "create_time": "2026-07-28T10:00:00Z",
        "update_time": "2026-07-28T10:00:01Z",
        "metadata": {"internal_job_id": "local-1"},
        "webhook": "https://hooks.example.test/salad",
        "output": None,
    }


def queue_payload() -> JSONObject:
    return {
        "id": str(QUEUE_ID),
        "name": "generation-v1",
        "display_name": "Generation v1",
        "description": "Production generation queue",
        "current_queue_length": 2,
        "container_groups": [],
        "create_time": "2026-07-28T09:00:00Z",
        "update_time": "2026-07-28T09:05:00Z",
    }


def container_group_payload() -> JSONObject:
    return {
        "id": str(GROUP_ID),
        "name": "worker-v1",
        "display_name": "Worker v1",
        "replicas": 0,
        "pending_change": False,
        "version": 3,
        "current_state": {
            "status": "stopped",
            "instance_status_counts": {
                "allocating_count": 0,
                "creating_count": 0,
                "running_count": 0,
                "stopping_count": 0,
            },
            "start_time": None,
            "finish_time": "2026-07-28T09:10:00Z",
        },
        "create_time": "2026-07-28T09:00:00Z",
        "update_time": "2026-07-28T09:10:00Z",
        "container": {"image": "registry.example.test/worker@sha256:digest"},
    }


@asynccontextmanager
async def mocked_salad_client(
    handler: Callable[[httpx2.Request], Coroutine[None, None, httpx2.Response]],
) -> AsyncIterator[SaladClient]:
    transport = httpx2.MockTransport(handler)
    async with httpx2.AsyncClient(transport=transport) as http_client:
        yield SaladClient(
            http_client=http_client,
            api_key=API_KEY,
            organization="creator-org",
            project="production",
        )


@pytest.mark.asyncio
async def test_create_job_uses_public_contract_and_returns_typed_status() -> None:
    async def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.method == "POST"
        assert str(request.url) == (
            f"{SALAD_API_BASE_URL}/organizations/creator-org/projects/production/"
            "queues/generation%20v1/jobs"
        )
        assert request.headers["Salad-Api-Key"] == API_KEY
        assert request.headers["Accept"] == "application/json"
        body: object = json.loads(request.content)
        assert body == {
            "input": {"prompt": "portrait"},
            "metadata": {"internal_job_id": "local-1"},
            "webhook": "https://hooks.example.test/salad",
        }
        return httpx2.Response(201, json=job_payload())

    async with mocked_salad_client(handler) as client:
        job = await client.create_job(
            "generation v1",
            input={"prompt": "portrait"},
            metadata={"internal_job_id": "local-1"},
            webhook="https://hooks.example.test/salad",
        )

    assert job.id == JOB_ID
    assert job.status is SaladJobStatus.PENDING
    assert job.events[0].action == "created"
    assert job.events[0].time.isoformat() == "2026-07-28T10:00:00+00:00"
    assert API_KEY not in repr(client)
    assert client.rate_limit_requests_per_minute == SALAD_REQUESTS_PER_MINUTE == 240


@pytest.mark.asyncio
async def test_discovery_operations_return_typed_results() -> None:
    async def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/quotas"):
            return httpx2.Response(
                200,
                json={
                    "container_groups_quotas": {
                        "container_replicas_quota": 10,
                        "container_replicas_used": 1,
                        "max_container_group_reallocations_per_minute": 5,
                        "max_container_group_recreates_per_minute": 6,
                        "max_container_group_restarts_per_minute": 7,
                    },
                    "create_time": "2026-01-01T00:00:00Z",
                    "update_time": "2026-07-28T00:00:00Z",
                },
            )
        if request.url.path.endswith("/gpu-classes"):
            return httpx2.Response(
                200,
                json={
                    "items": [
                        {
                            "id": str(GPU_ID),
                            "name": "RTX 4090",
                            "prices": [{"price": "0.22"}, {"price": "0.18"}],
                            "gpu_count": 1,
                            "is_high_demand": True,
                            "max_ram": 65536,
                            "max_storage": 1_000_000_000,
                            "max_vcpu": 16,
                            "min_ram": 1024,
                            "min_storage": 1_000_000,
                            "min_vcpu": 1,
                        },
                        {
                            "gpu_class_type": "community",
                            "id": str(LIVE_GPU_ID),
                            "name": "RTX 3090 (24 GB)",
                            "prices": [
                                {"price": "0.25", "priority": "high"},
                                {"price": "0.143", "priority": "low"},
                            ],
                            "is_high_demand": False,
                        },
                    ]
                },
            )
        assert request.url.path.endswith("/availability/sce-gpu-availability")
        body: object = json.loads(request.content)
        assert body == {
            "gpu_classes": [str(GPU_ID)],
            "country_codes": ["us"],
            "cpu": 4,
            "memory": 8192,
            "storage_amount": 1_000_000_000,
        }
        return httpx2.Response(
            200,
            json={
                "available_gpu_batch": 4,
                "available_gpu_high": 1,
                "available_gpu_low": 3,
                "available_gpu_medium": 2,
                "on_call_gpu": 1,
            },
        )

    async with mocked_salad_client(handler) as client:
        quotas = await client.get_quotas()
        gpu_classes = await client.list_gpu_classes()
        availability = await client.get_gpu_availability(
            gpu_classes=[GPU_ID],
            country_codes=["us"],
            cpu=4,
            memory=8192,
            storage_amount=1_000_000_000,
        )

    assert quotas.container_groups.container_replicas_quota == 10
    assert quotas.container_groups.container_replicas_used == 1
    assert gpu_classes[0].id == GPU_ID
    assert gpu_classes[0].name == "RTX 4090"
    assert gpu_classes[0].prices == ("0.22", "0.18")
    assert gpu_classes[1].id == LIVE_GPU_ID
    assert gpu_classes[1].name == "RTX 3090 (24 GB)"
    assert gpu_classes[1].prices == ("0.25", "0.143")
    assert gpu_classes[1].gpu_count == 1
    assert gpu_classes[1].max_ram == 0
    assert availability.available_gpu_batch == 4
    assert availability.available_gpu_high == 1


@pytest.mark.asyncio
async def test_queue_and_container_group_mutations_use_documented_methods() -> None:
    calls: list[tuple[str, str, str | None]] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append((request.method, request.url.path, request.headers.get("content-type")))
        if request.method == "POST" and request.url.path.endswith("/queues"):
            body: object = json.loads(request.content)
            assert body == {
                "name": "generation-v1",
                "display_name": "Generation v1",
                "description": "Production generation queue",
            }
            return httpx2.Response(201, json=queue_payload())
        if request.method == "POST" and request.url.path.endswith("/containers"):
            body = json.loads(request.content)
            assert body == {"name": "worker-v1", "replicas": 0}
            return httpx2.Response(201, json=container_group_payload())
        if request.method == "PATCH":
            body = json.loads(request.content)
            assert body == {"replicas": 1}
            return httpx2.Response(200, json=container_group_payload())
        return httpx2.Response(202)

    async with mocked_salad_client(handler) as client:
        queue = await client.create_queue(
            "generation-v1",
            display_name="Generation v1",
            description="Production generation queue",
        )
        group = await client.create_container_group({"name": "worker-v1", "replicas": 0})
        updated = await client.update_container_group("worker-v1", {"replicas": 1})
        await client.start_container_group("worker-v1")
        await client.stop_container_group("worker-v1")
        await client.cancel_job("generation-v1", JOB_ID)

    assert queue.id == QUEUE_ID
    assert group.id == GROUP_ID
    assert updated.status == "stopped"
    assert updated.current_state.description == ""
    assert calls == [
        (
            "POST",
            "/api/public/organizations/creator-org/projects/production/queues",
            "application/json",
        ),
        (
            "POST",
            "/api/public/organizations/creator-org/projects/production/containers",
            "application/json",
        ),
        (
            "PATCH",
            "/api/public/organizations/creator-org/projects/production/containers/worker-v1",
            "application/merge-patch+json",
        ),
        (
            "POST",
            "/api/public/organizations/creator-org/projects/production/containers/worker-v1/start",
            None,
        ),
        (
            "POST",
            "/api/public/organizations/creator-org/projects/production/containers/worker-v1/stop",
            None,
        ),
        (
            "DELETE",
            f"/api/public/organizations/creator-org/projects/production/queues/"
            f"generation-v1/jobs/{JOB_ID}",
            None,
        ),
    ]


@pytest.mark.asyncio
async def test_list_container_instances_accepts_minimal_documented_payload() -> None:
    async def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.method == "GET"
        assert str(request.url) == (
            f"{SALAD_API_BASE_URL}/organizations/creator-org/projects/production/"
            "containers/worker%20v1/instances"
        )
        return httpx2.Response(
            200,
            json={
                "instances": [
                    {
                        "id": "instance-provider-native-id",
                        "machine_id": "machine-provider-native-id",
                        "state": "running",
                        "update_time": "2026-08-09T12:00:00Z",
                        "version": 7,
                    }
                ]
            },
        )

    async with mocked_salad_client(handler) as client:
        page = await client.list_container_group_instances("worker v1")

    assert len(page.instances) == 1
    instance = page.instances[0]
    assert instance.id == "instance-provider-native-id"
    assert instance.machine_id == "machine-provider-native-id"
    assert instance.state == SaladContainerGroupInstanceState.RUNNING
    assert instance.update_time.isoformat() == "2026-08-09T12:00:00+00:00"
    assert instance.version == 7
    assert instance.ready is None
    assert instance.started is None


@pytest.mark.asyncio
async def test_list_jobs_uses_provider_compatible_pagination_and_parses_statuses() -> None:
    statuses = tuple(SaladJobStatus)

    async def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.params["page"] == "2"
        assert request.url.params["page_size"] == str(SALAD_QUEUE_JOB_PAGE_SIZE)
        items = [
            job_payload(status, job_id=UUID(int=index + 1)) for index, status in enumerate(statuses)
        ]
        return httpx2.Response(200, json={"items": items})

    async with mocked_salad_client(handler) as client:
        page = await client.list_jobs("generation-v1", page=2)
        for unsupported_page_size in (26, 50):
            with pytest.raises(ValueError, match="page_size"):
                await client.list_jobs(
                    "generation-v1",
                    page_size=unsupported_page_size,
                )
        with pytest.raises(ValueError, match="page"):
            await client.list_jobs("generation-v1", page=0)

    assert tuple(job.status for job in page.items) == statuses


@pytest.mark.asyncio
async def test_list_jobs_accepts_an_empty_provider_page() -> None:
    async def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.params["page"] == "1"
        assert request.url.params["page_size"] == str(SALAD_QUEUE_JOB_PAGE_SIZE)
        return httpx2.Response(200, json={"items": []})

    async with mocked_salad_client(handler) as client:
        page = await client.list_jobs("generation-v1")

    assert page.items == ()


@pytest.mark.asyncio
async def test_list_jobs_accepts_a_smaller_live_verified_page_size() -> None:
    async def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.params["page_size"] == "1"
        return httpx2.Response(200, json={"items": []})

    async with mocked_salad_client(handler) as client:
        page = await client.list_jobs("generation-v1", page_size=1)

    assert page.items == ()


@pytest.mark.asyncio
async def test_json_api_error_preserves_safe_details() -> None:
    async def handler(request: httpx2.Request) -> httpx2.Response:
        del request
        return httpx2.Response(
            400,
            json={"title": "Bad Request", "detail": "configuration rejected"},
            headers={"x-request-id": "request-123"},
        )

    async with mocked_salad_client(handler) as client:
        with pytest.raises(SaladAPIError) as captured:
            await client.get_queue("missing")

    error = captured.value
    assert error.status_code == 400
    assert error.message == "Bad Request: configuration rejected"
    assert error.request_id == "request-123"
    assert API_KEY not in str(error)


@pytest.mark.asyncio
async def test_html_api_error_is_readable_truncated_and_redacted() -> None:
    async def handler(request: httpx2.Request) -> httpx2.Response:
        del request
        return httpx2.Response(
            503,
            text=f"<html><title>Unavailable</title><body>{API_KEY} {'x' * 2_000}</body></html>",
        )

    async with mocked_salad_client(handler) as client:
        with pytest.raises(SaladAPIError) as captured:
            await client.get_queue("generation-v1")

    error = captured.value
    assert "Unavailable" in error.message
    assert "<html>" not in error.response_body
    assert API_KEY not in error.response_body
    assert len(error.response_body) == 1_000


@pytest.mark.asyncio
async def test_rate_limit_exposes_retry_hint_without_retrying() -> None:
    request_count = 0

    async def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal request_count
        del request
        request_count += 1
        return httpx2.Response(
            429,
            json={"title": "Too Many Requests", "detail": "slow down"},
            headers={"retry-after": "2.5"},
        )

    async with mocked_salad_client(handler) as client:
        with pytest.raises(SaladRateLimitError) as captured:
            await client.get_quotas()

    assert request_count == 1
    assert captured.value.retry_after_seconds == 2.5


@pytest.mark.asyncio
async def test_timeout_and_provider_schema_drift_are_classified() -> None:
    async def timeout_handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("slow response", request=request)

    async with mocked_salad_client(timeout_handler) as client:
        with pytest.raises(SaladTimeoutError):
            await client.get_quotas()

    invalid_job = job_payload()
    invalid_job["status"] = "unknown"

    async def invalid_handler(request: httpx2.Request) -> httpx2.Response:
        del request
        return httpx2.Response(200, json=invalid_job)

    async with mocked_salad_client(invalid_handler) as client:
        with pytest.raises(SaladProtocolError, match="documented Salad job status"):
            await client.get_job("generation-v1", JOB_ID)
