import asyncio
import json
import math
import os
import re
import stat
from collections import deque
from collections.abc import AsyncIterator, Mapping, MutableMapping
from contextlib import suppress
from functools import partial
from pathlib import Path
from typing import Any, NoReturn

import boto3
from anyio import to_thread
from botocore.client import Config
from botocore.exceptions import BotoCoreError, ClientError
from pydantic import AliasChoices, AnyHttpUrl, Field, SecretStr, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from gen_automation.domain.generation_limits import MAX_OUTPUTS_PER_GENERATION_JOB
from gen_automation.gpu_worker.artifacts import (
    MAX_ARTIFACT_PART_DOWNLOAD_ATTEMPTS,
    MAX_PARALLEL_ARTIFACT_PARTS,
    MULTIPART_ARTIFACT_PART_BYTES,
    STREAM_READ_BYTES,
    ArtifactBootstrapError,
    ArtifactDownloader,
    ArtifactManifest,
    ModelArtifactSpec,
    Sha256,
)
from gen_automation.gpu_worker.models import (
    DEFAULT_APPROVED_WORKFLOW_NODE_CLASSES,
    RuntimeAdmissionId,
    RuntimeWorkerInstanceId,
    WorkerEnvironment,
    WorkerSettings,
)

MAX_MANIFEST_BYTES = 256 * 1024
MAX_MANIFEST_DEPTH = 32
MAX_MANIFEST_ITEMS = 1024
_BUCKET_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,254}$")
_LOG_LEVELS = frozenset({"critical", "error", "warning", "info", "debug"})
_WORKER_BIND_HOST = "0.0.0.0"  # noqa: S104
_ARTIFACT_CREDENTIAL_ENV_NAMES = frozenset(
    {
        "GEN_WORKER_ARTIFACT_ACCESS_KEY_ID",
        "GEN_WORKER_ARTIFACT_SECRET_ACCESS_KEY",
        "GEN_WORKER_ARTIFACT_SESSION_TOKEN",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_SECURITY_TOKEN",
        "AWS_PROFILE",
        "AWS_SHARED_CREDENTIALS_FILE",
        "AWS_CONFIG_FILE",
        "BOTO_CONFIG",
    }
)
_ARTIFACT_PART_RETRY_BASE_SECONDS = 0.25


class _RetryableArtifactPartError(Exception):
    pass


class WorkerBootstrapConfigurationError(Exception):
    """A redacted startup failure safe to emit before accepting work."""


