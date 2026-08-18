from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, func, select

from gen_automation.db.models import (
    AuditEvent,
    ProviderBudgetGuard,
    ProviderSpendEntry,
    SaladDeployment,
)
from gen_automation.db.session import Database
from gen_automation.domain.enums import (
    BudgetState,
    DesiredDeploymentState,
    SaladDeploymentPurpose,
    SaladDeploymentState,
    SpendEntryType,
)
from gen_automation.domain.runtime_bindings import (
    SALAD_WORKER_RUNTIME_BINDING_REFERENCES,
    WORKER_MODEL_MANIFEST_JSON_BINDING,
)
from gen_automation.integrations.salad.errors import (
    SaladAPIError,
    SaladProtocolError,
    SaladRateLimitError,
    SaladTransportError,
)
from gen_automation.integrations.salad.models import (
    JSONValue,
    SaladContainerGroup,
    SaladContainerGroupInstance,
    SaladContainerGroupInstancePage,
    SaladContainerGroupInstanceState,
    SaladContainerGroupState,
    SaladQueue,
)
from gen_automation.services import salad_deployments as deployment_service
from gen_automation.services.budgets import BudgetError, ensure_budget_guard, record_spend_entry
from gen_automation.services.runtime_secrets import RuntimeSecretResolver
from gen_automation.services.salad_deployments import (
    DeploymentAction,
    SaladDeploymentNotFoundError,
    SaladDeploymentValidationError,
    _as_utc,
    _container_group_payload,
    _group_configuration_drift,
    _observed_replicas,
    _parse_runtime_bindings,
    _remote_drift_code,
    _validate_local_deployment,
    deterministic_provider_name,
    ensure_container_group_queue_admission,
    provision_deployment_step,
    reconcile_deployment,
    refresh_container_group_runtime,
)

NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
IMAGE_DIGEST = "registry.example.test/worker@sha256:" + "c" * 64
CONFIG_SHA256 = "a" * 64
QUEUE_ID = UUID("7dcd6922-50e9-4d56-89b5-91cde26f0211")
GROUP_ID = UUID("ab3a4591-efc3-46c0-b06a-3d820c0ec100")
INSTANCE_ID = "instance-creator-1"
LIVE_VALUE = "resolved-value-that-must-not-be-persisted"
STARTUP_PROBE: dict[str, JSONValue] = {
    "http": {
        "headers": [],
        "path": "/health",
        "port": 8000,
        "scheme": "http",
    },
    "initial_delay_seconds": 0,
    "period_seconds": 5,
    "timeout_seconds": 5,
    "success_threshold": 1,
    "failure_threshold": 20,
}
READINESS_PROBE: dict[str, JSONValue] = {
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
}
LIVENESS_PROBE: dict[str, JSONValue] = {
    "http": {
        "headers": [],
        "path": "/health",
        "port": 8000,
        "scheme": "http",
    },
    "initial_delay_seconds": 0,
    "period_seconds": 30,
    "timeout_seconds": 5,
    "success_threshold": 1,
    "failure_threshold": 3,
}


def test_startup_probe_detects_bootstrap_responder_promptly_with_live_safe_limits() -> None:
    assert STARTUP_PROBE["initial_delay_seconds"] == 0
    assert STARTUP_PROBE["period_seconds"] == 5
    assert STARTUP_PROBE["failure_threshold"] == 20


def api_error(status_code: int) -> SaladAPIError:
    return SaladAPIError(
        status_code=status_code,
        message="provider error",
        response_body="safe",
        request_id="request-id",
    )


def make_queue(
    name: str,
    *,
    queue_id: UUID = QUEUE_ID,
    length: int = 0,
    group_name: str | None = None,
    group_id: UUID = GROUP_ID,
) -> SaladQueue:
    return SaladQueue(
        id=queue_id,
        name=name,
        display_name=name,
        description=None,
        current_queue_length=length,
        container_groups=(
            ({"id": str(group_id), "name": group_name},) if group_name is not None else ()
        ),
        create_time=NOW,
        update_time=NOW,
    )


def make_group(
    name: str,
    queue_name: str,
    *,
    group_id: UUID = GROUP_ID,
    status: str = "running",
    description: str | None = None,
    replicas: int = 0,
    running: int = 0,
    pending_change: bool = False,
    version: int = 1,
    image: str = IMAGE_DIGEST,
    start_time: datetime | None = NOW,
    finish_time: datetime | None = None,
    autoscaler: Mapping[str, JSONValue] | None = None,
) -> SaladContainerGroup:
    raw: dict[str, JSONValue] = {
        "id": str(group_id),
        "name": name,
        "container": {
            "image": image,
            "resources": {
                "cpu": 4,
                "memory": 16384,
                "gpu_classes": ["gpu-class"],
            },
            "priority": "low",
            "image_caching": True,
        },
        "autostart_policy": True,
        "priority": "low",
        "queue_connection": {
            "queue_name": queue_name,
            "path": "/jobs/generate",
            "port": 8000,
        },
        "restart_policy": "on_failure",
        "startup_probe": deepcopy(STARTUP_PROBE),
        "readiness_probe": deepcopy(READINESS_PROBE),
        "liveness_probe": deepcopy(LIVENESS_PROBE),
        "queue_autoscaler": dict(
            autoscaler
            or {
                "min_replicas": 0,
                "max_replicas": 1,
                "desired_queue_length": 1,
                "polling_period": 30,
            }
        ),
    }
    return SaladContainerGroup(
        id=group_id,
        name=name,
        display_name=name,
        replicas=replicas,
        pending_change=pending_change,
        version=version,
        current_state=SaladContainerGroupState(
            status=status,
            description=status if description is None else description,
            allocating_count=0,
            creating_count=0,
            running_count=running,
            stopping_count=0,
            start_time=start_time,
            finish_time=finish_time,
        ),
        create_time=NOW,
        update_time=NOW,
        raw=raw,
    )


class FakeClient:
    def __init__(self) -> None:
        self.queues: dict[str, SaladQueue] = {}
        self.groups: dict[str, SaladContainerGroup] = {}
        self.create_queue_error: Exception | None = None
        self.create_group_error: Exception | None = None
        self.update_group_error: Exception | None = None
        self.start_error: Exception | None = None
        self.stop_error: Exception | None = None
        self.get_queue_error: Exception | None = None
        self.get_group_error: Exception | None = None
        self.list_instances_error: Exception | None = None
        self.get_group_results: list[SaladContainerGroup] = []
        self.instance_pages: dict[str, SaladContainerGroupInstancePage] = {}
        self.last_group: SaladContainerGroup | None = None
        self.update_group_result: SaladContainerGroup | None = None
        self.created_queue_names: list[str] = []
        self.created_group_payloads: list[dict[str, JSONValue]] = []
        self.updated_group_patches: list[dict[str, JSONValue]] = []
        self.start_names: list[str] = []
        self.stop_names: list[str] = []
        self.calls: list[tuple[str, str]] = []

    async def create_queue(
        self,
        name: str,
        *,
        display_name: str | None = None,
        description: str | None = None,
    ) -> SaladQueue:
        del display_name, description
        self.calls.append(("create_queue", name))
        self.created_queue_names.append(name)
        if self.create_queue_error is not None:
            raise self.create_queue_error
        queue = make_queue(name)
        self.queues[name] = queue
        return queue

    async def get_queue(self, queue_name: str) -> SaladQueue:
        self.calls.append(("get_queue", queue_name))
        if self.get_queue_error is not None:
            raise self.get_queue_error
        try:
            return self.queues[queue_name]
        except KeyError:
            raise api_error(404) from None

    async def create_container_group(
        self,
        configuration: Mapping[str, JSONValue],
    ) -> SaladContainerGroup:
        name = configuration["name"]
        assert isinstance(name, str)
        self.calls.append(("create_group", name))
        self.created_group_payloads.append(dict(configuration))
        if self.create_group_error is not None:
            raise self.create_group_error
        connection = configuration["queue_connection"]
        assert isinstance(connection, dict)
        queue_name = connection["queue_name"]
        assert isinstance(queue_name, str)
        container = configuration["container"]
        assert isinstance(container, dict)
        image = container["image"]
        assert isinstance(image, str)
        group = make_group(name, queue_name, image=image)
        self.groups[name] = group
        queue = self.queues.get(queue_name)
        if queue is not None:
            self.queues[queue_name] = make_queue(
                queue_name,
                queue_id=queue.id,
                length=queue.current_queue_length,
                group_name=name,
                group_id=group.id,
            )
        return group

    async def get_container_group(self, container_group_name: str) -> SaladContainerGroup:
        self.calls.append(("get_group", container_group_name))
        if self.get_group_error is not None:
            raise self.get_group_error
        if self.get_group_results:
            group = self.get_group_results.pop(0)
            self.last_group = group
            return group
        try:
            group = self.groups[container_group_name]
            self.last_group = group
            return group
        except KeyError:
            raise api_error(404) from None

    async def list_container_group_instances(
        self,
        container_group_name: str,
    ) -> SaladContainerGroupInstancePage:
        self.calls.append(("list_instances", container_group_name))
        if self.list_instances_error is not None:
            raise self.list_instances_error
        explicit = self.instance_pages.get(container_group_name)
        if explicit is not None:
            return explicit
        group = self.last_group or self.groups.get(container_group_name)
        if group is None or group.current_state.running_count == 0:
            return SaladContainerGroupInstancePage(instances=())
        return SaladContainerGroupInstancePage(
            instances=(
                SaladContainerGroupInstance(
                    id=INSTANCE_ID,
                    machine_id="machine-creator-1",
                    state=SaladContainerGroupInstanceState.RUNNING,
                    update_time=group.current_state.start_time or NOW,
                    version=group.version,
                ),
            )
        )

    async def update_container_group(
        self,
        container_group_name: str,
        patch: Mapping[str, JSONValue],
    ) -> SaladContainerGroup:
        self.calls.append(("update_group", container_group_name))
        self.updated_group_patches.append(dict(patch))
        if self.update_group_error is not None:
            raise self.update_group_error
        if self.update_group_result is not None:
            return self.update_group_result
        try:
            return self.groups[container_group_name]
        except KeyError:
            raise api_error(404) from None

    async def stop_container_group(self, container_group_name: str) -> None:
        self.calls.append(("stop_group", container_group_name))
        self.stop_names.append(container_group_name)
        if self.stop_error is not None:
            raise self.stop_error

    async def start_container_group(self, container_group_name: str) -> None:
        self.calls.append(("start_group", container_group_name))
        self.start_names.append(container_group_name)
        if self.start_error is not None:
            raise self.start_error


class FakeResolver(RuntimeSecretResolver):
    def __init__(self, values: Mapping[str, str]) -> None:
        self.values = values
        self.requests: list[tuple[str, ...]] = []

    async def resolve_many(self, bindings: Mapping[str, str]) -> Mapping[str, str]:
        self.requests.append(tuple(bindings))
        return {name: self.values[name] for name in bindings if name in self.values}

    async def aclose(self) -> None:
        return None


@dataclass(frozen=True)
class DeploymentContext:
    database: Database
    deployment_id: UUID


def provider_configuration(*, with_binding: bool = False) -> dict[str, object]:
    value: dict[str, object] = {
        "container": {
            "resources": {
                "cpu": 4,
                "memory": 16384,
                "gpu_classes": ["gpu-class"],
            },
            "priority": "low",
        },
        "replicas": 0,
        "queue_connection": {},
        "queue_autoscaler": {"polling_period": 30},
    }
    if with_binding:
        value["runtime_bindings"] = [
            {
                "name": WORKER_MODEL_MANIFEST_JSON_BINDING,
                "reference": SALAD_WORKER_RUNTIME_BINDING_REFERENCES[
                    WORKER_MODEL_MANIFEST_JSON_BINDING
                ],
            }
        ]
    return value


def unpersisted_deployment(configuration: object | None = None) -> SaladDeployment:
    queue_name, group_name = remote_names()
    return SaladDeployment(
        version_no=1,
        config_sha256=CONFIG_SHA256,
        provider_configuration=(
            provider_configuration() if configuration is None else configuration  # type: ignore[arg-type]
        ),
        worker_image_digest=IMAGE_DIGEST,
        organization_name="creator-org",
        project_name="production",
        queue_name=queue_name,
        container_group_name=group_name,
        state=SaladDeploymentState.PLANNED,
        desired_state=DesiredDeploymentState.ACTIVE,
        is_current=True,
        min_replicas=0,
        max_replicas=1,
        desired_queue_length=1,
        max_hourly_cost_microusd=3_600_000,
    )


