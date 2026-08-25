import asyncio
import base64
import hashlib
import io
import json
import threading
from dataclasses import dataclass, field
from typing import Any

import httpx2
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pydantic import ValidationError

from gen_automation.domain.signing import derive_public_key, encode_base64url
from gen_automation.gpu_worker.app import create_worker_app
from gen_automation.gpu_worker.models import (
    GenerateEnvelope,
    GeneratePayloadReference,
    ReferencedGenerateEnvelope,
    UploadGrant,
    WorkerEnvironment,
    WorkerSettings,
)
from gen_automation.gpu_worker.runtime import (
    HttpxMultipartUploader,
    HttpxPayloadDownloader,
    PayloadDownloader,
    WorkerPayloadDownloadError,
    WorkerUploadError,
)
from gen_automation.gpu_worker.security import calculate_signature

NOW = 2_000_000_000
TEST_PRIVATE_KEY = encode_base64url(bytes(range(1, 33)))
TEST_PUBLIC_KEY = derive_public_key(TEST_PRIVATE_KEY)
OTHER_PRIVATE_KEY = encode_base64url(bytes(range(33, 65)))
OTHER_PUBLIC_KEY = derive_public_key(OTHER_PRIVATE_KEY)
UPLOAD_ORIGIN = "https://uploads.example.test"


def _image_bytes(media_type: str = "image/png") -> bytes:
    image = Image.new("RGB", (2, 2), color=(20, 40, 60))
    output = io.BytesIO()
    formats = {
        "image/png": "PNG",
        "image/jpeg": "JPEG",
        "image/webp": "WEBP",
    }
    image.save(output, format=formats[media_type])
    return output.getvalue()


def _solid_image_bytes(color: tuple[int, int, int]) -> bytes:
    image = Image.new("RGB", (2, 2), color=color)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _output_for_content(content: bytes, index: int = 0) -> dict[str, object]:
    return {
        "output_index": index,
        "media_type": "image/png",
        "data_base64": base64.b64encode(content).decode(),
    }


def _output(index: int = 0, media_type: str = "image/png") -> dict[str, object]:
    return {
        "output_index": index,
        "media_type": media_type,
        "data_base64": base64.b64encode(_image_bytes(media_type)).decode(),
    }


@dataclass
class FakeExecutor:
    outputs: object = field(default_factory=lambda: [_output()])
    ready: bool = True
    ready_error: Exception | None = None
    execute_error: Exception | None = None
    workflows: list[dict[str, object]] = field(default_factory=list)

    def is_ready(self) -> bool:
        if self.ready_error is not None:
            raise self.ready_error
        return self.ready

    def execute(self, workflow: dict[str, object]) -> object:
        self.workflows.append(workflow)
        if self.execute_error is not None:
            raise self.execute_error
        return self.outputs


@dataclass
class RecordedUpload:
    grant: UploadGrant
    content: bytes
    media_type: str


@dataclass
class FakeUploader:
    error: Exception | None = None
    uploads: list[RecordedUpload] = field(default_factory=list)

    async def upload(
        self,
        *,
        grant: UploadGrant,
        content: bytes,
        media_type: str,
    ) -> None:
        if self.error is not None:
            raise self.error
        self.uploads.append(RecordedUpload(grant=grant, content=content, media_type=media_type))


@dataclass
class FakePayloadDownloader:
    body: bytes
    error: Exception | None = None
    calls: list[tuple[str, int]] = field(default_factory=list)

    async def download(self, *, url: str, expected_bytes: int) -> bytes:
        self.calls.append((url, expected_bytes))
        if self.error is not None:
            raise self.error
        return self.body


def _settings(**overrides: object) -> WorkerSettings:
    values: dict[str, object] = {
        "environment": WorkerEnvironment.TEST,
        "verification_keys": {"worker-key-1": TEST_PUBLIC_KEY},
        "allowed_upload_origin": UPLOAD_ORIGIN,
    }
    values.update(overrides)
    return WorkerSettings.model_validate(values)


