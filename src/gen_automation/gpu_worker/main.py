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
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
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
    MaterializedArtifact,
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
COMFY_HEALTH_CHECK_TIMEOUT_SECONDS = 5.0
COMFY_STARTUP_TIMEOUT_SECONDS = 300.0
COMFY_STARTUP_POLL_SECONDS = 1.0
COMFY_RECYCLE_MAX_CONSECUTIVE = 1
COMFY_RECYCLE_BUDGET_RESET_SECONDS = 600.0
QUEUE_WORKER_MAX_CONSECUTIVE_RESTARTS = 3
QUEUE_WORKER_RESTART_BACKOFF_BASE_SECONDS = 1.0
QUEUE_WORKER_RESTART_BACKOFF_MAX_SECONDS = 4.0
QUEUE_WORKER_RESTART_BUDGET_RESET_SECONDS = 300.0
QUEUE_WORKER_UNSAFE_REQUEST_DRAIN_SECONDS = 5.0
QUEUE_WORKER_UNSAFE_REQUEST_DRAIN_POLL_SECONDS = 0.05
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


@dataclass(frozen=True)
class _QueueWorkerLaunchSettings:
    salad_queue_worker_enabled: bool
    salad_queue_worker_path: Path
    salad_queue_worker_log_level: str
    comfy_runtime_root: Path

    @classmethod
    def from_runtime_settings(
        cls,
        settings: WorkerRuntimeSettings,
    ) -> "_QueueWorkerLaunchSettings":
        return cls(
            salad_queue_worker_enabled=settings.salad_queue_worker_enabled,
            salad_queue_worker_path=settings.salad_queue_worker_path,
            salad_queue_worker_log_level=settings.salad_queue_worker_log_level,
            comfy_runtime_root=settings.comfy_runtime_root,
        )


@dataclass(frozen=True)
class _ComfyLaunchSettings:
    comfy_base_url: str
    comfy_python: Path
    comfy_main: Path
    comfy_runtime_root: Path
    checkpoint_root: Path
    worker_log_level: str
    fp32_vae: bool

    @classmethod
    def from_runtime_settings(
        cls,
        settings: WorkerRuntimeSettings,
        *,
        fp32_vae: bool,
    ) -> "_ComfyLaunchSettings":
        return cls(
            comfy_base_url=settings.comfy_base_url,
            comfy_python=settings.comfy_python,
            comfy_main=settings.comfy_main,
            comfy_runtime_root=settings.comfy_runtime_root,
            checkpoint_root=settings.checkpoint_root,
            worker_log_level=settings.worker_log_level,
            fp32_vae=fp32_vae,
        )


@dataclass
class _ManagedChild:
    role: str
    process: subprocess.Popen[bytes]
    started_at: float


@dataclass
class _ManagedChildren:
    comfy: _ManagedChild
    queue_worker: _ManagedChild | None


