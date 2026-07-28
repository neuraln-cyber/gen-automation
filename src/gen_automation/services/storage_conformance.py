from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol
from urllib.parse import parse_qsl, urlsplit

import httpx2

from gen_automation.storage.base import (
    ObjectAlreadyExistsError,
    ObjectMetadata,
    ObjectNotFoundError,
    ObjectStore,
    ObjectStoreError,
    PresignedUpload,
)

CONFORMANCE_NAMESPACE = "conformance/"
CONFORMANCE_OPT_IN_FLAG = "--confirm-live-storage-conformance"
CONFORMANCE_CONTENT_TYPE = "image/png"
CONFORMANCE_MAX_BYTES = 4096
CONFORMANCE_PRESIGN_TTL_SECONDS = 60
_RUN_TOKEN_PATTERN = re.compile(r"[0-9a-f]{64}")
_PNG_PAYLOAD = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR"
    b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
    b"\x00\x00\x00\x0cIDATx\xdac\xf8\xcf\xc0\x00\x00\x03\x01\x01\x00"
    b"\xf7\x03AC\x00\x00\x00\x00IEND\xaeB`\x82"
)
_SAFE_STEP_NAMES = frozenset(
    {
        "configuration",
        "ping",
        "conditional_write",
        "duplicate_write",
        "exact_version_read",
        "immutable_copy",
        "duplicate_copy",
        "copy_exact_version_read",
        "presigned_download",
        "presigned_post_destination",
        "presigned_post_grant",
        "presigned_post_upload",
        "presigned_post_exact_version_read",
        "cleanup",
        "client_close",
    }
)


class ConformanceStepStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ConformanceStep:
    name: str
    status: ConformanceStepStatus
    code: str

    def __post_init__(self) -> None:
        if self.name not in _SAFE_STEP_NAMES:
            raise ValueError("storage conformance step name is invalid")
        if not isinstance(self.status, ConformanceStepStatus):
            raise ValueError("storage conformance step status is invalid")
        if not _is_machine_code(self.code):
            raise ValueError("storage conformance step code is invalid")

    def to_safe_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "status": self.status.value,
            "code": self.code,
        }


@dataclass(frozen=True, slots=True)
class StorageConformanceReport:
    success: bool
    backend: str
    steps: tuple[ConformanceStep, ...]
    failure_code: str | None
    exact_versions_deleted: int
    worker_presigned_post_exercised: bool

    def __post_init__(self) -> None:
        if self.backend not in {"s3", "unsupported", "unconfigured"}:
            raise ValueError("storage conformance backend report is invalid")
        if self.failure_code is not None and not _is_machine_code(self.failure_code):
            raise ValueError("storage conformance failure code is invalid")
        if (
            isinstance(self.exact_versions_deleted, bool)
            or not isinstance(self.exact_versions_deleted, int)
            or self.exact_versions_deleted < 0
        ):
            raise ValueError("storage conformance cleanup count is invalid")

    def to_safe_dict(self) -> dict[str, object]:
        return {
            "success": self.success,
            "backend": self.backend,
            "failure_code": self.failure_code,
            "exact_versions_deleted": self.exact_versions_deleted,
            "worker_presigned_post_exercised": self.worker_presigned_post_exercised,
            "steps": [step.to_safe_dict() for step in self.steps],
        }


@dataclass(frozen=True, slots=True)
class PresignedPostResult:
    version_id: str | None


class PresignedPostExecutor(Protocol):
    async def upload(
        self,
        *,
        grant: PresignedUpload,
        body: bytes,
        content_type: str,
    ) -> PresignedPostResult: ...


class PresignedPostExecutionError(Exception):
    """A redacted multipart-upload error safe for a conformance report."""


