import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from gen_automation.app import create_app
from gen_automation.config import Environment, Settings
from gen_automation.controller.runtime import (
    ControllerRuntime,
    ControllerRuntimeSnapshot,
    LoopSpec,
    RuntimeStatus,
    build_controller_runtime,
)
from gen_automation.db.session import Database
from gen_automation.storage.memory import MemoryObjectStore


@pytest.mark.asyncio
async def test_runtime_contains_loop_failure_and_keeps_task_alive() -> None:
    calls = 0
    recovered = asyncio.Event()

    async def cycle() -> bool:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("synthetic failure")
        recovered.set()
        return False

    runtime = ControllerRuntime(
        instance_id="controller-test-failure",
        loops=(
            LoopSpec(
                name="test-loop",
                cycle=cycle,
                idle_interval_seconds=0.01,
                timeout_seconds=0.1,
            ),
        ),
        error_backoff_max_seconds=0.02,
        shutdown_grace_seconds=0.1,
        jitter=lambda: 0.5,
    )
    await runtime.start()
    await asyncio.wait_for(recovered.wait(), timeout=1)

    snapshot = runtime.snapshot()
    assert snapshot.ready is True
    assert snapshot.loops[0].task_alive is True
    assert snapshot.loops[0].iterations >= 2
    assert snapshot.loops[0].last_failure_at is not None

    await runtime.stop()
    assert runtime.snapshot().status == RuntimeStatus.STOPPED


@pytest.mark.asyncio
async def test_optional_initial_success_does_not_block_startup_readiness() -> None:
    ordinary_completed = asyncio.Event()
    cold_provider_started = asyncio.Event()
    release_provider = asyncio.Event()

    async def ordinary_cycle() -> bool:
        ordinary_completed.set()
        return False

    async def cold_provider_cycle() -> bool:
        cold_provider_started.set()
        await release_provider.wait()
        return False

    runtime = ControllerRuntime(
        instance_id="controller-test-cold-provider-readiness",
        loops=(
            LoopSpec(
                name="ordinary-loop",
                cycle=ordinary_cycle,
                idle_interval_seconds=1,
                timeout_seconds=2,
            ),
            LoopSpec(
                name="scale-to-zero-provider-loop",
                cycle=cold_provider_cycle,
                idle_interval_seconds=1,
                timeout_seconds=2,
                requires_initial_success_for_readiness=False,
            ),
        ),
        shutdown_grace_seconds=0.1,
        shutdown_cancel_seconds=0.1,
        jitter=lambda: 0.5,
    )
    await runtime.start()
    await asyncio.wait_for(ordinary_completed.wait(), timeout=1)
    await asyncio.wait_for(cold_provider_started.wait(), timeout=1)
    for _ in range(10):
        snapshot = runtime.snapshot()
        if snapshot.loops[0].last_success_at is not None:
            break
        await asyncio.sleep(0)

    snapshot = runtime.snapshot()
    ordinary, cold_provider = snapshot.loops
    assert ordinary.last_success_at is not None
    assert cold_provider.task_alive is True
    assert cold_provider.last_success_at is None
    assert snapshot.ready is True
    assert snapshot.status == RuntimeStatus.HEALTHY

    release_provider.set()
    await runtime.stop()


@pytest.mark.asyncio
async def test_optional_initial_success_still_fails_readiness_after_repeated_errors() -> None:
    ordinary_completed = asyncio.Event()
    failure_threshold_reached = asyncio.Event()
    release_failure = asyncio.Event()
    failures = 0

    async def ordinary_cycle() -> bool:
        ordinary_completed.set()
        return False

    async def failing_provider_cycle() -> bool:
        nonlocal failures
        failures += 1
        if failures == 3:
            failure_threshold_reached.set()
            await release_failure.wait()
        raise RuntimeError("provider unavailable")

    runtime = ControllerRuntime(
        instance_id="controller-test-cold-provider-failures",
        loops=(
            LoopSpec(
                name="ordinary-loop",
                cycle=ordinary_cycle,
                idle_interval_seconds=0.001,
                timeout_seconds=0.1,
            ),
            LoopSpec(
                name="scale-to-zero-provider-loop",
                cycle=failing_provider_cycle,
                idle_interval_seconds=0.001,
                timeout_seconds=0.1,
                requires_initial_success_for_readiness=False,
            ),
        ),
        error_backoff_max_seconds=0.001,
        shutdown_grace_seconds=0.01,
        shutdown_cancel_seconds=0.02,
        readiness_failure_threshold=2,
        liveness_failure_threshold=4,
        stale_after_seconds=1,
        jitter=lambda: 0.5,
    )
    await runtime.start()
    await asyncio.wait_for(ordinary_completed.wait(), timeout=1)
    await asyncio.wait_for(failure_threshold_reached.wait(), timeout=1)

    snapshot = runtime.snapshot()
    provider = snapshot.loops[1]
    assert provider.last_success_at is None
    assert provider.consecutive_failures == 2
    assert snapshot.ready is False
    assert snapshot.live is True
    assert snapshot.status == RuntimeStatus.DEGRADED

    release_failure.set()
    await runtime.stop()