@pytest.fixture
async def deployment_context(tmp_path: Path) -> AsyncIterator[DeploymentContext]:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'deployments.db').as_posix()}")
    await database.create_schema()
    async with database.sessions() as session:
        deployment = SaladDeployment(
            version_no=1,
            config_sha256=CONFIG_SHA256,
            provider_configuration=provider_configuration(),
            worker_image_digest=IMAGE_DIGEST,
            organization_name="creator-org",
            project_name="production",
            queue_name="generation",
            container_group_name="worker",
            state=SaladDeploymentState.PLANNED,
            desired_state=DesiredDeploymentState.ACTIVE,
            is_current=True,
            min_replicas=0,
            max_replicas=1,
            desired_queue_length=1,
            max_hourly_cost_microusd=3_600_000,
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(deployment)
        await ensure_budget_guard(
            session,
            provider="salad",
            daily_limit_usd=Decimal("100"),
            monthly_limit_usd=Decimal("1000"),
            now=NOW,
        )
        await session.commit()
        deployment_id = deployment.id
    try:
        yield DeploymentContext(database=database, deployment_id=deployment_id)
    finally:
        await database.dispose()


def remote_names() -> tuple[str, str]:
    return (
        deterministic_provider_name(
            "generation",
            version_no=1,
            config_sha256=CONFIG_SHA256,
        ),
        deterministic_provider_name(
            "worker",
            version_no=1,
            config_sha256=CONFIG_SHA256,
        ),
    )


def test_deterministic_names_are_dns_safe_bounded_and_idempotent() -> None:
    result = deterministic_provider_name(
        " 9 / My Very Long Queue " * 10,
        version_no=42,
        config_sha256=CONFIG_SHA256,
    )
    replay = deterministic_provider_name(
        result,
        version_no=42,
        config_sha256=CONFIG_SHA256,
    )

    assert result == replay
    assert len(result) <= 63
    assert result.startswith("resource-9-my")
    assert result.endswith("-v42-aaaaaaaaaa")
    with pytest.raises(SaladDeploymentValidationError, match="version"):
        deterministic_provider_name("queue", version_no=0, config_sha256=CONFIG_SHA256)
    with pytest.raises(SaladDeploymentValidationError, match="hash"):
        deterministic_provider_name("queue", version_no=1, config_sha256="invalid")


def test_safe_provider_status_allowlists_and_bounds_untrusted_provider_text() -> None:
    queue_name, group_name = remote_names()
    untrusted_text = "user:SUPERSECRET@registry.example.test/private?token=opaque"
    status = deployment_service._safe_provider_status(
        make_queue(queue_name, length=10**200),
        make_group(
            group_name,
            queue_name,
            status=untrusted_text,
            description=f"Downloading image from {untrusted_text} at 4.7%",
            pending_change=True,
        ),
    )

    assert status == ("queue=999999999+;group=unknown;pending=1;phase=image_pull;progress=4")
    assert len(status) <= 100
    assert untrusted_text not in status


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("configuration", "message"),
    [
        ([], "must be an object"),
        (
            {
                **provider_configuration(),
                "container": {"environment_variables": {"VALUE": "plaintext"}},
            },
            "must not be stored",
        ),
        (
            {
                **provider_configuration(),
                "container": {"environmentVariables": {"VALUE": "plaintext"}},
            },
            "must not be stored",
        ),
        (
            {**provider_configuration(), "name": "another-group"},
            "name conflicts",
        ),
        (
            {**provider_configuration(), "replicas": 1},
            "replicas must be zero",
        ),
        (
            {**provider_configuration(), "container": None},
            "container is required",
        ),
        (
            {
                **provider_configuration(),
                "container": {"image": "registry.example/worker:latest"},
            },
            "immutable digest",
        ),
        (
            {**provider_configuration(), "queue_connection": None},
            "queue_connection is required",
        ),
        (
            {
                **provider_configuration(),
                "queue_connection": {"path": "relative", "port": 8080},
            },
            r"path must be /jobs/generate",
        ),
        (
            {
                **provider_configuration(),
                "queue_connection": {"path": "/jobs/generate", "port": 0},
            },
            "port must be 8000",
        ),
        (
            {
                **provider_configuration(),
                "queue_connection": {
                    "path": "/jobs/generate",
                    "port": 8000,
                    "queue_name": "wrong",
                },
            },
            "conflicts with deployment queue",
        ),
        (
            {**provider_configuration(), "restart_policy": "always"},
            "restart_policy must be on_failure",
        ),
        (
            {
                **provider_configuration(),
                "startup_probe": {
                    **STARTUP_PROBE,
                    "failure_threshold": 3,
                },
            },
            "startup_probe conflicts",
        ),
        (
            {**provider_configuration(), "readiness_probe": None},
            "readiness_probe conflicts",
        ),
        (
            {
                **provider_configuration(),
                "liveness_probe": {
                    **LIVENESS_PROBE,
                    "http": {
                        "headers": [],
                        "path": "/ready",
                        "port": 8000,
                        "scheme": "http",
                    },
                },
            },
            "liveness_probe conflicts",
        ),
        (
            {**provider_configuration(), "queue_autoscaler": []},
            "queue_autoscaler must be an object",
        ),
        (
            {
                **provider_configuration(),
                "queue_autoscaler": {"min_replicas": 1},
            },
            "min_replicas conflicts",
        ),
        (
            {
                **provider_configuration(),
                "queue_autoscaler": {"max_replicas": True},
            },
            "max_replicas conflicts",
        ),
    ],
)
async def test_container_payload_rejects_unsafe_or_conflicting_configuration(
    configuration: object,
    message: str,
) -> None:
    with pytest.raises(SaladDeploymentValidationError, match=message):
        await _container_group_payload(unpersisted_deployment(configuration), None)


@pytest.mark.parametrize(
    "value",
    [
        "not-an-array",
        ["not-an-object"],
        [{"name": "VALUE"}],
        [{"name": "lowercase", "reference": "vault://value"}],
        [
            {
                "name": WORKER_MODEL_MANIFEST_JSON_BINDING,
                "reference": "deployment-config://salad-worker/wrong",
            }
        ],
        [
            {"name": "VALUE", "reference": "vault://same"},
            {"name": "VALUE", "reference": "vault://other"},
        ],
        [
            {"name": "FIRST", "reference": "vault://same"},
            {"name": "SECOND", "reference": "vault://same"},
        ],
    ],
)
def test_runtime_binding_schema_is_strict(value: object) -> None:
    with pytest.raises(SaladDeploymentValidationError, match=r"runtime[_ ]bind"):
        _parse_runtime_bindings(value)


@pytest.mark.asyncio
async def test_runtime_binding_requires_resolver_and_complete_resolution() -> None:
    configuration = provider_configuration(with_binding=True)
    deployment = unpersisted_deployment(configuration)
    with pytest.raises(SaladDeploymentValidationError, match="require a secret resolver"):
        await _container_group_payload(deployment, None)
    resolver = FakeResolver({})
    with pytest.raises(SaladDeploymentValidationError, match="could not be resolved"):
        await _container_group_payload(deployment, resolver)


@pytest.mark.asyncio
async def test_refresh_container_group_runtime_injects_only_resolved_ephemeral_values() -> None:
    configuration = provider_configuration(with_binding=True)
    container_configuration = configuration["container"]
    assert isinstance(container_configuration, dict)
    container_configuration["image_caching"] = True
    deployment = unpersisted_deployment(configuration)
    deployment.provider_container_group_id = str(GROUP_ID)
    deployment.state = SaladDeploymentState.ACTIVE
    client = FakeClient()
    preflight = make_group(
        deployment.container_group_name,
        deployment.queue_name,
    )
    applied = make_group(
        deployment.container_group_name,
        deployment.queue_name,
        version=2,
    )
    client.groups[deployment.container_group_name] = applied
    client.get_group_results = [preflight, applied]
    client.update_group_result = applied
    resolver = FakeResolver({WORKER_MODEL_MANIFEST_JSON_BINDING: LIVE_VALUE})

    group = await refresh_container_group_runtime(deployment, client, resolver)

    assert group.id == GROUP_ID
    assert client.updated_group_patches == [
        {
            "container": {
                "image": IMAGE_DIGEST,
                "resources": {
                    "cpu": 4,
                    "memory": 16384,
                    "gpu_classes": ["gpu-class"],
                },
                "priority": "low",
                "image_caching": True,
                "environment_variables": {
                    WORKER_MODEL_MANIFEST_JSON_BINDING: LIVE_VALUE,
                },
            }
        }
    ]
    assert LIVE_VALUE not in repr(deployment.provider_configuration)
    assert resolver.requests == [(WORKER_MODEL_MANIFEST_JSON_BINDING,)]


@pytest.mark.asyncio
async def test_refresh_waits_for_pending_group_version_to_apply() -> None:
    deployment = unpersisted_deployment(provider_configuration(with_binding=True))
    deployment.provider_container_group_id = str(GROUP_ID)
    deployment.state = SaladDeploymentState.ACTIVE
    preflight = make_group(deployment.container_group_name, deployment.queue_name)
    pending = make_group(
        deployment.container_group_name,
        deployment.queue_name,
        pending_change=True,
        version=2,
    )
    applied = make_group(
        deployment.container_group_name,
        deployment.queue_name,
        version=2,
    )
    client = FakeClient()
    client.groups[deployment.container_group_name] = applied
    client.get_group_results = [preflight, pending, applied]
    client.update_group_result = pending

    result = await refresh_container_group_runtime(
        deployment,
        client,
        FakeResolver({WORKER_MODEL_MANIFEST_JSON_BINDING: LIVE_VALUE}),
        convergence_timeout_seconds=1,
        poll_interval_seconds=0,
    )

    assert result is applied
    assert [call[0] for call in client.calls] == [
        "get_group",
        "update_group",
        "get_group",
        "get_group",
    ]


@pytest.mark.asyncio
async def test_refresh_accepts_provider_patch_response_before_version_increment() -> None:
    deployment = unpersisted_deployment(provider_configuration(with_binding=True))
    deployment.provider_container_group_id = str(GROUP_ID)
    deployment.state = SaladDeploymentState.ACTIVE
    preflight = make_group(deployment.container_group_name, deployment.queue_name)
    accepted = make_group(
        deployment.container_group_name,
        deployment.queue_name,
        pending_change=True,
    )
    applied = make_group(
        deployment.container_group_name,
        deployment.queue_name,
        version=2,
    )
    client = FakeClient()
    client.groups[deployment.container_group_name] = applied
    client.get_group_results = [preflight, accepted, applied]
    client.update_group_result = accepted

    result = await refresh_container_group_runtime(
        deployment,
        client,
        FakeResolver({WORKER_MODEL_MANIFEST_JSON_BINDING: LIVE_VALUE}),
        convergence_timeout_seconds=1,
        poll_interval_seconds=0,
    )

    assert result is applied
    assert [call[0] for call in client.calls] == [
        "get_group",
        "update_group",
        "get_group",
        "get_group",
    ]


@pytest.mark.asyncio
async def test_refresh_fails_closed_when_pending_group_never_applies() -> None:
    deployment = unpersisted_deployment(provider_configuration(with_binding=True))
    deployment.provider_container_group_id = str(GROUP_ID)
    deployment.state = SaladDeploymentState.ACTIVE
    preflight = make_group(deployment.container_group_name, deployment.queue_name)
    pending = make_group(
        deployment.container_group_name,
        deployment.queue_name,
        pending_change=True,
        version=2,
    )
    client = FakeClient()
    client.groups[deployment.container_group_name] = pending
    client.get_group_results = [preflight]
    client.update_group_result = pending

    with pytest.raises(SaladDeploymentValidationError, match="did not converge"):
        await refresh_container_group_runtime(
            deployment,
            client,
            FakeResolver({WORKER_MODEL_MANIFEST_JSON_BINDING: LIVE_VALUE}),
            convergence_timeout_seconds=0.01,
            poll_interval_seconds=0.001,
        )


@pytest.mark.asyncio
async def test_queue_admission_starts_stopped_group_without_waiting_for_attachment() -> None:
    deployment = unpersisted_deployment(provider_configuration())
    deployment.provider_queue_id = str(QUEUE_ID)
    deployment.provider_container_group_id = str(GROUP_ID)
    deployment.state = SaladDeploymentState.ACTIVE
    stopped = make_group(
        deployment.container_group_name,
        deployment.queue_name,
        status="stopped",
        replicas=1,
        running=0,
        finish_time=NOW,
    )
    admitted_stopped = make_group(
        deployment.container_group_name,
        deployment.queue_name,
        status="stopped",
        replicas=1,
        running=0,
        finish_time=NOW,
        # Autoscaler-only updates may retain the current group version.
        version=1,
        autoscaler={
            "min_replicas": 1,
            "max_replicas": 1,
            "desired_queue_length": 1,
            "polling_period": 30,
        },
    )
    client = FakeClient()
    client.groups[deployment.container_group_name] = admitted_stopped
    # A cold worker is not listed as attached yet. The durable queue can still
    # accept work while Salad downloads and starts the exact group.
    client.queues[deployment.queue_name] = make_queue(deployment.queue_name)
    client.get_group_results = [stopped, admitted_stopped]
    client.update_group_result = admitted_stopped

    result = await ensure_container_group_queue_admission(
        deployment,
        client,
        effective_min_replicas=1,
        convergence_timeout_seconds=1,
        poll_interval_seconds=0,
    )

    assert result is admitted_stopped
    assert client.start_names == [deployment.container_group_name]
    assert client.updated_group_patches == [
        {
            "queue_autoscaler": {
                "min_replicas": 1,
                "max_replicas": 1,
                "desired_queue_length": 1,
                "polling_period": 30,
            }
        }
    ]
    assert [call[0] for call in client.calls] == [
        "get_group",
        "get_queue",
        "update_group",
        "get_group",
        "start_group",
    ]