@dataclass(frozen=True, slots=True)
class HttpxPresignedPostExecutor:
    timeout_seconds: float = 15.0
    transport: httpx2.AsyncBaseTransport | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not 1.0 <= self.timeout_seconds <= 60.0
        ):
            raise ValueError("presigned POST timeout must be between 1 and 60 seconds")

    async def upload(
        self,
        *,
        grant: PresignedUpload,
        body: bytes,
        content_type: str,
    ) -> PresignedPostResult:
        _validate_presigned_post_grant(grant, expected_content_type=content_type)
        if not isinstance(body, bytes) or not 0 < len(body) <= CONFORMANCE_MAX_BYTES:
            raise PresignedPostExecutionError
        timeout = httpx2.Timeout(self.timeout_seconds, connect=min(5.0, self.timeout_seconds))
        limits = httpx2.Limits(max_connections=1, max_keepalive_connections=0)
        try:
            async with httpx2.AsyncClient(
                timeout=timeout,
                limits=limits,
                follow_redirects=False,
                trust_env=False,
                transport=self.transport,
            ) as client:
                async with client.stream(
                    "POST",
                    grant.url,
                    data=dict(grant.fields),
                    files={"file": ("conformance.png", body, content_type)},
                    headers=dict(grant.headers),
                ) as response:
                    if response.status_code not in {200, 201, 204}:
                        raise PresignedPostExecutionError
                    version_id = response.headers.get("x-amz-version-id")
        except PresignedPostExecutionError:
            raise
        except (httpx2.TimeoutException, httpx2.TransportError):
            raise PresignedPostExecutionError from None
        return PresignedPostResult(version_id=version_id)


class _ConformanceFailureError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class _CreatedVersion:
    key: str
    version_id: str


