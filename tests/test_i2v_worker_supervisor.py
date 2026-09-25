from __future__ import annotations

import asyncio
import json
from pathlib import Path
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