@pytest.mark.asyncio
async def test_queue_admission_refuses_wrong_queue_identity_before_provider_mutation() -> None:
    deployment = unpersisted_deployment(provider_configuration())
    deployment.provider_queue_id = str(QUEUE_ID)
    deployment.provider_container_group_id = str(GROUP_ID)
    deployment.state = SaladDeploymentState.ACTIVE
    stopped = make_group(
        deployment.container_group_name,
        deployment.queue_name,
        status="stopped",
        replicas=1,
        running=0,
        finish_time=NOW,
    )
    client = FakeClient()
    client.groups[deployment.container_group_name] = stopped
    client.queues[deployment.queue_name] = make_queue(
        deployment.queue_name,
        queue_id=uuid4(),
    )
    client.get_group_results = [stopped]

    with pytest.raises(
        SaladDeploymentValidationError,
        match="queue identity does not match deployment",
    ):
        await ensure_container_group_queue_admission(
            deployment,
            client,
            effective_min_replicas=1,
        )

    assert client.start_names == []
    assert client.updated_group_patches == []
    assert [call[0] for call in client.calls] == ["get_group", "get_queue"]


@pytest.mark.asyncio
async def test_queue_admission_refuses_to_start_without_durable_demand() -> None:
    deployment = unpersisted_deployment(provider_configuration())
    deployment.provider_queue_id = str(QUEUE_ID)
    deployment.provider_container_group_id = str(GROUP_ID)
    deployment.state = SaladDeploymentState.ACTIVE
    client = FakeClient()

    with pytest.raises(
        SaladDeploymentValidationError,
        match="durable queue admission demand is required",
    ):
        await ensure_container_group_queue_admission(
            deployment,
            client,
            effective_min_replicas=0,
        )

    assert client.calls == []


def test_naive_controller_timestamp_is_rejected() -> None:
    with pytest.raises(SaladDeploymentValidationError, match="timezone"):
        _as_utc(datetime(2026, 7, 28, 12, 0))


@pytest.mark.asyncio
async def test_budget_guard_is_locked_before_deployment_row(
    deployment_context: DeploymentContext,
) -> None:
    statements: list[str] = []

    def capture_statement(
        conn: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        del conn, cursor, parameters, context, executemany
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement.lower())

    sync_engine = deployment_context.database.engine.sync_engine
    event.listen(sync_engine, "before_cursor_execute", capture_statement)
    try:
        async with deployment_context.database.sessions() as session:
            await provision_deployment_step(
                session,
                deployment_id=deployment_context.deployment_id,
                client=FakeClient(),
                now=NOW,
            )
            await session.rollback()
    finally:
        event.remove(sync_engine, "before_cursor_execute", capture_statement)

    budget_select = next(
        index for index, statement in enumerate(statements) if "provider_budget_guards" in statement
    )
    deployment_select = next(
        index for index, statement in enumerate(statements) if "salad_deployments" in statement
    )
    assert budget_select < deployment_select


@pytest.mark.asyncio
async def test_provisioning_commits_queue_before_group_and_resolves_values_just_in_time(
    deployment_context: DeploymentContext,
) -> None:
    client = FakeClient()
    resolver = FakeResolver({WORKER_MODEL_MANIFEST_JSON_BINDING: LIVE_VALUE})
    async with deployment_context.database.sessions() as session:
        deployment = await session.get(SaladDeployment, deployment_context.deployment_id)
        assert deployment is not None
        deployment.provider_configuration = provider_configuration(with_binding=True)
        await session.commit()

        first = await provision_deployment_step(
            session,
            deployment_id=deployment.id,
            client=client,
            secret_resolver=resolver,
            now=NOW,
        )
        await session.commit()

        assert first.action == DeploymentAction.QUEUE_CREATED
        assert first.provider_queue_id == str(QUEUE_ID)
        assert client.created_group_payloads == []
        assert resolver.requests == []

        second = await provision_deployment_step(
            session,
            deployment_id=deployment.id,
            client=client,
            secret_resolver=resolver,
            now=NOW + timedelta(seconds=1),
        )
        await session.commit()
        await session.refresh(deployment)

        assert second.action == DeploymentAction.GROUP_CREATED
        assert second.provider_container_group_id == str(GROUP_ID)
        assert resolver.requests == [(WORKER_MODEL_MANIFEST_JSON_BINDING,)]
        payload = client.created_group_payloads[0]
        assert payload["name"] == remote_names()[1]
        assert payload["replicas"] == 0
        assert "priority" not in payload
        assert payload["autostart_policy"] is True
        assert payload["restart_policy"] == "on_failure"
        assert payload["startup_probe"] == STARTUP_PROBE
        assert payload["readiness_probe"] == READINESS_PROBE
        assert payload["liveness_probe"] == LIVENESS_PROBE
        assert payload["queue_autoscaler"] == {
            "polling_period": 30,
            "min_replicas": 0,
            "max_replicas": 1,
            "desired_queue_length": 1,
        }
        assert payload["queue_connection"] == {
            "path": "/jobs/generate",
            "port": 8000,
            "queue_name": remote_names()[0],
        }
        container = payload["container"]
        assert isinstance(container, dict)
        assert container["image"] == IMAGE_DIGEST
        assert container["priority"] == "low"
        assert container["environment_variables"] == {
            WORKER_MODEL_MANIFEST_JSON_BINDING: LIVE_VALUE
        }
        assert LIVE_VALUE not in str(deployment.provider_configuration)
        audit_details = list((await session.scalars(select(AuditEvent.detail))).all())
        assert all(LIVE_VALUE not in str(detail) for detail in audit_details)


@pytest.mark.asyncio
async def test_ambiguous_queue_create_is_never_reposted_without_operator_resolution(
    deployment_context: DeploymentContext,
) -> None:
    client = FakeClient()
    client.create_queue_error = SaladTransportError("uncertain")
    async with deployment_context.database.sessions() as session:
        first = await provision_deployment_step(
            session,
            deployment_id=deployment_context.deployment_id,
            client=client,
            now=NOW,
        )
        await session.commit()
        second = await provision_deployment_step(
            session,
            deployment_id=deployment_context.deployment_id,
            client=client,
            now=NOW + timedelta(minutes=1),
        )
        await session.commit()

        deployment = await session.get(SaladDeployment, deployment_context.deployment_id)

    assert first.action == second.action == DeploymentAction.DEFERRED
    assert second.error_code == "queue_create_outcome_unknown"
    assert client.created_queue_names == [remote_names()[0]]
    assert deployment is not None
    assert deployment.state == SaladDeploymentState.UNKNOWN


@pytest.mark.asyncio
async def test_ambiguous_queue_create_can_be_adopted_by_deterministic_name(
    deployment_context: DeploymentContext,
) -> None:
    client = FakeClient()
    client.create_queue_error = SaladTransportError("uncertain")
    async with deployment_context.database.sessions() as session:
        await provision_deployment_step(
            session,
            deployment_id=deployment_context.deployment_id,
            client=client,
            now=NOW,
        )
        await session.commit()
        client.create_queue_error = None
        client.queues[remote_names()[0]] = make_queue(remote_names()[0])

        result = await provision_deployment_step(
            session,
            deployment_id=deployment_context.deployment_id,
            client=client,
            now=NOW + timedelta(minutes=1),
        )
        await session.commit()

    assert result.action == DeploymentAction.QUEUE_ADOPTED
    assert result.provider_queue_id == str(QUEUE_ID)
    assert client.created_queue_names == [remote_names()[0]]


@pytest.mark.asyncio
async def test_existing_group_is_adopted_after_persisted_queue_verification(
    deployment_context: DeploymentContext,
) -> None:
    client = FakeClient()
    queue_name, group_name = remote_names()
    client.queues[queue_name] = make_queue(queue_name, group_name=group_name)
    client.groups[group_name] = make_group(group_name, queue_name)
    async with deployment_context.database.sessions() as session:
        deployment = await session.get(SaladDeployment, deployment_context.deployment_id)
        assert deployment is not None
        deployment.queue_name = queue_name
        deployment.container_group_name = group_name
        deployment.provider_queue_id = str(QUEUE_ID)
        deployment.state = SaladDeploymentState.PROVISIONING
        await session.commit()

        result = await provision_deployment_step(
            session,
            deployment_id=deployment.id,
            client=client,
            now=NOW,
        )
        await session.commit()

    assert result.action == DeploymentAction.GROUP_ADOPTED
    assert result.provider_container_group_id == str(GROUP_ID)
    assert client.created_group_payloads == []


@pytest.mark.asyncio
async def test_missing_persisted_queue_fails_without_group_creation(
    deployment_context: DeploymentContext,
) -> None:
    client = FakeClient()
    async with deployment_context.database.sessions() as session:
        deployment = await session.get(SaladDeployment, deployment_context.deployment_id)
        assert deployment is not None
        deployment.queue_name = remote_names()[0]
        deployment.provider_queue_id = str(QUEUE_ID)
        deployment.state = SaladDeploymentState.PROVISIONING
        await session.commit()

        result = await provision_deployment_step(
            session,
            deployment_id=deployment.id,
            client=client,
            now=NOW,
        )
        await session.commit()

    assert result.action == DeploymentAction.FAILED
    assert result.error_code == "persisted_queue_missing"
    assert client.created_group_payloads == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected_code"),
    [
        (
            SaladRateLimitError(
                message="slow",
                response_body="safe",
                request_id=None,
                retry_after_seconds=1,
            ),
            "deployment_reconcile_rate_limited",
        ),
        (api_error(404), "deployment_reconcile_not_found"),
        (api_error(503), "deployment_reconcile_http_503"),
        (SaladProtocolError("schema"), "deployment_reconcile_protocol_error"),
        (SaladTransportError("network"), "deployment_reconcile_transport_error"),
    ],
)
async def test_provider_read_errors_are_safely_classified(
    deployment_context: DeploymentContext,
    error: Exception,
    expected_code: str,
) -> None:
    await make_fully_provisioned(deployment_context)
    client = FakeClient()
    client.get_queue_error = error
    async with deployment_context.database.sessions() as session:
        result = await reconcile_deployment(
            session,
            deployment_id=deployment_context.deployment_id,
            client=client,
            now=NOW + timedelta(minutes=1),
        )
        await session.commit()
        deployment = await session.get(SaladDeployment, deployment_context.deployment_id)
        assert deployment is not None

    assert result.action == DeploymentAction.DEFERRED
    assert result.state == SaladDeploymentState.UNKNOWN
    assert result.error_code == expected_code
    assert deployment.billing_observation_stale is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected_code"),
    [
        (api_error(409), "queue_create_conflict_requires_reconcile"),
        (api_error(503), "queue_create_http_503_unknown"),
        (SaladProtocolError("schema"), "queue_create_response_unknown"),
    ],
)
async def test_ambiguous_mutation_responses_require_reconciliation(
    deployment_context: DeploymentContext,
    error: Exception,
    expected_code: str,
) -> None:
    client = FakeClient()
    client.create_queue_error = error
    async with deployment_context.database.sessions() as session:
        result = await provision_deployment_step(
            session,
            deployment_id=deployment_context.deployment_id,
            client=client,
            now=NOW,
        )
        await session.commit()

    assert result.action == DeploymentAction.DEFERRED
    assert result.state == SaladDeploymentState.UNKNOWN
    assert result.error_code == expected_code


@pytest.mark.asyncio
async def test_current_rollout_waits_for_superseded_group_to_stop(
    deployment_context: DeploymentContext,
) -> None:
    async with deployment_context.database.sessions() as session:
        superseded = SaladDeployment(
            version_no=2,
            config_sha256="9" * 64,
            provider_configuration=provider_configuration(),
            worker_image_digest=IMAGE_DIGEST,
            organization_name="creator-org",
            project_name="production",
            queue_name="superseded-queue",
            provider_queue_id="superseded-queue-id",
            container_group_name="superseded-group",
            provider_container_group_id="superseded-group-id",
            state=SaladDeploymentState.DRAINING,
            desired_state=DesiredDeploymentState.STOPPED,
            is_current=False,
            min_replicas=0,
            max_replicas=1,
            desired_queue_length=1,
            max_hourly_cost_microusd=3_600_000,
        )
        session.add(superseded)
        await session.commit()

        client = FakeClient()
        result = await provision_deployment_step(
            session,
            deployment_id=deployment_context.deployment_id,
            client=client,
            now=NOW,
        )
        await session.commit()

    assert result.action == DeploymentAction.DEFERRED
    assert result.error_code == "superseded_deployment_not_stopped"
    assert client.calls == []


@pytest.mark.asyncio
async def test_confirmed_partial_identity_stop_does_not_block_current_rollout(
    deployment_context: DeploymentContext,
) -> None:
    async with deployment_context.database.sessions() as session:
        superseded = SaladDeployment(
            version_no=2,
            config_sha256="9" * 64,
            provider_configuration=provider_configuration(),
            worker_image_digest=IMAGE_DIGEST,
            organization_name="creator-org",
            project_name="production",
            queue_name="superseded-queue",
            container_group_name="superseded-group",
            provider_container_group_id="superseded-group-id",
            state=SaladDeploymentState.FAILED,
            desired_state=DesiredDeploymentState.STOPPED,
            is_current=False,
            min_replicas=0,
            max_replicas=1,
            desired_queue_length=1,
            max_hourly_cost_microusd=3_600_000,
        )
        session.add(superseded)
        await session.commit()

        client = FakeClient()
        stopped = await provision_deployment_step(
            session,
            deployment_id=superseded.id,
            client=client,
            now=NOW,
        )
        await session.commit()
        current = await provision_deployment_step(
            session,
            deployment_id=deployment_context.deployment_id,
            client=client,
            now=NOW + timedelta(seconds=1),
        )
        await session.commit()
        await session.refresh(superseded)

    assert stopped.action == DeploymentAction.STOPPED
    assert stopped.state == SaladDeploymentState.FAILED
    assert superseded.stopped_at is not None
    assert superseded.stopped_at.replace(tzinfo=UTC) == NOW
    assert current.action == DeploymentAction.QUEUE_CREATED
    assert current.error_code is None


