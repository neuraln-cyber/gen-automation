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
    assert log_calls == [("face stabilizer startup failed: reason_code=%s", ("internal",))]
    assert "sensitive detector path" not in repr(log_calls)
