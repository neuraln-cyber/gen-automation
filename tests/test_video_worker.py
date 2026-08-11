import asyncio
import hashlib
import io
import json
import struct
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx2
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from gen_automation.domain.signing import derive_public_key, encode_base64url
from gen_automation.video_worker.app import create_video_worker_app
from gen_automation.video_worker.comfy import (
    WAN_COMFY_HQ_WORKFLOW_SHA256,
    WAN_COMFY_WORKFLOW_REGISTRY_SHA256,
    WAN_COMFY_WORKFLOW_SHA256,
    NativeComfyWanExecutor,
    build_wan_workflow,
)
from gen_automation.video_worker.media import (
    FfprobeMp4Validator,
    ValidatedVideo,
    VideoOutputError,
)
from gen_automation.video_worker.model_integrity import (
    ModelArtifact,
    ModelIntegrityError,
    verify_model_artifact,
)
from gen_automation.video_worker.models import (
    AnimateEnvelope,
    SourceDownloadGrant,
    VideoUploadGrant,
    WorkerEnvironment,
    WorkerSettings,
)
from gen_automation.video_worker.profiles import (
    HQ_VIDEO_PROFILE,
    HQ_VIDEO_PROFILE_REGISTRATION,
    HQ_VIDEO_PROFILE_SHA256,
    PINNED_VIDEO_PROFILE,
    PINNED_VIDEO_PROFILE_SHA256,
    VIDEO_PROFILE_REGISTRY_SHA256,
    VideoRenderSpec,
)
from gen_automation.video_worker.runtime import (
    FfmpegPingPongEncoder,
    HttpxSourceDownloader,
    HttpxVideoUploader,
    SourceDownloadError,
    VideoUploadError,
)
from gen_automation.video_worker.security import calculate_signature, canonical_signing_payload

NOW = 2_000_000_000
TEST_PRIVATE_KEY = encode_base64url(bytes(range(1, 33)))
TEST_PUBLIC_KEY = derive_public_key(TEST_PRIVATE_KEY)
SOURCE_ORIGIN = "https://sources.example.test"
UPLOAD_ORIGIN = "https://uploads.example.test"


def _image_bytes(width: int = 8, height: int = 6) -> bytes:
    image = Image.new("RGB", (width, height), color=(20, 40, 60))
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _box(name: bytes, payload: bytes = b"") -> bytes:
    return struct.pack(">I4s", 8 + len(payload), name) + payload


def _mp4_stub(*, faststart: bool = True) -> bytes:
    ftyp = _box(b"ftyp", b"isom\x00\x00\x02\x00isomiso2")
    moov = _box(b"moov")
    mdat = _box(b"mdat", b"video-payload")
    return ftyp + (moov + mdat if faststart else mdat + moov)


@dataclass
class FakeDownloader:
    content: bytes
    error: Exception | None = None
    calls: list[tuple[SourceDownloadGrant, Path]] = field(default_factory=list)

    async def download(self, *, grant: SourceDownloadGrant, destination: Path) -> None:
        self.calls.append((grant, destination))
        if self.error is not None:
            raise self.error
        await asyncio.to_thread(destination.write_bytes, self.content)


@dataclass
class FakeExecutor:
    ready: bool = True
    error: Exception | None = None
    calls: list[dict[str, object]] = field(default_factory=list)

    def is_ready(self) -> bool:
        return self.ready

    def render(
        self,
        *,
        profile: object,
        render_spec: VideoRenderSpec,
        source_path: Path,
        native_frames_path: Path,
        prompt: str,
        negative_prompt: str,
        seed: int,
    ) -> None:
        self.calls.append(
            {
                "profile": profile,
                "render_spec": render_spec,
                "source_path": source_path,
                "native_frames_path": native_frames_path,
                "prompt": prompt,
                "negative_prompt": negative_prompt,
                "seed": seed,
                "source": source_path.read_bytes(),
            }
        )
        if self.error is not None:
            raise self.error
        for index in range(render_spec.native_frame_count):
            (native_frames_path / f"frame-{index:06d}.png").write_bytes(b"frame")