def _unsigned_request(
    *,
    uploads: int = 1,
    issued_at: int = NOW - 5,
    expires_at: int = NOW + 60,
    key_id: str = "worker-key-1",
    upload_content_type: str = "image/png",
    job_id: str = "job-1",
    attempt_id: str = "attempt-1",
) -> dict[str, Any]:
    grants = [
        {
            "asset_id": f"asset-{index}",
            "upload_attempt_id": f"upload-{index}",
            "output_index": index,
            "content_type": upload_content_type,
            "url": f"{UPLOAD_ORIGIN}/staging/output-{index}",
            "fields": {
                "key": f"private/object-{index}",
                "policy": "private-policy-value",
                "x-amz-signature": "presigned-secret-value",
            },
        }
        for index in range(uploads)
    ]
    return {
        "version": "v1",
        "key_id": key_id,
        "issued_at": issued_at,
        "expires_at": expires_at,
        "payload": {
            "job_id": job_id,
            "attempt_id": attempt_id,
            "workflow": {
                "1": {
                    "class_type": "CLIPTextEncode",
                    "inputs": {"text": "private generation prompt"},
                },
                "2": {
                    "class_type": "KSampler",
                    "inputs": {
                        "cfg": 5.5,
                        "steps": 20,
                        "latent_image": ["5", 0],
                    },
                },
                "3": {
                    "class_type": "VAEDecode",
                    "inputs": {"samples": ["2", 0]},
                },
                "4": {
                    "class_type": "SaveImage",
                    "inputs": {"images": ["3", 0]},
                },
                "5": {
                    "class_type": "EmptyLatentImage",
                    "inputs": {"width": 512, "height": 512, "batch_size": 1},
                },
            },
            "uploads": grants,
        },
        "signature": "A" * 86,
    }


def _signed_request(
    *,
    private_key: str = TEST_PRIVATE_KEY,
    uploads: int = 1,
    issued_at: int = NOW - 5,
    expires_at: int = NOW + 60,
    key_id: str = "worker-key-1",
    upload_content_type: str = "image/png",
    job_id: str = "job-1",
    attempt_id: str = "attempt-1",
) -> dict[str, Any]:
    raw = _unsigned_request(
        uploads=uploads,
        issued_at=issued_at,
        expires_at=expires_at,
        key_id=key_id,
        upload_content_type=upload_content_type,
        job_id=job_id,
        attempt_id=attempt_id,
    )
    envelope = GenerateEnvelope.model_validate(raw, strict=True)
    raw["signature"] = calculate_signature(envelope, private_key)
    return raw


def _sign_request(raw: dict[str, Any]) -> dict[str, Any]:
    envelope = GenerateEnvelope.model_validate(raw, strict=True)
    raw["signature"] = calculate_signature(envelope, TEST_PRIVATE_KEY)
    return raw


def _progressive_workflow(outputs: int) -> dict[str, object]:
    workflow: dict[str, object] = {}
    for output_index in range(outputs):
        prefix = f"output-{output_index:02d}-"
        workflow.update(
            {
                f"{prefix}2": {
                    "class_type": "KSampler",
                    "inputs": {"cfg": 5.5, "steps": 20, "latent_image": [f"{prefix}5", 0]},
                },
                f"{prefix}3": {
                    "class_type": "VAEDecode",
                    "inputs": {"samples": [f"{prefix}2", 0]},
                },
                f"{prefix}4": {
                    "class_type": "SaveImage",
                    "inputs": {"images": [f"{prefix}3", 0]},
                },
                f"{prefix}5": {
                    "class_type": "EmptyLatentImage",
                    "inputs": {"width": 512, "height": 512, "batch_size": 1},
                },
            }
        )
    return workflow


def _client(
    *,
    executor: FakeExecutor | None = None,
    uploader: FakeUploader | None = None,
    payload_downloader: PayloadDownloader | None = None,
    settings: WorkerSettings | None = None,
) -> tuple[TestClient, FakeExecutor, FakeUploader]:
    resolved_executor = executor or FakeExecutor()
    resolved_uploader = uploader or FakeUploader()
    app = create_worker_app(
        settings=settings or _settings(),
        executor=resolved_executor,
        uploader=resolved_uploader,
        payload_downloader=payload_downloader,
        now=lambda: NOW,
    )
    return TestClient(app), resolved_executor, resolved_uploader


def _signed_referenced_request(payload: dict[str, Any]) -> tuple[dict[str, Any], bytes]:
    body = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    envelope = ReferencedGenerateEnvelope(
        version="v2",
        key_id="worker-key-1",
        issued_at=NOW - 5,
        expires_at=NOW + 60,
        payload=GeneratePayloadReference(
            job_id=str(payload["job_id"]),
            attempt_id=str(payload["attempt_id"]),
            url=f"{UPLOAD_ORIGIN}/staging/worker-requests/request.json",
            sha256=hashlib.sha256(body).hexdigest(),
            byte_size=len(body),
        ),
        signature="A" * 86,
    )
    signed = envelope.model_copy(
        update={"signature": calculate_signature(envelope, TEST_PRIVATE_KEY)}
    )
    return signed.model_dump(mode="json"), body


