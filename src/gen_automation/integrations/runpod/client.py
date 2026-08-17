"""Typed, non-retrying RunPod Serverless Queue client."""

from __future__ import annotations

import html
import re
from typing import NoReturn
from urllib.parse import quote

import httpx2

from gen_automation.integrations.runpod.errors import (
    RunPodAPIError,
    RunPodProtocolError,
    RunPodRateLimitError,
    RunPodTimeoutError,
    RunPodTransportError,
)
from gen_automation.integrations.runpod.models import (
    JSONObject,
    RunPodEndpointHealth,
    RunPodJob,
    as_json_object,
    parse_health,
    parse_job,
)

RUNPOD_QUEUE_API_ROOT = "https://api.runpod.ai/v2"
_MAX_ERROR_BODY_LENGTH = 1_000
_HTML_TAG = re.compile(r"<[^>]*>")


class RunPodClient:
    """RunPod adapter with no automatic mutation retries.

    A timed-out submission is intentionally ambiguous. The durable I2V runtime
    relies on its worker-side exact-once claim before any GPU inference instead
    of risking an unbounded duplicate provider submission.
    """

    def __init__(
        self,
        *,
        http_client: httpx2.AsyncClient,
        api_key: str,
        endpoint_id: str,
        base_url: str = RUNPOD_QUEUE_API_ROOT,
        timeout: float | httpx2.Timeout = 30.0,
    ) -> None:
        if not api_key:
            raise ValueError("RunPod API key must not be empty")
        if not re.fullmatch(r"[A-Za-z0-9_-]{5,64}", endpoint_id):
            raise ValueError("RunPod endpoint ID is invalid")
        if base_url.rstrip("/") != RUNPOD_QUEUE_API_ROOT:
            raise ValueError("RunPod queue API root must be the official endpoint")
        self._http_client = http_client
        self.__api_key = api_key
        self.endpoint_id = endpoint_id
        self.provider_id = endpoint_id
        self.base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._endpoint_path = f"/{quote(endpoint_id, safe='')}"

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(endpoint_id={self.endpoint_id!r}, "
            f"base_url={self.base_url!r}, api_key=<redacted>)"
        )

    async def submit(
        self,
        *,
        input_payload: JSONObject,
        execution_timeout_ms: int,
        ttl_ms: int,
    ) -> RunPodJob:
        if execution_timeout_ms < 5_000 or ttl_ms < execution_timeout_ms:
            raise ValueError("RunPod job policy is invalid")
        payload: JSONObject = {
            "input": input_payload,
            "policy": {
                "executionTimeout": execution_timeout_ms,
                "lowPriority": False,
                "ttl": ttl_ms,
            },
        }
        data = await self._request_json(
            "POST",
            f"{self._endpoint_path}/run",
            json_body=payload,
        )
        return self._parse_job(data)

    async def get_job(self, job_id: str) -> RunPodJob:
        segment = _job_segment(job_id)
        data = await self._request_json(
            "GET",
            f"{self._endpoint_path}/status/{segment}",
        )
        return self._parse_job(data)

    async def cancel(self, job_id: str) -> RunPodJob:
        segment = _job_segment(job_id)
        data = await self._request_json(
            "POST",
            f"{self._endpoint_path}/cancel/{segment}",
            json_body={},
        )
        return self._parse_job(data)

    async def health(self) -> RunPodEndpointHealth:
        data = await self._request_json("GET", f"{self._endpoint_path}/health")
        try:
            return parse_health(data)
        except ValueError as exc:
            raise RunPodProtocolError(str(exc)) from exc

    async def reap_idle(self) -> None:
        """Serverless owns worker scale-to-zero; no controller cleanup is needed."""

        return None

    def _parse_job(self, data: JSONObject) -> RunPodJob:
        try:
            return parse_job(data)
        except ValueError as exc:
            raise RunPodProtocolError(str(exc)) from exc

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        json_body: JSONObject | None = None,
    ) -> JSONObject:
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.__api_key}",
        }
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        response: httpx2.Response | None = None
        request_error: RunPodTimeoutError | RunPodTransportError | None = None
        try:
            response = await self._http_client.request(
                method,
                f"{self.base_url}{path}",
                headers=headers,
                json=json_body,
                timeout=self._timeout,
            )
        except httpx2.TimeoutException:
            request_error = RunPodTimeoutError("RunPod request timed out")
        except httpx2.RequestError:
            request_error = RunPodTransportError("RunPod request failed")
        if request_error is not None:
            raise request_error
        if response is None:
            raise RunPodTransportError("RunPod request failed")
        if response.status_code < 200 or response.status_code >= 300:
            self._raise_api_error(response)
        try:
            return as_json_object(response.json(), "RunPod response")
        except ValueError as exc:
            raise RunPodProtocolError("RunPod returned an invalid JSON object") from exc

    def _raise_api_error(self, response: httpx2.Response) -> NoReturn:
        raw = response.text.replace(self.__api_key, "[redacted]")
        normalized = " ".join(html.unescape(_HTML_TAG.sub(" ", raw)).split())[
            :_MAX_ERROR_BODY_LENGTH
        ]
        message = normalized or response.reason_phrase or "RunPod request failed"
        if response.status_code == 429:
            retry_after = _retry_after(response.headers.get("retry-after"))
            raise RunPodRateLimitError(
                message=message,
                retry_after_seconds=retry_after,
            )
        raise RunPodAPIError(status_code=response.status_code, message=message)


def _job_segment(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{5,128}", value):
        raise ValueError("RunPod job ID is invalid")
    return quote(value, safe="")


def _retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None
