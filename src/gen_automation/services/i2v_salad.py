"""Small, strict SaladCloud adapter for the fresh I2V lane.

This module uses only the typed low-level Salad client.  It intentionally has no
dependency on the retired ``SaladDeployment`` or ``video_generation_*`` models.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from re import fullmatch
from typing import Protocol
from uuid import UUID

from gen_automation.domain.i2v import I2VOutputRegistration, I2VWorkerDeploymentState
from gen_automation.integrations.salad.client import SALAD_QUEUE_JOB_PAGE_SIZE
from gen_automation.integrations.salad.errors import SaladAPIError, SaladProtocolError
from gen_automation.integrations.salad.models import (
    JSONObject,
    JSONValue,
    SaladContainerGroup,
    SaladContainerGroupInstance,
    SaladContainerGroupInstanceState,
    SaladContainerGroupPage,
    SaladGpuClass,
    SaladJobStatus,
    SaladQueue,
    SaladQueueJob,
    SaladQueueJobPage,
)

I2V_SALAD_GPU_CLASS_NAME = "RTX 5090 (32 GB)"
I2V_SALAD_SUBMISSION_SCHEMA = "i2v-salad-submission/v1"
I2V_SALAD_JOB_SCHEMA = "i2v-salad-job/v1"
I2V_WORKER_OUTPUT_SCHEMA = "i2v-salad-result/v1"
_PINNED_IMAGE_PATTERN = r"[^\s@]+@sha256:[0-9a-f]{64}"


class I2VSaladError(Exception):
    """Base error for the clean I2V Salad lane."""


class I2VSaladConfigurationError(I2VSaladError):
    pass


class I2VSaladConflictError(I2VSaladError):
    pass


class I2VInfrastructureMutation(StrEnum):
    NONE = "none"
    QUEUE_CREATED = "queue_created"
    GROUP_CREATED = "group_created"


@dataclass(frozen=True)
class I2VSaladConfig:
    queue_name: str
    container_group_name: str
    worker_image: str
    gpu_class_id: UUID
    gpu_class_name: str = I2V_SALAD_GPU_CLASS_NAME
    prefetch: int = 3
    worker_lease_seconds: int = 86_400
    warm_idle_seconds: int | None = 1_800
    cpu: int = 8
    memory_mb: int = 32_768
    storage_bytes: int = 268_435_456_000
    priority: str = "high"
    max_replicas: int = 1
    worker_port: int = 8000
    worker_path: str = "/jobs/i2v"
    runtime_bindings: Mapping[str, str] = field(default_factory=dict)
    environment_variables: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for label, text_value in (
            ("queue name", self.queue_name),
            ("container group name", self.container_group_name),
        ):
            if not text_value or text_value != text_value.strip() or len(text_value) > 200:
                raise I2VSaladConfigurationError(f"I2V Salad {label} is invalid")
        if fullmatch(_PINNED_IMAGE_PATTERN, self.worker_image) is None:
            raise I2VSaladConfigurationError(
                "I2V worker image must be an immutable repository@sha256 reference"
            )
        if self.gpu_class_name != I2V_SALAD_GPU_CLASS_NAME:
            raise I2VSaladConfigurationError(
                f"I2V requires the exact {I2V_SALAD_GPU_CLASS_NAME} GPU class"
            )
        for label, numeric_value in (
            ("prefetch", self.prefetch),
            ("worker lease", self.worker_lease_seconds),
            ("CPU", self.cpu),
            ("memory", self.memory_mb),
            ("storage", self.storage_bytes),
            ("maximum replicas", self.max_replicas),
            ("worker port", self.worker_port),
        ):
            if (
                not isinstance(numeric_value, int)
                or isinstance(numeric_value, bool)
                or numeric_value <= 0
            ):
                raise I2VSaladConfigurationError(f"I2V Salad {label} must be positive")
        if self.warm_idle_seconds is not None and (
            not isinstance(self.warm_idle_seconds, int)
            or isinstance(self.warm_idle_seconds, bool)
            or self.warm_idle_seconds < 0
        ):
            raise I2VSaladConfigurationError("I2V warm idle seconds must be nonnegative or null")
        if self.priority not in {"low", "medium", "high", "batch"}:
            raise I2VSaladConfigurationError("I2V Salad priority is invalid")
        if not self.worker_path.startswith("/") or any(
            character.isspace() for character in self.worker_path
        ):
            raise I2VSaladConfigurationError("I2V worker path is invalid")
        for collection_name, collection in (
            ("runtime binding", self.runtime_bindings),
            ("environment variable", self.environment_variables),
        ):
            for key, value in collection.items():
                if (
                    not isinstance(key, str)
                    or not key
                    or key != key.strip()
                    or not isinstance(value, str)
                    or not value
                ):
                    raise I2VSaladConfigurationError(f"I2V Salad {collection_name} is invalid")

    def container_configuration(self) -> JSONObject:
        configuration: JSONObject = {
            "name": self.container_group_name,
            "display_name": "Image to video - RTX 5090",
            "container": {
                "image": self.worker_image,
                "resources": {
                    "cpu": self.cpu,
                    "memory": self.memory_mb,
                    "storage_amount": self.storage_bytes,
                    "gpu_classes": [str(self.gpu_class_id)],
                },
                "image_caching": True,
                "priority": self.priority,
            },
            "replicas": 0,
            "restart_policy": "on_failure",
            "startup_probe": _http_probe(path="/health", period=5, timeout=5, failure_threshold=20),
            "readiness_probe": _http_probe(path="/ready", period=5, timeout=3, failure_threshold=3),
            "liveness_probe": _http_probe(
                path="/health", period=30, timeout=5, failure_threshold=3
            ),
            "queue_connection": {
                "queue_name": self.queue_name,
                "path": self.worker_path,
                "port": self.worker_port,
            },
            # A started group stays warm until this controller explicitly applies
            # the configured idle policy.  This avoids provider-side scale-down
            # racing a sequence of queued generations.
            "queue_autoscaler": {
                "min_replicas": 1,
                "max_replicas": self.max_replicas,
                "desired_queue_length": 1,
                "polling_period": 15,
            },
        }
        if self.runtime_bindings:
            configuration["runtime_bindings"] = [
                {"name": name, "reference": reference}
                for name, reference in sorted(self.runtime_bindings.items())
            ]
        if self.environment_variables:
            container = configuration["container"]
            assert isinstance(container, dict)
            container["environment_variables"] = dict(sorted(self.environment_variables.items()))
        return configuration


class I2VSaladClient(Protocol):
    async def list_gpu_classes(self) -> tuple[SaladGpuClass, ...]: ...

    async def get_queue(self, queue_name: str) -> SaladQueue: ...

    async def create_queue(
        self,
        name: str,
        *,
        display_name: str | None = None,
        description: str | None = None,
    ) -> SaladQueue: ...

    async def get_container_group(self, container_group_name: str) -> SaladContainerGroup: ...

    async def list_container_groups(self) -> SaladContainerGroupPage: ...

    async def create_container_group(
        self, configuration: Mapping[str, JSONValue]
    ) -> SaladContainerGroup: ...

    async def update_container_group(
        self,
        container_group_name: str,
        patch: Mapping[str, JSONValue],
    ) -> SaladContainerGroup: ...

    async def list_container_group_instances(self, container_group_name: str) -> object: ...

    async def start_container_group(self, container_group_name: str) -> None: ...

    async def stop_container_group(self, container_group_name: str) -> None: ...

    async def create_job(
        self,
        queue_name: str,
        *,
        input: JSONValue,
        metadata: Mapping[str, JSONValue] | None = None,
        webhook: str | None = None,
    ) -> SaladQueueJob: ...

    async def get_job(self, queue_name: str, job_id: UUID | str) -> SaladQueueJob: ...

    async def list_jobs(
        self,
        queue_name: str,
        *,
        page: int = 1,
        page_size: int = SALAD_QUEUE_JOB_PAGE_SIZE,
    ) -> SaladQueueJobPage: ...

    async def cancel_job(self, queue_name: str, job_id: UUID | str) -> None: ...


@dataclass(frozen=True)
class I2VInfrastructureStep:
    mutation: I2VInfrastructureMutation
    queue: SaladQueue | None
    group: SaladContainerGroup | None

    @property
    def changed(self) -> bool:
        return self.mutation != I2VInfrastructureMutation.NONE


@dataclass(frozen=True)
class I2VProviderObservation:
    group_id: str
    state: I2VWorkerDeploymentState
    provider_status: str
    replicas: int
    instance_id: str | None
    machine_id: str | None
    instance_state: str | None
    ready: bool | None
    instances: tuple[JSONObject, ...]


def _http_probe(*, path: str, period: int, timeout: int, failure_threshold: int) -> JSONObject:
    return {
        "http": {"headers": [], "path": path, "port": 8000, "scheme": "http"},
        "initial_delay_seconds": 0,
        "period_seconds": period,
        "timeout_seconds": timeout,
        "success_threshold": 1,
        "failure_threshold": failure_threshold,
    }


def i2v_submission_metadata(
    *,
    job_id: UUID,
    attempt_id: UUID,
    submission_key: str,
    request_sha256: str,
) -> JSONObject:
    return {
        "schema": I2V_SALAD_SUBMISSION_SCHEMA,
        "i2v_job_id": str(job_id),
        "i2v_attempt_id": str(attempt_id),
        "submission_key": submission_key,
        "request_sha256": request_sha256,
    }


async def ensure_i2v_infrastructure_step(
    client: I2VSaladClient,
    config: I2VSaladConfig,
) -> I2VInfrastructureStep:
    """Inspect the deterministic infrastructure and perform at most one mutation."""

    await _require_exact_gpu(client, config)
    queue: SaladQueue | None
    try:
        queue = await client.get_queue(config.queue_name)
    except SaladAPIError as error:
        if error.status_code != 404:
            raise
        queue = await client.create_queue(
            config.queue_name,
            display_name="Image to video",
            description="Durable DaSiWa WAN 2.2 image-to-video queue",
        )
        return I2VInfrastructureStep(
            mutation=I2VInfrastructureMutation.QUEUE_CREATED,
            queue=queue,
            group=None,
        )
    if queue.name != config.queue_name:
        raise I2VSaladConflictError("Salad returned a different I2V queue")

    try:
        group = await client.get_container_group(config.container_group_name)
    except SaladAPIError as error:
        if error.status_code != 404:
            raise
        # A list is an ambiguity check: if a preceding create POST reached Salad
        # but its response was lost, never issue a second create under another ID.
        groups = await client.list_container_groups()
        matches = tuple(item for item in groups.items if item.name == config.container_group_name)
        if len(matches) > 1:
            raise I2VSaladConflictError("multiple deterministic I2V groups exist") from error
        if matches:
            group = matches[0]
        else:
            group = await client.create_container_group(config.container_configuration())
            return I2VInfrastructureStep(
                mutation=I2VInfrastructureMutation.GROUP_CREATED,
                queue=queue,
                group=group,
            )
    _validate_group_identity(group, config)
    return I2VInfrastructureStep(
        mutation=I2VInfrastructureMutation.NONE,
        queue=queue,
        group=group,
    )


async def observe_i2v_provider(
    client: I2VSaladClient,
    config: I2VSaladConfig,
    *,
    active_job_count: int,
) -> I2VProviderObservation:
    group = await client.get_container_group(config.container_group_name)
    _validate_group_identity(group, config)
    page = await client.list_container_group_instances(config.container_group_name)
    raw_instances = getattr(page, "instances", None)
    if not isinstance(raw_instances, tuple):
        raise SaladProtocolError("I2V container instance response is invalid")
    instances = tuple(raw_instances)
    if any(not isinstance(item, SaladContainerGroupInstance) for item in instances):
        raise SaladProtocolError("I2V container instance response is invalid")
    ordered = sorted(instances, key=_instance_preference, reverse=True)
    primary = ordered[0] if ordered else None
    state = _truthful_deployment_state(
        group,
        instances,
        active_job_count=active_job_count,
    )
    summaries: tuple[JSONObject, ...] = tuple(
        {
            "id": instance.id,
            "machine_id": instance.machine_id,
            "state": instance.state.value,
            "ready": instance.ready,
            "started": instance.started,
            "version": instance.version,
            "update_time": instance.update_time.isoformat(),
        }
        for instance in sorted(instances, key=lambda item: (item.update_time, item.id))
    )
    return I2VProviderObservation(
        group_id=str(group.id),
        state=state,
        provider_status=group.status,
        replicas=group.replicas,
        instance_id=primary.id if primary is not None else None,
        machine_id=primary.machine_id if primary is not None else None,
        instance_state=primary.state.value if primary is not None else None,
        ready=primary.ready if primary is not None else None,
        instances=summaries,
    )


async def find_i2v_submission(
    client: I2VSaladClient,
    *,
    queue_name: str,
    submission_key: str,
) -> SaladQueueJob | None:
    """Exhaustively scan the provider queue before an uncertain POST is repeated."""

    matches: list[SaladQueueJob] = []
    previous_page_ids: tuple[UUID, ...] | None = None
    page_number = 1
    while True:
        page = await client.list_jobs(
            queue_name,
            page=page_number,
            page_size=SALAD_QUEUE_JOB_PAGE_SIZE,
        )
        page_ids = tuple(job.id for job in page.items)
        if page_ids and page_ids == previous_page_ids:
            raise SaladProtocolError("Salad queue pagination repeated a page")
        previous_page_ids = page_ids
        matches.extend(
            job for job in page.items if job.metadata.get("submission_key") == submission_key
        )
        if len(page.items) < SALAD_QUEUE_JOB_PAGE_SIZE:
            break
        page_number += 1
    unique = {job.id: job for job in matches}
    if len(unique) > 1:
        raise I2VSaladConflictError("an I2V submission key matched multiple provider jobs")
    return next(iter(unique.values()), None)


def parse_i2v_worker_output(value: JSONValue) -> I2VOutputRegistration:
    if not isinstance(value, dict):
        raise SaladProtocolError("I2V worker output must be an object")
    payload: object = value
    if value.get("schema") == I2V_WORKER_OUTPUT_SCHEMA:
        payload = value.get("output")
    if not isinstance(payload, dict):
        raise SaladProtocolError("I2V worker output payload must be an object")
    try:
        return I2VOutputRegistration.model_validate(payload)
    except ValueError as error:
        raise SaladProtocolError("I2V worker output media contract is invalid") from error


def provider_job_has_started_inference(job: SaladQueueJob) -> bool:
    """Only Salad's RUNNING state is evidence that inference may have started."""

    return job.status in {
        SaladJobStatus.RUNNING,
        SaladJobStatus.SUCCEEDED,
        SaladJobStatus.FAILED,
    }


