import hashlib
import json
from uuid import uuid4

import httpx2
import pytest

from gen_automation.integrations.civitai import CivitaiClient, CivitaiModelType
from gen_automation.integrations.civitai.models import (
    CivitaiFileScan,
    CivitaiLicenseTerms,
    CivitaiResolvedLora,
)
from gen_automation.storage.base import (
    MultipartPart,
    MultipartUpload,
    ObjectStoreError,
    PresignedUpload,
)
from gen_automation.storage.memory import MemoryObjectStore
from gen_automation.storage.model_artifacts import (
    FINAL_CONTENT_TYPE,
    FINAL_KEY_PREFIX,
    MAX_MANAGED_LORA_BYTES,
    QUARANTINE_CONTENT_TYPE,
    ModelArtifactIntegrityError,
    ModelArtifactStore,
    ModelArtifactValidationError,
)


def safetensors_bytes(
    *,
    data: bytes = b"\x00\x00\x00\x00",
    tensor_name: str = "lora_A.weight",
    metadata: dict[str, str] | None = None,
) -> bytes:
    header = json.dumps(
        {
            "__metadata__": metadata or {"format": "pt"},
            tensor_name: {
                "dtype": "F32",
                "shape": [1],
                "data_offsets": [0, len(data)],
            },
        },
        separators=(",", ":"),
    ).encode()
    return len(header).to_bytes(8, "little") + header + data


def quarantine_key(identifier: str) -> str:
    return f"onboarding/loras/{identifier}/source.safetensors"


def resolved(body: bytes, *, expected_sha256: str | None = None) -> CivitaiResolvedLora:
    return CivitaiResolvedLora(
        model_id=123,
        version_id=456,
        file_id=789,
        model_type=CivitaiModelType.LORA,
        model_name="Creator LoRA",
        version_name="V1",
        target_filename="Creator-LoRA.safetensors",
        canonical_source_url="https://civitai.com/models/123?modelVersionId=456",
        creator="creator",
        base_model="SDXL 1.0",
        trained_words=("test-style",),
        declared_size_bytes=len(body),
        sha256=expected_sha256 or hashlib.sha256(body).hexdigest(),
        scan=CivitaiFileScan(pickle_result="Success", virus_result="Success"),
        license_terms=CivitaiLicenseTerms(
            allow_no_credit=False,
            commercial_use=("Image",),
            allow_derivatives=True,
            allow_different_license=False,
        ),
        nsfw=True,
        nsfw_level=4,
        _download_url="https://civitai.com/api/download/models/456",
    )


@pytest.mark.asyncio
async def test_quarantine_upload_is_short_lived_bounded_and_repr_redacted() -> None:
    store = MemoryObjectStore(bucket="managed-models")
    manager = ModelArtifactStore(store)
    identifier = uuid4()

    upload = await manager.create_quarantine_upload(
        upload_id=identifier,
        filename="My LoRA.safetensors",
        expires_in=600,
    )

    assert upload.key == quarantine_key(str(identifier))
    assert upload.max_bytes == MAX_MANAGED_LORA_BYTES
    assert upload.grant.fields["content-length-range"] == f"1,{MAX_MANAGED_LORA_BYTES}"
    assert upload.grant.fields["Content-Type"] == QUARANTINE_CONTENT_TYPE
    rendered = repr(upload) + repr(upload.grant)
    assert "memory://" not in rendered
    assert "expires=" not in rendered

    secret = "sensitive-signature-value"  # noqa: S105
    signed = PresignedUpload(
        url=f"https://bucket.example/upload?signature={secret}",
        method="POST",
        fields={"policy": secret},
        headers={"authorization": secret},
    )
    assert secret not in repr(signed)