@dataclass
class FakeLoopEncoder:
    error: Exception | None = None
    calls: list[tuple[Path, Path, VideoRenderSpec]] = field(default_factory=list)

    def encode(
        self,
        *,
        native_frames_path: Path,
        output_path: Path,
        render_spec: VideoRenderSpec,
    ) -> None:
        self.calls.append((native_frames_path, output_path, render_spec))
        if self.error is not None:
            raise self.error
        output_path.write_bytes(_mp4_stub())


@dataclass
class FakeValidator:
    error: Exception | None = None
    calls: list[tuple[Path, object, int]] = field(default_factory=list)

    def validate(
        self,
        *,
        path: Path,
        profile: object,
        render_spec: VideoRenderSpec,
        max_bytes: int,
    ) -> object:
        del render_spec
        self.calls.append((path, profile, max_bytes))
        if self.error is not None:
            raise self.error
        content = path.read_bytes()
        return ValidatedVideo(
            sha256=hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
            width=1280,
            height=704,
            duration_seconds=5.0,
            fps=24.0,
        )


@dataclass
class FakeUploader:
    error: Exception | None = None
    calls: list[tuple[VideoUploadGrant, Path, bytes]] = field(default_factory=list)

    async def upload(self, *, grant: VideoUploadGrant, source: Path) -> None:
        if self.error is not None:
            raise self.error
        content = await asyncio.to_thread(source.read_bytes)
        self.calls.append((grant, source, content))


def _settings(staging_root: Path, **overrides: object) -> WorkerSettings:
    values: dict[str, object] = {
        "environment": WorkerEnvironment.TEST,
        "verification_keys": {"video-key-1": TEST_PUBLIC_KEY},
        "allowed_source_origins": frozenset({SOURCE_ORIGIN}),
        "allowed_upload_origin": UPLOAD_ORIGIN,
        "staging_root": staging_root,
    }
    values.update(overrides)
    return WorkerSettings.model_validate(values)


def _unsigned_request(source: bytes | None = None) -> dict[str, Any]:
    resolved_source = source or _image_bytes()
    return {
        "version": "video-worker.v1",
        "key_id": "video-key-1",
        "issued_at": NOW - 5,
        "expires_at": NOW + 60,
        "payload": {
            "job_id": "job-1",
            "attempt_id": "attempt-1",
            "profile_id": PINNED_VIDEO_PROFILE.profile_id,
            "source": {
                "asset_id": "source-asset-1",
                "url": f"{SOURCE_ORIGIN}/private/source.png?signature=source-secret",
                "content_type": "image/png",
                "size_bytes": len(resolved_source),
                "sha256": hashlib.sha256(resolved_source).hexdigest(),
            },
            "upload": {
                "asset_id": "video-asset-1",
                "upload_attempt_id": "video-upload-1",
                "content_type": "video/mp4",
                "url": f"{UPLOAD_ORIGIN}/private/output",
                "fields": {
                    "key": "private/video.mp4",
                    "policy": "private-policy-value",
                    "x-amz-signature": "upload-secret",
                },
            },
            "prompt": "subtle character motion and a slow camera push",
            "negative_prompt": "camera shake, flicker",
            "seed": 42,
            "native_frame_count": 73,
            "fps": 24,
            "width": 832,
            "height": 480,
            "loop_mode": "ping_pong",
        },
        "signature": "A" * 86,
    }


def _signed_request(source: bytes | None = None) -> dict[str, Any]:
    raw = _unsigned_request(source)
    return _sign_request(raw)


def _sign_request(raw: dict[str, Any]) -> dict[str, Any]:
    envelope = AnimateEnvelope.model_validate(raw, strict=True)
    raw["signature"] = calculate_signature(envelope, TEST_PRIVATE_KEY)
    return raw


