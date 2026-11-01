from __future__ import annotations

import logging
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from gen_automation.i2v_worker import rtx_preflight as module


@pytest.fixture
def runtime(monkeypatch, caplog):
    monkeypatch.setattr(module._LOGGER, "disabled", False)
    caplog.set_level(logging.INFO, logger=module.__name__)
    monkeypatch.setenv("NVIDIA_DRIVER_CAPABILITIES", "compute,utility,ngx")
    calls = []

    def library(name):
        assert name == "libnvidia-ngx.so.1"
        calls.append("ngx")
        return object()

    class Effect:
        is_loaded = False

        def __init__(self, **kwargs):
            assert kwargs == {"quality": 4, "strength": 1.0, "device": 0, "image_encoding": 1}
            calls.append("create")

        def load(self):
            assert (self.output_width, self.output_height) == (768, 432)
            calls.append("load")
            self.is_loaded = True

        def close(self):
            calls.append("close")

    monkeypatch.setattr(module.ctypes, "CDLL", library)
    fake = SimpleNamespace(
        VideoSuperRes=Effect,
        QualityLevel=SimpleNamespace(ULTRA=4),
        ImageEncoding=SimpleNamespace(RGB10A2=1),
    )
    monkeypatch.setitem(sys.modules, "nvvfx", fake)
    return calls, Effect


@pytest.mark.parametrize("capabilities", ["compute,utility,ngx", "all"])
def test_real_load_contract_required_before_success(runtime, monkeypatch, caplog, capabilities):
    monkeypatch.setenv("NVIDIA_DRIVER_CAPABILITIES", capabilities)
    module.verify_rtx_runtime()
    assert runtime[0] == ["ngx", "create", "load", "close"]
    assert "status=passed check=vfx_load" in caplog.text


@pytest.mark.parametrize("capabilities", ["", "compute,utility", "graphics,video", "ngx"])
def test_missing_driver_capability_fails_before_effect(runtime, monkeypatch, caplog, capabilities):
    monkeypatch.setenv("NVIDIA_DRIVER_CAPABILITIES", capabilities)
    with pytest.raises(module.RTXStartupError):
        module.verify_rtx_runtime()
    assert runtime[0] == []
    assert "status=failed check=driver_capabilities" in caplog.text


@pytest.mark.parametrize(
    "stage", ["ngx_library", "vfx_import", "vfx_create", "vfx_load", "not_loaded"]
)
def test_failure_never_becomes_ready_or_leaks_native_details(runtime, monkeypatch, caplog, stage):
    calls, effect = runtime

    def fail(*args, **kwargs):
        raise RuntimeError("private-token https://private.invalid/secret object-key prompt")

    if stage == "ngx_library":
        monkeypatch.setattr(module.ctypes, "CDLL", fail)
    elif stage == "vfx_import":
        monkeypatch.setitem(sys.modules, "nvvfx", None)
    elif stage == "vfx_create":
        monkeypatch.setattr(sys.modules["nvvfx"], "VideoSuperRes", fail)
    elif stage == "vfx_load":
        monkeypatch.setattr(effect, "load", fail)
    else:
        monkeypatch.setattr(effect, "load", lambda self: None)
    with pytest.raises(module.RTXStartupError, match="RTX enhancement runtime is unavailable"):
        module.verify_rtx_runtime()
    check = "vfx_load" if stage == "not_loaded" else stage
    assert f"status=failed check={check}" in caplog.text
    assert "status=passed" not in caplog.text
    assert not any(s in caplog.text for s in ("private", "object-key", "prompt", "Traceback"))
    if stage in {"vfx_load", "not_loaded"}:
        assert calls[-1] == "close"


def test_image_requests_ngx_without_bundling_a_host_driver():
    text = (Path(__file__).parents[1] / "Dockerfile.i2v-worker").read_text()
    assert "ENV NVIDIA_DRIVER_CAPABILITIES=compute,utility,ngx" in text
    assert "apt-get install libnvidia" not in text
    assert "COPY --from=h3-import-driver" not in text
