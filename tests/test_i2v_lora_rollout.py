from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from pydantic import AnyHttpUrl, SecretStr

from gen_automation.config import SaladContainerPriority, Settings
from gen_automation.db.session import Database
from gen_automation.i2v_worker.settings import I2VWorkerSettings
from gen_automation.integrations.salad.models import (
    JSONObject,
    SaladContainerGroup,
    SaladContainerGroupInstance,
    SaladContainerGroupInstancePage,
    SaladContainerGroupInstanceState,
    SaladContainerGroupState,
    SaladGpuClass,
    SaladJobStatus,
    SaladQueue,
    SaladQueueJob,
    SaladQueueJobPage,
)
from gen_automation.services import i2v_lora_rollout as rollout
from gen_automation.services.i2v_environment import I2VRuntimeEnvironment, i2v_worker_identity
from gen_automation.services.i2v_lora_rollout import (
    I2VLoraRolloutError,
    PreparedReviewedManifest,
    ReviewedManifestCoordinates,
)
from gen_automation.services.i2v_salad import (
    I2V_SALAD_GPU_CLASS_NAME,
    I2VSaladConfig,
    I2VSaladError,
)

_NOW = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
_GPU_ID = UUID("11111111-1111-4111-8111-111111111111")
_QUEUE_ID = UUID("22222222-2222-4222-8222-222222222222")
_GROUP_ID = UUID(rollout.REVIEWED_PROVIDER_GROUP_ID)
_PRIOR_IMAGE = "ghcr.io/neuraln-cyber/gen-automation/i2v-worker@sha256:" + "a" * 64
_TARGET_IMAGE = "ghcr.io/neuraln-cyber/gen-automation/i2v-worker@sha256:" + "b" * 64
_TARGET_REVISION = "c" * 40
_PRIOR_ENVIRONMENT = {
    "GEN_I2V_WORKER_ENVIRONMENT": "production",
    "GEN_I2V_WORKER_LORA_WORKER_ENABLED": "false",
    "AWS_ACCESS_KEY_ID": "old-access",
    "AWS_SECRET_ACCESS_KEY": "old-secret",
    "AWS_SESSION_TOKEN": "old-session",
}
_ROTATED_PRIOR_ENVIRONMENT = {
    **_PRIOR_ENVIRONMENT,
    "AWS_ACCESS_KEY_ID": "fresh-access",
    "AWS_SECRET_ACCESS_KEY": "fresh-secret",
    "AWS_SESSION_TOKEN": "fresh-session",
}
_TARGET_ENVIRONMENT = {
    "GEN_I2V_WORKER_ENVIRONMENT": "production",
    "GEN_I2V_WORKER_LORA_WORKER_ENABLED": "true",
    "GEN_I2V_WORKER_PRIVATE_MANIFEST_SOURCE_SHA256": rollout.REVIEWED_SOURCE_SHA256,
    "GEN_I2V_WORKER_SOURCE_REVISION": _TARGET_REVISION,
    "GEN_I2V_WORKER_MODEL_OBJECTS_JSON": "reviewed-model-objects",
    "AWS_ACCESS_KEY_ID": "target-access",
    "AWS_SECRET_ACCESS_KEY": "target-secret",
    "AWS_SESSION_TOKEN": "target-session",
}


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    value = Database(f"sqlite+aiosqlite:///{(tmp_path / 'rollout.db').as_posix()}")
    await value.create_schema()
    try:
        yield value
    finally:
        await value.dispose()


def _settings(*, capable: bool) -> Settings:
    image = _TARGET_IMAGE if capable else _PRIOR_IMAGE
    return Settings.model_construct(
        database_url="sqlite+aiosqlite:///:memory:",
        i2v_enabled=capable,
        i2v_hires_profile_enabled=capable,
        i2v_lora_worker_enabled=capable,
        i2v_lora_profile_enabled=False,
        i2v_worker_image=image,
        i2v_worker_source_revision=_TARGET_REVISION if capable else None,
        i2v_private_manifest_source_sha256=(rollout.REVIEWED_SOURCE_SHA256 if capable else None),
        i2v_salad_gpu_class_id=_GPU_ID,
        i2v_salad_gpu_class_name=I2V_SALAD_GPU_CLASS_NAME,
        i2v_salad_queue_name="i2v-jobs-v1",
        i2v_salad_container_group_name="i2v-worker-v1",
        i2v_salad_prefetch=3,
        i2v_worker_lease_seconds=86_400,
        i2v_warm_idle_seconds=36_000,
        i2v_salad_cpu=8,
        i2v_salad_memory_mb=32_768,
        i2v_salad_storage_bytes=268_435_456_000,
        i2v_salad_priority=SimpleNamespace(value="high"),
        i2v_salad_max_replicas=1,
        i2v_output_prefix="i2v/outputs",
        i2v_model_manifest_json=SecretStr("{}"),
        i2v_model_manifest_sha256=SecretStr(rollout.REVIEWED_CANONICAL_MANIFEST_SHA256),
        salad_worker_artifact_bucket=SecretStr(rollout.REVIEWED_MANIFEST_BUCKET),
        salad_worker_artifact_region=SecretStr("eu-central-1"),
    )


def _config(*, capable: bool) -> I2VSaladConfig:
    return I2VSaladConfig(
        queue_name="i2v-jobs-v1",
        container_group_name="i2v-worker-v1",
        worker_image=_TARGET_IMAGE if capable else _PRIOR_IMAGE,
        gpu_class_id=_GPU_ID,
        readiness_probe_path=(
            f"/ready/capability/{rollout.REVIEWED_SOURCE_SHA256}/"
            f"{rollout.REVIEWED_ARTIFACT_IDENTITY_SHA256}/{_TARGET_REVISION}"
            if capable
            else "/ready"
        ),
    )