def _client(
    staging_root: Path,
    *,
    downloader: FakeDownloader | None = None,
    executor: FakeExecutor | None = None,
    validator: FakeValidator | None = None,
    uploader: FakeUploader | None = None,
    loop_encoder: FakeLoopEncoder | None = None,
    settings: WorkerSettings | None = None,
) -> tuple[TestClient, FakeDownloader, FakeExecutor, FakeValidator, FakeUploader]:
    resolved_downloader = downloader or FakeDownloader(_image_bytes())
    resolved_executor = executor or FakeExecutor()
    resolved_validator = validator or FakeValidator()
    resolved_uploader = uploader or FakeUploader()
    resolved_loop_encoder = loop_encoder or FakeLoopEncoder()
    app = create_video_worker_app(
        settings=settings or _settings(staging_root),
        downloader=resolved_downloader,
        executor=resolved_executor,
        validator=resolved_validator,
        uploader=resolved_uploader,
        loop_encoder=resolved_loop_encoder,
        now=lambda: NOW,
    )
    return (
        TestClient(app),
        resolved_downloader,
        resolved_executor,
        resolved_validator,
        resolved_uploader,
    )


def test_pinned_profile_descriptor_is_stable() -> None:
    assert PINNED_VIDEO_PROFILE.descriptor_sha256 == PINNED_VIDEO_PROFILE_SHA256
    assert PINNED_VIDEO_PROFILE.profile_id == "wan2.2-ti2v-5b-comfy-v1"
    assert PINNED_VIDEO_PROFILE.model_revision == "fb1388adc906ab39ffc26ee40e96b22886b56bc4"
    assert PINNED_VIDEO_PROFILE.default_native_frame_count == 73
    assert PINNED_VIDEO_PROFILE.permitted_native_frame_counts == (73, 121)


def test_standard_v1_wire_remains_legacy_worker_compatible() -> None:
    raw = _unsigned_request()
    envelope = AnimateEnvelope.model_validate(raw, strict=True)
    serialized = envelope.model_dump(mode="json", exclude_none=True)

    assert envelope.version == "video-worker.v1"
    assert "profile_sha256" not in raw["payload"]
    assert "profile_sha256" not in serialized["payload"]
    assert set(serialized["payload"]) == {
        "job_id",
        "attempt_id",
        "profile_id",
        "source",
        "upload",
        "prompt",
        "negative_prompt",
        "seed",
        "native_frame_count",
        "fps",
        "width",
        "height",
        "loop_mode",
    }
    assert hashlib.sha256(canonical_signing_payload(envelope)).hexdigest() == (
        "3f27f747f5bde08acefe6c8505f9b020fab4b1af716749c24de49ac7eb24f0fb"
    )
    assert calculate_signature(envelope, TEST_PRIVATE_KEY) == (
        "oH4DWV2oicXvONYHgweiAlBBrtzGH0pUdihxNLedIFIuJlFOjGBwSVdGkY7I0wAi1dpMzzhcUzp4MaZulNmzBw"
    )


