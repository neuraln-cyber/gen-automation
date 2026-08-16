"""Strict models for RunPod Serverless queue responses."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import cast

type JSONScalar = str | int | float | bool | None
type JSONValue = JSONScalar | list[JSONValue] | dict[str, JSONValue]
type JSONObject = dict[str, JSONValue]


class RunPodJobStatus(StrEnum):
    IN_QUEUE = "IN_QUEUE"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True, slots=True)
class RunPodJob:
    id: str
    status: RunPodJobStatus
    output: JSONValue
    error: str | None
    delay_time_ms: int | None
    execution_time_ms: int | None
    worker_id: str | None


@dataclass(frozen=True, slots=True)
class RunPodEndpointHealth:
    completed_jobs: int
    failed_jobs: int
    in_progress_jobs: int
    in_queue_jobs: int
    retried_jobs: int
    idle_workers: int
    running_workers: int


def as_json_object(value: object, context: str) -> JSONObject:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{context} must be a JSON object")
    return cast(JSONObject, value)


def parse_job(value: object) -> RunPodJob:
    data = as_json_object(value, "RunPod job")
    job_id = _required_text(data, "id", "RunPod job")
    try:
        status = RunPodJobStatus(_required_text(data, "status", "RunPod job"))
    except ValueError as exc:
        raise ValueError("RunPod job.status is not documented") from exc
    error_value = data.get("error")
    provider_error = error_value if isinstance(error_value, str) and error_value else None
    return RunPodJob(
        id=job_id,
        status=status,
        output=data.get("output"),
        error=provider_error,
        delay_time_ms=_optional_nonnegative_int(data, "delayTime", "RunPod job"),
        execution_time_ms=_optional_nonnegative_int(data, "executionTime", "RunPod job"),
        worker_id=_optional_text(data, "workerId", "RunPod job"),
    )


def parse_health(value: object) -> RunPodEndpointHealth:
    data = as_json_object(value, "RunPod endpoint health")
    jobs = as_json_object(data.get("jobs"), "RunPod endpoint health.jobs")
    workers = as_json_object(data.get("workers"), "RunPod endpoint health.workers")
    return RunPodEndpointHealth(
        completed_jobs=_nonnegative_int(jobs, "completed", "RunPod endpoint health.jobs"),
        failed_jobs=_nonnegative_int(jobs, "failed", "RunPod endpoint health.jobs"),
        in_progress_jobs=_nonnegative_int(jobs, "inProgress", "RunPod endpoint health.jobs"),
        in_queue_jobs=_nonnegative_int(jobs, "inQueue", "RunPod endpoint health.jobs"),
        retried_jobs=_nonnegative_int(jobs, "retried", "RunPod endpoint health.jobs"),
        idle_workers=_nonnegative_int(workers, "idle", "RunPod endpoint health.workers"),
        running_workers=_nonnegative_int(workers, "running", "RunPod endpoint health.workers"),
    )


def _required_text(data: JSONObject, key: str, context: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{context}.{key} must be a non-empty string")
    return value


def _optional_text(data: JSONObject, key: str, context: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{context}.{key} must be a non-empty string or null")
    return value


def _nonnegative_int(data: JSONObject, key: str, context: str) -> int:
    value = data.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{context}.{key} must be a nonnegative integer")
    return value


def _optional_nonnegative_int(data: JSONObject, key: str, context: str) -> int | None:
    if key not in data or data[key] is None:
        return None
    return _nonnegative_int(data, key, context)
