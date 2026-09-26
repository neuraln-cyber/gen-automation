from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import SecretStr

from gen_automation.i2v_worker.face_stabilizer import FaceStabilizationError
from gen_automation.i2v_worker.settings import I2VWorkerSettings
from gen_automation.i2v_worker.supervisor import WorkerSupervisor


def _settings(tmp_path: Path) -> I2VWorkerSettings:
    sha = "a" * 64
    objects = [
        {
            "role": role,
            "bucket": "models",
            "key": f"worker/i2v/sha256/{sha}",
            "version_id": "v1",
            "byte_size": 1,
            "sha256": sha,
            "install_path": install_path,
        }
        for role, install_path in (
            ("diffusion_model_high", "models/diffusion_models/high.safetensors"),
            ("diffusion_model_low", "models/diffusion_models/low.safetensors"),
            ("text_encoder", "models/text_encoders/text.safetensors"),
            ("vae", "models/vae/Wan/vae.safetensors"),
        )
    ]
    return I2VWorkerSettings(
        model_objects_json=SecretStr(json.dumps(objects)),
        environment="test",
        comfy_root=tmp_path / "comfy",
        runtime_root=tmp_path / "runtime",
        queue_worker_enabled=False,
    )


class _Bootstrapper:
    calls = 0

    def __init__(self, _settings: I2VWorkerSettings) -> None:
        pass

    async def bootstrap(self) -> None:
        type(self).calls += 1


class _ComfyClient:
    def __init__(self, **_kwargs: Any) -> None:
        self.closed = False

    async def ready(self) -> bool:
        return True

    async def close(self) -> None:
        self.closed = True


class _Process:
    pid = 42

    def poll(self) -> None:
        return None


async def _wait_for(predicate: Any) -> None:
    for _ in range(200):
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("supervisor transition timed out")