def test_hq_profile_and_workflow_contracts_are_stable_and_camera_locked() -> None:
    assert HQ_VIDEO_PROFILE.descriptor_sha256 == HQ_VIDEO_PROFILE_SHA256
    assert (HQ_VIDEO_PROFILE.portrait_width, HQ_VIDEO_PROFILE.portrait_height) == (1152, 1472)
    assert HQ_VIDEO_PROFILE.permitted_native_frame_counts == (73,)
    assert HQ_VIDEO_PROFILE_REGISTRATION.execution_timeout_seconds == 5400
    assert HQ_VIDEO_PROFILE_REGISTRATION.max_attempts == 1
    assert HQ_VIDEO_PROFILE_REGISTRATION.estimated_runtime_seconds(73) == 3058
    assert HQ_VIDEO_PROFILE_REGISTRATION.workflow_sha256 == WAN_COMFY_HQ_WORKFLOW_SHA256
    assert HQ_VIDEO_PROFILE_REGISTRATION.job_contract_sha256 == (
        "00fb341e491f295b2db16a32626a6383d83c6cda88978b29479caf245c817387"
    )
    assert VIDEO_PROFILE_REGISTRY_SHA256 == (
        "8f536e93c5e097aa70c8036c778c1a454fb8ce8ad5d0fd1dd614b67afd4c80eb"
    )
    assert WAN_COMFY_HQ_WORKFLOW_SHA256 == (
        "01c5b5350cab319e5cf7ec407971d7fbb269c4eb1e3c91c27c5c3cbb793a5151"
    )
    assert WAN_COMFY_WORKFLOW_REGISTRY_SHA256 == (
        "19e4426429def8dc731b2b8802f714fa85a9e3ec61b5fcc79609c26363783f69"
    )

    workflow = build_wan_workflow(
        source_filename="SOURCE.png",
        output_prefix="OUTPUT/frame",
        prompt="one blink and one breath",
        negative_prompt="",
        seed=42,
        render_spec=VideoRenderSpec(73, 24, 1152, 1472, "ping_pong"),
        profile=HQ_VIDEO_PROFILE,
    )
    negative = str(workflow["6"]["inputs"]["text"])
    sampler = workflow["9"]["inputs"]

    assert "static" not in negative.split(", ")
    assert "camera shake" in negative
    assert "background drift" in negative
    assert workflow["7"]["inputs"]["width"] == 1152
    assert workflow["7"]["inputs"]["height"] == 1472
    assert sampler["steps"] == 30
    assert sampler["cfg"] == 5.0
    assert sampler["sampler_name"] == "uni_pc"
    assert sampler["scheduler"] == "simple"
    assert sampler["denoise"] == 1.0


def test_signed_hq_portrait_job_selects_hq_profile(tmp_path: Path) -> None:
    source = _image_bytes(width=6, height=8)
    raw = _unsigned_request(source)
    raw["version"] = "video-worker.v2"
    raw["payload"].update(
        {
            "profile_id": HQ_VIDEO_PROFILE.profile_id,
            "profile_sha256": HQ_VIDEO_PROFILE_REGISTRATION.job_contract_sha256,
            "native_frame_count": 73,
            "width": 1152,
            "height": 1472,
        }
    )
    client, _downloader, executor, validator, _uploader = _client(
        tmp_path,
        downloader=FakeDownloader(source),
    )

    with client:
        response = client.post("/jobs/generate", json=_sign_request(raw))

    assert response.status_code == 200
    assert response.json()["version"] == "video-worker.v2"
    assert response.json()["profile_id"] == HQ_VIDEO_PROFILE.profile_id
    assert (response.json()["width"], response.json()["height"]) == (1152, 1472)
    assert executor.calls[0]["profile"] is HQ_VIDEO_PROFILE
    assert validator.calls[0][1] is HQ_VIDEO_PROFILE


