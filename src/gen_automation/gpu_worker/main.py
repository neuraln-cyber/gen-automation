import asyncio
import ctypes
import gc
import importlib
import ipaddress
import os
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any, NoReturn, cast
from urllib.parse import SplitResult, urlsplit

import uvicorn
from pydantic import ValidationError
from starlette.types import ASGIApp, Receive, Scope, Send

from gen_automation.gpu_worker.app import create_worker_app
from gen_automation.gpu_worker.artifacts import (
    ArtifactBootstrapError,
    ArtifactBootstrapResult,
    ArtifactKind,
    bootstrap_artifacts,
)
from gen_automation.gpu_worker.bootstrap import (
    S3ArtifactDownloader,
    WorkerBootstrapConfigurationError,
    WorkerRuntimeSettings,
    build_artifact_downloader,
    ensure_model_roots,
    load_artifact_manifest,
    scrub_artifact_credentials,
)
from gen_automation.gpu_worker.comfy import ComfyConfigurationError, ComfyExecutor
from gen_automation.gpu_worker.models import WorkerEnvironment, WorkerSettings

COMFY_SHUTDOWN_GRACE_SECONDS = 20.0
COMFY_MONITOR_INTERVAL_SECONDS = 1.0
WORKER_SERVER_START_TIMEOUT_SECONDS = 10.0
_PR_SET_DUMPABLE = 4
_PR_SET_NO_NEW_PRIVS = 38
_SAFE_CHILD_ENVIRONMENT_NAMES = frozenset(
    {
        "HOME",
        "LANG",
        "LC_ALL",
        "LD_LIBRARY_PATH",
        "PATH",
        "PYTHONPATH",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "TMP",
        "TMPDIR",
        "TEMP",
    }
)
_SAFE_CHILD_ENVIRONMENT_PREFIXES = (
    "CUDA_",
    "NVIDIA_",
    "PYTORCH_",
    "TORCH_",
    "TRITON_",
    "OMP_",
    "MKL_",
    "NUMEXPR_",
)
_FIXED_CHILD_ENVIRONMENT = {
    "DO_NOT_TRACK": "1",
    "HF_HUB_DISABLE_TELEMETRY": "1",
    "HF_HUB_OFFLINE": "1",
    "NO_PROXY": "127.0.0.1,localhost,::1",
    "TRANSFORMERS_OFFLINE": "1",
}
_QUEUE_CHILD_ENVIRONMENT_NAMES = frozenset(
    {
        "HOME",
        "LANG",
        "LC_ALL",
        "PATH",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
    }
)


def _apply_linux_process_hardening() -> None:
    try:
        resource_module = importlib.import_module("resource")
        setrlimit = cast(
            "Callable[[int, tuple[int, int]], None]",
            resource_module.setrlimit,
        )
        rlimit_core = cast("int", resource_module.RLIMIT_CORE)
        setrlimit(rlimit_core, (0, 0))
        libc = ctypes.CDLL(None, use_errno=True)
        prctl = libc.prctl
        prctl.argtypes = [
            ctypes.c_int,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
        ]
        prctl.restype = ctypes.c_int
        dumpable_result = prctl(_PR_SET_DUMPABLE, 0, 0, 0, 0)
        privileges_result = prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)
    except (AttributeError, OSError, ValueError):
        raise WorkerBootstrapConfigurationError(
            "worker bootstrap configuration is invalid"
        ) from None
    if dumpable_result != 0 or privileges_result != 0:
        raise WorkerBootstrapConfigurationError("worker bootstrap configuration is invalid")


def harden_parent_process(environment: WorkerEnvironment) -> None:
    """Block core dumps, privilege gains, and same-UID reads of parent secrets."""

    os.umask(0o077)
    if sys.platform != "linux":
        if environment == WorkerEnvironment.PRODUCTION:
            raise WorkerBootstrapConfigurationError("worker bootstrap configuration is invalid")
        return
    _apply_linux_process_hardening()


def _runtime_directory(path: Path) -> None:
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata = path.lstat()
        os.chmod(path, 0o700)
    except OSError:
        raise WorkerBootstrapConfigurationError(
            "worker bootstrap configuration is invalid"
        ) from None
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise WorkerBootstrapConfigurationError("worker bootstrap configuration is invalid")