def test_production_settings_fail_closed() -> None:
    with pytest.raises(ValidationError):
        WorkerSettings(
            verification_keys={"key": "too-short"},
            allowed_upload_origin=UPLOAD_ORIGIN,
        )
    with pytest.raises(ValidationError):
        WorkerSettings(
            verification_keys={"key": TEST_PUBLIC_KEY},
            allowed_upload_origin="http://uploads.example.test",
        )
    with pytest.raises(ValidationError):
        WorkerSettings(
            verification_keys={"key": TEST_PUBLIC_KEY},
            allowed_upload_origin=f"{UPLOAD_ORIGIN}/bucket",
        )

    settings = _settings()
    assert TEST_PRIVATE_KEY not in settings.model_dump_json()
    assert not hasattr(settings, "signing_private_key")


def test_approved_workflow_node_classes_are_immutable_and_fail_closed() -> None:
    settings = _settings()

    assert isinstance(settings.approved_workflow_node_classes, frozenset)
    assert "KSampler" in settings.approved_workflow_node_classes
    assert "ExecutePython" not in settings.approved_workflow_node_classes
    with pytest.raises(AttributeError):
        settings.approved_workflow_node_classes.add("ExecutePython")  # type: ignore[attr-defined]
    with pytest.raises(ValidationError):
        _settings(approved_workflow_node_classes=frozenset({"KSampler"}))
    with pytest.raises(ValidationError):
        _settings(
            approved_workflow_node_classes=frozenset({"KSampler", "SaveImage", "bad node class"})
        )


def test_health_and_ready_do_not_expose_runtime_details() -> None:
    client, executor, _uploader = _client()
    with client:
        assert client.get("/health").json() == {"status": "ok", "version": "v1"}
        assert client.get("/ready").json() == {"status": "ready", "version": "v1"}
        executor.ready_error = RuntimeError("private Comfy connection detail")
        response = client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "version": "v1"}
    assert "private" not in response.text


def test_unapproved_workflow_node_is_rejected_before_executor_submission() -> None:
    request = _unsigned_request()
    private_node_class = "ExecutePython_private_secret"
    request["payload"]["workflow"]["6"] = {
        "class_type": private_node_class,
        "inputs": {},
    }
    envelope = GenerateEnvelope.model_validate(request, strict=True)
    request["signature"] = calculate_signature(envelope, TEST_PRIVATE_KEY)
    client, executor, uploader = _client()

    with client:
        response = client.post("/jobs/generate", json=request)

    assert response.status_code == 400
    assert response.json() == {"detail": "invalid request"}
    assert private_node_class not in response.text
    assert executor.workflows == []
    assert uploader.uploads == []


def test_oversized_rendered_geometry_is_rejected_before_executor_submission() -> None:
    request = _unsigned_request()
    request["payload"]["workflow"]["5"]["inputs"]["width"] = 16384
    request["payload"]["workflow"]["5"]["inputs"]["height"] = 64
    envelope = GenerateEnvelope.model_validate(request, strict=True)
    request["signature"] = calculate_signature(envelope, TEST_PRIVATE_KEY)
    client, executor, uploader = _client()

    with client:
        response = client.post("/jobs/generate", json=request)

    assert response.status_code == 400
    assert response.json() == {"detail": "invalid request"}
    assert executor.workflows == []
    assert uploader.uploads == []


@dataclass
class BlockingExecutor:
    outputs: object = field(default_factory=lambda: [_output()])
    started: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)
    guard: threading.Lock = field(default_factory=threading.Lock)
    active: int = 0
    maximum_active: int = 0
    workflows: list[dict[str, object]] = field(default_factory=list)
    execution_thread_ids: list[int] = field(default_factory=list)

    def is_ready(self) -> bool:
        with self.guard:
            return self.active == 0

    def execute(self, workflow: dict[str, object]) -> object:
        with self.guard:
            self.active += 1
            self.maximum_active = max(self.maximum_active, self.active)
        self.execution_thread_ids.append(threading.get_ident())
        self.workflows.append(workflow)
        self.started.set()
        try:
            if not self.release.wait(timeout=5):
                raise RuntimeError("test execution timed out")
            return self.outputs
        finally:
            with self.guard:
                self.active -= 1


@pytest.mark.asyncio
async def test_blocking_execution_keeps_probes_responsive_and_gpu_lane_serial() -> None:
    executor = BlockingExecutor()
    app = create_worker_app(
        settings=_settings(),
        executor=executor,
        uploader=FakeUploader(),
        now=lambda: NOW,
    )
    event_loop_thread = threading.get_ident()

    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app),
            base_url="https://worker.example.test",
        ) as client:
            first = asyncio.create_task(client.post("/jobs/generate", json=_signed_request()))
            assert await asyncio.to_thread(executor.started.wait, 2)
            second = asyncio.create_task(
                client.post(
                    "/jobs/generate",
                    json=_signed_request(job_id="job-2", attempt_id="attempt-2"),
                )
            )

            health = await asyncio.wait_for(client.get("/health"), timeout=1)
            ready = await asyncio.wait_for(client.get("/ready"), timeout=1)
            assert health.status_code == 200
            assert ready.status_code == 503

            executor.release.set()
            first_response, second_response = await asyncio.gather(first, second)

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert executor.maximum_active == 1
    assert len(executor.workflows) == 2
    assert executor.execution_thread_ids
    assert set(executor.execution_thread_ids) == {executor.execution_thread_ids[0]}
    assert executor.execution_thread_ids[0] != event_loop_thread