class WorkerRuntimeSettings(BaseSettings):
    """Fail-closed settings for a single ephemeral GPU worker."""

    model_config = SettingsConfigDict(
        env_prefix="GEN_WORKER_",
        case_sensitive=False,
        extra="ignore",
        env_ignore_empty=True,
        frozen=True,
        hide_input_in_errors=True,
    )

    environment: WorkerEnvironment = WorkerEnvironment.PRODUCTION
    verification_keys: dict[str, str]
    allowed_upload_origin: str
    model_manifest_json: SecretStr
    model_manifest_sha256: Sha256
    runtime_admission_id: RuntimeAdmissionId
    runtime_worker_instance_id: RuntimeWorkerInstanceId = Field(
        validation_alias=AliasChoices("SALAD_INSTANCE_ID", "runtime_worker_instance_id")
    )
    checkpoint_root: Path = Path("/opt/comfyui/models/checkpoints")
    lora_root: Path = Path("/opt/comfyui/models/loras")
    detector_root: Path = Path("/opt/comfyui/models/ultralytics/bbox")
    artifact_bucket: str
    artifact_region: str = "us-east-1"
    artifact_endpoint_url: AnyHttpUrl | None = None
    artifact_access_key_id: SecretStr | None = None
    artifact_secret_access_key: SecretStr | None = None
    artifact_session_token: SecretStr | None = None
    artifact_connect_timeout_seconds: float = Field(default=10.0, ge=1.0, le=60.0)
    artifact_read_timeout_seconds: float = Field(default=120.0, ge=5.0, le=600.0)
    model_bootstrap_timeout_seconds: float = Field(
        default=3600.0,
        ge=60.0,
        le=7200.0,
    )
    max_body_bytes: int = Field(default=256 * 1024, ge=1024, le=1024 * 1024)
    max_signature_ttl_seconds: int = Field(default=7200, ge=5, le=7200)
    clock_skew_seconds: int = Field(default=15, ge=0, le=60)
    max_outputs: int = Field(default=MAX_OUTPUTS_PER_GENERATION_JOB, ge=1, le=32)
    max_output_bytes: int = Field(
        default=25 * 1024 * 1024,
        ge=1024,
        le=100 * 1024 * 1024,
    )
    max_total_output_bytes: int = Field(
        default=256 * 1024 * 1024,
        ge=1024,
        le=512 * 1024 * 1024,
    )
    max_image_dimension: int = Field(default=16_384, ge=64, le=65_536)
    max_image_pixels: int = Field(default=64_000_000, ge=4_096, le=256_000_000)
    max_replay_entries: int = Field(default=256, ge=1, le=1024)
    upload_timeout_seconds: float = Field(default=120.0, ge=1.0, le=600.0)
    readiness_timeout_seconds: float = Field(default=1.0, ge=0.05, le=5.0)
    approved_workflow_node_classes: frozenset[str] = Field(
        default=DEFAULT_APPROVED_WORKFLOW_NODE_CLASSES,
        min_length=1,
        max_length=128,
    )
    comfy_base_url: str = "http://127.0.0.1:8188"
    comfy_python: Path = Path("/opt/worker-venv/bin/python")
    comfy_main: Path = Path("/opt/comfyui/main.py")
    comfy_runtime_root: Path = Path("/opt/worker/runtime")
    comfy_execution_timeout_seconds: float = Field(default=3600.0, ge=30.0, le=7200.0)
    salad_queue_worker_enabled: bool = True
    salad_queue_worker_path: Path = Path("/usr/local/bin/salad-http-job-queue-worker")
    # WARN exposes only job IDs, HTTP status/errors, and fixed reallocation
    # reasons in the pinned queue worker. It is the minimum level that records
    # whether a provider retry followed a local 5xx or a queue-worker failure.
    salad_queue_worker_log_level: str = "warn"
    worker_host: str = _WORKER_BIND_HOST
    worker_port: int = Field(default=8000, ge=1024, le=65535)
    worker_log_level: str = "info"

    @model_validator(mode="after")
    def validate_runtime_boundary(self) -> "WorkerRuntimeSettings":
        errors: list[str] = []
        if _BUCKET_PATTERN.fullmatch(self.artifact_bucket) is None:
            errors.append("invalid artifact bucket")
        if not self.artifact_region or len(self.artifact_region) > 128:
            errors.append("invalid artifact region")
        if bool(self.artifact_access_key_id) != bool(self.artifact_secret_access_key):
            errors.append("artifact access key ID and secret must be provided together")
        if self.artifact_session_token is not None and self.artifact_access_key_id is None:
            errors.append("artifact session token requires an explicit access key")
        if (
            self.environment == WorkerEnvironment.PRODUCTION
            and self.artifact_endpoint_url is not None
            and not str(self.artifact_endpoint_url).startswith("https://")
        ):
            errors.append("production artifact storage requires HTTPS")
        if self.environment == WorkerEnvironment.PRODUCTION and self.artifact_access_key_id is None:
            errors.append("production artifact storage requires an explicit read-only identity")
        if any(
            not _is_container_absolute_path(path)
            for path in (
                self.checkpoint_root,
                self.diffusion_model_root,
                self.lora_root,
                self.text_encoder_root,
                self.vae_root,
                self.detector_root,
            )
        ):
            errors.append("model roots must be absolute")
        elif (
            self.checkpoint_root.parent != self.lora_root.parent
            or self.checkpoint_root.name != "checkpoints"
            or self.diffusion_model_root != self.checkpoint_root.parent / "diffusion_models"
            or self.lora_root.name != "loras"
            or self.text_encoder_root != self.checkpoint_root.parent / "text_encoders"
            or self.vae_root != self.checkpoint_root.parent / "vae"
            or self.detector_root != self.checkpoint_root.parent / "ultralytics" / "bbox"
        ):
            errors.append("model roots must use the fixed ComfyUI model directories")
        if (
            not _is_container_absolute_path(self.comfy_python)
            or not _is_container_absolute_path(self.comfy_main)
            or not _is_container_absolute_path(self.comfy_runtime_root)
            or self.comfy_python == self.comfy_main
            or self.comfy_runtime_root
            in {
                self.checkpoint_root,
                self.diffusion_model_root,
                self.lora_root,
                self.text_encoder_root,
                self.vae_root,
                self.detector_root,
            }
        ):
            errors.append("Comfy executable paths must be distinct and absolute")
        if self.worker_host not in {_WORKER_BIND_HOST, "::"}:
            errors.append("worker host must bind all container interfaces")
        normalized_log_level = self.worker_log_level.lower()
        if normalized_log_level not in _LOG_LEVELS:
            errors.append("invalid worker log level")
        if self.salad_queue_worker_enabled and not _is_container_absolute_path(
            self.salad_queue_worker_path
        ):
            errors.append("Salad queue worker path must be absolute")
        if self.salad_queue_worker_log_level.lower() not in {
            "debug",
            "info",
            "warn",
            "error",
        }:
            errors.append("invalid Salad queue worker log level")
        if errors:
            raise ValueError("; ".join(errors))

        # Reuse the request-boundary model so environment parsing cannot drift
        # from the worker application's security limits.
        self.to_worker_settings()
        return self

    def to_worker_settings(self) -> WorkerSettings:
        return WorkerSettings(
            environment=self.environment,
            verification_keys=self.verification_keys,
            allowed_upload_origin=self.allowed_upload_origin,
            artifact_manifest_sha256=self.model_manifest_sha256,
            runtime_admission_id=self.runtime_admission_id,
            runtime_worker_instance_id=self.runtime_worker_instance_id,
            max_body_bytes=self.max_body_bytes,
            max_signature_ttl_seconds=self.max_signature_ttl_seconds,
            clock_skew_seconds=self.clock_skew_seconds,
            max_outputs=self.max_outputs,
            max_output_bytes=self.max_output_bytes,
            max_total_output_bytes=self.max_total_output_bytes,
            max_image_dimension=self.max_image_dimension,
            max_image_pixels=self.max_image_pixels,
            max_replay_entries=self.max_replay_entries,
            upload_timeout_seconds=self.upload_timeout_seconds,
            readiness_timeout_seconds=self.readiness_timeout_seconds,
            approved_workflow_node_classes=self.approved_workflow_node_classes,
        )

    @property
    def diffusion_model_root(self) -> Path:
        return self.checkpoint_root.parent / "diffusion_models"

    @property
    def text_encoder_root(self) -> Path:
        return self.checkpoint_root.parent / "text_encoders"

    @property
    def vae_root(self) -> Path:
        return self.checkpoint_root.parent / "vae"