async def _require_exact_gpu(client: I2VSaladClient, config: I2VSaladConfig) -> None:
    classes = await client.list_gpu_classes()
    matches = tuple(item for item in classes if item.id == config.gpu_class_id)
    if len(matches) != 1 or matches[0].name != I2V_SALAD_GPU_CLASS_NAME:
        raise I2VSaladConfigurationError(
            f"configured GPU class is not the exact {I2V_SALAD_GPU_CLASS_NAME}; no fallback allowed"
        )


def _validate_group_identity(group: SaladContainerGroup, config: I2VSaladConfig) -> None:
    if group.name != config.container_group_name:
        raise I2VSaladConflictError("Salad returned a different I2V container group")
    container = group.raw.get("container")
    if not isinstance(container, dict):
        raise I2VSaladConflictError("I2V container group omitted its container contract")
    if container.get("image") != config.worker_image:
        raise I2VSaladConflictError("I2V worker image differs from the pinned image")
    resources = container.get("resources")
    if not isinstance(resources, dict) or resources.get("gpu_classes") != [
        str(config.gpu_class_id)
    ]:
        raise I2VSaladConflictError("I2V group is not pinned to the exact RTX 5090 class")
    queue_connection = group.raw.get("queue_connection")
    if (
        not isinstance(queue_connection, dict)
        or queue_connection.get("queue_name") != config.queue_name
    ):
        raise I2VSaladConflictError("I2V group is connected to the wrong queue")