@dataclass
class PausingUploader:
    started: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    uploads: list[RecordedUpload] = field(default_factory=list)

    async def upload(
        self,
        *,
        grant: UploadGrant,
        content: bytes,
        media_type: str,
    ) -> None:
        self.started.set()
        await self.release.wait()
        self.uploads.append(RecordedUpload(grant=grant, content=content, media_type=media_type))


@pytest.mark.asyncio
async def test_long_async_upload_keeps_health_and_readiness_responsive() -> None:
    uploader = PausingUploader()
    app = create_worker_app(
        settings=_settings(),
        executor=FakeExecutor(),
        uploader=uploader,
        now=lambda: NOW,
    )

    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app),
            base_url="https://worker.example.test",
        ) as client:
            generation = asyncio.create_task(client.post("/jobs/generate", json=_signed_request()))
            await asyncio.wait_for(uploader.started.wait(), timeout=2)

            health = await asyncio.wait_for(client.get("/health"), timeout=1)
            ready = await asyncio.wait_for(client.get("/ready"), timeout=1)
            assert health.status_code == 200
            assert ready.status_code == 200

            uploader.release.set()
            response = await generation

    assert response.status_code == 200
    assert len(uploader.uploads) == 1


@dataclass
class BlockingReadinessExecutor(FakeExecutor):
    ready_started: threading.Event = field(default_factory=threading.Event)
    ready_release: threading.Event = field(default_factory=threading.Event)

    def is_ready(self) -> bool:
        self.ready_started.set()
        if not self.ready_release.wait(timeout=5):
            raise RuntimeError("test readiness timed out")
        return True


@pytest.mark.asyncio
async def test_readiness_probe_is_time_bounded_without_blocking_health() -> None:
    executor = BlockingReadinessExecutor()
    app = create_worker_app(
        settings=_settings(readiness_timeout_seconds=0.05),
        executor=executor,
        uploader=FakeUploader(),
        now=lambda: NOW,
    )

    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app),
            base_url="https://worker.example.test",
        ) as client:
            readiness = asyncio.create_task(client.get("/ready"))
            assert await asyncio.to_thread(executor.ready_started.wait, 1)
            health = await asyncio.wait_for(client.get("/health"), timeout=1)
            response = await asyncio.wait_for(readiness, timeout=1)
            executor.ready_release.set()

    assert health.status_code == 200
    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "version": "v1"}


@pytest.mark.parametrize("media_type", ["image/png", "image/jpeg", "image/webp"])
def test_signed_generation_uploads_supported_image_and_returns_stable_ids(
    media_type: str,
) -> None:
    executor = FakeExecutor(outputs=[_output(media_type=media_type)])
    client, executor, uploader = _client(executor=executor)

    with client:
        response = client.post(
            "/jobs/generate",
            json=_signed_request(upload_content_type=media_type),
        )

    assert response.status_code == 200
    assert response.json() == {
        "version": "v1",
        "job_id": "job-1",
        "attempt_id": "attempt-1",
        "status": "succeeded",
        "outputs": [
            {
                "asset_id": "asset-0",
                "upload_attempt_id": "upload-0",
                "output_index": 0,
                "status": "uploaded",
            }
        ],
    }
    assert executor.workflows == [
        {
            "1": {
                "class_type": "CLIPTextEncode",
                "inputs": {"text": "private generation prompt"},
            },
            "2": {
                "class_type": "KSampler",
                "inputs": {
                    "cfg": 5.5,
                    "steps": 20,
                    "latent_image": ["5", 0],
                },
            },
            "3": {
                "class_type": "VAEDecode",
                "inputs": {"samples": ["2", 0]},
            },
            "4": {
                "class_type": "SaveImage",
                "inputs": {"images": ["3", 0]},
            },
            "5": {
                "class_type": "EmptyLatentImage",
                "inputs": {"width": 512, "height": 512, "batch_size": 1},
            },
        }
    ]
    assert len(uploader.uploads) == 1
    assert uploader.uploads[0].content == _image_bytes(media_type)
    assert uploader.uploads[0].media_type == media_type
    for private_value in (
        "private generation prompt",
        "uploads.example.test",
        "private-policy-value",
        "presigned-secret-value",
        "data_base64",
    ):
        assert private_value not in response.text


