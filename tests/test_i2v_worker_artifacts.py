from __future__ import annotations

import hashlib
import io
import json
import logging
import ssl
import threading
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

import boto3
import pytest
from botocore.awsrequest import AWSPreparedRequest, AWSResponse
from botocore.response import StreamingBody
from pydantic import SecretStr
from urllib3.exceptions import SSLError as URLLib3SSLError
from urllib3.response import HTTPResponse

from gen_automation.i2v_worker.artifacts import ModelBootstrapError, S3ModelBootstrapper
from gen_automation.i2v_worker.models import ModelObject
from gen_automation.i2v_worker.settings import I2VWorkerSettings


@pytest.fixture(autouse=True)
def _isolate_artifact_logger(monkeypatch: pytest.MonkeyPatch) -> None:
    # Alembic's migration test fileConfig disables existing loggers. Keep the
    # downloader's failure/redaction assertions independent of suite ordering;
    # production logging and all retry behavior assertions remain unchanged.
    monkeypatch.setattr(logging.getLogger("gen_automation.i2v_worker.artifacts"), "disabled", False)


class _Body(io.BytesIO):
    pass


class _S3:
    def __init__(self, content: bytes, version_id: str) -> None:
        self.content = content
        self.version_id = version_id
        self.ranges: list[str] = []

    def get_object(self, **kwargs: str) -> dict[str, object]:
        assert kwargs["VersionId"] == self.version_id
        raw_range = kwargs["Range"]
        self.ranges.append(raw_range)
        start, end = (int(value) for value in raw_range.removeprefix("bytes=").split("-"))
        part = self.content[start : end + 1]
        return {
            "VersionId": self.version_id,
            "ContentLength": len(part),
            "ContentRange": f"bytes {start}-{end}/{len(self.content)}",
            "Body": _Body(part),
        }


def _settings(tmp_path: Path, model: ModelObject) -> I2VWorkerSettings:
    roles = [
        "diffusion_model_high",
        "diffusion_model_low",
        "text_encoder",
        "vae",
    ]
    values = []
    for role in roles:
        value = model.model_copy(
            update={
                "role": role,
                "key": f"worker/i2v/sha256/{model.sha256}",
                "install_path": {
                    "diffusion_model_high": "models/diffusion_models/high.safetensors",
                    "diffusion_model_low": "models/diffusion_models/low.safetensors",
                    "text_encoder": "models/text_encoders/text.safetensors",
                    "vae": "models/vae/Wan/vae.safetensors",
                }[role],
            }
        )
        values.append(value.model_dump(mode="json"))
    # These tests isolate the ranged downloader. Startup completeness and exact
    # reviewed LoRA identity are covered by the worker-contract suite.
    return I2VWorkerSettings.model_construct(
        model_objects_json=SecretStr(json.dumps(values)),
        environment="test",
        aws_region="eu-central-1",
        s3_endpoint_url=None,
        comfy_root=tmp_path / "comfy",
        runtime_root=tmp_path / "runtime",
        artifact_chunk_bytes=1024 * 1024,
        network_attempts=5,
        network_timeout_seconds=120,
    )


@pytest.mark.asyncio
async def test_model_download_is_version_pinned_resumable_and_hash_verified(tmp_path: Path) -> None:
    content = b"verified-model-bytes"
    model = ModelObject(
        role="diffusion_model_high",
        bucket="models",
        key="worker/i2v/sha256/" + hashlib.sha256(content).hexdigest(),
        version_id="version-1",
        byte_size=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        install_path="models/diffusion_models/model.safetensors",
    )
    settings = _settings(tmp_path, model)
    installed_model = settings.model_objects[0]
    target = settings.comfy_root / installed_model.install_path
    target.parent.mkdir(parents=True)
    partial = target.with_name(f".{target.name}.partial")
    partial.write_bytes(content[:8])
    client = _S3(content, model.version_id)

    materialized = await S3ModelBootstrapper(settings, client=client).bootstrap()

    assert materialized[0].read_bytes() == content
    assert client.ranges[0] == f"bytes=8-{len(content) - 1}"
    assert not partial.exists()


