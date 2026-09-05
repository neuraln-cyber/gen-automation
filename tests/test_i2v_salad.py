from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from gen_automation.domain.i2v import I2VWorkerDeploymentState
from gen_automation.integrations.salad.errors import SaladAPIError
from gen_automation.integrations.salad.models import (
    SaladContainerGroup,
    SaladContainerGroupInstance,
    SaladContainerGroupInstancePage,
    SaladContainerGroupInstanceState,
    SaladContainerGroupPage,
    SaladContainerGroupState,
    SaladGpuClass,
    SaladJobStatus,
    SaladQueue,
    SaladQueueJob,
    SaladQueueJobPage,
)
from gen_automation.services.i2v_salad import (
    I2V_SALAD_GPU_CLASS_NAME,
    I2VInfrastructureMutation,
    I2VSaladConfig,
    I2VSaladConfigurationError,
    I2VSaladConflictError,
    ensure_i2v_infrastructure_step,
    find_i2v_submission,
    observe_i2v_provider,
    provider_job_has_started_inference,
)

_NOW = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)
_GPU_ID = UUID("11111111-1111-4111-8111-111111111111")
_QUEUE_ID = UUID("22222222-2222-4222-8222-222222222222")
_GROUP_ID = UUID("33333333-3333-4333-8333-333333333333")
_IMAGE = "ghcr.io/example/i2v@sha256:" + "a" * 64


def _config(**overrides: object) -> I2VSaladConfig:
    values: dict[str, object] = {
        "queue_name": "i2v-dasiwa-v1",
        "container_group_name": "i2v-dasiwa-5090-v1",
        "worker_image": _IMAGE,
        "gpu_class_id": _GPU_ID,
    }
    values.update(overrides)
    return I2VSaladConfig(**values)  # type: ignore[arg-type]


def _gpu(*, name: str = I2V_SALAD_GPU_CLASS_NAME) -> SaladGpuClass:
    return SaladGpuClass(
        id=_GPU_ID,
        name=name,
        prices=("0.50",),
        gpu_count=1,
        is_high_demand=True,
        max_ram=0,
        max_storage=0,
        max_vcpu=0,
        min_ram=0,
        min_storage=0,
        min_vcpu=0,
        raw={},
    )


def _queue() -> SaladQueue:
    return SaladQueue(
        id=_QUEUE_ID,
        name="i2v-dasiwa-v1",
        display_name="I2V",
        description=None,
        current_queue_length=0,
        container_groups=(),
        create_time=_NOW,
        update_time=_NOW,
    )


def _group(*, status: str = "running", replicas: int = 1) -> SaladContainerGroup:
    return SaladContainerGroup(
        id=_GROUP_ID,
        name="i2v-dasiwa-5090-v1",
        display_name="I2V",
        replicas=replicas,
        pending_change=False,
        version=1,
        current_state=SaladContainerGroupState(
            status=status,
            description="",
            allocating_count=0,
            creating_count=0,
            running_count=replicas,
            stopping_count=0,
            start_time=_NOW,
            finish_time=None,
        ),
        create_time=_NOW,
        update_time=_NOW,
        raw={
            "container": {
                "image": _IMAGE,
                "resources": {"gpu_classes": [str(_GPU_ID)]},
            },
            "priority": "high",
            "queue_connection": {"queue_name": "i2v-dasiwa-v1"},
            "queue_autoscaler": {
                "min_replicas": 1,
                "max_replicas": 1,
                "desired_queue_length": 1,
                "polling_period": 15,
                "max_upscale_per_minute": 1,
                "max_downscale_per_minute": 1,
            },
            "readiness_probe": {
                "http": {
                    "headers": [],
                    "path": "/ready",
                    "port": 8000,
                    "scheme": "http",
                },
                "initial_delay_seconds": 0,
                "period_seconds": 5,
                "timeout_seconds": 3,
                "success_threshold": 1,
                "failure_threshold": 3,
            },
        },
    )


def _instance(
    state: SaladContainerGroupInstanceState,
    *,
    instance_id: str,
    machine_id: str,
    ready: bool | None,
) -> SaladContainerGroupInstance:
    return SaladContainerGroupInstance(
        id=instance_id,
        machine_id=machine_id,
        state=state,
        update_time=_NOW,
        version=1,
        ready=ready,
        started=state == SaladContainerGroupInstanceState.RUNNING,
    )


def _job(*, submission_key: str, status: SaladJobStatus = SaladJobStatus.PENDING) -> SaladQueueJob:
    return SaladQueueJob(
        id=uuid4(),
        input={},
        status=status,
        events=(),
        create_time=_NOW,
        update_time=_NOW,
        metadata={"submission_key": submission_key},
        webhook=None,
        output=None,
    )


