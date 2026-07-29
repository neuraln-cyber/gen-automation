"""One-shot, fail-closed process isolation for untrusted quality analysis."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import math
import multiprocessing
import os
import pickle
import sys
from collections.abc import Mapping, MutableMapping
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any, Protocol, cast

from gen_automation.quality import (
    DEFAULT_QUALITY_CONFIG,
    SCORER_VERSION,
    NormalizedThumbnail,
    QualityConfig,
    QualityMetrics,
    QualityResult,
    QualityScoreBreakdown,
    UnsafeImageError,
    analyze_image,
    quality_config_sha256,
)

if TYPE_CHECKING:
    from multiprocessing.connection import Connection

_MEBIBYTE = 1024 * 1024
_REQUEST_ALLOWANCE_BYTES = 256 * 1024
_MAXIMUM_RESPONSE_BYTES = 512 * 1024
_POLL_INTERVAL_SECONDS = 0.05


class QualityIsolationError(RuntimeError):
    """Base error for the quality-analysis process boundary."""


class QualityIsolationUnavailableError(QualityIsolationError):
    """The required Linux process and resource limits are unavailable."""


class QualityIsolationTimeoutError(QualityIsolationError, TimeoutError):
    """The one-shot analyzer exceeded its wall-time limit."""


class QualityIsolationMemoryError(QualityIsolationError):
    """The one-shot analyzer exhausted its hard memory allowance."""


class QualityIsolationCrashError(QualityIsolationError):
    """The analyzer exited without a valid response."""


class QualityIsolationProtocolError(QualityIsolationError):
    """The analyzer returned malformed or over-budget IPC data."""


@dataclass(frozen=True, slots=True)
class QualityIsolationPolicy:
    wall_timeout_seconds: float = 45.0
    memory_limit_bytes: int = 768 * _MEBIBYTE
    termination_grace_seconds: float = 1.0

    def __post_init__(self) -> None:
        _bounded_real(
            self.wall_timeout_seconds,
            label="quality wall timeout",
            minimum=1.0,
            maximum=5 * 60.0,
        )
        if (
            isinstance(self.memory_limit_bytes, bool)
            or not isinstance(self.memory_limit_bytes, int)
            or not 256 * _MEBIBYTE <= self.memory_limit_bytes <= 4 * 1024 * _MEBIBYTE
        ):
            raise ValueError("quality memory limit must be between 256 MiB and 4 GiB")
        _bounded_real(
            self.termination_grace_seconds,
            label="quality termination grace",
            minimum=0.05,
            maximum=5.0,
        )


class _ChildProcess(Protocol):
    exitcode: int | None

    def is_alive(self) -> bool: ...

    def join(self, timeout: float | None = None) -> None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    def close(self) -> None: ...


class _ReceiveConnection(Protocol):
    def poll(self, timeout: float = 0.0) -> bool: ...

    def recv_bytes(self, maxlength: int | None = None) -> bytes: ...

    def close(self) -> None: ...


async def analyze_image_isolated(
    data: bytes,
    *,
    config: QualityConfig = DEFAULT_QUALITY_CONFIG,
    policy: QualityIsolationPolicy | None = None,
) -> QualityResult:
    """Analyze one bounded image in a new hard-limited Linux process."""

    selected_policy = policy or QualityIsolationPolicy()
    if not isinstance(config, QualityConfig):
        raise ValueError("quality configuration is invalid")
    if not isinstance(selected_policy, QualityIsolationPolicy):
        raise ValueError("quality isolation policy is invalid")
    _assert_production_isolation_available()
    payload = _bounded_payload(data, maximum=config.max_input_bytes)
    request = pickle.dumps((payload, config), protocol=5)
    if len(request) > config.max_input_bytes + _REQUEST_ALLOWANCE_BYTES:
        raise QualityIsolationProtocolError("quality request exceeds its IPC byte limit")

    try:
        context = multiprocessing.get_context("spawn")
        receive_connection, send_connection = context.Pipe(duplex=False)
        try:
            process = context.Process(
                target=_isolated_quality_child,
                args=(send_connection, request, selected_policy.memory_limit_bytes),
                name="quality-analyze-once",
                daemon=True,
            )
        except BaseException:
            receive_connection.close()
            send_connection.close()
            raise
    except (OSError, RuntimeError):
        raise QualityIsolationUnavailableError(
            "isolated quality-analysis resources could not be created"
        ) from None

    started = False
    response_received = False
    try:
        try:
            process.start()
            started = True
        except (OSError, RuntimeError):
            raise QualityIsolationUnavailableError(
                "isolated quality-analysis process could not be started"
            ) from None
        finally:
            send_connection.close()
        response = await _receive_child_message(
            cast(_ChildProcess, process),
            cast(_ReceiveConnection, receive_connection),
            timeout_seconds=selected_policy.wall_timeout_seconds,
        )
        response_received = True
        return _decode_child_response(
            response,
            expected_sha256=hashlib.sha256(payload).hexdigest(),
            expected_byte_size=len(payload),
            expected_config=config,
        )
    finally:
        receive_connection.close()
        if started:
            await asyncio.to_thread(
                _cleanup_process,
                cast(_ChildProcess, process),
                selected_policy.termination_grace_seconds,
                terminate_immediately=not response_received,
            )


async def _receive_child_message(
    process: _ChildProcess,
    connection: _ReceiveConnection,
    *,
    timeout_seconds: float,
) -> bytes:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise QualityIsolationTimeoutError(
                "isolated quality analysis exceeded its wall-time limit"
            )
        try:
            ready = await asyncio.wait_for(
                asyncio.to_thread(
                    connection.poll,
                    min(_POLL_INTERVAL_SECONDS, remaining),
                ),
                timeout=remaining,
            )
        except TimeoutError:
            raise QualityIsolationTimeoutError(
                "isolated quality analysis exceeded its wall-time limit"
            ) from None
        except (EOFError, OSError):
            raise QualityIsolationCrashError(
                "isolated quality-analysis IPC failed before a response"
            ) from None
        if ready:
            try:
                return await asyncio.wait_for(
                    asyncio.to_thread(
                        connection.recv_bytes,
                        _MAXIMUM_RESPONSE_BYTES,
                    ),
                    timeout=max(0.001, deadline - loop.time()),
                )
            except TimeoutError:
                raise QualityIsolationTimeoutError(
                    "isolated quality analysis exceeded its wall-time limit"
                ) from None
            except OSError:
                raise QualityIsolationProtocolError(
                    "isolated quality response exceeded its IPC byte limit"
                ) from None
            except EOFError:
                raise QualityIsolationCrashError(
                    "isolated quality analyzer closed IPC without a response"
                ) from None
        if not process.is_alive():
            if process.exitcode in {-9, 9, -11, 11}:
                raise QualityIsolationMemoryError(
                    "isolated quality analyzer exceeded its resource allowance"
                )
            raise QualityIsolationCrashError("isolated quality analyzer exited without a response")


def _cleanup_process(
    process: _ChildProcess,
    grace_seconds: float,
    *,
    terminate_immediately: bool = False,
) -> None:
    try:
        if terminate_immediately and process.is_alive():
            process.terminate()
            process.join(grace_seconds)
        else:
            process.join(grace_seconds)
            if process.is_alive():
                process.terminate()
                process.join(grace_seconds)
        if process.is_alive():
            process.kill()
            process.join(grace_seconds)
    finally:
        process.close()


def _assert_production_isolation_available() -> None:
    if not sys.platform.startswith("linux"):
        raise QualityIsolationUnavailableError(
            "hard-limited quality isolation is supported only on Linux"
        )
    try:
        import resource
    except ImportError:
        raise QualityIsolationUnavailableError(
            "Linux resource-limit support is unavailable"
        ) from None
    if not all(hasattr(resource, name) for name in ("RLIMIT_AS", "RLIMIT_RSS")):
        raise QualityIsolationUnavailableError(
            "required Linux address-space/RSS limits are unavailable"
        )


def _apply_linux_hard_limits(
    memory_limit_bytes: int,
    *,
    resource_module: Any | None = None,
    platform: str | None = None,
) -> None:
    selected_platform = sys.platform if platform is None else platform
    if not selected_platform.startswith("linux"):
        raise QualityIsolationUnavailableError(
            "hard-limited quality isolation is supported only on Linux"
        )
    if resource_module is None:
        try:
            import resource
        except ImportError:
            raise QualityIsolationUnavailableError(
                "Linux resource-limit support is unavailable"
            ) from None
        resource_api: Any = resource
    else:
        resource_api = resource_module

    for name in ("RLIMIT_AS", "RLIMIT_RSS"):
        if not hasattr(resource_api, name):
            raise QualityIsolationUnavailableError(
                "required Linux address-space/RSS limits are unavailable"
            )
        kind = cast(int, getattr(resource_api, name))
        try:
            _, current_hard = resource_api.getrlimit(kind)
            if (
                current_hard != resource_api.RLIM_INFINITY
                and int(current_hard) < memory_limit_bytes
            ):
                raise ValueError
            resource_api.setrlimit(kind, (memory_limit_bytes, memory_limit_bytes))
            applied_soft, applied_hard = resource_api.getrlimit(kind)
        except (OSError, TypeError, ValueError):
            raise QualityIsolationUnavailableError(
                "required Linux memory limits could not be installed"
            ) from None
        if applied_soft != memory_limit_bytes or applied_hard != memory_limit_bytes:
            raise QualityIsolationUnavailableError(
                "required Linux memory limits could not be verified"
            )
    if hasattr(resource_api, "RLIMIT_CORE"):
        try:
            resource_api.setrlimit(resource_api.RLIMIT_CORE, (0, 0))
            core_soft, core_hard = resource_api.getrlimit(resource_api.RLIMIT_CORE)
        except (OSError, TypeError, ValueError):
            raise QualityIsolationUnavailableError(
                "quality analyzer core-dump limit could not be installed"
            ) from None
        if core_soft != 0 or core_hard != 0:
            raise QualityIsolationUnavailableError(
                "quality analyzer core-dump limit could not be verified"
            )


def _scrub_child_environment(
    environment: MutableMapping[str, str] | None = None,
) -> None:
    selected_environment = os.environ if environment is None else environment
    try:
        selected_environment.clear()
    except Exception:
        raise QualityIsolationUnavailableError(
            "quality analyzer environment could not be scrubbed"
        ) from None
    if selected_environment:
        raise QualityIsolationUnavailableError("quality analyzer environment could not be verified")


def _isolated_quality_child(
    send_connection: Connection,
    request_payload: bytes,
    memory_limit_bytes: int,
) -> None:
    try:
        try:
            _apply_linux_hard_limits(memory_limit_bytes)
            _scrub_child_environment()
        except QualityIsolationUnavailableError:
            _send_child_message(
                send_connection,
                _error_message("isolation_unavailable"),
            )
            return
        try:
            request = pickle.loads(request_payload)  # noqa: S301 - trusted parent request
            if (
                not isinstance(request, tuple)
                or len(request) != 2
                or not isinstance(request[0], bytes)
                or not isinstance(request[1], QualityConfig)
            ):
                raise QualityIsolationProtocolError("quality request is invalid")
            result = analyze_image(request[0], config=request[1])
            response = _success_message(result)
        except UnsafeImageError:
            response = _error_message("corrupt")
        except MemoryError:
            response = _error_message("memory")
        except QualityIsolationProtocolError:
            response = _error_message("protocol")
        except BaseException:
            response = _error_message("internal")
        _send_child_message(send_connection, response)
    finally:
        send_connection.close()


def _send_child_message(connection: Connection, payload: bytes) -> None:
    try:
        connection.send_bytes(payload)
    except (BrokenPipeError, EOFError, OSError):
        return


def _success_message(result: QualityResult) -> bytes:
    return _json_message(
        {
            "ok": True,
            "result": {
                "sha256": result.sha256,
                "byte_size": result.byte_size,
                "width": result.width,
                "height": result.height,
                "image_format": result.image_format,
                "thumbnail": {
                    "width": result.thumbnail.width,
                    "height": result.thumbnail.height,
                    "luminance": base64.b64encode(result.thumbnail.luminance).decode("ascii"),
                },
                "metrics": asdict(result.metrics),
                "dhash64": result.dhash64,
                "score_micros": result.score_micros,
                "score_breakdown": asdict(result.score_breakdown),
                "config": asdict(result.config),
                "config_sha256": result.config_sha256,
                "scorer_version": result.scorer_version,
                "pillow_version": result.pillow_version,
            },
        }
    )


def _error_message(kind: str) -> bytes:
    return _json_message({"ok": False, "error": {"kind": kind}})


def _json_message(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def _decode_child_response(
    payload: bytes,
    *,
    expected_sha256: str,
    expected_byte_size: int,
    expected_config: QualityConfig,
) -> QualityResult:
    try:
        decoded = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise QualityIsolationProtocolError(
            "isolated quality analyzer returned malformed IPC data"
        ) from None
    root = _mapping(decoded)
    if not isinstance(root.get("ok"), bool):
        raise QualityIsolationProtocolError("isolated quality analyzer returned malformed IPC data")
    if root["ok"] is False:
        kind = _text(_mapping(root.get("error")).get("kind"), maximum=64)
        if kind == "corrupt":
            raise UnsafeImageError("image data is malformed or unsafe")
        if kind == "memory":
            raise QualityIsolationMemoryError(
                "isolated quality analyzer exceeded its resource allowance"
            )
        if kind == "isolation_unavailable":
            raise QualityIsolationUnavailableError(
                "isolated quality analyzer could not install hard limits"
            )
        if kind == "protocol":
            raise QualityIsolationProtocolError(
                "isolated quality analyzer rejected its request contract"
            )
        raise QualityIsolationCrashError("isolated quality analysis failed")

    wire = _mapping(root.get("result"))
    thumbnail_wire = _mapping(wire.get("thumbnail"))
    try:
        luminance = base64.b64decode(
            _text(thumbnail_wire.get("luminance"), maximum=128 * 1024),
            validate=True,
        )
    except (binascii.Error, ValueError):
        raise QualityIsolationProtocolError(
            "isolated quality analyzer returned malformed thumbnail data"
        ) from None
    config = _dataclass_from_mapping(QualityConfig, wire.get("config"))
    metrics = _dataclass_from_mapping(QualityMetrics, wire.get("metrics"))
    breakdown = _dataclass_from_mapping(
        QualityScoreBreakdown,
        wire.get("score_breakdown"),
    )
    try:
        result = QualityResult(
            sha256=_sha256(wire.get("sha256")),
            byte_size=_integer(
                wire.get("byte_size"),
                minimum=1,
                maximum=expected_config.max_input_bytes,
            ),
            width=_integer(wire.get("width"), minimum=1, maximum=expected_config.max_width),
            height=_integer(wire.get("height"), minimum=1, maximum=expected_config.max_height),
            image_format=_text(wire.get("image_format"), maximum=20),
            thumbnail=NormalizedThumbnail(
                width=_integer(
                    thumbnail_wire.get("width"),
                    minimum=1,
                    maximum=expected_config.thumbnail_size,
                ),
                height=_integer(
                    thumbnail_wire.get("height"),
                    minimum=1,
                    maximum=expected_config.thumbnail_size,
                ),
                luminance=luminance,
            ),
            metrics=metrics,
            dhash64=_integer(wire.get("dhash64"), minimum=0, maximum=(1 << 64) - 1),
            score_micros=_integer(wire.get("score_micros"), minimum=0, maximum=1_000_000),
            score_breakdown=breakdown,
            config=config,
            config_sha256=_sha256(wire.get("config_sha256")),
            scorer_version=_text(wire.get("scorer_version"), maximum=100),
            pillow_version=_text(wire.get("pillow_version"), maximum=50),
        )
    except (TypeError, ValueError):
        raise QualityIsolationProtocolError(
            "isolated quality analyzer returned invalid result metadata"
        ) from None
    if (
        result.sha256 != expected_sha256
        or result.byte_size != expected_byte_size
        or result.config != expected_config
        or result.config_sha256 != quality_config_sha256(expected_config)
        or result.scorer_version != SCORER_VERSION
        or result.thumbnail.width != expected_config.thumbnail_size
        or result.thumbnail.height != expected_config.thumbnail_size
        or len(result.thumbnail.luminance) != expected_config.thumbnail_size**2
        or result.width * result.height > expected_config.max_pixels
        or result.score_breakdown.total_micros != result.score_micros
    ):
        raise QualityIsolationProtocolError(
            "isolated quality result conflicts with the request contract"
        )
    return result


def _dataclass_from_mapping(dataclass_type: Any, value: object) -> Any:
    mapping = _mapping(value)
    try:
        return dataclass_type(**mapping)
    except (TypeError, ValueError):
        raise QualityIsolationProtocolError(
            "isolated quality analyzer returned invalid result metadata"
        ) from None


def _mapping(value: object) -> Mapping[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise QualityIsolationProtocolError("isolated quality analyzer returned malformed IPC data")
    return cast(Mapping[str, Any], value)


def _text(value: object, *, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise QualityIsolationProtocolError(
            "isolated quality analyzer returned malformed text metadata"
        )
    return value


def _sha256(value: object) -> str:
    text = _text(value, maximum=64)
    if len(text) != 64:
        raise QualityIsolationProtocolError(
            "isolated quality analyzer returned malformed checksum metadata"
        )
    try:
        int(text, 16)
    except ValueError:
        raise QualityIsolationProtocolError(
            "isolated quality analyzer returned malformed checksum metadata"
        ) from None
    return text


def _integer(value: object, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise QualityIsolationProtocolError(
            "isolated quality analyzer returned malformed numeric metadata"
        )
    return value


def _bounded_payload(value: object, *, maximum: int) -> bytes:
    if not isinstance(value, bytes) or not value:
        raise UnsafeImageError("image input must be non-empty bytes")
    if len(value) > maximum:
        raise UnsafeImageError("image input exceeds the configured byte limit")
    return value


def _bounded_real(value: float, *, label: str, minimum: float, maximum: float) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not minimum <= value <= maximum
    ):
        raise ValueError(f"{label} must be between {minimum} and {maximum} seconds")
