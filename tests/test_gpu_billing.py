from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from gen_automation.db.models import SaladDeployment
from gen_automation.db.session import Database
from gen_automation.domain.enums import (
    DesiredDeploymentState,
    ReleasePhase,
    ResourceHealth,
    SaladDeploymentState,
)
from gen_automation.integrations.salad.errors import SaladTransportError
from gen_automation.integrations.salad.models import (
    SaladContainerGroup,
    SaladContainerGroupInstance,
    SaladContainerGroupInstancePage,
    SaladContainerGroupInstanceState,
    SaladContainerGroupState,
)
from gen_automation.services.experiments import (
    ExperimentStatus,
    ExperimentVariantStatus,
    experiment_progress_payload,
)
from gen_automation.services.gpu_billing import (
    GpuBillingSnapshot,
    GpuBillingState,
    _snapshot_for_deployment,
    gpu_billing_payload,
    gpu_billing_poll_after_ms,
    load_shared_gpu_billing_snapshot,
)
from gen_automation.services.new_sets import (
    GenerationImageProgress,
    GenerationJobProgress,
    GenerationProgressStage,
    GenerationProgressStageView,
    NewSetStatus,
    new_set_progress_payload,
)
from gen_automation.services.salad_deployments import (
    _apply_billing_observation,
    _refresh_billing_observation,
)

NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)
GROUP_ID = UUID("ab3a4591-efc3-46c0-b06a-3d820c0ec100")


def _deployment(
    version_no: int = 1,
    *,
    is_current: bool = True,
) -> SaladDeployment:
    return SaladDeployment(
        version_no=version_no,
        config_sha256=f"{version_no:x}" * 64,
        provider_configuration={},
        worker_image_digest="registry.example.test/worker@sha256:" + ("b" * 64),
        organization_name="creator-org",
        project_name="production",
        queue_name=f"generation-v{version_no}",
        container_group_name=f"worker-v{version_no}",
        state=SaladDeploymentState.PLANNED,
        desired_state=DesiredDeploymentState.ACTIVE,
        is_current=is_current,
        max_hourly_cost_microusd=3_600_000,
        observed_replicas=0,
        ready_replicas=0,
        billing_accumulated_microseconds=0,
        billing_observation_stale=False,
        billing_estimated=False,
    )


def _instance(
    instance_id: str,
    state: SaladContainerGroupInstanceState,
    update_time: datetime,
) -> SaladContainerGroupInstance:
    return SaladContainerGroupInstance(
        id=instance_id,
        machine_id=f"machine-{instance_id}",
        state=state,
        update_time=update_time,
        version=1,
    )


def _group(*, replicas: int = 1, running: int = 1) -> SaladContainerGroup:
    return SaladContainerGroup(
        id=GROUP_ID,
        name="worker-v1",
        display_name="Worker v1",
        replicas=replicas,
        pending_change=False,
        version=1,
        current_state=SaladContainerGroupState(
            status="running",
            description="running",
            allocating_count=0,
            creating_count=0,
            running_count=running,
            stopping_count=0,
            start_time=NOW,
            finish_time=None,
        ),
        create_time=NOW,
        update_time=NOW,
        raw={},
    )


