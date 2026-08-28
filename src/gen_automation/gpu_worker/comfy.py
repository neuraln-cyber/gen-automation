from __future__ import annotations

import base64
import io
import ipaddress
import json
import math
import re
import threading
import time
from collections.abc import Callable, Collection, Mapping
from typing import Any
from urllib.parse import SplitResult, urlsplit

import httpx2
from PIL import Image, UnidentifiedImageError

from gen_automation.domain.deliverability import require_comfy_workflow_deliverability
from gen_automation.gpu_worker.models import (
    DEFAULT_APPROVED_WORKFLOW_NODE_CLASSES,
    MAX_HARD_OUTPUT_BYTES,
    MAX_HARD_OUTPUTS,
    MAX_HARD_TOTAL_OUTPUT_BYTES,
    NODE_CLASS_PATTERN,
    OUTPUT_NODE_CLASSES,
    JsonObject,
    validate_approved_workflow,
)

DEFAULT_COMFY_BASE_URL = "http://127.0.0.1:8188"
DEFAULT_REQUEST_TIMEOUT_SECONDS = 10.0
DEFAULT_EXECUTION_TIMEOUT_SECONDS = 60 * 60.0
DEFAULT_POLL_INTERVAL_SECONDS = 0.5
DEFAULT_MAX_API_RESPONSE_BYTES = 2 * 1024 * 1024
DEFAULT_MAX_WORKFLOW_BYTES = 2 * 1024 * 1024
DEFAULT_MAX_OUTPUT_BYTES = 25 * 1024 * 1024
DEFAULT_MAX_TOTAL_OUTPUT_BYTES = 256 * 1024 * 1024

_MAX_JSON_DEPTH = 64
_MAX_METADATA_TEXT_BYTES = 4096
_PROMPT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_ERROR_STATUS_NAMES = frozenset(
    {"error", "failed", "failure", "cancelled", "canceled", "interrupted"}
)
_ERROR_MESSAGE_NAMES = frozenset(
    {"execution_error", "execution_interrupted", "execution_cached_error"}
)


class ComfyExecutorError(Exception):
    """A redacted failure safe to expose to the worker service layer."""


class ComfyConfigurationError(ComfyExecutorError):
    """The executor was configured outside its local security boundary."""


class ComfyUnavailableError(ComfyExecutorError):
    """The local Comfy service could not be reached or rejected a request."""


class ComfyProtocolError(ComfyExecutorError):
    """The local Comfy service returned an invalid response."""


class ComfyExecutionError(ComfyExecutorError):
    """Comfy reported that the submitted graph failed."""


class ComfyExecutionTimeoutError(ComfyExecutorError):
    """The submitted graph did not reach a terminal state before its deadline."""


class ComfyOutputError(ComfyExecutorError):
    """A selected Comfy output was missing, unsafe, or invalid."""


class _ResponseTooLargeError(ComfyProtocolError):
    pass