async def make_fully_provisioned(
    context: DeploymentContext,
    *,
    desired_state: DesiredDeploymentState = DesiredDeploymentState.ACTIVE,
    last_observed_at: datetime | None = None,
    observed_replicas: int | None = None,
) -> None:
    queue_name, group_name = remote_names()
    async with context.database.sessions() as session:
        deployment = await session.get(SaladDeployment, context.deployment_id)
        assert deployment is not None
        deployment.queue_name = queue_name
        deployment.container_group_name = group_name
        deployment.provider_queue_id = str(QUEUE_ID)
        deployment.provider_container_group_id = str(GROUP_ID)
        deployment.state = SaladDeploymentState.ACTIVE
        deployment.desired_state = desired_state
        deployment.activated_at = NOW
        deployment.last_observed_at = last_observed_at
        deployment.observed_replicas = observed_replicas
        await session.commit()


@pytest.mark.asyncio
async def test_running_transition_during_instance_read_uses_response_time_bound(
    deployment_context: DeploymentContext,
) -> None:
    await make_fully_provisioned(deployment_context)
    client = FakeClient()
    queue_name, group_name = remote_names()
    cycle_started_at = NOW + timedelta(seconds=10)
    running_started_at = NOW + timedelta(seconds=20)
    response_received_at = NOW + timedelta(seconds=30)
    client.queues[queue_name] = make_queue(queue_name)
    client.groups[group_name] = make_group(
        group_name,
        queue_name,
        status="running",
        replicas=1,
        running=1,
        start_time=running_started_at,
    )
    client.instance_pages[group_name] = SaladContainerGroupInstancePage(
        instances=(
            SaladContainerGroupInstance(
                id=INSTANCE_ID,
                machine_id="machine-creator-1",
                state=SaladContainerGroupInstanceState.RUNNING,
                update_time=running_started_at,
                version=1,
            ),
        )
    )

    def billing_observation_clock() -> datetime:
        assert client.calls[-1] == ("list_instances", group_name)
        return response_received_at

    async with deployment_context.database.sessions() as session:
        result = await reconcile_deployment(
            session,
            deployment_id=deployment_context.deployment_id,
            client=client,
            now=cycle_started_at,
            billing_observation_clock=billing_observation_clock,
        )
        await session.commit()
        deployment = await session.get(SaladDeployment, deployment_context.deployment_id)
        assert deployment is not None

    assert result.state == SaladDeploymentState.ACTIVE
    assert deployment.billing_session_started_at is not None
    assert deployment.billing_session_started_at.replace(tzinfo=UTC) == running_started_at
    assert deployment.billing_active_started_at is not None
    assert deployment.billing_active_started_at.replace(tzinfo=UTC) == running_started_at
    assert deployment.billing_observed_at is not None
    assert deployment.billing_observed_at.replace(tzinfo=UTC) == response_received_at
    assert deployment.billing_estimated is False


@pytest.mark.asyncio
async def test_first_untracked_stopping_instance_is_stale_instead_of_exact_zero(
    deployment_context: DeploymentContext,
) -> None:
    await make_fully_provisioned(deployment_context)
    client = FakeClient()
    queue_name, group_name = remote_names()
    observed_at = NOW + timedelta(seconds=30)
    client.queues[queue_name] = make_queue(queue_name)
    client.groups[group_name] = make_group(group_name, queue_name, status="running")
    client.instance_pages[group_name] = SaladContainerGroupInstancePage(
        instances=(
            SaladContainerGroupInstance(
                id=INSTANCE_ID,
                machine_id="machine-creator-1",
                state=SaladContainerGroupInstanceState.STOPPING,
                update_time=NOW + timedelta(seconds=20),
                version=1,
            ),
        )
    )

    async with deployment_context.database.sessions() as session:
        await reconcile_deployment(
            session,
            deployment_id=deployment_context.deployment_id,
            client=client,
            now=observed_at,
        )
        await session.commit()
        deployment = await session.get(SaladDeployment, deployment_context.deployment_id)
        assert deployment is not None

    assert deployment.billing_session_started_at is None
    assert deployment.billing_accumulated_microseconds == 0
    assert deployment.billing_observation_stale is True
    assert deployment.billing_estimated is True


@pytest.mark.asyncio
async def test_stop_transition_during_instance_read_closes_at_exact_provider_time(
    deployment_context: DeploymentContext,
) -> None:
    await make_fully_provisioned(
        deployment_context,
        desired_state=DesiredDeploymentState.STOPPED,
    )
    client = FakeClient()
    queue_name, group_name = remote_names()
    cycle_started_at = NOW + timedelta(seconds=20)
    stopped_at = NOW + timedelta(seconds=30)
    response_received_at = NOW + timedelta(seconds=40)
    client.groups[group_name] = make_group(
        group_name,
        queue_name,
        status="stopped",
        finish_time=stopped_at,
    )
    client.instance_pages[group_name] = SaladContainerGroupInstancePage(
        instances=(
            SaladContainerGroupInstance(
                id=INSTANCE_ID,
                machine_id="machine-creator-1",
                state=SaladContainerGroupInstanceState.STOPPING,
                update_time=stopped_at,
                version=1,
            ),
        )
    )

    def billing_observation_clock() -> datetime:
        assert client.calls[-1] == ("list_instances", group_name)
        return response_received_at

    async with deployment_context.database.sessions() as session:
        deployment = await session.get(SaladDeployment, deployment_context.deployment_id)
        assert deployment is not None
        deployment.billing_session_started_at = NOW
        deployment.billing_active_instance_id = INSTANCE_ID
        deployment.billing_active_started_at = NOW
        deployment.billing_observed_at = NOW
        await session.commit()

        result = await reconcile_deployment(
            session,
            deployment_id=deployment.id,
            client=client,
            now=cycle_started_at,
            billing_observation_clock=billing_observation_clock,
        )
        await session.commit()
        await session.refresh(deployment)

    assert result.action == DeploymentAction.STOPPED
    assert deployment.billing_accumulated_microseconds == 30_000_000
    assert deployment.billing_active_instance_id is None
    assert deployment.billing_active_started_at is None
    assert deployment.billing_observed_at is not None
    assert deployment.billing_observed_at.replace(tzinfo=UTC) == response_received_at
    assert deployment.billing_observation_stale is False
    assert deployment.billing_estimated is False


@pytest.mark.asyncio
async def test_stopped_desire_only_invokes_stop_and_requires_observed_confirmation(
    deployment_context: DeploymentContext,
) -> None:
    await make_fully_provisioned(
        deployment_context,
        desired_state=DesiredDeploymentState.STOPPED,
    )
    client = FakeClient()
    queue_name, group_name = remote_names()
    client.queues[queue_name] = make_queue(queue_name)
    client.groups[group_name] = make_group(group_name, queue_name, status="running", running=1)

    async with deployment_context.database.sessions() as session:
        requested = await reconcile_deployment(
            session,
            deployment_id=deployment_context.deployment_id,
            client=client,
            now=NOW + timedelta(minutes=1),
        )
        await session.commit()
        client.groups[group_name] = make_group(
            group_name,
            queue_name,
            status="stopped",
            start_time=None,
            finish_time=NOW + timedelta(minutes=2),
        )
        confirmed = await reconcile_deployment(
            session,
            deployment_id=deployment_context.deployment_id,
            client=client,
            now=NOW + timedelta(minutes=2),
        )
        await session.commit()
        deployment = await session.get(SaladDeployment, deployment_context.deployment_id)
        assert deployment is not None
        entries = list((await session.scalars(select(ProviderSpendEntry))).all())

    assert requested.action == DeploymentAction.STOP_REQUESTED
    assert requested.state == SaladDeploymentState.DRAINING
    assert requested.metered_microusd == 60_000
    assert confirmed.action == DeploymentAction.STOPPED
    assert confirmed.state == SaladDeploymentState.STOPPED
    assert confirmed.metered_microusd == 60_000
    assert sum(entry.amount_microusd for entry in entries) == 120_000
    assert client.stop_names == [group_name]
    assert all(call[0] != "start_group" for call in client.calls)
    assert deployment.billing_session_started_at is not None
    assert deployment.billing_session_ended_at is not None
    assert deployment.billing_active_instance_id is None


@pytest.mark.asyncio
async def test_retired_video_deployment_is_never_restarted(
    deployment_context: DeploymentContext,
) -> None:
    await make_fully_provisioned(
        deployment_context,
        desired_state=DesiredDeploymentState.ACTIVE,
    )
    client = FakeClient()
    queue_name, group_name = remote_names()
    client.groups[group_name] = make_group(
        group_name,
        queue_name,
        status="stopped",
        start_time=None,
        finish_time=NOW,
    )

    async with deployment_context.database.sessions() as session:
        deployment = await session.get(SaladDeployment, deployment_context.deployment_id)
        assert deployment is not None
        deployment.purpose = SaladDeploymentPurpose.VIDEO
        await session.commit()

        result = await reconcile_deployment(
            session,
            deployment_id=deployment.id,
            client=client,
            now=NOW,
        )
        await session.commit()
        await session.refresh(deployment)

    assert result.action == DeploymentAction.STOPPED
    assert deployment.desired_state == DesiredDeploymentState.STOPPED
    assert deployment.administrative_stop_reason == "video_generation_retired"
    assert all(call[0] != "start_group" for call in client.calls)


@pytest.mark.asyncio
async def test_confirmed_stop_preserves_administrative_reason_after_transient_error(
    deployment_context: DeploymentContext,
) -> None:
    await make_fully_provisioned(
        deployment_context,
        desired_state=DesiredDeploymentState.STOPPED,
    )
    client = FakeClient()
    queue_name, group_name = remote_names()
    client.groups[group_name] = make_group(
        group_name,
        queue_name,
        status="stopped",
        start_time=None,
        finish_time=NOW + timedelta(minutes=1),
    )

    async with deployment_context.database.sessions() as session:
        deployment = await session.get(SaladDeployment, deployment_context.deployment_id)
        assert deployment is not None
        deployment.administrative_stop_reason = "gpu_allocation_disabled"
        deployment.last_error_code = "stop_container_group_transport_unknown"
        deployment.last_error_detail = "The earlier provider stop response was ambiguous."
        await session.commit()

        result = await reconcile_deployment(
            session,
            deployment_id=deployment.id,
            client=client,
            now=NOW + timedelta(minutes=1),
        )
        await session.commit()
        await session.refresh(deployment)

    assert result.action == DeploymentAction.STOPPED
    assert deployment.state == SaladDeploymentState.STOPPED
    assert deployment.last_error_code is None
    assert deployment.last_error_detail is None
    assert deployment.administrative_stop_reason == "gpu_allocation_disabled"


@pytest.mark.asyncio
async def test_draining_stop_converges_when_idle_provider_status_is_empty(
    deployment_context: DeploymentContext,
) -> None:
    await make_fully_provisioned(
        deployment_context,
        desired_state=DesiredDeploymentState.STOPPED,
    )
    client = FakeClient()
    queue_name, group_name = remote_names()
    client.groups[group_name] = make_group(group_name, queue_name, status="")

    async with deployment_context.database.sessions() as session:
        deployment = await session.get(SaladDeployment, deployment_context.deployment_id)
        assert deployment is not None
        deployment.state = SaladDeploymentState.DRAINING
        await session.commit()

        result = await reconcile_deployment(
            session,
            deployment_id=deployment_context.deployment_id,
            client=client,
            now=NOW + timedelta(minutes=1),
        )
        await session.commit()

    assert result.action == DeploymentAction.STOPPED
    assert result.state == SaladDeploymentState.STOPPED
    assert client.stop_names == []


@pytest.mark.asyncio
async def test_terminal_group_waits_for_authoritative_instance_shutdown(
    deployment_context: DeploymentContext,
) -> None:
    await make_fully_provisioned(
        deployment_context,
        desired_state=DesiredDeploymentState.STOPPED,
    )
    client = FakeClient()
    _queue_name, group_name = remote_names()
    client.groups[group_name] = make_group(
        group_name,
        remote_names()[0],
        status="stopped",
        finish_time=NOW + timedelta(seconds=20),
    )
    running = SaladContainerGroupInstance(
        id=INSTANCE_ID,
        machine_id="machine-creator-1",
        state=SaladContainerGroupInstanceState.RUNNING,
        update_time=NOW,
        version=1,
    )
    client.instance_pages[group_name] = SaladContainerGroupInstancePage(instances=(running,))

    async with deployment_context.database.sessions() as session:
        deployment = await session.get(SaladDeployment, deployment_context.deployment_id)
        assert deployment is not None
        deployment.billing_session_started_at = NOW
        deployment.billing_active_instance_id = INSTANCE_ID
        deployment.billing_active_started_at = NOW
        deployment.billing_observed_at = NOW + timedelta(seconds=5)
        await session.commit()

        still_running = await reconcile_deployment(
            session,
            deployment_id=deployment.id,
            client=client,
            now=NOW + timedelta(seconds=30),
        )
        await session.commit()
        await session.refresh(deployment)
        assert still_running.action == DeploymentAction.DEFERRED
        assert still_running.error_code == "billing_instance_stop_unconfirmed"
        assert deployment.state == SaladDeploymentState.DRAINING
        assert deployment.billing_session_ended_at is None
        assert deployment.billing_active_instance_id == INSTANCE_ID

        client.list_instances_error = SaladTransportError("instance status unavailable")
        unconfirmed = await reconcile_deployment(
            session,
            deployment_id=deployment.id,
            client=client,
            now=NOW + timedelta(seconds=40),
        )
        await session.commit()
        await session.refresh(deployment)
        assert unconfirmed.action == DeploymentAction.DEFERRED
        assert deployment.billing_observation_stale is True
        assert deployment.billing_session_ended_at is None

        client.list_instances_error = None
        client.instance_pages[group_name] = SaladContainerGroupInstancePage(instances=())
        confirmed = await reconcile_deployment(
            session,
            deployment_id=deployment.id,
            client=client,
            now=NOW + timedelta(seconds=50),
        )
        await session.commit()
        await session.refresh(deployment)

    assert confirmed.action == DeploymentAction.STOPPED
    assert deployment.billing_session_ended_at is not None
    assert deployment.billing_active_instance_id is None
    assert client.stop_names == []