def test_running_segments_exclude_free_reallocation_gap_and_end_idempotently() -> None:
    deployment = _deployment()
    first_running = _instance(
        "instance-a",
        SaladContainerGroupInstanceState.RUNNING,
        NOW,
    )
    _apply_billing_observation(
        deployment,
        instances=(first_running,),
        running=(first_running,),
        observed_at=NOW + timedelta(seconds=5),
        end_session=False,
    )

    first_stopping = _instance(
        "instance-a",
        SaladContainerGroupInstanceState.STOPPING,
        NOW + timedelta(seconds=10),
    )
    _apply_billing_observation(
        deployment,
        instances=(first_stopping,),
        running=(),
        observed_at=NOW + timedelta(seconds=12),
        end_session=False,
    )
    paused = _snapshot_for_deployment(deployment, now=NOW + timedelta(seconds=15))
    assert paused.state == GpuBillingState.PAUSED
    assert paused.elapsed_seconds == 10

    replacement = _instance(
        "instance-b",
        SaladContainerGroupInstanceState.RUNNING,
        NOW + timedelta(seconds=20),
    )
    _apply_billing_observation(
        deployment,
        instances=(replacement,),
        running=(replacement,),
        observed_at=NOW + timedelta(seconds=25),
        end_session=False,
    )
    charging = _snapshot_for_deployment(deployment, now=NOW + timedelta(seconds=30))
    assert charging.state == GpuBillingState.CHARGING
    assert charging.elapsed_seconds == 20
    assert charging.fresh_for_seconds == 85

    replacement_stopping = _instance(
        "instance-b",
        SaladContainerGroupInstanceState.STOPPING,
        NOW + timedelta(seconds=30),
    )
    _apply_billing_observation(
        deployment,
        instances=(replacement_stopping,),
        running=(),
        observed_at=NOW + timedelta(seconds=35),
        end_session=True,
    )
    ended_at = deployment.billing_session_ended_at
    assert (
        _snapshot_for_deployment(deployment, now=NOW + timedelta(seconds=40)).elapsed_seconds == 20
    )

    _apply_billing_observation(
        deployment,
        instances=(),
        running=(),
        observed_at=NOW + timedelta(minutes=5),
        end_session=True,
    )
    assert deployment.billing_session_ended_at == ended_at


def test_new_preparation_resets_ended_session_before_next_running_start() -> None:
    deployment = _deployment()
    deployment.billing_session_started_at = NOW - timedelta(minutes=2)
    deployment.billing_session_ended_at = NOW - timedelta(minutes=1)
    deployment.billing_accumulated_microseconds = 60_000_000
    deployment.billing_estimated = True
    allocating = _instance(
        "instance-next",
        SaladContainerGroupInstanceState.ALLOCATING,
        NOW,
    )

    _apply_billing_observation(
        deployment,
        instances=(allocating,),
        running=(),
        observed_at=NOW,
        end_session=False,
    )
    clean = _snapshot_for_deployment(deployment, now=NOW)
    assert clean.state == GpuBillingState.NOT_STARTED
    assert clean.elapsed_seconds == 0

    running = _instance(
        "instance-next",
        SaladContainerGroupInstanceState.RUNNING,
        NOW + timedelta(seconds=20),
    )
    _apply_billing_observation(
        deployment,
        instances=(running,),
        running=(running,),
        observed_at=NOW + timedelta(seconds=25),
        end_session=False,
    )
    restarted = _snapshot_for_deployment(deployment, now=NOW + timedelta(seconds=30))
    assert restarted.state == GpuBillingState.CHARGING
    assert restarted.elapsed_seconds == 10
    assert restarted.session_started_at == NOW + timedelta(seconds=20)


def test_stale_active_snapshot_freezes_at_last_provider_observation() -> None:
    deployment = _deployment()
    deployment.billing_session_started_at = NOW
    deployment.billing_active_instance_id = "instance-a"
    deployment.billing_active_started_at = NOW
    deployment.billing_observed_at = NOW + timedelta(seconds=5)

    snapshot = _snapshot_for_deployment(deployment, now=NOW + timedelta(minutes=2))

    assert snapshot.state == GpuBillingState.STALE
    assert snapshot.elapsed_seconds == 5
    assert snapshot.running_instances == 1
    assert snapshot.estimated is True
    assert snapshot.fresh_for_seconds == 0


