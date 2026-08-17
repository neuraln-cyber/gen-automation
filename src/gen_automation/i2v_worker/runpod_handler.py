"""Shared RunPod adapter for the existing DaSiWa I2V pipeline."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from typing import Any

import httpx2
from pydantic import ValidationError

from gen_automation.i2v_worker.app import _run_job
from gen_automation.i2v_worker.runpod_models import RunPodI2VEvent
from gen_automation.i2v_worker.runpod_volume import RunPodVolumeBootstrapper
from gen_automation.i2v_worker.settings import I2VWorkerSettings
from gen_automation.i2v_worker.supervisor import WorkerSupervisor
from gen_automation.i2v_worker.workflow import load_workflow_template

_LOGGER = logging.getLogger(__name__)


class RunPodHandlerError(Exception):
    """A redacted worker failure safe to expose to RunPod."""


class RunPodI2VHandler:
    """Keep one ComfyUI process warm across serial endpoint jobs."""

    def __init__(self, settings: I2VWorkerSettings) -> None:
        self.settings = settings.model_copy(update={"models_prepared": True})
        self.supervisor = WorkerSupervisor(self.settings)
        self.workflow = load_workflow_template(self.settings.workflow_template)
        self._runner = asyncio.Runner()
        self._initialized = False

    def __call__(self, raw_event: Mapping[str, Any]) -> dict[str, Any]:
        try:
            event = RunPodI2VEvent.model_validate(raw_event, strict=False)
        except ValidationError:
            raise RunPodHandlerError("invalid I2V job envelope") from None
        try:
            result = self._runner.run(self._handle(event))
        except RunPodHandlerError:
            raise
        except Exception:
            _LOGGER.exception("RunPod I2V handler failed")
            raise RunPodHandlerError("I2V generation failed") from None
        return result

    def close(self) -> None:
        if self._initialized:
            self._runner.run(self.supervisor.stop())
        self._runner.close()

    async def _handle(self, event: RunPodI2VEvent) -> dict[str, Any]:
        handler_started = time.monotonic()
        await self._claim(event)
        bootstrapper = RunPodVolumeBootstrapper(self.settings)
        await asyncio.to_thread(
            bootstrapper.claim_execution,
            submission_key=event.input.submission_key,
            provider_job_id=event.id,
        )
        worker_reused = self._initialized
        volume_bootstrap_ms = 0
        worker_startup_ms = 0
        if not worker_reused:
            bootstrap_started = time.monotonic()
            await asyncio.to_thread(
                bootstrapper.bootstrap,
                event.input.model_grants,
            )
            volume_bootstrap_ms = _elapsed_ms(bootstrap_started)
            startup_started = time.monotonic()
            await self.supervisor.start()
            while not self.supervisor.ready:
                if self.supervisor.failed:
                    raise RunPodHandlerError("I2V worker startup failed")
                await asyncio.sleep(2)
            worker_startup_ms = _elapsed_ms(startup_started)
            self._initialized = True
        generation_started = time.monotonic()
        result = await _run_job(
            event.input.job,
            settings=self.settings,
            supervisor=self.supervisor,
            workflow=self.workflow,
        )
        result.output.metadata["runpod_runtime"] = {
            "schema": "gen-automation/i2v-runpod-runtime/v1",
            "worker_reused": worker_reused,
            "volume_bootstrap_ms": volume_bootstrap_ms,
            "worker_startup_ms": worker_startup_ms,
            "generation_ms": _elapsed_ms(generation_started),
            "total_handler_ms": _elapsed_ms(handler_started),
        }
        return result.model_dump(mode="json", by_alias=True)

    async def _claim(self, event: RunPodI2VEvent) -> None:
        grant = event.input.claim
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {grant.bearer_token}",
            "Content-Type": "application/json",
        }
        try:
            async with httpx2.AsyncClient(
                follow_redirects=False,
                trust_env=False,
                timeout=httpx2.Timeout(30),
            ) as client:
                response = await client.post(
                    str(grant.url),
                    headers=headers,
                    json={"provider_job_id": event.id},
                )
        except httpx2.HTTPError:
            raise RunPodHandlerError("I2V execution claim failed") from None
        if response.status_code != 204:
            raise RunPodHandlerError("I2V execution claim rejected")


def _elapsed_ms(started: float) -> int:
    return max(0, round((time.monotonic() - started) * 1000))


def main() -> None:
    try:
        import runpod  # type: ignore[import-not-found]
    except ImportError:
        raise SystemExit("RunPod SDK is unavailable") from None
    handler = RunPodI2VHandler(I2VWorkerSettings())
    runpod.serverless.start({"handler": handler})


if __name__ == "__main__":
    main()