@pytest.mark.asyncio
async def test_supervisor_loads_one_cpu_face_detector_before_bootstrap(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    import gen_automation.i2v_worker.supervisor as module

    detector = object()
    events: list[str] = []

    def load_detector(*, device: str) -> object:
        events.append(f"face:{device}")
        return detector

    class OrderedBootstrapper(_Bootstrapper):
        async def bootstrap(self) -> None:
            events.append("models")
            await super().bootstrap()

    OrderedBootstrapper.calls = 0

    monkeypatch.setattr(module, "_verify_gpu_runtime", lambda _settings: events.append("gpu"))
    monkeypatch.setattr(module, "preflight_face_stabilizer", load_detector)
    monkeypatch.setattr(module, "S3ModelBootstrapper", OrderedBootstrapper)
    monkeypatch.setattr(module, "ComfyClient", _ComfyClient)
    monkeypatch.setattr(module, "_start_process", lambda *_args, **_kwargs: _Process())
    monkeypatch.setattr(module, "_stop_process", lambda _process: None)

    supervisor = WorkerSupervisor(_settings(tmp_path))
    await supervisor.start()
    await _wait_for(lambda: supervisor.ready)
    try:
        assert supervisor.face_detector is detector
        assert events[:3] == ["gpu", "face:cpu", "models"]
        assert events.count("face:cpu") == 1
        assert OrderedBootstrapper.calls == 1
    finally:
        await supervisor.stop()
    assert supervisor.face_detector is None
    assert supervisor.stage == "ready"


@pytest.mark.asyncio
async def test_face_detector_load_failure_fails_before_model_or_comfy_start(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    import gen_automation.i2v_worker.supervisor as module

    calls = {"bootstrap": 0, "start": 0}
    log_calls: list[tuple[str, tuple[object, ...]]] = []

    def record_error(message: str, *args: object) -> None:
        log_calls.append((message, args))

    monkeypatch.setattr(module._LOGGER, "error", record_error)

    class UnexpectedBootstrapper(_Bootstrapper):
        async def bootstrap(self) -> None:
            calls["bootstrap"] += 1

    def fail_detector(*, device: str) -> object:
        assert device == "cpu"
        raise FaceStabilizationError("sensitive detector path")

    def start_process(*_args: Any, **_kwargs: Any) -> _Process:
        calls["start"] += 1
        return _Process()

    monkeypatch.setattr(module, "_verify_gpu_runtime", lambda _settings: None)
    monkeypatch.setattr(module, "preflight_face_stabilizer", fail_detector)
    monkeypatch.setattr(module, "S3ModelBootstrapper", UnexpectedBootstrapper)
    monkeypatch.setattr(module, "_start_process", start_process)

    supervisor = WorkerSupervisor(_settings(tmp_path))
    await supervisor.start()
    await _wait_for(lambda: supervisor.failed)
    await supervisor.stop()

    assert supervisor.ready is False
    assert supervisor.face_detector is None
    assert calls == {"bootstrap": 0, "start": 0}
    assert log_calls == [
        ("face stabilizer startup failed: reason_code=%s", ("internal",)),
        (
            "i2v_startup_failed stage=%s error_type=%s",
            ("face_preflight", "WorkerStartupError"),
        ),
    ]
    assert "sensitive detector path" not in repr(log_calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["gpu_preflight", "model_download", "comfy_start"])
async def test_unexpected_startup_exception_fails_health_instead_of_hanging(
    tmp_path: Path, monkeypatch: Any, caplog: Any, stage: str
) -> None:
    import gen_automation.i2v_worker.supervisor as module

    def fail(*_args: Any, **_kwargs: Any) -> None:
        raise ValueError("https://private.invalid/?token=do-not-log")

    class FailingBootstrapper(_Bootstrapper):
        async def bootstrap(self) -> None:
            if stage == "model_download":
                fail()

    monkeypatch.setattr(
        module, "_verify_gpu_runtime", fail if stage == "gpu_preflight" else lambda _: None
    )
    monkeypatch.setattr(module, "preflight_face_stabilizer", lambda **_: object())
    monkeypatch.setattr(module, "S3ModelBootstrapper", FailingBootstrapper)
    monkeypatch.setattr(module, "_start_process", fail)
    supervisor = WorkerSupervisor(_settings(tmp_path))
    await supervisor.start()
    await _wait_for(lambda: supervisor.failed)
    await supervisor.stop()

    assert supervisor.ready is False
    assert supervisor.face_detector is None
    assert supervisor.stage == stage
    assert supervisor._task is not None and supervisor._task.exception() is None
    assert f"i2v_startup_failed stage={stage} error_type=ValueError" in caplog.text
    assert "private.invalid" not in caplog.text
    assert "do-not-log" not in caplog.text


@pytest.mark.asyncio
async def test_normal_cancellation_is_not_reported_as_startup_failure(
    tmp_path: Path, monkeypatch: Any, caplog: Any
) -> None:
    import gen_automation.i2v_worker.supervisor as module

    class WaitingBootstrapper(_Bootstrapper):
        async def bootstrap(self) -> None:
            await asyncio.Event().wait()

    monkeypatch.setattr(module, "_verify_gpu_runtime", lambda _: None)
    monkeypatch.setattr(module, "preflight_face_stabilizer", lambda **_: object())
    monkeypatch.setattr(module, "S3ModelBootstrapper", WaitingBootstrapper)
    supervisor = WorkerSupervisor(_settings(tmp_path))
    await supervisor.start()
    await _wait_for(lambda: supervisor.stage == "model_download")
    await supervisor.stop()

    assert supervisor.failed is False
    assert supervisor.ready is False
    assert "i2v_startup_failed" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "missing", "exited"])