def test_freshness_horizon_and_multi_running_drift_are_fail_closed() -> None:
    deployment = _deployment()
    running_a = _instance(
        "instance-a",
        SaladContainerGroupInstanceState.RUNNING,
        NOW,
    )
    running_b = _instance(
        "instance-b",
        SaladContainerGroupInstanceState.RUNNING,
        NOW + timedelta(seconds=1),
    )
    _apply_billing_observation(
        deployment,
        instances=(running_a, running_b),
        running=(running_a, running_b),
        observed_at=NOW + timedelta(seconds=5),
        end_session=False,
    )

    drift = _snapshot_for_deployment(deployment, now=NOW + timedelta(seconds=5))
    assert drift.state == GpuBillingState.STALE
    assert drift.running_instances == 1
    assert drift.elapsed_seconds == 5
    assert drift.estimated is True
    assert drift.fresh_for_seconds == 0

    deployment.billing_observation_stale = False
    at_89_seconds = _snapshot_for_deployment(
        deployment,
        now=NOW + timedelta(seconds=94),
    )
    assert at_89_seconds.state == GpuBillingState.CHARGING
    assert at_89_seconds.fresh_for_seconds == 1
    at_90_seconds = _snapshot_for_deployment(
        deployment,
        now=NOW + timedelta(seconds=95),
    )
    assert at_90_seconds.state == GpuBillingState.STALE
    assert at_90_seconds.fresh_for_seconds == 0


def test_restart_capable_empty_and_ended_sessions_age_to_stale() -> None:
    deployment = _deployment()
    deployment.provider_queue_id = "queue-id"
    deployment.provider_container_group_id = "group-id"
    deployment.state = SaladDeploymentState.ACTIVE
    deployment.billing_observed_at = NOW

    before_first_start = _snapshot_for_deployment(
        deployment,
        now=NOW + timedelta(seconds=89),
    )
    assert before_first_start.state == GpuBillingState.NOT_STARTED
    assert (
        _snapshot_for_deployment(
            deployment,
            now=NOW + timedelta(seconds=90),
        ).state
        == GpuBillingState.STALE
    )

    deployment.billing_session_started_at = NOW - timedelta(minutes=1)
    deployment.billing_session_ended_at = NOW - timedelta(seconds=30)
    deployment.billing_accumulated_microseconds = 30_000_000
    still_fresh_ended = _snapshot_for_deployment(
        deployment,
        now=NOW + timedelta(seconds=89),
    )
    assert still_fresh_ended.state == GpuBillingState.ENDED
    aged_ended = _snapshot_for_deployment(
        deployment,
        now=NOW + timedelta(seconds=90),
    )
    assert aged_ended.state == GpuBillingState.STALE
    assert aged_ended.session_started_at is None

    deployment.state = SaladDeploymentState.STOPPED
    confirmed_stopped = _snapshot_for_deployment(
        deployment,
        now=NOW + timedelta(hours=1),
    )
    assert confirmed_stopped.state == GpuBillingState.ENDED


class _InstanceClient:
    def __init__(self, page: SaladContainerGroupInstancePage | Exception) -> None:
        self.page = page

    async def list_container_group_instances(
        self,
        container_group_name: str,
    ) -> SaladContainerGroupInstancePage:
        assert container_group_name == "worker-v1"
        if isinstance(self.page, Exception):
            raise self.page
        return self.page


