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
from collections.abc import Callable, Mapping
from contextlib import suppress
from pathlib import Path
from typing import NoReturn, cast
from urllib.parse import SplitResult, urlsplit

import uvicorn
from pydantic import ValidationError

from gen_automation.gpu_worker.app import create_worker_app
from gen_automation.gpu_worker.artifacts import (
    ArtifactBootstrapError,
    ArtifactBootstrapResult,
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
        ensure_model_roots(settings.checkpoint_root, settings.lora_root)
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
    child_exit_event: asyncio.Event,
) -> None:
    while not server.should_exit:
        if any(process.poll() is not None for process in processes):
            child_exit_event.set()
            server.should_exit = True
            return
        await asyncio.sleep(COMFY_MONITOR_INTERVAL_SECONDS)


async def serve_worker(
    settings: WorkerSettings,
    processes: tuple[subprocess.Popen[bytes], ...],
    executor: ComfyExecutor,
    *,
    host: str,
    port: int,
    log_level: str,
) -> bool:
    application = create_worker_app(
        settings=settings,
        executor=executor,
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
    child_exit_event = asyncio.Event()
    monitor = asyncio.create_task(_monitor_comfy(processes, server, child_exit_event))
    try:
        await server.serve()
    finally:
        monitor.cancel()
        with suppress(asyncio.CancelledError):
            await monitor
    return child_exit_event.is_set()


def _startup_failure() -> NoReturn:
    print("GPU worker startup failed", file=sys.stderr)
    raise SystemExit(78)


def main() -> None:
    process: subprocess.Popen[bytes] | None = None
    queue_process: subprocess.Popen[bytes] | None = None
    executor: ComfyExecutor | None = None
    worker_settings: WorkerSettings | None = None
    worker_host = ""
    worker_port = 0
    worker_log_level = ""
    try:
        settings = WorkerRuntimeSettings()
        harden_parent_process(settings.environment)
        asyncio.run(bootstrap_worker_models(settings))
        executor = ComfyExecutor(
            base_url=settings.comfy_base_url,
            execution_timeout_seconds=settings.comfy_execution_timeout_seconds,
            max_output_bytes=settings.max_output_bytes,
            max_total_output_bytes=settings.max_total_output_bytes,
            approved_node_classes=settings.approved_workflow_node_classes,
        )
        process = start_comfy(settings)
        queue_process = start_salad_queue_worker(settings)
        worker_settings = settings.to_worker_settings()
        worker_host = settings.worker_host
        worker_port = settings.worker_port
        worker_log_level = settings.worker_log_level
        del settings
        # Drop the last durable Python reference to bootstrap-only object-store
        # credentials before the long-lived HTTP server accepts work.
        gc.collect()
    except (
        ArtifactBootstrapError,
        ComfyConfigurationError,
        TimeoutError,
        ValidationError,
        WorkerBootstrapConfigurationError,
    ):
        if executor is not None:
            executor.close()
        if process is not None:
            stop_comfy(process)
        if queue_process is not None:
            stop_comfy(queue_process)
        _startup_failure()

    assert process is not None
    assert executor is not None
    assert worker_settings is not None
    monitored_processes = (process, queue_process) if queue_process is not None else (process,)
    child_exited = False
    try:
        child_exited = asyncio.run(
            serve_worker(
                worker_settings,
                monitored_processes,
                executor,
                host=worker_host,
                port=worker_port,
                log_level=worker_log_level,
            )
        )
    finally:
        executor.close()
        if queue_process is not None:
            stop_comfy(queue_process)
        stop_comfy(process)

    if child_exited:
        _startup_failure()


if __name__ == "__main__":
    main()