@pytest.mark.asyncio
async def test_full_size_corrupt_partial_is_removed_before_retry(tmp_path: Path) -> None:
    content = b"correct"
    model = ModelObject(
        role="diffusion_model_high",
        bucket="models",
        key="worker/i2v/sha256/" + hashlib.sha256(content).hexdigest(),
        version_id="version-1",
        byte_size=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        install_path="models/diffusion_models/model.safetensors",
    )
    settings = _settings(tmp_path, model)
    installed_model = settings.model_objects[0]
    target = settings.comfy_root / installed_model.install_path
    target.parent.mkdir(parents=True)
    partial = target.with_name(f".{target.name}.partial")
    partial.write_bytes(b"wrong!!")
    client = _S3(content, model.version_id)

    with pytest.raises(ModelBootstrapError):
        await S3ModelBootstrapper(settings, client=client).bootstrap()

    assert not partial.exists()
    assert client.ranges == []

    materialized = await S3ModelBootstrapper(settings, client=client).bootstrap()

    assert materialized[0].read_bytes() == content
    assert client.ranges[0] == f"bytes=0-{len(content) - 1}"


@pytest.mark.asyncio
async def test_existing_wrong_model_fails_closed_without_overwrite(tmp_path: Path) -> None:
    content = b"correct"
    model = ModelObject(
        role="diffusion_model_high",
        bucket="models",
        key="worker/i2v/sha256/" + hashlib.sha256(content).hexdigest(),
        version_id="version-1",
        byte_size=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        install_path="models/diffusion_models/model.safetensors",
    )
    settings = _settings(tmp_path, model)
    target = settings.comfy_root / settings.model_objects[0].install_path
    target.parent.mkdir(parents=True)
    target.write_bytes(b"wrong!!")

    with pytest.raises(ModelBootstrapError):
        await S3ModelBootstrapper(settings, client=_S3(content, model.version_id)).bootstrap()
    assert target.read_bytes() == b"wrong!!"


def test_parallel_ranges_keep_order_and_exact_hash(tmp_path: Path) -> None:
    content = b"a" * (1024 * 1024) + b"b" * (1024 * 1024) + b"c" * (1024 * 1024) + b"d"
    digest = hashlib.sha256(content).hexdigest()
    model = ModelObject(
        role="diffusion_model_high",
        bucket="models",
        key=f"worker/i2v/sha256/{digest}",
        version_id="v1",
        byte_size=len(content),
        sha256=digest,
        install_path="models/diffusion_models/high.safetensors",
    )
    barrier = threading.Barrier(4, timeout=5)

    class ParallelS3(_S3):
        def get_object(self, **kwargs: str) -> dict[str, object]:
            barrier.wait()
            return super().get_object(**kwargs)

    client = ParallelS3(content, "v1")
    bootstrapper = S3ModelBootstrapper(_settings(tmp_path, model), client=client)
    assert bootstrapper._materialize(model).read_bytes() == content
    assert len(client.ranges) == 4


class _InterruptedTLSBody(io.BytesIO):
    """Feed some bytes before a TLS socket error, using the real response wrappers."""

    def __init__(self, content: bytes, error: Exception) -> None:
        super().__init__(content)
        self.error = error
        self.read_calls = 0

    def read(self, size: int | None = -1) -> bytes:
        self.read_calls += 1
        # httplib can receive a prefix from the socket, then fail while filling
        # the requested read. Those bytes must never reach the model file.
        super().read(2)
        raise self.error


class _StreamingS3(_S3):
    def __init__(
        self,
        content: bytes,
        version_id: str,
        *,
        fail_start: int = 8,
        failures: int = 1,
        error_factory: Callable[[], Exception] = lambda: ssl.SSLEOFError("private-detail"),
    ) -> None:
        super().__init__(content, version_id)
        self.fail_start = fail_start
        self.failures = failures
        self.error_factory = error_factory
        self.streams: list[io.BytesIO] = []
        self.requests: list[dict[str, str]] = []
        self.lock = threading.Lock()

    def get_object(self, **kwargs: str) -> dict[str, object]:
        with self.lock:
            response = super().get_object(**kwargs)
            self.requests.append(kwargs)
            start, end = (int(v) for v in kwargs["Range"].removeprefix("bytes=").split("-"))
            part = self.content[start : end + 1]
            stream: io.BytesIO = io.BytesIO(part)
            if start == self.fail_start and self.failures:
                self.failures -= 1
                stream = _InterruptedTLSBody(part, self.error_factory())
            self.streams.append(stream)
        # This is the actual botocore/urllib3 response stack used by S3, not a
        # fake StreamingBody that directly raises the exception we hope to catch.
        raw = HTTPResponse(
            body=stream,
            headers={"Content-Length": str(len(part))},
            preload_content=False,
            decode_content=False,
        )
        response["Body"] = StreamingBody(raw, len(part))
        return response