@pytest.mark.asyncio
async def test_first_observation_and_restart_failure_surface_stale(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'stale.db').as_posix()}")
    await database.create_schema()
    try:
        async with database.sessions() as session:
            deployment = _deployment()
            deployment.observed_replicas = 1
            deployment.ready_replicas = 1
            session.add(deployment)
            await session.flush()
            await _refresh_billing_observation(
                session,
                deployment,
                _InstanceClient(SaladTransportError("unavailable")),  # type: ignore[arg-type]
                _group(),
                observed_at=NOW,
                end_session=False,
                billing_observation_clock=lambda: NOW,
            )
            first = await load_shared_gpu_billing_snapshot(session, now=NOW)
            assert first.state == GpuBillingState.STALE
            assert first.session_started_at is None

            await _refresh_billing_observation(
                session,
                deployment,
                _InstanceClient(SaladTransportError("still unavailable")),  # type: ignore[arg-type]
                _group(replicas=0, running=0),
                observed_at=NOW + timedelta(seconds=5),
                end_session=False,
                billing_observation_clock=lambda: NOW + timedelta(seconds=5),
            )
            still_unresolved = await load_shared_gpu_billing_snapshot(
                session,
                now=NOW + timedelta(seconds=5),
            )
            assert still_unresolved.state == GpuBillingState.STALE
            assert deployment.billing_observation_stale is True

            deployment.billing_session_started_at = NOW - timedelta(minutes=2)
            deployment.billing_session_ended_at = NOW - timedelta(minutes=1)
            deployment.billing_accumulated_microseconds = 60_000_000
            deployment.billing_observation_stale = False
            await _refresh_billing_observation(
                session,
                deployment,
                _InstanceClient(SaladTransportError("unavailable")),  # type: ignore[arg-type]
                _group(),
                observed_at=NOW + timedelta(seconds=10),
                end_session=False,
                billing_observation_clock=lambda: NOW + timedelta(seconds=10),
            )
            restart_unknown = await load_shared_gpu_billing_snapshot(
                session,
                now=NOW + timedelta(seconds=10),
            )
            assert restart_unknown.state == GpuBillingState.STALE
            assert restart_unknown.session_started_at is None

            running = _instance(
                "instance-restarted",
                SaladContainerGroupInstanceState.RUNNING,
                NOW + timedelta(seconds=20),
            )
            await _refresh_billing_observation(
                session,
                deployment,
                _InstanceClient(SaladContainerGroupInstancePage(instances=(running,))),  # type: ignore[arg-type]
                _group(),
                observed_at=NOW + timedelta(seconds=25),
                end_session=False,
                billing_observation_clock=lambda: NOW + timedelta(seconds=25),
            )
            restarted = await load_shared_gpu_billing_snapshot(
                session,
                now=NOW + timedelta(seconds=30),
            )
            assert restarted.state == GpuBillingState.CHARGING
            assert restarted.session_started_at == NOW + timedelta(seconds=20)
            assert restarted.elapsed_seconds == 10
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_shared_snapshot_prefers_unresolved_old_then_clean_current(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'selection.db').as_posix()}")
    await database.create_schema()
    try:
        async with database.sessions() as session:
            old = _deployment(1, is_current=False)
            old.provider_queue_id = "queue-old"
            old.provider_container_group_id = "group-old"
            old.state = SaladDeploymentState.DRAINING
            old.desired_state = DesiredDeploymentState.STOPPED
            old.billing_session_started_at = NOW - timedelta(seconds=30)
            old.billing_active_instance_id = "instance-old"
            old.billing_active_started_at = NOW - timedelta(seconds=30)
            old.billing_observed_at = NOW - timedelta(seconds=2)
            current = _deployment(2, is_current=True)
            session.add_all((old, current))
            await session.flush()

            draining = await load_shared_gpu_billing_snapshot(session, now=NOW)
            assert draining.state == GpuBillingState.STOPPING
            assert draining.session_started_at == NOW - timedelta(seconds=30)

            old.billing_active_instance_id = None
            old.billing_active_started_at = None
            old.billing_accumulated_microseconds = 30_000_000
            old.billing_session_ended_at = NOW
            old.state = SaladDeploymentState.STOPPED
            await session.flush()

            clean_current = await load_shared_gpu_billing_snapshot(session, now=NOW)
            assert clean_current.state == GpuBillingState.NOT_STARTED
            assert clean_current.elapsed_seconds == 0
    finally:
        await database.dispose()


def test_public_payload_has_stable_timer_contract() -> None:
    deployment = _deployment()
    deployment.billing_session_started_at = NOW
    deployment.billing_active_instance_id = "instance-a"
    deployment.billing_active_started_at = NOW
    deployment.billing_observed_at = NOW + timedelta(seconds=10)
    snapshot = _snapshot_for_deployment(deployment, now=NOW + timedelta(seconds=20))

    assert gpu_billing_payload(snapshot) == {
        "state": "charging",
        "elapsed_seconds": 20,
        "running_instances": 1,
        "session_started_at": "2026-08-09T12:00:00+00:00",
        "observed_at": "2026-08-09T12:00:10+00:00",
        "estimated": False,
        "fresh_for_seconds": 80,
    }


