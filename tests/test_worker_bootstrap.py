import hashlib
import io
import json
import threading
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

import gen_automation.gpu_worker.bootstrap as worker_bootstrap
from gen_automation.domain.signing import derive_public_key, encode_base64url
from gen_automation.gpu_worker.artifacts import (
    ArtifactBootstrapError,
    ArtifactKind,
    ModelArtifactSpec,
    calculate_manifest_sha256,
)
from gen_automation.gpu_worker.bootstrap import (
    S3ArtifactDownloader,
    WorkerBootstrapConfigurationError,
    WorkerRuntimeSettings,
    ensure_model_roots,
    load_artifact_manifest,
    scrub_artifact_credentials,
)
from gen_automation.gpu_worker.models import WorkerEnvironment

WORKER_SIGNING_PRIVATE_KEY = encode_base64url(bytes(range(1, 33)))
WORKER_VERIFICATION_PUBLIC_KEY = derive_public_key(WORKER_SIGNING_PRIVATE_KEY)


def _safetensors() -> bytes:
    body = b"\x00\x00\x00\x00"
    header = json.dumps(
        {"weight": {"data_offsets": [0, len(body)], "dtype": "F32", "shape": [1]}},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return len(header).to_bytes(8, "little") + header + body


def _artifact(
    content: bytes | None = None,
    *,
    source_object_version_id: str | None = None,
) -> ModelArtifactSpec:
    value = content if content is not None else _safetensors()
    return ModelArtifactSpec(
        logical_name="illustrious",
        kind=ArtifactKind.CHECKPOINT,
        source_object_id="models/illustrious.safetensors",
        source_object_version_id=source_object_version_id,
        sha256=hashlib.sha256(value).hexdigest(),
        exact_size_bytes=len(value),
        max_size_bytes=len(value),
        target_filename="illustrious.safetensors",
    )


def _manifest_json(artifact: ModelArtifactSpec | None = None) -> str:
    entry = artifact if artifact is not None else _artifact()
    digest = calculate_manifest_sha256((entry,))
    return json.dumps(
        {
            "version": "v1",
            "artifacts": [entry.model_dump(mode="json")],
            "manifest_sha256": digest,
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def _settings(**changes: object) -> WorkerRuntimeSettings:
    values: dict[str, object] = {
        "environment": WorkerEnvironment.TEST,
        "verification_keys": {"key-1": WORKER_VERIFICATION_PUBLIC_KEY},
        "allowed_upload_origin": "https://uploads.example.test",
        "model_manifest_json": _manifest_json(),
        "model_manifest_sha256": "a" * 64,
        "runtime_admission_id": "1" * 32,
        "runtime_worker_instance_id": "instance-creator-1",
        "artifact_bucket": "models-private",
        "checkpoint_root": Path("/models/checkpoints"),
        "lora_root": Path("/models/loras"),
        "detector_root": Path("/models/ultralytics/bbox"),
        "comfy_python": Path("/opt/worker-venv/bin/python"),
        "comfy_main": Path("/comfy/main.py"),
    }
    values.update(changes)
    return WorkerRuntimeSettings.model_validate(values)


@pytest.mark.parametrize("timeout_seconds", [300.0, 301.0, 600.0])
def test_runtime_upload_timeout_remains_valid_in_worker_settings(timeout_seconds: float) -> None:
    settings = _settings(upload_timeout_seconds=timeout_seconds)
    assert settings.to_worker_settings().upload_timeout_seconds == timeout_seconds


def test_load_artifact_manifest_accepts_canonical_manifest() -> None:
    manifest = load_artifact_manifest(_manifest_json())

    assert manifest.version == "v1"
    assert manifest.artifacts[0].logical_name == "illustrious"
    assert manifest.manifest_sha256 == calculate_manifest_sha256(manifest.artifacts)


@pytest.mark.parametrize(
    "raw",
    [
        '{"version":"v1","version":"v1"}',
        '{"version":NaN}',
        "[]",
        "{" + '"padding":"' + ("x" * (256 * 1024)) + '"}',
    ],
    ids=["duplicate-key", "non-finite", "wrong-root", "oversized"],
)
def test_load_artifact_manifest_rejects_untrusted_json_without_echoing_it(raw: str) -> None:
    with pytest.raises(
        WorkerBootstrapConfigurationError,
        match="worker bootstrap configuration is invalid",
    ) as error:
        load_artifact_manifest(raw)

    assert "padding" not in str(error.value)
    assert "version" not in str(error.value)


def test_runtime_settings_reuse_worker_security_boundary() -> None:
    settings = _settings()

    worker = settings.to_worker_settings()

    assert worker.upload_origin == ("uploads.example.test", 443)
    assert worker.verification_keys == {"key-1": WORKER_VERIFICATION_PUBLIC_KEY}
    assert WORKER_SIGNING_PRIVATE_KEY not in settings.model_dump_json()
    assert WORKER_SIGNING_PRIVATE_KEY not in worker.model_dump_json()
    assert worker.approved_workflow_node_classes == settings.approved_workflow_node_classes
    assert worker.readiness_timeout_seconds == settings.readiness_timeout_seconds
    assert worker.max_outputs == 25
    assert settings.comfy_execution_timeout_seconds == 3600.0


def test_runtime_settings_require_https_storage_in_production() -> None:
    with pytest.raises(ValidationError, match="production artifact storage requires HTTPS"):
        _settings(
            environment=WorkerEnvironment.PRODUCTION,
            verification_keys={"key-1": WORKER_VERIFICATION_PUBLIC_KEY},
            artifact_endpoint_url="http://storage.internal",
        )


def test_runtime_settings_require_explicit_artifact_identity_in_production() -> None:
    with pytest.raises(ValidationError, match="explicit read-only identity"):
        _settings(
            environment=WorkerEnvironment.PRODUCTION,
            artifact_endpoint_url="https://storage.internal",
        )


def test_runtime_settings_reject_credential_mismatch() -> None:
    with pytest.raises(
        ValidationError,
        match="artifact access key ID and secret must be provided together",
    ):
        _settings(artifact_access_key_id="access-key")


def test_ensure_model_roots_creates_distinct_directories(tmp_path: Path) -> None:
    checkpoints = tmp_path / "models" / "checkpoints"
    loras = tmp_path / "models" / "loras"

    ensure_model_roots(checkpoints, loras)

    assert checkpoints.is_dir()
    assert loras.is_dir()


def test_ensure_model_roots_rejects_same_directory(tmp_path: Path) -> None:
    root = tmp_path / "models"

    with pytest.raises(WorkerBootstrapConfigurationError):
        ensure_model_roots(root, root)


class _Body(io.BytesIO):
    closed_by_adapter = False

    def close(self) -> None:
        self.closed_by_adapter = True
        super().close()


class _S3Client:
    def __init__(self, content: bytes, *, declared_size: int | None = None) -> None:
        self.content = content
        self.declared_size = len(content) if declared_size is None else declared_size
        self.body: _Body | None = None
        self.calls: list[dict[str, str]] = []
        self.closed = False

    def get_object(self, **parameters: str) -> dict[str, Any]:
        self.calls.append(parameters)
        self.body = _Body(self.content)
        return {"ContentLength": self.declared_size, "Body": self.body}

    def close(self) -> None:
        self.closed = True


class _CoordinatedBody(io.BytesIO):
    def __init__(self, content: bytes, *, client: "_RangedS3Client") -> None:
        super().__init__(content)
        self._client = client
        self._first_read = True
        self.closed_by_adapter = False

    def read(self, size: int = -1) -> bytes:
        if self._first_read:
            self._first_read = False
            with self._client.lock:
                self._client.active += 1
                self._client.peak = max(self._client.peak, self._client.active)
            try:
                self._client.barrier.wait(timeout=2)
            finally:
                with self._client.lock:
                    self._client.active -= 1
        return super().read(size)

    def close(self) -> None:
        self.closed_by_adapter = True
        super().close()


class _RangedS3Client:
    def __init__(self, content: bytes, *, first_range_truncated: bool = False) -> None:
        self.content = content
        self.first_range_truncated = first_range_truncated
        self.calls: list[dict[str, str]] = []
        self.bodies: list[_CoordinatedBody] = []
        self.range_attempts: dict[str, int] = {}
        self.lock = threading.Lock()
        self.barrier = threading.Barrier(3) if not first_range_truncated else None
        self.active = 0
        self.peak = 0

    def get_object(self, **parameters: str) -> dict[str, Any]:
        range_header = parameters["Range"]
        start_wire, end_wire = range_header.removeprefix("bytes=").split("-", maxsplit=1)
        start, end = int(start_wire), int(end_wire)
        with self.lock:
            attempt = self.range_attempts.get(range_header, 0) + 1
            self.range_attempts[range_header] = attempt
            self.calls.append(parameters)
        content = self.content[start : end + 1]
        if self.first_range_truncated and start == 0 and attempt == 1:
            content = content[:-1]
        if self.barrier is None:
            body = _Body(content)
        else:
            body = _CoordinatedBody(content, client=self)
        self.bodies.append(body)  # type: ignore[arg-type]
        return {
            "ContentLength": end - start + 1,
            "ContentRange": f"bytes {start}-{end}/{len(self.content)}",
            "VersionId": parameters["VersionId"],
            "Body": body,
        }


async def test_s3_downloader_streams_exact_object_and_closes_body() -> None:
    content = _safetensors()
    client = _S3Client(content)
    downloader = S3ArtifactDownloader(client=client, bucket="models-private")

    received = b"".join([chunk async for chunk in downloader.stream(_artifact(content))])
    await downloader.close()

    assert received == content
    assert client.calls == [{"Bucket": "models-private", "Key": "models/illustrious.safetensors"}]
    assert client.body is not None and client.body.closed_by_adapter
    assert client.closed


async def test_s3_downloader_rejects_incorrect_declared_size_and_closes_body() -> None:
    content = _safetensors()
    client = _S3Client(content, declared_size=len(content) + 1)
    downloader = S3ArtifactDownloader(client=client, bucket="models-private")

    with pytest.raises(ArtifactBootstrapError, match="artifact bootstrap failed"):
        _ = [chunk async for chunk in downloader.stream(_artifact(content))]

    assert client.body is not None and client.body.closed_by_adapter


async def test_s3_downloader_rejects_non_s3_manifest_source() -> None:
    content = _safetensors()
    source = _artifact(content).model_copy(
        update={"source_object_id": None, "downloader_key": "external-1"}
    )
    downloader = S3ArtifactDownloader(client=_S3Client(content), bucket="models-private")

    with pytest.raises(ArtifactBootstrapError, match="artifact bootstrap failed"):
        _ = [chunk async for chunk in downloader.stream(source)]


async def test_s3_downloader_fetches_version_pinned_ranges_concurrently_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = b"0123456789abcdefghijKLMNOPQRST"
    client = _RangedS3Client(content)
    downloader = S3ArtifactDownloader(client=client, bucket="models-private")
    artifact = _artifact(content, source_object_version_id="version-7")
    monkeypatch.setattr(worker_bootstrap, "MULTIPART_ARTIFACT_PART_BYTES", 10)
    monkeypatch.setattr(worker_bootstrap, "MAX_PARALLEL_ARTIFACT_PARTS", 3)

    received = b"".join([chunk async for chunk in downloader.ranged_stream(artifact)])

    assert received == content
    assert client.peak == 3
    assert {call["Range"] for call in client.calls} == {
        "bytes=0-9",
        "bytes=10-19",
        "bytes=20-29",
    }
    assert all(call["Bucket"] == "models-private" for call in client.calls)
    assert all(call["Key"] == "models/illustrious.safetensors" for call in client.calls)
    assert all(call["VersionId"] == "version-7" for call in client.calls)
    assert all(body.closed_by_adapter for body in client.bodies)


async def test_s3_ranged_downloader_retries_a_truncated_part_without_mixing_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = b"0123456789abcdefghij"
    client = _RangedS3Client(content, first_range_truncated=True)
    downloader = S3ArtifactDownloader(client=client, bucket="models-private")
    artifact = _artifact(content, source_object_version_id="version-8")
    monkeypatch.setattr(worker_bootstrap, "MULTIPART_ARTIFACT_PART_BYTES", 10)
    monkeypatch.setattr(worker_bootstrap, "_ARTIFACT_PART_RETRY_BASE_SECONDS", 0)

    received = b"".join([chunk async for chunk in downloader.ranged_stream(artifact)])

    assert received == content
    assert client.range_attempts["bytes=0-9"] == 2
    assert client.range_attempts["bytes=10-19"] == 1
    assert all(body.closed_by_adapter for body in client.bodies)


async def test_s3_ranged_downloader_requires_an_exact_immutable_version() -> None:
    content = b"0123456789"
    downloader = S3ArtifactDownloader(
        client=_RangedS3Client(content, first_range_truncated=True),
        bucket="models-private",
    )

    with pytest.raises(ArtifactBootstrapError, match="artifact bootstrap failed"):
        _ = [chunk async for chunk in downloader.ranged_stream(_artifact(content))]


def test_scrub_artifact_credentials_preserves_unrelated_runtime_values() -> None:
    environ = {
        "GEN_WORKER_ARTIFACT_ACCESS_KEY_ID": "access",
        "GEN_WORKER_ARTIFACT_SECRET_ACCESS_KEY": "secret",
        "AWS_PROFILE": "profile",
        "GEN_WORKER_VERIFICATION_KEYS": json.dumps({"key-1": WORKER_VERIFICATION_PUBLIC_KEY}),
        "NVIDIA_VISIBLE_DEVICES": "all",
    }

    scrub_artifact_credentials(environ)

    assert environ == {
        "GEN_WORKER_VERIFICATION_KEYS": json.dumps({"key-1": WORKER_VERIFICATION_PUBLIC_KEY}),
        "NVIDIA_VISIBLE_DEVICES": "all",
    }
