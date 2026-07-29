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
    _monitor_comfy,
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
        "artifact_bucket": "models-private",
        "checkpoint_root": Path("/opt/comfyui/models/checkpoints"),
        "lora_root": Path("/opt/comfyui/models/loras"),
        "comfy_python": Path("/opt/worker-venv/bin/python"),
        "comfy_main": Path("/opt/comfyui/main.py"),
    }
    values.update(changes)
    return WorkerRuntimeSettings.model_validate(values)


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
    def poll(self) -> int:
        return 0


async def test_monitor_marks_an_unexpected_child_exit() -> None:
    server = _MonitorServer()
    child_exit_event = asyncio.Event()

    await _monitor_comfy(
        (cast("subprocess.Popen[bytes]", cast(Any, _ExitedProcess())),),
        cast(Any, server),
        child_exit_event,
    )

    assert server.should_exit
    assert child_exit_event.is_set()


async def test_monitor_does_not_mark_normal_server_shutdown() -> None:
    server = _MonitorServer(should_exit=True)
    child_exit_event = asyncio.Event()

    await _monitor_comfy(
        (cast("subprocess.Popen[bytes]", cast(Any, _ExitedProcess())),),
        cast(Any, server),
        child_exit_event,
    )

    assert not child_exit_event.is_set()
