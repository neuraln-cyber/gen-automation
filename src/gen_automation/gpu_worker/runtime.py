from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

import httpx2

from gen_automation.gpu_worker.models import JsonObject, UploadGrant


class ComfyExecutor(Protocol):
    def is_ready(self) -> bool: ...

    def execute(self, workflow: JsonObject) -> object: ...

    def reset_model_and_node_cache(self, barrier_workflow: JsonObject) -> None: ...


class MultipartUploader(Protocol):
    async def upload(
        self,
        *,
        grant: UploadGrant,
        content: bytes,
        media_type: str,
    ) -> None: ...


class PayloadDownloader(Protocol):
    async def download(
        self,
        *,
        url: str,
        expected_bytes: int,
    ) -> bytes: ...


class WorkerUploadError(Exception):
    """A redacted upload failure safe for the worker service layer."""


class WorkerPayloadDownloadError(Exception):
    """A redacted referenced-payload failure safe for the worker boundary."""


@dataclass
class HttpxPayloadDownloader:
    """No-redirect, identity-encoded adapter for private request payloads."""

    client: httpx2.AsyncClient
    timeout_seconds: float = 60.0

    async def download(
        self,
        *,
        url: str,
        expected_bytes: int,
    ) -> bytes:
        body = bytearray()
        try:
            async with self.client.stream(
                "GET",
                url,
                headers={"Accept": "application/json", "Accept-Encoding": "identity"},
                timeout=self.timeout_seconds,
                follow_redirects=False,
            ) as response:
                if response.status_code != 200:
                    raise WorkerPayloadDownloadError("request payload download failed")
                content_encoding = response.headers.get("content-encoding", "identity")
                if content_encoding.casefold() not in {"", "identity"}:
                    raise WorkerPayloadDownloadError("request payload download failed")
                declared = response.headers.get("content-length")
                if declared is not None:
                    try:
                        declared_bytes = int(declared)
                    except ValueError:
                        raise WorkerPayloadDownloadError(
                            "request payload download failed"
                        ) from None
                    if declared_bytes != expected_bytes:
                        raise WorkerPayloadDownloadError("request payload download failed")
                async for chunk in response.aiter_bytes(64 * 1024):
                    body.extend(chunk)
                    if len(body) > expected_bytes:
                        raise WorkerPayloadDownloadError("request payload download failed")
        except WorkerPayloadDownloadError:
            raise
        except (httpx2.TimeoutException, httpx2.TransportError):
            raise WorkerPayloadDownloadError("request payload download failed") from None
        if len(body) != expected_bytes:
            raise WorkerPayloadDownloadError("request payload download failed")
        return bytes(body)


@dataclass
class HttpxMultipartUploader:
    """Default no-redirect multipart adapter for presigned object-store POSTs."""

    client: httpx2.AsyncClient
    timeout_seconds: float = 60.0

    async def upload(
        self,
        *,
        grant: UploadGrant,
        content: bytes,
        media_type: str,
    ) -> None:
        filename = f"output-{grant.output_index}"
        try:
            response = await self.client.post(
                grant.url,
                data=_copy_fields(grant.fields),
                files={"file": (filename, content, media_type)},
                timeout=self.timeout_seconds,
                follow_redirects=False,
            )
        except (httpx2.TimeoutException, httpx2.TransportError):
            raise WorkerUploadError("upload failed") from None

        if response.status_code not in {200, 201, 204}:
            raise WorkerUploadError("upload failed")


def _copy_fields(fields: Mapping[str, str]) -> dict[str, str]:
    return dict(fields)
