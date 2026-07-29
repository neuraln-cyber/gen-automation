from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import cast
from uuid import UUID

type JSONScalar = str | int | float | bool | None
type JSONValue = JSONScalar | list[JSONValue] | dict[str, JSONValue]
type JSONObject = dict[str, JSONValue]


class SaladJobStatus(StrEnum):
    """Documented Salad Job Queue states."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass(frozen=True)
class SaladJobEvent:
    action: str
    time: datetime


@dataclass(frozen=True)
class SaladQueueJob:
    id: UUID
    input: JSONValue
    status: SaladJobStatus
    events: tuple[SaladJobEvent, ...]
    create_time: datetime
    update_time: datetime
    metadata: JSONObject
    webhook: str | None
    output: JSONValue


@dataclass(frozen=True)
class SaladQueueJobPage:
    items: tuple[SaladQueueJob, ...]


@dataclass(frozen=True)
class SaladQueue:
    id: UUID
    name: str
    display_name: str
    description: str | None
    current_queue_length: int
    container_groups: tuple[JSONObject, ...]
    create_time: datetime
    update_time: datetime


@dataclass(frozen=True)
class SaladContainerGroupState:
    description: str
    allocating_count: int
    creating_count: int
    running_count: int
    stopping_count: int
    start_time: datetime | None
    finish_time: datetime | None


@dataclass(frozen=True)
class SaladContainerGroup:
    id: UUID
    name: str
    display_name: str
    replicas: int
    pending_change: bool
    version: int
    current_state: SaladContainerGroupState
    create_time: datetime
    update_time: datetime
    raw: JSONObject

    @property
    def status(self) -> str:
        """Expose Salad's free-form current-state description."""
        return self.current_state.description


@dataclass(frozen=True)
class SaladContainerGroupPage:
    items: tuple[SaladContainerGroup, ...]


@dataclass(frozen=True)
class SaladGpuClass:
    id: UUID
    name: str
    prices: tuple[str, ...]
    gpu_count: int
    is_high_demand: bool
    max_ram: int
    max_storage: int
    max_vcpu: int
    min_ram: int
    min_storage: int
    min_vcpu: int
    raw: JSONObject


@dataclass(frozen=True)
class SaladGpuAvailability:
    available_gpu_batch: int
    available_gpu_high: int
    available_gpu_low: int
    available_gpu_medium: int
    on_call_gpu: int


@dataclass(frozen=True)
class SaladContainerGroupQuotas:
    container_replicas_quota: int
    container_replicas_used: int
    max_container_group_reallocations_per_minute: int
    max_container_group_recreates_per_minute: int
    max_container_group_restarts_per_minute: int


@dataclass(frozen=True)
class SaladOrganizationQuotas:
    container_groups: SaladContainerGroupQuotas
    create_time: datetime | None
    update_time: datetime | None


def as_json_object(value: object, context: str) -> JSONObject:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{context} must be a JSON object")
    return cast(JSONObject, value)


def _required_value(data: JSONObject, key: str, context: str) -> JSONValue:
    if key not in data:
        raise ValueError(f"{context}.{key} is required")
    return data[key]


def _required_str(data: JSONObject, key: str, context: str) -> str:
    value = _required_value(data, key, context)
    if not isinstance(value, str):
        raise ValueError(f"{context}.{key} must be a string")
    return value


