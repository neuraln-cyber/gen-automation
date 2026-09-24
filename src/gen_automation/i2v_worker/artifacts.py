from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import stat
import time
from collections import deque
from collections.abc import Iterator, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import suppress
from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from gen_automation.domain.private_delivery import PrivateDeliveryRoute
from gen_automation.i2v_worker.models import ModelObject
from gen_automation.i2v_worker.settings import I2VWorkerSettings

_AWS_CREDENTIAL_ENV = (
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_SECURITY_TOKEN",
    "AWS_SHARED_CREDENTIALS_FILE",
    "AWS_CONFIG_FILE",
    "AWS_PROFILE",
    "AWS_WEB_IDENTITY_TOKEN_FILE",
)
_LOGGER = logging.getLogger(__name__)


class ModelBootstrapError(Exception):
    """A redacted bootstrap failure safe for worker logs."""


def _safe_target(root: Path, relative: str) -> Path:
    root_resolved = root.resolve()
    target = (root_resolved / relative).resolve()
    if not target.is_relative_to(root_resolved):
        raise ModelBootstrapError("model bootstrap failed")
    return target


def _hash_file(path: Path, chunk_bytes: int = 8 * 1024 * 1024) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    try:
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise OSError
        with path.open("rb") as stream:
            while chunk := stream.read(chunk_bytes):
                digest.update(chunk)
                size += len(chunk)
    except OSError:
        raise ModelBootstrapError("model bootstrap failed") from None
    return size, digest.hexdigest()


class S3ModelBootstrapper:
    def __init__(self, settings: I2VWorkerSettings, *, client: Any | None = None) -> None:
        self.settings = settings
        self._owns_client = client is None
        try:
            self.client = client or boto3.client(
                "s3",
                region_name=settings.aws_region,
                endpoint_url=settings.s3_endpoint_url,
                config=Config(
                    signature_version="s3v4",
                    s3={"addressing_style": "virtual"},
                    max_pool_connections=settings.artifact_download_concurrency,
                    retries={"mode": "standard", "max_attempts": settings.network_attempts},
                    connect_timeout=min(settings.network_timeout_seconds, 30),
                    read_timeout=settings.network_timeout_seconds,
                ),
            )
            if settings.model_delivery_domain is not None:
                first = settings.model_objects[0]
                route = PrivateDeliveryRoute(
                    settings.model_delivery_domain,
                    first.bucket,
                    settings.aws_region,
                    "models",
                )
                self.client.meta.events.register("before-send.s3.GetObject", route.before_send)
            elif settings.require_private_delivery:
                raise ValueError("private delivery is required")
        except (BotoCoreError, ClientError, ValueError):
            raise ModelBootstrapError("model bootstrap failed") from None

    async def bootstrap(self) -> tuple[Path, ...]:
        targets: list[Path] = []
        try:
            for model in self.settings.model_objects:
                targets.append(await asyncio.to_thread(self._materialize, model))
            return tuple(targets)
        finally:
            if self._owns_client:
                close = getattr(self.client, "close", None)
                if callable(close):
                    with suppress(Exception):
                        await asyncio.to_thread(close)
            for name in _AWS_CREDENTIAL_ENV:
                os.environ.pop(name, None)

    def _materialize(self, model: ModelObject) -> Path:
        target = _safe_target(self.settings.comfy_root, model.install_path)
        partial_path = target.with_name(f".{target.name}.partial")
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        if target.exists():
            size, existing_digest = _hash_file(target)
            if size == model.byte_size and existing_digest == model.sha256:
                _LOGGER.info("i2v_model_reused role=%s bytes=%d", model.role, size)
                return target
            raise ModelBootstrapError("model bootstrap failed")

        resume_at = 0
        digest = hashlib.sha256()
        if partial_path.exists():
            resume_at, _ = _hash_file(partial_path)
            if resume_at > model.byte_size:
                raise ModelBootstrapError("model bootstrap failed")
            with partial_path.open("rb") as existing:
                while chunk := existing.read(8 * 1024 * 1024):
                    digest.update(chunk)

        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(partial_path, flags, 0o600)
            started = time.monotonic()
            last_report = started
            written = resume_at
            _LOGGER.info(
                "i2v_model_download role=%s resumed_bytes=%d total_bytes=%d route=%s",
                model.role,
                resume_at,
                model.byte_size,
                "private_delivery" if self.settings.model_delivery_domain else "direct_s3",
            )
            with os.fdopen(descriptor, "ab", closefd=True) as output:
                for part in self._ordered_ranges(model, resume_at):
                    output.write(part)
                    digest.update(part)
                    output.flush()
                    os.fsync(output.fileno())
                    written += len(part)
                    current = time.monotonic()
                    if current - last_report >= 15 or written == model.byte_size:
                        _LOGGER.info(
                            "i2v_model_progress role=%s bytes=%d total_bytes=%d "
                            "mib_per_second=%.2f",
                            model.role,
                            written,
                            model.byte_size,
                            (written - resume_at) / max(current - started, 0.001) / (1024 * 1024),
                        )
                        last_report = current
            if partial_path.stat().st_size != model.byte_size or digest.hexdigest() != model.sha256:
                partial_path.unlink()
                raise ModelBootstrapError("model bootstrap failed")
            os.chmod(partial_path, 0o444)
            os.replace(partial_path, target)
            return target
        except ModelBootstrapError:
            raise
        except OSError:
            raise ModelBootstrapError("model bootstrap failed") from None

    def _ordered_ranges(self, model: ModelObject, start: int) -> Iterator[bytes]:
        """Bound memory/in-flight requests while retaining a contiguous resumable prefix."""
        starts = iter(range(start, model.byte_size, self.settings.artifact_chunk_bytes))
        pending: deque[Future[bytes]] = deque()
        with ThreadPoolExecutor(max_workers=self.settings.artifact_download_concurrency) as pool:

            def submit() -> None:
                next_start = next(starts, None)
                if next_start is not None:
                    end = min(next_start + self.settings.artifact_chunk_bytes, model.byte_size) - 1
                    pending.append(pool.submit(self._download_range, model, next_start, end))

            for _ in range(self.settings.artifact_download_concurrency):
                submit()
            try:
                while pending:
                    yield pending.popleft().result()
                    submit()
            finally:
                for future in pending:
                    future.cancel()

    def _download_range(self, model: ModelObject, start: int, end: int) -> bytes:
        expected = end - start + 1
        for attempt in range(self.settings.network_attempts):
            body: Any | None = None
            try:
                response = self.client.get_object(
                    Bucket=model.bucket,
                    Key=model.key,
                    VersionId=model.version_id,
                    Range=f"bytes={start}-{end}",
                )
                if not isinstance(response, Mapping):
                    raise ModelBootstrapError("model bootstrap failed")
                body = response.get("Body")
                if (
                    response.get("VersionId") != model.version_id
                    or response.get("ContentLength") != expected
                    or response.get("ContentRange") != f"bytes {start}-{end}/{model.byte_size}"
                    or body is None
                ):
                    raise ModelBootstrapError("model bootstrap failed")
                data = body.read(expected + 1)
                if not isinstance(data, bytes) or len(data) != expected:
                    raise OSError
                return data
            except ModelBootstrapError:
                raise
            except (BotoCoreError, ClientError, OSError):
                if attempt + 1 >= self.settings.network_attempts:
                    raise ModelBootstrapError("model bootstrap failed") from None
                time.sleep(min(2**attempt, 16))
            finally:
                if body is not None:
                    with suppress(Exception):
                        body.close()
        raise ModelBootstrapError("model bootstrap failed")
