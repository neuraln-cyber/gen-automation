import hashlib
import json
from dataclasses import dataclass
from typing import Any

import httpx2
import pytest

from gen_automation import storage_conformance_cli
from gen_automation.services.storage_conformance import (
    CONFORMANCE_MAX_BYTES,
    CONFORMANCE_NAMESPACE,
    CONFORMANCE_OPT_IN_FLAG,
    ConformanceStepStatus,
    HttpxPresignedPostExecutor,
    PresignedPostExecutionError,
    PresignedPostResult,
    run_storage_conformance,
)
from gen_automation.storage.base import (
    ObjectAlreadyExistsError,
    ObjectMetadata,
    ObjectNotFoundError,
    ObjectStoreError,
    ObjectTooLargeError,
    PresignedUpload,
)


@dataclass(frozen=True)
class _StoredVersion:
    body: bytes
    content_type: str
    metadata: dict[str, str]
    version_id: str


class _VersionedS3Fake:
    backend = "s3"
    bucket = "private-conformance"

    def __init__(
        self,
        *,
        reject_duplicates: bool = True,
        omit_write_version: bool = False,
        fail_presigned_download: bool = False,
        fail_ping_with_secret: bool = False,
        fail_delete_count: int = 0,
    ) -> None:
        self.reject_duplicates = reject_duplicates
        self.omit_write_version = omit_write_version
        self.fail_presigned_download = fail_presigned_download
        self.fail_ping_with_secret = fail_ping_with_secret
        self.fail_delete_count = fail_delete_count
        self.versions: dict[str, list[_StoredVersion]] = {}
        self.counter = 0
        self.ping_calls = 0
        self.delete_calls: list[tuple[str, str | None]] = []
        self.closed = False

    async def ping(self) -> None:
        self.ping_calls += 1
        if self.fail_ping_with_secret:
            raise ObjectStoreError(
                "credential=TOP-SECRET signed=https://signed.example/?secret object=PNG-BYTES"
            )

    async def presign_upload(
        self,
        *,
        key: str,
        content_type: str,
        metadata: dict[str, str],
        expires_in: int,
        max_bytes: int,
    ) -> PresignedUpload:
        assert expires_in == 60
        assert max_bytes == CONFORMANCE_MAX_BYTES
        fields = {
            "key": key,
            "Content-Type": content_type,
            "policy": "SIGNED-POLICY-SECRET",
            "x-amz-algorithm": "AWS4-HMAC-SHA256",
            "x-amz-credential": "SIGNED-CREDENTIAL-SECRET",
            "x-amz-date": "20260728T000000Z",
            "x-amz-signature": "SIGNED-FORM-SECRET",
            "x-amz-server-side-encryption": "AES256",
        }
        fields.update({f"x-amz-meta-{name}": value for name, value in metadata.items()})
        return PresignedUpload(
            url="https://upload.example/private-signed-target",
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
        del key, download_name
        assert expires_in == 60
        assert version_id
        if self.fail_presigned_download:
            raise ObjectStoreError("https://download.example/?X-Amz-Signature=DOWNLOAD-SECRET")
        return (
            "https://download.example/object?"
            f"versionId={version_id}"
            "&X-Amz-Algorithm=AWS4-HMAC-SHA256"
            "&X-Amz-Credential=SIGNED-CREDENTIAL-SECRET"
            "&X-Amz-Date=20260728T000000Z"
            "&X-Amz-Expires=60"
            "&X-Amz-Signature=DOWNLOAD-SECRET"
        )

    async def head(self, key: str) -> ObjectMetadata | None:
        versions = self.versions.get(key, [])
        if not versions:
            return None
        return self._metadata(versions[-1])

    async def read_bytes(
        self,
        key: str,
        *,
        max_bytes: int,
        version_id: str | None = None,
        etag: str | None = None,
    ) -> bytes:
        for stored in self.versions.get(key, []):
            if stored.version_id == version_id:
                if etag is not None and etag != self._etag(stored.body):
                    raise ObjectNotFoundError("etag")
                if len(stored.body) > max_bytes:
                    raise ObjectTooLargeError(key)
                return stored.body
        raise ObjectNotFoundError(key)

    async def write_bytes_if_absent(
        self,
        *,
        key: str,
        body: bytes,
        content_type: str,
        metadata: dict[str, str],
        max_bytes: int,
    ) -> ObjectMetadata:
        if len(body) > max_bytes:
            raise ObjectTooLargeError(key)
        if self.reject_duplicates and self.versions.get(key):
            raise ObjectAlreadyExistsError(key)
        stored = self._put(
            key,
            body,
            content_type=content_type,
            metadata={
                **metadata,
                "sha256": hashlib.sha256(body).hexdigest(),
            },
        )
        result = self._metadata(stored)
        if self.omit_write_version:
            return ObjectMetadata(
                key=result.key,
                byte_size=result.byte_size,
                content_type=result.content_type,
                metadata=result.metadata,
                version_id=None,
                etag=result.etag,
            )
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
        source = self._find(source_key, source_version_id)
        if source_etag is not None and source_etag != self._etag(source.body):
            raise ObjectNotFoundError(source_key)
        if self.reject_duplicates and self.versions.get(destination_key):
            raise ObjectAlreadyExistsError(destination_key)
        return self._metadata(
            self._put(
                destination_key,
                source.body,
                content_type=content_type,
                metadata=dict(metadata),
            )
        )

    async def delete(self, key: str, *, version_id: str | None = None) -> None:
        self.delete_calls.append((key, version_id))
        if self.fail_delete_count:
            self.fail_delete_count -= 1
            raise ObjectStoreError("DELETE-CREDENTIAL-SECRET")
        if version_id is None:
            raise AssertionError("conformance cleanup was not version-pinned")
        versions = self.versions.get(key, [])
        for index, stored in enumerate(versions):
            if stored.version_id == version_id:
                del versions[index]
                if not versions:
                    self.versions.pop(key, None)
                return
        raise ObjectNotFoundError(key)

    async def close(self) -> None:
        self.closed = True

    def post_object(
        self,
        *,
        key: str,
        body: bytes,
        content_type: str,
        metadata: dict[str, str],
    ) -> str:
        return self._put(
            key,
            body,
            content_type=content_type,
            metadata=metadata,
        ).version_id

    def _put(
        self,
        key: str,
        body: bytes,
        *,
        content_type: str,
        metadata: dict[str, str],
    ) -> _StoredVersion:
        self.counter += 1
        stored = _StoredVersion(
            body=body,
            content_type=content_type,
            metadata=dict(metadata),
            version_id=f"version-{self.counter}",
        )
        self.versions.setdefault(key, []).append(stored)
        return stored

    def _find(self, key: str, version_id: str | None) -> _StoredVersion:
        for stored in self.versions.get(key, []):
            if stored.version_id == version_id:
                return stored
        raise ObjectNotFoundError(key)

    def _metadata(self, stored: _StoredVersion) -> ObjectMetadata:
        key = next(
            key
            for key, versions in self.versions.items()
            if any(candidate is stored for candidate in versions)
        )
        return ObjectMetadata(
            key=key,
            byte_size=len(stored.body),
            content_type=stored.content_type,
            metadata=dict(stored.metadata),
            version_id=stored.version_id,
            etag=self._etag(stored.body),
        )

    @staticmethod
    def _etag(body: bytes) -> str:
        return hashlib.md5(body, usedforsecurity=False).hexdigest()


class _SensitiveHeaderS3Fake(_VersionedS3Fake):
    async def presign_upload(
        self,
        *,
        key: str,
        content_type: str,
        metadata: dict[str, str],
        expires_in: int,
        max_bytes: int,
    ) -> PresignedUpload:
        grant = await super().presign_upload(
            key=key,
            content_type=content_type,
            metadata=metadata,
            expires_in=expires_in,
            max_bytes=max_bytes,
        )
        return PresignedUpload(
            url=grant.url,
            method=grant.method,
            fields=grant.fields,
            headers={"Authorization": "Bearer HEADER-SECRET"},
        )


class _NoOpDeleteS3Fake(_VersionedS3Fake):
    async def delete(self, key: str, *, version_id: str | None = None) -> None:
        self.delete_calls.append((key, version_id))
        if version_id is None:
            raise AssertionError("conformance cleanup was not version-pinned")


class _InvalidDownloadS3Fake(_VersionedS3Fake):
    def __init__(self, download_url: str) -> None:
        super().__init__()
        self.download_url = download_url

    async def presign_download(
        self,
        *,
        key: str,
        expires_in: int,
        download_name: str | None = None,
        version_id: str | None = None,
    ) -> str:
        del key, download_name
        assert expires_in == 60
        assert version_id
        return self.download_url.replace("{version_id}", version_id)


@dataclass
class _FakePostExecutor:
    store: _VersionedS3Fake
    fail_after_upload: bool = False

    async def upload(
        self,
        *,
        grant: PresignedUpload,
        body: bytes,
        content_type: str,
    ) -> PresignedPostResult:
        metadata = {
            name.removeprefix("x-amz-meta-"): value
            for name, value in grant.fields.items()
            if name.startswith("x-amz-meta-")
        }
        version_id = self.store.post_object(
            key=grant.fields["key"],
            body=body,
            content_type=content_type,
            metadata=metadata,
        )
        if self.fail_after_upload:
            raise PresignedPostExecutionError("SIGNED-FORM-SECRET")
        return PresignedPostResult(version_id=version_id)


@dataclass
class _MismatchedVersionPostExecutor:
    store: _VersionedS3Fake

    async def upload(
        self,
        *,
        grant: PresignedUpload,
        body: bytes,
        content_type: str,
    ) -> PresignedPostResult:
        metadata = {
            name.removeprefix("x-amz-meta-"): value
            for name, value in grant.fields.items()
            if name.startswith("x-amz-meta-")
        }
        self.store.post_object(
            key=grant.fields["key"],
            body=body,
            content_type=content_type,
            metadata=metadata,
        )
        return PresignedPostResult(version_id="untrusted-response-version")


@dataclass
class _AmbiguousForeignLatestPostExecutor:
    store: _VersionedS3Fake

    async def upload(
        self,
        *,
        grant: PresignedUpload,
        body: bytes,
        content_type: str,
    ) -> PresignedPostResult:
        metadata = {
            name.removeprefix("x-amz-meta-"): value
            for name, value in grant.fields.items()
            if name.startswith("x-amz-meta-")
        }
        key = grant.fields["key"]
        self.store.post_object(
            key=key,
            body=body,
            content_type=content_type,
            metadata=metadata,
        )
        self.store.post_object(
            key=key,
            body=body,
            content_type=content_type,
            metadata={**metadata, "conformance-run": "f" * 64},
        )
        raise PresignedPostExecutionError("SIGNED-FORM-SECRET")


@dataclass
class _AmbiguousWrongBytesPostExecutor:
    store: _VersionedS3Fake

    async def upload(
        self,
        *,
        grant: PresignedUpload,
        body: bytes,
        content_type: str,
    ) -> PresignedPostResult:
        metadata = {
            name.removeprefix("x-amz-meta-"): value
            for name, value in grant.fields.items()
            if name.startswith("x-amz-meta-")
        }
        wrong_body = bytes((body[0] ^ 1,)) + body[1:]
        self.store.post_object(
            key=grant.fields["key"],
            body=wrong_body,
            content_type=content_type,
            metadata=metadata,
        )
        raise PresignedPostExecutionError("SIGNED-FORM-SECRET")


@pytest.mark.asyncio
async def test_http_executor_uses_bounded_no_redirect_worker_multipart() -> None:
    requests: list[httpx2.Request] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        request_body = await request.aread()
        assert request.method == "POST"
        assert "multipart/form-data" in request.headers["content-type"]
        assert b'filename="conformance.png"' in request_body
        return httpx2.Response(
            204,
            headers={"x-amz-version-id": "worker-version"},
            request=request,
        )

    store = _VersionedS3Fake()
    grant = await store.presign_upload(
        key=f"{CONFORMANCE_NAMESPACE}test/worker.png",
        content_type="image/png",
        metadata={
            "sha256": "a" * 64,
            "conformance-run": "b" * 64,
        },
        expires_in=60,
        max_bytes=CONFORMANCE_MAX_BYTES,
    )
    executor = HttpxPresignedPostExecutor(
        transport=httpx2.MockTransport(handler),
    )

    result = await executor.upload(
        grant=grant,
        body=b"bounded-image",
        content_type="image/png",
    )

    assert result.version_id == "worker-version"
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_http_executor_does_not_follow_redirects(
    caplog: pytest.LogCaptureFixture,
) -> None:
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(
            307,
            headers={"location": "https://redirect.example/SIGNED-SECRET"},
            request=request,
        )

    store = _VersionedS3Fake()
    grant = await store.presign_upload(
        key=f"{CONFORMANCE_NAMESPACE}test/worker.png",
        content_type="image/png",
        metadata={
            "sha256": "a" * 64,
            "conformance-run": "b" * 64,
        },
        expires_in=60,
        max_bytes=CONFORMANCE_MAX_BYTES,
    )
    executor = HttpxPresignedPostExecutor(
        transport=httpx2.MockTransport(handler),
    )

    with pytest.raises(PresignedPostExecutionError) as captured:
        await executor.upload(
            grant=grant,
            body=b"bounded-image",
            content_type="image/png",
        )

    assert len(requests) == 1
    assert "SIGNED-SECRET" not in str(captured.value)
    assert "SIGNED-SECRET" not in caplog.text


@pytest.mark.asyncio
async def test_complete_conformance_cleans_only_created_exact_versions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _VersionedS3Fake()
    monkeypatch.setattr(
        "gen_automation.services.storage_conformance.secrets.token_hex",
        lambda length: "a" * (length * 2),
    )

    report = await run_storage_conformance(
        store,
        confirmed=True,
        post_executor=_FakePostExecutor(store),
    )

    assert report.success is True
    assert report.failure_code is None
    assert report.exact_versions_deleted == 3
    assert report.worker_presigned_post_exercised is True
    assert store.versions == {}
    assert len(store.delete_calls) == 3
    expected_prefix = f"{CONFORMANCE_NAMESPACE}{'a' * 64}/"
    assert all(key.startswith(expected_prefix) for key, _ in store.delete_calls)
    assert all(version_id for _, version_id in store.delete_calls)
    assert all(step.status is ConformanceStepStatus.PASSED for step in report.steps)


@pytest.mark.asyncio
async def test_opt_in_and_s3_backend_are_required_before_any_storage_call() -> None:
    store = _VersionedS3Fake()

    missing_opt_in = await run_storage_conformance(
        store,
        confirmed=False,
        post_executor=_FakePostExecutor(store),
    )
    store.backend = "memory"
    wrong_backend = await run_storage_conformance(
        store,
        confirmed=True,
        post_executor=_FakePostExecutor(store),
    )

    assert missing_opt_in.failure_code == "explicit_opt_in_required"
    assert wrong_backend.failure_code == "s3_backend_required"
    assert store.ping_calls == 0
    assert store.versions == {}


@pytest.mark.asyncio
async def test_unversioned_storage_fails_closed_without_unpinned_delete() -> None:
    store = _VersionedS3Fake(omit_write_version=True)

    report = await run_storage_conformance(
        store,
        confirmed=True,
        post_executor=_FakePostExecutor(store),
    )

    assert report.success is False
    assert report.failure_code == "version_id_missing"
    assert report.exact_versions_deleted == 0
    assert store.delete_calls == []
    assert len(store.versions) == 1


@pytest.mark.asyncio
async def test_duplicate_write_acceptance_is_a_failure_and_both_versions_are_cleaned() -> None:
    store = _VersionedS3Fake(reject_duplicates=False)

    report = await run_storage_conformance(
        store,
        confirmed=True,
        post_executor=_FakePostExecutor(store),
    )

    assert report.success is False
    assert report.failure_code == "duplicate_write_was_not_rejected"
    assert report.exact_versions_deleted == 2
    assert len({version_id for _, version_id in store.delete_calls}) == 2
    assert store.versions == {}


@pytest.mark.asyncio
async def test_partial_failure_cleans_prior_exact_versions_without_listing() -> None:
    store = _VersionedS3Fake(fail_presigned_download=True)

    report = await run_storage_conformance(
        store,
        confirmed=True,
        post_executor=_FakePostExecutor(store),
    )

    assert report.success is False
    assert report.failure_code == "storage_operation_failed"
    assert report.exact_versions_deleted == 2
    assert len(store.delete_calls) == 2
    assert store.versions == {}


@pytest.mark.asyncio
async def test_post_failure_recovers_created_version_and_cleanup_continues() -> None:
    store = _VersionedS3Fake()

    report = await run_storage_conformance(
        store,
        confirmed=True,
        post_executor=_FakePostExecutor(store, fail_after_upload=True),
    )

    assert report.success is False
    assert report.failure_code == "worker_presigned_post_failed"
    assert report.exact_versions_deleted == 3
    assert store.versions == {}
    assert all(version_id is not None for _, version_id in store.delete_calls)


@pytest.mark.asyncio
async def test_untrusted_post_response_version_is_never_cleanup_eligible() -> None:
    store = _VersionedS3Fake()

    report = await run_storage_conformance(
        store,
        confirmed=True,
        post_executor=_MismatchedVersionPostExecutor(store),
    )

    assert report.success is False
    assert report.failure_code == "worker_post_version_changed"
    assert report.exact_versions_deleted == 3
    assert "untrusted-response-version" not in {version_id for _, version_id in store.delete_calls}
    assert store.versions == {}


@pytest.mark.asyncio
async def test_ambiguous_post_never_deletes_an_unattributed_latest_version() -> None:
    store = _VersionedS3Fake()

    report = await run_storage_conformance(
        store,
        confirmed=True,
        post_executor=_AmbiguousForeignLatestPostExecutor(store),
    )

    assert report.success is False
    assert report.failure_code == "worker_presigned_post_failed"
    assert report.exact_versions_deleted == 2
    assert len(store.versions) == 1
    worker_versions = next(iter(store.versions.values()))
    assert len(worker_versions) == 2
    assert not {version.version_id for version in worker_versions}.intersection(
        version_id for _, version_id in store.delete_calls
    )


@pytest.mark.asyncio
async def test_ambiguous_post_cleanup_requires_exact_version_byte_hash() -> None:
    store = _VersionedS3Fake()

    report = await run_storage_conformance(
        store,
        confirmed=True,
        post_executor=_AmbiguousWrongBytesPostExecutor(store),
    )

    assert report.success is False
    assert report.failure_code == "worker_presigned_post_failed"
    assert report.exact_versions_deleted == 2
    assert len(store.versions) == 1
    worker_versions = next(iter(store.versions.values()))
    assert len(worker_versions) == 1
    assert worker_versions[0].version_id not in {version_id for _, version_id in store.delete_calls}


@pytest.mark.asyncio
async def test_presigned_post_grant_rejects_sensitive_forwarded_headers() -> None:
    store = _SensitiveHeaderS3Fake()

    report = await run_storage_conformance(
        store,
        confirmed=True,
        post_executor=_FakePostExecutor(store),
    )
    serialized = json.dumps(report.to_safe_dict(), sort_keys=True)

    assert report.success is False
    assert report.failure_code == "worker_post_grant_invalid"
    assert report.worker_presigned_post_exercised is False
    assert report.exact_versions_deleted == 2
    assert store.versions == {}
    assert "HEADER-SECRET" not in serialized


@pytest.mark.asyncio
async def test_cleanup_failure_is_reported_and_other_versions_are_still_attempted() -> None:
    store = _VersionedS3Fake(
        fail_presigned_download=True,
        fail_delete_count=1,
    )

    report = await run_storage_conformance(
        store,
        confirmed=True,
        post_executor=_FakePostExecutor(store),
    )

    assert report.success is False
    assert report.failure_code == "storage_operation_failed"
    assert report.exact_versions_deleted == 1
    assert len(store.delete_calls) == 2
    assert report.steps[-1].code == "exact_version_cleanup_failed"


@pytest.mark.asyncio
async def test_cleanup_verifies_that_each_exact_version_is_actually_gone() -> None:
    store = _NoOpDeleteS3Fake(fail_presigned_download=True)

    report = await run_storage_conformance(
        store,
        confirmed=True,
        post_executor=_FakePostExecutor(store),
    )

    assert report.success is False
    assert report.failure_code == "storage_operation_failed"
    assert report.exact_versions_deleted == 0
    assert len(store.delete_calls) == 2
    assert len(store.versions) == 2
    assert report.steps[-1].code == "exact_version_cleanup_failed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "download_url",
    [
        "https://download.example/object?versionId={version_id}&unsigned=true",
        (
            "https://download.example/object?versionId={version_id}"
            "&VersionId={version_id}"
            "&X-Amz-Algorithm=AWS4-HMAC-SHA256"
            "&X-Amz-Credential=credential"
            "&X-Amz-Date=20260728T000000Z"
            "&X-Amz-Expires=60"
            "&X-Amz-Signature=signature"
        ),
    ],
)
async def test_download_grant_requires_sigv4_and_one_exact_version(
    download_url: str,
) -> None:
    store = _InvalidDownloadS3Fake(download_url)

    report = await run_storage_conformance(
        store,
        confirmed=True,
        post_executor=_FakePostExecutor(store),
    )

    assert report.success is False
    assert report.failure_code in {
        "presigned_url_invalid",
        "presigned_download_not_version_pinned",
    }
    assert report.exact_versions_deleted == 2
    assert store.versions == {}