def ensure_runtime_directories(settings: WorkerRuntimeSettings) -> None:
    for path in (
        settings.comfy_runtime_root,
        settings.comfy_runtime_root / "input",
        settings.comfy_runtime_root / "output",
        settings.comfy_runtime_root / "temp",
        settings.comfy_runtime_root / "user",
    ):
        _runtime_directory(path)


def write_verified_detector_whitelist(
    settings: WorkerRuntimeSettings,
    result: ArtifactBootstrapResult,
) -> None:
    """Allow only manifest-verified detector archives to use legacy Torch loading."""

    detector_names = tuple(
        artifact.target_filename
        for artifact in result.artifacts
        if artifact.kind == ArtifactKind.DETECTOR
    )
    if len(detector_names) > 1:
        raise WorkerBootstrapConfigurationError("worker bootstrap configuration is invalid")
    whitelist_directory = (
        settings.comfy_runtime_root / "user" / "default" / "ComfyUI-Impact-Subpack"
    )
    _runtime_directory(whitelist_directory)
    whitelist_path = whitelist_directory / "model-whitelist.txt"
    descriptor = -1
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(whitelist_path, flags, 0o600)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError
        content = "".join(f"{name}\n" for name in detector_names).encode()
        with os.fdopen(descriptor, "wb", closefd=False) as whitelist_file:
            whitelist_file.write(content)
            whitelist_file.flush()
            os.fsync(descriptor)
        os.chmod(whitelist_path, 0o600)
    except OSError:
        raise WorkerBootstrapConfigurationError(
            "worker bootstrap configuration is invalid"
        ) from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _comfy_origin(settings: WorkerRuntimeSettings) -> tuple[str, int]:
    try:
        parsed: SplitResult = urlsplit(settings.comfy_base_url)
        address = ipaddress.ip_address(parsed.hostname or "")
        port = parsed.port or 80
    except ValueError:
        raise WorkerBootstrapConfigurationError(
            "worker bootstrap configuration is invalid"
        ) from None
    if (
        parsed.scheme != "http"
        or not address.is_loopback
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or not 1 <= port <= 65535
    ):
        raise WorkerBootstrapConfigurationError("worker bootstrap configuration is invalid")
    return address.compressed, port


def build_comfy_command(settings: WorkerRuntimeSettings) -> tuple[str, ...]:
    address, port = _comfy_origin(settings)
    runtime_root = settings.comfy_runtime_root
    return (
        settings.comfy_python.as_posix(),
        settings.comfy_main.as_posix(),
        "--listen",
        address,
        "--port",
        str(port),
        "--disable-auto-launch",
        "--disable-all-custom-nodes",
        "--whitelist-custom-nodes",
        "ComfyUI-Impact-Pack",
        "ComfyUI-Impact-Subpack",
        "--disable-api-nodes",
        "--disable-metadata",
        "--models-directory",
        settings.checkpoint_root.parent.as_posix(),
        "--input-directory",
        (runtime_root / "input").as_posix(),
        "--output-directory",
        (runtime_root / "output").as_posix(),
        "--temp-directory",
        (runtime_root / "temp").as_posix(),
        "--user-directory",
        (runtime_root / "user").as_posix(),
        "--database-url",
        f"sqlite:///{(runtime_root / 'user' / 'comfyui.db').as_posix()}",
        "--preview-method",
        "none",
        "--verbose",
        settings.worker_log_level.upper(),
        "--log-stdout",
    )


def build_comfy_environment(source: Mapping[str, str]) -> dict[str, str]:
    """Construct an allowlisted child environment with no controller/storage keys."""

    result: dict[str, str] = {}
    for name, value in source.items():
        if (
            (
                name in _SAFE_CHILD_ENVIRONMENT_NAMES
                or name.startswith(_SAFE_CHILD_ENVIRONMENT_PREFIXES)
            )
            and "\x00" not in name
            and "\x00" not in value
        ):
            result[name] = value
    result.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    result.update(_FIXED_CHILD_ENVIRONMENT)
    return result


def build_queue_worker_environment(
    settings: WorkerRuntimeSettings,
    source: Mapping[str, str],
) -> dict[str, str]:
    result = {
        name: value
        for name, value in source.items()
        if name in _QUEUE_CHILD_ENVIRONMENT_NAMES and "\x00" not in name and "\x00" not in value
    }
    result.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    result["SALAD_LOG_LEVEL"] = settings.salad_queue_worker_log_level.lower()
    return result


