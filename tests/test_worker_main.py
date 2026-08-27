import asyncio
import hashlib
import io
import json
import os
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import pytest

import gen_automation.gpu_worker.main as worker_main
from gen_automation.domain.signing import derive_public_key, encode_base64url
from gen_automation.gpu_worker.artifacts import (
    ArtifactBootstrapResult,
    ArtifactKind,
    MaterializedArtifact,
    ModelArtifactSpec,
    calculate_manifest_sha256,
)
from gen_automation.gpu_worker.bootstrap import (
    S3ArtifactDownloader,
    WorkerBootstrapConfigurationError,
    WorkerRuntimeSettings,
)
from gen_automation.gpu_worker.main import (
    _ComfyLaunchSettings,
    _ManagedChild,
    _ManagedChildren,
    _manifest_requires_fp32_vae,
    _monitor_comfy,
    _serve_worker_lifecycle,
    _SwitchableWorkerApplication,
    _wait_for_comfy_ready,
    bootstrap_worker_models,
    build_comfy_command,
    build_comfy_environment,
    build_queue_worker_environment,
    ensure_runtime_directories,
    harden_parent_process,
    stop_comfy,
    write_verified_detector_whitelist,
)
from gen_automation.gpu_worker.models import WorkerEnvironment

WORKER_SIGNING_PRIVATE_KEY = encode_base64url(bytes(range(1, 33)))
WORKER_VERIFICATION_PUBLIC_KEY = derive_public_key(WORKER_SIGNING_PRIVATE_KEY)


def test_parent_hardening_applies_linux_controls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    umask_calls: list[int] = []
    hardening_calls: list[bool] = []
    monkeypatch.setattr(worker_main.sys, "platform", "linux")
    monkeypatch.setattr(worker_main.os, "umask", umask_calls.append)
    monkeypatch.setattr(
        worker_main,
        "_apply_linux_process_hardening",
        lambda: hardening_calls.append(True),
    )

    harden_parent_process(WorkerEnvironment.PRODUCTION)

    assert umask_calls == [0o077]
    assert hardening_calls == [True]


async def _asgi_request(
    application: Any,
    path: str,
) -> tuple[int, bytes]:
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    await application(
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 1),
            "server": ("127.0.0.1", 8000),
        },
        receive,
        send,
    )
    status = next(message["status"] for message in messages if "status" in message)
    body = b"".join(message.get("body", b"") for message in messages)
    return cast(int, status), body


async def test_bootstrap_probe_is_live_but_not_ready_and_hands_off_in_place() -> None:
    router = _SwitchableWorkerApplication()

    assert await _asgi_request(router, "/health") == (200, b'{"status":"bootstrapping"}')
    assert await _asgi_request(router, "/ready") == (503, b'{"status":"not_ready"}')

    async def active_application(
        _scope: object,
        _receive: object,
        send: Any,
    ) -> None:
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    router.activate(cast(Any, active_application))

    assert await _asgi_request(router, "/health") == (204, b"")


async def test_switchable_router_tracks_generation_until_application_returns_and_cancels() -> None:
    router = _SwitchableWorkerApplication()
    response_sent = asyncio.Event()
    release_response = asyncio.Event()

    async def generation_application(
        _scope: object,
        _receive: object,
        send: Any,
    ) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}", "more_body": False})
        response_sent.set()
        await release_response.wait()

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(_message: dict[str, Any]) -> None:
        return None

    router.activate(cast(Any, generation_application))
    request = asyncio.create_task(
        router(
            cast(
                Any,
                {
                    "type": "http",
                    "method": "POST",
                    "path": "/jobs/generate",
                },
            ),
            receive,
            cast(Any, send),
        )
    )
    await response_sent.wait()

    assert router.has_unsafe_active_request()

    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request

    assert not router.has_unsafe_active_request()


