from pathlib import Path

import pytest
from pydantic import ValidationError

from gen_automation.config import Environment, Settings
from gen_automation.controller.runtime import build_controller_runtime
from gen_automation.db.session import Database
from gen_automation.storage.memory import MemoryObjectStore


def test_quality_scoring_requires_storage_and_background_runtime() -> None:
    with pytest.raises(ValidationError, match="private object storage"):
        Settings(quality_scoring_enabled=True)
    with pytest.raises(ValidationError, match="background runtime"):
        Settings(
            quality_scoring_enabled=True,
            storage_enabled=True,
            storage_bucket="quality",
        )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        (
            {
                "background_quality_timeout_seconds": 49,
                "background_quality_analysis_timeout_seconds": 45,
            },
            "cover isolated analysis",
        ),
        (
            {
                "background_quality_timeout_seconds": 75,
                "background_quality_lease_seconds": 75,
            },
            "lease must exceed",
        ),
        (
            {
                "background_quality_retry_base_seconds": 31,
                "background_quality_retry_max_seconds": 30,
            },
            "retry maximum",
        ),
    ],
)
def test_quality_timing_contract_is_validated(
    overrides: dict[str, int],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        Settings(
            environment=Environment.TEST,
            background_runtime_enabled=True,
            quality_scoring_enabled=True,
            storage_enabled=True,
            storage_bucket="quality",
            **overrides,
        )


def test_controller_registers_bounded_quality_loop(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'controller.db').as_posix()}")
    settings = Settings(
        environment=Environment.TEST,
        background_runtime_enabled=True,
        quality_scoring_enabled=True,
        storage_enabled=True,
        storage_bucket="quality",
    )
    runtime = build_controller_runtime(
        settings=settings,
        sessions=database.sessions,
        salad_client=None,
        object_store=MemoryObjectStore(bucket="quality"),
    )
    specs = {spec.name: spec for spec in runtime._loop_specs}
    assert "quality-scoring" in specs
    assert specs["quality-scoring"].timeout_seconds == (
        settings.background_quality_timeout_seconds + 5
    )