@pytest.mark.asyncio
async def test_consecutive_instance_failures_never_clear_unresolved_stop(
    deployment_context: DeploymentContext,
) -> None:
    await make_fully_provisioned(
        deployment_context,
        desired_state=DesiredDeploymentState.STOPPED,
    )
    client = FakeClient()
    queue_name, group_name = remote_names()
    client.groups[group_name] = make_group(
        group_name,
        queue_name,
        status="running",
        replicas=1,
        running=1,
    )
    client.list_instances_error = SaladTransportError("instance status unavailable")

    async with deployment_context.database.sessions() as session:
        requested = await reconcile_deployment(
            session,
            deployment_id=deployment_context.deployment_id,
            client=client,
            now=NOW + timedelta(seconds=10),
        )
        await session.commit()
        deployment = await session.get(SaladDeployment, deployment_context.deployment_id)
        assert deployment is not None
        assert requested.action == DeploymentAction.STOP_REQUESTED
        assert deployment.billing_observation_stale is True
        assert deployment.billing_session_started_at is None

        client.groups[group_name] = make_group(
            group_name,
            queue_name,
            status="stopped",
            replicas=0,
            running=0,
            finish_time=NOW + timedelta(seconds=15),
        )
        unconfirmed = await reconcile_deployment(
            session,
            deployment_id=deployment.id,
            client=client,
            now=NOW + timedelta(seconds=20),
        )
        await session.commit()
        await session.refresh(deployment)

    assert unconfirmed.action == DeploymentAction.DEFERRED
    assert unconfirmed.error_code == "billing_instance_stop_unconfirmed"
    assert deployment.state == SaladDeploymentState.DRAINING
    assert deployment.billing_observation_stale is True
    assert deployment.billing_session_ended_at is None


@pytest.mark.asyncio
async def test_absent_group_ends_an_open_billing_session(
    deployment_context: DeploymentContext,
) -> None:
    await make_fully_provisioned(
        deployment_context,
        desired_state=DesiredDeploymentState.STOPPED,
    )
    client = FakeClient()
    async with deployment_context.database.sessions() as session:
        deployment = await session.get(SaladDeployment, deployment_context.deployment_id)
        assert deployment is not None
        deployment.billing_session_started_at = NOW
        deployment.billing_active_instance_id = INSTANCE_ID
        deployment.billing_active_started_at = NOW
        deployment.billing_observed_at = NOW + timedelta(seconds=5)
        await session.commit()

        result = await reconcile_deployment(
            session,
            deployment_id=deployment.id,
            client=client,
            now=NOW + timedelta(seconds=30),
        )
        await session.commit()
        await session.refresh(deployment)

    assert result.action == DeploymentAction.STOPPED
    assert deployment.billing_session_ended_at is not None
    assert deployment.billing_active_instance_id is None
    assert deployment.billing_estimated is True


@pytest.mark.asyncio
async def test_initially_idle_active_deployment_still_requests_provider_stop(
    deployment_context: DeploymentContext,
) -> None:
    await make_fully_provisioned(
        deployment_context,
        desired_state=DesiredDeploymentState.STOPPED,
    )
    client = FakeClient()
    queue_name, group_name = remote_names()
    client.groups[group_name] = make_group(group_name, queue_name, status="")

    async with deployment_context.database.sessions() as session:
        result = await reconcile_deployment(
            session,
            deployment_id=deployment_context.deployment_id,
            client=client,
            now=NOW + timedelta(minutes=1),
        )
        await session.commit()

    assert result.action == DeploymentAction.STOP_REQUESTED
    assert result.state == SaladDeploymentState.DRAINING
    assert client.stop_names == [group_name]


@pytest.mark.asyncio
async def test_draining_group_with_running_status_still_requests_provider_stop(
    deployment_context: DeploymentContext,
) -> None:
    await make_fully_provisioned(
        deployment_context,
        desired_state=DesiredDeploymentState.STOPPED,
    )
    client = FakeClient()
    queue_name, group_name = remote_names()
    client.groups[group_name] = make_group(group_name, queue_name, status="running")

    async with deployment_context.database.sessions() as session:
        deployment = await session.get(SaladDeployment, deployment_context.deployment_id)
        assert deployment is not None
        deployment.state = SaladDeploymentState.DRAINING
        await session.commit()

        result = await reconcile_deployment(
            session,
            deployment_id=deployment_context.deployment_id,
            client=client,
            now=NOW + timedelta(minutes=1),
        )
        await session.commit()

    assert result.action == DeploymentAction.STOP_REQUESTED
    assert result.state == SaladDeploymentState.DRAINING
    assert client.stop_names == [group_name]


@pytest.mark.asyncio
async def test_stop_still_executes_when_status_read_fails(
    deployment_context: DeploymentContext,
) -> None:
    await make_fully_provisioned(
        deployment_context,
        desired_state=DesiredDeploymentState.STOPPED,
    )
    client = FakeClient()
    client.get_group_error = api_error(503)
    async with deployment_context.database.sessions() as session:
        result = await reconcile_deployment(
            session,
            deployment_id=deployment_context.deployment_id,
            client=client,
            now=NOW + timedelta(minutes=1),
        )
        await session.commit()
        deployment = await session.get(SaladDeployment, deployment_context.deployment_id)
        assert deployment is not None

    assert result.action == DeploymentAction.STOP_REQUESTED
    assert client.stop_names == [remote_names()[1]]
    assert deployment.billing_observation_stale is True


@pytest.mark.asyncio
async def test_ambiguous_stop_enters_unknown_and_never_starts(
    deployment_context: DeploymentContext,
) -> None:
    await make_fully_provisioned(
        deployment_context,
        desired_state=DesiredDeploymentState.STOPPED,
    )
    client = FakeClient()
    queue_name, group_name = remote_names()
    client.groups[group_name] = make_group(group_name, queue_name, status="running")
    client.stop_error = SaladTransportError("uncertain")
    async with deployment_context.database.sessions() as session:
        result = await reconcile_deployment(
            session,
            deployment_id=deployment_context.deployment_id,
            client=client,
            now=NOW + timedelta(minutes=1),
        )
        await session.commit()

    assert result.action == DeploymentAction.DEFERRED
    assert result.state == SaladDeploymentState.UNKNOWN
    assert result.error_code == "group_stop_transport_unknown"


@pytest.mark.asyncio
async def test_stopped_unknown_deployment_recovers_missing_ids_before_stop(
    deployment_context: DeploymentContext,
) -> None:
    client = FakeClient()
    queue_name, group_name = remote_names()
    client.queues[queue_name] = make_queue(queue_name)
    client.groups[group_name] = make_group(
        group_name,
        queue_name,
        status="running",
        running=1,
    )
    async with deployment_context.database.sessions() as session:
        deployment = await session.get(
            SaladDeployment,
            deployment_context.deployment_id,
        )
        assert deployment is not None
        deployment.state = SaladDeploymentState.UNKNOWN
        deployment.desired_state = DesiredDeploymentState.STOPPED
        deployment.unknown_since = NOW - timedelta(minutes=1)
        deployment.provider_queue_id = None
        deployment.provider_container_group_id = None
        await session.commit()

        result = await provision_deployment_step(
            session,
            deployment_id=deployment.id,
            client=client,
            now=NOW + timedelta(minutes=1),
        )
        await session.commit()
        await session.refresh(deployment)

    assert result.action == DeploymentAction.STOP_REQUESTED
    assert deployment.state == SaladDeploymentState.UNKNOWN
    assert deployment.provider_queue_id is None
    assert deployment.provider_container_group_id == str(GROUP_ID)
    assert client.stop_names == [group_name]
    assert all(call[0] not in {"create_queue", "create_group"} for call in client.calls)


@pytest.mark.asyncio
async def test_reconcile_incomplete_resources_defers_without_provider_calls(
    deployment_context: DeploymentContext,
) -> None:
    client = FakeClient()
    async with deployment_context.database.sessions() as session:
        result = await reconcile_deployment(
            session,
            deployment_id=deployment_context.deployment_id,
            client=client,
            now=NOW,
        )
        await session.commit()

    assert result.action == DeploymentAction.DEFERRED
    assert result.error_code == "provider_resources_incomplete"
    assert client.calls == []


@pytest.mark.asyncio
async def test_reconcile_records_conservative_idempotent_runtime_interval(
    deployment_context: DeploymentContext,
) -> None:
    await make_fully_provisioned(
        deployment_context,
        last_observed_at=NOW,
        observed_replicas=1,
    )
    client = FakeClient()
    queue_name, group_name = remote_names()
    client.queues[queue_name] = make_queue(queue_name, length=2, group_name=group_name)
    client.groups[group_name] = make_group(
        group_name,
        queue_name,
        replicas=0,
        running=0,
        status="running",
    )
    end = NOW + timedelta(minutes=30)

    async with deployment_context.database.sessions() as session:
        first = await reconcile_deployment(
            session,
            deployment_id=deployment_context.deployment_id,
            client=client,
            now=end,
        )
        await session.commit()
        second = await reconcile_deployment(
            session,
            deployment_id=deployment_context.deployment_id,
            client=client,
            now=end,
        )
        await session.commit()
        entries = list((await session.scalars(select(ProviderSpendEntry))).all())

    assert first.action == DeploymentAction.RECONCILED
    assert first.state == SaladDeploymentState.ACTIVE
    assert first.metered_microusd == 1_800_000
    assert second.metered_microusd == 0
    assert len(entries) == 1
    assert entries[0].amount_microusd == 1_800_000


@pytest.mark.asyncio
async def test_stopped_group_replica_target_does_not_accrue_runtime_spend(
    deployment_context: DeploymentContext,
) -> None:
    await make_fully_provisioned(
        deployment_context,
        last_observed_at=NOW,
        observed_replicas=1,
    )
    client = FakeClient()
    queue_name, group_name = remote_names()
    client.queues[queue_name] = make_queue(
        queue_name,
        group_name=group_name,
    )
    client.groups[group_name] = make_group(
        group_name,
        queue_name,
        status="stopped",
        replicas=1,
        running=0,
        start_time=None,
        finish_time=None,
    )

    async with deployment_context.database.sessions() as session:
        result = await reconcile_deployment(
            session,
            deployment_id=deployment_context.deployment_id,
            client=client,
            now=NOW + timedelta(hours=1),
        )
        await session.commit()
        deployment = await session.get(SaladDeployment, deployment_context.deployment_id)
        entries = list((await session.scalars(select(ProviderSpendEntry))).all())

    assert result.metered_microusd == 0
    assert deployment is not None and deployment.observed_replicas == 0
    assert entries == []


@pytest.mark.asyncio
async def test_stopped_group_bills_stale_previous_replica_only_through_finish_time(
    deployment_context: DeploymentContext,
) -> None:
    await make_fully_provisioned(
        deployment_context,
        last_observed_at=NOW,
        observed_replicas=1,
    )
    client = FakeClient()
    queue_name, group_name = remote_names()
    client.queues[queue_name] = make_queue(queue_name, group_name=group_name)
    client.groups[group_name] = make_group(
        group_name,
        queue_name,
        status="stopped",
        replicas=1,
        running=0,
        start_time=NOW - timedelta(hours=1),
        finish_time=NOW + timedelta(minutes=15),
    )

    async with deployment_context.database.sessions() as session:
        result = await reconcile_deployment(
            session,
            deployment_id=deployment_context.deployment_id,
            client=client,
            now=NOW + timedelta(hours=1),
        )
        await session.commit()
        deployment = await session.get(SaladDeployment, deployment_context.deployment_id)
        entries = list((await session.scalars(select(ProviderSpendEntry))).all())

    assert result.metered_microusd == 900_000
    assert deployment is not None and deployment.observed_replicas == 0
    assert len(entries) == 1
    assert entries[0].effective_at.replace(tzinfo=UTC) == NOW
    assert entries[0].amount_microusd == 900_000


