"""Authenticated asynchronous API used by on-demand RunPod I2V Pods."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import time
from collections.abc import AsyncIterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from gen_automation.i2v_worker.app import _bounded_body
from gen_automation.i2v_worker.runpod_handler import RunPodHandlerError, RunPodI2VHandler
from gen_automation.i2v_worker.runpod_models import RunPodI2VEvent
from gen_automation.i2v_worker.settings import I2VWorkerSettings

_LOGGER = logging.getLogger(__name__)
_JOB_ID = re.compile(r"^rpod_[A-Za-z0-9_-]{3,64}_[0-9a-f]{32}$")
_MAX_BODY_BYTES = 1024 * 1024


@dataclass(slots=True)
class _JobRecord:
    request_sha256: str
    started_at: float
    status: str = "IN_PROGRESS"
    output: dict[str, Any] | None = None
    error: str | None = None
    completed_at: float | None = None


class _PodRuntime:
    def __init__(self, handler: RunPodI2VHandler) -> None:
        self.handler = handler
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="i2v-pod")
        self.jobs: dict[str, _JobRecord] = {}
        self.tasks: set[asyncio.Task[None]] = set()
        self.lock = asyncio.Lock()

    async def submit(
        self,
        *,
        job_id: str,
        request_sha256: str,
        event: Mapping[str, Any],
    ) -> _JobRecord:
        async with self.lock:
            existing = self.jobs.get(job_id)
            if existing is not None:
                if not secrets.compare_digest(existing.request_sha256, request_sha256):
                    raise HTTPException(status_code=409, detail="job identity conflict")
                return existing
            if any(record.status == "IN_PROGRESS" for record in self.jobs.values()):
                raise HTTPException(status_code=409, detail="worker is busy")
            record = _JobRecord(request_sha256=request_sha256, started_at=time.monotonic())
            self.jobs[job_id] = record
            task = asyncio.create_task(self._execute(job_id, dict(event)))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
            return record

    async def _execute(self, job_id: str, event: dict[str, Any]) -> None:
        loop = asyncio.get_running_loop()
        status: str
        output: dict[str, Any] | None
        error: str | None
        try:
            result = await loop.run_in_executor(self.executor, self.handler, event)
        except RunPodHandlerError:
            _LOGGER.warning("RunPod Pod I2V job failed: job_id=%s", job_id)
            status = "FAILED"
            output = None
            error = "I2V generation failed"
        except Exception:
            _LOGGER.exception("RunPod Pod I2V job failed unexpectedly: job_id=%s", job_id)
            status = "FAILED"
            output = None
            error = "I2V generation failed"
        else:
            status = "COMPLETED"
            output = result
            error = None
        async with self.lock:
            record = self.jobs[job_id]
            record.status = status
            record.output = output
            record.error = error
            record.completed_at = time.monotonic()

    async def close(self) -> None:
        for task in tuple(self.tasks):
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        await asyncio.get_running_loop().run_in_executor(self.executor, self.handler.close)
        self.executor.shutdown(wait=True, cancel_futures=True)


def create_runpod_pod_app(
    *,
    settings: I2VWorkerSettings | None = None,
    api_key: str | None = None,
    handler: RunPodI2VHandler | None = None,
) -> FastAPI:
    resolved_key = api_key or os.environ.get("GEN_I2V_WORKER_POD_API_KEY", "").strip()
    if re.fullmatch(r"[0-9a-f]{64}", resolved_key) is None:
        raise RuntimeError("RunPod Pod API key is unavailable")
    resolved_settings = settings or I2VWorkerSettings()
    resolved_handler = handler or RunPodI2VHandler(resolved_settings)
    runtime = _PodRuntime(resolved_handler)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await runtime.close()

    app = FastAPI(
        title="DaSiWa I2V RunPod Pod",
        version="1",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    def authorize(request: Request) -> None:
        supplied = request.headers.get("authorization", "")
        expected = f"Bearer {resolved_key}"
        if not secrets.compare_digest(supplied, expected):
            raise HTTPException(status_code=401, detail="unauthorized")

    @app.get("/health")
    async def health() -> JSONResponse:
        return JSONResponse(content={"status": "ok"})

    @app.get("/ready")
    async def ready(request: Request) -> JSONResponse:
        authorize(request)
        return JSONResponse(
            content={
                "status": "ready",
                "provider": "runpod-pod",
                "model_objects_sha256": resolved_settings.model_objects_sha256,
                "artifact_identity_sha256": resolved_settings.artifact_identity_sha256,
                "source_revision": resolved_settings.source_revision,
            }
        )

    @app.post("/v1/jobs/{job_id}")
    async def submit(job_id: str, request: Request) -> JSONResponse:
        authorize(request)
        if _JOB_ID.fullmatch(job_id) is None:
            raise HTTPException(status_code=404, detail="job not found")
        if request.headers.get("content-type", "").partition(";")[0].strip().lower() != (
            "application/json"
        ):
            raise HTTPException(status_code=415, detail="application/json required")
        body = await _bounded_body(request, _MAX_BODY_BYTES)
        request_sha256 = hashlib.sha256(body).hexdigest()
        try:
            decoded = json.loads(body)
            event = RunPodI2VEvent.model_validate(decoded, strict=False)
        except (json.JSONDecodeError, ValidationError, ValueError, TypeError):
            raise HTTPException(status_code=400, detail="invalid request") from None
        if event.id != job_id:
            raise HTTPException(status_code=409, detail="job identity conflict")
        record = await runtime.submit(
            job_id=job_id,
            request_sha256=request_sha256,
            event=event.model_dump(mode="json", by_alias=True),
        )
        return JSONResponse(status_code=202, content=_response(job_id, record))

    @app.get("/v1/jobs/{job_id}")
    async def status(job_id: str, request: Request) -> JSONResponse:
        authorize(request)
        if _JOB_ID.fullmatch(job_id) is None:
            raise HTTPException(status_code=404, detail="job not found")
        async with runtime.lock:
            record = runtime.jobs.get(job_id)
            if record is None:
                raise HTTPException(status_code=404, detail="job not found")
            return JSONResponse(content=_response(job_id, record))

    app.state.runtime = runtime
    return app


def _response(job_id: str, record: _JobRecord) -> dict[str, object]:
    completed_at = record.completed_at or time.monotonic()
    execution_ms = round((completed_at - record.started_at) * 1000)
    return {
        "id": job_id,
        "status": record.status,
        "output": record.output,
        "error": record.error,
        "executionTime": max(0, execution_ms),
        "workerId": os.environ.get("RUNPOD_POD_ID"),
    }


def main() -> None:
    uvicorn.run(
        create_runpod_pod_app(),
        host="0.0.0.0",  # noqa: S104 - RunPod's authenticated HTTPS proxy needs this listener.
        port=8000,
        log_level="info",
        access_log=False,
        workers=1,
    )


if __name__ == "__main__":
    main()