async def run_storage_conformance(
    store: ObjectStore,
    *,
    confirmed: bool,
    post_executor: PresignedPostExecutor | None = None,
) -> StorageConformanceReport:
    """Exercise the production object-store contract without listing storage."""

    if confirmed is not True:
        return storage_conformance_failure_report(
            backend="unconfigured",
            code="explicit_opt_in_required",
        )
    if (
        getattr(store, "backend", None) != "s3"
        or not isinstance(getattr(store, "bucket", None), str)
        or not store.bucket.strip()
    ):
        return storage_conformance_failure_report(
            backend="unsupported",
            code="s3_backend_required",
        )

    run_token = secrets.token_hex(32)
    if _RUN_TOKEN_PATTERN.fullmatch(run_token) is None:
        return storage_conformance_failure_report(
            backend="s3",
            code="secure_run_prefix_unavailable",
        )
    run_prefix = f"{CONFORMANCE_NAMESPACE}{run_token}/"
    run_marker = hashlib.sha256(run_prefix.encode("ascii")).hexdigest()
    direct_key = f"{run_prefix}conditional.png"
    copy_key = f"{run_prefix}immutable-copy.png"
    worker_key = f"{run_prefix}worker-post.png"
    payload = _PNG_PAYLOAD
    payload_sha256 = hashlib.sha256(payload).hexdigest()
    metadata = {
        "sha256": payload_sha256,
        "conformance-run": run_marker,
    }
    executor = post_executor or HttpxPresignedPostExecutor()
    steps: list[ConformanceStep] = []
    created_versions: list[_CreatedVersion] = []
    current_step = "configuration"
    failure_code: str | None = None
    worker_post_exercised = False

    try:
        current_step = "ping"
        await store.ping()
        _record_pass(steps, current_step, "bucket_reachable")

        current_step = "conditional_write"
        try:
            direct = await store.write_bytes_if_absent(
                key=direct_key,
                body=payload,
                content_type=CONFORMANCE_CONTENT_TYPE,
                metadata=metadata,
                max_bytes=CONFORMANCE_MAX_BYTES,
            )
            direct_version = await _verify_and_track_owned_version(
                store,
                direct,
                expected_key=direct_key,
                body=payload,
                metadata=metadata,
                created_versions=created_versions,
            )
        except _ConformanceFailureError as error:
            if error.code != "version_id_missing":
                await _capture_owned_version(
                    store,
                    key=direct_key,
                    body=payload,
                    metadata=metadata,
                    created_versions=created_versions,
                )
            raise
        except Exception:
            await _capture_owned_version(
                store,
                key=direct_key,
                body=payload,
                metadata=metadata,
                created_versions=created_versions,
            )
            raise
        _record_pass(steps, current_step, "bounded_conditional_write_verified")

        current_step = "duplicate_write"
        try:
            duplicate = await store.write_bytes_if_absent(
                key=direct_key,
                body=payload,
                content_type=CONFORMANCE_CONTENT_TYPE,
                metadata=metadata,
                max_bytes=CONFORMANCE_MAX_BYTES,
            )
        except ObjectAlreadyExistsError:
            _record_pass(steps, current_step, "duplicate_rejected")
        except Exception:
            await _capture_owned_version(
                store,
                key=direct_key,
                body=payload,
                metadata=metadata,
                created_versions=created_versions,
            )
            raise
        else:
            await _verify_and_track_owned_version(
                store,
                duplicate,
                expected_key=direct_key,
                body=payload,
                metadata=metadata,
                created_versions=created_versions,
            )
            raise _ConformanceFailureError("duplicate_write_was_not_rejected")

        current_step = "exact_version_read"
        direct_bytes = await store.read_bytes(
            direct_key,
            max_bytes=CONFORMANCE_MAX_BYTES,
            version_id=direct_version,
        )
        _verify_bytes(direct_bytes, payload_sha256)
        _record_pass(steps, current_step, "exact_version_bytes_verified")

        current_step = "immutable_copy"
        try:
            copied = await store.copy_if_absent(
                source_key=direct_key,
                destination_key=copy_key,
                content_type=CONFORMANCE_CONTENT_TYPE,
                metadata=metadata,
                source_version_id=direct_version,
                source_etag=direct.etag,
            )
            copy_version = await _verify_and_track_owned_version(
                store,
                copied,
                expected_key=copy_key,
                body=payload,
                metadata=metadata,
                created_versions=created_versions,
            )
        except _ConformanceFailureError as error:
            if error.code != "version_id_missing":
                await _capture_owned_version(
                    store,
                    key=copy_key,
                    body=payload,
                    metadata=metadata,
                    created_versions=created_versions,
                )
            raise
        except Exception:
            await _capture_owned_version(
                store,
                key=copy_key,
                body=payload,
                metadata=metadata,
                created_versions=created_versions,
            )
            raise
        _record_pass(steps, current_step, "version_pinned_copy_verified")

        current_step = "duplicate_copy"
        try:
            duplicate_copy = await store.copy_if_absent(
                source_key=direct_key,
                destination_key=copy_key,
                content_type=CONFORMANCE_CONTENT_TYPE,
                metadata=metadata,
                source_version_id=direct_version,
                source_etag=direct.etag,
            )
        except ObjectAlreadyExistsError:
            _record_pass(steps, current_step, "duplicate_copy_rejected")
        except Exception:
            await _capture_owned_version(
                store,
                key=copy_key,
                body=payload,
                metadata=metadata,
                created_versions=created_versions,
            )
            raise
        else:
            await _verify_and_track_owned_version(
                store,
                duplicate_copy,
                expected_key=copy_key,
                body=payload,
                metadata=metadata,
                created_versions=created_versions,
            )
            raise _ConformanceFailureError("duplicate_copy_was_not_rejected")

        current_step = "copy_exact_version_read"
        copied_bytes = await store.read_bytes(
            copy_key,
            max_bytes=CONFORMANCE_MAX_BYTES,
            version_id=copy_version,
        )
        _verify_bytes(copied_bytes, payload_sha256)
        _record_pass(steps, current_step, "copied_exact_version_bytes_verified")

        current_step = "presigned_download"
        download_url = await store.presign_download(
            key=copy_key,
            expires_in=CONFORMANCE_PRESIGN_TTL_SECONDS,
            download_name="conformance.png",
            version_id=copy_version,
        )
        _validate_signed_url(
            download_url,
            signature_in_form=False,
            expected_version_id=copy_version,
        )
        _record_pass(steps, current_step, "short_lived_version_pinned_url_created")

        current_step = "presigned_post_destination"
        if await store.head(worker_key) is not None:
            raise _ConformanceFailureError("worker_post_destination_not_empty")
        _record_pass(steps, current_step, "random_destination_empty")

        current_step = "presigned_post_grant"
        grant = await store.presign_upload(
            key=worker_key,
            content_type=CONFORMANCE_CONTENT_TYPE,
            metadata=metadata,
            expires_in=CONFORMANCE_PRESIGN_TTL_SECONDS,
            max_bytes=CONFORMANCE_MAX_BYTES,
        )
        _validate_presigned_post_grant(
            grant,
            expected_key=worker_key,
            expected_content_type=CONFORMANCE_CONTENT_TYPE,
            expected_metadata=metadata,
        )
        _record_pass(steps, current_step, "bounded_worker_post_grant_created")

        current_step = "presigned_post_upload"
        try:
            post_result = await executor.upload(
                grant=grant,
                body=payload,
                content_type=CONFORMANCE_CONTENT_TYPE,
            )
            worker_post_exercised = True
        except Exception:
            await _capture_owned_version(
                store,
                key=worker_key,
                body=payload,
                metadata=metadata,
                created_versions=created_versions,
            )
            raise
        if not isinstance(post_result, PresignedPostResult):
            await _capture_owned_version(
                store,
                key=worker_key,
                body=payload,
                metadata=metadata,
                created_versions=created_versions,
            )
            raise _ConformanceFailureError("worker_post_result_invalid")
        response_version = _optional_version_id(post_result.version_id)
        worker = await store.head(worker_key)
        if worker is None:
            raise _ConformanceFailureError("worker_post_object_missing")
        worker_version = await _verify_and_track_owned_version(
            store,
            worker,
            expected_key=worker_key,
            body=payload,
            metadata=metadata,
            created_versions=created_versions,
        )
        if response_version is not None and response_version != worker_version:
            raise _ConformanceFailureError("worker_post_version_changed")
        _record_pass(steps, current_step, "worker_style_post_verified")

        current_step = "presigned_post_exact_version_read"
        worker_bytes = await store.read_bytes(
            worker_key,
            max_bytes=CONFORMANCE_MAX_BYTES,
            version_id=worker_version,
        )
        _verify_bytes(worker_bytes, payload_sha256)
        _record_pass(steps, current_step, "worker_post_exact_version_bytes_verified")
    except _ConformanceFailureError as error:
        failure_code = error.code
        _record_failure(steps, current_step, error.code)
    except ObjectStoreError:
        failure_code = "storage_operation_failed"
        _record_failure(steps, current_step, failure_code)
    except PresignedPostExecutionError:
        failure_code = "worker_presigned_post_failed"
        _record_failure(steps, current_step, failure_code)
    except Exception:
        failure_code = "unexpected_conformance_failure"
        _record_failure(steps, current_step, failure_code)
    finally:
        deleted_count, cleanup_failed = await _cleanup_exact_versions(
            store,
            allowed_keys=frozenset((direct_key, copy_key, worker_key)),
            created_versions=created_versions,
        )
        if cleanup_failed:
            _record_failure(steps, "cleanup", "exact_version_cleanup_failed")
            if failure_code is None:
                failure_code = "exact_version_cleanup_failed"
        else:
            cleanup_code = "exact_versions_deleted" if deleted_count else "no_tracked_versions"
            _record_pass(steps, "cleanup", cleanup_code)

    return StorageConformanceReport(
        success=failure_code is None,
        backend="s3",
        steps=tuple(steps),
        failure_code=failure_code,
        exact_versions_deleted=deleted_count,
        worker_presigned_post_exercised=worker_post_exercised,
    )