@pytest.mark.asyncio
@pytest.mark.parametrize("prior_observed_replicas", (0, 1))
async def test_stopped_group_with_live_instance_preserves_conservative_runtime_metering(
    deployment_context: DeploymentContext,
    prior_observed_replicas: int,
) -> None:
    await make_fully_provisioned(
        deployment_context,
        last_observed_at=NOW,
        observed_replicas=prior_observed_replicas,
    )
    client = FakeClient()
    queue_name, group_name = remote_names()
    client.queues[queue_name] = make_queue(queue_name, group_name=group_name)
    client.groups[group_name] = make_group(
        group_name,
        queue_name,
        status="stopped",
        replicas=1,
        running=0,
        finish_time=NOW + timedelta(minutes=15),
    )
    client.instance_pages[group_name] = SaladContainerGroupInstancePage(
        instances=(
            SaladContainerGroupInstance(
                id=INSTANCE_ID,
                machine_id="machine-creator-1",
                state=SaladContainerGroupInstanceState.RUNNING,
                update_time=NOW,
                version=1,
            ),
        )
    )

    async with deployment_context.database.sessions() as session:
        result = await reconcile_deployment(
            session,
            deployment_id=deployment_context.deployment_id,
            client=client,
            now=NOW + timedelta(hours=1),
        )
        await session.commit()
        entries = list((await session.scalars(select(ProviderSpendEntry))).all())

    assert result.metered_microusd == 3_600_000
    assert len(entries) == 1
    assert entries[0].amount_microusd == 3_600_000


@pytest.mark.asyncio
@pytest.mark.parametrize("prior_observed_replicas", (0, 1))
async def test_stopped_group_with_failed_instance_read_preserves_conservative_metering(
    deployment_context: DeploymentContext,
    prior_observed_replicas: int,
) -> None:
    await make_fully_provisioned(
        deployment_context,
        last_observed_at=NOW,
        observed_replicas=prior_observed_replicas,
    )
    client = FakeClient()
    queue_name, group_name = remote_names()
    client.queues[queue_name] = make_queue(queue_name, group_name=group_name)
    client.groups[group_name] = make_group(
        group_name,
        queue_name,
        status="stopped",
        replicas=1,
        running=0,
        finish_time=None,
    )
    client.list_instances_error = SaladTransportError("instance read failed")

    async with deployment_context.database.sessions() as session:
        result = await reconcile_deployment(
            session,
            deployment_id=deployment_context.deployment_id,
            client=client,
            now=NOW + timedelta(hours=1),
        )
        await session.commit()
        deployment = await session.get(SaladDeployment, deployment_context.deployment_id)
        entries = list((await session.scalars(select(ProviderSpendEntry))).all())

    assert result.metered_microusd == 3_600_000
    assert deployment is not None and deployment.billing_observation_stale is True
    assert len(entries) == 1
    assert entries[0].amount_microusd == 3_600_000


def test_observed_replicas_preserves_active_and_pending_lifecycle_counts() -> None:
    queue_name, group_name = remote_names()

    active = make_group(group_name, queue_name, status="running", replicas=1)
    pending = make_group(
        group_name,
        queue_name,
        status="running",
        replicas=1,
        pending_change=True,
    )
    stopped_with_live_instance = make_group(
        group_name,
        queue_name,
        status="stopped",
        replicas=1,
        running=1,
    )

    assert _observed_replicas(active) == 1
    assert _observed_replicas(pending) == 1
    assert _observed_replicas(stopped_with_live_instance) == 1


@pytest.mark.asyncio
async def test_runtime_metering_error_fails_closed_and_stops_provider(
    deployment_context: DeploymentContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await make_fully_provisioned(
        deployment_context,
        last_observed_at=NOW,
        observed_replicas=1,
    )
    client = FakeClient()
    queue_name, group_name = remote_names()
    client.queues[queue_name] = make_queue(queue_name, group_name=group_name)
    client.groups[group_name] = make_group(group_name, queue_name, running=1)

    async def fail_metering(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise BudgetError("do not persist this text")

    monkeypatch.setattr(deployment_service, "record_spend_entry", fail_metering)
    async with deployment_context.database.sessions() as session:
        result = await reconcile_deployment(
            session,
            deployment_id=deployment_context.deployment_id,
            client=client,
            now=NOW + timedelta(minutes=1),
        )
        await session.commit()
        deployment = await session.get(SaladDeployment, deployment_context.deployment_id)

    assert result.action == DeploymentAction.STOP_REQUESTED
    assert deployment is not None
    assert deployment.desired_state == DesiredDeploymentState.STOPPED
    assert "do not persist" not in str(deployment.last_error_detail)
    assert client.stop_names == [group_name]


@pytest.mark.asyncio
async def test_runtime_metering_splits_at_utc_day_boundary(
    deployment_context: DeploymentContext,
) -> None:
    start = datetime(2026, 7, 28, 23, 30, tzinfo=UTC)
    await make_fully_provisioned(
        deployment_context,
        last_observed_at=start,
        observed_replicas=1,
    )
    client = FakeClient()
    queue_name, group_name = remote_names()
    client.queues[queue_name] = make_queue(queue_name, group_name=group_name)
    client.groups[group_name] = make_group(group_name, queue_name, status="running")

    async with deployment_context.database.sessions() as session:
        result = await reconcile_deployment(
            session,
            deployment_id=deployment_context.deployment_id,
            client=client,
            now=start + timedelta(hours=1),
        )
        await session.commit()
        entries = list(
            (
                await session.scalars(
                    select(ProviderSpendEntry).order_by(ProviderSpendEntry.effective_at)
                )
            ).all()
        )

    assert result.metered_microusd == 3_600_000
    assert [entry.amount_microusd for entry in entries] == [1_800_000, 1_800_000]
    assert len({entry.dedupe_key for entry in entries}) == 2


@pytest.mark.asyncio
async def test_runtime_spend_engages_budget_kill_switch_and_provider_stop(
    deployment_context: DeploymentContext,
) -> None:
    await make_fully_provisioned(
        deployment_context,
        last_observed_at=NOW,
        observed_replicas=1,
    )
    client = FakeClient()
    queue_name, group_name = remote_names()
    client.queues[queue_name] = make_queue(queue_name, group_name=group_name)
    client.groups[group_name] = make_group(group_name, queue_name, running=1)
    async with deployment_context.database.sessions() as session:
        guard = await session.scalar(
            select(ProviderBudgetGuard).where(ProviderBudgetGuard.provider == "salad")
        )
        assert guard is not None
        guard.daily_limit_microusd = 1_000_000
        guard.monthly_limit_microusd = 10_000_000
        await session.commit()

        result = await reconcile_deployment(
            session,
            deployment_id=deployment_context.deployment_id,
            client=client,
            now=NOW + timedelta(hours=1),
        )
        await session.commit()
        await session.refresh(guard)
        deployment = await session.get(SaladDeployment, deployment_context.deployment_id)

    assert result.action == DeploymentAction.STOP_REQUESTED
    assert result.metered_microusd == 3_600_000
    assert guard.state == BudgetState.BLOCKED
    assert deployment is not None
    assert deployment.desired_state == DesiredDeploymentState.STOPPED
    assert deployment.state == SaladDeploymentState.DRAINING
    assert client.stop_names == [group_name]
    assert [call[0] for call in client.calls] == [
        "get_queue",
        "get_group",
        "list_instances",
        "get_group",
        "list_instances",
        "stop_group",
    ]


@pytest.mark.asyncio
async def test_preblocked_budget_stops_existing_group_before_provisioning_work(
    deployment_context: DeploymentContext,
) -> None:
    await make_fully_provisioned(deployment_context)
    client = FakeClient()
    queue_name, group_name = remote_names()
    client.groups[group_name] = make_group(group_name, queue_name, status="running")
    async with deployment_context.database.sessions() as session:
        guard = await session.scalar(
            select(ProviderBudgetGuard).where(ProviderBudgetGuard.provider == "salad")
        )
        assert guard is not None
        guard.daily_limit_microusd = 1_000_000
        guard.monthly_limit_microusd = 10_000_000
        await record_spend_entry(
            session,
            provider="salad",
            dedupe_key="preexisting-runtime-charge",
            entry_type=SpendEntryType.USAGE,
            amount_microusd=2_000_000,
            effective_at=NOW,
            salad_deployment_id=deployment_context.deployment_id,
            now=NOW,
        )
        await session.commit()

        result = await provision_deployment_step(
            session,
            deployment_id=deployment_context.deployment_id,
            client=client,
            now=NOW + timedelta(minutes=1),
        )
        await session.commit()

    assert result.action == DeploymentAction.STOP_REQUESTED
    assert result.error_code == "budget_limit_exceeded"
    assert client.stop_names == [group_name]


@pytest.mark.asyncio
async def test_missing_budget_guard_fails_closed_before_provider_mutation(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'no-budget.db').as_posix()}")
    await database.create_schema()
    client = FakeClient()
    try:
        async with database.sessions() as session:
            deployment = SaladDeployment(
                version_no=1,
                config_sha256=CONFIG_SHA256,
                provider_configuration=provider_configuration(),
                worker_image_digest=IMAGE_DIGEST,
                organization_name="creator-org",
                project_name="production",
                queue_name="generation",
                container_group_name="worker",
                is_current=True,
                max_hourly_cost_microusd=3_600_000,
            )
            session.add(deployment)
            await session.commit()
            result = await provision_deployment_step(
                session,
                deployment_id=deployment.id,
                client=client,
                now=NOW,
            )
            await session.commit()
            await session.refresh(deployment)

        assert result.action == DeploymentAction.STOPPED
        assert result.state == SaladDeploymentState.FAILED
        assert deployment.desired_state == DesiredDeploymentState.STOPPED
        assert deployment.last_error_code == "budget_guard_unavailable"
        assert client.calls == [
            ("get_group", remote_names()[1]),
            ("get_queue", remote_names()[0]),
        ]
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_invalid_group_configuration_fails_before_resolving_or_posting(
    deployment_context: DeploymentContext,
) -> None:
    client = FakeClient()
    queue_name, _ = remote_names()
    client.queues[queue_name] = make_queue(queue_name)
    resolver = FakeResolver({})
    async with deployment_context.database.sessions() as session:
        deployment = await session.get(SaladDeployment, deployment_context.deployment_id)
        assert deployment is not None
        deployment.queue_name = queue_name
        deployment.provider_queue_id = str(QUEUE_ID)
        deployment.provider_configuration = {
            "container": {"resources": {"cpu": 4}},
            "replicas": 0,
        }
        deployment.state = SaladDeploymentState.PROVISIONING
        await session.commit()

        result = await provision_deployment_step(
            session,
            deployment_id=deployment.id,
            client=client,
            secret_resolver=resolver,
            now=NOW,
        )
        await session.commit()

    assert result.action == DeploymentAction.FAILED
    assert result.error_code == "invalid_container_group_configuration"
    assert client.created_group_payloads == []
    assert resolver.requests == []


@pytest.mark.asyncio
async def test_reconciliation_detects_immutable_image_drift(
    deployment_context: DeploymentContext,
) -> None:
    await make_fully_provisioned(deployment_context)
    client = FakeClient()
    queue_name, group_name = remote_names()
    client.queues[queue_name] = make_queue(queue_name)
    client.groups[group_name] = make_group(
        group_name,
        queue_name,
        image="registry.example.test/worker@sha256:" + "d" * 64,
    )
    async with deployment_context.database.sessions() as session:
        result = await provision_deployment_step(
            session,
            deployment_id=deployment_context.deployment_id,
            client=client,
            now=NOW + timedelta(minutes=1),
        )
        await session.commit()

    assert result.state == SaladDeploymentState.DEGRADED
    assert result.error_code == "provider_image_drift"


@pytest.mark.asyncio
async def test_reconciliation_repairs_missing_autoscaler_before_activation(
    deployment_context: DeploymentContext,
) -> None:
    await make_fully_provisioned(deployment_context)
    client = FakeClient()
    queue_name, group_name = remote_names()
    client.queues[queue_name] = make_queue(queue_name, group_name=group_name)
    group = make_group(group_name, queue_name)
    group.raw.pop("queue_autoscaler")
    client.groups[group_name] = group

    async with deployment_context.database.sessions() as session:
        result = await reconcile_deployment(
            session,
            deployment_id=deployment_context.deployment_id,
            client=client,
            now=NOW + timedelta(minutes=1),
        )
        await session.commit()

    assert result.action == DeploymentAction.AUTOSCALER_REPAIR_REQUESTED
    assert result.state == SaladDeploymentState.PROVISIONING
    assert result.error_code == "provider_autoscaler_repair_pending"
    assert client.updated_group_patches == [
        {
            "queue_autoscaler": {
                "polling_period": 30,
                "min_replicas": 0,
                "max_replicas": 1,
                "desired_queue_length": 1,
            }
        }
    ]
    assert client.start_names == []
    assert [call[0] for call in client.calls] == [
        "get_queue",
        "get_group",
        "list_instances",
        "update_group",
    ]


@pytest.mark.asyncio
async def test_pending_image_preparation_tracks_safe_progress_without_mutation(
    deployment_context: DeploymentContext,
) -> None:
    await make_fully_provisioned(deployment_context)
    client = FakeClient()
    queue_name, group_name = remote_names()
    client.queues[queue_name] = make_queue(queue_name)
    untrusted_text = "user:SUPERSECRET@registry.example.test/private?token=opaque"
    group = make_group(
        group_name,
        queue_name,
        status="preparing",
        description=f"Downloading image from {untrusted_text} at 4%",
        pending_change=True,
    )
    group.raw.pop("queue_autoscaler")
    client.groups[group_name] = group
    observed_at = NOW + timedelta(minutes=1)

    async with deployment_context.database.sessions() as session:
        result = await reconcile_deployment(
            session,
            deployment_id=deployment_context.deployment_id,
            client=client,
            now=observed_at,
        )
        await session.commit()
        deployment = await session.get(SaladDeployment, deployment_context.deployment_id)
        assert deployment is not None
        audit_details = list((await session.scalars(select(AuditEvent.detail))).all())

    assert result.action == DeploymentAction.RECONCILED
    assert result.state == SaladDeploymentState.PROVISIONING
    assert result.error_code == "provider_image_preparation_pending"
    assert deployment.provider_status == (
        "queue=0;group=preparing;pending=1;phase=image_pull;progress=4"
    )
    assert len(deployment.provider_status) <= 100
    assert deployment.unknown_since is not None
    assert deployment.unknown_since.replace(tzinfo=UTC) == observed_at
    assert untrusted_text not in deployment.provider_status
    assert untrusted_text not in (deployment.last_error_detail or "")
    assert all(untrusted_text not in str(detail) for detail in audit_details)
    assert client.updated_group_patches == []
    assert client.start_names == []
    assert client.stop_names == []


@pytest.mark.asyncio
async def test_pending_image_preparation_progress_resets_stall_clock(
    deployment_context: DeploymentContext,
) -> None:
    await make_fully_provisioned(deployment_context)
    client = FakeClient()
    queue_name, group_name = remote_names()
    client.queues[queue_name] = make_queue(queue_name)
    client.groups[group_name] = make_group(
        group_name,
        queue_name,
        status="preparing",
        description="Downloading image 4%",
        pending_change=True,
    )
    first_observed_at = NOW + timedelta(minutes=1)
    second_observed_at = first_observed_at + timedelta(minutes=40)

    async with deployment_context.database.sessions() as session:
        await reconcile_deployment(
            session,
            deployment_id=deployment_context.deployment_id,
            client=client,
            now=first_observed_at,
        )
        await session.commit()
        client.groups[group_name] = make_group(
            group_name,
            queue_name,
            status="preparing",
            description="Downloading image 5%",
            pending_change=True,
        )
        result = await reconcile_deployment(
            session,
            deployment_id=deployment_context.deployment_id,
            client=client,
            now=second_observed_at,
        )
        await session.commit()
        deployment = await session.get(SaladDeployment, deployment_context.deployment_id)
        assert deployment is not None

    assert result.state == SaladDeploymentState.PROVISIONING
    assert result.error_code == "provider_image_preparation_pending"
    assert deployment.unknown_since is not None
    assert deployment.unknown_since.replace(tzinfo=UTC) == second_observed_at
    assert deployment.provider_status is not None
    assert deployment.provider_status.endswith("phase=image_pull;progress=5")
    assert client.updated_group_patches == []
    assert client.start_names == []
    assert client.stop_names == []


@pytest.mark.asyncio
async def test_unchanged_pending_image_preparation_stalls_read_only_after_30_minutes(
    deployment_context: DeploymentContext,
) -> None:
    await make_fully_provisioned(deployment_context)
    client = FakeClient()
    queue_name, group_name = remote_names()
    client.queues[queue_name] = make_queue(queue_name)
    group = make_group(
        group_name,
        queue_name,
        status="preparing",
        description="Pulling image 4%",
        pending_change=True,
    )
    group.raw.pop("queue_autoscaler")
    client.groups[group_name] = group
    first_observed_at = NOW + timedelta(minutes=1)

    async with deployment_context.database.sessions() as session:
        await reconcile_deployment(
            session,
            deployment_id=deployment_context.deployment_id,
            client=client,
            now=first_observed_at,
        )
        await session.commit()
        result = await reconcile_deployment(
            session,
            deployment_id=deployment_context.deployment_id,
            client=client,
            now=first_observed_at + timedelta(minutes=30),
        )
        await session.commit()
        deployment = await session.get(SaladDeployment, deployment_context.deployment_id)
        assert deployment is not None

    assert result.action == DeploymentAction.RECONCILED
    assert result.state == SaladDeploymentState.DEGRADED
    assert result.error_code == "provider_image_preparation_stalled"
    assert deployment.unknown_since is not None
    assert deployment.unknown_since.replace(tzinfo=UTC) == first_observed_at
    assert client.updated_group_patches == []
    assert client.start_names == []
    assert client.stop_names == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("second_description", "expected_snapshot"),
    [
        ("Downloading image 4%", "phase=image_pull;progress=4"),
        ("Preparing container 5%", "phase=preparing;progress=5"),
    ],
)
async def test_pending_snapshot_change_resets_stall_clock(
    deployment_context: DeploymentContext,
    second_description: str,
    expected_snapshot: str,
) -> None:
    await make_fully_provisioned(deployment_context)
    client = FakeClient()
    queue_name, group_name = remote_names()
    client.queues[queue_name] = make_queue(queue_name)
    client.groups[group_name] = make_group(
        group_name,
        queue_name,
        status="preparing",
        description="Downloading image 5%",
        pending_change=True,
    )
    first_observed_at = NOW + timedelta(minutes=1)
    second_observed_at = first_observed_at + timedelta(minutes=31)

    async with deployment_context.database.sessions() as session:
        await reconcile_deployment(
            session,
            deployment_id=deployment_context.deployment_id,
            client=client,
            now=first_observed_at,
        )
        await session.commit()
        client.groups[group_name] = make_group(
            group_name,
            queue_name,
            status="preparing",
            description=second_description,
            pending_change=True,
        )
        result = await reconcile_deployment(
            session,
            deployment_id=deployment_context.deployment_id,
            client=client,
            now=second_observed_at,
        )
        await session.commit()
        deployment = await session.get(SaladDeployment, deployment_context.deployment_id)
        assert deployment is not None

    assert result.state == SaladDeploymentState.PROVISIONING
    assert result.error_code == "provider_image_preparation_pending"
    assert deployment.unknown_since is not None
    assert deployment.unknown_since.replace(tzinfo=UTC) == second_observed_at
    assert deployment.provider_status is not None
    assert deployment.provider_status.endswith(expected_snapshot)
    assert client.updated_group_patches == []
    assert client.start_names == []
    assert client.stop_names == []