def _streaming_model(content: bytes) -> ModelObject:
    digest = hashlib.sha256(content).hexdigest()
    return ModelObject(
        role="diffusion_model_high",
        bucket="models",
        key=f"worker/i2v/sha256/{digest}",
        version_id="v1",
        byte_size=len(content),
        sha256=digest,
        install_path="models/diffusion_models/high.safetensors",
    )


def test_real_streaming_body_exposes_unwrapped_urllib3_ssl_error() -> None:
    client = _StreamingS3(b"abcdefgh", "v1", fail_start=0)
    response = client.get_object(VersionId="v1", Range="bytes=0-7")
    body = response["Body"]
    assert isinstance(body, StreamingBody)
    try:
        with pytest.raises(URLLib3SSLError):
            body.read(9)
        stream = client.streams[0]
        assert isinstance(stream, _InterruptedTLSBody)
        assert stream.read_calls == 1
        assert stream.tell() == 2  # Received bytes before failure, none safe to append.
    finally:
        body.close()


@pytest.mark.parametrize("concurrency", [1, 4])
@pytest.mark.parametrize("tls_error", [ssl.SSLEOFError, ssl.SSLError])
def test_tls_read_retry_retains_prefix_and_exact_range(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    concurrency: int,
    tls_error: type[Exception],
) -> None:
    content = b"abcdefgh" * 4
    model = _streaming_model(content)
    settings = _settings(tmp_path, model).model_copy(
        update={"artifact_chunk_bytes": 8, "artifact_download_concurrency": concurrency}
    )
    client = _StreamingS3(content, "v1", error_factory=lambda: tls_error("private-detail"))
    delays: list[float] = []

    def backoff(seconds: float) -> None:
        # Failed streams must be released before sleeping/retrying, not after.
        assert all(s.closed for s in client.streams if isinstance(s, _InterruptedTLSBody))
        delays.append(seconds)

    monkeypatch.setattr("gen_automation.i2v_worker.artifacts.time.sleep", backoff)
    target = S3ModelBootstrapper(settings, client=client)._materialize(model)

    assert target.read_bytes() == content
    assert hashlib.sha256(target.read_bytes()).hexdigest() == model.sha256
    assert client.ranges.count("bytes=0-7") == 1
    assert client.ranges.count("bytes=8-15") == 2
    assert len(client.ranges) == 5
    retries = [r for r in client.requests if r["Range"] == "bytes=8-15"]
    assert retries[0] == retries[1]
    assert all(r["VersionId"] == model.version_id for r in client.requests)
    assert all(s.closed for s in client.streams)
    assert delays == [1]
    assert "i2v_model_range_retry" in caplog.text
    assert "private-detail" not in caplog.text
    assert model.key not in caplog.text


@pytest.mark.parametrize("tls_error", [ssl.SSLEOFError, ssl.SSLCertVerificationError])
def test_tls_exhaustion_is_bounded_redacted_and_resumable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tls_error: type[Exception],
) -> None:
    content = b"abcdefgh" * 4
    model = _streaming_model(content)
    settings = _settings(tmp_path, model).model_copy(
        update={"artifact_chunk_bytes": 8, "artifact_download_concurrency": 1}
    )
    client = _StreamingS3(
        content, "v1", failures=5, error_factory=lambda: tls_error("private-detail")
    )
    delays: list[float] = []
    monkeypatch.setattr("gen_automation.i2v_worker.artifacts.time.sleep", delays.append)
    bootstrapper = S3ModelBootstrapper(settings, client=client)

    with pytest.raises(ModelBootstrapError, match=r"^model bootstrap failed$") as raised:
        bootstrapper._materialize(model)

    assert raised.value.__suppress_context__ is True
    assert delays == [1, 2, 4, 8]
    assert client.ranges == ["bytes=0-7"] + ["bytes=8-15"] * 5
    assert all(s.closed for s in client.streams)
    target = settings.comfy_root / model.install_path
    assert not target.exists()
    assert target.with_name(f".{target.name}.partial").read_bytes() == content[:8]
    assert "i2v_model_range_failed" in caplog.text
    assert "private-detail" not in caplog.text
    assert model.key not in caplog.text

    # An explicit later bootstrap resumes the intact contiguous prefix. No
    # failed response bytes were appended, even when a response partially read.
    assert bootstrapper._materialize(model).read_bytes() == content
    assert client.ranges.count("bytes=0-7") == 1
    assert client.ranges[-3:] == ["bytes=8-15", "bytes=16-23", "bytes=24-31"]


