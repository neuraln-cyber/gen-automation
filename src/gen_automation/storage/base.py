from dataclasses import dataclass
from typing import Protocol


class ObjectStoreError(Exception):
    """Base error for storage operations."""


class ObjectNotFoundError(ObjectStoreError):
    pass


class ObjectAlreadyExistsError(ObjectStoreError):
    pass


class ObjectTooLargeError(ObjectStoreError):
    pass


class ObjectConflictError(ObjectStoreError):
    pass


@dataclass(frozen=True)
class ObjectMetadata:
    key: str
    byte_size: int
    content_type: str | None
    metadata: dict[str, str]
    version_id: str | None = None
    etag: str | None = None


@dataclass(frozen=True)
class PresignedUpload:
    url: str
    method: str
    fields: dict[str, str]
    headers: dict[str, str]


class ObjectStore(Protocol):
    backend: str
    bucket: str

    async def ping(self) -> None: ...

    async def presign_upload(
        self,
        *,
        key: str,
        content_type: str,
        metadata: dict[str, str],
        expires_in: int,
        max_bytes: int,
    ) -> PresignedUpload: ...

    async def presign_download(
        self,
        *,
        key: str,
        expires_in: int,
        download_name: str | None = None,
        version_id: str | None = None,
    ) -> str: ...

    async def head(self, key: str) -> ObjectMetadata | None: ...

    async def read_bytes(
        self,
        key: str,
        *,
        max_bytes: int,
        version_id: str | None = None,
        etag: str | None = None,
    ) -> bytes: ...

    async def write_bytes_if_absent(
        self,
        *,
        key: str,
        body: bytes,
        content_type: str,
        metadata: dict[str, str],
        max_bytes: int,
    ) -> ObjectMetadata: ...

    async def copy_if_absent(
        self,
        *,
        source_key: str,
        destination_key: str,
        content_type: str,
        metadata: dict[str, str],
        source_version_id: str | None = None,
        source_etag: str | None = None,
    ) -> ObjectMetadata: ...

    async def delete(self, key: str, *, version_id: str | None = None) -> None: ...

    async def close(self) -> None: ...
