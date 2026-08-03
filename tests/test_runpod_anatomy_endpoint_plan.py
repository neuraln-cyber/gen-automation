from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "runpod_anatomy_endpoint.py"


def _module():
    spec = importlib.util.spec_from_file_location("runpod_anatomy_endpoint", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_plan_is_pinned_and_scales_to_zero() -> None:
    module = _module()
    plan = module.public_plan()

    assert plan["mutates_runpod"] is False
    assert plan["template"]["imageName"].startswith("runpod/worker-v1-vllm@sha256:")
    assert plan["template"]["env"]["REVISION"] == module.MODEL_REVISION
    assert "MODEL_REVISION" not in plan["template"]["env"]
    assert plan["endpoint"]["workersMin"] == 0
    assert plan["endpoint"]["workersMax"] == 1
    assert plan["endpoint"]["gpuTypeIds"] == ["NVIDIA A40", "NVIDIA RTX A6000"]


def test_apply_is_locked_while_spending_is_not_released(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _module()
    monkeypatch.delenv(module.SPEND_SWITCH, raising=False)
    monkeypatch.setenv("RUNPOD_API_KEY", "not-sent-because-guard-fires-first")

    with pytest.raises(RuntimeError, match="paid provisioning is locked"):
        module.apply_plan(state_file=tmp_path / "state.json", acknowledge_spend=True)
