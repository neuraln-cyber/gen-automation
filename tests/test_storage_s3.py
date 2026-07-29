import base64
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import pytest
from botocore.exceptions import ClientError

from gen_automation.config import Environment, Settings
from gen_automation.storage.base import (
    ObjectAlreadyExistsError,
    ObjectConflictError,
    ObjectNotFoundError,
    ObjectStoreError,
    ObjectTooLargeError,
)
from gen_automation.storage.s3 import (
    PRIVATE_NO_STORE_CACHE_CONTROL,
    S3ObjectStore,
    build_object_store,
)


def s3_store() -> S3ObjectStore:
    return S3ObjectStore(
        bucket="private-assets",
        region="us-east-1",
        access_key_id="test-access-key",
        secret_access_key="test-secret-key",  # noqa: S106
    )


@pytest.mark.asyncio
async def test_presigned_post_has_exact_fields_and_size_policy() -> None:
    store = s3_store()
    try:
        grant = await store.presign_upload(
            key="staging/job/attempt/output",
            content_type="image/png",
            metadata={"asset-id": "asset-1"},
            expires_in=600,
            max_bytes=12345,
        )
    finally:
        await store.close()

    policy = json.loads(base64.b64decode(grant.fields["policy"]))
    assert grant.method == "POST"
    assert grant.fields["key"] == "staging/job/attempt/output"
    assert grant.fields["Content-Type"] == "image/png"
    assert grant.fields["Cache-Control"] == PRIVATE_NO_STORE_CACHE_CONTROL
    assert grant.fields["x-amz-meta-asset-id"] == "asset-1"
    assert ["content-length-range", 1, 12345] in policy["conditions"]
    assert {"key": "staging/job/attempt/output"} in policy["conditions"]
    assert {"Cache-Control": PRIVATE_NO_STORE_CACHE_CONTROL} in policy["conditions"]


@pytest.mark.asyncio
async def test_aws_presigned_urls_use_the_configured_regional_endpoint() -> None:
    store = S3ObjectStore(
        bucket="private-assets",
        region="eu-central-1",
        access_key_id="test-access-key",
        secret_access_key="test-secret-key",  # noqa: S106
    )
    try:
        upload = await store.presign_upload(
            key="staging/job/attempt/output",
            content_type="image/png",
            metadata={"asset-id": "asset-1"},
            expires_in=60,
            max_bytes=4096,
        )
        download = await store.presign_download(
            key="masters/output",
            expires_in=60,
            version_id="version-1",
        )
    finally:
        await store.close()

    expected_host = "private-assets.s3.eu-central-1.amazonaws.com"
    assert urlsplit(upload.url).hostname == expected_host
    assert urlsplit(download).hostname == expected_host


@dataclass
class FakeStreamingBody:
    content: bytes
    closed: bool = False
    offset: int = 0

    def read(self, amount: int) -> bytes:
        result = self.content[self.offset : self.offset + amount]
        self.offset += len(result)
        return result

    def close(self) -> None:
        self.closed = True