@pytest.mark.asyncio
async def test_pending_clear_resets_tracking_and_repairs_autoscaler(
    deployment_context: DeploymentContext,
) -> None:
    await make_fully_provisioned(deployment_context)
    client = FakeClient()
    queue_name, group_name = remote_names()
    client.queues[queue_name] = make_queue(queue_name)
    pending_group = make_group(
        group_name,
        queue_name,
        status="preparing",
        description="Downloading image 4%",
        pending_change=True,
    )
    pending_group.raw.pop("queue_autoscaler")
    client.groups[group_name] = pending_group
    first_observed_at = NOW + timedelta(minutes=1)

    async with deployment_context.database.sessions() as session:
        await reconcile_deployment(
            session,
            deployment_id=deployment_context.deployment_id,
            client=client,
            now=first_observed_at,
        )
        await session.commit()
        settled_group = make_group(
            group_name,
            queue_name,
            status="deploying",
            description="deploying",
            pending_change=False,
        )
        settled_group.raw.pop("queue_autoscaler")
        client.groups[group_name] = settled_group
        result = await reconcile_deployment(
            session,
            deployment_id=deployment_context.deployment_id,
            client=client,
            now=first_observed_at + timedelta(minutes=31),
        )
        await session.commit()
        deployment = await session.get(SaladDeployment, deployment_context.deployment_id)
        assert deployment is not None

    assert result.action == DeploymentAction.AUTOSCALER_REPAIR_REQUESTED
    assert result.state == SaladDeploymentState.PROVISIONING
    assert result.error_code == "provider_autoscaler_repair_pending"
    assert deployment.unknown_since is None
    assert deployment.provider_status == "queue=0;group=deploying;pending=0"
    assert client.updated_group_patches == [
        {
            "queue_autoscaler": {
                "polling_period": 30,
                "min_replicas": 0,
                "max_replicas": 1,
                "desired_queue_length": 1,
            }
        }
    ]
    assert client.start_names == []
    assert client.stop_names == []


@pytest.mark.asyncio
async def test_reconciliation_leaves_stopped_active_group_idle_without_demand(
    deployment_context: DeploymentContext,
) -> None:
    await make_fully_provisioned(deployment_context)
    client = FakeClient()
    queue_name, group_name = remote_names()
    client.queues[queue_name] = make_queue(queue_name)
    group = make_group(
        group_name,
        queue_name,
        status="stopped",
        start_time=None,
        finish_time=NOW,
    )
    group.raw["autostart_policy"] = False
    client.groups[group_name] = group

    async with deployment_context.database.sessions() as session:
        assert (
            await deployment_service.effective_worker_min_replicas(
                session,
                salad_deployment_id=deployment_context.deployment_id,
                now=NOW + timedelta(minutes=1),
            )
            == 0
        )
        result = await reconcile_deployment(
            session,
            deployment_id=deployment_context.deployment_id,
            client=client,
            now=NOW + timedelta(minutes=1),
        )
        await session.commit()

    assert result.action == DeploymentAction.RECONCILED
    assert result.state == SaladDeploymentState.ACTIVE
    assert result.error_code is None
    assert client.start_names == []
    assert client.updated_group_patches == []


@pytest.mark.asyncio
async def test_reconciliation_uses_status_to_start_stopped_active_group_with_demand(
    deployment_context: DeploymentContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await make_fully_provisioned(deployment_context)
    client = FakeClient()
    queue_name, group_name = remote_names()
    client.queues[queue_name] = make_queue(queue_name)
    group = make_group(
        group_name,
        queue_name,
        status="stopped",
        start_time=None,
        finish_time=NOW,
        autoscaler={
            "min_replicas": 1,
            "max_replicas": 1,
            "desired_queue_length": 1,
            "polling_period": 30,
        },
    )
    group.raw["autostart_policy"] = False
    client.groups[group_name] = group

    async def demand_one_worker(*_args: object, **_kwargs: object) -> int:
        return 1

    monkeypatch.setattr(
        deployment_service,
        "effective_worker_min_replicas",
        demand_one_worker,
    )

    async with deployment_context.database.sessions() as session:
        observed_at = NOW + timedelta(minutes=1)
        result = await reconcile_deployment(
            session,
            deployment_id=deployment_context.deployment_id,
            client=client,
            now=observed_at,
        )
        await session.commit()
        deployment = await session.get(SaladDeployment, deployment_context.deployment_id)
        assert deployment is not None

    assert result.action == DeploymentAction.START_REQUESTED
    assert result.state == SaladDeploymentState.PROVISIONING
    assert result.error_code == "provider_start_pending"
    assert deployment.unknown_since is not None
    assert deployment.unknown_since.replace(tzinfo=UTC) == observed_at
    assert client.start_names == [group_name]
    assert client.updated_group_patches == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("elapsed", "expected_state", "expected_error"),
    [
        (timedelta(minutes=29), SaladDeploymentState.PROVISIONING, "provider_start_pending"),
        (timedelta(minutes=30), SaladDeploymentState.DEGRADED, "provider_start_stalled"),
    ],
)
async def test_reconciliation_bounds_zero_ready_start_wait_without_provider_mutation(
    deployment_context: DeploymentContext,
    monkeypatch: pytest.MonkeyPatch,
    elapsed: timedelta,
    expected_state: SaladDeploymentState,
    expected_error: str,
) -> None:
    await make_fully_provisioned(deployment_context)
    client = FakeClient()
    queue_name, group_name = remote_names()
    client.queues[queue_name] = make_queue(queue_name)
    client.groups[group_name] = make_group(
        group_name,
        queue_name,
        status="deploying",
        autoscaler={
            "min_replicas": 1,
            "max_replicas": 1,
            "desired_queue_length": 1,
            "polling_period": 30,
        },
    )

    async def demand_one_worker(*_args: object, **_kwargs: object) -> int:
        return 1

    monkeypatch.setattr(
        deployment_service,
        "effective_worker_min_replicas",
        demand_one_worker,
    )
    first_observed_at = NOW + timedelta(minutes=1)

    async with deployment_context.database.sessions() as session:
        first = await reconcile_deployment(
            session,
            deployment_id=deployment_context.deployment_id,
            client=client,
            now=first_observed_at,
        )
        await session.commit()
        result = await reconcile_deployment(
            session,
            deployment_id=deployment_context.deployment_id,
            client=client,
            now=first_observed_at + elapsed,
        )
        await session.commit()
        deployment = await session.get(SaladDeployment, deployment_context.deployment_id)
        assert deployment is not None

    assert first.state == SaladDeploymentState.PROVISIONING
    assert first.error_code == "provider_start_pending"
    assert result.state == expected_state
    assert result.error_code == expected_error
    assert deployment.unknown_since is not None
    assert deployment.unknown_since.replace(tzinfo=UTC) == first_observed_at
    assert client.start_names == []
    assert client.updated_group_patches == []
    assert client.stop_names == []