async def test_salad_consumer_starts_after_comfy_and_failure_closes_readiness(
    tmp_path: Path, monkeypatch: Any, failure: str | None
) -> None:
    import gen_automation.i2v_worker.supervisor as module

    events: list[str] = []
    stopped: list[object] = []

    class QueueProcess(_Process):
        exit_code: int | None = None

        def poll(self) -> Any:
            return self.exit_code

    queue_process = QueueProcess()
    comfy_process = _Process()

    class Client(_ComfyClient):
        async def ready(self) -> bool:
            events.append("comfy_ready")
            return True

    def start(command: tuple[str, ...], **kwargs: Any) -> Any:
        if command[0].endswith("salad-http-job-queue-worker"):
            assert events == ["comfy_start", "comfy_ready"]
            events.append("queue_start")
            assert kwargs["environment"]["SALAD_LOG_LEVEL"] == "info"
            if failure == "missing":
                raise FileNotFoundError
            return queue_process
        events.append("comfy_start")
        return comfy_process

    monkeypatch.setattr(module, "_verify_gpu_runtime", lambda _: None)
    monkeypatch.setattr(module, "preflight_face_stabilizer", lambda **_: object())
    monkeypatch.setattr(module, "ComfyClient", Client)
    monkeypatch.setattr(module, "_start_process", start)
    monkeypatch.setattr(module, "_stop_process", stopped.append)
    settings = _settings(tmp_path).model_copy(
        update={"provider": "salad", "queue_worker_enabled": True, "models_prepared": True}
    )
    supervisor = WorkerSupervisor(settings)
    assert not supervisor.queue_ready
    await supervisor.start()
    try:
        await _wait_for(lambda: supervisor.ready or supervisor.failed)
        if failure == "exited":
            assert supervisor.queue_ready
            queue_process.exit_code = 1
            assert not supervisor.queue_ready
            await _wait_for(lambda: supervisor.failed)
        if failure:
            assert not supervisor.ready and supervisor.failed
            await _wait_for(lambda: comfy_process in stopped)
            assert comfy_process in stopped
        else:
            assert supervisor.ready and supervisor.queue_ready
            assert events == ["comfy_start", "comfy_ready", "queue_start"]
    finally:
        await supervisor.stop()
    if failure != "missing":
        assert queue_process in stopped


def test_queue_consumer_environment_excludes_credentials(monkeypatch: Any) -> None:
    from gen_automation.i2v_worker.supervisor import _queue_environment

    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "do-not-forward")
    monkeypatch.setenv("GEN_I2V_WORKER_MODEL_OBJECTS_JSON", "private")
    monkeypatch.setenv("HTTPS_PROXY", "do-not-forward")
    monkeypatch.setenv("SALAD_LOG_LEVEL", "debug")
    environment = _queue_environment()
    assert set(environment) <= {
        "HOME",
        "LANG",
        "LC_ALL",
        "PATH",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "SALAD_LOG_LEVEL",
    }
    assert environment["SALAD_LOG_LEVEL"] == "info"


def test_salad_configuration_cannot_disable_consumer(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Salad requires the queue consumer"):
        I2VWorkerSettings.model_validate({**_settings(tmp_path).model_dump(), "provider": "salad"})


def test_video_image_reuses_the_hardened_image_queue_consumer() -> None:
    root = Path(__file__).resolve().parents[1]
    image = (root / "Dockerfile.worker").read_text(encoding="utf-8")
    video = (root / "Dockerfile.i2v-worker").read_text(encoding="utf-8")
    assert video.split("FROM pytorch/", 1)[0] == image.split("FROM pytorch/", 1)[0]
    assert "COPY --from=salad-queue-worker-builder --chmod=0555" in video
    assert "RUN test -x /usr/local/bin/salad-http-job-queue-worker" in video


def test_h3_memory_policy_is_isolated_and_does_not_reduce_generation_settings(
    tmp_path, monkeypatch
):
    from gen_automation.i2v_worker.supervisor import _comfy_command, _comfy_environment

    monkeypatch.setenv("PYTORCH_ALLOC_CONF", "backend:cudaMallocAsync")
    monkeypatch.setenv("PYTORCH_CUDA_ALLOC_CONF", "backend:cudaMallocAsync")
    legacy = _settings(tmp_path)
    h3 = legacy.model_copy(update={"profile": "minimax_h3"})
    command = _comfy_command(h3)
    assert command[command.index("--reserve-vram") + 1] == "8"
    assert command[command.index("--vram-headroom") + 1] == "4"
    assert "--disable-cuda-malloc" in command
    assert "--cache-none" in command
    assert "--lowvram" not in command  # Do not move text encoding to CPU.
    environment = _comfy_environment(h3)
    assert environment["PYTORCH_ALLOC_CONF"] == "backend:native,expandable_segments:True"
    assert environment["PYTORCH_CUDA_ALLOC_CONF"] == environment["PYTORCH_ALLOC_CONF"]
    assert "--disable-cuda-malloc" not in _comfy_command(legacy)
    assert _comfy_environment(legacy)["PYTORCH_ALLOC_CONF"] == "backend:cudaMallocAsync"
    assert h3.execution_timeout_seconds is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True])
