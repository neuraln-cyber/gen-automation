import base64
import hashlib
from dataclasses import dataclass, field
from typing import Any

import pytest
from botocore.exceptions import ClientError

from gen_automation.storage.base import ObjectAlreadyExistsError
from gen_automation.storage.model_artifacts import FINAL_CONTENT_TYPE
from gen_automation.storage.s3 import PRIVATE_NO_STORE_CACHE_CONTROL, S3ObjectStore


@dataclass
class Body:
    content: bytes
    offset: int = 0
    closed: bool = False

    def read(self, amount: int) -> bytes:
        result = self.content[self.offset : self.offset + amount]
        self.offset += len(result)
        return result

    def close(self) -> None:
        self.closed = True


@dataclass
class ArtifactS3Client:
    content: bytes
    complete_error: ClientError | None = None
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    created_metadata: dict[str, str] = field(default_factory=dict)
    created_content_type: str = FINAL_CONTENT_TYPE
    completed: bool = False
    bodies: list[Body] = field(default_factory=list)

    def head_object(self, **parameters: Any) -> dict[str, Any]:
        self.calls.append(("head_object", parameters))
        return {
            "ContentLength": len(self.content),
            "ContentType": self.created_content_type,
            "Metadata": self.created_metadata,
            "VersionId": "exact-version",
            "ETag": '"0123456789abcdef0123456789abcdef"',
        }

    def get_object(self, **parameters: Any) -> dict[str, Any]:
        self.calls.append(("get_object", parameters))
        content = self.content
        raw_range = parameters.get("Range")
        if isinstance(raw_range, str):
            start_text, end_text = raw_range.removeprefix("bytes=").split("-", 1)
            content = content[int(start_text) : int(end_text) + 1]
        body = Body(content)
        self.bodies.append(body)
        return {"ContentLength": len(content), "Body": body}

    def create_multipart_upload(self, **parameters: Any) -> dict[str, Any]:
        self.calls.append(("create_multipart_upload", parameters))
        self.created_metadata = dict(parameters["Metadata"])
        self.created_content_type = str(parameters["ContentType"])
        return {"UploadId": "opaque-upload-id"}

    def upload_part(self, **parameters: Any) -> dict[str, Any]:
        self.calls.append(("upload_part", parameters))
        return {"ETag": '"part-etag"'}

    def complete_multipart_upload(self, **parameters: Any) -> dict[str, Any]:
        self.calls.append(("complete_multipart_upload", parameters))
        if self.complete_error is not None:
            raise self.complete_error
        self.completed = True
        return {"VersionId": "exact-version"}

    def abort_multipart_upload(self, **parameters: Any) -> dict[str, Any]:
        self.calls.append(("abort_multipart_upload", parameters))
        return {}

    def close(self) -> None:
        return None


def store_with(fake: ArtifactS3Client) -> S3ObjectStore:
    store = S3ObjectStore(
        bucket="managed-models",
        region="eu-central-1",
        access_key_id="test-access-key",
        secret_access_key="test-secret-key",  # noqa: S106
    )
    original = store.client
    store.client = fake
    original.close()
    return store


@pytest.mark.asyncio
async def test_s3_exact_version_head_range_and_streaming_sha256() -> None:
    content = b"0123456789abcdef"
    fake = ArtifactS3Client(content=content)
    store = store_with(fake)

    metadata = await store.head("object", version_id="exact-version")
    selected = await store.read_range(
        "object",
        start=2,
        length=5,
        version_id="exact-version",
        etag="0123456789abcdef0123456789abcdef",
    )
    digest = await store.sha256(
        "object",
        max_bytes=len(content),
        version_id="exact-version",
        etag="0123456789abcdef0123456789abcdef",
    )

    assert metadata is not None and metadata.version_id == "exact-version"
    assert selected == b"23456"
    assert digest.sha256 == hashlib.sha256(content).hexdigest()
    assert digest.byte_size == len(content)
    head_call = next(parameters for name, parameters in fake.calls if name == "head_object")
    assert head_call["VersionId"] == "exact-version"
    get_calls = [parameters for name, parameters in fake.calls if name == "get_object"]
    assert get_calls[0]["Range"] == "bytes=2-6"
    assert get_calls[0]["VersionId"] == "exact-version"
    assert get_calls[0]["IfMatch"] == "0123456789abcdef0123456789abcdef"
    assert get_calls[1]["VersionId"] == "exact-version"
    assert all(body.closed for body in fake.bodies)


