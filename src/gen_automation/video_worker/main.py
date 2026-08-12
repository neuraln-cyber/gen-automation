import asyncio
import json
import os
import signal
import stat
import subprocess
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any, NoReturn, cast

import httpx2
import uvicorn
from starlette.types import ASGIApp, Receive, Scope, Send

from gen_automation.video_worker.app import create_video_worker_app
from gen_automation.video_worker.comfy import NativeComfyWanExecutor
from gen_automation.video_worker.model_integrity import (
    A14B_VIDEO_PROFILE_IDS,
    ModelIntegrityError,
    verify_model_runtime,
)
from gen_automation.video_worker.models import WorkerEnvironment, WorkerSettings

_COMFY_ROOT = Path("/opt/comfyui")
_COMFY_RUNTIME_ROOT = Path("/opt/video-worker/runtime/comfy")
_COMFY_BASE_URL = "http://127.0.0.1:8188"
_QUEUE_WORKER_PATH = Path("/usr/local/bin/salad-http-job-queue-worker")
SERVER_START_TIMEOUT_SECONDS = 10.0
SERVER_GRACEFUL_SHUTDOWN_SECONDS = 5
SERVER_SHUTDOWN_TIMEOUT_SECONDS = 8.0
LIFESPAN_SHUTDOWN_TIMEOUT_SECONDS = 10.0
RUNTIME_MONITOR_INTERVAL_SECONDS = 0.1
PROCESS_SHUTDOWN_GRACE_SECONDS = 5.0
PROCESS_KILL_WAIT_SECONDS = 2.0


class VideoWorkerConfigurationError(RuntimeError):
    pass


def _required_environment(name: str) -> str:
    value = os.environ.get(name)
    if value is None or not value.strip():
        raise VideoWorkerConfigurationError("video worker configuration is invalid")
    return value


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise VideoWorkerConfigurationError("video worker configuration is invalid")
        result[key] = value
    return result


def _json_object(name: str) -> dict[str, str]:
    try:
        raw = json.loads(_required_environment(name), object_pairs_hook=_unique_object)
        if not isinstance(raw, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in raw.items()
        ):
            raise ValueError
        return raw
    except (json.JSONDecodeError, TypeError, ValueError):
        raise VideoWorkerConfigurationError("video worker configuration is invalid") from None