@pytest.mark.asyncio
async def test_manual_upload_hashes_exact_version_validates_header_and_promotes() -> None:
    store = MemoryObjectStore(bucket="managed-models")
    manager = ModelArtifactStore(store)
    identifier = str(uuid4())
    source_key = quarantine_key(identifier)
    body = safetensors_bytes()
    store.put_for_test(
        source_key,
        body,
        content_type=QUARANTINE_CONTENT_TYPE,
        metadata={"upload-id": identifier},
    )
    source = await store.head(source_key)
    assert source is not None and source.version_id is not None and source.etag is not None

    result = await manager.promote_quarantine(
        quarantine_key=source_key,
        version_id=source.version_id,
        etag=source.etag,
        target_filename="My LoRA.safetensors",
        expected_sha256=hashlib.sha256(body).hexdigest(),
        expected_size_bytes=len(body),
        provenance={"provider": "manual", "approval": "operator-reviewed"},
    )

    expected_hash = hashlib.sha256(body).hexdigest()
    assert result.key == f"{FINAL_KEY_PREFIX}/{expected_hash}.safetensors"
    assert result.bucket == "managed-models"
    assert result.sha256 == expected_hash
    assert result.size_bytes == len(body)
    assert result.target_filename == "My LoRA.safetensors"
    assert result.provenance["provider"] == "manual"
    assert await store.head(source_key, version_id=source.version_id) is not None
    stored = await store.read_bytes(
        result.key,
        max_bytes=len(body),
        version_id=result.version_id,
        etag=result.etag,
    )
    assert stored == body
    await manager.delete_quarantine_exact(
        key=source_key,
        version_id=source.version_id,
    )
    assert await store.head(source_key, version_id=source.version_id) is None


@pytest.mark.asyncio
async def test_manual_upload_accepts_bounded_training_metadata() -> None:
    store = MemoryObjectStore(bucket="managed-models")
    manager = ModelArtifactStore(store)
    identifier = str(uuid4())
    source_key = quarantine_key(identifier)
    body = safetensors_bytes(metadata={"ss_datasets": "x" * 13_787})
    store.put_for_test(source_key, body, content_type=QUARANTINE_CONTENT_TYPE)
    source = await store.head(source_key)
    assert source is not None and source.version_id is not None

    result = await manager.promote_quarantine(
        quarantine_key=source_key,
        version_id=source.version_id,
        etag=source.etag,
        target_filename="Metadata Heavy LoRA.safetensors",
    )

    assert result.sha256 == hashlib.sha256(body).hexdigest()


@pytest.mark.asyncio
async def test_manual_promotion_deduplicates_content_and_retains_each_quarantine() -> None:
    store = MemoryObjectStore()
    manager = ModelArtifactStore(store)
    body = safetensors_bytes()
    results = []
    for index in range(2):
        identifier = str(uuid4())
        key = quarantine_key(identifier)
        store.put_for_test(key, body, content_type=QUARANTINE_CONTENT_TYPE)
        source = await store.head(key)
        assert source is not None and source.version_id is not None
        results.append(
            await manager.promote_quarantine(
                quarantine_key=key,
                version_id=source.version_id,
                etag=source.etag,
                target_filename=f"Duplicate {index}.safetensors",
            )
        )
        assert await store.head(key, version_id=source.version_id) is not None
        await manager.delete_quarantine_exact(
            key=key,
            version_id=source.version_id,
        )
        assert await store.head(key, version_id=source.version_id) is None

    assert results[0].key == results[1].key
    assert results[0].version_id == results[1].version_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "expected_sha256"),
    [
        (b"not-a-safetensors-file", None),
        (safetensors_bytes(tensor_name="model.weight"), None),
        (safetensors_bytes(), "0" * 64),
    ],
)
async def test_manual_invalid_header_or_hash_never_promotes_or_deletes_source(
    body: bytes,
    expected_sha256: str | None,
) -> None:
    store = MemoryObjectStore()
    manager = ModelArtifactStore(store)
    identifier = str(uuid4())
    key = quarantine_key(identifier)
    store.put_for_test(key, body, content_type=QUARANTINE_CONTENT_TYPE)
    source = await store.head(key)
    assert source is not None and source.version_id is not None

    expected_error = (
        ModelArtifactIntegrityError if expected_sha256 is not None else ModelArtifactValidationError
    )
    with pytest.raises(expected_error):
        await manager.promote_quarantine(
            quarantine_key=key,
            version_id=source.version_id,
            etag=source.etag,
            target_filename="Invalid.safetensors",
            expected_sha256=expected_sha256,
        )

    assert await store.head(key, version_id=source.version_id) is not None
    assert not any(object_key.startswith(FINAL_KEY_PREFIX) for object_key in store.objects)


@pytest.mark.asyncio
async def test_manual_promotion_rejects_signed_url_provenance_before_storage_mutation() -> None:
    store = MemoryObjectStore()
    manager = ModelArtifactStore(store)
    with pytest.raises(ValueError, match="canonical HTTPS"):
        await manager.promote_quarantine(
            quarantine_key=quarantine_key(str(uuid4())),
            version_id="version-1",
            etag=None,
            target_filename="Rejected.safetensors",
            provenance={
                "source_url": "https://bucket.s3.amazonaws.com/object?X-Amz-Signature=secret"
            },
        )
    assert store.objects == {}


