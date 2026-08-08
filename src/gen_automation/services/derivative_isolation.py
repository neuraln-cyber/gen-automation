"""One-shot, fail-closed process isolation for untrusted image rendering."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import math
import multiprocessing
import pickle
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any, Protocol, cast

from gen_automation.domain.canonical import canonical_json_bytes, canonical_sha256

if TYPE_CHECKING:
    from multiprocessing.connection import Connection

    from gen_automation.services.derivatives import (
        DerivativeBundle,
        DerivativeRecipe,
        DerivativeSafetyLimits,
        ImageBytes,
    )

_MEBIBYTE = 1024 * 1024
_PROCESS_MEMORY_RESERVE_BYTES = 128 * _MEBIBYTE
_REQUEST_METADATA_ALLOWANCE_BYTES = 1 * _MEBIBYTE
_RESULT_METADATA_ALLOWANCE_BYTES = 2 * _MEBIBYTE
_POLL_INTERVAL_SECONDS = 0.05


class DerivativeIsolationError(RuntimeError):
    """Base error raised by the isolated rendering boundary."""


class DerivativeIsolationUnavailableError(DerivativeIsolationError):
    """Production-strength process isolation cannot be established."""


class DerivativeIsolationTimeoutError(DerivativeIsolationError, TimeoutError):
    """The one-shot renderer exceeded its hard wall-time budget."""


class DerivativeIsolationCrashError(DerivativeIsolationError):
    """The one-shot renderer exited without a valid response."""


class DerivativeIsolationProtocolError(DerivativeIsolationError):
    """The child returned malformed or over-budget IPC data."""


@dataclass(frozen=True, slots=True)
class DerivativeIsolationPolicy:
    wall_timeout_seconds: float = 120.0
    memory_limit_bytes: int = 512 * _MEBIBYTE
    termination_grace_seconds: float = 1.0

    def __post_init__(self) -> None:
        _bounded_real(
            self.wall_timeout_seconds,
            "renderer wall timeout",
            minimum=1.0,
            maximum=15 * 60.0,
        )
        if (
            isinstance(self.memory_limit_bytes, bool)
            or not isinstance(self.memory_limit_bytes, int)
            or not 256 * _MEBIBYTE <= self.memory_limit_bytes <= 8 * 1024 * _MEBIBYTE
        ):
            raise ValueError("renderer memory limit must be between 256 MiB and 8 GiB")
        _bounded_real(
            self.termination_grace_seconds,
            "renderer termination grace",
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


async def render_platform_derivatives_isolated(
    raw_master: ImageBytes,
    *,
    recipe: DerivativeRecipe,
    watermark_png: ImageBytes | None = None,
    targets: Sequence[str] | None = None,
    limits: DerivativeSafetyLimits | None = None,
    policy: DerivativeIsolationPolicy | None = None,
    renderer_version: str | None = None,
) -> DerivativeBundle:
    """Render once in a new, memory-limited Linux child process.

    The child is never reused. Pillow decoding and rendering happen only after
    that child has installed address-space and RSS limits. The async caller
    performs only bounded IPC/process coordination, never Pillow work.
    """

    from gen_automation.services import derivatives

    selected_limits = limits or derivatives.DEFAULT_DERIVATIVE_LIMITS
    selected_policy = policy or DerivativeIsolationPolicy()
    selected_renderer_version = renderer_version or derivatives.DERIVATIVE_RENDERER_VERSION
    if not isinstance(recipe, derivatives.DerivativeRecipe):
        raise derivatives.DerivativeRecipeError("derivative recipe is invalid")
    if not isinstance(selected_limits, derivatives.DerivativeSafetyLimits):
        raise derivatives.DerivativeRecipeError("derivative safety limits are invalid")
    if not isinstance(selected_policy, DerivativeIsolationPolicy):
        raise ValueError("derivative isolation policy is invalid")
    if selected_renderer_version not in derivatives.SUPPORTED_DERIVATIVE_RENDERER_VERSIONS:
        raise derivatives.DerivativeRecipeError("derivative renderer version is unsupported")
    if targets is None:
        target_values = tuple(target.value for target in derivatives.DerivativeTarget)
    else:
        if isinstance(targets, (str, bytes)) or not targets:
            raise derivatives.DerivativeRecipeError("render targets must be a non-empty sequence")
        try:
            target_values = tuple(derivatives.DerivativeTarget(target).value for target in targets)
        except ValueError:
            raise derivatives.DerivativeRecipeError("render target is invalid") from None
        if len(set(target_values)) != len(target_values):
            raise derivatives.DerivativeRecipeError("render targets must be unique")
    expected_target_values = tuple(
        target.value for target in derivatives.DerivativeTarget if target.value in target_values
    )
    _assert_production_isolation_available()
    required_memory = selected_limits.max_peak_working_set_bytes + _PROCESS_MEMORY_RESERVE_BYTES
    if selected_policy.memory_limit_bytes < required_memory:
        raise DerivativeIsolationUnavailableError(
            "renderer hard memory limit does not cover the configured working-set limit"
        )

    master_payload = _bounded_parent_bytes(
        raw_master,
        maximum=selected_limits.max_master_bytes,
        label="raw master",
    )
    teaser_requested = derivatives.DerivativeTarget.X_TEASER.value in target_values
    if watermark_png is not None and (recipe.watermark is None or not teaser_requested):
        raise derivatives.DerivativeRecipeError(
            "watermark bytes are accepted only for a watermarked X teaser"
        )
    if teaser_requested and recipe.watermark is not None and watermark_png is None:
        raise derivatives.DerivativeRecipeError("a watermarked X teaser requires watermark bytes")
    watermark_payload = (
        _bounded_parent_bytes(
            watermark_png,
            maximum=selected_limits.max_watermark_bytes,
            label="watermark",
        )
        if watermark_png is not None
        else None
    )
    recipe_payload = canonical_json_bytes(asdict(recipe))
    if len(recipe_payload) > selected_limits.max_recipe_bytes:
        raise derivatives.DerivativeRecipeError(
            "serialized derivative recipe exceeds the safety limit"
        )
    request_payload = pickle.dumps(
        (
            master_payload,
            recipe,
            watermark_payload,
            selected_limits,
            expected_target_values,
            selected_renderer_version,
        ),
        protocol=5,
    )
    maximum_request_bytes = (
        selected_limits.max_master_bytes
        + selected_limits.max_watermark_bytes
        + selected_limits.max_recipe_bytes
        + _REQUEST_METADATA_ALLOWANCE_BYTES
    )
    if len(request_payload) > maximum_request_bytes:
        raise DerivativeIsolationProtocolError(
            "isolated renderer request exceeds its IPC byte limit"
        )
    maximum_result_bytes = _maximum_result_message_bytes(selected_limits.max_output_bytes)

    try:
        context = multiprocessing.get_context("spawn")
        receive_connection, send_connection = context.Pipe(duplex=False)
        try:
            process = context.Process(
                target=_isolated_renderer_child,
                args=(
                    send_connection,
                    request_payload,
                    selected_policy.memory_limit_bytes,
                ),
                name="derivative-render-once",
                daemon=True,
            )
        except BaseException:
            receive_connection.close()
            send_connection.close()
            raise
    except (OSError, RuntimeError):
        raise DerivativeIsolationUnavailableError(
            "isolated renderer IPC/process resources could not be created"
        ) from None
    started = False
    response_received = False
    try:
        try:
            process.start()
            started = True
        except (OSError, RuntimeError):
            raise DerivativeIsolationUnavailableError(
                "isolated renderer process could not be started"
            ) from None
        finally:
            send_connection.close()

        response = await _receive_child_message(
            cast(_ChildProcess, process),
            cast(_ReceiveConnection, receive_connection),
            timeout_seconds=selected_policy.wall_timeout_seconds,
            maximum_result_bytes=maximum_result_bytes,
        )
        response_received = True
        return await asyncio.to_thread(
            _decode_child_response,
            response,
            selected_limits,
            expected_source_sha256=hashlib.sha256(master_payload).hexdigest(),
            expected_recipe=recipe,
            expected_target_values=expected_target_values,
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
    maximum_result_bytes: int,
) -> bytes:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise DerivativeIsolationTimeoutError(
                "isolated derivative rendering exceeded its wall-time limit"
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
            raise DerivativeIsolationTimeoutError(
                "isolated derivative rendering exceeded its wall-time limit"
            ) from None
        except (EOFError, OSError):
            raise DerivativeIsolationCrashError(
                "isolated renderer IPC failed before a response"
            ) from None
        if ready:
            receive_timeout = deadline - loop.time()
            if receive_timeout <= 0:
                raise DerivativeIsolationTimeoutError(
                    "isolated derivative rendering exceeded its wall-time limit"
                )
            try:
                return await asyncio.wait_for(
                    asyncio.to_thread(
                        connection.recv_bytes,
                        maximum_result_bytes,
                    ),
                    timeout=receive_timeout,
                )
            except TimeoutError:
                raise DerivativeIsolationTimeoutError(
                    "isolated derivative rendering exceeded its wall-time limit"
                ) from None
            except OSError:
                raise DerivativeIsolationProtocolError(
                    "isolated renderer response exceeded its IPC byte limit"
                ) from None
            except EOFError:
                raise DerivativeIsolationCrashError(
                    "isolated renderer closed its IPC channel without a response"
                ) from None
        if not process.is_alive():
            raise DerivativeIsolationCrashError(
                f"isolated renderer exited without a response (exit code {process.exitcode})"
            )


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
        raise DerivativeIsolationUnavailableError(
            "hard-limited derivative isolation is supported only on Linux"
        )
    try:
        import resource
    except ImportError:
        raise DerivativeIsolationUnavailableError(
            "Linux resource-limit support is unavailable"
        ) from None
    if not all(hasattr(resource, name) for name in ("RLIMIT_AS", "RLIMIT_RSS")):
        raise DerivativeIsolationUnavailableError(
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
        raise DerivativeIsolationUnavailableError(
            "hard-limited derivative isolation is supported only on Linux"
        )
    if resource_module is None:
        try:
            import resource
        except ImportError:
            raise DerivativeIsolationUnavailableError(
                "Linux resource-limit support is unavailable"
            ) from None
        resource_api: Any = resource
    else:
        resource_api = resource_module

    for name in ("RLIMIT_AS", "RLIMIT_RSS"):
        if not hasattr(resource_api, name):
            raise DerivativeIsolationUnavailableError(
                "required Linux address-space/RSS limits are unavailable"
            )
        resource_kind = cast(int, getattr(resource_api, name))
        try:
            _, current_hard = resource_api.getrlimit(resource_kind)
            infinity = resource_api.RLIM_INFINITY
            if current_hard != infinity and int(current_hard) < memory_limit_bytes:
                raise ValueError
            effective_limit = memory_limit_bytes
            resource_api.setrlimit(
                resource_kind,
                (effective_limit, effective_limit),
            )
            applied_soft, applied_hard = resource_api.getrlimit(resource_kind)
        except (OSError, TypeError, ValueError):
            raise DerivativeIsolationUnavailableError(
                "required Linux memory limits could not be installed"
            ) from None
        if applied_soft != effective_limit or applied_hard != effective_limit:
            raise DerivativeIsolationUnavailableError(
                "required Linux memory limits could not be verified"
            )

    if hasattr(resource_api, "RLIMIT_CORE"):
        try:
            resource_api.setrlimit(resource_api.RLIMIT_CORE, (0, 0))
        except (OSError, TypeError, ValueError):
            raise DerivativeIsolationUnavailableError(
                "renderer core-dump limit could not be installed"
            ) from None


def _isolated_renderer_child(
    send_connection: Connection,
    request_payload: bytes,
    memory_limit_bytes: int,
) -> None:
    try:
        try:
            _apply_linux_hard_limits(memory_limit_bytes)
        except DerivativeIsolationUnavailableError:
            _send_child_message(
                send_connection,
                _error_message(
                    "isolation_unavailable",
                    "isolated renderer could not install required hard memory limits",
                ),
            )
            return

        from gen_automation.services import derivatives

        try:
            request = pickle.loads(request_payload)  # noqa: S301 - trusted parent request
            if (
                not isinstance(request, tuple)
                or len(request) != 6
                or not isinstance(request[0], bytes)
                or (request[2] is not None and not isinstance(request[2], bytes))
                or not isinstance(request[1], derivatives.DerivativeRecipe)
                or not isinstance(request[3], derivatives.DerivativeSafetyLimits)
                or not isinstance(request[4], tuple)
                or not request[4]
                or any(not isinstance(target, str) for target in request[4])
                or not isinstance(request[5], str)
                or request[5] not in derivatives.SUPPORTED_DERIVATIVE_RENDERER_VERSIONS
            ):
                raise DerivativeIsolationProtocolError("isolated renderer request is invalid")
            master_payload, recipe, watermark_payload, limits, target_values, renderer_version = (
                request
            )
            bundle = derivatives.render_platform_derivatives(
                master_payload,
                recipe=recipe,
                watermark_png=watermark_payload,
                targets=target_values,
                limits=limits,
                renderer_version=renderer_version,
            )
            response = _success_message(bundle)
        except derivatives.DerivativeInputError as error:
            response = _error_message("input", str(error))
        except derivatives.DerivativeRecipeError as error:
            response = _error_message("recipe", str(error))
        except derivatives.DerivativeRenderError as error:
            response = _error_message("render", str(error))
        except (DerivativeIsolationProtocolError, MemoryError):
            response = _error_message("internal", "isolated derivative rendering failed")
        except BaseException:
            response = _error_message("internal", "isolated derivative rendering failed")
        _send_child_message(send_connection, response)
    finally:
        send_connection.close()


def _send_child_message(connection: Connection, payload: bytes) -> None:
    try:
        connection.send_bytes(payload)
    except (BrokenPipeError, EOFError, OSError):
        return


def _success_message(bundle: Any) -> bytes:
    artifacts: list[dict[str, object]] = []
    for artifact in bundle.artifacts:
        lineage = artifact.lineage
        artifacts.append(
            {
                "target": artifact.target.value,
                "output_filename": artifact.output_filename,
                "data": base64.b64encode(artifact.data).decode("ascii"),
                "sha256": artifact.sha256,
                "byte_size": artifact.byte_size,
                "image_format": artifact.image_format.value,
                "content_type": artifact.content_type,
                "extension": artifact.extension,
                "width": artifact.width,
                "height": artifact.height,
                "recipe_sha256": artifact.recipe_sha256,
                "lineage_sha256": artifact.lineage_sha256,
                "lineage": {
                    "target": lineage.target.value,
                    "source_sha256": lineage.source_sha256,
                    "source_byte_size": lineage.source_byte_size,
                    "source_format": lineage.source_format,
                    "source_width": lineage.source_width,
                    "source_height": lineage.source_height,
                    "normalized_width": lineage.normalized_width,
                    "normalized_height": lineage.normalized_height,
                    "recipe_version": lineage.recipe_version,
                    "recipe_sha256": lineage.recipe_sha256,
                    "watermark_sha256": lineage.watermark_sha256,
                    "renderer_version": lineage.renderer_version,
                    "pillow_version": lineage.pillow_version,
                    "operations": list(lineage.operations),
                },
            }
        )
    return _json_message(
        {
            "ok": True,
            "bundle": {
                "source_sha256": bundle.source_sha256,
                "recipe_sha256": bundle.recipe_sha256,
                "artifacts": artifacts,
            },
        }
    )


def _error_message(kind: str, message: str) -> bytes:
    return _json_message(
        {
            "ok": False,
            "error": {
                "kind": kind,
                "message": message[:512],
            },
        }
    )


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
    limits: DerivativeSafetyLimits,
    *,
    expected_source_sha256: str | None = None,
    expected_recipe: DerivativeRecipe | None = None,
    expected_target_values: tuple[str, ...] | None = None,
) -> DerivativeBundle:
    from gen_automation.services import derivatives

    try:
        decoded = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise DerivativeIsolationProtocolError(
            "isolated renderer returned malformed IPC data"
        ) from None
    if not isinstance(decoded, dict) or not isinstance(decoded.get("ok"), bool):
        raise DerivativeIsolationProtocolError("isolated renderer returned malformed IPC data")
    if decoded["ok"] is False:
        error = _mapping(decoded.get("error"))
        kind = _text(error.get("kind"))
        message = _text(error.get("message"))
        if kind == "input":
            raise derivatives.DerivativeInputError(message)
        if kind == "recipe":
            raise derivatives.DerivativeRecipeError(message)
        if kind == "render":
            raise derivatives.DerivativeRenderError(message)
        if kind == "isolation_unavailable":
            raise DerivativeIsolationUnavailableError(message)
        raise DerivativeIsolationCrashError(message)

    bundle_wire = _mapping(decoded.get("bundle"))
    source_sha256 = _sha256_text(bundle_wire.get("source_sha256"))
    recipe_sha256 = _sha256_text(bundle_wire.get("recipe_sha256"))
    if expected_source_sha256 is not None and source_sha256 != expected_source_sha256:
        raise DerivativeIsolationProtocolError(
            "isolated renderer returned an unexpected source identity"
        )
    if expected_recipe is not None and recipe_sha256 != derivatives.derivative_recipe_sha256(
        expected_recipe
    ):
        raise DerivativeIsolationProtocolError(
            "isolated renderer returned an unexpected recipe identity"
        )
    if expected_target_values is None:
        expected_targets = tuple(derivatives.DerivativeTarget)
    else:
        try:
            expected_targets = tuple(
                derivatives.DerivativeTarget(value) for value in expected_target_values
            )
        except ValueError:
            raise DerivativeIsolationProtocolError(
                "expected isolated renderer targets are invalid"
            ) from None
        if (
            not expected_targets
            or len(set(expected_targets)) != len(expected_targets)
            or expected_targets
            != tuple(
                target for target in derivatives.DerivativeTarget if target in expected_targets
            )
        ):
            raise DerivativeIsolationProtocolError("expected isolated renderer targets are invalid")
    artifacts_wire = bundle_wire.get("artifacts")
    if not isinstance(artifacts_wire, list) or len(artifacts_wire) != len(expected_targets):
        raise DerivativeIsolationProtocolError(
            "isolated renderer returned an invalid artifact collection"
        )
    try:
        artifacts = tuple(
            _decode_artifact(
                _mapping(artifact_wire),
                limits=limits,
                expected_source_sha256=source_sha256,
                expected_recipe_sha256=recipe_sha256,
            )
            for artifact_wire in artifacts_wire
        )
    except DerivativeIsolationProtocolError:
        raise
    except (TypeError, ValueError):
        raise DerivativeIsolationProtocolError(
            "isolated renderer returned invalid artifact metadata"
        ) from None
    if tuple(artifact.target for artifact in artifacts) != expected_targets:
        raise DerivativeIsolationProtocolError(
            "isolated renderer returned artifacts in an invalid order"
        )
    if expected_recipe is not None:
        expected_filenames = tuple(
            (
                expected_recipe.full.output_filename
                if target is derivatives.DerivativeTarget.FULL_RESOLUTION
                else expected_recipe.x_teaser.output_filename
            )
            for target in expected_targets
        )
    else:
        expected_filenames = None
    if (
        expected_filenames is not None
        and tuple(artifact.output_filename for artifact in artifacts) != expected_filenames
    ):
        raise DerivativeIsolationProtocolError(
            "isolated renderer returned unexpected artifact filenames"
        )
    return derivatives.DerivativeBundle(
        source_sha256=source_sha256,
        recipe_sha256=recipe_sha256,
        artifacts=cast(tuple[derivatives.RenderedDerivative, ...], artifacts),
    )


def _decode_artifact(
    wire: Mapping[str, object],
    *,
    limits: DerivativeSafetyLimits,
    expected_source_sha256: str,
    expected_recipe_sha256: str,
) -> Any:
    from gen_automation.services import derivatives

    target = derivatives.DerivativeTarget(_text(wire.get("target")))
    maximum_bytes = limits.output_byte_limit(target)
    data = _bounded_base64_field(
        wire.get("data"),
        maximum_decoded_bytes=maximum_bytes,
    )
    sha256 = _sha256_text(wire.get("sha256"))
    if hashlib.sha256(data).hexdigest() != sha256:
        raise DerivativeIsolationProtocolError(
            "isolated renderer returned an artifact checksum mismatch"
        )
    byte_size = _integer(wire.get("byte_size"), minimum=1, maximum=maximum_bytes)
    if byte_size != len(data):
        raise DerivativeIsolationProtocolError(
            "isolated renderer returned an artifact size mismatch"
        )
    width = _integer(wire.get("width"), minimum=1, maximum=limits.max_output_width)
    height = _integer(wire.get("height"), minimum=1, maximum=limits.max_output_height)
    if width * height > limits.max_output_pixels:
        raise DerivativeIsolationProtocolError(
            "isolated renderer returned artifact dimensions outside the output limit"
        )
    recipe_sha256 = _sha256_text(wire.get("recipe_sha256"))
    if recipe_sha256 != expected_recipe_sha256:
        raise DerivativeIsolationProtocolError(
            "isolated renderer returned an artifact recipe mismatch"
        )
    lineage_wire = _mapping(wire.get("lineage"))
    lineage = derivatives.DerivativeLineage(
        target=derivatives.DerivativeTarget(_text(lineage_wire.get("target"))),
        source_sha256=_sha256_text(lineage_wire.get("source_sha256")),
        source_byte_size=_integer(
            lineage_wire.get("source_byte_size"),
            minimum=1,
            maximum=limits.max_master_bytes,
        ),
        source_format=_text(lineage_wire.get("source_format")),
        source_width=_integer(
            lineage_wire.get("source_width"),
            minimum=1,
            maximum=limits.max_input_width,
        ),
        source_height=_integer(
            lineage_wire.get("source_height"),
            minimum=1,
            maximum=limits.max_input_height,
        ),
        normalized_width=_integer(
            lineage_wire.get("normalized_width"),
            minimum=1,
            maximum=limits.max_input_width,
        ),
        normalized_height=_integer(
            lineage_wire.get("normalized_height"),
            minimum=1,
            maximum=limits.max_input_height,
        ),
        recipe_version=_text(lineage_wire.get("recipe_version")),
        recipe_sha256=_sha256_text(lineage_wire.get("recipe_sha256")),
        watermark_sha256=_optional_sha256_text(lineage_wire.get("watermark_sha256")),
        renderer_version=_text(lineage_wire.get("renderer_version")),
        pillow_version=_text(lineage_wire.get("pillow_version")),
        operations=_text_tuple(lineage_wire.get("operations")),
    )
    if (
        lineage.source_width * lineage.source_height > limits.max_input_pixels
        or lineage.normalized_width * lineage.normalized_height
        != lineage.source_width * lineage.source_height
        or max(lineage.source_width, lineage.source_height)
        > min(lineage.source_width, lineage.source_height) * limits.max_input_aspect_ratio
        or lineage.source_format not in {"JPEG", "PNG", "WEBP"}
        or lineage.renderer_version != derivatives.DERIVATIVE_RENDERER_VERSION
    ):
        raise DerivativeIsolationProtocolError("isolated renderer returned invalid source lineage")
    if (
        lineage.source_sha256 != expected_source_sha256
        or lineage.recipe_sha256 != expected_recipe_sha256
    ):
        raise DerivativeIsolationProtocolError("isolated renderer returned inconsistent lineage")
    if lineage.target is not target:
        raise DerivativeIsolationProtocolError(
            "isolated renderer returned a lineage target mismatch"
        )
    lineage_sha256 = _sha256_text(wire.get("lineage_sha256"))
    if canonical_sha256(asdict(lineage)) != lineage_sha256:
        raise DerivativeIsolationProtocolError(
            "isolated renderer returned a lineage checksum mismatch"
        )
    image_format = derivatives.OutputFormat(_text(wire.get("image_format")))
    content_type = _text(wire.get("content_type"))
    extension = _text(wire.get("extension"))
    expected_content_type = (
        "image/jpeg" if image_format is derivatives.OutputFormat.JPEG else "image/png"
    )
    expected_extension = "jpg" if image_format is derivatives.OutputFormat.JPEG else "png"
    if content_type != expected_content_type or extension != expected_extension:
        raise DerivativeIsolationProtocolError(
            "isolated renderer returned inconsistent format metadata"
        )
    if (
        image_format is derivatives.OutputFormat.PNG and not data.startswith(b"\x89PNG\r\n\x1a\n")
    ) or (
        image_format is derivatives.OutputFormat.JPEG
        and not (data.startswith(b"\xff\xd8\xff") and data.endswith(b"\xff\xd9"))
    ):
        raise DerivativeIsolationProtocolError(
            "isolated renderer returned bytes inconsistent with the output format"
        )
    return derivatives.RenderedDerivative(
        target=target,
        output_filename=_text(wire.get("output_filename")),
        data=data,
        sha256=sha256,
        byte_size=byte_size,
        image_format=image_format,
        content_type=content_type,
        extension=extension,
        width=width,
        height=height,
        recipe_sha256=recipe_sha256,
        lineage_sha256=lineage_sha256,
        lineage=lineage,
    )


def _maximum_result_message_bytes(maximum_output_bytes: int) -> int:
    encoded_artifact_bytes = 4 * ((maximum_output_bytes + 2) // 3)
    return 2 * encoded_artifact_bytes + _RESULT_METADATA_ALLOWANCE_BYTES


def _bounded_parent_bytes(value: object, *, maximum: int, label: str) -> bytes:
    from gen_automation.services.derivatives import DerivativeInputError

    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise DerivativeInputError(f"{label} must be bytes-like")
    try:
        size = value.nbytes if isinstance(value, memoryview) else len(value)
    except (TypeError, ValueError):
        raise DerivativeInputError(f"{label} is invalid") from None
    if size <= 0:
        raise DerivativeInputError(f"{label} is empty")
    if size > maximum:
        raise DerivativeInputError(f"{label} exceeds the input byte limit")
    try:
        payload = bytes(value)
    except (MemoryError, TypeError, ValueError):
        raise DerivativeInputError(f"{label} is invalid") from None
    if len(payload) != size:
        raise DerivativeInputError(f"{label} changed while it was copied")
    return payload


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise DerivativeIsolationProtocolError("isolated renderer returned malformed IPC data")
    return cast(Mapping[str, object], value)


def _bounded_base64_field(value: object, *, maximum_decoded_bytes: int) -> bytes:
    if not isinstance(value, str) or not value:
        raise DerivativeIsolationProtocolError("isolated renderer returned invalid artifact bytes")
    maximum_encoded_characters = 4 * ((maximum_decoded_bytes + 2) // 3)
    if len(value) > maximum_encoded_characters:
        raise DerivativeIsolationProtocolError(
            "isolated renderer returned artifact bytes outside the output limit"
        )
    try:
        encoded = value.encode("ascii")
        decoded = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError, UnicodeEncodeError):
        raise DerivativeIsolationProtocolError(
            "isolated renderer returned invalid artifact bytes"
        ) from None
    if not decoded:
        raise DerivativeIsolationProtocolError("isolated renderer returned invalid artifact bytes")
    if len(decoded) > maximum_decoded_bytes:
        raise DerivativeIsolationProtocolError(
            "isolated renderer returned artifact bytes outside the output limit"
        )
    return decoded


def _text(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 1024:
        raise DerivativeIsolationProtocolError("isolated renderer returned malformed text metadata")
    return value


def _sha256_text(value: object) -> str:
    text = _text(value)
    if len(text) != 64:
        raise DerivativeIsolationProtocolError(
            "isolated renderer returned malformed checksum metadata"
        )
    try:
        int(text, 16)
    except ValueError:
        raise DerivativeIsolationProtocolError(
            "isolated renderer returned malformed checksum metadata"
        ) from None
    return text


def _optional_sha256_text(value: object) -> str | None:
    return None if value is None else _sha256_text(value)


def _integer(value: object, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise DerivativeIsolationProtocolError(
            "isolated renderer returned malformed integer metadata"
        )
    return value


def _text_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= 32:
        raise DerivativeIsolationProtocolError(
            "isolated renderer returned malformed operation metadata"
        )
    return tuple(_text(item) for item in value)


def _bounded_real(value: float, label: str, *, minimum: float, maximum: float) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not minimum <= value <= maximum
    ):
        raise ValueError(f"{label} must be between {minimum} and {maximum} seconds")
