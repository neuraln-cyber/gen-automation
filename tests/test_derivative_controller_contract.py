from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from gen_automation.config import Environment, Settings
from gen_automation.controller import runtime as runtime_module
from gen_automation.controller.runtime import (
    ControllerWorkloads,
    build_controller_runtime,
)
from gen_automation.db.session import Database
from gen_automation.services.derivative_isolation import DerivativeIsolationPolicy
from gen_automation.services.derivative_runtime import DerivativeCycleResult
from gen_automation.storage.memory import MemoryObjectStore


def _enabled_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "environment": Environment.TEST,
        "background_runtime_enabled": True,
        "derivative_rendering_enabled": True,
        "storage_enabled": True,
        "storage_bucket": "derivatives",
    }
    values.update(overrides)
    return Settings(**values)


def test_derivative_rendering_requires_storage_and_background_runtime() -> None:
    with pytest.raises(ValidationError, match="private object storage"):
        Settings(derivative_rendering_enabled=True)
    with pytest.raises(ValidationError, match="background runtime"):
        Settings(
            derivative_rendering_enabled=True,
            storage_enabled=True,
            storage_bucket="derivatives",
        )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        (
            {
                "background_derivative_timeout_seconds": 134,
                "background_derivative_render_timeout_seconds": 120,
            },
            "cover isolated rendering",
        ),
        (
            {
                "background_derivative_timeout_seconds": 150,
                "background_derivative_lease_seconds": 150,
            },
            "lease must exceed",
        ),
        (
            {
                "background_derivative_retry_base_seconds": 31,
                "background_derivative_retry_max_seconds": 30,
            },
            "retry maximum",
        ),
    ],
)
def test_derivative_timing_contract_is_validated(
    overrides: dict[str, int],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        _enabled_settings(**overrides)


def test_derivative_loop_participates_in_staleness_budget() -> None:
    with pytest.raises(
        ValidationError,
        match="longest cycle timeout plus maximum jittered delay",
    ):
        _enabled_settings(
            background_collection_timeout_seconds=5,
            background_derivative_timeout_seconds=150,
            background_error_backoff_max_seconds=60,
            background_loop_stale_after_seconds=227,
        )


def test_controller_registers_bounded_derivative_loop(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'controller.db').as_posix()}")
    settings = _enabled_settings()
    runtime = build_controller_runtime(
        settings=settings,
        sessions=database.sessions,
        salad_client=None,
        object_store=MemoryObjectStore(bucket="derivatives"),
    )

    specs = {spec.name: spec for spec in runtime._loop_specs}

    assert "derivative-rendering" in specs
    assert specs["derivative-rendering"].timeout_seconds == (
        settings.background_derivative_timeout_seconds + 5
    )
    assert (
        specs["derivative-rendering"].idle_interval_seconds
        == settings.background_poll_interval_seconds
    )


def test_controller_omits_derivative_loop_when_disabled(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'controller-disabled.db').as_posix()}")
    settings = Settings(
        environment=Environment.TEST,
        background_runtime_enabled=True,
        storage_enabled=True,
        storage_bucket="derivatives",
    )
    runtime = build_controller_runtime(
        settings=settings,
        sessions=database.sessions,
        salad_client=None,
        object_store=MemoryObjectStore(bucket="derivatives"),
    )

    assert "derivative-rendering" not in {spec.name for spec in runtime._loop_specs}


@pytest.mark.asyncio
async def test_derivative_workload_forwards_bounded_policy_and_retry_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'controller-once.db').as_posix()}")
    settings = _enabled_settings(
        background_derivative_render_timeout_seconds=91,
        background_derivative_timeout_seconds=120,
        background_derivative_memory_limit_bytes=640 * 1024 * 1024,
        background_derivative_lease_seconds=240,
        background_derivative_retry_base_seconds=17,
        background_derivative_retry_max_seconds=301,
    )
    store = MemoryObjectStore(bucket="derivatives")
    captured: dict[str, object] = {}

    async def fake_cycle(*args: object, **kwargs: object) -> DerivativeCycleResult:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return DerivativeCycleResult(claimed_job=True)

    monkeypatch.setattr(runtime_module, "run_derivative_cycle", fake_cycle)
    workloads = ControllerWorkloads(
        settings=settings,
        sessions=database.sessions,
        instance_id="controller-test",
        salad_client=None,
        object_store=store,
    )

    did_work = await workloads.derivative_once()

    assert did_work is True
    assert captured["args"] == (database.sessions, store)
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["worker_id"] == "controller-test:derivative"
    assert kwargs["lease_seconds"] == 240
    assert kwargs["retry_base_seconds"] == 17
    assert kwargs["retry_max_seconds"] == 301
    policy = kwargs["isolation_policy"]
    assert isinstance(policy, DerivativeIsolationPolicy)
    assert policy.wall_timeout_seconds == 91
    assert policy.memory_limit_bytes == 640 * 1024 * 1024
