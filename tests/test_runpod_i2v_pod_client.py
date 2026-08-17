from __future__ import annotations

import json

import httpx2
import pytest

from gen_automation.integrations.runpod.errors import RunPodAPIError, RunPodTimeoutError
from gen_automation.integrations.runpod.models import RunPodJobStatus
from gen_automation.integrations.runpod.pod_client import (
    RunPodPodClient,
    derive_pod_api_key,
)

IMAGE = "ghcr.io/neuraln-cyber/gen-automation/i2v-worker@sha256:" + "a" * 64
SUBMISSION_KEY = "b" * 64
POD_ID = "podabc123"
JOB_ID = f"rpod_{POD_ID}_{SUBMISSION_KEY[:32]}"
OBJECTS_SHA = "c" * 64
ARTIFACT_SHA = "d" * 64
SOURCE_REVISION = "e" * 40


def _ready() -> dict[str, str]:
    return {
        "status": "ready",
        "provider": "runpod-pod",
        "model_objects_sha256": OBJECTS_SHA,
        "artifact_identity_sha256": ARTIFACT_SHA,
        "source_revision": SOURCE_REVISION,
    }


def _client(
    transport: httpx2.MockTransport,
) -> tuple[RunPodPodClient, httpx2.AsyncClient]:
    http_client = httpx2.AsyncClient(transport=transport)
    return (
        RunPodPodClient(
            http_client=http_client,
            api_key="runpod-account-key-for-tests",
            worker_image=IMAGE,
            network_volume_id="volume123",
            worker_environment={"GEN_I2V_WORKER_MODEL_OBJECTS_JSON": "[]"},
            expected_model_objects_sha256=OBJECTS_SHA,
            expected_artifact_identity_sha256=ARTIFACT_SHA,
            expected_source_revision=SOURCE_REVISION,
        ),
        http_client,
    )