@pytest.mark.asyncio
async def test_report_redacts_storage_errors_urls_form_secrets_and_bytes() -> None:
    store = _VersionedS3Fake(fail_ping_with_secret=True)

    report = await run_storage_conformance(
        store,
        confirmed=True,
        post_executor=_FakePostExecutor(store),
    )
    serialized = json.dumps(report.to_safe_dict(), sort_keys=True)

    assert report.failure_code == "storage_operation_failed"
    for secret in (
        "TOP-SECRET",
        "SIGNED-POLICY-SECRET",
        "SIGNED-FORM-SECRET",
        "DOWNLOAD-SECRET",
        "PNG-BYTES",
        "private-conformance",
    ):
        assert secret not in serialized
    assert CONFORMANCE_NAMESPACE not in serialized


def test_cli_requires_exact_opt_in_before_loading_configuration(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def configuration_must_not_load() -> Any:
        raise AssertionError("configuration loaded without explicit opt-in")

    monkeypatch.setattr(storage_conformance_cli, "Settings", configuration_must_not_load)

    exit_code = storage_conformance_cli.storage_conformance_main([])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert captured.out == ""
    report = json.loads(captured.err)
    assert report["failure_code"] == "explicit_opt_in_required"


def test_cli_wrong_storage_configuration_is_redacted_and_nonzero(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake_settings = object()
    monkeypatch.setattr(storage_conformance_cli, "Settings", lambda: fake_settings)
    monkeypatch.setattr(
        storage_conformance_cli,
        "build_object_store",
        lambda settings: None,
    )

    exit_code = storage_conformance_cli.storage_conformance_main([CONFORMANCE_OPT_IN_FLAG])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert captured.out == ""
    report = json.loads(captured.err)
    assert report["failure_code"] == "storage_not_enabled"
    assert "fake_settings" not in captured.err


def test_cli_never_prints_live_failure_details(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = _VersionedS3Fake(fail_ping_with_secret=True)
    monkeypatch.setattr(storage_conformance_cli, "Settings", lambda: object())
    monkeypatch.setattr(
        storage_conformance_cli,
        "build_object_store",
        lambda settings: store,
    )

    exit_code = storage_conformance_cli.storage_conformance_main([CONFORMANCE_OPT_IN_FLAG])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert store.closed is True
    for secret in ("TOP-SECRET", "signed.example", "PNG-BYTES", store.bucket):
        assert secret not in captured.err