async def test_worker_lifecycle_handoff_closes_every_owned_resource(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    restart_events: list[asyncio.Event] = []

    class FakeServer:
        def __init__(self) -> None:
            self.started = False
            self._should_exit = False
            self.exit_event = asyncio.Event()

        @property
        def should_exit(self) -> bool:
            return self._should_exit

        @should_exit.setter
        def should_exit(self, value: bool) -> None:
            self._should_exit = value
            if value:
                self.exit_event.set()

        async def serve(self) -> None:
            self.started = True
            await self.exit_event.wait()

    class FakeProcess:
        def poll(self) -> None:
            return None

    class FakeExecutor:
        def is_ready(self) -> bool:
            events.append("comfy_ready")
            return True

        def close(self) -> None:
            events.append("executor_closed")

    class FakeLifespan:
        async def __aenter__(self) -> None:
            events.append("lifespan_entered")

        async def __aexit__(self, *_args: object) -> None:
            events.append("lifespan_exited")

    class FakeRouter:
        def lifespan_context(self, _application: object) -> FakeLifespan:
            return FakeLifespan()

    class FakeApplication:
        router = FakeRouter()

        async def __call__(self, _scope: object, _receive: object, _send: object) -> None:
            return None

    server = FakeServer()
    process = FakeProcess()

    async def bootstrap(_settings: WorkerRuntimeSettings) -> ArtifactBootstrapResult:
        events.append("models_bootstrapped")
        return ArtifactBootstrapResult(
            version="v1",
            manifest_sha256="a" * 64,
            artifacts=(),
        )

    monkeypatch.setattr(worker_main, "_build_switchable_server", lambda *_args, **_kw: server)
    monkeypatch.setattr(worker_main, "bootstrap_worker_models", bootstrap)
    monkeypatch.setattr(worker_main, "write_verified_detector_whitelist", lambda *_args: None)
    monkeypatch.setattr(worker_main, "ComfyExecutor", lambda **_kwargs: FakeExecutor())
    monkeypatch.setattr(worker_main, "start_comfy", lambda _settings: process)
    monkeypatch.setattr(
        worker_main,
        "start_salad_queue_worker",
        lambda _settings: events.append("queue_started") or None,
    )

    def create_application(**kwargs: object) -> FakeApplication:
        restart_event = cast(asyncio.Event, kwargs["worker_restart_event"])
        restart_events.append(restart_event)
        restart_event.set()
        return FakeApplication()

    monkeypatch.setattr(worker_main, "create_worker_app", create_application)
    monkeypatch.setattr(
        worker_main,
        "stop_comfy",
        lambda stopped: events.append("process_stopped") if stopped is process else None,
    )

    stage = ["runtime_settings"]
    restart_required = await _serve_worker_lifecycle(
        _settings(),
        startup_stage=stage,
        startup_started_at=worker_main.time.monotonic(),
    )

    assert restart_required is True
    assert len(restart_events) == 1
    assert restart_events[0].is_set()
    assert events == [
        "models_bootstrapped",
        "lifespan_entered",
        "comfy_ready",
        "queue_started",
        "lifespan_exited",
        "executor_closed",
        "process_stopped",
    ]


async def test_comfy_readiness_wait_is_bounded_before_queue_attachment() -> None:
    class FakeProcess:
        def poll(self) -> None:
            return None

    observations = iter((False, False, True))
    now = 0.0
    sleeps: list[float] = []

    def health_check() -> bool:
        return next(observations)

    async def advance(delay: float) -> None:
        nonlocal now
        sleeps.append(delay)
        now += delay

    await _wait_for_comfy_ready(
        cast(Any, FakeProcess()),
        health_check,
        timeout_seconds=1.0,
        poll_interval_seconds=0.1,
        monotonic=lambda: now,
        sleep=advance,
    )

    assert sleeps == [0.1, 0.1]


def test_parent_hardening_rejects_non_linux_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(worker_main.sys, "platform", "win32")
    monkeypatch.setattr(worker_main.os, "umask", lambda _mode: None)

    with pytest.raises(WorkerBootstrapConfigurationError):
        harden_parent_process(WorkerEnvironment.PRODUCTION)


def test_parent_hardening_allows_non_linux_tests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hardening_calls: list[bool] = []
    monkeypatch.setattr(worker_main.sys, "platform", "win32")
    monkeypatch.setattr(worker_main.os, "umask", lambda _mode: None)
    monkeypatch.setattr(
        worker_main,
        "_apply_linux_process_hardening",
        lambda: hardening_calls.append(True),
    )

    harden_parent_process(WorkerEnvironment.TEST)

    assert not hardening_calls


def _safetensors() -> bytes:
    body = b"\x00\x00\x00\x00"
    header = json.dumps(
        {"weight": {"data_offsets": [0, len(body)], "dtype": "F32", "shape": [1]}},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return len(header).to_bytes(8, "little") + header + body


def _manifest(content: bytes) -> tuple[str, str]:
    artifact = ModelArtifactSpec(
        logical_name="illustrious",
        kind=ArtifactKind.CHECKPOINT,
        source_object_id="models/illustrious.safetensors",
        source_object_version_id="model-version-1",
        sha256=hashlib.sha256(content).hexdigest(),
        exact_size_bytes=len(content),
        max_size_bytes=len(content),
        target_filename="illustrious.safetensors",
    )
    digest = calculate_manifest_sha256((artifact,))
    raw = json.dumps(
        {
            "version": "v1",
            "artifacts": [artifact.model_dump(mode="json")],
            "manifest_sha256": digest,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return raw, digest


def _settings(content: bytes | None = None, **changes: object) -> WorkerRuntimeSettings:
    raw_manifest, digest = _manifest(content or _safetensors())
    values: dict[str, object] = {
        "environment": WorkerEnvironment.TEST,
        "verification_keys": {"key-1": WORKER_VERIFICATION_PUBLIC_KEY},
        "allowed_upload_origin": "https://uploads.example.test",
        "model_manifest_json": raw_manifest,
        "model_manifest_sha256": digest,
        "runtime_admission_id": "1" * 32,
        "runtime_worker_instance_id": "instance-creator-1",
        "artifact_bucket": "models-private",
        "checkpoint_root": Path("/opt/comfyui/models/checkpoints"),
        "lora_root": Path("/opt/comfyui/models/loras"),
        "comfy_python": Path("/opt/worker-venv/bin/python"),
        "comfy_main": Path("/opt/comfyui/main.py"),
    }
    values.update(changes)
    return WorkerRuntimeSettings.model_validate(values)


def test_main_reports_validation_stage_without_secret_input(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sensitive_input = os.urandom(16).hex()
    values = _settings().model_dump()
    values["artifact_connect_timeout_seconds"] = sensitive_input

    def invalid_runtime_settings() -> WorkerRuntimeSettings:
        return WorkerRuntimeSettings.model_validate(values)

    monkeypatch.setattr(worker_main, "WorkerRuntimeSettings", invalid_runtime_settings)

    with pytest.raises(SystemExit) as raised:
        worker_main.main()

    stderr = capsys.readouterr().err
    assert raised.value.code == 78
    assert "stage=runtime_settings" in stderr
    assert "exception=ValidationError" in stderr
    assert "message=artifact_connect_timeout_seconds:" in stderr
    assert sensitive_input not in stderr


def test_main_reports_model_bootstrap_exception(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def fail_bootstrap(_settings: WorkerRuntimeSettings) -> ArtifactBootstrapResult:
        raise worker_main.ArtifactBootstrapError("artifact bootstrap failed")

    monkeypatch.setattr(worker_main, "WorkerRuntimeSettings", _settings)
    monkeypatch.setattr(worker_main, "harden_parent_process", lambda _environment: None)
    monkeypatch.setattr(worker_main, "bootstrap_worker_models", fail_bootstrap)

    with pytest.raises(SystemExit) as raised:
        worker_main.main()

    stderr = capsys.readouterr().err
    assert raised.value.code == 78
    assert "stage=bootstrap_probe_server status=ready" in stderr
    assert "GPU worker startup progress: stage=model_bootstrap status=started" in stderr
    assert "stage=model_bootstrap" in stderr
    assert "exception=ArtifactBootstrapError" in stderr
    assert "message=artifact bootstrap failed" in stderr
    assert WORKER_SIGNING_PRIVATE_KEY not in stderr


def test_main_exits_nonzero_when_worker_restart_is_requested(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def request_restart(
        _settings: WorkerRuntimeSettings,
        **_kwargs: object,
    ) -> bool:
        return True

    monkeypatch.setattr(worker_main, "WorkerRuntimeSettings", _settings)
    monkeypatch.setattr(worker_main, "harden_parent_process", lambda _environment: None)
    monkeypatch.setattr(worker_main, "_serve_worker_lifecycle", request_restart)

    with pytest.raises(SystemExit) as raised:
        worker_main.main()

    assert raised.value.code == 78
    assert "stage=managed_child_monitor" in capsys.readouterr().err


def test_worker_runtime_settings_contain_only_verification_keys() -> None:
    settings = _settings()
    worker_settings = settings.to_worker_settings()

    assert settings.verification_keys == {"key-1": WORKER_VERIFICATION_PUBLIC_KEY}
    assert worker_settings.verification_keys == {"key-1": WORKER_VERIFICATION_PUBLIC_KEY}
    assert WORKER_SIGNING_PRIVATE_KEY not in settings.model_dump_json()
    assert WORKER_SIGNING_PRIVATE_KEY not in worker_settings.model_dump_json()
    assert not hasattr(settings, "signing_private_key")
    assert not hasattr(worker_settings, "signing_private_key")
    assert settings.model_bootstrap_timeout_seconds == 3600.0
    assert settings.salad_queue_worker_log_level == "warn"


def test_build_comfy_command_is_loopback_offline_and_narrowly_whitelisted() -> None:
    command = build_comfy_command(_settings())

    assert command[:2] == ("/opt/worker-venv/bin/python", "/opt/comfyui/main.py")
    assert command[command.index("--listen") + 1] == "127.0.0.1"
    assert command[command.index("--port") + 1] == "8188"
    assert command[command.index("--models-directory") + 1] == "/opt/comfyui/models"
    assert "--disable-all-custom-nodes" in command
    whitelist_index = command.index("--whitelist-custom-nodes")
    assert command[whitelist_index + 1 : whitelist_index + 3] == (
        "ComfyUI-Impact-Pack",
        "ComfyUI-Impact-Subpack",
    )
    assert "--disable-api-nodes" in command
    assert "--disable-metadata" in command
    assert "--enable-manager" not in command


def test_anima_qwen_precision_launch_adds_each_pinned_flag_once() -> None:
    settings = _settings()
    split_launch = _ComfyLaunchSettings.from_runtime_settings(settings, fp32_vae=True)
    checkpoint_launch = _ComfyLaunchSettings.from_runtime_settings(settings, fp32_vae=False)

    split_command = build_comfy_command(split_launch)
    assert split_command[-2:] == ("--fp16-unet", "--fp32-vae")
    assert split_command.count("--fp16-unet") == 1
    assert split_command.count("--fp32-vae") == 1
    assert build_comfy_command(split_launch) == split_command
    for command in (build_comfy_command(checkpoint_launch), build_comfy_command(settings)):
        assert "--fp16-unet" not in command
        assert "--fp32-vae" not in command


def test_fp32_vae_policy_is_scoped_to_anima_qwen_split_manifest() -> None:
    def artifact(kind: ArtifactKind, logical_name: str) -> MaterializedArtifact:
        return MaterializedArtifact(
            logical_name=logical_name,
            kind=kind,
            target_filename=f"{logical_name}.safetensors",
            sha256="a" * 64,
            size_bytes=1024,
            adopted_existing=False,
        )

    anima = (
        artifact(ArtifactKind.DIFFUSION_MODEL, "miaomiao-anima-base"),
        artifact(ArtifactKind.TEXT_ENCODER, "qwen-3-06b-base"),
        artifact(ArtifactKind.VAE, "qwen-image-vae"),
    )
    other_split_model = (
        artifact(ArtifactKind.DIFFUSION_MODEL, "other-model"),
        artifact(ArtifactKind.TEXT_ENCODER, "other-text"),
        artifact(ArtifactKind.VAE, "other-vae"),
    )

    assert _manifest_requires_fp32_vae(anima)
    assert not _manifest_requires_fp32_vae(other_split_model)

    anima_command = build_comfy_command(
        _ComfyLaunchSettings.from_runtime_settings(
            _settings(),
            fp32_vae=_manifest_requires_fp32_vae(anima),
        )
    )
    other_command = build_comfy_command(
        _ComfyLaunchSettings.from_runtime_settings(
            _settings(),
            fp32_vae=_manifest_requires_fp32_vae(other_split_model),
        )
    )
    assert anima_command[-2:] == ("--fp16-unet", "--fp32-vae")
    assert "--fp16-unet" not in other_command
    assert "--fp32-vae" not in other_command


@pytest.mark.parametrize(
    "url",
    [
        "http://0.0.0.0:8188",
        "https://127.0.0.1:8188",
        "http://127.0.0.1:8188/path",
        "http://user@127.0.0.1:8188",
    ],
)
def test_build_comfy_command_rejects_non_loopback_origin(url: str) -> None:
    settings = _settings(comfy_base_url=url)

    with pytest.raises(WorkerBootstrapConfigurationError):
        build_comfy_command(settings)


def test_build_comfy_environment_is_an_explicit_secret_deny_boundary() -> None:
    environment = build_comfy_environment(
        {
            "PATH": "/usr/local/bin:/usr/bin",
            "NVIDIA_VISIBLE_DEVICES": "all",
            "CUDA_VISIBLE_DEVICES": "0",
            "GEN_WORKER_VERIFICATION_KEYS": json.dumps({"key-1": WORKER_VERIFICATION_PUBLIC_KEY}),
            "GEN_WORKER_SIGNING_PRIVATE_KEY": WORKER_SIGNING_PRIVATE_KEY,
            "GEN_WORKER_ARTIFACT_SECRET_ACCESS_KEY": "secret",
            "AWS_SECRET_ACCESS_KEY": "secret",
            "HF_TOKEN": "secret",
            "SALAD_API_KEY": "secret",
        }
    )

    assert environment["PATH"] == "/usr/local/bin:/usr/bin"
    assert environment["NVIDIA_VISIBLE_DEVICES"] == "all"
    assert environment["CUDA_VISIBLE_DEVICES"] == "0"
    assert environment["HF_HUB_OFFLINE"] == "1"
    assert environment["TRANSFORMERS_OFFLINE"] == "1"
    assert WORKER_SIGNING_PRIVATE_KEY not in environment.values()
    assert not any(
        "SECRET" in key or "TOKEN" in key or key.startswith("GEN_") for key in environment
    )


def test_queue_worker_environment_exposes_only_provider_log_level() -> None:
    settings = _settings(salad_queue_worker_log_level="warn")

    environment = build_queue_worker_environment(
        settings,
        {
            "PATH": "/usr/local/bin:/usr/bin",
            "LANG": "C.UTF-8",
            "SALAD_API_KEY": "controller-secret",
            "SALAD_QUEUE_NAME": "not-required",
            "AWS_SECRET_ACCESS_KEY": "storage-secret",
            "GEN_WORKER_VERIFICATION_KEYS": json.dumps({"key-1": WORKER_VERIFICATION_PUBLIC_KEY}),
            "GEN_WORKER_SIGNING_PRIVATE_KEY": WORKER_SIGNING_PRIVATE_KEY,
        },
    )

    assert environment == {
        "PATH": "/usr/local/bin:/usr/bin",
        "LANG": "C.UTF-8",
        "SALAD_LOG_LEVEL": "warn",
    }
    assert WORKER_SIGNING_PRIVATE_KEY not in environment.values()


def test_ensure_runtime_directories_creates_private_tree(tmp_path: Path) -> None:
    settings = _settings().model_copy(update={"comfy_runtime_root": tmp_path / "runtime"})

    ensure_runtime_directories(settings)

    assert {child.name for child in (tmp_path / "runtime").iterdir() if child.is_dir()} == {
        "input",
        "output",
        "temp",
        "user",
    }


def test_only_manifest_verified_detector_is_added_to_impact_whitelist(
    tmp_path: Path,
) -> None:
    settings = _settings().model_copy(update={"comfy_runtime_root": tmp_path / "runtime"})
    ensure_runtime_directories(settings)
    result = ArtifactBootstrapResult(
        version="v1",
        manifest_sha256="a" * 64,
        artifacts=(
            MaterializedArtifact(
                logical_name="face-yolov8m",
                kind=ArtifactKind.DETECTOR,
                target_filename="face-yolov8m.pt",
                sha256="b" * 64,
                size_bytes=100,
                adopted_existing=False,
            ),
        ),
    )

    write_verified_detector_whitelist(settings, result)

    whitelist = (
        settings.comfy_runtime_root
        / "user"
        / "default"
        / "ComfyUI-Impact-Subpack"
        / "model-whitelist.txt"
    )
    assert whitelist.read_text(encoding="utf-8") == "face-yolov8m.pt\n"


class _Body(io.BytesIO):
    pass


class _S3Client:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.closed = False
        self.get_object_parameters: dict[str, str] | None = None

    def get_object(self, **parameters: str) -> dict[str, object]:
        self.get_object_parameters = parameters
        return {
            "ContentLength": len(self.content),
            "VersionId": parameters.get("VersionId"),
            "Body": _Body(self.content),
        }

    def close(self) -> None:
        self.closed = True


async def test_bootstrap_worker_models_materializes_verified_checkpoint(
    tmp_path: Path,
) -> None:
    content = _safetensors()
    checkpoint_root = tmp_path / "models" / "checkpoints"
    lora_root = tmp_path / "models" / "loras"
    client = _S3Client(content)
    downloader = S3ArtifactDownloader(client=client, bucket="models-private")
    settings = _settings(content).model_copy(
        update={
            "checkpoint_root": checkpoint_root,
            "lora_root": lora_root,
            "detector_root": tmp_path / "models" / "ultralytics" / "bbox",
            "comfy_runtime_root": tmp_path / "runtime",
        }
    )

    result = await bootstrap_worker_models(settings, downloader=downloader)

    assert result.artifacts[0].sha256 == hashlib.sha256(content).hexdigest()
    assert (checkpoint_root / "illustrious.safetensors").read_bytes() == content
    assert client.get_object_parameters == {
        "Bucket": "models-private",
        "Key": "models/illustrious.safetensors",
        "VersionId": "model-version-1",
    }
    assert client.closed


class _HangingDownloader:
    closed = False

    async def stream(self, _artifact: ModelArtifactSpec) -> AsyncIterator[bytes]:
        await asyncio.sleep(60)
        yield b"unreachable"

    async def close(self) -> None:
        self.closed = True


async def test_bootstrap_timeout_closes_downloader_and_scrubs_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    downloader = _HangingDownloader()
    settings = _settings().model_copy(
        update={
            "checkpoint_root": tmp_path / "models" / "checkpoints",
            "lora_root": tmp_path / "models" / "loras",
            "detector_root": tmp_path / "models" / "ultralytics" / "bbox",
            "comfy_runtime_root": tmp_path / "runtime",
            "model_bootstrap_timeout_seconds": 0.01,
        }
    )
    monkeypatch.setenv("GEN_WORKER_ARTIFACT_SECRET_ACCESS_KEY", "private-storage-secret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "ambient-storage-secret")

    with pytest.raises(TimeoutError):
        await bootstrap_worker_models(
            settings,
            downloader=cast(S3ArtifactDownloader, cast(Any, downloader)),
        )

    assert downloader.closed
    assert "GEN_WORKER_ARTIFACT_SECRET_ACCESS_KEY" not in os.environ
    assert "AWS_SECRET_ACCESS_KEY" not in os.environ


class _HungProcess:
    pid = 999
    returncode: int | None = None

    def __init__(self) -> None:
        self.terminated = False
        self.killed = False
        self.wait_calls = 0

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls += 1
        if self.wait_calls == 1:
            raise subprocess.TimeoutExpired("comfy", timeout)
        return self.returncode or 0


def test_stop_comfy_escalates_after_grace_period() -> None:
    process = _HungProcess()

    stop_comfy(
        cast("subprocess.Popen[bytes]", cast(Any, process)),
        grace_seconds=0.01,
        process_group=False,
    )

    assert process.terminated
    assert process.killed
    assert process.wait_calls == 2


class _MonitorServer:
    def __init__(self, *, should_exit: bool = False) -> None:
        self.should_exit = should_exit


class _ExitedProcess:
    pid = 101

    def poll(self) -> int:
        return 0


async def test_monitor_marks_an_unexpected_child_exit(
    capsys: pytest.CaptureFixture[str],
) -> None:
    server = _MonitorServer()
    child_exit_event = asyncio.Event()

    await _monitor_comfy(
        _ManagedChildren(
            comfy=_ManagedChild(
                "comfy",
                cast("subprocess.Popen[bytes]", cast(Any, _ExitedProcess())),
                1.0,
            ),
            queue_worker=None,
        ),
        cast(Any, server),
        child_exit_event,
        monotonic=lambda: 3.5,
    )

    assert server.should_exit
    assert child_exit_event.is_set()
    stderr = capsys.readouterr().err
    assert "role=comfy pid=101 returncode=0" in stderr
    assert "signal=none signal_name=none uptime_seconds=2.500" in stderr
    assert "reason=child_exited" in stderr


async def test_monitor_does_not_mark_normal_server_shutdown() -> None:
    server = _MonitorServer(should_exit=True)
    child_exit_event = asyncio.Event()

    await _monitor_comfy(
        _ManagedChildren(
            comfy=_ManagedChild(
                "comfy",
                cast("subprocess.Popen[bytes]", cast(Any, _ExitedProcess())),
                1.0,
            ),
            queue_worker=None,
        ),
        cast(Any, server),
        child_exit_event,
    )

    assert not child_exit_event.is_set()


class _RunningProcess:
    pid = 102

    def poll(self) -> None:
        return None


async def test_monitor_gracefully_stops_server_after_worker_restart_request(
    capsys: pytest.CaptureFixture[str],
) -> None:
    server = _MonitorServer()
    worker_restart_event = asyncio.Event()
    worker_restart_event.set()

    await _monitor_comfy(
        _ManagedChildren(
            comfy=_ManagedChild(
                "comfy",
                cast("subprocess.Popen[bytes]", cast(Any, _RunningProcess())),
                1.0,
            ),
            queue_worker=None,
        ),
        cast(Any, server),
        worker_restart_event,
    )

    assert server.should_exit
    assert worker_restart_event.is_set()
    assert "reason=blank_or_fatal_output_requested" in capsys.readouterr().err


class _SupervisedProcess:
    def __init__(self, pid: int, returncode: int | None) -> None:
        self.pid = pid
        self.returncode = returncode

    def poll(self) -> int | None:
        return self.returncode


async def test_monitor_recycles_comfy_in_place_after_fatal_output(
    capsys: pytest.CaptureFixture[str],
) -> None:
    server = _MonitorServer()
    worker_restart_event = asyncio.Event()
    worker_recycle_event = asyncio.Event()
    worker_recycle_event.set()
    children = _managed_children(queue_process=_SupervisedProcess(301, None))
    replacement_comfy = _SupervisedProcess(302, None)
    replacement_queue = _SupervisedProcess(303, None)
    events: list[str] = []

    def stop_child(process: subprocess.Popen[bytes]) -> None:
        events.append(f"stop:{cast(Any, process).pid}")

    def start_comfy_process() -> subprocess.Popen[bytes]:
        events.append("start:comfy")
        return cast("subprocess.Popen[bytes]", cast(Any, replacement_comfy))

    async def wait_for_ready(
        process: subprocess.Popen[bytes],
        _health_check: Any,
    ) -> None:
        assert cast(Any, process) is replacement_comfy
        events.append("ready:comfy")

    def start_queue_worker() -> subprocess.Popen[bytes]:
        assert not worker_recycle_event.is_set()
        events.append("start:queue")
        return cast("subprocess.Popen[bytes]", cast(Any, replacement_queue))

    async def finish_monitor(_event: asyncio.Event, _delay_seconds: float) -> bool:
        server.should_exit = True
        return False

    await _monitor_comfy(
        children,
        cast(Any, server),
        worker_restart_event,
        worker_recycle_event=worker_recycle_event,
        start_comfy_process=start_comfy_process,
        start_queue_worker=start_queue_worker,
        unsafe_request_active=lambda: False,
        comfy_health_check=lambda: True,
        monotonic=lambda: 10.0,
        wait_for_event=finish_monitor,
        stop_child=stop_child,
        wait_for_comfy_ready=wait_for_ready,
    )

    assert events == [
        "stop:301",
        "stop:200",
        "start:comfy",
        "ready:comfy",
        "start:queue",
    ]
    assert children.comfy.process is cast(Any, replacement_comfy)
    assert children.queue_worker is not None
    assert children.queue_worker.process is cast(Any, replacement_queue)
    assert not worker_recycle_event.is_set()
    assert not worker_restart_event.is_set()
    assert "recycle_ordinal=1" in capsys.readouterr().err


async def test_monitor_escalates_repeated_fatal_output_after_one_in_place_recycle(
    capsys: pytest.CaptureFixture[str],
) -> None:
    server = _MonitorServer()
    worker_restart_event = asyncio.Event()
    worker_recycle_event = asyncio.Event()
    worker_recycle_event.set()
    children = _managed_children(queue_process=_SupervisedProcess(311, None))
    replacement_comfy = _SupervisedProcess(312, None)
    replacement_queue = _SupervisedProcess(313, None)
    stopped: list[int] = []

    async def wait_for_ready(
        _process: subprocess.Popen[bytes],
        _health_check: Any,
    ) -> None:
        return None

    async def request_second_recycle(
        _event: asyncio.Event,
        _delay_seconds: float,
    ) -> bool:
        worker_recycle_event.set()
        return False

    await _monitor_comfy(
        children,
        cast(Any, server),
        worker_restart_event,
        worker_recycle_event=worker_recycle_event,
        start_comfy_process=lambda: cast("subprocess.Popen[bytes]", cast(Any, replacement_comfy)),
        start_queue_worker=lambda: cast("subprocess.Popen[bytes]", cast(Any, replacement_queue)),
        unsafe_request_active=lambda: False,
        comfy_health_check=lambda: True,
        monotonic=lambda: 10.0,
        wait_for_event=request_second_recycle,
        stop_child=lambda process: stopped.append(cast(Any, process).pid),
        wait_for_comfy_ready=wait_for_ready,
    )

    assert stopped == [311, 200]
    assert server.should_exit
    assert worker_restart_event.is_set()
    assert worker_recycle_event.is_set()
    assert "reason=fatal_output_recycle_budget_exhausted" in capsys.readouterr().err


async def test_monitor_fails_closed_when_fatal_output_request_does_not_drain(
    capsys: pytest.CaptureFixture[str],
) -> None:
    server = _MonitorServer()
    worker_restart_event = asyncio.Event()
    worker_recycle_event = asyncio.Event()
    worker_recycle_event.set()
    starts: list[str] = []

    await _monitor_comfy(
        _managed_children(queue_process=_SupervisedProcess(321, None)),
        cast(Any, server),
        worker_restart_event,
        worker_recycle_event=worker_recycle_event,
        start_comfy_process=lambda: starts.append("comfy") or cast(Any, None),
        start_queue_worker=lambda: starts.append("queue") or cast(Any, None),
        unsafe_request_active=lambda: True,
        comfy_health_check=lambda: True,
        monotonic=lambda: 10.0,
        unsafe_request_drain_seconds=0.0,
    )

    assert starts == []
    assert server.should_exit
    assert worker_restart_event.is_set()
    assert "reason=fatal_output_request_did_not_drain" in capsys.readouterr().err


async def test_monitor_recycles_comfy_child_exit_without_rehydrating_models(
    capsys: pytest.CaptureFixture[str],
) -> None:
    server = _MonitorServer()
    worker_restart_event = asyncio.Event()
    worker_recycle_event = asyncio.Event()
    children = _ManagedChildren(
        comfy=_ManagedChild(
            "comfy",
            cast(
                "subprocess.Popen[bytes]",
                cast(Any, _SupervisedProcess(331, 1)),
            ),
            0.0,
        ),
        queue_worker=_ManagedChild(
            "salad_queue_worker",
            cast(
                "subprocess.Popen[bytes]",
                cast(Any, _SupervisedProcess(332, None)),
            ),
            0.0,
        ),
    )
    replacement_comfy = _SupervisedProcess(333, None)
    replacement_queue = _SupervisedProcess(334, None)
    stopped: list[int] = []

    async def wait_for_ready(
        _process: subprocess.Popen[bytes],
        _health_check: Any,
    ) -> None:
        return None

    async def finish_monitor(_event: asyncio.Event, _delay_seconds: float) -> bool:
        server.should_exit = True
        return False

    await _monitor_comfy(
        children,
        cast(Any, server),
        worker_restart_event,
        worker_recycle_event=worker_recycle_event,
        start_comfy_process=lambda: cast("subprocess.Popen[bytes]", cast(Any, replacement_comfy)),
        start_queue_worker=lambda: cast("subprocess.Popen[bytes]", cast(Any, replacement_queue)),
        unsafe_request_active=lambda: False,
        comfy_health_check=lambda: True,
        monotonic=lambda: 10.0,
        wait_for_event=finish_monitor,
        stop_child=lambda process: stopped.append(cast(Any, process).pid),
        wait_for_comfy_ready=wait_for_ready,
    )

    assert stopped == [332]
    assert children.comfy.process is cast(Any, replacement_comfy)
    assert children.queue_worker is not None
    assert children.queue_worker.process is cast(Any, replacement_queue)
    assert not worker_recycle_event.is_set()
    assert not worker_restart_event.is_set()
    stderr = capsys.readouterr().err
    assert "role=comfy pid=331 returncode=1" in stderr
    assert "reason=comfy_child_exit" in stderr


async def test_monitor_escalates_when_comfy_recycle_never_becomes_ready(
    capsys: pytest.CaptureFixture[str],
) -> None:
    server = _MonitorServer()
    worker_restart_event = asyncio.Event()
    worker_recycle_event = asyncio.Event()
    worker_recycle_event.set()
    replacement_comfy = _SupervisedProcess(343, None)
    stopped: list[int] = []

    async def fail_readiness(
        _process: subprocess.Popen[bytes],
        _health_check: Any,
    ) -> None:
        raise WorkerBootstrapConfigurationError("not ready")

    await _monitor_comfy(
        _managed_children(queue_process=_SupervisedProcess(341, None)),
        cast(Any, server),
        worker_restart_event,
        worker_recycle_event=worker_recycle_event,
        start_comfy_process=lambda: cast("subprocess.Popen[bytes]", cast(Any, replacement_comfy)),
        start_queue_worker=lambda: cast(Any, _SupervisedProcess(344, None)),
        unsafe_request_active=lambda: False,
        comfy_health_check=lambda: False,
        monotonic=lambda: 10.0,
        stop_child=lambda process: stopped.append(cast(Any, process).pid),
        wait_for_comfy_ready=fail_readiness,
    )

    assert stopped == [341, 200, 343]
    assert server.should_exit
    assert worker_restart_event.is_set()
    assert worker_recycle_event.is_set()
    assert "reason=fatal_output_recycle_readiness_failed" in capsys.readouterr().err


def _managed_children(
    *,
    queue_process: _SupervisedProcess,
) -> _ManagedChildren:
    return _ManagedChildren(
        comfy=_ManagedChild(
            "comfy",
            cast(
                "subprocess.Popen[bytes]",
                cast(Any, _SupervisedProcess(200, None)),
            ),
            0.0,
        ),
        queue_worker=_ManagedChild(
            "salad_queue_worker",
            cast("subprocess.Popen[bytes]", cast(Any, queue_process)),
            0.0,
        ),
    )


async def test_monitor_restarts_idle_queue_worker_with_exact_diagnostics(
    capsys: pytest.CaptureFixture[str],
) -> None:
    server = _MonitorServer()
    worker_restart_event = asyncio.Event()
    children = _managed_children(queue_process=_SupervisedProcess(201, -9))
    replacement = _SupervisedProcess(202, None)
    waits: list[float] = []

    async def wait_for_event(_event: asyncio.Event, delay_seconds: float) -> bool:
        waits.append(delay_seconds)
        if len(waits) == 2:
            server.should_exit = True
        return False

    await _monitor_comfy(
        children,
        cast(Any, server),
        worker_restart_event,
        start_queue_worker=lambda: cast(
            "subprocess.Popen[bytes]",
            cast(Any, replacement),
        ),
        unsafe_request_active=lambda: False,
        comfy_health_check=lambda: True,
        monotonic=lambda: 10.0,
        wait_for_event=wait_for_event,
    )

    assert children.queue_worker is not None
    assert cast(Any, children.queue_worker.process) is replacement
    assert not worker_restart_event.is_set()
    assert waits == [1.0, 1.0]
    stderr = capsys.readouterr().err
    assert "role=salad_queue_worker pid=201 returncode=-9 signal=9" in stderr
    assert "uptime_seconds=10.000" in stderr
    assert "previous_pid=201 previous_returncode=-9" in stderr
    assert "new_pid=202 restart_ordinal=1 backoff_seconds=1.000" in stderr


async def test_monitor_fails_closed_when_queue_dies_during_generation(
    capsys: pytest.CaptureFixture[str],
) -> None:
    server = _MonitorServer()
    worker_restart_event = asyncio.Event()
    starts: list[bool] = []

    def record_start() -> None:
        starts.append(True)

    await _monitor_comfy(
        _managed_children(queue_process=_SupervisedProcess(211, 1)),
        cast(Any, server),
        worker_restart_event,
        start_queue_worker=record_start,
        unsafe_request_active=lambda: True,
        comfy_health_check=lambda: True,
        monotonic=lambda: 2.0,
        unsafe_request_drain_seconds=0.0,
    )

    assert not starts
    assert server.should_exit
    assert worker_restart_event.is_set()
    assert "reason=unsafe_active_request" in capsys.readouterr().err


async def test_monitor_lets_response_boundary_drain_before_restarting_queue() -> None:
    server = _MonitorServer()
    worker_restart_event = asyncio.Event()
    children = _managed_children(queue_process=_SupervisedProcess(212, 1))
    replacement = _SupervisedProcess(213, None)
    request_active = True
    starts: list[bool] = []
    waits = 0

    async def clear_response_boundary() -> None:
        nonlocal request_active
        await asyncio.sleep(0)
        request_active = False

    async def wait_for_event(_event: asyncio.Event, _delay_seconds: float) -> bool:
        nonlocal waits
        waits += 1
        if waits == 2:
            server.should_exit = True
        return False

    drain_task = asyncio.create_task(clear_response_boundary())
    await _monitor_comfy(
        children,
        cast(Any, server),
        worker_restart_event,
        start_queue_worker=lambda: (
            starts.append(True) or cast("subprocess.Popen[bytes]", cast(Any, replacement))
        ),
        unsafe_request_active=lambda: request_active,
        comfy_health_check=lambda: True,
        monotonic=lambda: 2.0,
        wait_for_event=wait_for_event,
        unsafe_request_drain_poll_seconds=0.0,
    )
    await drain_task

    assert starts == [True]
    assert children.queue_worker is not None
    assert cast(Any, children.queue_worker.process) is replacement
    assert not worker_restart_event.is_set()


async def test_monitor_blank_output_signal_wins_after_active_request_drains(
    capsys: pytest.CaptureFixture[str],
) -> None:
    server = _MonitorServer()
    worker_restart_event = asyncio.Event()
    worker_restart_event.set()
    request_active = True
    starts: list[bool] = []

    async def clear_response_boundary() -> None:
        nonlocal request_active
        await asyncio.sleep(0)
        request_active = False

    drain_task = asyncio.create_task(clear_response_boundary())
    await _monitor_comfy(
        _managed_children(queue_process=_SupervisedProcess(214, 1)),
        cast(Any, server),
        worker_restart_event,
        start_queue_worker=lambda: starts.append(True),
        unsafe_request_active=lambda: request_active,
        comfy_health_check=lambda: True,
        monotonic=lambda: 2.0,
        unsafe_request_drain_poll_seconds=0.0,
    )
    await drain_task

    assert starts == []
    assert server.should_exit
    assert worker_restart_event.is_set()
    assert "reason=blank_or_fatal_output_requested" in capsys.readouterr().err


async def test_monitor_does_not_restart_queue_when_comfy_is_unhealthy(
    capsys: pytest.CaptureFixture[str],
) -> None:
    server = _MonitorServer()
    worker_restart_event = asyncio.Event()
    starts: list[bool] = []

    def record_start() -> None:
        starts.append(True)

    async def finish_backoff(_event: asyncio.Event, _delay_seconds: float) -> bool:
        return False

    await _monitor_comfy(
        _managed_children(queue_process=_SupervisedProcess(216, 1)),
        cast(Any, server),
        worker_restart_event,
        start_queue_worker=record_start,
        unsafe_request_active=lambda: False,
        comfy_health_check=lambda: False,
        monotonic=lambda: 2.0,
        wait_for_event=finish_backoff,
    )

    assert not starts
    assert server.should_exit
    assert worker_restart_event.is_set()
    assert "role=comfy reason=health_check_failed" in capsys.readouterr().err


async def test_monitor_never_restarts_queue_after_blank_output_request() -> None:
    server = _MonitorServer()
    worker_restart_event = asyncio.Event()
    starts: list[bool] = []

    def record_start() -> None:
        starts.append(True)

    async def request_restart(_event: asyncio.Event, _timeout: float) -> bool:
        worker_restart_event.set()
        return True

    await _monitor_comfy(
        _managed_children(queue_process=_SupervisedProcess(221, 1)),
        cast(Any, server),
        worker_restart_event,
        start_queue_worker=record_start,
        unsafe_request_active=lambda: False,
        comfy_health_check=lambda: True,
        monotonic=lambda: 2.0,
        wait_for_event=request_restart,
    )

    assert not starts
    assert server.should_exit
    assert worker_restart_event.is_set()


async def test_monitor_exits_after_consecutive_queue_restart_budget() -> None:
    server = _MonitorServer()
    worker_restart_event = asyncio.Event()
    children = _managed_children(queue_process=_SupervisedProcess(231, 1))
    replacements = iter(_SupervisedProcess(pid, 1) for pid in range(232, 235))
    waits: list[float] = []

    async def wait_for_event(_event: asyncio.Event, delay_seconds: float) -> bool:
        waits.append(delay_seconds)
        return False

    await _monitor_comfy(
        children,
        cast(Any, server),
        worker_restart_event,
        start_queue_worker=lambda: cast(
            "subprocess.Popen[bytes]",
            cast(Any, next(replacements)),
        ),
        unsafe_request_active=lambda: False,
        comfy_health_check=lambda: True,
        monotonic=lambda: 10.0,
        wait_for_event=wait_for_event,
    )

    assert waits == [1.0, 2.0, 4.0]
    assert server.should_exit
    assert worker_restart_event.is_set()


async def test_monitor_bounds_queue_spawn_oserror_retries(
    capsys: pytest.CaptureFixture[str],
) -> None:
    server = _MonitorServer()
    worker_restart_event = asyncio.Event()
    attempts = 0
    waits: list[float] = []

    def fail_start() -> None:
        nonlocal attempts
        attempts += 1
        raise OSError("spawn failed")

    async def wait_for_event(_event: asyncio.Event, delay_seconds: float) -> bool:
        waits.append(delay_seconds)
        return False

    await _monitor_comfy(
        _managed_children(queue_process=_SupervisedProcess(241, 1)),
        cast(Any, server),
        worker_restart_event,
        start_queue_worker=fail_start,
        unsafe_request_active=lambda: False,
        comfy_health_check=lambda: True,
        monotonic=lambda: 10.0,
        wait_for_event=wait_for_event,
    )

    assert attempts == 3
    assert waits == [1.0, 2.0, 4.0]
    assert server.should_exit
    assert worker_restart_event.is_set()
    assert "reason=restart_budget_exhausted" in capsys.readouterr().err


async def test_monitor_fails_closed_on_internal_exception(
    capsys: pytest.CaptureFixture[str],
) -> None:
    server = _MonitorServer()
    worker_restart_event = asyncio.Event()

    def fail_clock() -> float:
        raise ValueError("unexpected monitor failure")

    await _monitor_comfy(
        _managed_children(queue_process=_SupervisedProcess(251, None)),
        cast(Any, server),
        worker_restart_event,
        monotonic=fail_clock,
    )

    assert server.should_exit
    assert worker_restart_event.is_set()
    stderr = capsys.readouterr().err
    assert "exception=ValueError" in stderr
    assert "reason=monitor_exception" in stderr