@pytest.mark.asyncio
async def test_accepted_start_is_not_repeated_and_keeps_one_stall_clock(
    deployment_context: DeploymentContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await make_fully_provisioned(deployment_context)
    client = FakeClient()
    queue_name, group_name = remote_names()
    client.queues[queue_name] = make_queue(queue_name)
    warm_autoscaler = {
        "min_replicas": 1,
        "max_replicas": 1,
        "desired_queue_length": 1,
        "polling_period": 30,
    }
    client.groups[group_name] = make_group(
        group_name,
        queue_name,
        status="stopped",
        start_time=None,
        finish_time=NOW,
        autoscaler=warm_autoscaler,
    )
    demand = 1

    async def demand_worker(*_args: object, **_kwargs: object) -> int:
        return demand

    monkeypatch.setattr(
        deployment_service,
        "effective_worker_min_replicas",
        demand_worker,
    )
    first_observed_at = NOW + timedelta(minutes=1)

    async with deployment_context.database.sessions() as session:
        first = await reconcile_deployment(
            session,
            deployment_id=deployment_context.deployment_id,
            client=client,
            now=first_observed_at,
        )
        await session.commit()
        second = await reconcile_deployment(
            session,
            deployment_id=deployment_context.deployment_id,
            client=client,
            now=first_observed_at + timedelta(minutes=15),
        )
        await session.commit()
        client.groups[group_name] = make_group(
            group_name,
            queue_name,
            status="deploying",
            autoscaler=warm_autoscaler,
        )
        third = await reconcile_deployment(
            session,
            deployment_id=deployment_context.deployment_id,
            client=client,
            now=first_observed_at + timedelta(minutes=20),
        )
        await session.commit()
        stalled = await reconcile_deployment(
            session,
            deployment_id=deployment_context.deployment_id,
            client=client,
            now=first_observed_at + timedelta(minutes=30),
        )
        await session.commit()
        deployment = await session.get(SaladDeployment, deployment_context.deployment_id)
        assert deployment is not None
        stalled_since = deployment.unknown_since

        demand = 0
        client.groups[group_name] = make_group(
            group_name,
            queue_name,
            status="stopped",
            start_time=None,
            finish_time=first_observed_at + timedelta(minutes=31),
        )
        idle = await reconcile_deployment(
            session,
            deployment_id=deployment_context.deployment_id,
            client=client,
            now=first_observed_at + timedelta(minutes=31),
        )
        await session.commit()
        await session.refresh(deployment)

    assert first.action == DeploymentAction.START_REQUESTED
    assert first.error_code == "provider_start_pending"
    assert second.state == SaladDeploymentState.PROVISIONING
    assert second.error_code == "provider_start_pending"
    assert third.state == SaladDeploymentState.PROVISIONING
    assert third.error_code == "provider_start_pending"
    assert stalled.state == SaladDeploymentState.DEGRADED
    assert stalled.error_code == "provider_start_stalled"
    assert stalled_since is not None
    assert stalled_since.replace(tzinfo=UTC) == first_observed_at
    assert client.start_names == [group_name]
    assert client.updated_group_patches == []
    assert client.stop_names == []
    assert idle.state == SaladDeploymentState.ACTIVE
    assert idle.error_code is None
    assert deployment.unknown_since is None


@pytest.mark.asyncio
async def test_ready_start_readback_clears_wait_tracking(
    deployment_context: DeploymentContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await make_fully_provisioned(deployment_context)
    client = FakeClient()
    queue_name, group_name = remote_names()
    client.queues[queue_name] = make_queue(queue_name)
    autoscaler = {
        "min_replicas": 1,
        "max_replicas": 1,
        "desired_queue_length": 1,
        "polling_period": 30,
    }
    client.groups[group_name] = make_group(
        group_name,
        queue_name,
        status="deploying",
        autoscaler=autoscaler,
    )

    async def demand_one_worker(*_args: object, **_kwargs: object) -> int:
        return 1

    monkeypatch.setattr(
        deployment_service,
        "effective_worker_min_replicas",
        demand_one_worker,
    )
    first_observed_at = NOW + timedelta(minutes=1)

    async with deployment_context.database.sessions() as session:
        await reconcile_deployment(
            session,
            deployment_id=deployment_context.deployment_id,
            client=client,
            now=first_observed_at,
        )
        await session.commit()
        client.groups[group_name] = make_group(
            group_name,
            queue_name,
            status="running",
            replicas=1,
            running=1,
            autoscaler=autoscaler,
        )
        result = await reconcile_deployment(
            session,
            deployment_id=deployment_context.deployment_id,
            client=client,
            now=first_observed_at + timedelta(minutes=31),
        )
        await session.commit()
        deployment = await session.get(SaladDeployment, deployment_context.deployment_id)
        assert deployment is not None

    assert result.state == SaladDeploymentState.ACTIVE
    assert result.error_code is None
    assert deployment.unknown_since is None
    assert deployment.observed_replicas == 1
    assert deployment.ready_replicas == 1
    assert client.start_names == []
    assert client.updated_group_patches == []
    assert client.stop_names == []


@pytest.mark.asyncio
async def test_zero_ready_deploying_group_remains_valid_while_idle(
    deployment_context: DeploymentContext,
) -> None:
    await make_fully_provisioned(deployment_context)
    client = FakeClient()
    queue_name, group_name = remote_names()
    client.queues[queue_name] = make_queue(queue_name)
    client.groups[group_name] = make_group(group_name, queue_name, status="deploying")

    async with deployment_context.database.sessions() as session:
        result = await reconcile_deployment(
            session,
            deployment_id=deployment_context.deployment_id,
            client=client,
            now=NOW + timedelta(minutes=1),
        )
        await session.commit()
        deployment = await session.get(SaladDeployment, deployment_context.deployment_id)
        assert deployment is not None

    assert result.state == SaladDeploymentState.ACTIVE
    assert result.error_code is None
    assert deployment.unknown_since is None
    assert client.start_names == []
    assert client.updated_group_patches == []
    assert client.stop_names == []


@pytest.mark.asyncio
async def test_reconciliation_accepts_live_readback_without_autostart_or_queue_membership(
    deployment_context: DeploymentContext,
) -> None:
    await make_fully_provisioned(deployment_context)
    client = FakeClient()
    queue_name, group_name = remote_names()
    client.queues[queue_name] = make_queue(queue_name)
    group = make_group(group_name, queue_name, status="running")
    group.raw["autostart_policy"] = False
    client.groups[group_name] = group

    async with deployment_context.database.sessions() as session:
        result = await reconcile_deployment(
            session,
            deployment_id=deployment_context.deployment_id,
            client=client,
            now=NOW + timedelta(minutes=1),
        )
        await session.commit()

    assert result.action == DeploymentAction.RECONCILED
    assert result.state == SaladDeploymentState.ACTIVE
    assert result.error_code is None
    assert client.start_names == []
    assert client.updated_group_patches == []


def group_with_configuration_override(
    key: str,
    value: JSONValue,
) -> SaladContainerGroup:
    group = make_group(remote_names()[1], remote_names()[0])
    group.raw[key] = value
    return group


@pytest.mark.parametrize(
    ("group", "expected"),
    [
        (
            make_group(
                remote_names()[1],
                "wrong-queue",
            ),
            "provider_queue_connection_drift",
        ),
        (
            group_with_configuration_override(
                "queue_connection",
                {
                    "queue_name": remote_names()[0],
                    "path": "/wrong",
                    "port": 8000,
                },
            ),
            "provider_queue_connection_drift",
        ),
        (
            group_with_configuration_override(
                "queue_connection",
                {
                    "queue_name": remote_names()[0],
                    "path": "/jobs/generate",
                    "port": 8080,
                },
            ),
            "provider_queue_connection_drift",
        ),
        (
            group_with_configuration_override("restart_policy", "always"),
            "provider_restart_policy_drift",
        ),
        (
            group_with_configuration_override(
                "startup_probe",
                {
                    **STARTUP_PROBE,
                    "failure_threshold": 3,
                },
            ),
            "provider_startup_probe_drift",
        ),
        (
            group_with_configuration_override(
                "readiness_probe",
                {
                    **READINESS_PROBE,
                    "http": {
                        "headers": [],
                        "path": "/health",
                        "port": 8000,
                        "scheme": "http",
                    },
                },
            ),
            "provider_readiness_probe_drift",
        ),
        (
            group_with_configuration_override("liveness_probe", None),
            "provider_liveness_probe_drift",
        ),
        (
            make_group(
                remote_names()[1],
                remote_names()[0],
                autoscaler={"min_replicas": 0},
            ),
            "provider_autoscaler_drift",
        ),
        (
            group_with_configuration_override("priority", "high"),
            "provider_priority_drift",
        ),
        (
            make_group(
                remote_names()[1],
                remote_names()[0],
                replicas=2,
            ),
            "provider_replica_limit_drift",
        ),
    ],
)
def test_group_configuration_drift_variants(
    group: SaladContainerGroup,
    expected: str,
) -> None:
    assert _group_configuration_drift(unpersisted_deployment(), group) == expected


def test_group_configuration_rejects_omitted_autoscaler() -> None:
    group = make_group(remote_names()[1], remote_names()[0])
    group.raw.pop("queue_autoscaler")

    assert (
        _group_configuration_drift(unpersisted_deployment(), group) == "provider_autoscaler_drift"
    )


def test_group_configuration_ignores_autostart_policy_readback() -> None:
    group = make_group(remote_names()[1], remote_names()[0])
    group.raw["autostart_policy"] = False

    assert _group_configuration_drift(unpersisted_deployment(), group) is None


def test_group_configuration_uses_deployment_autoscaler_values() -> None:
    deployment = unpersisted_deployment()
    deployment.max_replicas = 3
    deployment.desired_queue_length = 2
    group = make_group(
        remote_names()[1],
        remote_names()[0],
        autoscaler={
            "min_replicas": 0,
            "max_replicas": 3,
            "desired_queue_length": 2,
            "polling_period": 30,
        },
    )

    assert _group_configuration_drift(deployment, group) is None


def test_remote_drift_ignores_queue_group_membership_readback() -> None:
    deployment = unpersisted_deployment()
    deployment.provider_queue_id = str(QUEUE_ID)
    deployment.provider_container_group_id = str(GROUP_ID)
    group = make_group(deployment.container_group_name, deployment.queue_name)

    assert _remote_drift_code(deployment, make_queue(deployment.queue_name), group) is None
    assert (
        _remote_drift_code(
            deployment,
            make_queue(
                deployment.queue_name,
                group_name=deployment.container_group_name,
                group_id=uuid4(),
            ),
            group,
        )
        is None
    )
    assert (
        _remote_drift_code(
            deployment,
            make_queue(
                deployment.queue_name,
                group_name=deployment.container_group_name,
            ),
            group,
        )
        is None
    )


def test_group_configuration_accepts_only_salad_default_shm_size() -> None:
    default_group = make_group(remote_names()[1], remote_names()[0])
    default_container = default_group.raw["container"]
    assert isinstance(default_container, dict)
    default_resources = default_container["resources"]
    assert isinstance(default_resources, dict)
    default_resources["shm_size"] = 64
    assert _group_configuration_drift(unpersisted_deployment(), default_group) is None

    changed_group = make_group(remote_names()[1], remote_names()[0])
    changed_container = changed_group.raw["container"]
    assert isinstance(changed_container, dict)
    changed_resources = changed_container["resources"]
    assert isinstance(changed_resources, dict)
    changed_resources["shm_size"] = 128
    assert (
        _group_configuration_drift(unpersisted_deployment(), changed_group)
        == "provider_container_contract_drift"
    )


@pytest.mark.asyncio
async def test_rate_limit_is_deferred_but_definitive_rejection_fails(
    deployment_context: DeploymentContext,
) -> None:
    client = FakeClient()
    client.create_queue_error = SaladRateLimitError(
        message="slow down",
        response_body="safe",
        request_id=None,
        retry_after_seconds=10,
    )
    async with deployment_context.database.sessions() as session:
        deferred = await provision_deployment_step(
            session,
            deployment_id=deployment_context.deployment_id,
            client=client,
            now=NOW,
        )
        await session.commit()
        deployment = await session.get(SaladDeployment, deployment_context.deployment_id)
        assert deployment is not None
        deployment.state = SaladDeploymentState.PLANNED
        client.create_queue_error = api_error(400)
        failed = await provision_deployment_step(
            session,
            deployment_id=deployment_context.deployment_id,
            client=client,
            now=NOW + timedelta(minutes=1),
        )
        await session.commit()

    assert deferred.action == DeploymentAction.DEFERRED
    assert deferred.error_code == "queue_create_rate_limited"
    assert failed.action == DeploymentAction.FAILED
    assert failed.error_code == "queue_create_rejected"


@pytest.mark.asyncio
async def test_missing_and_invalid_local_deployment_are_rejected(
    deployment_context: DeploymentContext,
) -> None:
    client = FakeClient()
    async with deployment_context.database.sessions() as session:
        with pytest.raises(SaladDeploymentNotFoundError):
            await reconcile_deployment(
                session,
                deployment_id=uuid4(),
                client=client,
                now=NOW,
            )
        deployment = await session.get(SaladDeployment, deployment_context.deployment_id)
        assert deployment is not None
        deployment.worker_image_digest = "mutable:latest"
        await session.flush()
        with pytest.raises(SaladDeploymentValidationError, match="immutable"):
            await provision_deployment_step(
                session,
                deployment_id=deployment.id,
                client=client,
                now=NOW,
            )


def test_local_replica_and_hourly_limits_are_fail_closed() -> None:
    deployment = unpersisted_deployment()
    deployment.max_replicas = 0
    with pytest.raises(SaladDeploymentValidationError, match="autoscaler"):
        _validate_local_deployment(deployment)
    deployment.max_replicas = 1
    deployment.max_hourly_cost_microusd = 0
    with pytest.raises(SaladDeploymentValidationError, match="hourly"):
        _validate_local_deployment(deployment)


@pytest.mark.asyncio
async def test_spend_entry_count_query_smoke(
    deployment_context: DeploymentContext,
) -> None:
    async with deployment_context.database.sessions() as session:
        count = await session.scalar(select(func.count()).select_from(ProviderSpendEntry))
    assert count == 0