class ComfyExecutor:
    """Synchronous, loopback-only adapter for ComfyUI's HTTP API.

    One executor serializes graph execution. This makes the best-effort global
    ``/interrupt`` call safe for a dedicated worker-local Comfy process.
    """

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_COMFY_BASE_URL,
        request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
        execution_timeout_seconds: float = DEFAULT_EXECUTION_TIMEOUT_SECONDS,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
        max_api_response_bytes: int = DEFAULT_MAX_API_RESPONSE_BYTES,
        max_workflow_bytes: int = DEFAULT_MAX_WORKFLOW_BYTES,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
        max_total_output_bytes: int = DEFAULT_MAX_TOTAL_OUTPUT_BYTES,
        approved_node_classes: Collection[str] | None = None,
        interrupt_on_timeout: bool = True,
        transport: httpx2.BaseTransport | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        normalized_base_url = _normalize_loopback_base_url(base_url)
        self._request_timeout_seconds = _bounded_float(
            request_timeout_seconds,
            name="request timeout",
            maximum=300.0,
        )
        self._execution_timeout_seconds = _bounded_float(
            execution_timeout_seconds,
            name="execution timeout",
            maximum=24 * 60 * 60.0,
        )
        self._poll_interval_seconds = _bounded_float(
            poll_interval_seconds,
            name="poll interval",
            maximum=60.0,
        )
        self._max_api_response_bytes = _bounded_integer(
            max_api_response_bytes,
            name="API response limit",
            maximum=16 * 1024 * 1024,
        )
        self._max_workflow_bytes = _bounded_integer(
            max_workflow_bytes,
            name="workflow limit",
            maximum=16 * 1024 * 1024,
        )
        self._max_output_bytes = _bounded_integer(
            max_output_bytes,
            name="output limit",
            maximum=MAX_HARD_OUTPUT_BYTES,
        )
        self._max_total_output_bytes = _bounded_integer(
            max_total_output_bytes,
            name="total output limit",
            maximum=MAX_HARD_TOTAL_OUTPUT_BYTES,
        )
        if self._max_total_output_bytes < self._max_output_bytes:
            raise ComfyConfigurationError("invalid Comfy executor configuration")
        if not isinstance(interrupt_on_timeout, bool):
            raise ComfyConfigurationError("invalid Comfy executor configuration")

        self._approved_node_classes = _normalize_approved_node_classes(approved_node_classes)
        self._interrupt_on_timeout = interrupt_on_timeout
        self._monotonic = monotonic
        self._sleep = sleep
        self._execution_lock = threading.Lock()
        self._closed = False
        self._client = httpx2.Client(
            base_url=normalized_base_url,
            follow_redirects=False,
            trust_env=False,
            timeout=httpx2.Timeout(
                self._request_timeout_seconds,
                connect=min(self._request_timeout_seconds, 5.0),
            ),
            limits=httpx2.Limits(max_connections=2, max_keepalive_connections=2),
            transport=transport,
        )

    def __enter__(self) -> ComfyExecutor:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._client.close()

    @property
    def approved_node_classes(self) -> frozenset[str]:
        return self._approved_node_classes

    def is_ready(self) -> bool:
        if self._closed or not self._execution_lock.acquire(blocking=False):
            return False
        try:
            response = self._request_json("GET", "/system_stats")
            return isinstance(response.get("system"), dict) and isinstance(
                response.get("devices"), list
            )
        except ComfyExecutorError:
            return False
        finally:
            self._execution_lock.release()

    def execute(self, workflow: JsonObject) -> object:
        if self._closed:
            raise ComfyUnavailableError("Comfy is unavailable")
        if not self._execution_lock.acquire(blocking=False):
            raise ComfyUnavailableError("Comfy is unavailable")

        try:
            return self._execute_locked(workflow)
        finally:
            self._execution_lock.release()

    def reset_model_and_node_cache(self, barrier_workflow: JsonObject) -> None:
        """Reset Comfy state without restarting the worker or changing its identity."""

        if self._closed:
            raise ComfyUnavailableError("Comfy is unavailable")
        if not self._execution_lock.acquire(blocking=False):
            raise ComfyUnavailableError("Comfy is unavailable")

        try:
            self._request_bytes(
                "POST",
                "/free",
                content=b'{"unload_models":true,"free_memory":true}',
                content_type="application/json",
                limit=64 * 1024,
                expected_statuses=frozenset({200, 204}),
                timeout_seconds=min(self._request_timeout_seconds, 5.0),
            )
            # Comfy consumes /free flags after a prompt. Replaying the already
            # approved workflow as an internal barrier guarantees the reset is
            # applied before the next real output without restarting the
            # container or creating another provider job.
            self._execute_locked(barrier_workflow)
        finally:
            self._execution_lock.release()

    def _execute_locked(self, workflow: JsonObject) -> object:
        prompt_id: str | None = None
        try:
            workflow_body, selected_nodes = self._prepare_workflow(workflow)
            prompt_id = self._submit_workflow(workflow_body)
            deadline = self._monotonic() + self._execution_timeout_seconds
            history_entry = self._wait_for_history(prompt_id, deadline)
            metadata = self._extract_output_metadata(history_entry, selected_nodes)
            return self._download_outputs(metadata)
        except ComfyExecutionTimeoutError:
            if prompt_id is not None and self._interrupt_on_timeout:
                self._best_effort_interrupt()
            raise

    def _prepare_workflow(self, workflow: JsonObject) -> tuple[bytes, tuple[str, ...]]:
        try:
            validate_approved_workflow(workflow, self._approved_node_classes)
            require_comfy_workflow_deliverability(workflow)
        except ValueError:
            raise ComfyProtocolError("invalid Comfy workflow") from None

        selected_nodes: list[str] = []
        for node_id, raw_node in workflow.items():
            if (
                not isinstance(node_id, str)
                or _PROMPT_ID_PATTERN.fullmatch(node_id) is None
                or not isinstance(raw_node, dict)
            ):
                raise ComfyProtocolError("invalid Comfy workflow")
            class_type = raw_node.get("class_type")
            inputs = raw_node.get("inputs")
            if (
                not isinstance(class_type, str)
                or not class_type
                or not _utf8_length_within(class_type, 256)
                or not isinstance(inputs, dict)
            ):
                raise ComfyProtocolError("invalid Comfy workflow")
            if class_type in OUTPUT_NODE_CLASSES:
                selected_nodes.append(node_id)

        if not selected_nodes or len(selected_nodes) > MAX_HARD_OUTPUTS:
            raise ComfyProtocolError("invalid Comfy workflow")

        try:
            _validate_json_value(workflow)
            serialized = json.dumps(
                {"prompt": workflow},
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (OverflowError, RecursionError, TypeError, ValueError):
            raise ComfyProtocolError("invalid Comfy workflow") from None
        if len(serialized) > self._max_workflow_bytes:
            raise ComfyProtocolError("invalid Comfy workflow")
        return serialized, tuple(sorted(selected_nodes, key=_node_sort_key))

    def _submit_workflow(self, body: bytes) -> str:
        response = self._request_json(
            "POST",
            "/prompt",
            content=body,
            content_type="application/json",
        )
        node_errors = response.get("node_errors")
        if node_errors not in (None, {}) or response.get("error") is not None:
            raise ComfyExecutionError("Comfy execution failed")
        prompt_id = response.get("prompt_id")
        if not isinstance(prompt_id, str) or _PROMPT_ID_PATTERN.fullmatch(prompt_id) is None:
            raise ComfyProtocolError("invalid Comfy response")
        return prompt_id

    def _wait_for_history(self, prompt_id: str, deadline: float) -> dict[str, object]:
        while True:
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise ComfyExecutionTimeoutError("Comfy execution timed out")

            response = self._request_json(
                "GET",
                f"/history/{prompt_id}",
                timeout_seconds=min(self._request_timeout_seconds, remaining),
            )
            if response:
                if set(response) != {prompt_id}:
                    raise ComfyProtocolError("invalid Comfy response")
                raw_entry = response[prompt_id]
                if not isinstance(raw_entry, dict):
                    raise ComfyProtocolError("invalid Comfy response")
                terminal = self._parse_history_status(raw_entry)
                if terminal:
                    return raw_entry

            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise ComfyExecutionTimeoutError("Comfy execution timed out")
            try:
                self._sleep(min(self._poll_interval_seconds, remaining))
            except Exception:
                raise ComfyUnavailableError("Comfy is unavailable") from None

    def _parse_history_status(self, entry: dict[str, object]) -> bool:
        raw_status = entry.get("status")
        if not isinstance(raw_status, dict):
            raise ComfyProtocolError("invalid Comfy response")
        status_name = raw_status.get("status_str")
        completed = raw_status.get("completed")
        if not isinstance(status_name, str) or not isinstance(completed, bool):
            raise ComfyProtocolError("invalid Comfy response")

        normalized_status = status_name.lower()
        if normalized_status in _ERROR_STATUS_NAMES or _contains_execution_error(raw_status):
            raise ComfyExecutionError("Comfy execution failed")
        if completed:
            if normalized_status != "success":
                raise ComfyExecutionError("Comfy execution failed")
            if not isinstance(entry.get("outputs"), dict):
                raise ComfyProtocolError("invalid Comfy response")
            return True
        if normalized_status == "success":
            raise ComfyProtocolError("invalid Comfy response")
        return False

    def _extract_output_metadata(
        self,
        history_entry: dict[str, object],
        selected_nodes: tuple[str, ...],
    ) -> list[tuple[str, str, str]]:
        raw_outputs = history_entry.get("outputs")
        if not isinstance(raw_outputs, dict):
            raise ComfyProtocolError("invalid Comfy response")

        metadata: list[tuple[str, str, str]] = []
        seen: set[tuple[str, str, str]] = set()
        for node_id in selected_nodes:
            raw_node_output = raw_outputs.get(node_id)
            if not isinstance(raw_node_output, dict):
                raise ComfyOutputError("invalid Comfy output")
            raw_images = raw_node_output.get("images")
            if not isinstance(raw_images, list) or not raw_images:
                raise ComfyOutputError("invalid Comfy output")
            node_metadata = [_validate_output_metadata(item) for item in raw_images]
            for item in sorted(node_metadata, key=lambda value: (value[1], value[0], value[2])):
                if item in seen:
                    raise ComfyOutputError("invalid Comfy output")
                seen.add(item)
                metadata.append(item)
                if len(metadata) > MAX_HARD_OUTPUTS:
                    raise ComfyOutputError("invalid Comfy output")
        return metadata

    def _download_outputs(
        self,
        metadata: list[tuple[str, str, str]],
    ) -> list[dict[str, object]]:
        outputs: list[dict[str, object]] = []
        total_bytes = 0
        for output_index, (filename, subfolder, output_type) in enumerate(metadata):
            remaining_total = self._max_total_output_bytes - total_bytes
            if remaining_total <= 0:
                raise ComfyOutputError("invalid Comfy output")
            limit = min(self._max_output_bytes, remaining_total)
            try:
                content = self._request_bytes(
                    "GET",
                    "/view",
                    params={
                        "filename": filename,
                        "subfolder": subfolder,
                        "type": output_type,
                    },
                    limit=limit,
                )
            except _ResponseTooLargeError:
                raise ComfyOutputError("invalid Comfy output") from None
            if not content:
                raise ComfyOutputError("invalid Comfy output")
            media_type = _identify_image_media_type(content)
            total_bytes += len(content)
            outputs.append(
                {
                    "output_index": output_index,
                    "media_type": media_type,
                    "data_base64": base64.b64encode(content).decode("ascii"),
                }
            )
        return outputs

    def _best_effort_interrupt(self) -> None:
        try:
            self._request_bytes(
                "POST",
                "/interrupt",
                content=b"{}",
                content_type="application/json",
                limit=64 * 1024,
                expected_statuses=frozenset({200, 204}),
                timeout_seconds=min(self._request_timeout_seconds, 2.0),
            )
        except ComfyExecutorError:
            pass

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        content: bytes | None = None,
        content_type: str | None = None,
        timeout_seconds: float | None = None,
    ) -> dict[str, object]:
        raw = self._request_bytes(
            method,
            path,
            content=content,
            content_type=content_type,
            limit=self._max_api_response_bytes,
            timeout_seconds=timeout_seconds,
        )
        try:
            parsed: Any = json.loads(
                raw,
                object_pairs_hook=_unique_object,
                parse_constant=_reject_json_constant,
            )
            _validate_json_value(parsed)
        except (json.JSONDecodeError, RecursionError, TypeError, ValueError):
            raise ComfyProtocolError("invalid Comfy response") from None
        if not isinstance(parsed, dict):
            raise ComfyProtocolError("invalid Comfy response")
        return parsed

    def _request_bytes(
        self,
        method: str,
        path: str,
        *,
        content: bytes | None = None,
        content_type: str | None = None,
        params: Mapping[str, str] | None = None,
        limit: int,
        expected_statuses: frozenset[int] = frozenset({200}),
        timeout_seconds: float | None = None,
    ) -> bytes:
        headers = {"content-type": content_type} if content_type is not None else None
        timeout = timeout_seconds or self._request_timeout_seconds
        try:
            with self._client.stream(
                method,
                path,
                content=content,
                params=params,
                headers=headers,
                follow_redirects=False,
                timeout=timeout,
            ) as response:
                if response.status_code in _REDIRECT_STATUSES:
                    raise ComfyProtocolError("invalid Comfy response")
                if response.status_code not in expected_statuses:
                    raise ComfyUnavailableError("Comfy is unavailable")

                declared_length = response.headers.get("content-length")
                if declared_length is not None:
                    try:
                        parsed_length = int(declared_length)
                    except ValueError:
                        raise ComfyProtocolError("invalid Comfy response") from None
                    if parsed_length < 0:
                        raise ComfyProtocolError("invalid Comfy response")
                    if parsed_length > limit:
                        raise _ResponseTooLargeError("invalid Comfy response")

                parts: list[bytes] = []
                total = 0
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > limit:
                        raise _ResponseTooLargeError("invalid Comfy response")
                    parts.append(chunk)
                return b"".join(parts)
        except ComfyExecutorError:
            raise
        except (httpx2.TimeoutException, httpx2.TransportError):
            raise ComfyUnavailableError("Comfy is unavailable") from None
        except Exception:
            raise ComfyUnavailableError("Comfy is unavailable") from None