def _complete_new_set_status(
    gpu_billing: GpuBillingSnapshot,
    *,
    poll_after_ms: int,
) -> NewSetStatus:
    return NewSetStatus(
        release_id=uuid4(),
        project_slug="main",
        release_slug="complete-set",
        title="Complete set",
        phase=ReleasePhase.REVIEWING,
        health=ResourceHealth.HEALTHY,
        desired_accepted_count=1,
        specification_sha256="a" * 64,
        total_jobs=1,
        expected_outputs=1,
        jobs_by_state=(),
        stage=GenerationProgressStageView(
            key=GenerationProgressStage.REVIEW,
            step=5,
            step_count=5,
            label="Ready for review",
            detail="Complete",
        ),
        images=GenerationImageProgress(generated=1, expected=1, percent=100.0),
        jobs=GenerationJobProgress(
            completed=1,
            total=1,
            active=0,
            failed=0,
            states={"succeeded": 1},
        ),
        scoring=None,
        error=None,
        ready_for_review=True,
        next_url="/dashboard/releases/complete-set",
        poll_after_ms=poll_after_ms,
        gpu_billing=gpu_billing,
    )


def test_terminal_progress_polls_until_shared_billing_ends() -> None:
    paused = GpuBillingSnapshot(
        state=GpuBillingState.PAUSED,
        elapsed_seconds=30,
        running_instances=0,
        session_started_at=NOW,
        observed_at=NOW + timedelta(seconds=30),
        estimated=False,
        fresh_for_seconds=90,
    )
    ended = GpuBillingSnapshot(
        state=GpuBillingState.ENDED,
        elapsed_seconds=30,
        running_instances=0,
        session_started_at=NOW,
        observed_at=NOW + timedelta(seconds=40),
        estimated=False,
        fresh_for_seconds=0,
        session_ended_at=NOW + timedelta(seconds=40),
    )
    stale = GpuBillingSnapshot(
        state=GpuBillingState.STALE,
        elapsed_seconds=30,
        running_instances=1,
        session_started_at=NOW,
        observed_at=NOW + timedelta(seconds=30),
        estimated=True,
        fresh_for_seconds=0,
    )
    stopping = GpuBillingSnapshot(
        state=GpuBillingState.STOPPING,
        elapsed_seconds=30,
        running_instances=1,
        session_started_at=NOW,
        observed_at=NOW + timedelta(seconds=30),
        estimated=False,
        fresh_for_seconds=90,
    )
    for unresolved in (paused, stale, stopping):
        assert (
            gpu_billing_poll_after_ms(
                unresolved,
                settled_poll_after_ms=0,
            )
            == 3_000
        )
    assert gpu_billing_poll_after_ms(ended, settled_poll_after_ms=0) == 0

    paused_status = _complete_new_set_status(paused, poll_after_ms=3_000)
    paused_variant = ExperimentVariantStatus(
        index=0,
        label="Complete",
        release_id=paused_status.release_id,
        release_slug=paused_status.release_slug,
        status=paused_status,
    )
    paused_experiment = experiment_progress_payload(
        ExperimentStatus(
            group_slug="experiment-123456789abc",
            title="Complete comparison",
            variants=(paused_variant,),
            gpu_billing=paused,
        )
    )
    assert paused_experiment["schema_version"] == 2
    assert paused_experiment["poll_after_ms"] == 3_000
    assert paused_experiment["gpu_billing"] == gpu_billing_payload(paused)
    variants = paused_experiment["variants"]
    assert isinstance(variants, list)
    assert "gpu_billing" not in variants[0]

    ended_status = _complete_new_set_status(ended, poll_after_ms=0)
    ended_variant = ExperimentVariantStatus(
        index=0,
        label="Complete",
        release_id=ended_status.release_id,
        release_slug=ended_status.release_slug,
        status=ended_status,
    )
    ended_experiment = experiment_progress_payload(
        ExperimentStatus(
            group_slug="experiment-123456789abc",
            title="Complete comparison",
            variants=(ended_variant,),
            gpu_billing=ended,
        )
    )
    assert ended_experiment["poll_after_ms"] == 0
    ended_new_set = new_set_progress_payload(ended_status)
    assert ended_new_set["schema_version"] == 2
    assert ended_new_set["poll_after_ms"] == 0
    assert ended_new_set["gpu_billing"] == gpu_billing_payload(ended)