@pytest.mark.asyncio
async def test_s3_multipart_is_encrypted_conditional_verified_and_abortable() -> None:
    body = b"bounded multipart body"
    sha256 = hashlib.sha256(body).hexdigest()
    fake = ArtifactS3Client(content=body)
    store = store_with(fake)

    upload = await store.create_multipart_upload(
        key=f"worker/managed-loras/sha256/{sha256}.safetensors",
        content_type=FINAL_CONTENT_TYPE,
        metadata={
            "artifact-kind": "managed-lora",
            "format": "safetensors",
            "sha256": sha256,
        },
        storage_class="INTELLIGENT_TIERING",
    )
    part = await store.upload_part(upload, part_number=1, body=body)
    result = await store.complete_multipart_upload(
        upload,
        parts=(part,),
        total_bytes=len(body),
    )
    await store.abort_multipart_upload(upload)

    create_call = next(
        parameters for name, parameters in fake.calls if name == "create_multipart_upload"
    )
    assert create_call["ServerSideEncryption"] == "AES256"
    assert create_call["StorageClass"] == "INTELLIGENT_TIERING"
    assert create_call["CacheControl"] == PRIVATE_NO_STORE_CACHE_CONTROL
    upload_call = next(parameters for name, parameters in fake.calls if name == "upload_part")
    assert upload_call["ContentLength"] == len(body)
    assert (
        upload_call["ContentMD5"]
        == base64.b64encode(hashlib.md5(body, usedforsecurity=False).digest()).decode()
    )
    complete_call = next(
        parameters for name, parameters in fake.calls if name == "complete_multipart_upload"
    )
    assert complete_call["IfNoneMatch"] == "*"
    assert complete_call["MpuObjectSize"] == len(body)
    assert complete_call["MultipartUpload"] == {"Parts": [{"PartNumber": 1, "ETag": '"part-etag"'}]}
    assert result.version_id == "exact-version"
    assert result.byte_size == len(body)
    assert repr(upload) == (
        "MultipartUpload(key="
        f"'worker/managed-loras/sha256/{sha256}.safetensors', upload_id=<redacted>)"
    )
    abort_call = next(
        parameters for name, parameters in fake.calls if name == "abort_multipart_upload"
    )
    assert abort_call["UploadId"] == "opaque-upload-id"


@pytest.mark.asyncio
async def test_s3_conditional_multipart_conflict_is_classified_for_runtime_abort() -> None:
    error = ClientError(
        {
            "Error": {"Code": "PreconditionFailed", "Message": "exists"},
            "ResponseMetadata": {"HTTPStatusCode": 412},
        },
        "CompleteMultipartUpload",
    )
    fake = ArtifactS3Client(content=b"body", complete_error=error)
    store = store_with(fake)
    upload = await store.create_multipart_upload(
        key=f"worker/managed-loras/sha256/{'a' * 64}.safetensors",
        content_type=FINAL_CONTENT_TYPE,
        metadata={"sha256": "a" * 64},
    )
    part = await store.upload_part(upload, part_number=1, body=b"body")

    with pytest.raises(ObjectAlreadyExistsError):
        await store.complete_multipart_upload(upload, parts=(part,), total_bytes=4)
    await store.abort_multipart_upload(upload)

    assert any(name == "abort_multipart_upload" for name, _ in fake.calls)
