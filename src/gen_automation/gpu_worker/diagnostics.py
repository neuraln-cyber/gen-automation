"""Closed-schema failure telemetry: no exception text, requests or media."""

import hashlib
import json
from typing import Literal

from gen_automation.gpu_worker.comfy import (
    ComfyConfigurationError,
    ComfyExecutionError,
    ComfyExecutionTimeoutError,
    ComfyOutputError,
    ComfyProtocolError,
    ComfyUnavailableError,
)
from gen_automation.gpu_worker.runtime import (
    SAFE_STORAGE_ERROR_CODES,
    UPLOAD_FAILURE_CATEGORIES,
    WorkerUploadError,
)


def log_generation_failure(
    *,
    phase: Literal["execute", "validate_output", "upload"],
    attempt_id: str,
    output_index: int | None,
    uploaded_count: int,
    error: Exception | None = None,
) -> None:
    category = "unexpected_error" if error is not None else "output_invalid"
    for error_type, label in (
        (ComfyExecutionTimeoutError, "comfy_execution_timeout"),
        (ComfyExecutionError, "comfy_execution_failed"),
        (ComfyOutputError, "comfy_output_invalid"),
        (ComfyProtocolError, "comfy_protocol_invalid"),
        (ComfyUnavailableError, "comfy_unavailable"),
        (ComfyConfigurationError, "comfy_configuration_invalid"),
    ):
        if isinstance(error, error_type):
            category = label
            break
    status_code = None
    storage_code = None
    if isinstance(error, WorkerUploadError):
        category = (
            error.category if error.category in UPLOAD_FAILURE_CATEGORIES else "upload_failed"
        )
        if type(error.status_code) is int and 100 <= error.status_code <= 599:
            status_code = error.status_code
        if error.storage_code in SAFE_STORAGE_ERROR_CODES:
            storage_code = error.storage_code
    event = {
        "event": "image_worker_failure",
        "schema": 1,
        "phase": phase,
        "category": category,
        "attempt_sha256": hashlib.sha256(attempt_id.encode()).hexdigest(),
        "output_index": output_index,
        "uploaded_count": uploaded_count,
        "http_status": status_code,
        "storage_code": storage_code,
    }
    try:
        print(json.dumps(event, separators=(",", ":")), flush=True)
    except OSError:
        # Telemetry must not change submission, retry or replay semantics.
        pass
