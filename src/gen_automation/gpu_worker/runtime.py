import re
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

    def __init__(
        self,
        message: str = "upload failed",
        *,
        category: str = "upload_failed",
        status_code: int | None = None,
        storage_code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category if category in UPLOAD_FAILURE_CATEGORIES else "upload_failed"
        self.status_code = (
            status_code if type(status_code) is int and 100 <= status_code <= 599 else None
        )
        self.storage_code = storage_code if storage_code in SAFE_STORAGE_ERROR_CODES else None


UPLOAD_FAILURE_CATEGORIES = frozenset(
    {"upload_failed", "upload_timeout", "upload_transport", "upload_http"}
)
SAFE_STORAGE_ERROR_CODES = frozenset(
    {
        "AccessDenied",
        "ExpiredToken",
        "InvalidToken",
        "SignatureDoesNotMatch",
        "RequestTimeTooSkewed",
        "RequestTimeout",
        "EntityTooLarge",
        "EntityTooSmall",
        "InvalidArgument",
        "InvalidPolicyDocument",
        "InternalError",
        "ServiceUnavailable",
        "SlowDown",
        "NoSuchBucket",
        "MalformedPOSTRequest",
        "TokenRefreshRequired",
    }
)


def _safe_storage_error_code(body: bytes) -> str | None:
    # Inspect only a bounded literal XML Code, never parse entities or retain the
    # response (which can contain credentials, policy fields and object keys).
    match = re.search(rb"<Code>([A-Za-z]{1,40})</Code>", body[:8192])
    code = match.group(1).decode("ascii") if match else None
    return code if code in SAFE_STORAGE_ERROR_CODES else None


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
        except httpx2.TimeoutException:
            raise WorkerUploadError(category="upload_timeout") from None
        except httpx2.TransportError:
            raise WorkerUploadError(category="upload_transport") from None

        if response.status_code not in {200, 201, 204}:
            raise WorkerUploadError(
                category="upload_http",
                status_code=response.status_code,
                storage_code=_safe_storage_error_code(response.content),
            )


def _copy_fields(fields: Mapping[str, str]) -> dict[str, str]:
    return dict(fields)
