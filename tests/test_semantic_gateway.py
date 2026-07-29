from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Callable

import httpx2
import pytest

from gen_automation.domain.enums import SemanticIssueCode, SemanticVerdict
from gen_automation.integrations.semantic_vlm import SemanticVlmClient
from gen_automation.semantic import (
    ANATOMY_ASSESSMENT_PROMPT,
    ANATOMY_OUTPUT_SCHEMA,
    SEMANTIC_SCHEMA_VERSION,
    prompt_sha256,
    schema_sha256,
)
from gen_automation.semantic_gateway import SemanticGatewaySettings, create_app

_MODEL = "Qwen/Qwen3-VL-8B-Instruct"
_REVISION = "0123456789abcdef"
_UPSTREAM_URL = "http://vllm.internal/v1/chat/completions"


def _settings(**updates: object) -> SemanticGatewaySettings:
    values: dict[str, object] = {
        "upstream_chat_completions_url": _UPSTREAM_URL,
        "model": _MODEL,
        "model_revision": _REVISION,
        "upstream_api_key": "private-test-key",
    }
    values.update(updates)
    return SemanticGatewaySettings.model_validate(values)


def _gateway_body(payload: bytes) -> tuple[dict[str, object], str]:
    digest = hashlib.sha256(payload).hexdigest()
    request_id = hashlib.sha256(
        (f"{digest}:{_MODEL}:{_REVISION}:{prompt_sha256()}:{schema_sha256()}").encode()
    ).hexdigest()
    return (
        {
            "schema_version": SEMANTIC_SCHEMA_VERSION,
            "request_id": request_id,
            "model": _MODEL,
            "model_revision": _REVISION,
            "image": {
                "content_type": "image/png",
                "sha256": digest,
                "base64": base64.b64encode(payload).decode("ascii"),
            },
            "task": {
                "prompt": ANATOMY_ASSESSMENT_PROMPT,
                "prompt_sha256": prompt_sha256(),
                "output_schema": ANATOMY_OUTPUT_SCHEMA,
                "schema_sha256": schema_sha256(),
            },
        },
        request_id,
    )


def _completion_response(request: httpx2.Request) -> httpx2.Response:
    upstream_body = json.loads(request.content)
    schema = upstream_body["response_format"]["json_schema"]["schema"]
    properties = schema["properties"]
    envelope = {
        "schema_version": SEMANTIC_SCHEMA_VERSION,
        "request_id": properties["request_id"]["const"],
        "model": properties["model"]["const"],
        "model_revision": properties["model_revision"]["const"],
        "asset_sha256": properties["asset_sha256"]["const"],
        "assessment": {
            "verdict": "severe",
            "confidence": 0.96,
            "issues": [
                {
                    "code": "extra_finger",
                    "confidence": 0.98,
                    "box": {
                        "x_min": 0.1,
                        "y_min": 0.2,
                        "x_max": 0.4,
                        "y_max": 0.7,
                    },
                }
            ],
        },
    }
    return httpx2.Response(
        200,
        headers={"content-type": "application/json"},
        json={
            "id": "completion-1",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": json.dumps(envelope),
                    },
                    "finish_reason": "stop",
                }
            ],
        },
        request=request,
    )


@pytest.mark.asyncio
async def test_gateway_matches_controller_contract_and_binds_vllm_output() -> None:
    payload = b"bounded-private-image"
    upstream_requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        upstream_requests.append(request)
        return _completion_response(request)

    async with (
        httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as upstream_client,
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(
                app=create_app(_settings(), upstream_client=upstream_client)
            ),
            base_url="http://semantic-gateway.internal",
        ) as gateway_client,
    ):
        controller_client = SemanticVlmClient(
            http_client=gateway_client,
            endpoint_url="http://semantic-gateway.internal/v1/anatomy/assess",
            model=_MODEL,
            model_revision=_REVISION,
            timeout_seconds=30,
        )
        result = await controller_client.assess(
            payload,
            content_type="image/png",
            asset_sha256=hashlib.sha256(payload).hexdigest(),
        )

    assert result.verdict == SemanticVerdict.SEVERE
    assert result.confidence_micros == 960_000
    assert result.issues[0].code == SemanticIssueCode.EXTRA_FINGER
    assert len(upstream_requests) == 1
    upstream_request = upstream_requests[0]
    assert str(upstream_request.url) == _UPSTREAM_URL
    assert upstream_request.headers["authorization"] == "Bearer private-test-key"
    assert upstream_request.headers["x-gen-automation-model-revision"] == _REVISION
    upstream_body = json.loads(upstream_request.content)
    assert upstream_body["model"] == _MODEL
    assert upstream_body["temperature"] == 0
    assert upstream_body["response_format"]["type"] == "json_schema"
    assert upstream_body["response_format"]["json_schema"]["strict"] is True
    assert upstream_body["messages"][0]["content"][1]["text"] == ANATOMY_ASSESSMENT_PROMPT


@pytest.mark.asyncio
async def test_gateway_rejects_tampering_and_oversized_images_before_inference() -> None:
    upstream_calls = 0

    def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal upstream_calls
        upstream_calls += 1
        return _completion_response(request)

    async with (
        httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as upstream_client,
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(
                app=create_app(
                    _settings(max_image_bytes=8),
                    upstream_client=upstream_client,
                )
            ),
            base_url="http://semantic-gateway.internal",
        ) as gateway_client,
    ):
        tampered_body, request_id = _gateway_body(b"image")
        tampered_body["task"]["prompt_sha256"] = "0" * 64  # type: ignore[index]
        tampered = await gateway_client.post(
            "/v1/anatomy/assess",
            headers={"Idempotency-Key": request_id},
            json=tampered_body,
        )

        oversized_body, oversized_request_id = _gateway_body(b"123456789")
        oversized = await gateway_client.post(
            "/v1/anatomy/assess",
            headers={"Idempotency-Key": oversized_request_id},
            json=oversized_body,
        )

    assert tampered.status_code == 422
    assert oversized.status_code == 413
    assert upstream_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler_factory", "expected_status"),
    [
        (
            lambda: (
                lambda request: (_ for _ in ()).throw(
                    httpx2.ConnectError("private model is cold", request=request)
                )
            ),
            503,
        ),
        (
            lambda: (
                lambda request: httpx2.Response(
                    200,
                    headers={"content-type": "application/json"},
                    json={
                        "choices": [
                            {
                                "message": {"content": "not-json"},
                                "finish_reason": "stop",
                            }
                        ]
                    },
                    request=request,
                )
            ),
            502,
        ),
    ],
)
async def test_gateway_bounds_unavailable_and_malformed_upstream(
    handler_factory: Callable[[], Callable[[httpx2.Request], httpx2.Response]],
    expected_status: int,
) -> None:
    body, request_id = _gateway_body(b"private-image")
    async with (
        httpx2.AsyncClient(transport=httpx2.MockTransport(handler_factory())) as upstream_client,
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(
                app=create_app(_settings(), upstream_client=upstream_client)
            ),
            base_url="http://semantic-gateway.internal",
        ) as gateway_client,
    ):
        response = await gateway_client.post(
            "/v1/anatomy/assess",
            headers={"Idempotency-Key": request_id},
            json=body,
        )

    assert response.status_code == expected_status