def test_multi_prompt_job_executes_and_uploads_each_image_in_order() -> None:
    events: list[str] = []

    @dataclass
    class OrderedExecutor(FakeExecutor):
        def execute(self, workflow: dict[str, object]) -> object:
            output_index = len(self.workflows)
            events.append(f"execute-{output_index}")
            self.workflows.append(workflow)
            return [_output()]

    @dataclass
    class OrderedUploader(FakeUploader):
        async def upload(
            self,
            *,
            grant: UploadGrant,
            content: bytes,
            media_type: str,
        ) -> None:
            events.append(f"upload-{grant.output_index}")
            self.uploads.append(RecordedUpload(grant=grant, content=content, media_type=media_type))

    request = _unsigned_request(uploads=3)
    request["payload"]["workflow"] = _progressive_workflow(3)
    executor = OrderedExecutor()
    uploader = OrderedUploader()
    client, executor, uploader = _client(executor=executor, uploader=uploader)

    with client:
        response = client.post("/jobs/generate", json=_sign_request(request))

    assert response.status_code == 200
    assert events == [
        "execute-0",
        "upload-0",
        "execute-1",
        "upload-1",
        "execute-2",
        "upload-2",
    ]
    assert [item["output_index"] for item in response.json()["outputs"]] == [0, 1, 2]
    assert len(executor.workflows) == 3
    assert all(
        all(node_id.startswith(f"output-{output_index:02d}-") for node_id in workflow)
        for output_index, workflow in enumerate(executor.workflows)
    )
    assert [upload.grant.output_index for upload in uploader.uploads] == [0, 1, 2]


def test_referenced_twenty_five_output_job_executes_and_uploads_in_order() -> None:
    inline = _unsigned_request(uploads=25)
    payload = inline["payload"]
    payload["workflow"] = _progressive_workflow(25)
    request, body = _signed_referenced_request(payload)
    downloader = FakePayloadDownloader(body=body)
    client, executor, uploader = _client(payload_downloader=downloader)

    with client:
        response = client.post("/jobs/generate", json=request)

    assert response.status_code == 200
    assert downloader.calls == [
        (
            f"{UPLOAD_ORIGIN}/staging/worker-requests/request.json",
            len(body),
        )
    ]
    assert [item["output_index"] for item in response.json()["outputs"]] == list(range(25))
    assert len(executor.workflows) == 25
    assert [upload.grant.output_index for upload in uploader.uploads] == list(range(25))


def test_referenced_payload_hash_mismatch_is_rejected_before_generation() -> None:
    inline = _unsigned_request(uploads=9)
    payload = inline["payload"]
    payload["workflow"] = _progressive_workflow(9)
    request, body = _signed_referenced_request(payload)
    downloader = FakePayloadDownloader(body=body + b" ")
    client, executor, uploader = _client(payload_downloader=downloader)

    with client:
        response = client.post("/jobs/generate", json=request)

    assert response.status_code == 400
    assert executor.workflows == []
    assert uploader.uploads == []


def test_referenced_payload_is_not_downloaded_until_outer_signature_is_valid() -> None:
    inline = _unsigned_request(uploads=9)
    inline["payload"]["workflow"] = _progressive_workflow(9)
    request, body = _signed_referenced_request(inline["payload"])
    request["payload"]["byte_size"] += 1
    downloader = FakePayloadDownloader(body=body)
    client, executor, uploader = _client(payload_downloader=downloader)

    with client:
        response = client.post("/jobs/generate", json=request)

    assert response.status_code == 401
    assert downloader.calls == []
    assert executor.workflows == []
    assert uploader.uploads == []


def test_referenced_payload_origin_and_inner_identity_fail_closed() -> None:
    inline = _unsigned_request(uploads=9)
    inline["payload"]["workflow"] = _progressive_workflow(9)
    request, body = _signed_referenced_request(inline["payload"])

    request["payload"]["url"] = "https://untrusted.example/staging/request.json"
    unsigned = ReferencedGenerateEnvelope.model_validate(request, strict=True)
    request["signature"] = calculate_signature(unsigned, TEST_PRIVATE_KEY)
    bad_origin_downloader = FakePayloadDownloader(body=body)
    client, bad_origin_executor, bad_origin_uploader = _client(
        payload_downloader=bad_origin_downloader
    )
    with client:
        bad_origin = client.post("/jobs/generate", json=request)

    request, body = _signed_referenced_request(inline["payload"])
    request["payload"]["attempt_id"] = "different-attempt"
    unsigned = ReferencedGenerateEnvelope.model_validate(request, strict=True)
    request["signature"] = calculate_signature(unsigned, TEST_PRIVATE_KEY)
    downloader = FakePayloadDownloader(body=body)
    client, executor, uploader = _client(payload_downloader=downloader)
    with client:
        mismatched_identity = client.post("/jobs/generate", json=request)

    assert bad_origin.status_code == 400
    assert bad_origin_downloader.calls == []
    assert bad_origin_executor.workflows == []
    assert bad_origin_uploader.uploads == []
    assert mismatched_identity.status_code == 400
    assert executor.workflows == []
    assert uploader.uploads == []


