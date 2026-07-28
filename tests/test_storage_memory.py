import hashlib

import pytest

from gen_automation.storage.base import (
    ObjectAlreadyExistsError,
    ObjectNotFoundError,
    ObjectTooLargeError,
)
from gen_automation.storage.memory import MemoryObjectStore


@pytest.mark.asyncio
async def test_memory_store_enforces_versions_etags_and_limits() -> None:
    store = MemoryObjectStore()
    await store.ping()
    assert await store.head("missing") is None
    with pytest.raises(ObjectNotFoundError):
        await store.presign_download(key="missing", expires_in=60)
    with pytest.raises(ObjectNotFoundError):
        await store.read_bytes("missing", max_bytes=10)
    with pytest.raises(ObjectNotFoundError):
        await store.copy_if_absent(
            source_key="missing",
            destination_key="destination",
            content_type="image/png",
            metadata={},
        )

    store.put_for_test("source", b"source bytes")
    source = await store.head("source")
    assert source is not None
    with pytest.raises(ObjectNotFoundError):
        await store.read_bytes(
            "source",
            max_bytes=100,
            version_id="wrong-version",
        )
    with pytest.raises(ObjectNotFoundError):
        await store.read_bytes("source", max_bytes=100, etag="wrong-etag")
    with pytest.raises(ObjectTooLargeError):
        await store.read_bytes("source", max_bytes=1)
    with pytest.raises(ObjectNotFoundError):
        await store.copy_if_absent(
            source_key="source",
            destination_key="destination",
            content_type="image/png",
            metadata={},
            source_version_id="wrong-version",
        )
    with pytest.raises(ObjectNotFoundError):
        await store.copy_if_absent(
            source_key="source",
            destination_key="destination",
            content_type="image/png",
            metadata={},
            source_etag="wrong-etag",
        )

    await store.copy_if_absent(
        source_key="source",
        destination_key="destination",
        content_type="image/png",
        metadata={"verified": "true"},
        source_version_id=source.version_id,
        source_etag=source.etag,
    )
    direct = await store.write_bytes_if_absent(
        key="derivative",
        body=b"derivative bytes",
        content_type="image/webp",
        metadata={"lineage": "v1"},
        max_bytes=100,
    )
    assert direct.byte_size == len(b"derivative bytes")
    assert direct.content_type == "image/webp"
    assert direct.metadata == {
        "lineage": "v1",
        "sha256": hashlib.sha256(b"derivative bytes").hexdigest(),
    }
    with pytest.raises(ObjectAlreadyExistsError):
        await store.write_bytes_if_absent(
            key="derivative",
            body=b"derivative bytes",
            content_type="image/webp",
            metadata={},
            max_bytes=100,
        )
    with pytest.raises(ObjectTooLargeError):
        await store.write_bytes_if_absent(
            key="oversized",
            body=b"derivative bytes",
            content_type="image/webp",
            metadata={},
            max_bytes=1,
        )
    with pytest.raises(ValueError, match="sha256"):
        await store.write_bytes_if_absent(
            key="bad-checksum",
            body=b"derivative bytes",
            content_type="image/webp",
            metadata={"sha256": "0" * 64},
            max_bytes=100,
        )
    with pytest.raises(ObjectAlreadyExistsError):
        await store.copy_if_absent(
            source_key="source",
            destination_key="destination",
            content_type="image/png",
            metadata={},
        )
    await store.delete("missing")
    await store.close()