def _instance_preference(instance: SaladContainerGroupInstance) -> tuple[int, int, object, str]:
    rank = {
        SaladContainerGroupInstanceState.STOPPING: 0,
        SaladContainerGroupInstanceState.ALLOCATING: 1,
        SaladContainerGroupInstanceState.DOWNLOADING: 2,
        SaladContainerGroupInstanceState.CREATING: 3,
        SaladContainerGroupInstanceState.RUNNING: 4,
    }[instance.state]
    return (rank, int(instance.ready is True), instance.update_time, instance.id)


def _truthful_deployment_state(
    group: SaladContainerGroup,
    instances: tuple[SaladContainerGroupInstance, ...],
    *,
    active_job_count: int,
) -> I2VWorkerDeploymentState:
    status = group.status.lower()
    if "fail" in status or "error" in status:
        return I2VWorkerDeploymentState.FAILED
    if any(instance.state == SaladContainerGroupInstanceState.STOPPING for instance in instances):
        return I2VWorkerDeploymentState.DRAINING
    ready = tuple(
        instance
        for instance in instances
        if instance.state == SaladContainerGroupInstanceState.RUNNING and instance.ready is True
    )
    if ready:
        return (
            I2VWorkerDeploymentState.BUSY
            if active_job_count > 0
            else I2VWorkerDeploymentState.READY
        )
    if any(
        instance.state
        in {SaladContainerGroupInstanceState.CREATING, SaladContainerGroupInstanceState.RUNNING}
        for instance in instances
    ):
        return I2VWorkerDeploymentState.STARTING
    if any(
        instance.state
        in {
            SaladContainerGroupInstanceState.ALLOCATING,
            SaladContainerGroupInstanceState.DOWNLOADING,
        }
        for instance in instances
    ):
        return I2VWorkerDeploymentState.PROVISIONING
    if status == "stopped" and group.replicas == 0:
        return I2VWorkerDeploymentState.STOPPED
    # A provider-level running/pending group with no ready instance is not ready.
    return I2VWorkerDeploymentState.PROVISIONING