def test_referenced_payload_transport_failure_is_retryable_and_redacted() -> None:
    inline = _unsigned_request(uploads=9)
    inline["payload"]["workflow"] = _progressive_workflow(9)
    request, body = _signed_referenced_request(inline["payload"])
    downloader = FakePayloadDownloader(
        body=body,
        error=WorkerPayloadDownloadError("private signed URL"),
    )
    client, executor, uploader = _client(payload_downloader=downloader)

    with client:
        response = client.post("/jobs/generate", json=request)

    assert response.status_code == 502
    assert response.json() == {"detail": "request payload unavailable"}
    assert "private" not in response.text
    assert executor.workflows == []
    assert uploader.uploads == []


def test_worker_rejects_twenty_six_outputs_before_generation() -> None:
    request = _unsigned_request(uploads=26)
    request["payload"]["workflow"] = _progressive_workflow(26)
    client, executor, uploader = _client()

    with client:
        response = client.post("/jobs/generate", json=_sign_request(request))

    assert response.status_code == 400
    assert executor.workflows == []
    assert uploader.uploads == []


def test_ed25519_authenticates_the_payload_and_upload_grants() -> None:
    request = _signed_request()
    request["payload"]["workflow"]["2"]["inputs"]["steps"] = 21
    request["payload"]["uploads"][0]["url"] = f"{UPLOAD_ORIGIN}/other"
    client, executor, uploader = _client()

    with client:
        response = client.post("/jobs/generate", json=request)

    assert response.status_code == 401
    assert response.json() == {"detail": "invalid authorization"}
    assert executor.workflows == []
    assert uploader.uploads == []


def test_worker_accepts_public_key_rotation_without_private_material() -> None:
    settings = _settings(
        verification_keys={
            "worker-key-1": TEST_PUBLIC_KEY,
            "worker-key-2": OTHER_PUBLIC_KEY,
        }
    )
    request = _signed_request(
        key_id="worker-key-2",
        private_key=OTHER_PRIVATE_KEY,
    )
    client, _executor, _uploader = _client(settings=settings)

    with client:
        response = client.post("/jobs/generate", json=request)

    assert response.status_code == 200
    assert len(request["signature"]) == 86
    assert TEST_PRIVATE_KEY not in settings.model_dump_json()
    assert OTHER_PRIVATE_KEY not in settings.model_dump_json()


def test_successful_attempt_replay_is_cached_and_conflicting_replay_is_rejected() -> None:
    client, executor, uploader = _client()
    request = _signed_request()
    conflict = _signed_request()
    conflict["payload"]["workflow"]["2"]["inputs"]["steps"] = 21
    conflict_envelope = GenerateEnvelope.model_validate(conflict, strict=True)
    conflict["signature"] = calculate_signature(conflict_envelope, TEST_PRIVATE_KEY)

    with client:
        first = client.post("/jobs/generate", json=request)
        replay = client.post("/jobs/generate", json=request)
        rejected = client.post("/jobs/generate", json=conflict)

    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.json() == first.json()
    assert rejected.status_code == 409
    assert rejected.json() == {"detail": "request conflicts with completed attempt"}
    assert len(executor.workflows) == 1
    assert len(uploader.uploads) == 1


@pytest.mark.parametrize(
    ("signed_body", "settings"),
    [
        (_signed_request(private_key=OTHER_PRIVATE_KEY), _settings()),
        (
            _signed_request(),
            _settings(verification_keys={"worker-key-1": OTHER_PUBLIC_KEY}),
        ),
        (
            _signed_request(issued_at=NOW - 100, expires_at=NOW - 20),
            _settings(clock_skew_seconds=5),
        ),
        (
            _signed_request(issued_at=NOW - 1, expires_at=NOW + 301),
            _settings(max_signature_ttl_seconds=300),
        ),
        (
            _signed_request(issued_at=NOW + 70, expires_at=NOW + 100),
            _settings(clock_skew_seconds=5),
        ),
        (
            _signed_request(key_id="unknown", private_key=TEST_PRIVATE_KEY),
            _settings(),
        ),
    ],
)
def test_invalid_authorization_is_generic(
    signed_body: dict[str, Any],
    settings: WorkerSettings,
) -> None:
    client, _executor, _uploader = _client(settings=settings)

    with client:
        response = client.post("/jobs/generate", json=signed_body)

    assert response.status_code == 401
    assert response.json() == {"detail": "invalid authorization"}
    assert TEST_PRIVATE_KEY not in response.text