@pytest.mark.asyncio
async def test_optional_initial_success_still_fails_readiness_when_stale() -> None:
    ordinary_completed = asyncio.Event()
    provider_timeout_started = asyncio.Event()
    release_provider = asyncio.Event()

    async def ordinary_cycle() -> bool:
        ordinary_completed.set()
        return False

    async def stuck_provider_cycle() -> bool:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            provider_timeout_started.set()
            while not release_provider.is_set():
                try:
                    await release_provider.wait()
                except asyncio.CancelledError:
                    continue
        return False

    runtime = ControllerRuntime(
        instance_id="controller-test-cold-provider-stale",
        loops=(
            LoopSpec(
                name="ordinary-loop",
                cycle=ordinary_cycle,
                idle_interval_seconds=0.001,
                timeout_seconds=0.01,
            ),
            LoopSpec(
                name="scale-to-zero-provider-loop",
                cycle=stuck_provider_cycle,
                idle_interval_seconds=0.001,
                timeout_seconds=0.01,
                requires_initial_success_for_readiness=False,
            ),
        ),
        error_backoff_max_seconds=0.001,
        shutdown_grace_seconds=0.01,
        shutdown_cancel_seconds=0.01,
        stale_after_seconds=0.03,
        jitter=lambda: 0.5,
    )
    await runtime.start()
    await asyncio.wait_for(ordinary_completed.wait(), timeout=1)
    await asyncio.wait_for(provider_timeout_started.wait(), timeout=1)

    await asyncio.sleep(0.04)
    snapshot = runtime.snapshot()
    assert snapshot.loops[1].stale is True
    assert snapshot.ready is False
    assert snapshot.live is False

    await runtime.stop()
    release_provider.set()
    await asyncio.wait_for(
        asyncio.gather(*runtime._tasks.values(), return_exceptions=True),
        timeout=1,
    )


def test_noncritical_background_loops_relax_initial_readiness_gate(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'semantic-runtime.db').as_posix()}")
    settings = Settings(
        environment=Environment.TEST,
        background_runtime_enabled=True,
        storage_enabled=True,
        storage_bucket="semantic-runtime",
        quality_scoring_enabled=True,
        semantic_anatomy_enabled=True,
        semantic_anatomy_max_assessments_per_profile=400,
        semantic_anatomy_model_revision="60595ebc30ec8e3b1d3b9e65d4943ca011c0006a",
        semantic_anatomy_endpoint_url="http://127.0.0.1:8091/v1/anatomy/assess",
    )
    runtime = build_controller_runtime(
        settings=settings,
        sessions=database.sessions,
        salad_client=None,
        object_store=MemoryObjectStore(bucket="semantic-runtime"),
    )

    specs = {spec.name: spec for spec in runtime._loop_specs}
    assert specs["semantic-anatomy-qc"].requires_initial_success_for_readiness is False
    assert specs["finished-set-archives"].requires_initial_success_for_readiness is False
    assert all(
        spec.requires_initial_success_for_readiness
        for name, spec in specs.items()
        if name not in {"semantic-anatomy-qc", "finished-set-archives"}
    )


@pytest.mark.asyncio
async def test_runtime_bounds_hung_cycle_and_reports_degraded_health() -> None:
    calls = 0
    restarted = asyncio.Event()
    never = asyncio.Event()

    async def cycle() -> bool:
        nonlocal calls
        calls += 1
        if calls >= 2:
            restarted.set()
        await never.wait()
        return False

    runtime = ControllerRuntime(
        instance_id="controller-test-timeout",
        loops=(
            LoopSpec(
                name="hung-loop",
                cycle=cycle,
                idle_interval_seconds=0.005,
                timeout_seconds=0.02,
            ),
        ),
        error_backoff_max_seconds=0.01,
        shutdown_grace_seconds=0.02,
        jitter=lambda: 0.5,
    )
    await runtime.start()
    await asyncio.wait_for(restarted.wait(), timeout=1)

    snapshot = runtime.snapshot()
    assert snapshot.ready is False
    assert snapshot.status == RuntimeStatus.DEGRADED
    assert snapshot.loops[0].task_alive is True
    assert snapshot.loops[0].consecutive_failures >= 1
    assert snapshot.loops[0].last_error_type == "TimeoutError"

    await runtime.stop()


