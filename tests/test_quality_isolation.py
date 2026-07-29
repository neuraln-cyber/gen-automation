import asyncio
import sys
from io import BytesIO

import pytest
from PIL import Image

from gen_automation.quality import analyze_image
from gen_automation.services.quality_isolation import (
    QualityIsolationPolicy,
    QualityIsolationUnavailableError,
    _apply_linux_hard_limits,
    _scrub_child_environment,
    analyze_image_isolated,
)


def _png() -> bytes:
    output = BytesIO()
    Image.new("RGB", (64, 64), (21, 72, 131)).save(output, format="PNG")
    return output.getvalue()


class _ResourceApi:
    RLIM_INFINITY = -1
    RLIMIT_AS = 1
    RLIMIT_RSS = 2
    RLIMIT_CORE = 3

    def __init__(self) -> None:
        self.limits = {
            self.RLIMIT_AS: (self.RLIM_INFINITY, self.RLIM_INFINITY),
            self.RLIMIT_RSS: (self.RLIM_INFINITY, self.RLIM_INFINITY),
            self.RLIMIT_CORE: (self.RLIM_INFINITY, self.RLIM_INFINITY),
        }

    def getrlimit(self, kind: int) -> tuple[int, int]:
        return self.limits[kind]

    def setrlimit(self, kind: int, value: tuple[int, int]) -> None:
        self.limits[kind] = value


def test_quality_isolation_policy_and_linux_limits_are_bounded() -> None:
    with pytest.raises(ValueError, match="wall timeout"):
        QualityIsolationPolicy(wall_timeout_seconds=0.5)
    with pytest.raises(ValueError, match="memory limit"):
        QualityIsolationPolicy(memory_limit_bytes=128 * 1024 * 1024)

    resources = _ResourceApi()
    limit = 512 * 1024 * 1024
    _apply_linux_hard_limits(
        limit,
        resource_module=resources,
        platform="linux",
    )
    assert resources.limits[resources.RLIMIT_AS] == (limit, limit)
    assert resources.limits[resources.RLIMIT_RSS] == (limit, limit)
    assert resources.limits[resources.RLIMIT_CORE] == (0, 0)


def test_quality_child_scrubs_inherited_environment_before_parsing() -> None:
    inherited = {
        "GEN_AUTOMATION_STORAGE_SESSION_TOKEN": "temporary-secret",
        "HTTPS_PROXY": "https://proxy.example",
    }

    _scrub_child_environment(inherited)

    assert inherited == {}


def test_quality_isolation_rejects_unverifiable_core_dump_limit() -> None:
    class IgnoredCoreLimitResource(_ResourceApi):
        def setrlimit(self, kind: int, value: tuple[int, int]) -> None:
            if kind != self.RLIMIT_CORE:
                super().setrlimit(kind, value)

    with pytest.raises(QualityIsolationUnavailableError, match="could not be verified"):
        _apply_linux_hard_limits(
            512 * 1024 * 1024,
            resource_module=IgnoredCoreLimitResource(),
            platform="linux",
        )


def test_quality_isolation_fails_closed_off_linux(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(QualityIsolationUnavailableError, match="only on Linux"):
        asyncio.run(analyze_image_isolated(_png()))


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="requires Linux RLIMIT support")
@pytest.mark.asyncio
async def test_real_spawned_quality_analysis_matches_in_process_result() -> None:
    payload = _png()
    expected = analyze_image(payload)
    actual = await analyze_image_isolated(
        payload,
        policy=QualityIsolationPolicy(
            wall_timeout_seconds=30,
            memory_limit_bytes=768 * 1024 * 1024,
        ),
    )
    assert actual == expected