@pytest.mark.asyncio
async def test_civitai_stream_is_multipart_content_addressed_and_persists_no_handle() -> None:
    body = safetensors_bytes()
    requests: list[httpx2.Request] = []
    progress: list[int] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(200, content=body, request=request)

    async def record_progress(transferred: int) -> None:
        progress.append(transferred)

    store = MemoryObjectStore(bucket="managed-models")
    manager = ModelArtifactStore(store)
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as http_client:
        client = CivitaiClient(
            api_token="private-civitai-token",  # noqa: S106
            http_client=http_client,
        )
        result = await manager.ingest_civitai(
            resolved(body),
            client,
            provenance={"adult_use_attested": True},
            progress=record_progress,
        )

    digest = hashlib.sha256(body).hexdigest()
    assert result.key == f"{FINAL_KEY_PREFIX}/{digest}.safetensors"
    assert result.provenance["provider"] == "civitai"
    assert result.provenance["onboarding"] == {"adult_use_attested": True}
    rendered = json.dumps(result.provenance)
    assert "api/download" not in rendered
    assert "private-civitai-token" not in rendered
    assert requests[0].headers["authorization"] == "Bearer private-civitai-token"
    assert store._multipart_uploads == {}
    stored = await store.head(result.key, version_id=result.version_id)
    assert stored is not None
    assert stored.content_type == FINAL_CONTENT_TYPE
    assert progress == [len(body)]


class FailingPartStore(MemoryObjectStore):
    def __init__(self) -> None:
        super().__init__()
        self.abort_calls = 0

    async def upload_part(
        self,
        upload: MultipartUpload,
        *,
        part_number: int,
        body: bytes,
    ) -> MultipartPart:
        del upload, part_number, body
        raise ObjectStoreError("simulated multipart part failure")

    async def abort_multipart_upload(self, upload: MultipartUpload) -> None:
        self.abort_calls += 1
        await super().abort_multipart_upload(upload)


@pytest.mark.asyncio
async def test_civitai_part_failure_aborts_multipart_upload() -> None:
    body = safetensors_bytes()

    async def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=body, request=request)

    store = FailingPartStore()
    manager = ModelArtifactStore(store)
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as http_client:
        client = CivitaiClient(http_client=http_client)
        with pytest.raises(ObjectStoreError):
            await manager.ingest_civitai(resolved(body), client)

    assert store.abort_calls == 1
    assert store._multipart_uploads == {}
    assert store.objects == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "expected_hash", "expected_error"),
    [
        (safetensors_bytes(), "0" * 64, ModelArtifactIntegrityError),
        (b"not-safetensors", None, ModelArtifactValidationError),
    ],
)
async def test_civitai_integrity_failure_aborts_before_completion(
    body: bytes,
    expected_hash: str | None,
    expected_error: type[Exception],
) -> None:
    async def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=body, request=request)

    store = MemoryObjectStore()
    manager = ModelArtifactStore(store)
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as http_client:
        client = CivitaiClient(http_client=http_client)
        with pytest.raises(expected_error):
            await manager.ingest_civitai(
                resolved(body, expected_sha256=expected_hash),
                client,
            )

    assert store._multipart_uploads == {}
    assert store.objects == {}


@pytest.mark.asyncio
async def test_exact_version_delete_is_scoped_to_managed_content_addressed_key() -> None:
    store = MemoryObjectStore()
    manager = ModelArtifactStore(store)
    body = safetensors_bytes()
    digest = hashlib.sha256(body).hexdigest()
    key = f"{FINAL_KEY_PREFIX}/{digest}.safetensors"
    store.put_for_test(
        key,
        body,
        content_type=FINAL_CONTENT_TYPE,
        metadata={
            "artifact-kind": "managed-lora",
            "format": "safetensors",
            "sha256": digest,
        },
    )
    metadata = await store.head(key)
    assert metadata is not None and metadata.version_id is not None

    await manager.delete_exact(key=key, version_id="wrong-version")
    assert await store.head(key) is not None
    with pytest.raises(ValueError, match="outside"):
        await manager.delete_exact(key="unrelated/object", version_id=metadata.version_id)

    await manager.delete_exact(key=key, version_id=metadata.version_id)
    assert await store.head(key) is None