def _is_container_absolute_path(path: Path) -> bool:
    normalized = path.as_posix()
    return (
        normalized.startswith("/")
        and "\x00" not in normalized
        and all(part not in {"", ".", ".."} for part in normalized.split("/")[1:])
    )


def _reject_json_constant(_value: str) -> NoReturn:
    raise ValueError("invalid JSON constant")


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON property")
        result[key] = value
    return result


def _validate_json_shape(
    value: object,
    *,
    depth: int = 0,
    item_count: list[int] | None = None,
) -> None:
    if depth > MAX_MANIFEST_DEPTH:
        raise ValueError("manifest is too deeply nested")
    count = item_count if item_count is not None else [0]
    count[0] += 1
    if count[0] > MAX_MANIFEST_ITEMS:
        raise ValueError("manifest has too many items")
    if value is None or isinstance(value, (bool, str, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("manifest has a non-finite number")
        return
    if isinstance(value, list):
        for item in value:
            _validate_json_shape(item, depth=depth + 1, item_count=count)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("manifest has an invalid key")
            _validate_json_shape(item, depth=depth + 1, item_count=count)
        return
    raise ValueError("manifest is not JSON")


def load_artifact_manifest(raw_manifest: str) -> ArtifactManifest:
    """Parse a bounded manifest without exposing its content in an error."""

    try:
        encoded = raw_manifest.encode("utf-8")
        if not encoded or len(encoded) > MAX_MANIFEST_BYTES:
            raise ValueError("manifest size is invalid")
        parsed = json.loads(
            raw_manifest,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
        _validate_json_shape(parsed)
        normalized = json.dumps(
            parsed,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return ArtifactManifest.model_validate_json(normalized, strict=True)
    except (
        UnicodeEncodeError,
        json.JSONDecodeError,
        RecursionError,
        TypeError,
        ValueError,
        ValidationError,
    ):
        raise WorkerBootstrapConfigurationError(
            "worker bootstrap configuration is invalid"
        ) from None


def ensure_model_roots(
    checkpoint_root: Path,
    lora_root: Path,
    detector_root: Path | None = None,
    diffusion_model_root: Path | None = None,
    text_encoder_root: Path | None = None,
    vae_root: Path | None = None,
) -> None:
    """Create expected model directories and reject links/non-directories."""

    roots = tuple(
        path
        for path in (
            checkpoint_root,
            diffusion_model_root,
            lora_root,
            text_encoder_root,
            vae_root,
            detector_root,
        )
        if path is not None
    )
    for path in roots:
        try:
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
            metadata = path.lstat()
        except OSError:
            raise WorkerBootstrapConfigurationError(
                "worker bootstrap configuration is invalid"
            ) from None
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise WorkerBootstrapConfigurationError("worker bootstrap configuration is invalid")
    try:
        resolved = [path.resolve(strict=True) for path in roots]
        if len(set(resolved)) != len(resolved):
            raise WorkerBootstrapConfigurationError("worker bootstrap configuration is invalid")
    except (OSError, RuntimeError):
        raise WorkerBootstrapConfigurationError(
            "worker bootstrap configuration is invalid"
        ) from None


class S3ArtifactDownloader(ArtifactDownloader):
    """Read-only, streaming adapter for immutable manifest object identifiers."""

    def __init__(self, *, client: Any, bucket: str) -> None:
        if _BUCKET_PATTERN.fullmatch(bucket) is None:
            raise WorkerBootstrapConfigurationError("worker bootstrap configuration is invalid")
        self._client = client
        self._bucket = bucket
        self._closed = False

    async def stream(self, artifact: ModelArtifactSpec) -> AsyncIterator[bytes]:
        if self._closed or artifact.source_object_id is None or artifact.downloader_key is not None:
            raise ArtifactBootstrapError("artifact bootstrap failed")

        body: Any | None = None
        try:
            parameters = {
                "Bucket": self._bucket,
                "Key": artifact.source_object_id,
            }
            if artifact.source_object_version_id is not None:
                parameters["VersionId"] = artifact.source_object_version_id
            response = await to_thread.run_sync(partial(self._client.get_object, **parameters))
            if not isinstance(response, Mapping):
                raise ArtifactBootstrapError("artifact bootstrap failed")
            content_length = response.get("ContentLength")
            response_version_id = response.get("VersionId")
            body = response.get("Body")
            if (
                isinstance(content_length, bool)
                or not isinstance(content_length, int)
                or content_length != artifact.exact_size_bytes
                or (
                    artifact.source_object_version_id is not None
                    and response_version_id != artifact.source_object_version_id
                )
                or body is None
                or not hasattr(body, "read")
            ):
                raise ArtifactBootstrapError("artifact bootstrap failed")

            total = 0
            while True:
                chunk = await to_thread.run_sync(body.read, STREAM_READ_BYTES)
                if not chunk:
                    break
                if not isinstance(chunk, bytes):
                    raise ArtifactBootstrapError("artifact bootstrap failed")
                total += len(chunk)
                if total > artifact.exact_size_bytes:
                    raise ArtifactBootstrapError("artifact bootstrap failed")
                yield chunk
            if total != artifact.exact_size_bytes:
                raise ArtifactBootstrapError("artifact bootstrap failed")
        except ArtifactBootstrapError:
            raise
        except (BotoCoreError, ClientError, OSError):
            raise ArtifactBootstrapError("artifact bootstrap failed") from None
        except Exception:
            raise ArtifactBootstrapError("artifact bootstrap failed") from None
        finally:
            if body is not None and hasattr(body, "close"):
                with suppress(Exception):
                    await to_thread.run_sync(body.close)

    async def ranged_stream(self, artifact: ModelArtifactSpec) -> AsyncIterator[bytes]:
        """Yield a pinned large object in order using bounded parallel range GETs."""

        if (
            self._closed
            or artifact.source_object_id is None
            or artifact.source_object_version_id is None
            or artifact.downloader_key is not None
        ):
            raise ArtifactBootstrapError("artifact bootstrap failed")

        ranges = iter(
            (start, min(start + MULTIPART_ARTIFACT_PART_BYTES, artifact.exact_size_bytes) - 1)
            for start in range(
                0,
                artifact.exact_size_bytes,
                MULTIPART_ARTIFACT_PART_BYTES,
            )
        )
        pending: deque[asyncio.Task[tuple[bytes, ...]]] = deque()

        def schedule_next() -> bool:
            try:
                start, end = next(ranges)
            except StopIteration:
                return False
            pending.append(asyncio.create_task(self._download_range(artifact, start, end)))
            return True

        for _ in range(MAX_PARALLEL_ARTIFACT_PARTS):
            if not schedule_next():
                break

        try:
            while pending:
                chunks = await pending.popleft()
                schedule_next()
                for chunk in chunks:
                    yield chunk
        finally:
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

    async def _download_range(
        self,
        artifact: ModelArtifactSpec,
        start: int,
        end: int,
    ) -> tuple[bytes, ...]:
        for attempt in range(MAX_ARTIFACT_PART_DOWNLOAD_ATTEMPTS):
            try:
                return await self._download_range_once(artifact, start, end)
            except _RetryableArtifactPartError:
                if attempt + 1 >= MAX_ARTIFACT_PART_DOWNLOAD_ATTEMPTS:
                    raise ArtifactBootstrapError("artifact bootstrap failed") from None
                await asyncio.sleep(_ARTIFACT_PART_RETRY_BASE_SECONDS * (2**attempt))
        raise ArtifactBootstrapError("artifact bootstrap failed")

    async def _download_range_once(
        self,
        artifact: ModelArtifactSpec,
        start: int,
        end: int,
    ) -> tuple[bytes, ...]:
        body: Any | None = None
        expected_size = end - start + 1
        try:
            parameters = {
                "Bucket": self._bucket,
                "Key": artifact.source_object_id,
                "VersionId": artifact.source_object_version_id,
                "Range": f"bytes={start}-{end}",
            }
            response = await to_thread.run_sync(partial(self._client.get_object, **parameters))
            if not isinstance(response, Mapping):
                raise ArtifactBootstrapError("artifact bootstrap failed")
            content_length = response.get("ContentLength")
            response_version_id = response.get("VersionId")
            content_range = response.get("ContentRange")
            body = response.get("Body")
            if (
                isinstance(content_length, bool)
                or not isinstance(content_length, int)
                or content_length != expected_size
                or response_version_id != artifact.source_object_version_id
                or content_range != f"bytes {start}-{end}/{artifact.exact_size_bytes}"
                or body is None
                or not hasattr(body, "read")
            ):
                raise ArtifactBootstrapError("artifact bootstrap failed")

            chunks: list[bytes] = []
            total_size = 0
            while total_size < expected_size:
                chunk = await to_thread.run_sync(
                    body.read,
                    min(STREAM_READ_BYTES, expected_size - total_size),
                )
                if not chunk:
                    raise _RetryableArtifactPartError
                if not isinstance(chunk, bytes) or total_size + len(chunk) > expected_size:
                    raise ArtifactBootstrapError("artifact bootstrap failed")
                chunks.append(chunk)
                total_size += len(chunk)
            extra = await to_thread.run_sync(body.read, 1)
            if extra:
                raise ArtifactBootstrapError("artifact bootstrap failed")
            return tuple(chunks)
        except ArtifactBootstrapError:
            raise
        except _RetryableArtifactPartError:
            raise
        except (BotoCoreError, ClientError, OSError):
            raise _RetryableArtifactPartError from None
        except Exception:
            raise _RetryableArtifactPartError from None
        finally:
            if body is not None and hasattr(body, "close"):
                with suppress(Exception):
                    await to_thread.run_sync(body.close)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        close = getattr(self._client, "close", None)
        if callable(close):
            with suppress(Exception):
                await to_thread.run_sync(close)


def build_artifact_downloader(settings: WorkerRuntimeSettings) -> S3ArtifactDownloader:
    client_arguments: dict[str, Any] = {
        "service_name": "s3",
        "region_name": settings.artifact_region,
        "config": Config(
            signature_version="s3v4",
            retries={"mode": "standard", "max_attempts": 4},
            connect_timeout=settings.artifact_connect_timeout_seconds,
            read_timeout=settings.artifact_read_timeout_seconds,
        ),
    }
    if settings.artifact_endpoint_url is not None:
        client_arguments["endpoint_url"] = str(settings.artifact_endpoint_url)
    if settings.artifact_access_key_id is not None:
        client_arguments["aws_access_key_id"] = settings.artifact_access_key_id.get_secret_value()
        client_arguments["aws_secret_access_key"] = (
            settings.artifact_secret_access_key.get_secret_value()
            if settings.artifact_secret_access_key is not None
            else ""
        )
    if settings.artifact_session_token is not None:
        client_arguments["aws_session_token"] = settings.artifact_session_token.get_secret_value()
    try:
        client = boto3.client(**client_arguments)
    except (BotoCoreError, ClientError, ValueError):
        raise WorkerBootstrapConfigurationError(
            "worker bootstrap configuration is invalid"
        ) from None
    return S3ArtifactDownloader(client=client, bucket=settings.artifact_bucket)


def scrub_artifact_credentials(
    environ: MutableMapping[str, str] = os.environ,
) -> None:
    """Remove all ambient object-store credential sources before Comfy starts."""

    for name in _ARTIFACT_CREDENTIAL_ENV_NAMES:
        environ.pop(name, None)