@pytest.mark.asyncio
async def test_runtime_gracefully_finishes_inflight_cycle_before_cancelling() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    cancelled = False

    async def cycle() -> bool:
        nonlocal cancelled
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled = True
            raise
        return False

    runtime = ControllerRuntime(
        instance_id="controller-test-shutdown",
        loops=(
            LoopSpec(
                name="shutdown-loop",
                cycle=cycle,
                idle_interval_seconds=1,
                timeout_seconds=1,
            ),
        ),
        shutdown_grace_seconds=0.5,
        jitter=lambda: 0.5,
    )
    await runtime.start()
    await asyncio.wait_for(started.wait(), timeout=1)
    stop_task = asyncio.create_task(runtime.stop())
    await asyncio.sleep(0)
    release.set()
    await asyncio.wait_for(stop_task, timeout=1)

    assert cancelled is False
    assert runtime.snapshot().ready is False


@pytest.mark.asyncio
async def test_runtime_bounds_cancellation_cleanup_that_suppresses_cancellation() -> None:
    started = asyncio.Event()
    release_cleanup = asyncio.Event()

    async def cycle() -> bool:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            while not release_cleanup.is_set():
                try:
                    await release_cleanup.wait()
                except asyncio.CancelledError:
                    continue
        return False

    runtime = ControllerRuntime(
        instance_id="controller-test-bounded-cancellation",
        loops=(
            LoopSpec(
                name="bounded-cancellation-loop",
                cycle=cycle,
                idle_interval_seconds=1,
                timeout_seconds=1,
            ),
        ),
        error_backoff_max_seconds=0.1,
        shutdown_grace_seconds=0.01,
        shutdown_cancel_seconds=0.02,
        stale_after_seconds=3,
        jitter=lambda: 0.5,
    )
    await runtime.start()
    await asyncio.wait_for(started.wait(), timeout=1)

    await asyncio.wait_for(runtime.stop(), timeout=0.2)

    assert runtime.snapshot().status == RuntimeStatus.STOPPED
    release_cleanup.set()
    tasks = tuple(runtime._tasks.values())
    await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=1)


@pytest.mark.asyncio
async def test_runtime_finishes_bounded_loop_cleanup_when_stop_caller_is_cancelled() -> None:
    started = asyncio.Event()
    cleanup_finished = asyncio.Event()

    async def cycle() -> bool:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_finished.set()

    runtime = ControllerRuntime(
        instance_id="controller-test-cancelled-stop",
        loops=(
            LoopSpec(
                name="cancelled-stop-loop",
                cycle=cycle,
                idle_interval_seconds=1,
                timeout_seconds=1,
            ),
        ),
        error_backoff_max_seconds=0.1,
        shutdown_grace_seconds=0.01,
        shutdown_cancel_seconds=0.05,
        stale_after_seconds=3,
        jitter=lambda: 0.5,
    )
    await runtime.start()
    await asyncio.wait_for(started.wait(), timeout=1)
    stop_task = asyncio.create_task(runtime.stop())
    await asyncio.sleep(0)
    stop_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(stop_task, timeout=0.2)

    assert cleanup_finished.is_set()
    assert runtime.snapshot().status == RuntimeStatus.STOPPED