async def bootstrap_worker_models(
    settings: WorkerRuntimeSettings,
    *,
    downloader: S3ArtifactDownloader | None = None,
) -> ArtifactBootstrapResult:
    async with asyncio.timeout(settings.model_bootstrap_timeout_seconds):
        ensure_model_roots(
            settings.checkpoint_root,
            settings.lora_root,
            settings.detector_root,
            settings.diffusion_model_root,
            settings.text_encoder_root,
            settings.vae_root,
        )
        ensure_runtime_directories(settings)
        manifest = load_artifact_manifest(settings.model_manifest_json.get_secret_value())
        resolved_downloader = downloader or build_artifact_downloader(settings)
        try:
            return await bootstrap_artifacts(
                manifest,
                resolved_downloader,
                expected_manifest_sha256=settings.model_manifest_sha256,
                checkpoint_root=settings.checkpoint_root,
                lora_root=settings.lora_root,
                detector_root=settings.detector_root,
                diffusion_model_root=settings.diffusion_model_root,
                text_encoder_root=settings.text_encoder_root,
                vae_root=settings.vae_root,
            )
        finally:
            await resolved_downloader.close()
            scrub_artifact_credentials()


def start_comfy(
    settings: WorkerRuntimeSettings,
) -> subprocess.Popen[bytes]:
    command = build_comfy_command(settings)
    environment = build_comfy_environment(os.environ)
    try:
        for required_path in (settings.comfy_python, settings.comfy_main):
            metadata = required_path.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise OSError
        return subprocess.Popen(  # noqa: S603
            command,
            cwd=settings.comfy_main.parent,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=None,
            stderr=None,
            close_fds=True,
            start_new_session=True,
        )
    except OSError:
        raise WorkerBootstrapConfigurationError(
            "worker bootstrap configuration is invalid"
        ) from None


def start_salad_queue_worker(
    settings: WorkerRuntimeSettings,
) -> subprocess.Popen[bytes] | None:
    if not settings.salad_queue_worker_enabled:
        return None
    try:
        metadata = settings.salad_queue_worker_path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise OSError
        return subprocess.Popen(  # noqa: S603
            (settings.salad_queue_worker_path.as_posix(),),
            cwd=settings.comfy_runtime_root,
            env=build_queue_worker_environment(settings, os.environ),
            stdin=subprocess.DEVNULL,
            stdout=None,
            stderr=None,
            close_fds=True,
            start_new_session=True,
        )
    except OSError:
        raise WorkerBootstrapConfigurationError(
            "worker bootstrap configuration is invalid"
        ) from None


def stop_comfy(
    process: subprocess.Popen[bytes],
    *,
    grace_seconds: float = COMFY_SHUTDOWN_GRACE_SECONDS,
    process_group: bool | None = None,
) -> None:
    if process.poll() is not None:
        return
    signal_group = os.name == "posix" if process_group is None else process_group
    try:
        if signal_group:
            _signal_process_group(process.pid, int(signal.SIGTERM))
        else:
            process.terminate()
        process.wait(timeout=grace_seconds)
        return
    except (OSError, subprocess.TimeoutExpired):
        pass
    with suppress(OSError):
        if signal_group:
            _signal_process_group(
                process.pid,
                int(getattr(signal, "SIGKILL", signal.SIGTERM)),
            )
        else:
            process.kill()
    with suppress(OSError, subprocess.TimeoutExpired):
        process.wait(timeout=5.0)


def _signal_process_group(process_id: int, signal_number: int) -> None:
    kill_group = cast(
        "Callable[[int, int], None] | None",
        getattr(os, "killpg", None),
    )
    if kill_group is None:
        raise OSError
    kill_group(process_id, signal_number)


async def _monitor_comfy(
    processes: tuple[subprocess.Popen[bytes], ...],
    server: uvicorn.Server,
    worker_restart_event: asyncio.Event,
) -> None:
    while not server.should_exit:
        if worker_restart_event.is_set() or any(
            process.poll() is not None for process in processes
        ):
            worker_restart_event.set()
            server.should_exit = True
            return
        try:
            await asyncio.wait_for(
                worker_restart_event.wait(),
                timeout=COMFY_MONITOR_INTERVAL_SECONDS,
            )
        except TimeoutError:
            continue
        server.should_exit = True
        return