def _manifest_requires_fp32_vae(
    artifacts: tuple[MaterializedArtifact, ...],
) -> bool:
    """Enable decode-only precision for the exact Anima/Qwen split stack."""

    def identifies(artifact: MaterializedArtifact, token: str) -> bool:
        return token in artifact.logical_name.lower() or token in artifact.target_filename.lower()

    return bool(
        any(
            artifact.kind == ArtifactKind.DIFFUSION_MODEL and identifies(artifact, "anima")
            for artifact in artifacts
        )
        and any(
            artifact.kind == ArtifactKind.TEXT_ENCODER and identifies(artifact, "qwen")
            for artifact in artifacts
        )
        and any(
            artifact.kind == ArtifactKind.VAE and identifies(artifact, "qwen")
            for artifact in artifacts
        )
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


def _comfy_origin(
    settings: WorkerRuntimeSettings | _ComfyLaunchSettings,
) -> tuple[str, int]:
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


def build_comfy_command(
    settings: WorkerRuntimeSettings | _ComfyLaunchSettings,
) -> tuple[str, ...]:
    address, port = _comfy_origin(settings)
    runtime_root = settings.comfy_runtime_root
    command = (
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
    if isinstance(settings, _ComfyLaunchSettings) and settings.fp32_vae:
        return (*command, "--fp32-vae")
    return command


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
    settings: WorkerRuntimeSettings | _QueueWorkerLaunchSettings,
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
    settings: WorkerRuntimeSettings | _ComfyLaunchSettings,
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
    settings: WorkerRuntimeSettings | _QueueWorkerLaunchSettings,
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


def _child_exit_details(
    child: _ManagedChild,
    returncode: int,
    *,
    now: float,
) -> tuple[int | None, str, float]:
    signal_number = -returncode if returncode < 0 else None
    signal_name = "none"
    if signal_number is not None:
        try:
            signal_name = signal.Signals(signal_number).name
        except ValueError:
            signal_name = "unknown"
    uptime_seconds = max(0.0, now - child.started_at)
    return signal_number, signal_name, uptime_seconds


def _log_child_exit(
    child: _ManagedChild,
    returncode: int,
    *,
    now: float,
) -> tuple[int | None, str, float]:
    signal_number, signal_name, uptime_seconds = _child_exit_details(
        child,
        returncode,
        now=now,
    )
    signal_value = "none" if signal_number is None else str(signal_number)
    print(
        "GPU worker managed child exited: "
        f"role={child.role} pid={child.process.pid} returncode={returncode} "
        f"signal={signal_value} signal_name={signal_name} "
        f"uptime_seconds={uptime_seconds:.3f}",
        file=sys.stderr,
        flush=True,
    )
    return signal_number, signal_name, uptime_seconds


def _request_controlled_worker_restart(
    server: uvicorn.Server,
    worker_restart_event: asyncio.Event,
    *,
    role: str,
    reason: str,
    consecutive_restarts: int,
) -> None:
    print(
        "GPU worker managed child recovery stopped: "
        f"role={role} reason={reason} "
        f"consecutive_restarts={consecutive_restarts}",
        file=sys.stderr,
        flush=True,
    )
    worker_restart_event.set()
    server.should_exit = True


def _stop_for_requested_worker_restart(
    server: uvicorn.Server,
    *,
    consecutive_restarts: int,
) -> None:
    print(
        "GPU worker managed restart requested: "
        "reason=blank_or_fatal_output_requested "
        f"consecutive_restarts={consecutive_restarts}",
        file=sys.stderr,
        flush=True,
    )
    server.should_exit = True


async def _wait_for_event_or_timeout(
    event: asyncio.Event,
    timeout_seconds: float,
) -> bool:
    try:
        await asyncio.wait_for(event.wait(), timeout=timeout_seconds)
    except TimeoutError:
        return False
    return True


async def _bounded_comfy_health_check(health_check: Callable[[], bool]) -> bool:
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(health_check),
            timeout=COMFY_HEALTH_CHECK_TIMEOUT_SECONDS,
        )
    except Exception:
        return False


async def _wait_for_comfy_ready(
    process: subprocess.Popen[bytes],
    health_check: Callable[[], bool],
    *,
    server_task: asyncio.Task[None] | None = None,
    timeout_seconds: float = COMFY_STARTUP_TIMEOUT_SECONDS,
    poll_interval_seconds: float = COMFY_STARTUP_POLL_SECONDS,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Keep the queue detached until the manifest-bound app can execute."""

    deadline = monotonic() + timeout_seconds
    while True:
        if process.poll() is not None or (server_task is not None and server_task.done()):
            raise WorkerBootstrapConfigurationError("worker bootstrap configuration is invalid")
        if await _bounded_comfy_health_check(health_check):
            return
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise WorkerBootstrapConfigurationError("worker bootstrap configuration is invalid")
        await sleep(min(poll_interval_seconds, remaining))


async def _wait_for_unsafe_request_drain(
    unsafe_request_active: Callable[[], bool],
    *,
    timeout_seconds: float = QUEUE_WORKER_UNSAFE_REQUEST_DRAIN_SECONDS,
    poll_interval_seconds: float = QUEUE_WORKER_UNSAFE_REQUEST_DRAIN_POLL_SECONDS,
) -> bool:
    """Let a response-bound request unwind before deciding child recovery."""

    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.0, timeout_seconds)
    while unsafe_request_active():
        remaining = deadline - loop.time()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(max(0.0, poll_interval_seconds), remaining))
    return True


async def _recover_comfy_in_place(
    children: _ManagedChildren,
    server: uvicorn.Server,
    worker_restart_event: asyncio.Event,
    worker_recycle_event: asyncio.Event,
    *,
    recovery_reason: str,
    consecutive_recycles: int,
    start_comfy_process: Callable[[], subprocess.Popen[bytes]] | None,
    start_queue_worker: Callable[[], subprocess.Popen[bytes] | None] | None,
    unsafe_request_active: Callable[[], bool],
    comfy_health_check: Callable[[], bool] | None,
    monotonic: Callable[[], float],
    stop_child: Callable[[subprocess.Popen[bytes]], None],
    wait_for_comfy_ready: Callable[[subprocess.Popen[bytes], Callable[[], bool]], Awaitable[None]],
    unsafe_request_drain_seconds: float,
    unsafe_request_drain_poll_seconds: float,
) -> bool:
    """Recycle Comfy in-place after fatal output or a child exit."""

    if unsafe_request_active():
        drained = await _wait_for_unsafe_request_drain(
            unsafe_request_active,
            timeout_seconds=unsafe_request_drain_seconds,
            poll_interval_seconds=unsafe_request_drain_poll_seconds,
        )
        if server.should_exit:
            return False
        if not drained:
            _request_controlled_worker_restart(
                server,
                worker_restart_event,
                role="comfy",
                reason=f"{recovery_reason}_request_did_not_drain",
                consecutive_restarts=consecutive_recycles,
            )
            return False

    if consecutive_recycles >= COMFY_RECYCLE_MAX_CONSECUTIVE:
        _request_controlled_worker_restart(
            server,
            worker_restart_event,
            role="comfy",
            reason=f"{recovery_reason}_recycle_budget_exhausted",
            consecutive_restarts=consecutive_recycles,
        )
        return False
    if start_comfy_process is None or start_queue_worker is None or comfy_health_check is None:
        _request_controlled_worker_restart(
            server,
            worker_restart_event,
            role="comfy",
            reason=f"{recovery_reason}_recovery_unavailable",
            consecutive_restarts=consecutive_recycles,
        )
        return False

    queue_child = children.queue_worker
    if queue_child is not None:
        queue_returncode = queue_child.process.poll()
        if queue_returncode is not None:
            _log_child_exit(queue_child, queue_returncode, now=monotonic())
        else:
            await asyncio.to_thread(stop_child, queue_child.process)
        children.queue_worker = None

    comfy_child = children.comfy
    comfy_returncode = comfy_child.process.poll()
    if comfy_returncode is not None:
        _log_child_exit(comfy_child, comfy_returncode, now=monotonic())
    else:
        await asyncio.to_thread(stop_child, comfy_child.process)
    try:
        replacement_comfy = start_comfy_process()
    except (OSError, WorkerBootstrapConfigurationError):
        replacement_comfy = None
    if replacement_comfy is None:
        _request_controlled_worker_restart(
            server,
            worker_restart_event,
            role="comfy",
            reason=f"{recovery_reason}_recycle_start_failed",
            consecutive_restarts=consecutive_recycles,
        )
        return False

    replacement_started_at = monotonic()
    children.comfy = _ManagedChild("comfy", replacement_comfy, replacement_started_at)
    try:
        await wait_for_comfy_ready(replacement_comfy, comfy_health_check)
    except (OSError, TimeoutError, WorkerBootstrapConfigurationError):
        await asyncio.to_thread(stop_child, replacement_comfy)
        _request_controlled_worker_restart(
            server,
            worker_restart_event,
            role="comfy",
            reason=f"{recovery_reason}_recycle_readiness_failed",
            consecutive_restarts=consecutive_recycles,
        )
        return False
    if server.should_exit or replacement_comfy.poll() is not None:
        await asyncio.to_thread(stop_child, replacement_comfy)
        _request_controlled_worker_restart(
            server,
            worker_restart_event,
            role="comfy",
            reason=f"{recovery_reason}_recycle_child_exited",
            consecutive_restarts=consecutive_recycles,
        )
        return False

    # No queue consumer exists at this point, so clearing the application gate
    # after exact Comfy readiness cannot admit work early. Clear it before
    # spawning the queue child so the child's first claim cannot race a stale
    # recovery-in-progress 503.
    worker_recycle_event.clear()
    try:
        replacement_queue = start_queue_worker()
    except (OSError, WorkerBootstrapConfigurationError):
        replacement_queue = None
    if replacement_queue is None:
        worker_recycle_event.set()
        await asyncio.to_thread(stop_child, replacement_comfy)
        _request_controlled_worker_restart(
            server,
            worker_restart_event,
            role="salad_queue_worker",
            reason=f"{recovery_reason}_recycle_queue_start_failed",
            consecutive_restarts=consecutive_recycles,
        )
        return False

    children.queue_worker = _ManagedChild(
        "salad_queue_worker",
        replacement_queue,
        monotonic(),
    )
    print(
        "GPU worker Comfy recovery completed: "
        f"reason={recovery_reason} "
        f"comfy_pid={replacement_comfy.pid} queue_pid={replacement_queue.pid} "
        f"recycle_ordinal={consecutive_recycles + 1}",
        file=sys.stderr,
        flush=True,
    )
    return True


async def _monitor_comfy_impl(
    children: _ManagedChildren,
    server: uvicorn.Server,
    worker_restart_event: asyncio.Event,
    *,
    worker_recycle_event: asyncio.Event | None = None,
    start_comfy_process: Callable[[], subprocess.Popen[bytes]] | None = None,
    start_queue_worker: Callable[[], subprocess.Popen[bytes] | None] | None = None,
    unsafe_request_active: Callable[[], bool] = lambda: False,
    comfy_health_check: Callable[[], bool] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    wait_for_event: Callable[[asyncio.Event, float], Awaitable[bool]] = (
        _wait_for_event_or_timeout
    ),
    unsafe_request_drain_seconds: float = QUEUE_WORKER_UNSAFE_REQUEST_DRAIN_SECONDS,
    unsafe_request_drain_poll_seconds: float = (QUEUE_WORKER_UNSAFE_REQUEST_DRAIN_POLL_SECONDS),
    stop_child: Callable[[subprocess.Popen[bytes]], None] = stop_comfy,
    wait_for_comfy_ready: Callable[
        [subprocess.Popen[bytes], Callable[[], bool]], Awaitable[None]
    ] = _wait_for_comfy_ready,
) -> None:
    consecutive_queue_restarts = 0
    consecutive_comfy_recycles = 0
    while not server.should_exit:
        now = monotonic()
        if (
            consecutive_comfy_recycles > 0
            and now - children.comfy.started_at >= COMFY_RECYCLE_BUDGET_RESET_SECONDS
        ):
            consecutive_comfy_recycles = 0
        if worker_recycle_event is not None and worker_recycle_event.is_set():
            recovered = await _recover_comfy_in_place(
                children,
                server,
                worker_restart_event,
                worker_recycle_event,
                recovery_reason="fatal_output",
                consecutive_recycles=consecutive_comfy_recycles,
                start_comfy_process=start_comfy_process,
                start_queue_worker=start_queue_worker,
                unsafe_request_active=unsafe_request_active,
                comfy_health_check=comfy_health_check,
                monotonic=monotonic,
                stop_child=stop_child,
                wait_for_comfy_ready=wait_for_comfy_ready,
                unsafe_request_drain_seconds=unsafe_request_drain_seconds,
                unsafe_request_drain_poll_seconds=unsafe_request_drain_poll_seconds,
            )
            if not recovered:
                return
            consecutive_comfy_recycles += 1
            consecutive_queue_restarts = 0
            continue
        comfy_returncode = children.comfy.process.poll()
        if comfy_returncode is not None:
            if worker_recycle_event is None:
                _log_child_exit(children.comfy, comfy_returncode, now=now)
                _request_controlled_worker_restart(
                    server,
                    worker_restart_event,
                    role=children.comfy.role,
                    reason="child_exited",
                    consecutive_restarts=consecutive_queue_restarts,
                )
                return
            worker_recycle_event.set()
            recovered = await _recover_comfy_in_place(
                children,
                server,
                worker_restart_event,
                worker_recycle_event,
                recovery_reason="comfy_child_exit",
                consecutive_recycles=consecutive_comfy_recycles,
                start_comfy_process=start_comfy_process,
                start_queue_worker=start_queue_worker,
                unsafe_request_active=unsafe_request_active,
                comfy_health_check=comfy_health_check,
                monotonic=monotonic,
                stop_child=stop_child,
                wait_for_comfy_ready=wait_for_comfy_ready,
                unsafe_request_drain_seconds=unsafe_request_drain_seconds,
                unsafe_request_drain_poll_seconds=unsafe_request_drain_poll_seconds,
            )
            if not recovered:
                return
            consecutive_comfy_recycles += 1
            consecutive_queue_restarts = 0
            continue

        queue_child = children.queue_worker
        if (
            queue_child is not None
            and consecutive_queue_restarts > 0
            and now - queue_child.started_at >= QUEUE_WORKER_RESTART_BUDGET_RESET_SECONDS
        ):
            consecutive_queue_restarts = 0

        queue_returncode = queue_child.process.poll() if queue_child is not None else None
        if queue_child is not None and queue_returncode is not None:
            signal_number, signal_name, uptime_seconds = _log_child_exit(
                queue_child,
                queue_returncode,
                now=now,
            )
            if uptime_seconds >= QUEUE_WORKER_RESTART_BUDGET_RESET_SECONDS:
                consecutive_queue_restarts = 0
            children.queue_worker = None

            if unsafe_request_active():
                drained = await _wait_for_unsafe_request_drain(
                    unsafe_request_active,
                    timeout_seconds=unsafe_request_drain_seconds,
                    poll_interval_seconds=unsafe_request_drain_poll_seconds,
                )
                if worker_restart_event.is_set():
                    _stop_for_requested_worker_restart(
                        server,
                        consecutive_restarts=consecutive_queue_restarts,
                    )
                    return
                if not drained:
                    _request_controlled_worker_restart(
                        server,
                        worker_restart_event,
                        role=queue_child.role,
                        reason="unsafe_active_request",
                        consecutive_restarts=consecutive_queue_restarts,
                    )
                    return
                if worker_recycle_event is not None and worker_recycle_event.is_set():
                    # The response has unwound and the queue child is detached.
                    # The next monitor iteration performs the bounded Comfy
                    # recycle before any new queue consumer is started.
                    continue
                if server.should_exit:
                    return
            if worker_restart_event.is_set():
                _stop_for_requested_worker_restart(
                    server,
                    consecutive_restarts=consecutive_queue_restarts,
                )
                return
            comfy_returncode = children.comfy.process.poll()
            if comfy_returncode is not None:
                _log_child_exit(children.comfy, comfy_returncode, now=monotonic())
                _request_controlled_worker_restart(
                    server,
                    worker_restart_event,
                    role=children.comfy.role,
                    reason="child_exited",
                    consecutive_restarts=consecutive_queue_restarts,
                )
                return
            if start_queue_worker is None or comfy_health_check is None:
                _request_controlled_worker_restart(
                    server,
                    worker_restart_event,
                    role=queue_child.role,
                    reason="recovery_unavailable",
                    consecutive_restarts=consecutive_queue_restarts,
                )
                return

            while children.queue_worker is None:
                if consecutive_queue_restarts >= QUEUE_WORKER_MAX_CONSECUTIVE_RESTARTS:
                    _request_controlled_worker_restart(
                        server,
                        worker_restart_event,
                        role=queue_child.role,
                        reason="restart_budget_exhausted",
                        consecutive_restarts=consecutive_queue_restarts,
                    )
                    return

                restart_ordinal = consecutive_queue_restarts + 1
                backoff_seconds = min(
                    QUEUE_WORKER_RESTART_BACKOFF_BASE_SECONDS * (2 ** (restart_ordinal - 1)),
                    QUEUE_WORKER_RESTART_BACKOFF_MAX_SECONDS,
                )
                if await wait_for_event(worker_restart_event, backoff_seconds):
                    _stop_for_requested_worker_restart(
                        server,
                        consecutive_restarts=consecutive_queue_restarts,
                    )
                    return
                if worker_recycle_event is not None and worker_recycle_event.is_set():
                    break
                if server.should_exit:
                    return
                if unsafe_request_active():
                    _request_controlled_worker_restart(
                        server,
                        worker_restart_event,
                        role=queue_child.role,
                        reason="unsafe_active_request",
                        consecutive_restarts=consecutive_queue_restarts,
                    )
                    return

                now = monotonic()
                comfy_returncode = children.comfy.process.poll()
                if comfy_returncode is not None:
                    _log_child_exit(children.comfy, comfy_returncode, now=now)
                    _request_controlled_worker_restart(
                        server,
                        worker_restart_event,
                        role=children.comfy.role,
                        reason="child_exited",
                        consecutive_restarts=consecutive_queue_restarts,
                    )
                    return
                if not await _bounded_comfy_health_check(comfy_health_check):
                    _request_controlled_worker_restart(
                        server,
                        worker_restart_event,
                        role=children.comfy.role,
                        reason="health_check_failed",
                        consecutive_restarts=consecutive_queue_restarts,
                    )
                    return
                if worker_restart_event.is_set():
                    _stop_for_requested_worker_restart(
                        server,
                        consecutive_restarts=consecutive_queue_restarts,
                    )
                    return
                if unsafe_request_active():
                    _request_controlled_worker_restart(
                        server,
                        worker_restart_event,
                        role=queue_child.role,
                        reason="unsafe_active_request",
                        consecutive_restarts=consecutive_queue_restarts,
                    )
                    return

                consecutive_queue_restarts = restart_ordinal
                try:
                    restarted_process = start_queue_worker()
                except (OSError, WorkerBootstrapConfigurationError):
                    restarted_process = None
                if restarted_process is None:
                    print(
                        "GPU worker managed child restart failed: "
                        f"role={queue_child.role} previous_pid={queue_child.process.pid} "
                        f"restart_ordinal={restart_ordinal} "
                        f"backoff_seconds={backoff_seconds:.3f}",
                        file=sys.stderr,
                        flush=True,
                    )
                    continue

                restarted_at = monotonic()
                children.queue_worker = _ManagedChild(
                    role=queue_child.role,
                    process=restarted_process,
                    started_at=restarted_at,
                )
                previous_signal = "none" if signal_number is None else str(signal_number)
                print(
                    "GPU worker managed child restarted: "
                    f"role={queue_child.role} previous_pid={queue_child.process.pid} "
                    f"previous_returncode={queue_returncode} "
                    f"previous_signal={previous_signal} "
                    f"previous_signal_name={signal_name} "
                    f"previous_uptime_seconds={uptime_seconds:.3f} "
                    f"new_pid={restarted_process.pid} "
                    f"restart_ordinal={restart_ordinal} "
                    f"backoff_seconds={backoff_seconds:.3f}",
                    file=sys.stderr,
                    flush=True,
                )
                break
            if worker_recycle_event is not None and worker_recycle_event.is_set():
                continue
            continue

        if worker_restart_event.is_set():
            _stop_for_requested_worker_restart(
                server,
                consecutive_restarts=consecutive_queue_restarts,
            )
            return
        if await wait_for_event(
            worker_restart_event,
            COMFY_MONITOR_INTERVAL_SECONDS,
        ):
            _stop_for_requested_worker_restart(
                server,
                consecutive_restarts=consecutive_queue_restarts,
            )
            return


async def _monitor_comfy(
    children: _ManagedChildren | tuple[subprocess.Popen[bytes], ...],
    server: uvicorn.Server,
    worker_restart_event: asyncio.Event,
    *,
    worker_recycle_event: asyncio.Event | None = None,
    start_comfy_process: Callable[[], subprocess.Popen[bytes]] | None = None,
    start_queue_worker: Callable[[], subprocess.Popen[bytes] | None] | None = None,
    unsafe_request_active: Callable[[], bool] = lambda: False,
    comfy_health_check: Callable[[], bool] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    wait_for_event: Callable[[asyncio.Event, float], Awaitable[bool]] = (
        _wait_for_event_or_timeout
    ),
    unsafe_request_drain_seconds: float = QUEUE_WORKER_UNSAFE_REQUEST_DRAIN_SECONDS,
    unsafe_request_drain_poll_seconds: float = (QUEUE_WORKER_UNSAFE_REQUEST_DRAIN_POLL_SECONDS),
    stop_child: Callable[[subprocess.Popen[bytes]], None] = stop_comfy,
    wait_for_comfy_ready: Callable[
        [subprocess.Popen[bytes], Callable[[], bool]], Awaitable[None]
    ] = _wait_for_comfy_ready,
) -> None:
    try:
        if isinstance(children, tuple):
            if not 1 <= len(children) <= 2:
                raise ValueError("invalid managed child count")
            started_at = monotonic()
            children = _ManagedChildren(
                comfy=_ManagedChild("comfy", children[0], started_at),
                queue_worker=(
                    _ManagedChild("salad_queue_worker", children[1], started_at)
                    if len(children) == 2
                    else None
                ),
            )
        await _monitor_comfy_impl(
            children,
            server,
            worker_restart_event,
            worker_recycle_event=worker_recycle_event,
            start_comfy_process=start_comfy_process,
            start_queue_worker=start_queue_worker,
            unsafe_request_active=unsafe_request_active,
            comfy_health_check=comfy_health_check,
            monotonic=monotonic,
            wait_for_event=wait_for_event,
            unsafe_request_drain_seconds=unsafe_request_drain_seconds,
            unsafe_request_drain_poll_seconds=unsafe_request_drain_poll_seconds,
            stop_child=stop_child,
            wait_for_comfy_ready=wait_for_comfy_ready,
        )
    except Exception as error:
        print(
            f"GPU worker managed child monitor failed: exception={type(error).__name__}",
            file=sys.stderr,
            flush=True,
        )
        _request_controlled_worker_restart(
            server,
            worker_restart_event,
            role="managed_child_monitor",
            reason="monitor_exception",
            consecutive_restarts=0,
        )


async def serve_worker(
    settings: WorkerSettings,
    processes: tuple[subprocess.Popen[bytes], ...],
    executor: ComfyExecutor,
    *,
    host: str,
    port: int,
    log_level: str,
) -> bool:
    if not processes:
        raise WorkerBootstrapConfigurationError("worker bootstrap configuration is invalid")
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
    started_at = time.monotonic()
    children = _ManagedChildren(
        comfy=_ManagedChild("comfy", processes[0], started_at),
        queue_worker=(
            _ManagedChild("salad_queue_worker", processes[1], started_at)
            if len(processes) > 1
            else None
        ),
    )
    monitor = asyncio.create_task(
        _monitor_comfy(
            children,
            server,
            worker_restart_event,
            comfy_health_check=executor.is_ready,
        )
    )
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
        self._unsafe_active_requests = 0

    def activate(self, application: ASGIApp) -> None:
        self._application = application

    def has_unsafe_active_request(self) -> bool:
        return self._unsafe_active_requests > 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        application = self._application
        unsafe_request = (
            scope["type"] == "http"
            and scope.get("method") == "POST"
            and scope.get("path") == "/jobs/generate"
        )
        if unsafe_request:
            self._unsafe_active_requests += 1
        try:
            await application(scope, receive, send)
        finally:
            if unsafe_request:
                self._unsafe_active_requests -= 1


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
    managed_children: _ManagedChildren | None = None
    executor: ComfyExecutor | None = None
    monitor: asyncio.Task[None] | None = None
    worker_lifespan: Any | None = None
    worker_lifespan_started = False
    worker_restart_event = asyncio.Event()
    worker_recycle_event = asyncio.Event()
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
        comfy_launch_settings = _ComfyLaunchSettings.from_runtime_settings(
            settings,
            fp32_vae=_manifest_requires_fp32_vae(bootstrap_result.artifacts),
        )
        startup_stage[0] = "comfy_start"
        process = start_comfy(comfy_launch_settings)
        comfy_started_at = time.monotonic()
        startup_stage[0] = "worker_settings"
        worker_settings = settings.to_worker_settings()
        application = create_worker_app(
            settings=worker_settings,
            executor=executor,
            worker_restart_event=worker_restart_event,
            worker_recycle_event=worker_recycle_event,
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
        startup_stage[0] = "comfy_readiness"
        comfy_readiness_started_at = time.monotonic()
        _startup_progress(
            startup_stage[0],
            "started",
            elapsed_seconds=comfy_readiness_started_at - startup_started_at,
        )
        await _wait_for_comfy_ready(
            process,
            executor.is_ready,
            server_task=server_task,
        )
        _startup_progress(
            startup_stage[0],
            "completed",
            elapsed_seconds=time.monotonic() - startup_started_at,
            duration_seconds=time.monotonic() - comfy_readiness_started_at,
        )

        # No queue consumer may exist until the real manifest-bound app and
        # Comfy readiness are active. This startup invariant makes it safe for
        # the controller to enqueue durably once every old instance is gone.
        startup_stage[0] = "queue_worker_start"
        queue_launch_settings = _QueueWorkerLaunchSettings.from_runtime_settings(settings)
        queue_process = start_salad_queue_worker(queue_launch_settings)
        queue_started_at = time.monotonic()
        managed_children = _ManagedChildren(
            comfy=_ManagedChild("comfy", process, comfy_started_at),
            queue_worker=(
                _ManagedChild("salad_queue_worker", queue_process, queue_started_at)
                if queue_process is not None
                else None
            ),
        )
        del settings
        gc.collect()

        monitor = asyncio.create_task(
            _monitor_comfy(
                managed_children,
                server,
                worker_restart_event,
                worker_recycle_event=worker_recycle_event,
                start_comfy_process=lambda: start_comfy(comfy_launch_settings),
                start_queue_worker=lambda: start_salad_queue_worker(queue_launch_settings),
                unsafe_request_active=router.has_unsafe_active_request,
                comfy_health_check=executor.is_ready,
            )
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
            if managed_children is not None:
                if managed_children.queue_worker is not None:
                    stop_comfy(managed_children.queue_worker.process)
                stop_comfy(managed_children.comfy.process)
            else:
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