async def test_recovery_replaces_only_comfy_without_downloads_or_queue_restart(
    tmp_path, monkeypatch, caplog, failure
):
    import gen_automation.i2v_worker.supervisor as module

    old, new, queue = _Process(), _Process(), _Process()
    stopped = []
    starts = []
    supervisor = WorkerSupervisor(
        _settings(tmp_path).model_copy(
            update={
                "queue_worker_enabled": True,
                "provider": "salad",
            }
        )
    )
    supervisor.comfy, supervisor.queue_worker = old, queue
    supervisor.comfy_client = _ComfyClient()
    supervisor.ready = True

    def start(command, **kwargs):
        assert supervisor.ready is False
        assert supervisor._recovering is True
        starts.append(command)
        if failure:
            raise RuntimeError("private credential must not appear")
        return new

    async def forbidden_bootstrap():
        raise AssertionError("must not download/rebootstrap")

    monkeypatch.setattr(supervisor, "_bootstrap", forbidden_bootstrap)
    monkeypatch.setattr(module, "_start_process", start)
    monkeypatch.setattr(module, "_stop_process", stopped.append)
    assert await supervisor.recover_comfy() is (not failure)
    assert len(starts) == 1
    assert queue not in stopped and supervisor.queue_worker is queue
    assert old in stopped
    assert supervisor.ready is (not failure)
    assert supervisor.failed is failure
    assert supervisor._recovering is False
    assert "private credential" not in caplog.text


@pytest.mark.asyncio
async def test_supervision_does_not_kill_consumer_while_comfy_is_replaced(tmp_path, monkeypatch):
    import gen_automation.i2v_worker.supervisor as module

    supervisor = WorkerSupervisor(
        _settings(tmp_path).model_copy(
            update={
                "queue_worker_enabled": True,
            }
        )
    )
    supervisor.queue_worker = _Process()
    supervisor._recovering = True
    stopped = []

    async def bootstrap():
        pass

    monkeypatch.setattr(supervisor, "_bootstrap", bootstrap)
    monkeypatch.setattr(module, "_stop_process", stopped.append)
    await supervisor.start()
    await _wait_for(lambda: supervisor.stage == "ready")
    await asyncio.sleep(0.02)
    assert not supervisor.failed and not stopped
    await supervisor.stop()


def _fake_torch(**overrides: Any) -> SimpleNamespace:
    values = {
        "version": "2.9.1+cu128",
        "available": True,
        "count": 1,
        "name": "NVIDIA GeForce RTX 5090",
        "memory": 32 * 1024**3,
    }
    values.update(overrides)
    return SimpleNamespace(
        __version__=values["version"],
        version=SimpleNamespace(cuda="12.8"),
        cuda=SimpleNamespace(
            is_available=lambda: values["available"],
            device_count=lambda: values["count"],
            get_device_name=lambda _: values["name"],
            get_device_properties=lambda _: SimpleNamespace(total_memory=values["memory"]),
        ),
    )


@pytest.mark.parametrize(
    ("overrides", "reason", "check"),
    [
        ({"version": "2.10.0"}, "torch_version_mismatch", "torch_version"),
        ({"available": False}, "cuda_unavailable", "cuda_available"),
        ({"count": 0}, "device_count_mismatch", "device_count"),
        ({"count": 2}, "device_count_mismatch", "device_count"),
        ({"name": "NVIDIA GeForce RTX 5090 D"}, "gpu_name_not_allowed", "gpu_name"),
        ({"memory": 31 * 1024**3 - 1}, "insufficient_vram", "gpu_vram"),
    ],
)
def test_gpu_preflight_reports_exact_failed_gate_without_relaxing_it(
    tmp_path, monkeypatch, caplog, overrides, reason, check
):
    from gen_automation.i2v_worker.supervisor import WorkerStartupError, _verify_gpu_runtime

    monkeypatch.setitem(sys.modules, "torch", _fake_torch(**overrides))
    with pytest.raises(WorkerStartupError, match="supported GPU runtime is unavailable"):
        _verify_gpu_runtime(_settings(tmp_path))
    assert f"status=failed reason_code={reason} check={check}" in caplog.text
    assert "status=passed" not in caplog.text


