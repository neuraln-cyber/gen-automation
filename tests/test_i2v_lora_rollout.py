from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr

from gen_automation.config import Settings
from gen_automation.db.session import Database
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
from gen_automation.services.i2v_lora_rollout import (
    I2VLoraRolloutError,
    PreparedReviewedManifest,
    ReviewedManifestCoordinates,
)
from gen_automation.services.i2v_salad import I2V_SALAD_GPU_CLASS_NAME, I2VSaladConfig

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
        container["image"] = patch_container["image"]
        current_environment = cast(dict[str, str], dict(container["environment_variables"]))
        for key, value in cast(JSONObject, patch_container["environment_variables"]).items():
            if value is None:
                current_environment.pop(key, None)
            else:
                assert isinstance(value, str)
                current_environment[key] = value
        container["environment_variables"] = current_environment
        raw["readiness_probe"] = deepcopy(patch.get("readiness_probe"))
        next_version = self.group.version + 1
        replicas = self.group.replicas
        self.group = SaladContainerGroup(
            id=self.group.id,
            name=self.group.name,
            display_name=self.group.display_name,
            replicas=replicas,
            pending_change=False,
            version=next_version,
            current_state=replace(
                self.group.current_state,
                status="running" if replicas else "stopped",
                running_count=replicas,
            ),
            create_time=self.group.create_time,
            update_time=_NOW,
            raw=raw,
        )
        self.instances = (
            _instance_page(version=next_version) if replicas else _empty_instance_page()
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
        raise AssertionError("rollout must not call a provider stop endpoint")


class _RuntimeEnvironment:
    calls = 0

    def __init__(self, *, settings: Settings, resolver: object) -> None:
        del settings, resolver

    async def resolve(self) -> Mapping[str, str]:
        type(self).calls += 1
        return _ROTATED_PRIOR_ENVIRONMENT


def _patch_promotion_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    _RuntimeEnvironment.calls = 0

    async def fetch(_client: object, _coordinates: object) -> PreparedReviewedManifest:
        return _MANIFEST

    async def target_environment(_settings: Settings, _resolver: object) -> Mapping[str, str]:
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


async def _no_artifact_check(_client: object, _manifest: object) -> None:
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


def _with_group_mutation(
    group: SaladContainerGroup,
    mutation: str,
) -> tuple[SaladContainerGroup, SaladContainerGroupInstancePage]:
    if mutation == "pending":
        return replace(group, pending_change=True), _instance_page(version=group.version)
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