@pytest.mark.parametrize("field", ["VersionId", "ContentLength", "ContentRange", "Body"])
def test_retry_does_not_accept_invalid_range_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    model = _streaming_model(b"abcdefgh")
    stream = _Body(b"abcdefgh")

    class InvalidS3(_S3):
        def get_object(self, **kwargs: str) -> dict[str, object]:
            response = super().get_object(**kwargs)
            response["Body"] = stream
            response[field] = None
            return response

    client = InvalidS3(b"abcdefgh", "v1")
    delays: list[float] = []
    monkeypatch.setattr("gen_automation.i2v_worker.artifacts.time.sleep", delays.append)
    with pytest.raises(ModelBootstrapError):
        S3ModelBootstrapper(_settings(tmp_path, model), client=client)._materialize(model)
    assert client.ranges == ["bytes=0-7"]
    assert delays == []
    if field != "Body":
        assert stream.closed


def test_real_s3_retries_stay_signed_and_cloudfront_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = b"abcdefgh" * 3
    model = _streaming_model(content)
    settings = _settings(tmp_path, model).model_copy(
        update={
            "artifact_chunk_bytes": 8,
            "artifact_download_concurrency": 1,
            "model_delivery_domain": "dexample.cloudfront.net",
            "require_private_delivery": True,
        }
    )
    backend = _StreamingS3(content, "v1")
    requests: list[AWSPreparedRequest] = []
    client = boto3.client(
        "s3",
        region_name="eu-central-1",
        aws_access_key_id="test-access",
        aws_secret_access_key="test-secret",  # noqa: S106 - offline signing fixture
        aws_session_token="test-token",  # noqa: S106 - offline signing fixture
    )

    def send(request: AWSPreparedRequest) -> AWSResponse:
        requests.append(request)
        raw_range = request.headers["Range"].decode()
        response = backend.get_object(VersionId="v1", Range=raw_range)
        body = response["Body"]
        assert isinstance(body, StreamingBody)
        return AWSResponse(
            request.url,
            206,
            {
                "content-length": str(response["ContentLength"]),
                "content-range": response["ContentRange"],
                "x-amz-version-id": "v1",
            },
            body._raw_stream,
        )

    monkeypatch.setattr(client._endpoint.http_session, "send", send)
    monkeypatch.setattr("gen_automation.i2v_worker.artifacts.time.sleep", lambda _: None)
    try:
        assert client._endpoint.http_session._verify is True
        result = S3ModelBootstrapper(settings, client=client)._materialize(model)
        assert result.read_bytes() == content
        assert len(requests) == 4
        assert backend.ranges == ["bytes=0-7", "bytes=8-15", "bytes=8-15", "bytes=16-23"]
        for request in requests:
            url = urlsplit(request.url)
            assert url.scheme == "https"
            assert url.netloc == settings.model_delivery_domain
            assert url.path.startswith("/models/")
            assert url.query == "versionId=v1"
            assert request.headers["Host"] == "dexample.cloudfront.net"
            assert request.headers["Authorization"].startswith(b"AWS4-HMAC-SHA256 ")
            assert request.headers["X-Amz-Security-Token"] == b"test-token"
        assert client._endpoint.http_session._verify is True
        assert all(s.closed for s in backend.streams)
    finally:
        client.close()


def test_non_transport_bug_is_not_retried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    content = b"abcdefgh"
    model = _streaming_model(content)
    client = _StreamingS3(
        content, "v1", fail_start=0, error_factory=lambda: RuntimeError("test code bug")
    )
    delays: list[float] = []
    monkeypatch.setattr("gen_automation.i2v_worker.artifacts.time.sleep", delays.append)
    with pytest.raises(RuntimeError, match="test code bug"):
        S3ModelBootstrapper(_settings(tmp_path, model), client=client)._materialize(model)
    assert client.ranges == ["bytes=0-7"]
    assert all(s.closed for s in client.streams)
    assert delays == []


def test_successful_tls_retry_still_rejects_bad_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = b"abcdefgh" * 3
    model = _streaming_model(content).model_copy(update={"sha256": "0" * 64})
    settings = _settings(tmp_path, model).model_copy(
        update={"artifact_chunk_bytes": 8, "artifact_download_concurrency": 1}
    )
    client = _StreamingS3(content, "v1")
    monkeypatch.setattr("gen_automation.i2v_worker.artifacts.time.sleep", lambda _: None)
    with pytest.raises(ModelBootstrapError):
        S3ModelBootstrapper(settings, client=client)._materialize(model)
    assert client.ranges.count("bytes=8-15") == 2
    target = settings.comfy_root / model.install_path
    assert not target.exists()
    assert not target.with_name(f".{target.name}.partial").exists()
