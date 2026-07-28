import hashlib
from dataclasses import dataclass
from urllib.parse import quote

from gen_automation.domain.ids import uuid7
from gen_automation.storage.base import (
    ObjectAlreadyExistsError,
    ObjectMetadata,
    ObjectNotFoundError,
    ObjectTooLargeError,
    PresignedUpload,
)


@dataclass(frozen=True)
class StoredObject:
    body: bytes
    content_type: str
    metadata: dict[str, str]
    version_id: str


class MemoryObjectStore:
    backend = "memory"

    def __init__(self, bucket: str = "test-assets") -> None:
        self.bucket = bucket
        self.objects: dict[str, StoredObject] = {}

    async def ping(self) -> None:
        return None

    async def presign_upload(
        self,
        *,
        key: str,
        content_type: str,
        metadata: dict[str, str],
        expires_in: int,
        max_bytes: int,
    ) -> PresignedUpload:
        fields = {
            "key": key,
            "Content-Type": content_type,
            "x-amz-server-side-encryption": "AES256",
            "content-length-range": f"1,{max_bytes}",
        }
        fields.update({f"x-amz-meta-{name}": value for name, value in metadata.items()})
        return PresignedUpload(
            url=f"memory://{self.bucket}?expires={expires_in}",
            method="POST",
            fields=fields,
            headers={},
        )

    async def presign_download(
        self,
        *,
        key: str,
        expires_in: int,
        download_name: str | None = None,
        version_id: str | None = None,
    ) -> str:
        stored = self.objects.get(key)
        if stored is None:
            raise ObjectNotFoundError(key)
        if version_id is not None and stored.version_id != version_id:
            raise ObjectNotFoundError(f"{key} version {version_id}")
        name = f"&name={quote(download_name)}" if download_name else ""
        version = f"&version={quote(version_id)}" if version_id else ""
        return f"memory://{self.bucket}/{quote(key)}?expires={expires_in}{name}{version}"

    async def head(self, key: str) -> ObjectMetadata | None:
        stored = self.objects.get(key)
        if stored is None:
            return None
        return ObjectMetadata(
            key=key,
            byte_size=len(stored.body),
            content_type=stored.content_type,
            metadata=dict(stored.metadata),
            version_id=stored.version_id,
            etag=hashlib.md5(stored.body, usedforsecurity=False).hexdigest(),
        )

    async def read_bytes(
        self,
        key: str,
        *,
        max_bytes: int,
        version_id: str | None = None,
        etag: str | None = None,
    ) -> bytes:
        stored = self.objects.get(key)
        if stored is None:
            raise ObjectNotFoundError(key)
        current_etag = hashlib.md5(
            stored.body,
            usedforsecurity=False,
        ).hexdigest()
        if version_id is not None and stored.version_id != version_id:
            raise ObjectNotFoundError(f"{key} version {version_id}")
        if etag is not None and current_etag != etag:
            raise ObjectNotFoundError(f"{key} etag changed")
        if len(stored.body) > max_bytes:
            raise ObjectTooLargeError(f"{key} exceeds {max_bytes} bytes")
        return stored.body

    async def write_bytes_if_absent(
        self,
        *,
        key: str,
        body: bytes,
        content_type: str,
        metadata: dict[str, str],
        max_bytes: int,
    ) -> ObjectMetadata:
        if key in self.objects:
            raise ObjectAlreadyExistsError(key)
        if not isinstance(body, bytes) or not body:
            raise ValueError("object body must be non-empty bytes")
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
            raise ValueError("max_bytes must be a positive integer")
        if len(body) > max_bytes:
            raise ObjectTooLargeError(f"{key} exceeds {max_bytes} bytes")
        expected_sha256 = hashlib.sha256(body).hexdigest()
        supplied_sha256 = metadata.get("sha256")
        if supplied_sha256 is not None and supplied_sha256 != expected_sha256:
            raise ValueError("object sha256 metadata does not match its bytes")
        stored_metadata = dict(metadata)
        stored_metadata["sha256"] = expected_sha256
        self.put_for_test(
            key,
            body,
            content_type=content_type,
            metadata=stored_metadata,
        )
        result = await self.head(key)
        if result is None:
            raise ObjectNotFoundError(key)
        return result

    async def copy_if_absent(
        self,
        *,
        source_key: str,
        destination_key: str,
        content_type: str,
        metadata: dict[str, str],
        source_version_id: str | None = None,
        source_etag: str | None = None,
    ) -> ObjectMetadata:
        source = self.objects.get(source_key)
        if source is None:
            raise ObjectNotFoundError(source_key)
        current_etag = hashlib.md5(
            source.body,
            usedforsecurity=False,
        ).hexdigest()
        if source_version_id is not None and source.version_id != source_version_id:
            raise ObjectNotFoundError(f"{source_key} version {source_version_id}")
        if source_etag is not None and current_etag != source_etag:
            raise ObjectNotFoundError(f"{source_key} etag changed")
        if destination_key in self.objects:
            raise ObjectAlreadyExistsError(destination_key)

        self.put_for_test(
            destination_key,
            source.body,
            content_type=content_type,
            metadata=metadata,
        )
        result = await self.head(destination_key)
        if result is None:
            raise ObjectNotFoundError(destination_key)
        return result

    async def delete(self, key: str, *, version_id: str | None = None) -> None:
        stored = self.objects.get(key)
        if stored is None:
            return
        if version_id is not None and stored.version_id != version_id:
            raise ObjectNotFoundError(f"{key} version {version_id}")
        del self.objects[key]

    async def close(self) -> None:
        return None

    def put_for_test(
        self,
        key: str,
        body: bytes,
        *,
        content_type: str = "image/png",
        metadata: dict[str, str] | None = None,
    ) -> None:
        self.objects[key] = StoredObject(
            body=body,
            content_type=content_type,
            metadata=metadata or {},
            version_id=str(uuid7()),
        )