@dataclass
class FakeS3Client:
    copy_error: ClientError | None = None
    put_error: ClientError | None = None
    head_error: ClientError | None = None
    get_error: ClientError | None = None
    signing_error: ClientError | None = None
    body: FakeStreamingBody | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    def copy_object(self, **parameters: Any) -> dict[str, Any]:
        self.calls.append(parameters)
        if self.copy_error is not None:
            raise self.copy_error
        return {}

    def put_object(self, **parameters: Any) -> dict[str, Any]:
        self.calls.append(parameters)
        if self.put_error is not None:
            raise self.put_error
        return {"VersionId": "master-version"}

    def head_object(self, **parameters: Any) -> dict[str, Any]:
        del parameters
        if self.head_error is not None:
            raise self.head_error
        put_call = next(
            (call for call in reversed(self.calls) if isinstance(call.get("Body"), bytes)),
            None,
        )
        if put_call is not None:
            return {
                "ContentLength": put_call["ContentLength"],
                "ContentType": put_call["ContentType"],
                "Metadata": put_call["Metadata"],
                "VersionId": "master-version",
                "ETag": '"master-etag"',
            }
        return {
            "ContentLength": 10,
            "ContentType": "image/png",
            "Metadata": {"sha256": "a" * 64},
            "VersionId": "master-version",
            "ETag": '"master-etag"',
        }

    def get_object(self, **parameters: Any) -> dict[str, Any]:
        self.calls.append(parameters)
        if self.get_error is not None:
            raise self.get_error
        assert self.body is not None
        return {
            "ContentLength": len(self.body.content),
            "Body": self.body,
        }

    def generate_presigned_url(
        self,
        operation: str,
        **parameters: Any,
    ) -> str:
        self.calls.append({"operation": operation, **parameters})
        if self.signing_error is not None:
            raise self.signing_error
        return "https://signed.example/download"

    def generate_presigned_post(self, **parameters: Any) -> dict[str, Any]:
        self.calls.append(parameters)
        if self.signing_error is not None:
            raise self.signing_error
        return {"url": "https://signed.example/upload", "fields": {"key": "object"}}

    def head_bucket(self, **parameters: Any) -> dict[str, Any]:
        self.calls.append(parameters)
        if self.head_error is not None:
            raise self.head_error
        return {}

    def delete_object(self, **parameters: Any) -> dict[str, Any]:
        self.calls.append(parameters)
        if self.head_error is not None:
            raise self.head_error
        return {}

    def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_copy_is_conditional_and_source_pinned() -> None:
    store = s3_store()
    original_client = store.client
    fake = FakeS3Client()
    store.client = fake
    original_client.close()

    await store.copy_if_absent(
        source_key="staging/source",
        destination_key="masters/destination",
        content_type="image/png",
        metadata={"sha256": "a" * 64},
        source_version_id="source-version",
        source_etag="source-etag",
    )

    copy_call = fake.calls[0]
    assert copy_call["IfNoneMatch"] == "*"
    assert copy_call["CopySource"]["VersionId"] == "source-version"
    assert copy_call["CopySourceIfMatch"] == "source-etag"
    assert copy_call["CacheControl"] == PRIVATE_NO_STORE_CACHE_CONTROL


@pytest.mark.asyncio
async def test_direct_write_is_bounded_conditional_and_verified() -> None:
    store = s3_store()
    original_client = store.client
    fake = FakeS3Client()
    store.client = fake
    original_client.close()
    body = b"0123456789"

    result = await store.write_bytes_if_absent(
        key="derivatives/release/output.png",
        body=body,
        content_type="image/png",
        metadata={"lineage": "lineage-v1"},
        max_bytes=len(body),
    )

    put_call = fake.calls[0]
    assert put_call["IfNoneMatch"] == "*"
    assert put_call["ContentLength"] == len(body)
    assert put_call["ContentMD5"] == base64.b64encode(
        hashlib.md5(body, usedforsecurity=False).digest()
    ).decode("ascii")
    assert put_call["CacheControl"] == PRIVATE_NO_STORE_CACHE_CONTROL
    assert put_call["Metadata"]["lineage"] == "lineage-v1"
    assert put_call["Metadata"]["sha256"] == hashlib.sha256(body).hexdigest()
    assert result.version_id == "master-version"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "code", "expected_error"),
    [
        (412, "PreconditionFailed", ObjectAlreadyExistsError),
        (409, "ConditionalRequestConflict", ObjectConflictError),
        (500, "InternalError", ObjectStoreError),
    ],
)
async def test_direct_write_errors_are_classified(
    status_code: int,
    code: str,
    expected_error: type[Exception],
) -> None:
    store = s3_store()
    original_client = store.client
    store.client = FakeS3Client(put_error=client_error(status_code, code))
    original_client.close()

    with pytest.raises(expected_error):
        await store.write_bytes_if_absent(
            key="derivatives/release/output.png",
            body=b"0123456789",
            content_type="image/png",
            metadata={},
            max_bytes=10,
        )


@pytest.mark.asyncio
async def test_direct_write_rejects_bad_size_or_checksum_before_s3() -> None:
    store = s3_store()
    original_client = store.client
    fake = FakeS3Client()
    store.client = fake
    original_client.close()

    with pytest.raises(ObjectTooLargeError):
        await store.write_bytes_if_absent(
            key="derivatives/release/output.png",
            body=b"too large",
            content_type="image/png",
            metadata={},
            max_bytes=1,
        )
    with pytest.raises(ValueError, match="sha256"):
        await store.write_bytes_if_absent(
            key="derivatives/release/output.png",
            body=b"0123456789",
            content_type="image/png",
            metadata={"sha256": "0" * 64},
            max_bytes=10,
        )
    assert fake.calls == []