def _optional_str(data: JSONObject, key: str, context: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{context}.{key} must be a string or null")
    return value


def _required_int(data: JSONObject, key: str, context: str) -> int:
    value = _required_value(data, key, context)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{context}.{key} must be an integer")
    return value


def _optional_int(data: JSONObject, key: str, context: str, default: int = 0) -> int:
    if key not in data:
        return default
    return _required_int(data, key, context)


def _required_bool(data: JSONObject, key: str, context: str) -> bool:
    value = _required_value(data, key, context)
    if not isinstance(value, bool):
        raise ValueError(f"{context}.{key} must be a boolean")
    return value


def _required_uuid(data: JSONObject, key: str, context: str) -> UUID:
    value = _required_str(data, key, context)
    try:
        return UUID(value)
    except ValueError as error:
        raise ValueError(f"{context}.{key} must be a UUID") from error


def _parse_datetime_value(value: JSONValue, context: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{context} must be an ISO 8601 date-time")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{context} must be an ISO 8601 date-time") from error


def _required_datetime(data: JSONObject, key: str, context: str) -> datetime:
    return _parse_datetime_value(_required_value(data, key, context), f"{context}.{key}")


def _optional_datetime(data: JSONObject, key: str, context: str) -> datetime | None:
    value = data.get(key)
    if value is None:
        return None
    return _parse_datetime_value(value, f"{context}.{key}")


def _object_items(data: JSONObject, key: str, context: str) -> tuple[JSONObject, ...]:
    value = _required_value(data, key, context)
    if not isinstance(value, list):
        raise ValueError(f"{context}.{key} must be an array")
    return tuple(
        as_json_object(item, f"{context}.{key}[{index}]") for index, item in enumerate(value)
    )


def parse_queue_job(data: JSONObject) -> SaladQueueJob:
    context = "queue job"
    try:
        status = SaladJobStatus(_required_str(data, "status", context))
    except ValueError as error:
        raise ValueError("queue job.status is not a documented Salad job status") from error

    event_objects = _object_items(data, "events", context)
    events = tuple(
        SaladJobEvent(
            action=_required_str(event, "action", f"{context}.events[{index}]"),
            time=_required_datetime(event, "time", f"{context}.events[{index}]"),
        )
        for index, event in enumerate(event_objects)
    )
    metadata_value = data.get("metadata", {})
    metadata = as_json_object(metadata_value, f"{context}.metadata")
    return SaladQueueJob(
        id=_required_uuid(data, "id", context),
        input=data.get("input"),
        status=status,
        events=events,
        create_time=_required_datetime(data, "create_time", context),
        update_time=_required_datetime(data, "update_time", context),
        metadata=metadata,
        webhook=_optional_str(data, "webhook", context),
        output=data.get("output"),
    )


def parse_queue(data: JSONObject) -> SaladQueue:
    context = "queue"
    return SaladQueue(
        id=_required_uuid(data, "id", context),
        name=_required_str(data, "name", context),
        display_name=_required_str(data, "display_name", context),
        description=_optional_str(data, "description", context),
        current_queue_length=_optional_int(data, "current_queue_length", context),
        container_groups=_object_items(data, "container_groups", context),
        create_time=_required_datetime(data, "create_time", context),
        update_time=_required_datetime(data, "update_time", context),
    )


def parse_container_group(data: JSONObject) -> SaladContainerGroup:
    context = "container group"
    state_data = as_json_object(_required_value(data, "current_state", context), "current_state")
    counts_data = as_json_object(
        _required_value(state_data, "instance_status_counts", "current_state"),
        "current_state.instance_status_counts",
    )
    state = SaladContainerGroupState(
        description=_required_str(state_data, "description", "current_state"),
        allocating_count=_optional_int(counts_data, "allocating_count", "instance status counts"),
        creating_count=_optional_int(counts_data, "creating_count", "instance status counts"),
        running_count=_optional_int(counts_data, "running_count", "instance status counts"),
        stopping_count=_optional_int(counts_data, "stopping_count", "instance status counts"),
        start_time=_optional_datetime(state_data, "start_time", "current_state"),
        finish_time=_optional_datetime(state_data, "finish_time", "current_state"),
    )
    return SaladContainerGroup(
        id=_required_uuid(data, "id", context),
        name=_required_str(data, "name", context),
        display_name=_required_str(data, "display_name", context),
        replicas=_required_int(data, "replicas", context),
        pending_change=_required_bool(data, "pending_change", context),
        version=_required_int(data, "version", context),
        current_state=state,
        create_time=_required_datetime(data, "create_time", context),
        update_time=_required_datetime(data, "update_time", context),
        raw=data,
    )


def parse_gpu_class(data: JSONObject) -> SaladGpuClass:
    context = "GPU class"
    price_objects = _object_items(data, "prices", context)
    prices = tuple(
        _required_str(price, "price", f"{context}.prices[{index}]")
        for index, price in enumerate(price_objects)
    )
    return SaladGpuClass(
        id=_required_uuid(data, "id", context),
        name=_required_str(data, "name", context),
        prices=prices,
        gpu_count=_required_int(data, "gpu_count", context),
        is_high_demand=_required_bool(data, "is_high_demand", context),
        max_ram=_required_int(data, "max_ram", context),
        max_storage=_required_int(data, "max_storage", context),
        max_vcpu=_required_int(data, "max_vcpu", context),
        min_ram=_required_int(data, "min_ram", context),
        min_storage=_required_int(data, "min_storage", context),
        min_vcpu=_required_int(data, "min_vcpu", context),
        raw=data,
    )


def parse_gpu_availability(data: JSONObject) -> SaladGpuAvailability:
    context = "GPU availability"
    return SaladGpuAvailability(
        available_gpu_batch=_required_int(data, "available_gpu_batch", context),
        available_gpu_high=_required_int(data, "available_gpu_high", context),
        available_gpu_low=_required_int(data, "available_gpu_low", context),
        available_gpu_medium=_required_int(data, "available_gpu_medium", context),
        on_call_gpu=_required_int(data, "on_call_gpu", context),
    )


def parse_organization_quotas(data: JSONObject) -> SaladOrganizationQuotas:
    context = "organization quotas"
    group_data = as_json_object(
        _required_value(data, "container_groups_quotas", context),
        f"{context}.container_groups_quotas",
    )
    groups = SaladContainerGroupQuotas(
        container_replicas_quota=_required_int(
            group_data, "container_replicas_quota", "container group quotas"
        ),
        container_replicas_used=_required_int(
            group_data, "container_replicas_used", "container group quotas"
        ),
        max_container_group_reallocations_per_minute=_required_int(
            group_data,
            "max_container_group_reallocations_per_minute",
            "container group quotas",
        ),
        max_container_group_recreates_per_minute=_required_int(
            group_data,
            "max_container_group_recreates_per_minute",
            "container group quotas",
        ),
        max_container_group_restarts_per_minute=_required_int(
            group_data,
            "max_container_group_restarts_per_minute",
            "container group quotas",
        ),
    )
    return SaladOrganizationQuotas(
        container_groups=groups,
        create_time=_optional_datetime(data, "create_time", context),
        update_time=_optional_datetime(data, "update_time", context),
    )
