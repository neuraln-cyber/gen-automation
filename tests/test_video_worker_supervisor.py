import asyncio
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest
from starlette.types import ASGIApp

from gen_automation.domain.signing import derive_public_key, encode_base64url
from gen_automation.video_worker import main as worker_main
from gen_automation.video_worker.model_integrity import ModelIntegrityError
from gen_automation.video_worker.models import (
    DEFAULT_MAX_IMAGE_DIMENSION,
    DEFAULT_MAX_IMAGE_PIXELS,
    DEFAULT_MAX_SOURCE_BYTES,
    WorkerEnvironment,
    WorkerSettings,
)


async def _asgi_request(application: ASGIApp, path: str) -> tuple[int, bytes]:
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


async def test_bootstrap_probe_hands_off_on_the_same_application() -> None:
    router = worker_main.SwitchableVideoWorkerApplication()

    assert await _asgi_request(router, "/health") == (
        200,
        b'{"status":"bootstrapping","version":"video-worker.v1"}',
    )
    assert await _asgi_request(router, "/ready") == (
        503,
        b'{"status":"not_ready","version":"video-worker.v1"}',
    )

    async def active_application(
        _scope: object,
        _receive: object,
        send: Any,
    ) -> None:
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    router.activate(cast(ASGIApp, active_application))
    assert await _asgi_request(router, "/health") == (204, b"")


class _ProbeServer:
    def __init__(self, application: ASGIApp) -> None:
        self.application = application
        self.started = False
        self.force_exit = False
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


def _settings(staging_root: Path) -> WorkerSettings:
    private_key = encode_base64url(bytes(range(1, 33)))
    return WorkerSettings(
        environment=WorkerEnvironment.TEST,
        verification_keys={"video-worker-v1": derive_public_key(private_key)},
        allowed_source_origins=frozenset({"https://sources.example.test"}),
        allowed_upload_origin="https://uploads.example.test",
        staging_root=staging_root,
    )


async def test_health_is_served_while_model_integrity_hashing_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server: _ProbeServer | None = None
    observed: list[tuple[int, bytes]] = []

    def build_server(application: ASGIApp) -> _ProbeServer:
        nonlocal server
        server = _ProbeServer(application)
        return server

    def fail_after_probe() -> None:
        assert server is not None
        observed.append(asyncio.run(_asgi_request(server.application, "/health")))
        observed.append(asyncio.run(_asgi_request(server.application, "/ready")))
        raise ModelIntegrityError("expected test stop")

    monkeypatch.setattr(worker_main, "build_worker_server", build_server)
    monkeypatch.setattr(worker_main, "verify_model_runtime", fail_after_probe)

    with pytest.raises(ModelIntegrityError, match="expected test stop"):
        await worker_main.serve_worker_lifecycle(_settings(tmp_path))

    assert observed == [
        (200, b'{"status":"bootstrapping","version":"video-worker.v1"}'),
        (503, b'{"status":"not_ready","version":"video-worker.v1"}'),
    ]
    assert server is not None
    assert server.started


class _FakeServer:
    def __init__(self, events: list[str]) -> None:
        self._should_exit = False
        self.events = events

    @property
    def should_exit(self) -> bool:
        return self._should_exit

    @should_exit.setter
    def should_exit(self, value: bool) -> None:
        self._should_exit = value
        if value:
            self.events.append("uvicorn-shutdown")


class _RunningProcess:
    pid = 123

    def poll(self) -> None:
        return None


class _ExitedProcess:
    pid = 456

    def poll(self) -> int:
        return 1


async def test_queue_exit_interrupts_comfy_before_draining_an_in_flight_render(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    render_interrupted = asyncio.Event()
    server = _FakeServer(events)
    comfy_process = cast("subprocess.Popen[bytes]", cast(Any, _RunningProcess()))
    queue_process = cast("subprocess.Popen[bytes]", cast(Any, _ExitedProcess()))

    async def in_flight_render() -> None:
        events.append("render-started")
        await render_interrupted.wait()
        events.append("render-interrupted")

    def interrupt(process: subprocess.Popen[bytes]) -> None:
        assert process is comfy_process
        events.append("comfy-interrupt")
        render_interrupted.set()

    monkeypatch.setattr(worker_main, "interrupt_process", interrupt)
    render_task = asyncio.create_task(in_flight_render())
    await asyncio.sleep(0)
    child_exit_event = asyncio.Event()

    await worker_main.monitor_runtime_processes(
        comfy_process=comfy_process,
        queue_process=queue_process,
        server=cast(Any, server),
        child_exit_event=child_exit_event,
    )
    await render_task

    assert child_exit_event.is_set()
    assert events == [
        "render-started",
        "comfy-interrupt",
        "uvicorn-shutdown",
        "render-interrupted",
    ]


async def test_uvicorn_shutdown_is_bounded_when_server_task_hangs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancelled = asyncio.Event()

    class HungServer:
        should_exit = False
        force_exit = False

    async def hung_server() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    server = HungServer()
    server_task = asyncio.create_task(hung_server())
    monkeypatch.setattr(worker_main, "SERVER_SHUTDOWN_TIMEOUT_SECONDS", 0.01)

    await worker_main._bounded_server_shutdown(cast(Any, server), server_task)

    assert server.should_exit
    assert server.force_exit
    assert server_task.cancelled()
    assert cancelled.is_set()


class _HungProcess:
    pid = 789

    def __init__(self) -> None:
        self.terminated = False
        self.killed = False
        self.wait_calls = 0

    def poll(self) -> None:
        return None

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls += 1
        if self.wait_calls == 1:
            raise subprocess.TimeoutExpired("video-runtime", timeout)
        return -9


def test_process_shutdown_escalates_when_runtime_ignores_interrupt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _HungProcess()

    def unavailable_process_group(_process_id: int, _signal_number: int) -> None:
        raise OSError

    monkeypatch.setattr(worker_main, "_signal_process_group", unavailable_process_group)
    worker_main.stop_comfy(
        cast("subprocess.Popen[bytes]", cast(Any, process)),
        grace_seconds=0.01,
    )

    assert process.terminated
    assert process.killed
    assert process.wait_calls == 2


def test_worker_default_source_limits_are_exported_contract_constants(tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    assert settings.max_source_bytes == DEFAULT_MAX_SOURCE_BYTES == 50 * 1024 * 1024
    assert settings.max_image_dimension == DEFAULT_MAX_IMAGE_DIMENSION == 16_384
    assert settings.max_image_pixels == DEFAULT_MAX_IMAGE_PIXELS == 64_000_000