@pytest.mark.asyncio
async def test_destination_precondition_failure_is_reconciled() -> None:
    error = ClientError(
        {
            "Error": {"Code": "PreconditionFailed", "Message": "exists"},
            "ResponseMetadata": {"HTTPStatusCode": 412},
        },
        "CopyObject",
    )
    store = s3_store()
    original_client = store.client
    store.client = FakeS3Client(copy_error=error)
    original_client.close()

    with pytest.raises(ObjectAlreadyExistsError):
        await store.copy_if_absent(
            source_key="staging/source",
            destination_key="masters/destination",
            content_type="image/png",
            metadata={"sha256": "a" * 64},
            source_version_id="source-version",
            source_etag="source-etag",
        )


@pytest.mark.asyncio
async def test_oversized_read_closes_the_streaming_body() -> None:
    body = FakeStreamingBody(b"x" * 100)
    store = s3_store()
    original_client = store.client
    store.client = FakeS3Client(body=body)
    original_client.close()

    with pytest.raises(ObjectTooLargeError):
        await store.read_bytes("staging/source", max_bytes=10)

    assert body.closed is True


@pytest.mark.asyncio
async def test_successful_read_is_source_pinned_and_closes_body() -> None:
    body = FakeStreamingBody(b"verified bytes")
    store = s3_store()
    original_client = store.client
    fake = FakeS3Client(body=body)
    store.client = fake
    original_client.close()

    result = await store.read_bytes(
        "staging/source",
        max_bytes=100,
        version_id="source-version",
        etag="source-etag",
    )

    assert result == b"verified bytes"
    assert fake.calls[0]["VersionId"] == "source-version"
    assert fake.calls[0]["IfMatch"] == "source-etag"
    assert body.closed is True


@pytest.mark.asyncio
async def test_download_is_version_pinned_and_filename_is_encoded() -> None:
    store = s3_store()
    original_client = store.client
    fake = FakeS3Client()
    store.client = fake
    original_client.close()

    url = await store.presign_download(
        key="masters/object",
        expires_in=600,
        download_name="private master.png",
        version_id="master-version",
    )

    assert url == "https://signed.example/download"
    parameters = fake.calls[0]["Params"]
    assert parameters["VersionId"] == "master-version"
    assert parameters["ResponseCacheControl"] == PRIVATE_NO_STORE_CACHE_CONTROL
    assert "private%20master.png" in parameters["ResponseContentDisposition"]


@pytest.mark.asyncio
async def test_ping_and_version_specific_delete_use_expected_parameters() -> None:
    store = s3_store()
    original_client = store.client
    fake = FakeS3Client()
    store.client = fake
    original_client.close()

    await store.ping()
    await store.delete("staging/source", version_id="source-version")

    assert fake.calls == [
        {"Bucket": "private-assets"},
        {
            "Bucket": "private-assets",
            "Key": "staging/source",
            "VersionId": "source-version",
        },
    ]


def client_error(status_code: int, code: str) -> ClientError:
    return ClientError(
        {
            "Error": {"Code": code, "Message": code},
            "ResponseMetadata": {"HTTPStatusCode": status_code},
        },
        "S3Operation",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "code", "expected_error"),
    [
        (404, "NoSuchKey", ObjectNotFoundError),
        (409, "ConditionalRequestConflict", ObjectConflictError),
        (500, "InternalError", ObjectStoreError),
    ],
)
async def test_copy_errors_are_classified(
    status_code: int,
    code: str,
    expected_error: type[Exception],
) -> None:
    store = s3_store()
    original_client = store.client
    store.client = FakeS3Client(copy_error=client_error(status_code, code))
    original_client.close()

    with pytest.raises(expected_error):
        await store.copy_if_absent(
            source_key="staging/source",
            destination_key="masters/destination",
            content_type="image/png",
            metadata={},
        )


@pytest.mark.asyncio
async def test_precondition_without_destination_means_source_changed() -> None:
    store = s3_store()
    original_client = store.client
    store.client = FakeS3Client(
        copy_error=client_error(412, "PreconditionFailed"),
        head_error=client_error(404, "NoSuchKey"),
    )
    original_client.close()

    with pytest.raises(ObjectNotFoundError, match="source changed"):
        await store.copy_if_absent(
            source_key="staging/source",
            destination_key="masters/destination",
            content_type="image/png",
            metadata={},
        )