def _group(
    *,
    capable: bool,
    version: int = 1,
    pending_change: bool = False,
    replicas: int = 1,
    environment: Mapping[str, str] | None = None,
) -> SaladContainerGroup:
    config = _config(capable=capable)
    raw = deepcopy(config.container_configuration())
    raw["replicas"] = replicas
    raw["priority"] = config.priority
    container = cast(JSONObject, raw["container"])
    container["environment_variables"] = dict(
        environment
        if environment is not None
        else (_TARGET_ENVIRONMENT if capable else _PRIOR_ENVIRONMENT)
    )
    return SaladContainerGroup(
        id=_GROUP_ID,
        name=config.container_group_name,
        display_name="Image to video - RTX 5090",
        replicas=replicas,
        pending_change=pending_change,
        version=version,
        current_state=SaladContainerGroupState(
            status="running" if replicas else "stopped",
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
        raw=raw,
    )


def _without_autostart_policy(group: SaladContainerGroup) -> SaladContainerGroup:
    raw = deepcopy(group.raw)
    raw.pop("autostart_policy", None)
    return replace(group, raw=raw)


def _without_legacy_default_fields(group: SaladContainerGroup) -> SaladContainerGroup:
    group = _without_autostart_policy(group)
    raw = deepcopy(group.raw)
    raw.pop("readiness_probe", None)
    return replace(group, raw=raw)


def _instance_page(*, version: int = 1) -> SaladContainerGroupInstancePage:
    return SaladContainerGroupInstancePage(
        instances=(
            SaladContainerGroupInstance(
                id="instance-1",
                machine_id="machine-1",
                state=SaladContainerGroupInstanceState.RUNNING,
                update_time=_NOW,
                version=version,
                ready=True,
                started=True,
            ),
        )
    )


def _empty_instance_page() -> SaladContainerGroupInstancePage:
    return SaladContainerGroupInstancePage(instances=())


def _pre_running_instance_page(
    state: SaladContainerGroupInstanceState,
    *,
    version: int = 1,
    ready: bool | None = False,
    started: bool | None = False,
) -> SaladContainerGroupInstancePage:
    return SaladContainerGroupInstancePage(
        instances=(
            SaladContainerGroupInstance(
                id="instance-1",
                machine_id="machine-1",
                state=state,
                update_time=_NOW,
                version=version,
                ready=ready,
                started=started,
            ),
        )
    )


def _allocating_group(*, version: int = 1) -> SaladContainerGroup:
    group = _group(capable=False, version=version)
    return replace(
        group,
        current_state=replace(
            group.current_state,
            allocating_count=1,
            creating_count=0,
            running_count=0,
            stopping_count=0,
        ),
    )


def _pre_running_group(
    state: SaladContainerGroupInstanceState,
    *,
    version: int = 1,
) -> SaladContainerGroup:
    group = _group(capable=False, version=version)
    allocating_count = int(state == SaladContainerGroupInstanceState.ALLOCATING)
    creating_count = int(
        state
        in {
            SaladContainerGroupInstanceState.DOWNLOADING,
            SaladContainerGroupInstanceState.CREATING,
        }
    )
    return replace(
        group,
        current_state=replace(
            group.current_state,
            allocating_count=allocating_count,
            creating_count=creating_count,
            running_count=0,
            stopping_count=0,
        ),
    )


def _queue(*, length: int = 0) -> SaladQueue:
    return SaladQueue(
        id=_QUEUE_ID,
        name="i2v-jobs-v1",
        display_name="I2V",
        description=None,
        current_queue_length=length,
        container_groups=(),
        create_time=_NOW,
        update_time=_NOW,
    )


def _job(*, status: SaladJobStatus = SaladJobStatus.PENDING) -> SaladQueueJob:
    return SaladQueueJob(
        id=uuid4(),
        input={},
        status=status,
        events=(),
        create_time=_NOW,
        update_time=_NOW,
        metadata={},
        webhook=None,
        output=None,
    )


def _gpu() -> SaladGpuClass:
    return SaladGpuClass(
        id=_GPU_ID,
        name=I2V_SALAD_GPU_CLASS_NAME,
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


class _ArtifactClient:
    def __init__(self, *, fail_at: int | None = None) -> None:
        self.fail_at = fail_at
        self.calls: list[dict[str, object]] = []
        self.closed = False

    def head_object(self, **kwargs: object) -> Mapping[str, object]:
        self.calls.append(kwargs)
        if len(self.calls) == self.fail_at:
            raise RuntimeError("secret provider failure")
        item = _MANIFEST.objects[len(self.calls) - 1]
        return {"VersionId": item["version_id"], "ContentLength": item["bytes"]}

    def close(self) -> None:
        self.closed = True


_MANIFEST = PreparedReviewedManifest(
    canonical_json="{}",
    canonical_sha256=rollout.REVIEWED_CANONICAL_MANIFEST_SHA256,
    objects=tuple(
        {
            "key": f"worker/i2v/object-{index}",
            "version_id": f"version-{index}",
            "bytes": index,
        }
        for index in range(1, 15)
    ),
)
_COORDINATES = ReviewedManifestCoordinates(
    bucket=rollout.REVIEWED_MANIFEST_BUCKET,
    key=rollout.REVIEWED_MANIFEST_KEY,
    version_id=rollout.REVIEWED_MANIFEST_VERSION,
    source_sha256=rollout.REVIEWED_SOURCE_SHA256,
)


def _reviewed_manifest_source_bytes() -> bytes:
    fixture = Path("tests/fixtures/i2v-reviewed-private-manifest.json")
    source = fixture.read_text(encoding="utf-8").replace("\r\n", "\n")
    return source.replace("\n", "\r\n").encode("utf-8")


class _FakeSalad:
    def __init__(self, *, group: SaladContainerGroup | None = None) -> None:
        self.group = group or _group(capable=False)
        self.instances = _instance_page(version=self.group.version)
        self.queue = _queue()
        self.job_pages: list[tuple[SaladQueueJob, ...]] = []
        self.list_jobs_calls = 0
        self.get_group_calls = 0
        self.update_patches: list[JSONObject] = []
        self.start_calls = 0
        self.stop_calls = 0
        self.allow_stop = False
        self.raise_after_first_update = False

    async def list_gpu_classes(self) -> tuple[SaladGpuClass, ...]:
        return (_gpu(),)

    async def get_queue(self, _name: str) -> SaladQueue:
        return self.queue

    async def list_jobs(
        self, _name: str, *, page: int = 1, page_size: int = 100
    ) -> SaladQueueJobPage:
        del page_size
        self.list_jobs_calls += 1
        items = self.job_pages[self.list_jobs_calls - 1] if self.job_pages else ()
        assert page == 1
        return SaladQueueJobPage(items=items)

    async def get_container_group(self, _name: str) -> SaladContainerGroup:
        self.get_group_calls += 1
        return self.group

    async def list_container_group_instances(self, _name: str) -> SaladContainerGroupInstancePage:
        return self.instances

    async def update_container_group(self, _name: str, patch: JSONObject) -> SaladContainerGroup:
        self.update_patches.append(deepcopy(patch))
        raw = deepcopy(self.group.raw)
        patch_container = cast(JSONObject, patch["container"])
        container = cast(JSONObject, raw["container"])
        if "image" in patch_container:
            container["image"] = patch_container["image"]
        current_environment = cast(dict[str, str], dict(container["environment_variables"]))
        for key, value in cast(JSONObject, patch_container["environment_variables"]).items():
            if value is None:
                current_environment.pop(key, None)
            else:
                assert isinstance(value, str)
                current_environment[key] = value
        container["environment_variables"] = current_environment
        if "readiness_probe" in patch:
            readiness_probe = patch["readiness_probe"]
            if readiness_probe is None:
                raw.pop("readiness_probe", None)
            else:
                raw["readiness_probe"] = deepcopy(readiness_probe)
        next_version = self.group.version + 1
        replicas = self.group.replicas
        remains_stopped = self.group.current_state.status == "stopped"
        self.group = SaladContainerGroup(
            id=self.group.id,
            name=self.group.name,
            display_name=self.group.display_name,
            replicas=replicas,
            pending_change=False,
            version=next_version,
            current_state=replace(
                self.group.current_state,
                status="stopped" if remains_stopped else ("running" if replicas else "stopped"),
                allocating_count=0,
                creating_count=0,
                running_count=0 if remains_stopped else replicas,
                stopping_count=0,
            ),
            create_time=self.group.create_time,
            update_time=_NOW,
            raw=raw,
        )
        self.instances = (
            _empty_instance_page()
            if remains_stopped or not replicas
            else _instance_page(version=next_version)
        )
        if self.raise_after_first_update and len(self.update_patches) == 1:
            raise RuntimeError("ambiguous provider response with secret details")
        return self.group

    async def start_container_group(self, _name: str) -> None:
        self.start_calls += 1
        if not rollout._is_exact_stopped_group(self.group):
            raise AssertionError("only an exact stopped rollback profile may be started")
        raw = deepcopy(self.group.raw)
        raw["replicas"] = 1
        self.group = replace(
            self.group,
            replicas=1,
            current_state=replace(self.group.current_state, status="running", running_count=1),
            raw=raw,
        )
        self.instances = _instance_page(version=self.group.version)

    async def stop_container_group(self, _name: str) -> None:
        if not self.allow_stop:
            raise AssertionError("only an explicit recycle may stop the provider group")
        self.stop_calls += 1
        raw = deepcopy(self.group.raw)
        raw["replicas"] = 0
        self.group = replace(
            self.group,
            replicas=0,
            current_state=replace(
                self.group.current_state,
                status="stopped",
                allocating_count=0,
                creating_count=0,
                running_count=0,
                stopping_count=0,
            ),
            raw=raw,
        )
        self.instances = _empty_instance_page()


class _DeferredPatchResponseSalad(_FakeSalad):
    def __init__(self, *, group: SaladContainerGroup | None = None) -> None:
        super().__init__(group=group)
        self.deferred_readback: SaladContainerGroup | None = None

    async def get_container_group(self, name: str) -> SaladContainerGroup:
        if self.deferred_readback is not None:
            self.get_group_calls += 1
            deferred = self.deferred_readback
            self.deferred_readback = None
            return deferred
        return await super().get_container_group(name)

    async def update_container_group(self, name: str, patch: JSONObject) -> SaladContainerGroup:
        prior = self.group
        await super().update_container_group(name, patch)
        self.deferred_readback = replace(prior, pending_change=True)
        return self.deferred_readback


class _RuntimeEnvironment:
    calls = 0

    def __init__(self, *, settings: Settings, resolver: object) -> None:
        del settings, resolver

    async def resolve(self) -> Mapping[str, str]:
        type(self).calls += 1
        return _ROTATED_PRIOR_ENVIRONMENT


def _patch_promotion_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    _RuntimeEnvironment.calls = 0

    async def fetch(
        _client: object, _coordinates: object, **_kwargs: object
    ) -> PreparedReviewedManifest:
        return _MANIFEST

    async def target_environment(
        _settings: Settings, _resolver: object, **_kwargs: object
    ) -> Mapping[str, str]:
        return _TARGET_ENVIRONMENT

    def runtime_config(settings: Settings) -> SimpleNamespace:
        return SimpleNamespace(salad=_config(capable=settings.i2v_lora_worker_enabled))

    monkeypatch.setattr(rollout, "fetch_reviewed_manifest", fetch)
    monkeypatch.setattr(
        rollout,
        "reviewed_target_settings",
        lambda *_args, **_kwargs: _settings(capable=True),
    )
    monkeypatch.setattr(rollout, "resolve_reviewed_worker_environment", target_environment)
    monkeypatch.setattr(rollout, "I2VRuntimeEnvironment", _RuntimeEnvironment)
    monkeypatch.setattr(rollout, "i2v_runtime_config_from_settings", runtime_config)


def _rollback_state(prior: SaladContainerGroup) -> rollout.ProviderRollbackState:
    return rollout._rollback_state(
        prior,
        promoted_image=_TARGET_IMAGE,
        promoted_environment=_TARGET_ENVIRONMENT,
        promoted_readiness_probe=rollout._readiness_probe(
            _config(capable=True).readiness_probe_path
        ),
    )


async def _promote(
    *,
    database: Database,
    client: _FakeSalad,
    artifact: _ArtifactClient,
    tmp_path: Path,
) -> rollout.I2VLoraRolloutResult:
    return await rollout.promote_reviewed_worker(
        settings=_settings(capable=False),
        sessions=database.sessions,
        salad_client=cast(Any, client),
        resolver=cast(Any, object()),
        manifest_client=cast(Any, object()),
        artifact_client_factory=lambda _environment: artifact,
        coordinates=_COORDINATES,
        worker_image=_TARGET_IMAGE,
        worker_source_revision=_TARGET_REVISION,
        prepared_host_env_output=tmp_path / "target.env",
        rollback_state_output=tmp_path / "rollback.json",
        provider_mutation_marker_output=tmp_path / "provider-mutation.json",
        timeout_seconds=0.1,
    )


async def _dry_run(
    *,
    database: Database,
    client: _FakeSalad,
    artifact: _ArtifactClient,
    tmp_path: Path,
) -> rollout.I2VLoraRolloutResult:
    return await rollout.dry_run_reviewed_worker_rollout(
        settings=_settings(capable=False),
        sessions=database.sessions,
        salad_client=cast(Any, client),
        resolver=cast(Any, object()),
        manifest_client=cast(Any, object()),
        artifact_client_factory=lambda _environment: artifact,
        coordinates=_COORDINATES,
        worker_image=_TARGET_IMAGE,
        worker_source_revision=_TARGET_REVISION,
        prepared_host_env_output=tmp_path / "target.env",
        diagnostic_output=tmp_path / "rollout-diagnostic.json",
    )


async def _recycle_promote(
    *,
    database: Database,
    client: _FakeSalad,
    artifact: _ArtifactClient,
    tmp_path: Path,
) -> rollout.I2VLoraRolloutResult:
    client.allow_stop = True
    return await rollout.recycle_promote_reviewed_worker(
        settings=_settings(capable=False),
        sessions=database.sessions,
        salad_client=cast(Any, client),
        resolver=cast(Any, object()),
        manifest_client=cast(Any, object()),
        artifact_client_factory=lambda _environment: artifact,
        coordinates=_COORDINATES,
        worker_image=_TARGET_IMAGE,
        worker_source_revision=_TARGET_REVISION,
        prepared_host_env_output=tmp_path / "target.env",
        rollback_state_output=tmp_path / "rollback.json",
        provider_mutation_marker_output=tmp_path / "recycle-mutation.json",
        diagnostic_output=tmp_path / "rollout-diagnostic.json",
        timeout_seconds=0.1,
    )


async def _no_sleep(_seconds: float) -> None:
    return None


def test_exact_ready_instance_requires_one_current_running_ready_replica() -> None:
    exact = _group(capable=True, version=8)
    assert rollout._has_exact_ready_instance(exact, _instance_page(version=8))

    assert not rollout._has_exact_ready_instance(
        _group(capable=True, version=8, replicas=0), _empty_instance_page()
    )
    assert not rollout._has_exact_ready_instance(exact, _instance_page(version=7))
    assert not rollout._has_exact_ready_instance(
        _group(capable=True, version=8, pending_change=True), _instance_page(version=8)
    )


def test_safe_prior_rollout_baseline_accepts_only_exact_empty_allocating_state() -> None:
    group = _allocating_group(version=8)

    assert not rollout._validate_safe_prior_rollout_baseline(
        group,
        _empty_instance_page(),
        _config(capable=False),
    )


@pytest.mark.asyncio
async def test_recycle_promote_prepares_target_then_stops_patches_starts_and_waits_ready(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_promotion_dependencies(monkeypatch)
    client = _FakeSalad()
    artifact = _ArtifactClient()

    result = await _recycle_promote(
        database=database,
        client=client,
        artifact=artifact,
        tmp_path=tmp_path,
    )

    assert result.operation == "recycle-promote"
    assert result.provider_image == _TARGET_IMAGE
    assert result.provider_ready is True
    assert result.durable_active_jobs == 0
    assert result.durable_active_attempts == 0
    assert result.provider_active_jobs == 0
    assert client.stop_calls == 1
    assert client.start_calls == 1
    assert len(client.update_patches) == 1
    assert cast(JSONObject, client.update_patches[0]["container"])["image"] == _TARGET_IMAGE
    assert len(artifact.calls) == 14
    state = rollout.read_provider_rollback_state(tmp_path / "rollback.json")
    assert state.prior_version == 1
    assert state.promoted_version == 2
    assert client.group.version == 2
    assert (tmp_path / "target.env").exists()
    assert (tmp_path / "recycle-mutation.json").exists()
    assert json.loads((tmp_path / "rollout-diagnostic.json").read_text()) == {
        "operation": "recycle-promote",
        "outcome": "ready",
        "provider": {
            "allocating_count": 0,
            "creating_count": 0,
            "pending_change": False,
            "replicas": 1,
            "running_count": 1,
            "status": "running",
            "stopping_count": 0,
            "version": 2,
        },
        "schema": "gen-automation/i2v-lora-diagnostic/v1",
        "stage": "complete",
    }


@pytest.mark.asyncio
async def test_recycle_promote_accepts_and_preserves_legacy_missing_default_fields(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_promotion_dependencies(monkeypatch)
    client = _FakeSalad(group=_without_legacy_default_fields(_group(capable=False)))

    result = await _recycle_promote(
        database=database,
        client=client,
        artifact=_ArtifactClient(),
        tmp_path=tmp_path,
    )

    state = rollout.read_provider_rollback_state(tmp_path / "rollback.json")
    assert result.provider_ready
    assert "autostart_policy" not in state.prior_contract
    assert state.prior_readiness_probe is None
    assert "readiness_probe" not in state.prior_contract
    assert "autostart_policy" not in client.group.raw


@pytest.mark.asyncio
async def test_recycle_promote_accepts_deferred_exact_next_version_acknowledgement(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_promotion_dependencies(monkeypatch)
    monkeypatch.setattr(rollout.asyncio, "sleep", _no_sleep)
    client = _DeferredPatchResponseSalad()

    result = await _recycle_promote(
        database=database,
        client=client,
        artifact=_ArtifactClient(),
        tmp_path=tmp_path,
    )

    assert result.provider_ready
    assert client.group.version == 2
    assert client.start_calls == 1


@pytest.mark.asyncio
async def test_recycle_promote_finishes_all_target_artifact_work_before_stop(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_promotion_dependencies(monkeypatch)
    client = _FakeSalad()
    artifact = _ArtifactClient(fail_at=14)

    with pytest.raises(I2VLoraRolloutError, match="cannot read every reviewed object"):
        await _recycle_promote(
            database=database,
            client=client,
            artifact=artifact,
            tmp_path=tmp_path,
        )

    assert len(artifact.calls) == 14
    assert client.get_group_calls == 0
    assert client.stop_calls == 0
    assert client.start_calls == 0
    assert client.update_patches == []
    assert not (tmp_path / "recycle-mutation.json").exists()
    diagnostic = json.loads((tmp_path / "rollout-diagnostic.json").read_text())
    assert diagnostic["stage"] == "artifact-head-access"
    assert diagnostic["outcome"] == "preparing"
    assert "secret" not in json.dumps(diagnostic).lower()


@pytest.mark.asyncio
async def test_recycle_promote_accepts_exact_idle_running_not_ready_source(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_promotion_dependencies(monkeypatch)
    client = _FakeSalad()
    current = client.instances.instances[0]
    client.instances = SaladContainerGroupInstancePage(
        instances=(replace(current, ready=False, started=True),)
    )

    result = await _recycle_promote(
        database=database,
        client=client,
        artifact=_ArtifactClient(),
        tmp_path=tmp_path,
    )

    assert result.operation == "recycle-promote"
    assert result.provider_image == _TARGET_IMAGE
    assert result.provider_ready is True
    assert client.stop_calls == 1
    assert client.start_calls == 1
    assert len(client.update_patches) == 1


@pytest.mark.asyncio
async def test_recycle_promote_patches_an_exact_explicitly_stopped_source_before_start(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_promotion_dependencies(monkeypatch)
    source = _group(capable=False)
    source = replace(
        source,
        current_state=replace(
            source.current_state,
            status="stopped",
            allocating_count=0,
            creating_count=0,
            running_count=0,
            stopping_count=0,
        ),
    )
    client = _FakeSalad(group=source)
    client.instances = _empty_instance_page()

    result = await _recycle_promote(
        database=database,
        client=client,
        artifact=_ArtifactClient(),
        tmp_path=tmp_path,
    )

    assert result.operation == "recycle-promote"
    assert result.provider_image == _TARGET_IMAGE
    assert result.provider_ready is True
    assert client.stop_calls == 0
    assert client.start_calls == 1
    assert len(client.update_patches) == 1


@pytest.mark.asyncio
async def test_recycle_promote_refuses_all_provider_mutation_when_any_queue_plane_is_nonzero(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_promotion_dependencies(monkeypatch)
    client = _FakeSalad()
    client.queue = _queue(length=1)

    with pytest.raises(I2VLoraRolloutError, match="zero active"):
        await _recycle_promote(
            database=database,
            client=client,
            artifact=_ArtifactClient(),
            tmp_path=tmp_path,
        )

    assert client.stop_calls == 0
    assert client.start_calls == 0
    assert client.update_patches == []
    assert not (tmp_path / "recycle-mutation.json").exists()


@pytest.mark.asyncio
async def test_recycle_promote_refuses_pending_provider_lifecycle_before_stop(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_promotion_dependencies(monkeypatch)
    client = _FakeSalad(group=_group(capable=False, pending_change=True))

    with pytest.raises(I2VLoraRolloutError):
        await _recycle_promote(
            database=database,
            client=client,
            artifact=_ArtifactClient(),
            tmp_path=tmp_path,
        )

    assert client.stop_calls == 0
    assert client.start_calls == 0
    assert not (tmp_path / "recycle-mutation.json").exists()


@pytest.mark.asyncio
async def test_recycle_promote_ambiguous_stop_restores_prior_and_clears_marker(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_promotion_dependencies(monkeypatch)

    class AmbiguousStopSalad(_FakeSalad):
        async def stop_container_group(self, name: str) -> None:
            await super().stop_container_group(name)
            raise RuntimeError("ambiguous provider transport with secret detail")

    client = AmbiguousStopSalad()
    marker = tmp_path / "recycle-mutation.json"

    with pytest.raises(RuntimeError, match="ambiguous provider transport"):
        await _recycle_promote(
            database=database,
            client=client,
            artifact=_ArtifactClient(),
            tmp_path=tmp_path,
        )

    assert client.stop_calls == 1
    assert client.start_calls == 1
    assert client.group.replicas == 1
    assert rollout._group_image(client.group) == _PRIOR_IMAGE
    assert len(client.update_patches) == 1
    assert not marker.exists()


@pytest.mark.asyncio
async def test_recycle_promote_recovery_accepts_only_an_exact_degraded_prior_restart(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_promotion_dependencies(monkeypatch)

    class DegradedRecoverySalad(_FakeSalad):
        async def stop_container_group(self, name: str) -> None:
            await super().stop_container_group(name)
            raise RuntimeError("ambiguous stop")

        async def start_container_group(self, name: str) -> None:
            await super().start_container_group(name)
            self.group = _allocating_group(version=self.group.version)
            self.instances = _empty_instance_page()

    client = DegradedRecoverySalad()

    with pytest.raises(RuntimeError, match="ambiguous stop"):
        await _recycle_promote(
            database=database,
            client=client,
            artifact=_ArtifactClient(),
            tmp_path=tmp_path,
        )

    assert rollout._group_image(client.group) == _PRIOR_IMAGE
    assert client.group.current_state.allocating_count == 1
    assert client.instances.instances == ()
    assert not (tmp_path / "recycle-mutation.json").exists()


@pytest.mark.asyncio
async def test_recycle_promote_fails_closed_if_stop_changes_group_version(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_promotion_dependencies(monkeypatch)

    class VersionChangingStopSalad(_FakeSalad):
        async def stop_container_group(self, name: str) -> None:
            await super().stop_container_group(name)
            self.group = replace(self.group, version=self.group.version + 1)

    client = VersionChangingStopSalad()

    with pytest.raises(I2VLoraRolloutError, match="contract changed outside"):
        await _recycle_promote(
            database=database,
            client=client,
            artifact=_ArtifactClient(),
            tmp_path=tmp_path,
        )

    assert client.stop_calls == 1
    assert client.start_calls == 1
    assert len(client.update_patches) == 1
    assert "image" not in cast(JSONObject, client.update_patches[0]["container"])
    assert rollout._group_image(client.group) == _PRIOR_IMAGE
    assert client.group.version == 3
    assert not (tmp_path / "recycle-mutation.json").exists()


@pytest.mark.asyncio
async def test_recycle_promote_ambiguous_target_patch_restores_prior_profile(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_promotion_dependencies(monkeypatch)
    client = _FakeSalad()
    client.raise_after_first_update = True

    with pytest.raises(RuntimeError, match="ambiguous provider response"):
        await _recycle_promote(
            database=database,
            client=client,
            artifact=_ArtifactClient(),
            tmp_path=tmp_path,
        )

    assert client.stop_calls == 1
    assert client.start_calls == 1
    assert len(client.update_patches) == 2
    assert cast(JSONObject, client.update_patches[0]["container"])["image"] == _TARGET_IMAGE
    assert cast(JSONObject, client.update_patches[1]["container"])["image"] == _PRIOR_IMAGE
    assert rollout._group_image(client.group) == _PRIOR_IMAGE
    assert client.group.replicas == 1
    assert not (tmp_path / "recycle-mutation.json").exists()


@pytest.mark.parametrize(
    ("queue_race_call", "expected_updates"),
    ((4, 0), (6, 1)),
)
@pytest.mark.asyncio
async def test_recycle_promote_rechecks_provider_queue_immediately_before_each_mutation(
    queue_race_call: int,
    expected_updates: int,
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_promotion_dependencies(monkeypatch)

    class QueueRaceSalad(_FakeSalad):
        queue_reads = 0

        async def get_queue(self, name: str) -> SaladQueue:
            self.queue_reads += 1
            if self.queue_reads >= queue_race_call:
                return _queue(length=1)
            return await super().get_queue(name)

    client = QueueRaceSalad()

    with pytest.raises(I2VLoraRolloutError, match="operator attention"):
        await _recycle_promote(
            database=database,
            client=client,
            artifact=_ArtifactClient(),
            tmp_path=tmp_path,
        )

    assert client.stop_calls == 1
    assert len(client.update_patches) == expected_updates
    assert client.start_calls == 0
    assert (tmp_path / "recycle-mutation.json").exists()


@pytest.mark.asyncio
async def test_recycle_promote_rereads_target_after_final_queue_guard_before_start(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_promotion_dependencies(monkeypatch)

    class StartRaceSalad(_FakeSalad):
        queue_reads = 0

        async def get_queue(self, name: str) -> SaladQueue:
            self.queue_reads += 1
            queue = await super().get_queue(name)
            if self.queue_reads == 6:
                self.group = replace(self.group, name="raced-worker")
            return queue

    client = StartRaceSalad()

    with pytest.raises(I2VLoraRolloutError, match="operator attention"):
        await _recycle_promote(
            database=database,
            client=client,
            artifact=_ArtifactClient(),
            tmp_path=tmp_path,
        )

    assert client.start_calls == 0
    assert (tmp_path / "recycle-mutation.json").exists()


@pytest.mark.parametrize(
    "state",
    (
        SaladContainerGroupInstanceState.ALLOCATING,
        SaladContainerGroupInstanceState.DOWNLOADING,
        SaladContainerGroupInstanceState.CREATING,
    ),
)
def test_safe_prior_rollout_baseline_accepts_exact_pre_running_singleton(
    state: SaladContainerGroupInstanceState,
) -> None:
    group = _pre_running_group(state, version=8)

    assert not rollout._validate_safe_prior_rollout_baseline(
        group,
        _pre_running_instance_page(state, version=8),
        _config(capable=False),
    )
    assert not rollout._has_exact_ready_instance(
        group,
        _pre_running_instance_page(state, version=8),
    )


@pytest.mark.parametrize(
    ("mutation", "instances"),
    (
        ("pending", _empty_instance_page()),
        ("allocating-zero", _empty_instance_page()),
        ("allocating-two", _empty_instance_page()),
        ("running", _empty_instance_page()),
        ("creating", _empty_instance_page()),
        ("stopping", _empty_instance_page()),
        ("instance", _instance_page(version=8)),
    ),
)
def test_safe_prior_rollout_baseline_rejects_every_allocating_state_drift(
    mutation: str,
    instances: SaladContainerGroupInstancePage,
) -> None:
    group = _allocating_group(version=8)
    if mutation == "pending":
        group = replace(group, pending_change=True)
    elif mutation == "allocating-zero":
        group = replace(
            group,
            current_state=replace(group.current_state, allocating_count=0),
        )
    elif mutation == "allocating-two":
        group = replace(
            group,
            current_state=replace(group.current_state, allocating_count=2),
        )
    elif mutation == "running":
        group = replace(
            group,
            current_state=replace(group.current_state, running_count=1),
        )
    elif mutation == "creating":
        group = replace(
            group,
            current_state=replace(group.current_state, creating_count=1),
        )
    elif mutation == "stopping":
        group = replace(
            group,
            current_state=replace(group.current_state, stopping_count=1),
        )
    elif mutation != "instance":
        raise AssertionError(f"unknown mutation {mutation}")

    with pytest.raises(I2VLoraRolloutError, match="exact safe baseline"):
        rollout._validate_safe_prior_rollout_baseline(
            group,
            instances,
            _config(capable=False),
        )


@pytest.mark.parametrize(
    ("mutation", "state"),
    (
        ("pending", SaladContainerGroupInstanceState.DOWNLOADING),
        ("replicas", SaladContainerGroupInstanceState.DOWNLOADING),
        ("status", SaladContainerGroupInstanceState.DOWNLOADING),
        ("allocating", SaladContainerGroupInstanceState.DOWNLOADING),
        ("creating", SaladContainerGroupInstanceState.DOWNLOADING),
        ("running", SaladContainerGroupInstanceState.DOWNLOADING),
        ("stopping", SaladContainerGroupInstanceState.DOWNLOADING),
        ("empty", SaladContainerGroupInstanceState.DOWNLOADING),
        ("two", SaladContainerGroupInstanceState.DOWNLOADING),
        ("stale-version", SaladContainerGroupInstanceState.DOWNLOADING),
        ("ready-none", SaladContainerGroupInstanceState.DOWNLOADING),
        ("ready-true", SaladContainerGroupInstanceState.DOWNLOADING),
        ("started-none", SaladContainerGroupInstanceState.DOWNLOADING),
        ("started-true", SaladContainerGroupInstanceState.DOWNLOADING),
        ("running-instance", SaladContainerGroupInstanceState.DOWNLOADING),
        ("stopping-instance", SaladContainerGroupInstanceState.DOWNLOADING),
        ("allocating-count-mismatch", SaladContainerGroupInstanceState.ALLOCATING),
        ("downloading-count-mismatch", SaladContainerGroupInstanceState.DOWNLOADING),
        ("creating-count-mismatch", SaladContainerGroupInstanceState.CREATING),
    ),
)
def test_safe_prior_rollout_baseline_rejects_every_pre_running_singleton_drift(
    mutation: str,
    state: SaladContainerGroupInstanceState,
) -> None:
    group = _pre_running_group(state, version=8)
    page = _pre_running_instance_page(state, version=8)
    if mutation == "pending":
        group = replace(group, pending_change=True)
    elif mutation == "replicas":
        raw = deepcopy(group.raw)
        raw["replicas"] = 2
        group = replace(group, replicas=2, raw=raw)
    elif mutation == "status":
        group = replace(
            group,
            current_state=replace(group.current_state, status="stopped"),
        )
    elif mutation == "allocating":
        group = replace(
            group,
            current_state=replace(group.current_state, allocating_count=1),
        )
    elif mutation == "creating":
        group = replace(
            group,
            current_state=replace(group.current_state, creating_count=0),
        )
    elif mutation == "running":
        group = replace(
            group,
            current_state=replace(group.current_state, running_count=1),
        )
    elif mutation == "stopping":
        group = replace(
            group,
            current_state=replace(group.current_state, stopping_count=1),
        )
    elif mutation == "empty":
        page = _empty_instance_page()
    elif mutation == "two":
        instance = page.instances[0]
        page = SaladContainerGroupInstancePage(instances=(instance, replace(instance, id="two")))
    elif mutation == "stale-version":
        page = _pre_running_instance_page(state, version=7)
    elif mutation == "ready-none":
        page = _pre_running_instance_page(state, version=8, ready=None)
    elif mutation == "ready-true":
        page = _pre_running_instance_page(state, version=8, ready=True)
    elif mutation == "started-none":
        page = _pre_running_instance_page(state, version=8, started=None)
    elif mutation == "started-true":
        page = _pre_running_instance_page(state, version=8, started=True)
    elif mutation == "running-instance":
        page = _pre_running_instance_page(SaladContainerGroupInstanceState.RUNNING, version=8)
    elif mutation == "stopping-instance":
        page = _pre_running_instance_page(SaladContainerGroupInstanceState.STOPPING, version=8)
    elif mutation == "allocating-count-mismatch":
        group = replace(
            group,
            current_state=replace(
                group.current_state,
                allocating_count=0,
                creating_count=1,
            ),
        )
    elif mutation in {"downloading-count-mismatch", "creating-count-mismatch"}:
        group = replace(
            group,
            current_state=replace(
                group.current_state,
                allocating_count=1,
                creating_count=0,
            ),
        )
    else:
        raise AssertionError(f"unknown mutation {mutation}")

    with pytest.raises(I2VLoraRolloutError):
        rollout._validate_safe_prior_rollout_baseline(
            group,
            page,
            _config(capable=False),
        )


@pytest.mark.parametrize(
    "instances",
    (
        SimpleNamespace(instances=[]),
        SimpleNamespace(instances=(object(),)),
    ),
)
def test_safe_prior_rollout_baseline_rejects_invalid_pre_running_instance_readback(
    instances: object,
) -> None:
    with pytest.raises(I2VLoraRolloutError, match="instance readback is invalid"):
        rollout._validate_safe_prior_rollout_baseline(
            _pre_running_group(SaladContainerGroupInstanceState.DOWNLOADING),
            instances,
            _config(capable=False),
        )


@pytest.mark.parametrize(
    ("durable_counts", "provider_jobs", "queue_length"),
    (
        ((1, 0), (), 0),
        ((0, 1), (), 0),
        ((0, 0), (_job(),), 0),
        ((0, 0), (), 1),
    ),
)
async def test_zero_work_guard_refuses_each_durable_and_provider_work_source(
    durable_counts: tuple[int, int],
    provider_jobs: tuple[SaladQueueJob, ...],
    queue_length: int,
) -> None:
    class Session:
        def __init__(self) -> None:
            self.counts = iter(durable_counts)

        def get_bind(self) -> SimpleNamespace:
            return SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))

        async def scalar(self, _statement: object) -> int:
            return next(self.counts)

    client = _FakeSalad()
    client.job_pages = [provider_jobs]
    client.queue = _queue(length=queue_length)

    with pytest.raises(I2VLoraRolloutError, match="requires zero active"):
        await rollout.assert_zero_active_rollout_work(
            cast(Any, Session()),
            cast(Any, client),
            queue_name="i2v-jobs-v1",
        )

    assert client.update_patches == []


async def test_reviewed_artifact_preflight_heads_exactly_all_fourteen_objects() -> None:
    client = _ArtifactClient()

    await rollout.verify_reviewed_artifact_access(client, _MANIFEST)

    assert len(client.calls) == 14
    assert client.calls == [
        {
            "Bucket": rollout.REVIEWED_MANIFEST_BUCKET,
            "Key": item["key"],
            "VersionId": item["version_id"],
        }
        for item in _MANIFEST.objects
    ]


async def test_exact_reviewed_manifest_derives_the_worker_readiness_identities(
    tmp_path: Path,
) -> None:
    source_bytes = _reviewed_manifest_source_bytes()
    assert len(source_bytes) == rollout.REVIEWED_MANIFEST_BYTES

    class ManifestClient:
        def get_object(self, **kwargs: object) -> Mapping[str, object]:
            assert kwargs == {
                "Bucket": rollout.REVIEWED_MANIFEST_BUCKET,
                "Key": rollout.REVIEWED_MANIFEST_KEY,
                "VersionId": rollout.REVIEWED_MANIFEST_VERSION,
            }
            return {
                "Body": BytesIO(source_bytes),
                "ContentLength": len(source_bytes),
                "VersionId": rollout.REVIEWED_MANIFEST_VERSION,
            }

    manifest = await rollout.fetch_reviewed_manifest(ManifestClient(), _COORDINATES)
    assert manifest.canonical_sha256 == rollout.REVIEWED_CANONICAL_MANIFEST_SHA256
    base = _settings(capable=False).model_copy(
        update={
            "i2v_salad_priority": SaladContainerPriority.HIGH,
            "salad_enabled": True,
            "salad_api_key": SecretStr("test-api-key"),
            "salad_organization": "organization",
            "salad_project": "project",
            "salad_queue_name": "jobs-v1",
            "salad_container_group_name": "worker-v1",
            "salad_webhook_secret": SecretStr("test-webhook-secret"),
            "salad_worker_image": _PRIOR_IMAGE,
            "salad_worker_artifact_role_arn": "arn:aws:iam::111111111111:role/test-reader",
            "storage_enabled": True,
            "storage_bucket": "test-private-bucket",
            "background_runtime_enabled": True,
            "public_base_url": AnyHttpUrl("https://example.invalid"),
        }
    )
    target = rollout.reviewed_target_settings(
        base,
        worker_image=_TARGET_IMAGE,
        worker_source_revision=_TARGET_REVISION,
        manifest=manifest,
    )

    class Resolver:
        async def resolve_many(self, bindings: Mapping[str, str]) -> Mapping[str, str]:
            credentials = {
                "GEN_WORKER_ARTIFACT_ACCESS_KEY_ID": "test-access",
                "GEN_WORKER_ARTIFACT_BUCKET": rollout.REVIEWED_MANIFEST_BUCKET,
                "GEN_WORKER_ARTIFACT_REGION": "eu-central-1",
                "GEN_WORKER_ARTIFACT_SECRET_ACCESS_KEY": "test-secret",
                "GEN_WORKER_ARTIFACT_SESSION_TOKEN": "test-session",
                "GEN_WORKER_ENVIRONMENT": "production",
            }
            return {name: credentials[name] for name in bindings}

        async def aclose(self) -> None:
            return None

    environment = await I2VRuntimeEnvironment(
        settings=target,
        resolver=Resolver(),
    ).resolve()
    rollout._validate_worker_environment_identity(environment, settings=target)
    assert i2v_worker_identity(target) == (
        rollout.REVIEWED_MODEL_OBJECTS_SHA256,
        rollout.REVIEWED_ARTIFACT_IDENTITY_SHA256,
    )
    worker = I2VWorkerSettings(
        comfy_root=tmp_path / "comfyui",
        model_objects_json=SecretStr(environment["GEN_I2V_WORKER_MODEL_OBJECTS_JSON"]),
        lora_worker_enabled=True,
        runtime_root=tmp_path / "runtime",
        source_revision=_TARGET_REVISION,
        private_manifest_source_sha256=rollout.REVIEWED_SOURCE_SHA256,
    )
    assert len(worker.model_objects) == 14
    assert worker.model_objects_sha256 == rollout.REVIEWED_MODEL_OBJECTS_SHA256
    assert worker.artifact_identity_sha256 == rollout.REVIEWED_ARTIFACT_IDENTITY_SHA256


async def test_artifact_preflight_failure_occurs_before_any_provider_update(
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    tmp_path: Path,
) -> None:
    _patch_promotion_dependencies(monkeypatch)
    client = _FakeSalad()
    artifact = _ArtifactClient(fail_at=7)

    with pytest.raises(I2VLoraRolloutError, match="cannot read every reviewed object"):
        await _promote(database=database, client=client, artifact=artifact, tmp_path=tmp_path)

    assert len(artifact.calls) == 7
    assert artifact.closed
    assert client.get_group_calls == 0
    assert client.update_patches == []


async def test_dry_run_accepts_exact_empty_allocating_prior_as_not_ready(
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    tmp_path: Path,
) -> None:
    _patch_promotion_dependencies(monkeypatch)
    monkeypatch.setattr(rollout, "verify_reviewed_artifact_access", _no_artifact_check)
    client = _FakeSalad(group=_allocating_group())
    client.instances = _empty_instance_page()

    result = await _dry_run(
        database=database,
        client=client,
        artifact=_ArtifactClient(),
        tmp_path=tmp_path,
    )

    assert result.operation == "dry-run"
    assert result.provider_image == _PRIOR_IMAGE
    assert not result.provider_ready
    assert client.update_patches == []


@pytest.mark.parametrize(
    "state",
    (
        SaladContainerGroupInstanceState.ALLOCATING,
        SaladContainerGroupInstanceState.DOWNLOADING,
        SaladContainerGroupInstanceState.CREATING,
    ),
)
async def test_dry_run_accepts_exact_pre_running_singleton_as_not_ready(
    state: SaladContainerGroupInstanceState,
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    tmp_path: Path,
) -> None:
    _patch_promotion_dependencies(monkeypatch)
    monkeypatch.setattr(rollout, "verify_reviewed_artifact_access", _no_artifact_check)
    client = _FakeSalad(group=_pre_running_group(state))
    client.instances = _pre_running_instance_page(state)

    result = await _dry_run(
        database=database,
        client=client,
        artifact=_ArtifactClient(),
        tmp_path=tmp_path,
    )

    assert not result.provider_ready
    assert client.update_patches == []


async def test_dry_run_accepts_legacy_missing_default_fields_without_mutation(
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    tmp_path: Path,
) -> None:
    _patch_promotion_dependencies(monkeypatch)
    monkeypatch.setattr(rollout, "verify_reviewed_artifact_access", _no_artifact_check)
    source = _without_legacy_default_fields(
        _pre_running_group(SaladContainerGroupInstanceState.DOWNLOADING)
    )
    container = cast(JSONObject, source.raw["container"])
    cast(JSONObject, container["resources"])["shm_size"] = 64
    client = _FakeSalad(group=source)
    client.instances = _pre_running_instance_page(SaladContainerGroupInstanceState.DOWNLOADING)

    result = await _dry_run(
        database=database,
        client=client,
        artifact=_ArtifactClient(),
        tmp_path=tmp_path,
    )

    assert not result.provider_ready
    assert client.update_patches == []
    assert "autostart_policy" not in client.group.raw
    assert "readiness_probe" not in client.group.raw


@pytest.mark.parametrize(
    ("failure", "expected_stage"),
    (
        ("group-name", "provider-source-group-name"),
        ("container", "provider-source-container"),
        ("image", "provider-source-image"),
        ("gpu-resources", "provider-source-gpu-resources"),
        ("priority", "provider-source-priority"),
        ("readiness-probe", "provider-source-readiness-probe"),
        ("compute", "provider-source-compute"),
        ("autostart", "provider-source-autostart"),
        ("restart-policy", "provider-source-restart-policy"),
        ("startup-probe", "provider-source-startup-probe"),
        ("liveness-probe", "provider-source-liveness-probe"),
        ("queue-connection", "provider-source-queue-connection"),
        ("autoscaler", "provider-source-autoscaler"),
        ("group-identity", "provider-source-group-identity"),
        ("runtime-bindings", "provider-source-runtime-bindings"),
        ("lifecycle", "provider-source-lifecycle"),
    ),
)
async def test_dry_run_records_only_the_fixed_provider_source_failure_category(
    failure: str,
    expected_stage: str,
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    tmp_path: Path,
) -> None:
    _patch_promotion_dependencies(monkeypatch)
    monkeypatch.setattr(rollout, "verify_reviewed_artifact_access", _no_artifact_check)
    group = _group(capable=False)
    raw = deepcopy(group.raw)
    container = cast(JSONObject, raw["container"])
    resources = cast(JSONObject, container["resources"])
    if failure == "group-name":
        group = replace(group, name="unreviewed-worker")
    elif failure == "container":
        raw["container"] = None
    elif failure == "image":
        container["image"] = _TARGET_IMAGE
    elif failure == "gpu-resources":
        resources["gpu_classes"] = [str(uuid4())]
    elif failure == "priority":
        raw["priority"] = "low"
    elif failure == "readiness-probe":
        raw["readiness_probe"] = rollout._readiness_probe("/wrong-ready")
    elif failure == "compute":
        resources["memory"] = 1
    elif failure == "autostart":
        raw["autostart_policy"] = True
    elif failure == "restart-policy":
        raw["restart_policy"] = "always"
    elif failure == "startup-probe":
        raw["startup_probe"] = None
    elif failure == "liveness-probe":
        raw["liveness_probe"] = None
    elif failure == "queue-connection":
        cast(JSONObject, raw["queue_connection"])["queue_name"] = "wrong-queue"
    elif failure == "autoscaler":
        cast(JSONObject, raw["queue_autoscaler"])["polling_period"] = 16
    elif failure == "group-identity":
        group = replace(group, id=uuid4())
    elif failure == "runtime-bindings":
        raw["runtime_bindings"] = [{"name": "unreviewed", "reference": "unreviewed"}]
    elif failure == "lifecycle":
        raw["replicas"] = 0
        group = replace(
            group,
            replicas=0,
            current_state=replace(group.current_state, status="stopped", running_count=0),
        )
    else:
        raise AssertionError(f"unknown failure {failure}")
    group = replace(group, raw=raw)
    client = _FakeSalad(group=group)

    with pytest.raises(I2VLoraRolloutError):
        await _dry_run(
            database=database,
            client=client,
            artifact=_ArtifactClient(),
            tmp_path=tmp_path,
        )

    assert json.loads((tmp_path / "rollout-diagnostic.json").read_text()) == {
        "schema": "gen-automation/i2v-lora-diagnostic/v1",
        "operation": "dry-run",
        "stage": expected_stage,
        "outcome": "preparing",
    }
    assert client.update_patches == []


@pytest.mark.parametrize(
    ("failure", "expected_stage"),
    (
        ("readiness-after-null-autoscaler-extension", "provider-source-readiness-probe"),
        ("null-autoscaler-extension", "provider-source-autoscaler"),
        ("queue-path", "provider-source-queue-connection"),
    ),
)
async def test_dry_run_provider_source_categories_follow_validator_precedence(
    failure: str,
    expected_stage: str,
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    tmp_path: Path,
) -> None:
    _patch_promotion_dependencies(monkeypatch)
    monkeypatch.setattr(rollout, "verify_reviewed_artifact_access", _no_artifact_check)
    group = _group(capable=False)
    raw = deepcopy(group.raw)
    autoscaler = cast(JSONObject, raw["queue_autoscaler"])
    if failure in {
        "readiness-after-null-autoscaler-extension",
        "null-autoscaler-extension",
    }:
        autoscaler["optional_provider_default"] = None
    if failure == "readiness-after-null-autoscaler-extension":
        raw["readiness_probe"] = rollout._readiness_probe("/wrong-ready")
    elif failure == "queue-path":
        cast(JSONObject, raw["queue_connection"])["path"] = "/wrong-worker"
    group = replace(group, raw=raw)
    client = _FakeSalad(group=group)

    with pytest.raises(I2VLoraRolloutError):
        await _dry_run(
            database=database,
            client=client,
            artifact=_ArtifactClient(),
            tmp_path=tmp_path,
        )

    assert json.loads((tmp_path / "rollout-diagnostic.json").read_text()) == {
        "schema": "gen-automation/i2v-lora-diagnostic/v1",
        "operation": "dry-run",
        "stage": expected_stage,
        "outcome": "preparing",
    }


async def test_dry_run_unknown_base_contract_failure_uses_redacted_fallback_category(
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    tmp_path: Path,
) -> None:
    _patch_promotion_dependencies(monkeypatch)
    monkeypatch.setattr(rollout, "verify_reviewed_artifact_access", _no_artifact_check)

    def reject_with_unknown_provider_message(_group: object, _config: object) -> None:
        raise I2VSaladError("secret provider value")

    monkeypatch.setattr(
        rollout,
        "validate_i2v_group_contract",
        reject_with_unknown_provider_message,
    )

    with pytest.raises(I2VLoraRolloutError, match="base contract is not rollout-safe"):
        await _dry_run(
            database=database,
            client=_FakeSalad(),
            artifact=_ArtifactClient(),
            tmp_path=tmp_path,
        )

    diagnostic = json.loads((tmp_path / "rollout-diagnostic.json").read_text())
    assert diagnostic == {
        "schema": "gen-automation/i2v-lora-diagnostic/v1",
        "operation": "dry-run",
        "stage": "provider-source-base-contract",
        "outcome": "preparing",
    }
    assert "secret provider value" not in json.dumps(diagnostic)


@pytest.mark.parametrize(
    ("failure", "expected_stage"),
    (
        ("zero-work", "provider-source-zero-work"),
        ("group-readback", "provider-source-group-readback"),
        ("gpu-class", "provider-source-gpu-class"),
        ("instance-readback", "provider-source-instance-readback"),
    ),
)
async def test_dry_run_provider_read_failures_keep_only_the_fixed_operation_category(
    failure: str,
    expected_stage: str,
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    tmp_path: Path,
) -> None:
    _patch_promotion_dependencies(monkeypatch)
    monkeypatch.setattr(rollout, "verify_reviewed_artifact_access", _no_artifact_check)

    class FailingReadSalad(_FakeSalad):
        async def get_queue(self, name: str) -> SaladQueue:
            if failure == "zero-work":
                raise RuntimeError("secret provider queue value")
            return await super().get_queue(name)

        async def get_container_group(self, name: str) -> SaladContainerGroup:
            if failure == "group-readback":
                raise RuntimeError("secret provider group value")
            return await super().get_container_group(name)

        async def list_gpu_classes(self) -> tuple[SaladGpuClass, ...]:
            if failure == "gpu-class":
                raise RuntimeError("secret provider GPU value")
            return await super().list_gpu_classes()

        async def list_container_group_instances(
            self, name: str
        ) -> SaladContainerGroupInstancePage:
            if failure == "instance-readback":
                raise RuntimeError("secret provider instance value")
            return await super().list_container_group_instances(name)

    with pytest.raises(RuntimeError, match="secret provider"):
        await _dry_run(
            database=database,
            client=FailingReadSalad(),
            artifact=_ArtifactClient(),
            tmp_path=tmp_path,
        )

    assert json.loads((tmp_path / "rollout-diagnostic.json").read_text()) == {
        "schema": "gen-automation/i2v-lora-diagnostic/v1",
        "operation": "dry-run",
        "stage": expected_stage,
        "outcome": "preparing",
    }


@pytest.mark.parametrize("value", (None, True, 0, "false"))
def test_prior_contract_rejects_every_explicit_non_false_autostart_policy(
    value: object,
) -> None:
    group = _group(capable=False)
    group.raw["autostart_policy"] = cast(Any, value)

    with pytest.raises(I2VLoraRolloutError, match="scheduling contract drifted"):
        rollout._validate_safe_prior_rollout_baseline(
            group,
            _instance_page(),
            _config(capable=False),
        )


def test_prior_contract_rejects_a_non_false_reviewed_autostart_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = I2VSaladConfig.container_configuration

    def _unexpected_autostart(self: I2VSaladConfig) -> JSONObject:
        desired = original(self)
        desired["autostart_policy"] = True
        return desired

    monkeypatch.setattr(I2VSaladConfig, "container_configuration", _unexpected_autostart)

    with pytest.raises(
        I2VLoraRolloutError, match="reviewed provider autostart contract is invalid"
    ):
        rollout._validate_safe_prior_rollout_baseline(
            _without_autostart_policy(_group(capable=False)),
            _instance_page(),
            _config(capable=False),
        )


def test_prior_contract_accepts_only_salads_injected_default_shm_size() -> None:
    group = _without_legacy_default_fields(_group(capable=False))
    container = cast(JSONObject, group.raw["container"])
    resources = cast(JSONObject, container["resources"])
    resources["shm_size"] = 64

    rollout._validate_safe_prior_rollout_baseline(
        group,
        _instance_page(),
        _config(capable=False),
    )

    resources["shm_size"] = 128
    with pytest.raises(I2VLoraRolloutError, match="compute contract drifted"):
        rollout._validate_safe_prior_rollout_baseline(
            group,
            _instance_page(),
            _config(capable=False),
        )


def test_prior_contract_rejects_extra_compute_resource_fields() -> None:
    group = _without_legacy_default_fields(_group(capable=False))
    container = cast(JSONObject, group.raw["container"])
    resources = cast(JSONObject, container["resources"])
    resources["shm_size"] = 64
    resources["unreviewed"] = None

    with pytest.raises(I2VLoraRolloutError, match="compute contract drifted"):
        rollout._validate_safe_prior_rollout_baseline(
            group,
            _instance_page(),
            _config(capable=False),
        )


def test_prior_contract_accepts_legacy_missing_baseline_readiness_probe() -> None:
    group = _without_autostart_policy(_group(capable=False))
    group.raw.pop("readiness_probe")

    rollout._validate_safe_prior_rollout_baseline(
        group,
        _instance_page(),
        _config(capable=False),
    )


def test_prior_contract_rejects_explicit_wrong_baseline_readiness_probe() -> None:
    group = _without_autostart_policy(_group(capable=False))
    group.raw["readiness_probe"] = rollout._readiness_probe("/wrong-ready")

    with pytest.raises(I2VLoraRolloutError, match="base contract is not rollout-safe"):
        rollout._validate_safe_prior_rollout_baseline(
            group,
            _instance_page(),
            _config(capable=False),
        )


async def test_promotion_is_one_patch_only_and_rechecks_zero_work_before_mutation(
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    tmp_path: Path,
) -> None:
    _patch_promotion_dependencies(monkeypatch)
    monkeypatch.setattr(rollout, "verify_reviewed_artifact_access", _no_artifact_check)
    client = _FakeSalad()

    result = await _promote(
        database=database,
        client=client,
        artifact=_ArtifactClient(),
        tmp_path=tmp_path,
    )

    assert result.operation == "promote"
    assert result.provider_image == _TARGET_IMAGE
    assert result.provider_ready
    assert client.list_jobs_calls == 2
    assert len(client.update_patches) == 1
    assert client.update_patches[0]["container"]["image"] == _TARGET_IMAGE
    assert _RuntimeEnvironment.calls == 0


async def test_promotion_accepts_exact_empty_allocating_prior_at_both_guards(
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    tmp_path: Path,
) -> None:
    _patch_promotion_dependencies(monkeypatch)
    monkeypatch.setattr(rollout, "verify_reviewed_artifact_access", _no_artifact_check)
    client = _FakeSalad(group=_allocating_group())
    client.instances = _empty_instance_page()

    result = await _promote(
        database=database,
        client=client,
        artifact=_ArtifactClient(),
        tmp_path=tmp_path,
    )

    assert result.provider_ready
    assert len(client.update_patches) == 1
    assert client.update_patches[0]["container"]["image"] == _TARGET_IMAGE


@pytest.mark.parametrize(
    "state",
    (
        SaladContainerGroupInstanceState.ALLOCATING,
        SaladContainerGroupInstanceState.DOWNLOADING,
        SaladContainerGroupInstanceState.CREATING,
    ),
)
async def test_promotion_accepts_exact_pre_running_singleton_at_both_guards(
    state: SaladContainerGroupInstanceState,
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    tmp_path: Path,
) -> None:
    _patch_promotion_dependencies(monkeypatch)
    monkeypatch.setattr(rollout, "verify_reviewed_artifact_access", _no_artifact_check)
    client = _FakeSalad(group=_pre_running_group(state))
    client.instances = _pre_running_instance_page(state)

    result = await _promote(
        database=database,
        client=client,
        artifact=_ArtifactClient(),
        tmp_path=tmp_path,
    )

    assert result.provider_ready
    assert client.list_jobs_calls == 2
    assert len(client.update_patches) == 1


@pytest.mark.parametrize("race", ("pending", "version"))
async def test_allocating_promotion_rejects_group_race_immediately_before_patch(
    race: str,
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    tmp_path: Path,
) -> None:
    _patch_promotion_dependencies(monkeypatch)
    monkeypatch.setattr(rollout, "verify_reviewed_artifact_access", _no_artifact_check)
    client = _FakeSalad(group=_allocating_group())
    client.instances = _empty_instance_page()
    reads = 0

    async def get_group(_name: str) -> SaladContainerGroup:
        nonlocal reads
        reads += 1
        if reads == 1:
            return client.group
        if race == "pending":
            return replace(client.group, pending_change=True)
        return replace(client.group, version=client.group.version + 1)

    monkeypatch.setattr(client, "get_container_group", get_group)

    with pytest.raises(I2VLoraRolloutError, match="group changed before rollout"):
        await _promote(
            database=database,
            client=client,
            artifact=_ArtifactClient(),
            tmp_path=tmp_path,
        )

    assert client.update_patches == []


async def test_allocating_promotion_rejects_queue_race_immediately_before_patch(
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    tmp_path: Path,
) -> None:
    _patch_promotion_dependencies(monkeypatch)
    monkeypatch.setattr(rollout, "verify_reviewed_artifact_access", _no_artifact_check)
    client = _FakeSalad(group=_allocating_group())
    client.instances = _empty_instance_page()
    queue_lengths = iter((0, 0, 1))

    async def get_queue(_name: str) -> SaladQueue:
        return _queue(length=next(queue_lengths))

    monkeypatch.setattr(client, "get_queue", get_queue)

    with pytest.raises(I2VLoraRolloutError, match="queue changed immediately"):
        await _promote(
            database=database,
            client=client,
            artifact=_ArtifactClient(),
            tmp_path=tmp_path,
        )

    assert client.update_patches == []


@pytest.mark.parametrize(
    "race",
    (
        "empty",
        "two",
        "stale-version",
        "ready",
        "started",
        "running",
        "stopping",
    ),
)
async def test_pre_running_promotion_rejects_instance_race_immediately_before_patch(
    race: str,
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    tmp_path: Path,
) -> None:
    _patch_promotion_dependencies(monkeypatch)
    monkeypatch.setattr(rollout, "verify_reviewed_artifact_access", _no_artifact_check)
    state = SaladContainerGroupInstanceState.DOWNLOADING
    client = _FakeSalad(group=_pre_running_group(state, version=8))
    initial = _pre_running_instance_page(state, version=8)
    if race == "empty":
        raced = _empty_instance_page()
    elif race == "two":
        instance = initial.instances[0]
        raced = SaladContainerGroupInstancePage(
            instances=(instance, replace(instance, id="instance-2"))
        )
    elif race == "stale-version":
        raced = _pre_running_instance_page(state, version=7)
    elif race == "ready":
        raced = _pre_running_instance_page(state, version=8, ready=True)
    elif race == "started":
        raced = _pre_running_instance_page(state, version=8, started=True)
    elif race == "running":
        raced = _pre_running_instance_page(SaladContainerGroupInstanceState.RUNNING, version=8)
    elif race == "stopping":
        raced = _pre_running_instance_page(SaladContainerGroupInstanceState.STOPPING, version=8)
    else:
        raise AssertionError(f"unknown race {race}")
    pages = iter((initial, raced))

    async def list_instances(_name: str) -> SaladContainerGroupInstancePage:
        return next(pages)

    monkeypatch.setattr(client, "list_container_group_instances", list_instances)

    with pytest.raises(I2VLoraRolloutError):
        await _promote(
            database=database,
            client=client,
            artifact=_ArtifactClient(),
            tmp_path=tmp_path,
        )

    assert client.update_patches == []


@pytest.mark.parametrize(
    "race",
    (
        "pending",
        "version",
        "replicas",
        "status",
        "allocating",
        "creating",
        "running",
        "stopping",
    ),
)
async def test_pre_running_promotion_rejects_group_race_immediately_before_patch(
    race: str,
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    tmp_path: Path,
) -> None:
    _patch_promotion_dependencies(monkeypatch)
    monkeypatch.setattr(rollout, "verify_reviewed_artifact_access", _no_artifact_check)
    state = SaladContainerGroupInstanceState.DOWNLOADING
    initial = _pre_running_group(state, version=8)
    raced = initial
    if race == "pending":
        raced = replace(raced, pending_change=True)
    elif race == "version":
        raced = replace(raced, version=9)
    elif race == "replicas":
        raced = replace(raced, replicas=2)
    elif race == "status":
        raced = replace(
            raced,
            current_state=replace(raced.current_state, status="stopped"),
        )
    elif race == "allocating":
        raced = replace(
            raced,
            current_state=replace(raced.current_state, allocating_count=1),
        )
    elif race == "creating":
        raced = replace(
            raced,
            current_state=replace(raced.current_state, creating_count=0),
        )
    elif race == "running":
        raced = replace(
            raced,
            current_state=replace(raced.current_state, running_count=1),
        )
    elif race == "stopping":
        raced = replace(
            raced,
            current_state=replace(raced.current_state, stopping_count=1),
        )
    else:
        raise AssertionError(f"unknown race {race}")
    client = _FakeSalad(group=initial)
    client.instances = _pre_running_instance_page(state, version=8)
    reads = 0

    async def get_group(_name: str) -> SaladContainerGroup:
        nonlocal reads
        reads += 1
        return initial if reads == 1 else raced

    monkeypatch.setattr(client, "get_container_group", get_group)

    with pytest.raises(I2VLoraRolloutError):
        await _promote(
            database=database,
            client=client,
            artifact=_ArtifactClient(),
            tmp_path=tmp_path,
        )

    assert client.update_patches == []


async def test_pre_running_promotion_accepts_safe_lifecycle_progress_before_patch(
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    tmp_path: Path,
) -> None:
    _patch_promotion_dependencies(monkeypatch)
    monkeypatch.setattr(rollout, "verify_reviewed_artifact_access", _no_artifact_check)
    initial_state = SaladContainerGroupInstanceState.ALLOCATING
    current_state = SaladContainerGroupInstanceState.DOWNLOADING
    initial_group = _pre_running_group(initial_state, version=8)
    current_group = _pre_running_group(current_state, version=8)
    client = _FakeSalad(group=initial_group)
    groups = iter((initial_group, current_group))
    pages = iter(
        (
            _pre_running_instance_page(initial_state, version=8),
            _pre_running_instance_page(current_state, version=8),
        )
    )
    group_reads = 0
    instance_reads = 0

    async def get_group(_name: str) -> SaladContainerGroup:
        nonlocal group_reads
        group_reads += 1
        return next(groups) if group_reads <= 2 else client.group

    async def list_instances(_name: str) -> SaladContainerGroupInstancePage:
        nonlocal instance_reads
        instance_reads += 1
        return next(pages) if instance_reads <= 2 else client.instances

    monkeypatch.setattr(client, "get_container_group", get_group)
    monkeypatch.setattr(client, "list_container_group_instances", list_instances)

    result = await _promote(
        database=database,
        client=client,
        artifact=_ArtifactClient(),
        tmp_path=tmp_path,
    )

    assert result.provider_ready
    assert len(client.update_patches) == 1


async def _no_artifact_check(_client: object, _manifest: object, **_kwargs: object) -> None:
    return None


async def test_second_zero_work_guard_blocks_toctou_provider_job_without_patching(
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    tmp_path: Path,
) -> None:
    _patch_promotion_dependencies(monkeypatch)
    monkeypatch.setattr(rollout, "verify_reviewed_artifact_access", _no_artifact_check)
    client = _FakeSalad()
    client.job_pages = [(), (_job(),)]

    with pytest.raises(I2VLoraRolloutError, match="requires zero active"):
        await _promote(
            database=database,
            client=client,
            artifact=_ArtifactClient(),
            tmp_path=tmp_path,
        )

    assert client.list_jobs_calls == 2
    assert client.update_patches == []


@pytest.mark.parametrize(
    "group",
    (
        _group(capable=False, pending_change=True),
        _group(capable=False, replicas=0),
    ),
)
async def test_promotion_refuses_unstable_provider_group_before_patch(
    group: SaladContainerGroup,
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    tmp_path: Path,
) -> None:
    _patch_promotion_dependencies(monkeypatch)
    monkeypatch.setattr(rollout, "verify_reviewed_artifact_access", _no_artifact_check)
    client = _FakeSalad(group=group)
    client.instances = (
        _empty_instance_page() if group.replicas == 0 else _instance_page(version=group.version)
    )

    with pytest.raises(I2VLoraRolloutError):
        await _promote(
            database=database,
            client=client,
            artifact=_ArtifactClient(),
            tmp_path=tmp_path,
        )

    assert client.update_patches == []


async def test_failed_promotion_automatically_rolls_back_with_tombstones_and_rotated_credentials(
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    tmp_path: Path,
) -> None:
    _patch_promotion_dependencies(monkeypatch)
    monkeypatch.setattr(rollout, "verify_reviewed_artifact_access", _no_artifact_check)
    client = _FakeSalad()

    wait_calls = 0

    async def fail_target_readiness_then_accept_rollback(
        _client: object, **_kwargs: object
    ) -> SaladContainerGroup:
        nonlocal wait_calls
        wait_calls += 1
        if wait_calls == 1:
            raise I2VLoraRolloutError("synthetic target readiness failure")
        return client.group

    monkeypatch.setattr(
        rollout, "_wait_for_ready_group", fail_target_readiness_then_accept_rollback
    )

    with pytest.raises(I2VLoraRolloutError, match="synthetic target readiness failure"):
        await _promote(
            database=database,
            client=client,
            artifact=_ArtifactClient(),
            tmp_path=tmp_path,
        )

    assert len(client.update_patches) == 2
    rollback_patch = cast(JSONObject, client.update_patches[1]["container"])
    rollback_environment = cast(JSONObject, rollback_patch["environment_variables"])
    assert rollback_patch["image"] == _PRIOR_IMAGE
    for key in set(_TARGET_ENVIRONMENT) - set(_PRIOR_ENVIRONMENT):
        assert rollback_environment[key] is None
    for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        assert rollback_environment[key] == _ROTATED_PRIOR_ENVIRONMENT[key]
    assert client.group.raw["container"]["environment_variables"] == _ROTATED_PRIOR_ENVIRONMENT
    assert _RuntimeEnvironment.calls == 1
    assert wait_calls == 2


async def test_promotion_accepts_deferred_exact_next_version_acknowledgement(
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    tmp_path: Path,
) -> None:
    _patch_promotion_dependencies(monkeypatch)
    monkeypatch.setattr(rollout.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(rollout, "verify_reviewed_artifact_access", _no_artifact_check)
    client = _DeferredPatchResponseSalad()

    result = await _promote(
        database=database,
        client=client,
        artifact=_ArtifactClient(),
        tmp_path=tmp_path,
    )

    assert result.provider_ready
    assert client.group.version == 2


@pytest.mark.parametrize(
    ("version_offset", "pending_change", "name"),
    (
        (0, False, "i2v-worker-v1"),
        (-1, True, "i2v-worker-v1"),
        (2, True, "i2v-worker-v1"),
        (1, False, "other-worker"),
    ),
)
def test_patch_acknowledgement_rejects_ambiguous_identity_or_version(
    version_offset: int,
    pending_change: bool,
    name: str,
) -> None:
    response = replace(
        _group(capable=False, version=10 + version_offset),
        name=name,
        pending_change=pending_change,
    )

    with pytest.raises(I2VLoraRolloutError, match="patch response"):
        rollout._validate_accepted_patch_response(
            response,
            group_id=str(_GROUP_ID),
            group_name="i2v-worker-v1",
            prior_version=10,
        )


async def test_ambiguous_promotion_response_rolls_back_when_provider_applied_the_patch(
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    tmp_path: Path,
) -> None:
    _patch_promotion_dependencies(monkeypatch)
    monkeypatch.setattr(rollout, "verify_reviewed_artifact_access", _no_artifact_check)
    client = _FakeSalad()
    client.raise_after_first_update = True

    with pytest.raises(RuntimeError, match="ambiguous provider response"):
        await _promote(
            database=database,
            client=client,
            artifact=_ArtifactClient(),
            tmp_path=tmp_path,
        )

    assert len(client.update_patches) == 2
    assert client.group.raw["container"]["image"] == _PRIOR_IMAGE
    assert client.group.raw["container"]["environment_variables"] == _ROTATED_PRIOR_ENVIRONMENT
    assert _RuntimeEnvironment.calls == 1


async def test_rollback_refuses_to_clobber_a_later_provider_contract_drift() -> None:
    prior = _group(capable=False, version=1)
    state = _rollback_state(prior)
    client = _FakeSalad(
        group=_group(
            capable=True,
            version=3,
            environment={**_TARGET_ENVIRONMENT, "UNREVIEWED_DRIFT": "present"},
        )
    )
    client.instances = _instance_page(version=3)

    with pytest.raises(I2VLoraRolloutError, match="contract changed"):
        await rollout._restore_provider(
            cast(Any, client),
            state=state,
            environment=_ROTATED_PRIOR_ENVIRONMENT,
            timeout_seconds=0.1,
        )

    assert client.update_patches == []


async def test_rollback_refuses_same_id_with_wrong_saved_group_name_before_patch() -> None:
    prior = _group(capable=False, version=1)
    state = _rollback_state(prior)
    client = _FakeSalad(group=replace(_group(capable=True, version=3), name="other-worker"))

    with pytest.raises(I2VLoraRolloutError, match="identity changed"):
        await rollout._restore_provider(
            cast(Any, client),
            state=state,
            environment=_ROTATED_PRIOR_ENVIRONMENT,
            timeout_seconds=0.1,
        )

    assert client.update_patches == []


async def test_rollback_accepts_a_later_exact_promoted_contract() -> None:
    prior = _group(capable=False, version=1)
    state = _rollback_state(prior)
    client = _FakeSalad(group=_group(capable=True, version=3))
    client.instances = _instance_page(version=3)

    restored = await rollout._restore_provider(
        cast(Any, client),
        state=state,
        environment=_ROTATED_PRIOR_ENVIRONMENT,
        timeout_seconds=0.1,
    )

    assert restored.raw["container"]["image"] == _PRIOR_IMAGE
    assert len(client.update_patches) == 1


async def test_rollback_preserves_legacy_missing_default_fields() -> None:
    prior = _without_legacy_default_fields(_group(capable=False, version=1))
    state = _rollback_state(prior)
    promoted = _without_autostart_policy(_group(capable=True, version=3))
    client = _FakeSalad(group=promoted)
    client.instances = _instance_page(version=3)

    restored = await rollout._restore_provider(
        cast(Any, client),
        state=state,
        environment=_ROTATED_PRIOR_ENVIRONMENT,
        timeout_seconds=0.1,
    )

    assert "autostart_policy" not in state.prior_contract
    assert state.prior_readiness_probe is None
    assert "autostart_policy" not in restored.raw
    assert "readiness_probe" not in restored.raw
    assert restored.raw["container"]["image"] == _PRIOR_IMAGE


async def test_rollback_accepts_deferred_promoted_profile_patch_acknowledgement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rollout.asyncio, "sleep", _no_sleep)
    prior = _group(capable=False, version=1)
    state = _rollback_state(prior)
    client = _DeferredPatchResponseSalad(group=_group(capable=True, version=3))
    client.instances = _instance_page(version=3)

    restored = await rollout._restore_provider(
        cast(Any, client),
        state=state,
        environment=_ROTATED_PRIOR_ENVIRONMENT,
        timeout_seconds=1,
    )

    assert restored.raw["container"]["image"] == _PRIOR_IMAGE
    assert restored.version == 4
    assert client.start_calls == 0


async def test_exact_restored_prior_waits_for_readiness_without_another_patch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prior = _group(capable=False, version=3)
    client = _FakeSalad(group=prior)
    client.instances = _empty_instance_page()
    state = _rollback_state(_group(capable=False, version=1))
    calls = 0

    async def instances(_name: str) -> SaladContainerGroupInstancePage:
        nonlocal calls
        calls += 1
        return _empty_instance_page() if calls == 1 else _instance_page(version=3)

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(client, "list_container_group_instances", instances)
    monkeypatch.setattr(rollout.asyncio, "sleep", no_sleep)

    restored = await rollout._restore_provider(
        cast(Any, client),
        state=state,
        environment=_ROTATED_PRIOR_ENVIRONMENT,
        timeout_seconds=1,
    )

    assert restored == prior
    assert calls == 2
    assert client.update_patches == []


async def test_rollback_does_not_patch_an_exact_prior_version_only_to_rotate_credentials() -> None:
    prior = _group(capable=False, version=1)
    state = _rollback_state(prior)
    client = _FakeSalad(group=prior)
    client.instances = _instance_page(version=1)

    restored = await rollout._restore_provider(
        cast(Any, client),
        state=state,
        environment=_ROTATED_PRIOR_ENVIRONMENT,
        timeout_seconds=0.1,
    )

    assert restored == prior
    assert client.update_patches == []
    assert prior.raw["container"]["environment_variables"] == _PRIOR_ENVIRONMENT


async def test_rollback_recovers_an_exact_warm_idle_promoted_group() -> None:
    prior = _group(capable=False, version=1)
    state = _rollback_state(prior)
    client = _FakeSalad(group=_group(capable=True, version=3, replicas=0))
    client.instances = _empty_instance_page()

    restored = await rollout._restore_provider(
        cast(Any, client),
        state=state,
        environment=_ROTATED_PRIOR_ENVIRONMENT,
        timeout_seconds=1,
    )

    assert restored.raw["container"]["image"] == _PRIOR_IMAGE
    assert client.start_calls == 1
    assert len(client.update_patches) == 1


async def test_rollback_accepts_deferred_stopped_prior_credential_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rollout.asyncio, "sleep", _no_sleep)
    prior = _group(capable=False, version=1)
    state = _rollback_state(prior)
    client = _DeferredPatchResponseSalad(group=_group(capable=False, version=3, replicas=0))
    client.instances = _empty_instance_page()

    restored = await rollout._restore_provider(
        cast(Any, client),
        state=state,
        environment=_ROTATED_PRIOR_ENVIRONMENT,
        timeout_seconds=1,
    )

    assert restored.version == 4
    assert client.start_calls == 1


async def test_deferred_patch_must_converge_before_a_stopped_group_is_started() -> None:
    client = _FakeSalad(group=replace(_group(capable=False, version=3), pending_change=True))
    client.instances = _empty_instance_page()
    state = _rollback_state(_group(capable=False, version=1))

    with pytest.raises(I2VLoraRolloutError, match="exact saved version"):
        await rollout._start_exact_group_if_stopped(
            cast(Any, client),
            state=state,
            environment=_ROTATED_PRIOR_ENVIRONMENT,
            expected_version=4,
            deadline=asyncio.get_running_loop().time() + 0.01,
        )

    assert client.start_calls == 0


@pytest.mark.parametrize("retained_instance", (False, True))
async def test_rollback_refuses_to_start_wrong_or_nonempty_exact_next_version(
    retained_instance: bool,
) -> None:
    state = _rollback_state(_group(capable=False, version=1))
    group = _group(
        capable=False if retained_instance else True,
        version=4,
        replicas=0,
        environment=_ROTATED_PRIOR_ENVIRONMENT if retained_instance else _TARGET_ENVIRONMENT,
    )
    client = _FakeSalad(group=group)
    client.instances = _instance_page(version=4) if retained_instance else _empty_instance_page()

    with pytest.raises(I2VLoraRolloutError):
        await rollout._start_exact_group_if_stopped(
            cast(Any, client),
            state=state,
            environment=_ROTATED_PRIOR_ENVIRONMENT,
            expected_version=4,
            deadline=asyncio.get_running_loop().time() + 1,
        )

    assert client.start_calls == 0


async def test_rollback_rereads_stopped_prior_immediately_before_start() -> None:
    state = _rollback_state(_group(capable=False, version=1))
    client = _FakeSalad(
        group=_group(
            capable=False,
            version=4,
            replicas=0,
            environment=_ROTATED_PRIOR_ENVIRONMENT,
        )
    )
    client.instances = _empty_instance_page()
    reads = 0

    async def instances(_name: str) -> SaladContainerGroupInstancePage:
        nonlocal reads
        reads += 1
        if reads == 1:
            client.group = replace(client.group, name="raced-worker")
        return _empty_instance_page()

    client.list_container_group_instances = instances  # type: ignore[method-assign]

    with pytest.raises(I2VLoraRolloutError, match="identity changed"):
        await rollout._start_exact_group_if_stopped(
            cast(Any, client),
            state=state,
            environment=_ROTATED_PRIOR_ENVIRONMENT,
            expected_version=4,
            deadline=asyncio.get_running_loop().time() + 1,
        )

    assert client.start_calls == 0


async def test_provider_job_pagination_has_a_hard_one_hundred_page_bound() -> None:
    class EndlessClient:
        def __init__(self) -> None:
            self.calls = 0

        async def list_jobs(self, _name: str, *, page: int, page_size: int) -> SimpleNamespace:
            self.calls += 1
            assert page == self.calls
            return SimpleNamespace(
                items=tuple(
                    SimpleNamespace(
                        id=f"{page}-{index}",
                        status=SaladJobStatus.SUCCEEDED,
                    )
                    for index in range(page_size)
                )
            )

    client = EndlessClient()
    with pytest.raises(I2VLoraRolloutError, match="safety bound"):
        await rollout._provider_active_jobs(cast(Any, client), queue_name="i2v-jobs-v1")
    assert client.calls == 100


def _patch_profile_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    class RuntimeEnvironment:
        def __init__(self, *, settings: Settings, resolver: object) -> None:
            del settings, resolver

        async def resolve(self) -> Mapping[str, str]:
            return _TARGET_ENVIRONMENT

    monkeypatch.setattr(rollout, "I2VRuntimeEnvironment", RuntimeEnvironment)
    monkeypatch.setattr(
        rollout,
        "i2v_runtime_config_from_settings",
        lambda _settings: SimpleNamespace(salad=_config(capable=True)),
    )
    monkeypatch.setattr(
        rollout,
        "i2v_worker_identity",
        lambda _settings: (
            rollout.REVIEWED_MODEL_OBJECTS_SHA256,
            rollout.REVIEWED_ARTIFACT_IDENTITY_SHA256,
        ),
    )
    monkeypatch.setattr(rollout, "_validate_worker_environment_identity", lambda *_a, **_k: None)


async def _profile_preflight(
    *, database: Any, client: _FakeSalad, settings: Settings | None = None
) -> rollout.I2VLoraRolloutResult:
    return await rollout.profile_preflight(
        settings=settings or _settings(capable=True),
        sessions=database.sessions,
        salad_client=cast(Any, client),
        resolver=cast(Any, object()),
        expected_worker_image=_TARGET_IMAGE,
        expected_worker_source_revision=_TARGET_REVISION,
        expected_public_profile=False,
    )


async def test_profile_preflight_accepts_only_the_exact_current_ready_group(
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
) -> None:
    _patch_profile_dependencies(monkeypatch)
    client = _FakeSalad(group=_group(capable=True, version=9))
    client.instances = _instance_page(version=9)

    result = await _profile_preflight(database=database, client=client)

    assert result.operation == "profile-preflight"
    assert result.provider_ready
    assert result.provider_active_jobs == 0


async def test_profile_preflight_accepts_legacy_missing_autostart_policy(
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
) -> None:
    _patch_profile_dependencies(monkeypatch)
    group = _without_autostart_policy(_group(capable=True, version=9))
    client = _FakeSalad(group=group)
    client.instances = _instance_page(version=9)

    result = await _profile_preflight(database=database, client=client)

    assert result.provider_ready
    assert "autostart_policy" not in client.group.raw


async def test_profile_preflight_rejects_missing_capability_readiness_probe(
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
) -> None:
    _patch_profile_dependencies(monkeypatch)
    group = _without_autostart_policy(_group(capable=True, version=9))
    group.raw.pop("readiness_probe")
    client = _FakeSalad(group=group)
    client.instances = _instance_page(version=9)

    with pytest.raises(I2VLoraRolloutError, match="base contract is not rollout-safe"):
        await _profile_preflight(database=database, client=client)


@pytest.mark.parametrize(
    "state",
    (
        SaladContainerGroupInstanceState.ALLOCATING,
        SaladContainerGroupInstanceState.DOWNLOADING,
        SaladContainerGroupInstanceState.CREATING,
    ),
)
async def test_profile_preflight_still_rejects_pre_running_singletons(
    state: SaladContainerGroupInstanceState,
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
) -> None:
    _patch_profile_dependencies(monkeypatch)
    capable = _group(capable=True, version=9)
    pre_running = replace(
        capable,
        current_state=replace(
            capable.current_state,
            allocating_count=int(state == SaladContainerGroupInstanceState.ALLOCATING),
            creating_count=int(
                state
                in {
                    SaladContainerGroupInstanceState.DOWNLOADING,
                    SaladContainerGroupInstanceState.CREATING,
                }
            ),
            running_count=0,
            stopping_count=0,
        ),
    )
    client = _FakeSalad(group=pre_running)
    client.instances = _pre_running_instance_page(state, version=9)

    with pytest.raises(I2VLoraRolloutError, match="exact warm replica"):
        await _profile_preflight(database=database, client=client)

    assert client.update_patches == []


def _with_group_mutation(
    group: SaladContainerGroup,
    mutation: str,
) -> tuple[SaladContainerGroup, SaladContainerGroupInstancePage]:
    if mutation == "pending":
        return replace(group, pending_change=True), _instance_page(version=group.version)
    if mutation == "allocating":
        allocating = replace(
            group,
            current_state=replace(
                group.current_state,
                allocating_count=1,
                running_count=0,
            ),
        )
        return allocating, _empty_instance_page()
    if mutation == "stale-instance":
        return group, _instance_page(version=group.version - 1)
    if mutation == "zero-replica":
        stopped = replace(
            group,
            replicas=0,
            current_state=replace(group.current_state, status="stopped", running_count=0),
        )
        stopped.raw["replicas"] = 0
        return stopped, _empty_instance_page()
    raw = deepcopy(group.raw)
    if mutation == "wrong-group":
        return replace(group, name="other-worker", raw=raw), _instance_page(version=group.version)
    if mutation == "wrong-group-id":
        return replace(group, id=uuid4(), raw=raw), _instance_page(version=group.version)
    if mutation == "wrong-queue":
        raw["queue_connection"] = {"queue_name": "other-queue", "path": "/jobs/i2v", "port": 8000}
    elif mutation == "wrong-image":
        cast(JSONObject, raw["container"])["image"] = _PRIOR_IMAGE
    elif mutation == "wrong-gpu":
        cast(JSONObject, raw["container"])["resources"] = {
            "cpu": 8,
            "memory": 32_768,
            "storage_amount": 268_435_456_000,
            "gpu_classes": [str(uuid4())],
        }
    elif mutation == "wrong-scheduling":
        raw["autostart_policy"] = True
    elif mutation == "wrong-probe":
        raw["readiness_probe"] = rollout._readiness_probe("/ready")
    elif mutation == "wrong-runtime-bindings":
        raw["runtime_bindings"] = [{"name": "unreviewed", "reference": "secret"}]
    elif mutation == "extra-environment":
        environment = cast(JSONObject, cast(JSONObject, raw["container"])["environment_variables"])
        environment["UNREVIEWED_EXTRA"] = "not-allowed"
    elif mutation == "wrong-environment":
        environment = cast(JSONObject, cast(JSONObject, raw["container"])["environment_variables"])
        environment["GEN_I2V_WORKER_SOURCE_REVISION"] = "d" * 40
    else:
        raise AssertionError(f"unknown mutation {mutation}")
    return replace(group, raw=raw), _instance_page(version=group.version)


@pytest.mark.parametrize(
    "mutation",
    (
        "pending",
        "allocating",
        "stale-instance",
        "zero-replica",
        "wrong-group",
        "wrong-group-id",
        "wrong-queue",
        "wrong-image",
        "wrong-gpu",
        "wrong-scheduling",
        "wrong-probe",
        "wrong-runtime-bindings",
        "extra-environment",
        "wrong-environment",
    ),
)
async def test_profile_preflight_rejects_every_provider_contract_drift(
    mutation: str,
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
) -> None:
    _patch_profile_dependencies(monkeypatch)
    group, instances = _with_group_mutation(_group(capable=True, version=9), mutation)
    client = _FakeSalad(group=group)
    client.instances = instances

    with pytest.raises(I2VLoraRolloutError):
        await _profile_preflight(database=database, client=client)

    assert client.update_patches == []


async def test_profile_preflight_requires_active_attempts_to_pin_the_exact_worker_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_profile_dependencies(monkeypatch)
    provider_job = _job()

    class Transaction:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *_args: object) -> None:
            return None

    class Session:
        def __init__(self, worker_image: str) -> None:
            self.worker_image = worker_image

        async def __aenter__(self) -> Session:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        def begin(self) -> Transaction:
            return Transaction()

        def get_bind(self) -> SimpleNamespace:
            return SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))

        async def execute(self, _statement: object) -> SimpleNamespace:
            attempt = SimpleNamespace(
                worker_image_digest=self.worker_image,
                provider_job_id=str(provider_job.id),
            )
            job = SimpleNamespace(settings_snapshot={})
            return SimpleNamespace(all=lambda: [(attempt, job)])

    class Sessions:
        def __init__(self, worker_image: str) -> None:
            self.worker_image = worker_image

        def __call__(self) -> Session:
            return Session(self.worker_image)

    client = _FakeSalad(group=_group(capable=True, version=9))
    client.instances = _instance_page(version=9)
    client.job_pages = [(provider_job,)]

    exact = SimpleNamespace(sessions=Sessions(_TARGET_IMAGE))
    result = await _profile_preflight(database=exact, client=client)
    assert result.durable_active_attempts == 1
    assert result.provider_active_jobs == 1

    client.list_jobs_calls = 0
    wrong = SimpleNamespace(sessions=Sessions(_PRIOR_IMAGE))
    with pytest.raises(I2VLoraRolloutError, match="different worker image"):
        await _profile_preflight(database=wrong, client=client)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("i2v_enabled", False),
        ("i2v_hires_profile_enabled", False),
        ("i2v_lora_worker_enabled", False),
        ("i2v_lora_profile_enabled", True),
        ("i2v_worker_image", _PRIOR_IMAGE),
        ("i2v_worker_source_revision", "d" * 40),
        ("i2v_private_manifest_source_sha256", "e" * 64),
        ("i2v_model_manifest_sha256", SecretStr("f" * 64)),
    ),
)
async def test_profile_preflight_rejects_every_host_identity_mismatch_before_provider_read(
    field: str,
    value: object,
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
) -> None:
    _patch_profile_dependencies(monkeypatch)
    settings = _settings(capable=True).model_copy(update={field: value})

    class UnreadableProvider(_FakeSalad):
        async def get_container_group(self, _name: str) -> SaladContainerGroup:
            raise AssertionError("host mismatch must fail before a provider read")

    with pytest.raises(I2VLoraRolloutError, match="host I2V profile"):
        await _profile_preflight(
            database=database,
            client=UnreadableProvider(group=_group(capable=True)),
            settings=settings,
        )