async def serve_worker(
    settings: WorkerSettings,
    processes: tuple[subprocess.Popen[bytes], ...],
    executor: ComfyExecutor,
    *,
    host: str,
    port: int,
    log_level: str,
) -> bool:
    worker_restart_event = asyncio.Event()
    application = create_worker_app(
        settings=settings,
        executor=executor,
        worker_restart_event=worker_restart_event,
    )
    configuration = uvicorn.Config(
        application,
        host=host,
        port=port,
        log_level=log_level.lower(),
        access_log=False,
        proxy_headers=False,
        server_header=False,
        date_header=False,
        workers=1,
        limit_concurrency=8,
        timeout_keep_alive=5,
    )
    server = uvicorn.Server(configuration)
    monitor = asyncio.create_task(_monitor_comfy(processes, server, worker_restart_event))
    try:
        await server.serve()
    finally:
        monitor.cancel()
        with suppress(asyncio.CancelledError):
            await monitor
    return worker_restart_event.is_set()


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
        body = b'{"status":"bootstrapping"}'
    elif path == "/ready":
        status_code = 503
        body = b'{"status":"not_ready"}'
    else:
        status_code = 404
        body = b'{"status":"not_found"}'
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


class _SwitchableWorkerApplication:
    """Keep one listening socket while startup hands off to the real app."""

    def __init__(self) -> None:
        self._application: ASGIApp = _bootstrap_probe_application

    def activate(self, application: ASGIApp) -> None:
        self._application = application

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        application = self._application
        await application(scope, receive, send)


def _build_switchable_server(
    application: ASGIApp,
    *,
    host: str,
    port: int,
    log_level: str,
) -> uvicorn.Server:
    return uvicorn.Server(
        uvicorn.Config(
            application,
            host=host,
            port=port,
            log_level=log_level.lower(),
            access_log=False,
            proxy_headers=False,
            server_header=False,
            date_header=False,
            workers=1,
            limit_concurrency=8,
            timeout_keep_alive=5,
            # The real FastAPI lifespan is entered explicitly at handoff because
            # the bootstrap responder owns the socket when Uvicorn starts.
            lifespan="off",
        )
    )


async def _wait_for_server_start(
    server: uvicorn.Server,
    server_task: asyncio.Task[None],
) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + WORKER_SERVER_START_TIMEOUT_SECONDS
    while not server.started:
        if server_task.done():
            with suppress(asyncio.CancelledError, OSError, SystemExit):
                await server_task
            raise WorkerBootstrapConfigurationError("worker bootstrap configuration is invalid")
        if loop.time() >= deadline:
            server.should_exit = True
            raise WorkerBootstrapConfigurationError("worker bootstrap configuration is invalid")
        await asyncio.sleep(0.01)


