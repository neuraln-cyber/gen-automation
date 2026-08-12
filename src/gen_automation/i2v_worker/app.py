from __future__ import annotations

import asyncio
import json
import shutil
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from gen_automation.i2v_worker.comfy import ComfyError
from gen_automation.i2v_worker.media import (
    MediaError,
    download_input,
    encode_video,
    upload_video,
)
from gen_automation.i2v_worker.models import I2VJob, I2VResult, OutputResult
from gen_automation.i2v_worker.settings import I2VWorkerSettings
from gen_automation.i2v_worker.supervisor import WorkerSupervisor
from gen_automation.i2v_worker.workflow import (
    WorkflowError,
    load_workflow_template,
    render_workflow,
)


async def _bounded_body(request: Request, maximum: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > maximum or int(declared) < 0:
                raise HTTPException(status_code=413, detail="request body too large")
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid request") from None
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > maximum:
            raise HTTPException(status_code=413, detail="request body too large")
        chunks.append(chunk)
    return b"".join(chunks)


def create_i2v_worker_app(
    settings: I2VWorkerSettings,
    *,
    supervisor: WorkerSupervisor | None = None,
) -> FastAPI:
    resolved_supervisor = supervisor or WorkerSupervisor(settings)
    execution_lock = asyncio.Lock()
    workflow = load_workflow_template(settings.workflow_template)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await resolved_supervisor.start()
        try:
            yield
        finally:
            await resolved_supervisor.stop()

    app = FastAPI(
        title="DaSiWa I2V Worker",
        version="1",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    @app.get("/health")
    async def health() -> JSONResponse:
        return JSONResponse(
            status_code=503 if resolved_supervisor.failed else 200,
            content={"status": "failed" if resolved_supervisor.failed else "ok"},
        )

    @app.get("/ready")
    async def ready() -> JSONResponse:
        comfy = resolved_supervisor.comfy_client
        is_ready = bool(resolved_supervisor.ready and comfy is not None and await comfy.ready())
        return JSONResponse(
            status_code=200 if is_ready else 503,
            content={"status": "ready" if is_ready else "not_ready"},
        )

    @app.post("/jobs/i2v", response_model=I2VResult)
    async def generate(request: Request) -> I2VResult:
        content_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
        if content_type != "application/json":
            raise HTTPException(status_code=415, detail="application/json required")
        if not resolved_supervisor.ready or resolved_supervisor.comfy_client is None:
            raise HTTPException(status_code=503, detail="worker not ready")
        try:
            body = await _bounded_body(request, settings.max_body_bytes)
            job = I2VJob.model_validate_json(body, strict=True)
        except (ValidationError, ValueError, json.JSONDecodeError):
            raise HTTPException(status_code=400, detail="invalid request") from None

        async with execution_lock:
            try:
                return await _run_job(
                    job,
                    settings=settings,
                    supervisor=resolved_supervisor,
                    workflow=workflow,
                )
            except (ComfyError, MediaError, WorkflowError):
                raise HTTPException(status_code=500, detail="generation failed") from None

    app.state.supervisor = resolved_supervisor
    return app


async def _run_job(
    job: I2VJob,
    *,
    settings: I2VWorkerSettings,
    supervisor: WorkerSupervisor,
    workflow: dict[str, Any],
) -> I2VResult:
    job_root = settings.runtime_root / "jobs" / str(job.attempt_id)
    input_suffix = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/webp": ".webp",
    }[job.input_snapshot.content_type]
    input_name = f"source-{job.attempt_id}{input_suffix}"
    input_path = settings.runtime_root / "input" / input_name
    allow_http = settings.environment == "test"
    try:
        job_root.mkdir(parents=True, exist_ok=False, mode=0o700)
        await download_input(
            job.input_grant,
            job.input_snapshot,
            input_path,
            timeout_seconds=settings.network_timeout_seconds,
            attempts=settings.network_attempts,
            allow_http=allow_http,
        )
        rendered, seed, _prefix = render_workflow(
            workflow,
            input_filename=input_name,
            positive_prompt=job.positive_prompt,
            negative_prompt=job.negative_prompt,
            settings=job.settings_snapshot,
            job_id=job.job_id,
            attempt_id=job.attempt_id,
        )
        comfy = supervisor.comfy_client
        if comfy is None:
            raise MediaError("worker lost ComfyUI")
        frames = await comfy.execute(rendered, settings.runtime_root / "output")
        video, metadata = await asyncio.to_thread(
            encode_video,
            frames,
            job.settings_snapshot,
            job_root,
        )
        version_id, byte_size, sha256 = await upload_video(
            video,
            job.output_grant,
            timeout_seconds=settings.network_timeout_seconds,
            attempts=settings.network_attempts,
            allow_http=allow_http,
        )
        return I2VResult(
            job_id=job.job_id,
            attempt_id=job.attempt_id,
            request_sha256=job.request_sha256,
            output=OutputResult(
                storage_backend=job.output_grant.storage_backend,
                storage_bucket=job.output_grant.storage_bucket,
                object_key=job.output_grant.object_key,
                object_version_id=version_id,
                sha256=sha256,
                width=metadata["width"],
                height=metadata["height"],
                frame_count=metadata["frame_count"],
                fps=metadata["fps"],
                duration_ms=metadata["duration_ms"],
                byte_size=byte_size,
                metadata={
                    "workflow": "dasiwa-wan22-i2v-v1",
                    "seed": seed,
                    "codec": metadata["codec"],
                    "pixel_format": metadata["pixel_format"],
                    "faststart": metadata["faststart"],
                },
            ),
        )
    finally:
        input_path.unlink(missing_ok=True)
        shutil.rmtree(job_root, ignore_errors=True)
        output_attempt = (
            settings.runtime_root / "output/i2v" / str(job.job_id) / str(job.attempt_id)
        )
        shutil.rmtree(output_attempt, ignore_errors=True)
