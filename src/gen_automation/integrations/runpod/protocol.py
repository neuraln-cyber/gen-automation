"""Provider contract shared by RunPod Serverless and on-demand Pods."""

from __future__ import annotations

from typing import Protocol

from gen_automation.integrations.runpod.models import (
    JSONObject,
    RunPodEndpointHealth,
    RunPodJob,
)


class RunPodProvider(Protocol):
    provider_id: str

    async def submit(
        self,
        *,
        input_payload: JSONObject,
        execution_timeout_ms: int,
        ttl_ms: int,
    ) -> RunPodJob: ...

    async def get_job(self, job_id: str) -> RunPodJob: ...

    async def cancel(self, job_id: str) -> RunPodJob: ...

    async def health(self) -> RunPodEndpointHealth: ...

    async def reap_idle(self) -> None: ...
