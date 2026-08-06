from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from gen_automation.config import Environment, Settings
from gen_automation.controller import runtime as runtime_module
from gen_automation.controller.runtime import ControllerWorkloads, build_controller_runtime
from gen_automation.db.session import Database
from gen_automation.storage.memory import MemoryObjectStore


def _archive_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "environment": Environment.TEST,
        "background_runtime_enabled": True,
        "storage_enabled": True,
        "storage_bucket": "finished-sets",
    }
    values.update(overrides)
    return Settings(**values)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        (
            {
                "background_finished_set_archive_timeout_seconds": 120,
                "background_finished_set_archive_lease_seconds": 120,
            },
            "lease must exceed",
        ),
        (
            {
                "background_finished_set_archive_retry_base_seconds": 31,
                "background_finished_set_archive_retry_max_seconds": 30,
            },
            "retry maximum",
        ),
    ],
)
def test_finished_set_archive_timing_contract_is_validated(
    overrides: dict[str, int],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        _archive_settings(**overrides)


def test_controller_registers_archive_loop_without_publication(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'controller.db').as_posix()}")
    settings = _archive_settings(publishing_enabled=False)
    runtime = build_controller_runtime(
        settings=settings,
        sessions=database.sessions,
        salad_client=None,
        object_store=MemoryObjectStore(bucket="finished-sets"),
    )

    specs = {spec.name: spec for spec in runtime._loop_specs}

    assert "finished-set-archives" in specs
    assert specs["finished-set-archives"].timeout_seconds == (
        settings.background_finished_set_archive_timeout_seconds + 5
    )
    assert specs["finished-set-archives"].requires_initial_success_for_readiness is False
    assert "publication-orchestration" not in specs


@pytest.mark.asyncio
async def test_archive_workload_forwards_independent_bounded_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'controller-once.db').as_posix()}")
    settings = _archive_settings(
        background_finished_set_archive_timeout_seconds=120,
        background_finished_set_archive_lease_seconds=240,
        background_finished_set_archive_retry_base_seconds=17,
        background_finished_set_archive_retry_max_seconds=301,
        background_finished_set_archive_max_archive_bytes=123_456_789,
    )
    store = MemoryObjectStore(bucket="finished-sets")
    captured: dict[str, object] = {}

    async def fake_cycle(*args: object, **kwargs: object) -> SimpleNamespace:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return SimpleNamespace(did_work=True)

    monkeypatch.setattr(runtime_module, "run_finished_set_archive_cycle", fake_cycle)
    workloads = ControllerWorkloads(
        settings=settings,
        sessions=database.sessions,
        instance_id="controller-test",
        salad_client=None,
        object_store=store,
    )

    did_work = await workloads.finished_set_archive_once()

    assert did_work is True
    assert captured["args"] == (database.sessions, store)
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs == {
        "worker_id": "controller-test:finished-set-archive",
        "lease_seconds": 240,
        "retry_base_seconds": 17,
        "retry_max_seconds": 301,
        "max_archive_bytes": 123_456_789,
    }