def storage_conformance_failure_report(
    *,
    backend: str,
    code: str,
) -> StorageConformanceReport:
    return StorageConformanceReport(
        success=False,
        backend=backend,
        steps=(
            ConformanceStep(
                name="configuration",
                status=ConformanceStepStatus.FAILED,
                code=code,
            ),
        ),
        failure_code=code,
        exact_versions_deleted=0,
        worker_presigned_post_exercised=False,
    )


def report_with_client_close_failure(
    report: StorageConformanceReport,
) -> StorageConformanceReport:
    failure_code = report.failure_code or "storage_client_close_failed"
    return StorageConformanceReport(
        success=False,
        backend=report.backend,
        steps=(
            *report.steps,
            ConformanceStep(
                name="client_close",
                status=ConformanceStepStatus.FAILED,
                code="storage_client_close_failed",
            ),
        ),
        failure_code=failure_code,
        exact_versions_deleted=report.exact_versions_deleted,
        worker_presigned_post_exercised=report.worker_presigned_post_exercised,
    )


def _validate_owned_object_metadata(
    metadata_result: ObjectMetadata,
    *,
    expected_key: str,
    body: bytes,
    metadata: dict[str, str],
) -> str:
    version_id = _required_version_id(metadata_result.version_id)
    if (
        metadata_result.key != expected_key
        or metadata_result.byte_size != len(body)
        or metadata_result.content_type != CONFORMANCE_CONTENT_TYPE
        or metadata_result.metadata.get("sha256") != metadata["sha256"]
        or metadata_result.metadata.get("conformance-run") != metadata["conformance-run"]
    ):
        raise _ConformanceFailureError("object_metadata_verification_failed")
    return version_id