def test_signed_one_image_job_uploads_one_mp4_and_cleans_staging(tmp_path: Path) -> None:
    client, downloader, executor, validator, uploader = _client(tmp_path)

    with client:
        response = client.post("/jobs/generate", json=_signed_request())

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "version": "video-worker.v1",
        "job_id": "job-1",
        "attempt_id": "attempt-1",
        "status": "succeeded",
        "profile_id": "wan2.2-ti2v-5b-comfy-v1",
        "source_asset_id": "source-asset-1",
        "output_asset_id": "video-asset-1",
        "upload_attempt_id": "video-upload-1",
        "output_sha256": hashlib.sha256(_mp4_stub()).hexdigest(),
        "output_size_bytes": len(_mp4_stub()),
        "loop_mode": "ping_pong",
        "fps": 24,
        "width": 832,
        "height": 480,
        "native_frame_count": 73,
        "native_duration_seconds": 3.041667,
        "output_frame_count": 144,
        "output_duration_seconds": 6.0,
    }
    assert (
        len(downloader.calls)
        == len(executor.calls)
        == len(validator.calls)
        == len(uploader.calls)
        == 1
    )
    source_grant, staged_source = downloader.calls[0]
    assert source_grant.sha256 == hashlib.sha256(_image_bytes()).hexdigest()
    assert source_grant.size_bytes == len(_image_bytes())
    assert executor.calls[0]["profile"] is PINNED_VIDEO_PROFILE
    assert executor.calls[0]["prompt"] == "subtle character motion and a slow camera push"
    assert executor.calls[0]["negative_prompt"] == "camera shake, flicker"
    assert executor.calls[0]["seed"] == 42
    upload_grant, staged_video, uploaded = uploader.calls[0]
    assert upload_grant.content_type == "video/mp4"
    assert uploaded == _mp4_stub()
    assert not staged_source.exists()
    assert not staged_video.exists()
    assert list(tmp_path.iterdir()) == []


def test_health_and_readiness_do_not_expose_adapter_details(tmp_path: Path) -> None:
    client, _downloader, executor, _validator, _uploader = _client(tmp_path)
    with client:
        assert client.get("/health").json() == {
            "status": "ok",
            "version": "video-worker.v1",
        }
        ready = client.get("/ready")
        executor.ready = False
        not_ready = client.get("/ready")

    assert ready.status_code == 200
    assert ready.json() == {"status": "ready", "version": "video-worker.v1"}
    assert not_ready.status_code == 503
    assert not_ready.json() == {"status": "not_ready", "version": "video-worker.v1"}


@pytest.mark.parametrize("field", ["source", "upload"])
def test_signed_grants_cannot_be_tampered(field: str, tmp_path: Path) -> None:
    request = _signed_request()
    if field == "source":
        request["payload"]["source"]["sha256"] = "0" * 64
    else:
        request["payload"]["upload"]["url"] = f"{UPLOAD_ORIGIN}/different"
    client, downloader, executor, _validator, uploader = _client(tmp_path)

    with client:
        response = client.post("/jobs/generate", json=request)

    assert response.status_code == 401
    assert response.json() == {"detail": "invalid authorization"}
    assert downloader.calls == []
    assert executor.calls == []
    assert uploader.calls == []


def test_failure_cleans_job_directory_and_redacts_adapter_error(tmp_path: Path) -> None:
    executor = FakeExecutor(error=RuntimeError("private model path and prompt"))
    client, _downloader, _executor, _validator, uploader = _client(
        tmp_path,
        executor=executor,
    )

    with client:
        response = client.post("/jobs/generate", json=_signed_request())

    assert response.status_code == 502
    assert response.json() == {"detail": "generation failed"}
    assert "private" not in response.text
    assert uploader.calls == []
    assert list(tmp_path.iterdir()) == []


def test_portrait_dimensions_are_derived_from_signed_source_orientation(tmp_path: Path) -> None:
    portrait = _image_bytes(6, 8)
    request = _unsigned_request(portrait)
    request["payload"]["width"] = 480
    request["payload"]["height"] = 832
    client, _downloader, executor, _validator, _uploader = _client(
        tmp_path,
        downloader=FakeDownloader(portrait),
    )

    with client:
        response = client.post("/jobs/generate", json=_sign_request(request))

    assert response.status_code == 200
    render_spec = executor.calls[0]["render_spec"]
    assert isinstance(render_spec, VideoRenderSpec)
    assert (render_spec.width, render_spec.height) == (480, 832)


def test_successful_retry_is_cached_without_redownload_or_regeneration(tmp_path: Path) -> None:
    client, downloader, executor, _validator, uploader = _client(tmp_path)
    request = _signed_request()

    with client:
        first = client.post("/jobs/generate", json=request)
        second = client.post("/jobs/generate", json=request)

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert len(downloader.calls) == len(executor.calls) == len(uploader.calls) == 1