def test_request_body_and_media_type_are_bounded_before_parsing() -> None:
    settings = _settings(max_body_bytes=1024)
    client, executor, _uploader = _client(settings=settings)

    with client:
        too_large = client.post(
            "/jobs/generate",
            content=b"x" * 1025,
            headers={"content-type": "application/json"},
        )
        wrong_media = client.post(
            "/jobs/generate",
            content=b"{}",
            headers={"content-type": "text/plain"},
        )

    assert too_large.status_code == 413
    assert wrong_media.status_code == 415
    assert executor.workflows == []


def test_malformed_or_extra_envelope_fields_are_rejected_generically() -> None:
    request = _signed_request()
    request["private_extra"] = "must not be reflected"
    client, _executor, _uploader = _client()

    with client:
        response = client.post("/jobs/generate", json=request)

    assert response.status_code == 400
    assert response.json() == {"detail": "invalid request"}
    assert "private_extra" not in response.text


def test_duplicate_json_object_keys_are_rejected_before_authorization() -> None:
    raw = json.dumps(_signed_request(), separators=(",", ":"))
    duplicate = raw.replace('"version":"v1"', '"version":"v1","version":"v1"', 1)
    client, executor, _uploader = _client()

    with client:
        response = client.post(
            "/jobs/generate",
            content=duplicate,
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 400
    assert response.json() == {"detail": "invalid request"}
    assert executor.workflows == []


@pytest.mark.parametrize(
    "url",
    [
        "https://uploads.example.test.evil/object",
        "https://user@uploads.example.test/object",
        "http://uploads.example.test/object",
        "https://uploads.example.test:444/object",
        "https://uploads.example.test/object#fragment",
    ],
)
def test_only_the_configured_https_upload_origin_is_allowed(url: str) -> None:
    request = _unsigned_request()
    request["payload"]["uploads"][0]["url"] = url
    envelope = GenerateEnvelope.model_validate(request, strict=True)
    request["signature"] = calculate_signature(envelope, TEST_PRIVATE_KEY)
    client, executor, uploader = _client()

    with client:
        response = client.post("/jobs/generate", json=request)

    assert response.status_code == 400
    assert response.json() == {"detail": "invalid request"}
    assert executor.workflows == []
    assert uploader.uploads == []


def test_grant_indices_must_be_exact_and_contiguous() -> None:
    request = _unsigned_request(uploads=2)
    request["payload"]["uploads"][1]["output_index"] = 2
    client, executor, _uploader = _client()

    with client:
        response = client.post("/jobs/generate", json=request)

    assert response.status_code == 400
    assert executor.workflows == []


@pytest.mark.parametrize(
    "outputs",
    [
        [],
        [_output(0), _output(1)],
        [_output(1)],
        [
            {
                "output_index": 0,
                "media_type": "image/png",
                "data_base64": "not-valid-base64",
            }
        ],
        [
            {
                "output_index": 0,
                "media_type": "image/jpeg",
                "data_base64": base64.b64encode(_image_bytes("image/png")).decode(),
            }
        ],
    ],
)
def test_executor_outputs_must_exactly_match_grants_and_image_contract(
    outputs: object,
) -> None:
    client, _executor, uploader = _client(executor=FakeExecutor(outputs=outputs))

    with client:
        response = client.post("/jobs/generate", json=_signed_request())

    assert response.status_code == 502
    assert response.json() == {"detail": "generation output invalid"}
    assert uploader.uploads == []


def test_decoded_output_size_is_bounded() -> None:
    oversized = base64.b64encode(b"x" * 1025).decode()
    executor = FakeExecutor(
        outputs=[
            {
                "output_index": 0,
                "media_type": "image/png",
                "data_base64": oversized,
            }
        ]
    )
    client, _executor, uploader = _client(
        executor=executor,
        settings=_settings(max_output_bytes=1024, max_total_output_bytes=1024),
    )

    with client:
        response = client.post("/jobs/generate", json=_signed_request())

    assert response.status_code == 502
    assert uploader.uploads == []


def test_near_black_output_requests_restart_before_upload() -> None:
    restart_event = asyncio.Event()
    executor = FakeExecutor(outputs=[_output_for_content(_solid_image_bytes((4, 4, 4)))])
    uploader = FakeUploader()
    app = create_worker_app(
        settings=_settings(),
        executor=executor,
        uploader=uploader,
        worker_restart_event=restart_event,
        now=lambda: NOW,
    )

    with TestClient(app) as client:
        response = client.post("/jobs/generate", json=_signed_request())

    assert response.status_code == 502
    assert response.json() == {"detail": "generation output invalid"}
    assert restart_event.is_set()
    assert uploader.uploads == []


def test_dark_nonblank_output_is_accepted_without_restart() -> None:
    restart_event = asyncio.Event()
    executor = FakeExecutor(outputs=[_output_for_content(_solid_image_bytes((5, 0, 0)))])
    uploader = FakeUploader()
    app = create_worker_app(
        settings=_settings(),
        executor=executor,
        uploader=uploader,
        worker_restart_event=restart_event,
        now=lambda: NOW,
    )

    with TestClient(app) as client:
        response = client.post("/jobs/generate", json=_signed_request())

    assert response.status_code == 200
    assert not restart_event.is_set()
    assert len(uploader.uploads) == 1


def test_progressive_job_stops_after_near_black_output_and_requests_restart() -> None:
    @dataclass
    class SequentialExecutor(FakeExecutor):
        generated_outputs: list[dict[str, object]] = field(default_factory=list)

        def execute(self, workflow: dict[str, object]) -> object:
            output_index = len(self.workflows)
            self.workflows.append(workflow)
            return [self.generated_outputs[output_index]]

    restart_event = asyncio.Event()
    executor = SequentialExecutor(
        generated_outputs=[
            _output_for_content(_image_bytes()),
            _output_for_content(_solid_image_bytes((0, 0, 0))),
            _output_for_content(_image_bytes()),
        ]
    )
    uploader = FakeUploader()
    request = _unsigned_request(uploads=3)
    request["payload"]["workflow"] = _progressive_workflow(3)
    app = create_worker_app(
        settings=_settings(),
        executor=executor,
        uploader=uploader,
        worker_restart_event=restart_event,
        now=lambda: NOW,
    )

    with TestClient(app) as client:
        response = client.post("/jobs/generate", json=_sign_request(request))

    assert response.status_code == 502
    assert response.json() == {"detail": "generation output invalid"}
    assert restart_event.is_set()
    assert len(executor.workflows) == 2
    assert [upload.grant.output_index for upload in uploader.uploads] == [0]


def test_executor_and_upload_failures_are_redacted() -> None:
    executor = FakeExecutor(execute_error=RuntimeError("private workflow detail"))
    client, _executor, _uploader = _client(executor=executor)
    with client:
        generation_failure = client.post("/jobs/generate", json=_signed_request())

    uploader = FakeUploader(error=RuntimeError("signed URL and storage response"))
    client, _executor, _uploader = _client(uploader=uploader)
    with client:
        upload_failure = client.post("/jobs/generate", json=_signed_request())

    assert generation_failure.status_code == 502
    assert generation_failure.json() == {"detail": "generation failed"}
    assert "private" not in generation_failure.text
    assert upload_failure.status_code == 502
    assert upload_failure.json() == {"detail": "upload failed"}
    assert "storage" not in upload_failure.text


@pytest.mark.asyncio
async def test_httpx_adapter_posts_multipart_without_following_redirects() -> None:
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(307, headers={"location": f"{UPLOAD_ORIGIN}/redirected"})

    async with httpx2.AsyncClient(
        transport=httpx2.MockTransport(handler),
        follow_redirects=False,
        trust_env=False,
    ) as client:
        adapter = HttpxMultipartUploader(client=client)
        grant = UploadGrant.model_validate(
            _unsigned_request()["payload"]["uploads"][0],
            strict=True,
        )
        with pytest.raises(WorkerUploadError, match="upload failed"):
            await adapter.upload(
                grant=grant,
                content=b"image-bytes",
                media_type="image/png",
            )

    assert len(requests) == 1
    body = requests[0].content
    assert requests[0].url == f"{UPLOAD_ORIGIN}/staging/output-0"
    assert b'name="file"' in body
    assert b"image-bytes" in body
    assert b"private-policy-value" in body


@pytest.mark.asyncio
async def test_httpx_payload_downloader_rejects_redirects_without_a_second_request() -> None:
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(307, headers={"location": f"{UPLOAD_ORIGIN}/redirected"})

    async with httpx2.AsyncClient(
        transport=httpx2.MockTransport(handler),
        follow_redirects=False,
        trust_env=False,
    ) as client:
        adapter = HttpxPayloadDownloader(client=client)
        with pytest.raises(WorkerPayloadDownloadError, match="download failed"):
            await adapter.download(
                url=f"{UPLOAD_ORIGIN}/staging/worker-requests/request.json",
                expected_bytes=123,
            )

    assert len(requests) == 1
    assert requests[0].headers["accept-encoding"] == "identity"