@pytest.mark.asyncio
async def test_pod_submit_creates_one_exact_pod_and_uses_derived_worker_auth() -> None:
    requests: list[httpx2.Request] = []
    pod = {
        "id": POD_ID,
        "name": f"gen-automation-i2v-{SUBMISSION_KEY[:24]}",
        "imageName": IMAGE,
        "networkVolumeId": "volume123",
        "desiredStatus": "RUNNING",
    }

    def respond(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        if (
            request.url.host == "rest.runpod.io"
            and request.method == "GET"
            and request.url.path == "/v1/pods"
        ):
            return httpx2.Response(200, json=[])
        if request.url.host == "rest.runpod.io" and request.method == "POST":
            return httpx2.Response(201, json=pod)
        if request.url.host == "rest.runpod.io" and request.url.path == f"/v1/pods/{POD_ID}":
            return httpx2.Response(200, json=pod)
        if request.url.path == "/ready":
            return httpx2.Response(200, json=_ready())
        if request.url.path == f"/v1/jobs/{JOB_ID}" and request.method == "POST":
            return httpx2.Response(202, json={"id": JOB_ID, "status": "IN_PROGRESS"})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    client, http_client = _client(httpx2.MockTransport(respond))
    result = await client.submit(
        input_payload={"submission_key": SUBMISSION_KEY},
        execution_timeout_ms=21_600_000,
        ttl_ms=604_800_000,
    )

    assert result.id == JOB_ID
    assert result.status is RunPodJobStatus.IN_PROGRESS
    creates = [
        request
        for request in requests
        if request.method == "POST" and request.url.host == "rest.runpod.io"
    ]
    assert len(creates) == 1
    body = json.loads(creates[0].content)
    assert body["networkVolumeId"] == "volume123"
    assert body["imageName"] == IMAGE
    assert body["gpuTypeIds"] == ["NVIDIA RTX PRO 4500 Blackwell"]
    assert "dockerEntrypoint" not in body
    assert "dockerStartCmd" not in body
    assert body["env"]["GEN_I2V_WORKER_POD_API_KEY"] == derive_pod_api_key(
        "runpod-account-key-for-tests"
    )
    assert "runpod-account-key-for-tests" not in creates[0].content.decode()
    proxy = next(
        request
        for request in requests
        if request.url.host.endswith("proxy.runpod.net") and request.method == "POST"
    )
    assert proxy.headers["authorization"] == (
        "Bearer " + derive_pod_api_key("runpod-account-key-for-tests")
    )
    await http_client.aclose()


@pytest.mark.asyncio
async def test_ambiguous_create_reconciles_by_name_without_repeating_mutation() -> None:
    list_calls = 0
    create_calls = 0
    pod = {
        "id": POD_ID,
        "name": f"gen-automation-i2v-{SUBMISSION_KEY[:24]}",
        "imageName": IMAGE,
        "networkVolumeId": "volume123",
        "desiredStatus": "RUNNING",
    }

    def respond(request: httpx2.Request) -> httpx2.Response:
        nonlocal create_calls, list_calls
        if (
            request.url.host == "rest.runpod.io"
            and request.method == "GET"
            and request.url.path == "/v1/pods"
        ):
            list_calls += 1
            return httpx2.Response(200, json=[] if list_calls == 1 else [pod])
        if request.url.host == "rest.runpod.io" and request.method == "POST":
            create_calls += 1
            raise httpx2.ReadTimeout("ambiguous")
        if request.url.host == "rest.runpod.io":
            return httpx2.Response(200, json=pod)
        if request.url.path == "/ready":
            return httpx2.Response(200, json=_ready())
        if request.method == "POST":
            return httpx2.Response(202, json={"id": JOB_ID, "status": "IN_PROGRESS"})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    client, http_client = _client(httpx2.MockTransport(respond))
    result = await client.submit(
        input_payload={"submission_key": SUBMISSION_KEY},
        execution_timeout_ms=21_600_000,
        ttl_ms=604_800_000,
    )
    assert result.id == JOB_ID
    assert create_calls == 1
    assert list_calls == 2
    await http_client.aclose()


@pytest.mark.asyncio
async def test_unresolved_ambiguous_create_never_retries() -> None:
    create_calls = 0
    list_calls = 0

    def respond(request: httpx2.Request) -> httpx2.Response:
        nonlocal create_calls, list_calls
        if request.method == "GET":
            list_calls += 1
            return httpx2.Response(200, json=[])
        create_calls += 1
        raise httpx2.ReadTimeout("ambiguous")

    client, http_client = _client(httpx2.MockTransport(respond))
    with pytest.raises(RunPodTimeoutError):
        await client.submit(
            input_payload={"submission_key": SUBMISSION_KEY},
            execution_timeout_ms=21_600_000,
            ttl_ms=604_800_000,
        )
    assert create_calls == 1
    assert list_calls == 2
    await http_client.aclose()


@pytest.mark.asyncio
async def test_startup_failure_deletes_unbilled_job_pod_before_returning() -> None:
    deleted: list[str] = []
    pod = {
        "id": POD_ID,
        "name": f"gen-automation-i2v-{SUBMISSION_KEY[:24]}",
        "imageName": IMAGE,
        "networkVolumeId": "volume123",
        "desiredStatus": "EXITED",
    }

    def respond(request: httpx2.Request) -> httpx2.Response:
        if request.method == "GET" and request.url.path == "/v1/pods":
            return httpx2.Response(200, json=[])
        if request.method == "POST":
            return httpx2.Response(201, json=pod)
        if request.method == "GET":
            return httpx2.Response(200, json=pod)
        deleted.append(request.url.path)
        return httpx2.Response(204)

    client, http_client = _client(httpx2.MockTransport(respond))
    with pytest.raises(RunPodAPIError, match="stopped"):
        await client.submit(
            input_payload={"submission_key": SUBMISSION_KEY},
            execution_timeout_ms=21_600_000,
            ttl_ms=604_800_000,
        )
    assert deleted == [f"/v1/pods/{POD_ID}"]
    await http_client.aclose()


@pytest.mark.asyncio
async def test_reap_idle_deletes_only_exact_managed_volume_pods() -> None:
    deleted: list[str] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        if request.method == "GET":
            return httpx2.Response(
                200,
                json=[
                    {
                        "id": "managed1",
                        "name": "gen-automation-i2v-abcdef",
                        "networkVolumeId": "volume123",
                    },
                    {
                        "id": "other1",
                        "name": "gen-automation-i2v-other",
                        "networkVolumeId": "another-volume",
                    },
                    {
                        "id": "unrelated1",
                        "name": "another-workload",
                        "networkVolumeId": "volume123",
                    },
                ],
            )
        deleted.append(request.url.path)
        return httpx2.Response(204)

    client, http_client = _client(httpx2.MockTransport(respond))
    await client.reap_idle()
    assert deleted == ["/v1/pods/managed1"]
    await http_client.aclose()