@pytest.mark.asyncio
async def test_head_and_signing_errors_are_wrapped() -> None:
    failure = client_error(500, "InternalError")
    store = s3_store()
    original_client = store.client
    store.client = FakeS3Client(head_error=failure, signing_error=failure)
    original_client.close()

    with pytest.raises(ObjectStoreError):
        await store.head("object")
    with pytest.raises(ObjectStoreError):
        await store.presign_upload(
            key="staging/object",
            content_type="image/png",
            metadata={},
            expires_in=600,
            max_bytes=100,
        )
    with pytest.raises(ObjectStoreError):
        await store.presign_download(key="masters/object", expires_in=600)
    with pytest.raises(ObjectStoreError):
        await store.ping()


@pytest.mark.asyncio
async def test_get_error_is_wrapped() -> None:
    store = s3_store()
    original_client = store.client
    store.client = FakeS3Client(get_error=client_error(500, "InternalError"))
    original_client.close()

    with pytest.raises(ObjectStoreError):
        await store.read_bytes("staging/object", max_bytes=100)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "code", "expected_error"),
    [
        (404, "NoSuchKey", ObjectNotFoundError),
        (404, "NoSuchVersion", ObjectNotFoundError),
        (412, "PreconditionFailed", ObjectConflictError),
    ],
)
async def test_exact_read_errors_are_classified(
    status_code: int,
    code: str,
    expected_error: type[Exception],
) -> None:
    store = s3_store()
    original_client = store.client
    store.client = FakeS3Client(get_error=client_error(status_code, code))
    original_client.close()

    with pytest.raises(expected_error):
        await store.read_bytes(
            "staging/object",
            max_bytes=100,
            version_id="version-1",
        )


@pytest.mark.parametrize(
    ("access_key_id", "secret_access_key", "session_token"),
    [
        ("", "secret", None),
        ("access", " ", None),
        ("access", "secret", "\ttoken"),
    ],
)
def test_s3_client_rejects_empty_or_untrimmed_explicit_credentials(
    access_key_id: str,
    secret_access_key: str,
    session_token: str | None,
) -> None:
    with pytest.raises(ValueError, match="visible text"):
        S3ObjectStore(
            bucket="private-assets",
            region="us-east-1",
            access_key_id=access_key_id,
            secret_access_key=secret_access_key,
            session_token=session_token,
        )


@pytest.mark.asyncio
async def test_build_object_store_handles_disabled_and_explicit_credentials() -> None:
    assert (
        build_object_store(
            Settings(
                environment=Environment.TEST,
                session_secret="test-session-secret-with-more-than-32-characters",  # noqa: S106
            )
        )
        is None
    )
    configured = build_object_store(
        Settings(
            environment=Environment.TEST,
            session_secret="test-session-secret-with-more-than-32-characters",  # noqa: S106
            storage_enabled=True,
            storage_bucket="private-assets",
            storage_access_key_id="test-access-key",
            storage_secret_access_key="test-secret-key",  # noqa: S106
        )
    )
    assert configured is not None
    assert configured.bucket == "private-assets"
    await configured.close()


@pytest.mark.asyncio
async def test_build_object_store_passes_temporary_session_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    fake_client = FakeS3Client()

    def client_factory(**kwargs: Any) -> FakeS3Client:
        captured.update(kwargs)
        return fake_client

    monkeypatch.setattr("gen_automation.storage.s3.boto3.client", client_factory)
    token = "temporary-session-token-for-storage"  # noqa: S105
    configured = build_object_store(
        Settings(
            environment=Environment.TEST,
            storage_enabled=True,
            storage_bucket="private-assets",
            storage_access_key_id="temporary-access-key",
            storage_secret_access_key="temporary-secret-key",  # noqa: S106
            storage_session_token=token,
        )
    )
    assert configured is not None
    assert captured["aws_access_key_id"] == "temporary-access-key"
    assert captured["aws_secret_access_key"] == "temporary-secret-key"  # noqa: S105
    assert captured["aws_session_token"] == token
    assert token not in repr(configured)
    assert "session_token" not in configured.__dict__
    await configured.close()


@pytest.mark.asyncio
async def test_s3_client_preserves_ambient_identity_when_credentials_are_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    fake_client = FakeS3Client()

    def client_factory(**kwargs: Any) -> FakeS3Client:
        captured.update(kwargs)
        return fake_client

    monkeypatch.setattr("gen_automation.storage.s3.boto3.client", client_factory)
    store = S3ObjectStore(bucket="private-assets", region="us-east-1")
    assert "aws_access_key_id" not in captured
    assert "aws_secret_access_key" not in captured
    assert "aws_session_token" not in captured
    await store.close()