class FakeSalad:
    def __init__(self) -> None:
        self.gpus = (_gpu(),)
        self.queue: SaladQueue | None = _queue()
        self.group: SaladContainerGroup | None = _group()
        self.groups: tuple[SaladContainerGroup, ...] = ()
        self.instances: tuple[SaladContainerGroupInstance, ...] = ()
        self.jobs: tuple[SaladQueueJob, ...] = ()
        self.mutations: list[str] = []
        self.created_configuration: dict[str, object] | None = None
        self.updated_patches: list[dict[str, object]] = []
        self.omit_autoscaler_on_create = False
        self.omit_autoscaler_on_update = False

    async def list_gpu_classes(self) -> tuple[SaladGpuClass, ...]:
        return self.gpus

    async def get_queue(self, _name: str) -> SaladQueue:
        if self.queue is None:
            raise _not_found()
        return self.queue

    async def create_queue(self, *_args: object, **_kwargs: object) -> SaladQueue:
        self.mutations.append("create_queue")
        self.queue = _queue()
        return self.queue

    async def get_container_group(self, _name: str) -> SaladContainerGroup:
        if self.group is None:
            raise _not_found()
        return self.group

    async def list_container_groups(self) -> SaladContainerGroupPage:
        return SaladContainerGroupPage(items=self.groups)

    async def create_container_group(self, configuration: object) -> SaladContainerGroup:
        assert isinstance(configuration, dict)
        assert configuration["container"]["resources"]["gpu_classes"] == [str(_GPU_ID)]
        self.created_configuration = configuration
        self.mutations.append("create_group")
        self.group = _group(status="stopped", replicas=0)
        if self.omit_autoscaler_on_create:
            self.group.raw.pop("queue_autoscaler")
        return self.group

    async def update_container_group(
        self,
        _name: str,
        patch: object,
    ) -> SaladContainerGroup:
        assert self.group is not None
        assert isinstance(patch, dict)
        self.updated_patches.append(patch)
        self.mutations.append("update_group")
        container = patch.get("container")
        if isinstance(container, dict) and "priority" in container:
            self.group.raw["priority"] = container["priority"]
        autoscaler = patch.get("queue_autoscaler")
        if isinstance(autoscaler, dict):
            self.group.raw["queue_autoscaler"] = dict(autoscaler)
        readiness_probe = patch.get("readiness_probe")
        if isinstance(readiness_probe, dict):
            self.group.raw["readiness_probe"] = dict(readiness_probe)
        if self.omit_autoscaler_on_update:
            self.group.raw.pop("queue_autoscaler", None)
        return self.group

    async def list_container_group_instances(self, _name: str) -> SaladContainerGroupInstancePage:
        return SaladContainerGroupInstancePage(instances=self.instances)

    async def list_jobs(
        self, _name: str, *, page: int = 1, page_size: int = 25
    ) -> SaladQueueJobPage:
        start = (page - 1) * page_size
        return SaladQueueJobPage(items=self.jobs[start : start + page_size])


def _not_found() -> SaladAPIError:
    return SaladAPIError(
        status_code=404,
        message="not found",
        response_body="",
        request_id=None,
    )


def test_i2v_salad_config_has_no_small_limits_but_requires_exact_pinning() -> None:
    config = _config(prefetch=50_000, worker_lease_seconds=10**9, max_replicas=500)
    assert config.prefetch == 50_000
    assert config.worker_lease_seconds == 10**9
    assert config.max_replicas == 500
    assert config.queue_autoscaler_configuration()["max_upscale_per_minute"] == 100
    assert config.queue_autoscaler_configuration()["max_downscale_per_minute"] == 100
    assert config.container_configuration()["container"]["resources"]["gpu_classes"] == [
        str(_GPU_ID)
    ]
    with pytest.raises(I2VSaladConfigurationError, match="exact"):
        _config(gpu_class_name="RTX 5090 Laptop (24 GB)")
    with pytest.raises(I2VSaladConfigurationError, match="immutable"):
        _config(worker_image="ghcr.io/example/i2v:latest")


def test_i2v_salad_ready_idle_defaults_to_fifteen_minutes_with_explicit_override() -> None:
    assert _config().warm_idle_seconds == 900
    assert _config(warm_idle_seconds=36_000).warm_idle_seconds == 36_000
    assert _config(warm_idle_seconds=None).warm_idle_seconds is None


async def test_infrastructure_reconcile_performs_only_one_mutation_and_recovers_create() -> None:
    client = FakeSalad()
    client.queue = None
    client.group = None
    first = await ensure_i2v_infrastructure_step(client, _config())
    assert first.mutation == I2VInfrastructureMutation.QUEUE_CREATED
    assert client.mutations == ["create_queue"]

    second = await ensure_i2v_infrastructure_step(client, _config())
    assert second.mutation == I2VInfrastructureMutation.GROUP_CREATED
    assert client.mutations == ["create_queue", "create_group"]
    assert client.created_configuration is not None
    assert client.created_configuration["autostart_policy"] is False
    container = client.created_configuration["container"]
    assert isinstance(container, dict) and container["priority"] == "high"
    assert client.created_configuration["queue_autoscaler"] == {
        "min_replicas": 1,
        "max_replicas": 1,
        "desired_queue_length": 1,
        "polling_period": 15,
        "max_upscale_per_minute": 1,
        "max_downscale_per_minute": 1,
    }

    client.group = None
    client.groups = (_group(),)
    recovered = await ensure_i2v_infrastructure_step(client, _config())
    assert recovered.mutation == I2VInfrastructureMutation.NONE
    assert recovered.group is client.groups[0]
    assert client.mutations == ["create_queue", "create_group"]