@pytest.mark.parametrize("memory", [31 * 1024**3, 32 * 1024**3])
def test_gpu_preflight_passes_existing_supported_runtime_and_logs_hardware_facts(
    tmp_path, monkeypatch, caplog, memory
):
    from gen_automation.i2v_worker.supervisor import _verify_gpu_runtime

    monkeypatch.setitem(sys.modules, "torch", _fake_torch(memory=memory))
    caplog.set_level(logging.INFO, logger="gen_automation.i2v_worker.supervisor")
    _verify_gpu_runtime(_settings(tmp_path))
    assert "status=passed torch_version=2.9.1 cuda_version=12.8 device_count=1" in caplog.text
    assert "gpu_name=NVIDIA_GeForce_RTX_5090" in caplog.text
    assert f"vram_bytes={memory}" in caplog.text
    assert "status=failed" not in caplog.text


@pytest.mark.parametrize(
    ("method", "check"),
    [
        ("is_available", "cuda_available"),
        ("device_count", "device_count"),
        ("get_device_name", "gpu_name"),
        ("get_device_properties", "gpu_vram"),
    ],
)
def test_gpu_query_errors_log_only_safe_check_and_exception_type(
    tmp_path, monkeypatch, caplog, method, check
):
    from gen_automation.i2v_worker.supervisor import WorkerStartupError, _verify_gpu_runtime

    def fail(*_args):
        raise RuntimeError("https://private.invalid/model?token=do-not-log")

    torch = _fake_torch()
    setattr(torch.cuda, method, fail)
    monkeypatch.setitem(sys.modules, "torch", torch)
    with pytest.raises(WorkerStartupError):
        _verify_gpu_runtime(_settings(tmp_path))
    assert f"reason_code=runtime_query_failed check={check} error_type=RuntimeError" in caplog.text
    assert "private.invalid" not in caplog.text and "do-not-log" not in caplog.text
    assert "Traceback" not in caplog.text


def test_gpu_torch_import_failure_is_identifiable(tmp_path, monkeypatch, caplog):
    from gen_automation.i2v_worker.supervisor import WorkerStartupError, _verify_gpu_runtime

    monkeypatch.setitem(sys.modules, "torch", None)
    with pytest.raises(WorkerStartupError):
        _verify_gpu_runtime(_settings(tmp_path))
    assert "reason_code=torch_import_failed check=torch_import" in caplog.text


@pytest.mark.parametrize("field", ["version", "name"])
def test_gpu_preflight_never_logs_arbitrary_version_or_device_strings(
    tmp_path, monkeypatch, caplog, field
):
    from gen_automation.i2v_worker.supervisor import WorkerStartupError, _verify_gpu_runtime

    monkeypatch.setitem(
        sys.modules, "torch", _fake_torch(**{field: "private.invalid/?token=do-not-log\nforged"})
    )
    with pytest.raises(WorkerStartupError):
        _verify_gpu_runtime(_settings(tmp_path))
    assert "unrecognized" in caplog.text
    assert all(value not in caplog.text for value in ("private.invalid", "do-not-log", "forged"))


@pytest.mark.asyncio
async def test_gpu_failure_keeps_health_failed_and_never_starts_downloads(
    tmp_path, monkeypatch, caplog
):
    import gen_automation.i2v_worker.supervisor as module

    monkeypatch.setitem(sys.modules, "torch", _fake_torch(available=False))

    def forbidden(*_args, **_kwargs):
        raise AssertionError("GPU failure must precede downloads and child processes")

    monkeypatch.setattr(module, "S3ModelBootstrapper", forbidden)
    monkeypatch.setattr(module, "_start_process", forbidden)
    supervisor = WorkerSupervisor(_settings(tmp_path))
    await supervisor.start()
    await _wait_for(lambda: supervisor.failed)
    await supervisor.stop()
    assert not supervisor.ready and supervisor.stage == "gpu_preflight"
    assert "reason_code=cuda_unavailable" in caplog.text
    assert "i2v_startup_failed stage=gpu_preflight error_type=WorkerStartupError" in caplog.text