@pytest.mark.asyncio
async def test_runtime_failure_thresholds_fail_readiness_then_liveness() -> None:
    readiness_gate_started = asyncio.Event()
    readiness_gate = asyncio.Event()
    liveness_gate_started = asyncio.Event()
    liveness_gate = asyncio.Event()
    calls = 0

    async def cycle() -> bool:
        nonlocal calls
        calls += 1
        if calls == 1:
            return False
        if calls == 4:
            readiness_gate_started.set()
            await readiness_gate.wait()
        if calls == 6:
            liveness_gate_started.set()
            await liveness_gate.wait()
        raise RuntimeError("persistent failure")

    runtime = ControllerRuntime(
        instance_id="controller-test-failure-thresholds",
        loops=(
            LoopSpec(
                name="persistent-failure-loop",
                cycle=cycle,
                idle_interval_seconds=0.001,
                timeout_seconds=0.1,
            ),
        ),
        error_backoff_max_seconds=0.001,
        shutdown_grace_seconds=0.01,
        shutdown_cancel_seconds=0.02,
        readiness_failure_threshold=2,
        liveness_failure_threshold=4,
        stale_after_seconds=1,
        jitter=lambda: 0.5,
    )
    await runtime.start()
    await asyncio.wait_for(readiness_gate_started.wait(), timeout=1)

    readiness_snapshot = runtime.snapshot()
    assert readiness_snapshot.ready is False
    assert readiness_snapshot.live is True

    readiness_gate.set()
    await asyncio.wait_for(liveness_gate_started.wait(), timeout=1)

    liveness_snapshot = runtime.snapshot()
    assert liveness_snapshot.ready is False
    assert liveness_snapshot.live is False

    liveness_gate.set()
    await runtime.stop()


@pytest.mark.asyncio
async def test_runtime_fails_liveness_for_stale_and_dead_loops() -> None:
    cancellation_suppressed = asyncio.Event()
    release = asyncio.Event()

    async def stale_cycle() -> bool:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancellation_suppressed.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    continue
        return False

    stale_runtime = ControllerRuntime(
        instance_id="controller-test-stale-loop",
        loops=(
            LoopSpec(
                name="stale-loop",
                cycle=stale_cycle,
                idle_interval_seconds=0.001,
                timeout_seconds=0.01,
            ),
        ),
        error_backoff_max_seconds=0.001,
        shutdown_grace_seconds=0.01,
        shutdown_cancel_seconds=0.01,
        stale_after_seconds=0.03,
        jitter=lambda: 0.5,
    )
    await stale_runtime.start()
    await asyncio.wait_for(cancellation_suppressed.wait(), timeout=1)
    await asyncio.sleep(0.04)

    stale_snapshot = stale_runtime.snapshot()
    assert stale_snapshot.loops[0].stale is True
    assert stale_snapshot.ready is False
    assert stale_snapshot.live is False

    await stale_runtime.stop()
    release.set()
    await asyncio.wait_for(
        asyncio.gather(*stale_runtime._tasks.values(), return_exceptions=True),
        timeout=1,
    )

    completed = asyncio.Event()

    async def healthy_cycle() -> bool:
        completed.set()
        return False

    dead_runtime = ControllerRuntime(
        instance_id="controller-test-dead-loop",
        loops=(
            LoopSpec(
                name="dead-loop",
                cycle=healthy_cycle,
                idle_interval_seconds=0.01,
                timeout_seconds=0.1,
            ),
        ),
        error_backoff_max_seconds=0.1,
        shutdown_grace_seconds=0.01,
        shutdown_cancel_seconds=0.01,
        stale_after_seconds=1,
        jitter=lambda: 0.5,
    )
    await dead_runtime.start()
    await asyncio.wait_for(completed.wait(), timeout=1)
    task = dead_runtime._tasks["dead-loop"]
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    dead_snapshot = dead_runtime.snapshot()
    assert dead_snapshot.ready is False
    assert dead_snapshot.live is False
    assert dead_snapshot.failed_loops == ("dead-loop",)
    await dead_runtime.stop()


class _FakeRuntime:
    def __init__(self, *, ready: bool = True, live: bool = True) -> None:
        self.ready = ready
        self.live = live
        self.start_calls = 0
        self.stop_calls = 0

    async def start(self) -> None:
        self.start_calls += 1

    async def stop(self) -> None:
        self.stop_calls += 1

    def snapshot(self) -> ControllerRuntimeSnapshot:
        return ControllerRuntimeSnapshot(
            instance_id="controller-lifecycle-test",
            status=RuntimeStatus.HEALTHY if self.ready else RuntimeStatus.DEGRADED,
            ready=self.ready,
            started_at=None,
            loops=(),
            live=self.live,
        )