async def test_reconcile_repairs_priority_and_autoscaler_before_use() -> None:
    client = FakeSalad()
    assert client.group is not None
    client.group.raw["priority"] = "low"
    client.group.raw["queue_autoscaler"] = {
        "min_replicas": 0,
        "max_replicas": 9,
        "desired_queue_length": 7,
        "polling_period": 30,
    }

    repaired = await ensure_i2v_infrastructure_step(client, _config())

    assert repaired.mutation == I2VInfrastructureMutation.GROUP_CONTRACT_REPAIRED
    assert client.updated_patches == [
        {
            "container": {"priority": "high"},
            "queue_autoscaler": {
                "min_replicas": 1,
                "max_replicas": 1,
                "desired_queue_length": 1,
                "polling_period": 15,
                "max_upscale_per_minute": 1,
                "max_downscale_per_minute": 1,
            },
        }
    ]


async def test_reconcile_restores_baseline_readiness_probe_after_capability_rollback() -> None:
    client = FakeSalad()
    assert client.group is not None
    client.group.raw["readiness_probe"] = {
        "http": {
            "path": "/ready/capability/" + "a" * 64 + "/" + "b" * 64 + "/" + "c" * 40,
            "port": 8000,
            "scheme": "http",
            "headers": [],
        },
        "initial_delay_seconds": 0,
        "period_seconds": 5,
        "timeout_seconds": 3,
        "success_threshold": 1,
        "failure_threshold": 3,
    }

    repaired = await ensure_i2v_infrastructure_step(client, _config())

    assert repaired.mutation == I2VInfrastructureMutation.GROUP_CONTRACT_REPAIRED
    assert client.updated_patches == [
        {
            "readiness_probe": {
                "http": {
                    "path": "/ready",
                    "port": 8000,
                    "scheme": "http",
                    "headers": [],
                },
                "initial_delay_seconds": 0,
                "period_seconds": 5,
                "timeout_seconds": 3,
                "success_threshold": 1,
                "failure_threshold": 3,
            }
        }
    ]


async def test_create_and_update_readback_omit_autoscaler_fail_closed() -> None:
    client = FakeSalad()
    client.group = None
    client.omit_autoscaler_on_create = True
    with pytest.raises(I2VSaladConflictError, match="priority or queue autoscaler"):
        await ensure_i2v_infrastructure_step(client, _config())

    client.omit_autoscaler_on_update = True
    with pytest.raises(I2VSaladConflictError, match="priority or queue autoscaler"):
        await ensure_i2v_infrastructure_step(client, _config())
    assert client.mutations == ["create_group", "update_group"]


async def test_exact_gpu_name_has_no_fallback() -> None:
    client = FakeSalad()
    client.gpus = (_gpu(name="NVIDIA GeForce RTX 4090"),)
    with pytest.raises(I2VSaladConfigurationError, match="no fallback"):
        await ensure_i2v_infrastructure_step(client, _config())


async def test_truthful_instance_states_and_reallocation_summary() -> None:
    client = FakeSalad()
    client.instances = (
        _instance(
            SaladContainerGroupInstanceState.DOWNLOADING,
            instance_id="old-instance",
            machine_id="old-machine",
            ready=False,
        ),
    )
    downloading = await observe_i2v_provider(client, _config(), active_job_count=1)
    assert downloading.state == I2VWorkerDeploymentState.PROVISIONING
    assert downloading.ready is False

    client.instances = (
        client.instances[0],
        _instance(
            SaladContainerGroupInstanceState.RUNNING,
            instance_id="new-instance",
            machine_id="new-machine",
            ready=True,
        ),
    )
    ready = await observe_i2v_provider(client, _config(), active_job_count=1)
    assert ready.state == I2VWorkerDeploymentState.BUSY
    assert ready.instance_id == "new-instance"
    assert {item["machine_id"] for item in ready.instances} == {"old-machine", "new-machine"}


async def test_submission_recovery_scans_all_pages_before_resubmit() -> None:
    client = FakeSalad()
    wanted = _job(submission_key="wanted")
    client.jobs = (
        *(_job(submission_key=f"other-{index}") for index in range(27)),
        wanted,
    )
    found = await find_i2v_submission(
        client,
        queue_name="i2v-dasiwa-v1",
        submission_key="wanted",
    )
    assert found is wanted


def test_pending_is_never_evidence_of_started_inference() -> None:
    assert not provider_job_has_started_inference(_job(submission_key="x"))
    assert provider_job_has_started_inference(
        _job(submission_key="x", status=SaladJobStatus.RUNNING)
    )
