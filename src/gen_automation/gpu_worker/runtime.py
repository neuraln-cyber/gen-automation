from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

import httpx2

from gen_automation.gpu_worker.models import JsonObject, UploadGrant


class ComfyExecutor(Protocol):
    def is_ready(self) -> bool: ...

    def execute(self, workflow: JsonObject) -> object: ...


class MultipartUploader(Protocol):
    async def upload(
        self,
        *,
        grant: UploadGrant,
        content: bytes,
        media_type: str,
    ) -> None: ...


class WorkerUploadError(Exception):
    """A redacted upload failure safe for the worker service layer."""


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