async def _serve_worker_lifecycle(
    settings: WorkerRuntimeSettings,
    *,
    startup_stage: list[str],
    startup_started_at: float,
) -> bool:
    router = _SwitchableWorkerApplication()
    server = _build_switchable_server(
        router,
        host=settings.worker_host,
        port=settings.worker_port,
        log_level=settings.worker_log_level,
    )
    server_task = asyncio.create_task(server.serve())
    process: subprocess.Popen[bytes] | None = None
    queue_process: subprocess.Popen[bytes] | None = None
    executor: ComfyExecutor | None = None
    monitor: asyncio.Task[None] | None = None
    worker_lifespan: Any | None = None
    worker_lifespan_started = False
    worker_restart_event = asyncio.Event()
    try:
        startup_stage[0] = "bootstrap_probe_server"
        await _wait_for_server_start(server, server_task)
        _startup_progress(
            startup_stage[0],
            "ready",
            elapsed_seconds=time.monotonic() - startup_started_at,
        )

        startup_stage[0] = "model_bootstrap"
        bootstrap_started_at = time.monotonic()
        _startup_progress(
            startup_stage[0],
            "started",
            elapsed_seconds=bootstrap_started_at - startup_started_at,
        )
        bootstrap_task = asyncio.create_task(bootstrap_worker_models(settings))
        completed, _pending = await asyncio.wait(
            (bootstrap_task, server_task),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if server_task in completed and not bootstrap_task.done():
            bootstrap_task.cancel()
            with suppress(asyncio.CancelledError):
                await bootstrap_task
            if server.should_exit:
                return False
            raise WorkerBootstrapConfigurationError("worker bootstrap configuration is invalid")
        bootstrap_result = await bootstrap_task
        bootstrap_completed_at = time.monotonic()
        _startup_progress(
            startup_stage[0],
            "completed",
            elapsed_seconds=bootstrap_completed_at - startup_started_at,
            duration_seconds=bootstrap_completed_at - bootstrap_started_at,
            artifact_count=len(bootstrap_result.artifacts),
        )

        startup_stage[0] = "detector_whitelist"
        write_verified_detector_whitelist(settings, bootstrap_result)
        startup_stage[0] = "executor_initialization"
        executor = ComfyExecutor(
            base_url=settings.comfy_base_url,
            execution_timeout_seconds=settings.comfy_execution_timeout_seconds,
            max_output_bytes=settings.max_output_bytes,
            max_total_output_bytes=settings.max_total_output_bytes,
            approved_node_classes=settings.approved_workflow_node_classes,
        )
        startup_stage[0] = "comfy_start"
        process = start_comfy(settings)
        startup_stage[0] = "queue_worker_start"
        queue_process = start_salad_queue_worker(settings)
        startup_stage[0] = "worker_settings"
        worker_settings = settings.to_worker_settings()
        application = create_worker_app(
            settings=worker_settings,
            executor=executor,
            worker_restart_event=worker_restart_event,
        )
        worker_lifespan = application.router.lifespan_context(application)
        await worker_lifespan.__aenter__()
        worker_lifespan_started = True
        router.activate(cast(ASGIApp, application))
        _startup_progress(
            "worker_application",
            "active",
            elapsed_seconds=time.monotonic() - startup_started_at,
        )
        del settings
        gc.collect()

        monitored_processes = (process, queue_process) if queue_process is not None else (process,)
        monitor = asyncio.create_task(
            _monitor_comfy(monitored_processes, server, worker_restart_event)
        )
        await server_task
        return worker_restart_event.is_set()
    finally:
        server.should_exit = True
        if monitor is not None:
            monitor.cancel()
            with suppress(asyncio.CancelledError):
                await monitor
        if not server_task.done():
            with suppress(asyncio.CancelledError, OSError, SystemExit):
                await server_task
        elif not server_task.cancelled():
            # Retrieve any provider-server failure even when startup failed
            # first, avoiding an unobserved task exception during teardown.
            server_task.exception()
        try:
            if worker_lifespan_started:
                assert worker_lifespan is not None
                await worker_lifespan.__aexit__(None, None, None)
        finally:
            if executor is not None:
                executor.close()
            if queue_process is not None:
                stop_comfy(queue_process)
            if process is not None:
                stop_comfy(process)


def _safe_startup_error_message(error: BaseException) -> str:
    if isinstance(error, ValidationError):
        details = []
        for item in error.errors(
            include_url=False,
            include_context=False,
            include_input=False,
        ):
            location = ".".join(str(part) for part in item["loc"])
            message = str(item["msg"])
            details.append(f"{location}: {message}" if location else message)
        return "; ".join(details) or "validation failed"
    return " ".join(str(error).split()) or "no detail"


def _startup_failure(stage: str, error: BaseException | None = None) -> NoReturn:
    detail = f"GPU worker startup failed: stage={stage}"
    if error is not None:
        detail += f" exception={type(error).__name__} message={_safe_startup_error_message(error)}"
    print(detail, file=sys.stderr, flush=True)
    raise SystemExit(78)


def _startup_progress(
    stage: str,
    status: str,
    *,
    elapsed_seconds: float,
    duration_seconds: float | None = None,
    artifact_count: int | None = None,
) -> None:
    detail = (
        f"GPU worker startup progress: stage={stage} status={status} "
        f"elapsed_seconds={elapsed_seconds:.3f}"
    )
    if duration_seconds is not None:
        detail += f" duration_seconds={duration_seconds:.3f}"
    if artifact_count is not None:
        detail += f" artifact_count={artifact_count}"
    print(detail, file=sys.stderr, flush=True)


def main() -> None:
    startup_stage = ["runtime_settings"]
    startup_started_at = time.monotonic()
    try:
        settings = WorkerRuntimeSettings()
        startup_stage[0] = "process_hardening"
        harden_parent_process(settings.environment)
        restart_required = asyncio.run(
            _serve_worker_lifecycle(
                settings,
                startup_stage=startup_stage,
                startup_started_at=startup_started_at,
            )
        )
    except (
        ArtifactBootstrapError,
        ComfyConfigurationError,
        TimeoutError,
        ValidationError,
        WorkerBootstrapConfigurationError,
    ) as error:
        _startup_failure(startup_stage[0], error)

    if restart_required:
        _startup_failure("managed_child_monitor")


if __name__ == "__main__":
    main()