async def _verify_and_track_owned_version(
    store: ObjectStore,
    metadata_result: ObjectMetadata,
    *,
    expected_key: str,
    body: bytes,
    metadata: dict[str, str],
    created_versions: list[_CreatedVersion],
) -> str:
    """Attribute one exact version to this run before making it cleanup-eligible."""

    version_id = _validate_owned_object_metadata(
        metadata_result,
        expected_key=expected_key,
        body=body,
        metadata=metadata,
    )
    exact_bytes = await store.read_bytes(
        expected_key,
        max_bytes=CONFORMANCE_MAX_BYTES,
        version_id=version_id,
    )
    _verify_bytes(exact_bytes, metadata["sha256"])
    _track_version(
        created_versions,
        key=expected_key,
        version_id=version_id,
    )
    return version_id


async def _capture_owned_version(
    store: ObjectStore,
    *,
    key: str,
    body: bytes,
    metadata: dict[str, str],
    created_versions: list[_CreatedVersion],
) -> None:
    try:
        candidate = await store.head(key)
        if candidate is None:
            return
        await _verify_and_track_owned_version(
            store,
            candidate,
            expected_key=key,
            body=body,
            metadata=metadata,
            created_versions=created_versions,
        )
    except Exception:
        return


async def _cleanup_exact_versions(
    store: ObjectStore,
    *,
    allowed_keys: frozenset[str],
    created_versions: list[_CreatedVersion],
) -> tuple[int, bool]:
    deleted_count = 0
    cleanup_failed = False
    seen: set[tuple[str, str]] = set()
    for created in reversed(created_versions):
        identity = (created.key, created.version_id)
        if identity in seen:
            continue
        seen.add(identity)
        if created.key not in allowed_keys or _optional_version_id(created.version_id) is None:
            cleanup_failed = True
            continue
        try:
            await store.delete(
                created.key,
                version_id=created.version_id,
            )
        except Exception:
            cleanup_failed = True
            continue
        try:
            await store.read_bytes(
                created.key,
                max_bytes=CONFORMANCE_MAX_BYTES,
                version_id=created.version_id,
            )
        except ObjectNotFoundError:
            deleted_count += 1
        except Exception:
            cleanup_failed = True
        else:
            cleanup_failed = True
    return deleted_count, cleanup_failed


def _track_version(
    created_versions: list[_CreatedVersion],
    *,
    key: str,
    version_id: str,
) -> None:
    created = _CreatedVersion(key=key, version_id=version_id)
    if created not in created_versions:
        created_versions.append(created)


def _verify_bytes(value: bytes, expected_sha256: str) -> None:
    if (
        not isinstance(value, bytes)
        or not 0 < len(value) <= CONFORMANCE_MAX_BYTES
        or hashlib.sha256(value).hexdigest() != expected_sha256
    ):
        raise _ConformanceFailureError("exact_version_bytes_mismatch")


def _required_version_id(value: str | None) -> str:
    version_id = _optional_version_id(value)
    if version_id is None:
        raise _ConformanceFailureError("version_id_missing")
    return version_id


def _optional_version_id(value: str | None) -> str | None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
        or len(value) > 1024
        or value.strip().casefold() == "null"
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        return None
    return value