def test_signed_models_do_not_repr_urls_policy_signature_or_prompt() -> None:
    envelope = AnimateEnvelope.model_validate(_signed_request(), strict=True)
    rendered = repr(envelope)

    for secret in (
        "source-secret",
        "private-policy-value",
        "upload-secret",
        envelope.signature,
        envelope.payload.prompt,
        envelope.payload.negative_prompt,
    ):
        assert secret not in rendered


def test_ping_pong_encoder_excludes_duplicate_endpoints(tmp_path: Path) -> None:
    frames = tmp_path / "frames"
    frames.mkdir()
    for index in range(73):
        (frames / f"frame-{index:06d}.png").write_bytes(b"png")
    output = tmp_path / "output.mp4"
    observed_manifest: list[str] = []
    observed_command: list[str] = []

    def runner(command: list[str], _timeout: float) -> subprocess.CompletedProcess[bytes]:
        observed_command.extend(command)
        manifest_path = Path(command[command.index("-i") + 1])
        observed_manifest.extend(manifest_path.read_text(encoding="ascii").splitlines())
        output.write_bytes(_mp4_stub())
        return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

    FfmpegPingPongEncoder(runner=runner).encode(
        native_frames_path=frames,
        output_path=output,
        render_spec=VideoRenderSpec(73, 24, 832, 480, "ping_pong"),
    )

    file_lines = [line for line in observed_manifest if line.startswith("file ")]
    encoded_lines = file_lines[:-1]
    assert len(encoded_lines) == 144
    assert encoded_lines[0] == "file 'frame-000000.png'"
    assert encoded_lines[72] == "file 'frame-000072.png'"
    assert encoded_lines[73] == "file 'frame-000071.png'"
    assert encoded_lines[-1] == "file 'frame-000001.png'"
    assert encoded_lines.count("file 'frame-000000.png'") == 1
    assert encoded_lines.count("file 'frame-000072.png'") == 1
    assert observed_command[observed_command.index("-frames:v") + 1] == "144"
    assert "+faststart" in observed_command


def test_model_integrity_checks_exact_size_and_hash(tmp_path: Path) -> None:
    model = tmp_path / "model.safetensors"
    model.write_bytes(b"model-bytes")
    valid = ModelArtifact(
        path=model,
        size_bytes=len(b"model-bytes"),
        sha256=hashlib.sha256(b"model-bytes").hexdigest(),
    )
    verify_model_artifact(valid)

    with pytest.raises(ModelIntegrityError, match="integrity"):
        verify_model_artifact(ModelArtifact(model, len(b"model-bytes"), "0" * 64))