def _normalize_loopback_base_url(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or "\\" in value
        or any(character.isspace() or ord(character) < 32 for character in value)
    ):
        raise ComfyConfigurationError("invalid Comfy executor configuration")
    try:
        parsed: SplitResult = urlsplit(value)
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
        address = ipaddress.ip_address(parsed.hostname or "")
    except ValueError:
        raise ComfyConfigurationError("invalid Comfy executor configuration") from None
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not address.is_loopback
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or not 1 <= port <= 65535
    ):
        raise ComfyConfigurationError("invalid Comfy executor configuration")
    host = f"[{address.compressed}]" if address.version == 6 else address.compressed
    return f"{parsed.scheme.lower()}://{host}:{port}/"


def _bounded_float(value: float, *, name: str, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ComfyConfigurationError("invalid Comfy executor configuration")
    result = float(value)
    if not math.isfinite(result) or result <= 0 or result > maximum:
        raise ComfyConfigurationError(f"invalid {name}")
    return result


def _bounded_integer(value: int, *, name: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0 or value > maximum:
        raise ComfyConfigurationError(f"invalid {name}")
    return value


def _normalize_approved_node_classes(
    value: Collection[str] | None,
) -> frozenset[str]:
    if value is None:
        return DEFAULT_APPROVED_WORKFLOW_NODE_CLASSES
    if isinstance(value, (str, bytes)) or not 1 <= len(value) <= 128:
        raise ComfyConfigurationError("invalid Comfy executor configuration")
    if any(
        not isinstance(node_class, str) or NODE_CLASS_PATTERN.fullmatch(node_class) is None
        for node_class in value
    ):
        raise ComfyConfigurationError("invalid Comfy executor configuration")
    normalized = frozenset(value)
    if not normalized.intersection(OUTPUT_NODE_CLASSES):
        raise ComfyConfigurationError("invalid Comfy executor configuration")
    return normalized


def _validate_json_value(value: object, *, depth: int = 0) -> None:
    if depth > _MAX_JSON_DEPTH:
        raise ValueError("JSON nesting is too deep")
    if value is None or isinstance(value, (bool, str, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("JSON number is not finite")
        return
    if isinstance(value, list):
        for item in value:
            _validate_json_value(item, depth=depth + 1)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("JSON object key is invalid")
            _validate_json_value(item, depth=depth + 1)
        return
    raise ValueError("value is not JSON")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> None:
    raise ValueError("invalid JSON constant")


def _node_sort_key(node_id: str) -> tuple[int, int, str]:
    if node_id.isascii() and node_id.isdecimal():
        return (0, int(node_id), node_id)
    return (1, 0, node_id)


def _contains_execution_error(status: dict[str, object]) -> bool:
    messages = status.get("messages")
    if messages is None:
        return False
    if not isinstance(messages, list):
        raise ComfyProtocolError("invalid Comfy response")
    for message in messages:
        if not isinstance(message, list) or not message or not isinstance(message[0], str):
            raise ComfyProtocolError("invalid Comfy response")
        if message[0].lower() in _ERROR_MESSAGE_NAMES:
            return True
    return False


def _validate_output_metadata(value: object) -> tuple[str, str, str]:
    if not isinstance(value, dict):
        raise ComfyOutputError("invalid Comfy output")
    filename = value.get("filename")
    subfolder = value.get("subfolder")
    output_type = value.get("type")
    if (
        not isinstance(filename, str)
        or not isinstance(subfolder, str)
        or output_type != "output"
        or not _safe_filename(filename)
        or not _safe_subfolder(subfolder)
    ):
        raise ComfyOutputError("invalid Comfy output")
    return filename, subfolder, output_type


def _safe_filename(value: str) -> bool:
    return (
        value not in {"", ".", ".."}
        and _utf8_length_within(value, _MAX_METADATA_TEXT_BYTES)
        and "/" not in value
        and "\\" not in value
        and all(ord(character) >= 32 and ord(character) != 127 for character in value)
    )


def _safe_subfolder(value: str) -> bool:
    if not _utf8_length_within(value, _MAX_METADATA_TEXT_BYTES):
        return False
    if value == "":
        return True
    if value.startswith("/") or value.endswith("/") or "\\" in value:
        return False
    components = value.split("/")
    return all(
        component not in {"", ".", ".."}
        and all(ord(character) >= 32 and ord(character) != 127 for character in component)
        for component in components
    )


def _utf8_length_within(value: str, limit: int) -> bool:
    try:
        return len(value.encode("utf-8")) <= limit
    except UnicodeEncodeError:
        return False


def _identify_image_media_type(content: bytes) -> str:
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        expected_format = "PNG"
        media_type = "image/png"
    elif content.startswith(b"\xff\xd8\xff"):
        expected_format = "JPEG"
        media_type = "image/jpeg"
    elif len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        expected_format = "WEBP"
        media_type = "image/webp"
    else:
        raise ComfyOutputError("invalid Comfy output")

    try:
        with Image.open(io.BytesIO(content)) as image:
            if image.format != expected_format or getattr(image, "n_frames", 1) != 1:
                raise ComfyOutputError("invalid Comfy output")
            image.verify()
    except ComfyOutputError:
        raise
    except (
        Image.DecompressionBombError,
        UnidentifiedImageError,
        OSError,
        SyntaxError,
        ValueError,
    ):
        raise ComfyOutputError("invalid Comfy output") from None
    return media_type
