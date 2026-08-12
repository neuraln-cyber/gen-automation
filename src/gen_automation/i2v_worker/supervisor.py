from __future__ import annotations

import asyncio
import os
import signal
import stat
import subprocess
from contextlib import suppress
from pathlib import Path

from gen_automation.i2v_worker.artifacts import ModelBootstrapError, S3ModelBootstrapper
from gen_automation.i2v_worker.comfy import ComfyClient
from gen_automation.i2v_worker.settings import I2VWorkerSettings


class WorkerStartupError(Exception):
    pass


class WorkerSupervisor:
    def __init__(self, settings: I2VWorkerSettings) -> None:
        self.settings = settings
        self.comfy: subprocess.Popen[bytes] | None = None
        self.queue_worker: subprocess.Popen[bytes] | None = None
        self.comfy_client: ComfyClient | None = None
        self.ready = False
        self.failed = False
        self._task: asyncio.Task[None] | None = None
        self._stopping = False

    async def start(self) -> None:
        if self._task is not None:
            raise WorkerStartupError("worker supervisor already started")
        self._task = asyncio.create_task(self._run(), name="i2v-worker-bootstrap")

    async def stop(self) -> None:
        self._stopping = True
        self.ready = False
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
        for process in (self.queue_worker, self.comfy):
            if process is not None:
                await asyncio.to_thread(_stop_process, process)
        if self.comfy_client is not None:
            await self.comfy_client.close()

    async def _run(self) -> None:
        try:
            _ensure_directories(self.settings)
            await asyncio.to_thread(_verify_gpu_runtime)
            await S3ModelBootstrapper(self.settings).bootstrap()
            self.comfy = _start_process(
                _comfy_command(self.settings),
                cwd=self.settings.comfy_root,
                environment=_child_environment(),
            )
            self.comfy_client = ComfyClient(
                base_url=self.settings.comfy_base_url,
                request_timeout_seconds=self.settings.network_timeout_seconds,
                network_attempts=self.settings.network_attempts,
                poll_seconds=self.settings.comfy_poll_seconds,
            )
            while not await self.comfy_client.ready():
                if self.comfy.poll() is not None:
                    raise WorkerStartupError("ComfyUI exited during startup")
                await asyncio.sleep(2)
            self.ready = True
            if self.settings.queue_worker_enabled:
                self.queue_worker = _start_process(
                    (self.settings.queue_worker_path.as_posix(),),
                    cwd=self.settings.runtime_root,
                    environment=_queue_environment(),
                )
            while not self._stopping:
                if self.comfy.poll() is not None or (
                    self.queue_worker is not None and self.queue_worker.poll() is not None
                ):
                    raise WorkerStartupError("worker child process exited")
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            raise
        except (ModelBootstrapError, WorkerStartupError, OSError):
            self.ready = False
            self.failed = True


def _ensure_directories(settings: I2VWorkerSettings) -> None:
    paths = (
        settings.comfy_root / "models/diffusion_models",
        settings.comfy_root / "models/text_encoders",
        settings.comfy_root / "models/vae/Wan",
        settings.comfy_root / "models/loras",
        settings.runtime_root / "input",
        settings.runtime_root / "output",
        settings.runtime_root / "temp",
        settings.runtime_root / "user",
        settings.runtime_root / "jobs",
    )
    for path in paths:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise WorkerStartupError("worker runtime path is invalid")


def _verify_gpu_runtime() -> None:
    try:
        import torch  # type: ignore[import-not-found]

        if (
            torch.__version__.split("+", 1)[0] != "2.9.1"
            or not torch.cuda.is_available()
            or torch.cuda.device_count() != 1
            or torch.cuda.get_device_name(0) != "NVIDIA GeForce RTX 5090"
        ):
            raise ValueError
    except (ImportError, RuntimeError, ValueError):
        raise WorkerStartupError("exact RTX 5090 runtime is unavailable") from None


def _comfy_command(settings: I2VWorkerSettings) -> tuple[str, ...]:
    runtime = settings.runtime_root
    return (
        settings.comfy_python.as_posix(),
        settings.comfy_main.as_posix(),
        "--listen",
        "127.0.0.1",
        "--port",
        "8188",
        "--disable-auto-launch",
        "--disable-all-custom-nodes",
        "--disable-api-nodes",
        "--disable-metadata",
        "--base-directory",
        settings.comfy_root.as_posix(),
        "--input-directory",
        (runtime / "input").as_posix(),
        "--output-directory",
        (runtime / "output").as_posix(),
        "--temp-directory",
        (runtime / "temp").as_posix(),
        "--user-directory",
        (runtime / "user").as_posix(),
        "--database-url",
        f"sqlite:///{(runtime / 'user/comfyui.db').as_posix()}",
        "--preview-method",
        "none",
        "--highvram",
        "--log-stdout",
    )


def _child_environment() -> dict[str, str]:
    allowed = {
        "HOME",
        "LANG",
        "LC_ALL",
        "LD_LIBRARY_PATH",
        "PATH",
        "PYTHONPATH",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "TMPDIR",
    }
    prefixes = ("CUDA_", "NVIDIA_", "PYTORCH_", "TORCH_", "TRITON_")
    environment = {
        key: value
        for key, value in os.environ.items()
        if key in allowed or key.startswith(prefixes)
    }
    environment.update(
        {
            "DO_NOT_TRACK": "1",
            "HF_HUB_DISABLE_TELEMETRY": "1",
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "NO_PROXY": "127.0.0.1,localhost,::1",
        }
    )
    return environment


def _queue_environment() -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key in {"HOME", "LANG", "LC_ALL", "PATH", "SSL_CERT_DIR", "SSL_CERT_FILE"}
        or key.startswith("SALAD_")
    }
    environment["SALAD_LOG_LEVEL"] = "error"
    return environment


def _start_process(
    command: tuple[str, ...],
    *,
    cwd: Path,
    environment: dict[str, str],
) -> subprocess.Popen[bytes]:
    executable = Path(command[0])
    metadata = executable.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise WorkerStartupError("worker executable is invalid")
    return subprocess.Popen(  # noqa: S603
        command,
        cwd=cwd,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=None,
        stderr=None,
        close_fds=True,
        start_new_session=True,
    )


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)  # type: ignore[attr-defined]
        else:
            process.terminate()
        process.wait(timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        with suppress(OSError):
            if os.name == "posix":
                os.killpg(  # type: ignore[attr-defined]
                    process.pid,
                    getattr(signal, "SIGKILL", signal.SIGTERM),
                )
            else:
                process.kill()