def test_native_comfy_adapter_submits_pinned_graph_and_cleans_intermediates(
    tmp_path: Path,
) -> None:
    input_directory = tmp_path / "comfy-input"
    output_directory = tmp_path / "comfy-output"
    input_directory.mkdir()
    output_directory.mkdir()
    native_frames = tmp_path / "native-frames"
    source = tmp_path / "source.png"
    source.write_bytes(_image_bytes())
    prompt_id = "12345678-1234-1234-1234-123456789abc"
    generated_images: list[dict[str, object]] = []
    submitted_workflow: dict[str, Any] = {}

    frame = Image.new("RGB", (832, 480), color=(40, 60, 80))
    frame_buffer = io.BytesIO()
    frame.save(frame_buffer, format="PNG")
    frame_bytes = frame_buffer.getvalue()

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.method == "GET" and request.url.path == "/system_stats":
            return httpx2.Response(200, json={"system": {}}, request=request)
        if request.method == "POST" and request.url.path == "/prompt":
            payload = json.loads(request.content)
            submitted_workflow.update(payload["prompt"])
            prefix = submitted_workflow["11"]["inputs"]["filename_prefix"]
            subfolder = str(Path(prefix).parent).replace("\\", "/")
            destination = output_directory / Path(subfolder)
            destination.mkdir(parents=True)
            for index in range(73):
                filename = f"frame_{index:05d}_.png"
                (destination / filename).write_bytes(frame_bytes)
                generated_images.append(
                    {"filename": filename, "subfolder": subfolder, "type": "output"}
                )
            return httpx2.Response(200, json={"prompt_id": prompt_id}, request=request)
        if request.method == "GET" and request.url.path == f"/history/{prompt_id}":
            return httpx2.Response(
                200,
                json={
                    prompt_id: {
                        "status": {"status_str": "success", "completed": True},
                        "outputs": {"11": {"images": generated_images}},
                    }
                },
                request=request,
            )
        raise AssertionError(f"unexpected Comfy request: {request.method} {request.url.path}")

    with httpx2.Client(
        base_url="http://127.0.0.1:8188",
        transport=httpx2.MockTransport(handler),
        trust_env=False,
        follow_redirects=False,
    ) as client:
        executor = NativeComfyWanExecutor(
            client=client,
            input_directory=input_directory,
            output_directory=output_directory,
            execution_timeout_seconds=2,
            poll_interval_seconds=0.001,
        )
        assert executor.is_ready() is True
        executor.render(
            profile=PINNED_VIDEO_PROFILE,
            render_spec=VideoRenderSpec(73, 24, 832, 480, "ping_pong"),
            source_path=source,
            native_frames_path=native_frames,
            prompt="move gently",
            negative_prompt="camera shake",
            seed=42,
        )

    assert WAN_COMFY_WORKFLOW_SHA256 == (
        "ecba3eef1c14abcd4d0d2ba3cec1f53042767f1a177d833dcfd3fd6b11f09ab3"
    )
    assert submitted_workflow["1"]["class_type"] == "UNETLoader"
    assert submitted_workflow["7"]["class_type"] == "Wan22ImageToVideoLatent"
    assert submitted_workflow["7"]["inputs"]["length"] == 73
    assert submitted_workflow["6"]["inputs"]["text"].startswith("camera shake, ")
    assert len(list(native_frames.glob("frame-*.png"))) == 73
    assert list(input_directory.iterdir()) == []
    assert list((output_directory / "video-worker").iterdir()) == []


@pytest.mark.asyncio
async def test_http_source_downloader_enforces_signed_size_hash_type_and_no_redirects(
    tmp_path: Path,
) -> None:
    content = _image_bytes()
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(
            200,
            content=content,
            headers={
                "content-type": "image/png",
                "content-length": str(len(content)),
            },
        )

    grant = SourceDownloadGrant.model_validate(
        _unsigned_request()["payload"]["source"],
        strict=True,
    )
    destination = tmp_path / "source.png"
    async with httpx2.AsyncClient(
        transport=httpx2.MockTransport(handler),
        trust_env=False,
        follow_redirects=False,
    ) as client:
        await HttpxSourceDownloader(client, timeout_seconds=2).download(
            grant=grant,
            destination=destination,
        )

    assert destination.read_bytes() == content
    assert len(requests) == 1
    assert requests[0].headers["accept-encoding"] == "identity"

    bad_grant = grant.model_copy(update={"sha256": "0" * 64})
    bad_destination = tmp_path / "bad.png"
    async with httpx2.AsyncClient(
        transport=httpx2.MockTransport(handler),
        trust_env=False,
        follow_redirects=False,
    ) as client:
        with pytest.raises(SourceDownloadError, match="source download failed"):
            await HttpxSourceDownloader(client, timeout_seconds=2).download(
                grant=bad_grant,
                destination=bad_destination,
            )
    assert not bad_destination.exists()