def _json_string_set(name: str) -> frozenset[str]:
    try:
        raw = json.loads(_required_environment(name))
        if not isinstance(raw, list) or not raw or not all(isinstance(item, str) for item in raw):
            raise ValueError
        return frozenset(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        raise VideoWorkerConfigurationError("video worker configuration is invalid") from None


def load_settings() -> WorkerSettings:
    raw_environment = os.environ.get("VIDEO_WORKER_ENVIRONMENT", WorkerEnvironment.PRODUCTION)
    try:
        return WorkerSettings(
            environment=WorkerEnvironment(raw_environment),
            verification_keys=_json_object("VIDEO_WORKER_VERIFICATION_KEYS_JSON"),
            allowed_source_origins=_json_string_set("VIDEO_WORKER_ALLOWED_SOURCE_ORIGINS_JSON"),
            allowed_upload_origin=_required_environment("VIDEO_WORKER_ALLOWED_UPLOAD_ORIGIN"),
            allowed_profile_ids=_json_string_set("VIDEO_WORKER_PROFILE_IDS_JSON"),
            staging_root=Path(
                os.environ.get("VIDEO_WORKER_STAGING_ROOT", "/opt/video-worker/runtime")
            ),
        )
    except (ValueError, TypeError):
        raise VideoWorkerConfigurationError("video worker configuration is invalid") from None


def ensure_staging_root(path: Path) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
        status = path.lstat()
        if not stat.S_ISDIR(status.st_mode) or path.is_symlink():
            raise OSError
        probe = path / ".startup-write-probe"
        with probe.open("xb") as handle:
            handle.write(b"ok")
            handle.flush()
            os.fsync(handle.fileno())
        probe.unlink()
    except OSError:
        raise VideoWorkerConfigurationError("video worker runtime is unavailable") from None


def _comfy_environment(source: dict[str, str]) -> dict[str, str]:
    allowed_names = {
        "HOME",
        "LANG",
        "LC_ALL",
        "LD_LIBRARY_PATH",
        "PATH",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "TZ",
    }
    allowed_prefixes = ("CUDA_", "NVIDIA_", "PYTORCH_", "TORCH_")
    result = {
        name: value
        for name, value in source.items()
        if (name in allowed_names or name.startswith(allowed_prefixes))
        and "\x00" not in name
        and "\x00" not in value
    }
    result.update(
        {
            "HF_HUB_OFFLINE": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": _COMFY_ROOT.as_posix(),
            "TRANSFORMERS_OFFLINE": "1",
        }
    )
    return result


def start_comfy(*, profile_ids: frozenset[str]) -> subprocess.Popen[bytes]:
    input_directory = _COMFY_RUNTIME_ROOT / "input"
    output_directory = _COMFY_RUNTIME_ROOT / "output"
    temp_directory = _COMFY_RUNTIME_ROOT / "temp"
    user_directory = _COMFY_RUNTIME_ROOT / "user"
    for directory in (input_directory, output_directory, temp_directory, user_directory):
        directory.mkdir(parents=True, exist_ok=True)
    profile_runtime_flags = (
        (
            "--disable-all-custom-nodes",
            "--whitelist-custom-nodes",
            "ComfyUI-GGUF",
            "ComfyUI-NAG",
            "--lowvram",
            "--disable-smart-memory",
            "--cache-none",
            "--reserve-vram",
            "3",
            "--async-offload",
            "2",
        )
        if profile_ids == A14B_VIDEO_PROFILE_IDS
        else ("--disable-all-custom-nodes",)
    )
    command = (
        "/opt/video-worker-venv/bin/python",
        (_COMFY_ROOT / "main.py").as_posix(),
        "--listen",
        "127.0.0.1",
        "--port",
        "8188",
        "--disable-auto-launch",
        *profile_runtime_flags,
        "--disable-api-nodes",
        "--disable-metadata",
        "--base-directory",
        _COMFY_ROOT.as_posix(),
        "--input-directory",
        input_directory.as_posix(),
        "--output-directory",
        output_directory.as_posix(),
        "--temp-directory",
        temp_directory.as_posix(),
        "--user-directory",
        user_directory.as_posix(),
        "--database-url",
        f"sqlite:///{(user_directory / 'comfyui.db').as_posix()}",
        "--preview-method",
        "none",
        "--log-stdout",
    )
    try:
        return subprocess.Popen(  # noqa: S603 - immutable local executable/arguments
            command,
            cwd=_COMFY_ROOT,
            env=_comfy_environment(dict(os.environ)),
            start_new_session=True,
        )
    except OSError:
        raise VideoWorkerConfigurationError("video worker runtime is unavailable") from None


def interrupt_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            _signal_process_group(process.pid, int(signal.SIGTERM))
        else:
            process.terminate()
    except OSError:
        with suppress(OSError):
            process.terminate()


def stop_comfy(
    process: subprocess.Popen[bytes],
    *,
    grace_seconds: float = PROCESS_SHUTDOWN_GRACE_SECONDS,
) -> None:
    if process.poll() is not None:
        return
    interrupt_process(process)
    try:
        process.wait(timeout=grace_seconds)
        return
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        if os.name == "posix":
            _signal_process_group(
                process.pid,
                int(getattr(signal, "SIGKILL", signal.SIGTERM)),
            )
        else:
            process.kill()
    except OSError:
        try:
            process.kill()
        except OSError:
            return
    with suppress(OSError, subprocess.TimeoutExpired):
        process.wait(timeout=PROCESS_KILL_WAIT_SECONDS)


def _signal_process_group(process_id: int, signal_number: int) -> None:
    kill_group = cast(
        "Callable[[int, int], None] | None",
        getattr(os, "killpg", None),
    )
    if kill_group is None:
        raise OSError
    kill_group(process_id, signal_number)


def start_queue_worker() -> subprocess.Popen[bytes]:
    try:
        metadata = _QUEUE_WORKER_PATH.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise OSError
        log_level = os.environ.get("VIDEO_WORKER_SALAD_LOG_LEVEL", "error").lower()
        if log_level not in {"debug", "info", "warn", "error"}:
            raise ValueError
        allowed = {
            "HOME",
            "LANG",
            "LC_ALL",
            "PATH",
            "SSL_CERT_DIR",
            "SSL_CERT_FILE",
        }
        environment = {
            name: value
            for name, value in os.environ.items()
            if name in allowed and "\x00" not in name and "\x00" not in value
        }
        environment.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
        environment["SALAD_LOG_LEVEL"] = log_level
        return subprocess.Popen(  # noqa: S603 - immutable local executable
            (_QUEUE_WORKER_PATH.as_posix(),),
            cwd=_COMFY_RUNTIME_ROOT,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=None,
            stderr=None,
            close_fds=True,
            start_new_session=True,
        )
    except (OSError, ValueError):
        raise VideoWorkerConfigurationError("video worker runtime is unavailable") from None


async def _wait_for_server_start(
    server: uvicorn.Server,
    server_task: asyncio.Task[None],
) -> None:
    deadline = asyncio.get_running_loop().time() + SERVER_START_TIMEOUT_SECONDS
    while not server.started:
        if server_task.done() or asyncio.get_running_loop().time() >= deadline:
            server.should_exit = True
            raise VideoWorkerConfigurationError("video worker runtime is unavailable")
        await asyncio.sleep(0.01)


async def _bootstrap_probe_application(
    scope: Scope,
    receive: Receive,
    send: Send,
) -> None:
    del receive
    if scope["type"] != "http":
        return
    path = scope.get("path", "")
    method = scope.get("method", "GET")
    if path == "/health":
        status_code = 200
        body = b'{"status":"bootstrapping","version":"video-worker.v1"}'
    elif path == "/ready":
        status_code = 503
        body = b'{"status":"not_ready","version":"video-worker.v1"}'
    else:
        status_code = 404
        body = b'{"status":"not_found","version":"video-worker.v1"}'
    headers = [
        (b"content-type", b"application/json"),
        (b"cache-control", b"no-store"),
        (b"content-length", str(len(body)).encode("ascii")),
    ]
    await send({"type": "http.response.start", "status": status_code, "headers": headers})
    await send(
        {
            "type": "http.response.body",
            "body": b"" if method == "HEAD" else body,
            "more_body": False,
        }
    )


class SwitchableVideoWorkerApplication:
    """Keep one listening socket from bootstrap through active service."""

    def __init__(self) -> None:
        self._application: ASGIApp = _bootstrap_probe_application

    def activate(self, application: ASGIApp) -> None:
        self._application = application

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        application = self._application
        await application(scope, receive, send)


def build_worker_server(application: ASGIApp) -> uvicorn.Server:
    return uvicorn.Server(
        uvicorn.Config(
            application,
            host="0.0.0.0",  # noqa: S104
            port=8000,
            access_log=False,
            proxy_headers=False,
            server_header=False,
            date_header=False,
            workers=1,
            limit_concurrency=4,
            timeout_keep_alive=5,
            timeout_graceful_shutdown=SERVER_GRACEFUL_SHUTDOWN_SECONDS,
            # The real FastAPI lifespan is entered explicitly at the in-place
            # handoff because the bootstrap probe owns the listening socket.
            lifespan="off",
        )
    )


async def _bounded_server_shutdown(
    server: uvicorn.Server,
    server_task: asyncio.Task[None],
) -> None:
    server.should_exit = True
    if server_task.done():
        with suppress(asyncio.CancelledError, OSError, SystemExit):
            await server_task
        return
    try:
        await asyncio.wait_for(
            asyncio.shield(server_task),
            timeout=SERVER_SHUTDOWN_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        server.force_exit = True
        server_task.cancel()
        with suppress(asyncio.CancelledError, OSError, SystemExit):
            await server_task


async def _bounded_lifespan_shutdown(lifespan: Any) -> None:
    shutdown_task = asyncio.create_task(lifespan.__aexit__(None, None, None))
    try:
        await asyncio.wait_for(
            asyncio.shield(shutdown_task),
            timeout=LIFESPAN_SHUTDOWN_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        shutdown_task.cancel()
        with suppress(asyncio.CancelledError):
            await shutdown_task


async def monitor_runtime_processes(
    *,
    comfy_process: subprocess.Popen[bytes],
    queue_process: subprocess.Popen[bytes],
    server: uvicorn.Server,
    child_exit_event: asyncio.Event,
) -> None:
    while not server.should_exit:
        queue_exited = queue_process.poll() is not None
        comfy_exited = comfy_process.poll() is not None
        if queue_exited or comfy_exited:
            child_exit_event.set()
            # Interrupt the surviving paid runtime before asking Uvicorn to
            # drain active requests. A strict queue-sidecar disconnect exits
            # its process, so the same branch covers provider disconnection.
            if queue_exited and not comfy_exited:
                interrupt_process(comfy_process)
            elif comfy_exited and not queue_exited:
                interrupt_process(queue_process)
            server.should_exit = True
            return
        await asyncio.sleep(RUNTIME_MONITOR_INTERVAL_SECONDS)


def wait_for_comfy(executor: NativeComfyWanExecutor, process: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise VideoWorkerConfigurationError("video worker runtime is unavailable")
        if executor.is_ready():
            return
        time.sleep(1)
    raise VideoWorkerConfigurationError("video worker runtime is unavailable")


async def serve_worker_lifecycle(settings: WorkerSettings) -> bool:
    router = SwitchableVideoWorkerApplication()
    server = build_worker_server(router)
    server_task = asyncio.create_task(server.serve())
    comfy_process: subprocess.Popen[bytes] | None = None
    queue_process: subprocess.Popen[bytes] | None = None
    client: httpx2.Client | None = None
    monitor_task: asyncio.Task[None] | None = None
    worker_lifespan: Any | None = None
    worker_lifespan_started = False
    child_exit_event = asyncio.Event()
    try:
        await _wait_for_server_start(server, server_task)
        # Hashing 18 GB is deliberately moved off the event loop. The socket
        # above remains healthy while integrity and Comfy startup complete.
        await asyncio.to_thread(
            verify_model_runtime,
            profile_ids=settings.allowed_profile_ids,
        )
        comfy_process = start_comfy(profile_ids=settings.allowed_profile_ids)
        client = httpx2.Client(
            base_url=_COMFY_BASE_URL,
            follow_redirects=False,
            trust_env=False,
        )
        executor = NativeComfyWanExecutor(
            client=client,
            input_directory=_COMFY_RUNTIME_ROOT / "input",
            output_directory=_COMFY_RUNTIME_ROOT / "output",
        )
        await asyncio.to_thread(wait_for_comfy, executor, comfy_process)
        application = create_video_worker_app(settings=settings, executor=executor)
        worker_lifespan = application.router.lifespan_context(application)
        await worker_lifespan.__aenter__()
        worker_lifespan_started = True
        router.activate(cast(ASGIApp, application))

        # Start the queue only after /jobs/generate is active on the already
        # listening socket; this avoids both a bind race and bootstrap 404s.
        queue_process = start_queue_worker()
        monitor_task = asyncio.create_task(
            monitor_runtime_processes(
                comfy_process=comfy_process,
                queue_process=queue_process,
                server=server,
                child_exit_event=child_exit_event,
            )
        )
        completed, _pending = await asyncio.wait(
            (server_task, monitor_task),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if monitor_task in completed and child_exit_event.is_set():
            await _bounded_server_shutdown(server, server_task)
            return True
        await server_task
        return child_exit_event.is_set()
    finally:
        server.should_exit = True
        if monitor_task is not None:
            monitor_task.cancel()
            with suppress(asyncio.CancelledError):
                await monitor_task
        await _bounded_server_shutdown(server, server_task)
        if worker_lifespan_started:
            assert worker_lifespan is not None
            await _bounded_lifespan_shutdown(worker_lifespan)
        if client is not None:
            client.close()
        if queue_process is not None:
            stop_comfy(queue_process)
        if comfy_process is not None:
            stop_comfy(comfy_process)


def _startup_failure() -> NoReturn:
    raise SystemExit("video worker failed to start")


def main() -> None:
    try:
        settings = load_settings()
        ensure_staging_root(settings.staging_root)
        runtime_failed = asyncio.run(serve_worker_lifecycle(settings))
    except (
        ModelIntegrityError,
        OSError,
        VideoWorkerConfigurationError,
        ValueError,
    ):
        _startup_failure()
    if runtime_failed:
        _startup_failure()


if __name__ == "__main__":
    main()