def test_app_lifespan_starts_runtime_reports_health_and_stops_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _FakeRuntime()
    build_calls = 0

    def fake_build_controller_runtime(**_kwargs: object) -> _FakeRuntime:
        nonlocal build_calls
        build_calls += 1
        return runtime

    monkeypatch.setattr(
        "gen_automation.app.build_controller_runtime",
        fake_build_controller_runtime,
    )
    settings = Settings(
        environment=Environment.TEST,
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'runtime.db').as_posix()}",
        auto_create_schema=True,
        background_runtime_enabled=True,
        session_secret="test-session-secret-with-more-than-32-characters",  # noqa: S106
    )

    with TestClient(create_app(settings)) as client:
        live = client.get("/api/v1/health/live")
        ready = client.get("/api/v1/health/ready")
        assert live.status_code == 200
        assert live.json()["controller_runtime"] == "healthy"
        assert ready.status_code == 200
        assert runtime.start_calls == 1
        assert build_calls == 1

    assert runtime.stop_calls == 1


def test_ready_health_allows_alive_uninitialized_optional_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def cold_provider_cycle() -> bool:
        await asyncio.Event().wait()
        return False

    runtime = ControllerRuntime(
        instance_id="controller-health-cold-provider",
        loops=(
            LoopSpec(
                name="semantic-anatomy-qc",
                cycle=cold_provider_cycle,
                idle_interval_seconds=1,
                timeout_seconds=60,
                requires_initial_success_for_readiness=False,
            ),
        ),
        shutdown_grace_seconds=0.01,
        shutdown_cancel_seconds=0.05,
        jitter=lambda: 0.5,
    )
    monkeypatch.setattr(
        "gen_automation.app.build_controller_runtime",
        lambda **_kwargs: runtime,
    )
    settings = Settings(
        environment=Environment.TEST,
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'cold-provider-ready.db').as_posix()}",
        auto_create_schema=True,
        background_runtime_enabled=True,
        session_secret="test-session-secret-with-more-than-32-characters",  # noqa: S106
    )

    with TestClient(create_app(settings)) as client:
        response = client.get("/api/v1/health/ready")
        assert response.status_code == 200
        assert response.json()["controller_runtime"] == "healthy"
        loop = runtime.snapshot().loops[0]
        assert loop.task_alive is True
        assert loop.last_success_at is None


def test_ready_health_fails_when_enabled_runtime_is_not_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _FakeRuntime(ready=False)
    monkeypatch.setattr(
        "gen_automation.app.build_controller_runtime",
        lambda **_kwargs: runtime,
    )
    settings = Settings(
        environment=Environment.TEST,
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'unready.db').as_posix()}",
        auto_create_schema=True,
        background_runtime_enabled=True,
        session_secret="test-session-secret-with-more-than-32-characters",  # noqa: S106
    )

    with TestClient(create_app(settings)) as client:
        assert client.get("/api/v1/health/live").status_code == 200
        response = client.get("/api/v1/health/ready")
        assert response.status_code == 503
        assert response.json()["detail"] == "background controller runtime is not ready"


def test_live_health_fails_when_enabled_runtime_is_not_live(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _FakeRuntime(ready=False, live=False)
    monkeypatch.setattr(
        "gen_automation.app.build_controller_runtime",
        lambda **_kwargs: runtime,
    )
    settings = Settings(
        environment=Environment.TEST,
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'not-live.db').as_posix()}",
        auto_create_schema=True,
        background_runtime_enabled=True,
        session_secret="test-session-secret-with-more-than-32-characters",  # noqa: S106
    )

    with TestClient(create_app(settings)) as client:
        response = client.get("/api/v1/health/live")
        assert response.status_code == 503
        assert response.json()["detail"] == "background controller runtime is not live"


def test_background_runtime_is_opt_in_outside_production() -> None:
    assert Settings(environment=Environment.LOCAL).background_runtime_enabled is False
    assert Settings(environment=Environment.TEST).background_runtime_enabled is False


@pytest.mark.parametrize("environment", [Environment.STAGING, Environment.PRODUCTION])
def test_protected_salad_requires_background_runtime(environment: Environment) -> None:
    with pytest.raises(ValidationError, match="requires the background runtime"):
        Settings(
            environment=environment,
            database_url="postgresql+psycopg://user:pass@db/example",
            public_base_url="https://studio.example.com",
            session_secret="a-secure-random-session-secret-that-is-long",  # noqa: S106
            storage_enabled=True,
            storage_bucket="private-assets",
            salad_enabled=True,
            salad_api_key="test-key",
            salad_organization="organization",
            salad_project="project",
            salad_queue_name="generation-queue",
            salad_container_group_name="generation-workers",
            salad_webhook_secret="whsec_test",  # noqa: S106
            salad_worker_image=f"registry.example/worker@sha256:{'a' * 64}",
        )