@pytest.mark.asyncio
async def test_http_upload_adapter_does_not_follow_redirects(tmp_path: Path) -> None:
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(307, headers={"location": f"{UPLOAD_ORIGIN}/redirected"})

    grant = VideoUploadGrant.model_validate(
        _unsigned_request()["payload"]["upload"],
        strict=True,
    )
    source = tmp_path / "output.mp4"
    source.write_bytes(_mp4_stub())
    async with httpx2.AsyncClient(
        transport=httpx2.MockTransport(handler),
        trust_env=False,
        follow_redirects=False,
    ) as client:
        with pytest.raises(VideoUploadError, match="upload failed"):
            await HttpxVideoUploader(client, timeout_seconds=2).upload(
                grant=grant,
                source=source,
            )

    assert len(requests) == 1
    assert requests[0].url == f"{UPLOAD_ORIGIN}/private/output"
    assert b'name="file"' in requests[0].content
    assert b"output.mp4" in requests[0].content


def _probe_result(path: Path, **video_overrides: object) -> subprocess.CompletedProcess[str]:
    video: dict[str, object] = {
        "codec_type": "video",
        "codec_name": "h264",
        "pix_fmt": "yuv420p",
        "width": 832,
        "height": 480,
        "avg_frame_rate": "24/1",
        "nb_frames": "144",
        "nb_read_frames": "144",
    }
    video.update(video_overrides)
    payload = {
        "streams": [video],
        "format": {
            "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
            "duration": "6.000000",
            "size": str(path.stat().st_size),
        },
    }
    return subprocess.CompletedProcess([], 0, stdout=json.dumps(payload), stderr="")


def test_ffprobe_validator_requires_h264_yuv420p_24fps_and_faststart(tmp_path: Path) -> None:
    output = tmp_path / "output.mp4"
    output.write_bytes(_mp4_stub())
    commands: list[tuple[list[str], float]] = []

    def runner(command: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        commands.append((command, timeout))
        return _probe_result(output)

    validated = FfprobeMp4Validator(
        ffprobe_path="/pinned/ffprobe",
        timeout_seconds=7,
        runner=runner,
    ).validate(
        path=output,
        profile=PINNED_VIDEO_PROFILE,
        render_spec=VideoRenderSpec(73, 24, 832, 480, "ping_pong"),
        max_bytes=1024,
    )

    assert validated.sha256 == hashlib.sha256(output.read_bytes()).hexdigest()
    assert validated.width == 832
    assert commands == [
        (
            [
                "/pinned/ffprobe",
                "-v",
                "error",
                "-count_frames",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                "--",
                str(output),
            ],
            7,
        )
    ]

    output.write_bytes(_mp4_stub(faststart=False))
    with pytest.raises(VideoOutputError, match="video output invalid"):
        FfprobeMp4Validator(runner=runner).validate(
            path=output,
            profile=PINNED_VIDEO_PROFILE,
            render_spec=VideoRenderSpec(73, 24, 832, 480, "ping_pong"),
            max_bytes=1024,
        )


@pytest.mark.parametrize(
    ("override", "value"),
    [
        ("codec_name", "hevc"),
        ("pix_fmt", "yuv444p"),
        ("avg_frame_rate", "30/1"),
        ("width", 1920),
        ("nb_read_frames", "122"),
    ],
)
def test_ffprobe_validator_rejects_non_profile_video(
    tmp_path: Path,
    override: str,
    value: object,
) -> None:
    output = tmp_path / "output.mp4"
    output.write_bytes(_mp4_stub())

    def runner(_command: list[str], _timeout: float) -> subprocess.CompletedProcess[str]:
        return _probe_result(output, **{override: value})

    with pytest.raises(VideoOutputError, match="video output invalid"):
        FfprobeMp4Validator(runner=runner).validate(
            path=output,
            profile=PINNED_VIDEO_PROFILE,
            render_spec=VideoRenderSpec(73, 24, 832, 480, "ping_pong"),
            max_bytes=1024,
        )
