"""On-demand RunPod Pod adapter for the durable I2V runtime."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import re
from typing import NoReturn, cast
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
    JSONValue,
    RunPodEndpointHealth,
    RunPodJob,
    RunPodJobStatus,
    as_json_object,
    parse_job,
)

RUNPOD_REST_API_ROOT = "https://rest.runpod.io/v1"
_POD_NAME_PREFIX = "gen-automation-i2v-"
_PROXY_PORT = 8000
_MAX_ERROR_BODY_LENGTH = 1_000
_HTML_TAG = re.compile(r"<[^>]*>")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]{3,128}$")
_SUBMISSION_KEY = re.compile(r"^[0-9a-f]{64}$")


def derive_pod_api_key(runpod_api_key: str) -> str:
    """Derive a least-privilege Pod API credential without storing another secret."""

    if not runpod_api_key:
        raise ValueError("RunPod API key must not be empty")
    return hmac.new(
        runpod_api_key.encode("utf-8"),
        b"gen-automation/i2v-pod-api/v1",
        hashlib.sha256,
    ).hexdigest()


class RunPodPodClient:
    """Create one exact Pod per durable job and remove it when the job is done."""

    def __init__(
        self,
        *,
        http_client: httpx2.AsyncClient,
        api_key: str,
        worker_image: str,
        network_volume_id: str,
        worker_environment: dict[str, str],
        expected_model_objects_sha256: str,
        expected_artifact_identity_sha256: str,
        expected_source_revision: str | None,
        data_center_id: str = "EU-RO-1",
        gpu_type_ids: tuple[str, ...] = ("NVIDIA RTX PRO 4500 Blackwell",),
        startup_timeout_seconds: int = 30 * 60,
        base_url: str = RUNPOD_REST_API_ROOT,
    ) -> None:
        if not api_key:
            raise ValueError("RunPod API key must not be empty")
        if not worker_image.startswith("ghcr.io/neuraln-cyber/gen-automation/i2v-worker@sha256:"):
            raise ValueError("RunPod Pod worker image is invalid")
        if _IDENTIFIER.fullmatch(network_volume_id) is None:
            raise ValueError("RunPod network volume ID is invalid")
        if _IDENTIFIER.fullmatch(data_center_id) is None:
            raise ValueError("RunPod data center ID is invalid")
        if re.fullmatch(r"[0-9a-f]{64}", expected_model_objects_sha256) is None:
            raise ValueError("RunPod model object identity is invalid")
        if re.fullmatch(r"[0-9a-f]{64}", expected_artifact_identity_sha256) is None:
            raise ValueError("RunPod artifact identity is invalid")
        if (
            expected_source_revision is not None
            and re.fullmatch(r"[0-9a-f]{40}", expected_source_revision) is None
        ):
            raise ValueError("RunPod source revision is invalid")
        if not gpu_type_ids or any(not value.strip() for value in gpu_type_ids):
            raise ValueError("RunPod Pod GPU pool is invalid")
        if startup_timeout_seconds < 60:
            raise ValueError("RunPod Pod startup bound is invalid")
        if base_url.rstrip("/") != RUNPOD_REST_API_ROOT:
            raise ValueError("RunPod REST API root must be the official endpoint")
        self._http_client = http_client
        self.__api_key = api_key
        self.__pod_api_key = derive_pod_api_key(api_key)
        self.worker_image = worker_image
        self.network_volume_id = network_volume_id
        self.provider_id = f"pod-{network_volume_id}"
        self.expected_model_objects_sha256 = expected_model_objects_sha256
        self.expected_artifact_identity_sha256 = expected_artifact_identity_sha256
        self.expected_source_revision = expected_source_revision
        self.worker_environment = {
            **worker_environment,
            "GEN_I2V_WORKER_RUNPOD_MODE": "pod",
            "GEN_I2V_WORKER_POD_API_KEY": self.__pod_api_key,
            "GEN_I2V_WORKER_VOLUME_ROOT": "/runpod-volume",
            "GEN_I2V_WORKER_REQUIRE_PRESEEDED_VOLUME": "true",
        }
        self.data_center_id = data_center_id
        self.gpu_type_ids = gpu_type_ids
        self.startup_timeout_seconds = startup_timeout_seconds
        self.base_url = base_url.rstrip("/")

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(provider_id={self.provider_id!r}, "
            f"network_volume_id={self.network_volume_id!r}, api_key=<redacted>)"
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
        submission_key = input_payload.get("submission_key")
        if not isinstance(submission_key, str) or _SUBMISSION_KEY.fullmatch(submission_key) is None:
            raise ValueError("RunPod submission key is invalid")
        pod = await self._ensure_pod(submission_key)
        pod_id = _required_text(pod, "id", "RunPod Pod")
        provider_job_id = f"rpod_{pod_id}_{submission_key[:32]}"
        try:
            await self._wait_ready(pod_id)
        except Exception:
            # No job has been sent yet, so this exact Pod is safe to terminate.
            await self._delete_pod(pod_id)
            raise
        event: JSONObject = {"id": provider_job_id, "input": input_payload}
        data = await self._proxy_json(
            pod_id,
            "POST",
            f"/v1/jobs/{quote(provider_job_id, safe='')}",
            json_body=event,
        )
        try:
            remote = parse_job(data)
        except ValueError as exc:
            raise RunPodProtocolError(str(exc)) from exc
        if remote.id != provider_job_id:
            raise RunPodProtocolError("RunPod Pod returned a mismatched job identity")
        return remote

    async def get_job(self, job_id: str) -> RunPodJob:
        pod_id = _pod_id_from_job(job_id)
        data = await self._proxy_json(
            pod_id,
            "GET",
            f"/v1/jobs/{quote(job_id, safe='')}",
        )
        try:
            return parse_job(data)
        except ValueError as exc:
            raise RunPodProtocolError(str(exc)) from exc

    async def cancel(self, job_id: str) -> RunPodJob:
        pod_id = _pod_id_from_job(job_id)
        await self._delete_pod(pod_id)
        return RunPodJob(
            id=job_id,
            status=RunPodJobStatus.CANCELLED,
            output=None,
            error=None,
            delay_time_ms=None,
            execution_time_ms=None,
            worker_id=pod_id,
        )

    async def reap_idle(self) -> None:
        """Remove only managed Pods; called only when no durable attempt is active."""

        for pod in await self._managed_pods():
            await self._delete_pod(_required_text(pod, "id", "RunPod Pod"))

    async def health(self) -> RunPodEndpointHealth:
        pods = await self._managed_pods()
        running = sum(pod.get("desiredStatus") == "RUNNING" for pod in pods)
        return RunPodEndpointHealth(
            completed_jobs=0,
            failed_jobs=0,
            in_progress_jobs=0,
            in_queue_jobs=0,
            retried_jobs=0,
            idle_workers=0,
            running_workers=running,
        )

    async def _ensure_pod(self, submission_key: str) -> JSONObject:
        pod_name = f"{_POD_NAME_PREFIX}{submission_key[:24]}"
        matches = [pod for pod in await self._pods() if pod.get("name") == pod_name]
        if len(matches) > 1:
            raise RunPodProtocolError("RunPod returned duplicate managed Pods")
        if matches:
            pod = matches[0]
            self._verify_pod_contract(pod, expected_name=pod_name)
            return pod
        payload: JSONObject = {
            "name": pod_name,
            "imageName": self.worker_image,
            "computeType": "GPU",
            "cloudType": "SECURE",
            "gpuTypeIds": list(self.gpu_type_ids),
            "gpuTypePriority": "availability",
            "gpuCount": 1,
            "dataCenterIds": [self.data_center_id],
            "dataCenterPriority": "availability",
            "allowedCudaVersions": ["12.8", "12.9", "13.0"],
            "containerDiskInGb": 50,
            "networkVolumeId": self.network_volume_id,
            "volumeMountPath": "/runpod-volume",
            "ports": [f"{_PROXY_PORT}/http"],
            "env": cast(dict[str, JSONValue], self.worker_environment),
            "interruptible": False,
        }
        try:
            data = await self._request_json("POST", "/pods", json_body=payload)
        except RunPodTransportError:
            # The create outcome is ambiguous, so never repeat the paid mutation.
            # Reconcile it by its deterministic exact name using a read-only list.
            matches = [pod for pod in await self._pods() if pod.get("name") == pod_name]
            if len(matches) != 1:
                raise
            data = matches[0]
        self._verify_pod_contract(data, expected_name=pod_name)
        return data

    async def _wait_ready(self, pod_id: str) -> None:
        deadline = asyncio.get_running_loop().time() + self.startup_timeout_seconds
        while True:
            pod = await self._request_json("GET", f"/pods/{quote(pod_id, safe='')}")
            self._verify_pod_contract(pod)
            if pod.get("desiredStatus") not in {"RUNNING", None}:
                raise RunPodAPIError(status_code=409, message="RunPod Pod stopped before ready")
            try:
                ready = await self._proxy_json(pod_id, "GET", "/ready")
            except RunPodAPIError as error:
                if error.status_code not in {404, 502, 503, 524}:
                    raise
            except RunPodTransportError:
                pass
            else:
                if ready == {
                    "status": "ready",
                    "provider": "runpod-pod",
                    "model_objects_sha256": self.expected_model_objects_sha256,
                    "artifact_identity_sha256": self.expected_artifact_identity_sha256,
                    "source_revision": self.expected_source_revision,
                }:
                    return
            if asyncio.get_running_loop().time() >= deadline:
                raise RunPodTimeoutError("RunPod Pod did not become ready")
            await asyncio.sleep(5)

    async def _managed_pods(self) -> list[JSONObject]:
        return [
            pod
            for pod in await self._pods()
            if isinstance(pod.get("name"), str)
            and cast(str, pod["name"]).startswith(_POD_NAME_PREFIX)
            and _pod_volume_id(pod) == self.network_volume_id
        ]

    async def _pods(self) -> list[JSONObject]:
        value = await self._request_value("GET", "/pods?computeType=GPU")
        if not isinstance(value, list):
            raise RunPodProtocolError("RunPod Pods response is invalid")
        try:
            return [as_json_object(item, "RunPod Pod") for item in value]
        except ValueError as exc:
            raise RunPodProtocolError(str(exc)) from exc

    def _verify_pod_contract(self, pod: JSONObject, *, expected_name: str | None = None) -> None:
        if expected_name is not None and pod.get("name") != expected_name:
            raise RunPodProtocolError("RunPod Pod name drifted")
        if pod.get("imageName") not in {None, self.worker_image}:
            raise RunPodProtocolError("RunPod Pod image drifted")
        volume_id = _pod_volume_id(pod)
        if volume_id not in {None, self.network_volume_id}:
            raise RunPodProtocolError("RunPod Pod volume drifted")
        gpu = pod.get("gpu")
        if isinstance(gpu, dict):
            gpu_id = gpu.get("id")
            if gpu_id is not None and gpu_id not in self.gpu_type_ids:
                raise RunPodProtocolError("RunPod Pod GPU drifted")

    async def _delete_pod(self, pod_id: str) -> None:
        if _IDENTIFIER.fullmatch(pod_id) is None:
            raise ValueError("RunPod Pod ID is invalid")
        try:
            await self._request_value("DELETE", f"/pods/{quote(pod_id, safe='')}")
        except RunPodAPIError as error:
            if error.status_code != 404:
                raise

    async def _proxy_json(
        self,
        pod_id: str,
        method: str,
        path: str,
        *,
        json_body: JSONObject | None = None,
    ) -> JSONObject:
        if _IDENTIFIER.fullmatch(pod_id) is None or not path.startswith("/"):
            raise ValueError("RunPod Pod proxy target is invalid")
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.__pod_api_key}",
        }
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        response = await self._perform_request(
            method,
            f"https://{pod_id}-{_PROXY_PORT}.proxy.runpod.net{path}",
            headers=headers,
            json_body=json_body,
            request_timeout=30,
        )
        return self._response_object(response)

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        json_body: JSONObject | None = None,
    ) -> JSONObject:
        value = await self._request_value(method, path, json_body=json_body)
        try:
            return as_json_object(value, "RunPod response")
        except ValueError as exc:
            raise RunPodProtocolError(str(exc)) from exc

    async def _request_value(
        self,
        method: str,
        path: str,
        *,
        json_body: JSONObject | None = None,
    ) -> object:
        if not path.startswith("/"):
            raise ValueError("RunPod API path is invalid")
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.__api_key}",
        }
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        response = await self._perform_request(
            method,
            f"{self.base_url}{path}",
            headers=headers,
            json_body=json_body,
            request_timeout=60,
        )
        if response.status_code == 204 or not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise RunPodProtocolError("RunPod returned invalid JSON") from exc

    async def _perform_request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        json_body: JSONObject | None,
        request_timeout: float,
    ) -> httpx2.Response:
        try:
            response = await self._http_client.request(
                method,
                url,
                headers=headers,
                json=json_body,
                timeout=request_timeout,
            )
        except httpx2.TimeoutException:
            raise RunPodTimeoutError("RunPod request timed out") from None
        except httpx2.RequestError:
            raise RunPodTransportError("RunPod request failed") from None
        if response.status_code < 200 or response.status_code >= 300:
            self._raise_api_error(response)
        return response

    def _response_object(self, response: httpx2.Response) -> JSONObject:
        try:
            return as_json_object(response.json(), "RunPod Pod response")
        except ValueError as exc:
            raise RunPodProtocolError("RunPod Pod returned invalid JSON") from exc

    def _raise_api_error(self, response: httpx2.Response) -> NoReturn:
        raw = response.text.replace(self.__api_key, "[redacted]").replace(
            self.__pod_api_key, "[redacted]"
        )
        normalized = " ".join(html.unescape(_HTML_TAG.sub(" ", raw)).split())[
            :_MAX_ERROR_BODY_LENGTH
        ]
        message = normalized or response.reason_phrase or "RunPod request failed"
        if response.status_code == 429:
            raise RunPodRateLimitError(message=message, retry_after_seconds=None)
        raise RunPodAPIError(status_code=response.status_code, message=message)


def _required_text(data: JSONObject, key: str, context: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RunPodProtocolError(f"{context}.{key} is invalid")
    return value


def _pod_id_from_job(job_id: str) -> str:
    match = re.fullmatch(r"rpod_([A-Za-z0-9-]{3,64})_[0-9a-f]{32}", job_id)
    if match is None:
        raise ValueError("RunPod Pod job ID is invalid")
    return match.group(1)


def _pod_volume_id(pod: JSONObject) -> str | None:
    direct = pod.get("networkVolumeId")
    if isinstance(direct, str):
        return direct
    nested = pod.get("networkVolume")
    if isinstance(nested, dict) and isinstance(nested.get("id"), str):
        return cast(str, nested["id"])
    return None