def _validate_presigned_post_grant(
    grant: PresignedUpload,
    *,
    expected_content_type: str,
    expected_key: str | None = None,
    expected_metadata: dict[str, str] | None = None,
) -> None:
    if (
        not isinstance(grant, PresignedUpload)
        or grant.method != "POST"
        or not 1 <= len(grant.fields) <= 64
        or len(grant.headers) > 32
    ):
        raise _ConformanceFailureError("worker_post_grant_invalid")
    _validate_signed_url(grant.url, signature_in_form=True)
    required_signature_fields = {
        "policy",
        "x-amz-algorithm",
        "x-amz-credential",
        "x-amz-date",
        "x-amz-signature",
    }
    if not required_signature_fields.issubset(grant.fields):
        raise _ConformanceFailureError("worker_post_grant_invalid")
    for name, value in grant.fields.items():
        if (
            not isinstance(name, str)
            or not name
            or len(name) > 256
            or not isinstance(value, str)
            or len(value) > 16_384
            or any(ord(character) < 32 or ord(character) == 127 for character in name)
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise _ConformanceFailureError("worker_post_grant_invalid")
    sensitive_headers = frozenset({"authorization", "cookie", "proxy-authorization"})
    for name, value in grant.headers.items():
        if (
            not isinstance(name, str)
            or not name
            or len(name) > 256
            or not isinstance(value, str)
            or len(value) > 16_384
            or name.casefold() in sensitive_headers
            or any(ord(character) < 32 or ord(character) == 127 for character in name)
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise _ConformanceFailureError("worker_post_grant_invalid")
    if expected_key is not None and grant.fields.get("key") != expected_key:
        raise _ConformanceFailureError("worker_post_grant_key_mismatch")
    if grant.fields.get("Content-Type") != expected_content_type:
        raise _ConformanceFailureError("worker_post_grant_content_type_mismatch")
    if grant.fields.get("x-amz-server-side-encryption") != "AES256":
        raise _ConformanceFailureError("worker_post_grant_encryption_mismatch")
    if expected_metadata is not None:
        for name, value in expected_metadata.items():
            if grant.fields.get(f"x-amz-meta-{name}") != value:
                raise _ConformanceFailureError("worker_post_grant_metadata_mismatch")


def _validate_signed_url(
    value: str,
    *,
    signature_in_form: bool,
    expected_version_id: str | None = None,
) -> None:
    if not isinstance(value, str) or not 9 <= len(value) <= 8192:
        raise _ConformanceFailureError("presigned_url_invalid")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or (not signature_in_form and not parsed.query)
    ):
        raise _ConformanceFailureError("presigned_url_invalid")
    if expected_version_id is not None:
        try:
            pairs = parse_qsl(
                parsed.query,
                keep_blank_values=True,
                max_num_fields=64,
            )
        except ValueError:
            raise _ConformanceFailureError("presigned_url_invalid") from None
        query: dict[str, list[str]] = {}
        for name, value in pairs:
            query.setdefault(name.casefold(), []).append(value)
        required_signature_fields = {
            "x-amz-algorithm",
            "x-amz-credential",
            "x-amz-date",
            "x-amz-expires",
            "x-amz-signature",
        }
        if any(
            len(query.get(name, [])) != 1 or not query[name][0]
            for name in required_signature_fields
        ):
            raise _ConformanceFailureError("presigned_url_invalid")
        if query["x-amz-algorithm"][0] != "AWS4-HMAC-SHA256" or query["x-amz-expires"][0] != str(
            CONFORMANCE_PRESIGN_TTL_SECONDS
        ):
            raise _ConformanceFailureError("presigned_url_invalid")
        version_values = query.get("versionid", [])
        if version_values != [expected_version_id]:
            raise _ConformanceFailureError("presigned_download_not_version_pinned")


def _record_pass(steps: list[ConformanceStep], name: str, code: str) -> None:
    steps.append(
        ConformanceStep(
            name=name,
            status=ConformanceStepStatus.PASSED,
            code=code,
        )
    )


def _record_failure(steps: list[ConformanceStep], name: str, code: str) -> None:
    steps.append(
        ConformanceStep(
            name=name,
            status=ConformanceStepStatus.FAILED,
            code=code,
        )
    )


def _is_machine_code(value: str) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 64
        and value.replace("_", "").isalnum()
        and value == value.casefold()
    )
