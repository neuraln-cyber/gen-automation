from __future__ import annotations

import asyncio
import json
import logging
import shutil
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from gen_automation.i2v_worker.comfy import ComfyError
from gen_automation.i2v_worker.face_stabilizer import (
    FaceDetector,
    FaceStabilizationError,
    FaceStabilizationReason,
    SourceFaceAnalysis,
    face_stabilizer_capability,
    preflight_source_face,
    stabilize_face_frames,
)
from gen_automation.i2v_worker.media import (
    MediaError,
    download_input,
    encode_video,
    prepare_input_image,
    resolve_generation_settings,
    upload_video,
)
from gen_automation.i2v_worker.models import I2VJob, I2VResult, OutputResult
from gen_automation.i2v_worker.settings import I2VWorkerSettings
from gen_automation.i2v_worker.supervisor import WorkerSupervisor
from gen_automation.i2v_worker.workflow import (
    WorkflowError,
    effective_negative_prompt,
    effective_positive_prompt,
    load_workflow_template,
    lora_provenance,
    render_workflow,
)

_LOGGER = logging.getLogger(__name__)


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

    async def _readiness() -> tuple[bool, dict[str, object]]:
        comfy = resolved_supervisor.comfy_client
        is_ready = bool(
            resolved_supervisor.ready
            and comfy is not None
            and resolved_supervisor.face_detector is not None
            and await comfy.ready()
        )
        content: dict[str, object] = {
            "status": "ready" if is_ready else "not_ready",
        }
        if is_ready:
            content["capability"] = {
                "schema": "gen-automation/i2v-worker-capability/v1",
                "lora_worker_enabled": settings.lora_worker_enabled,
                "private_manifest_source_sha256": (settings.private_manifest_source_sha256),
                "model_objects_sha256": settings.model_objects_sha256,
                "artifact_identity_sha256": settings.artifact_identity_sha256,
                "model_roles": [item.role for item in settings.model_objects],
                "source_revision": settings.source_revision,
                "face_stabilizer": face_stabilizer_capability(),
            }
        return is_ready, content

    @app.get("/ready")
    async def ready() -> JSONResponse:
        is_ready, content = await _readiness()
        return JSONResponse(
            status_code=200 if is_ready else 503,
            content=content,
        )

    @app.get("/ready/capability/{manifest_sha256}/{artifact_identity_sha256}/{source_revision}")
    async def exact_capability(
        manifest_sha256: str,
        artifact_identity_sha256: str,
        source_revision: str,
    ) -> JSONResponse:
        if (
            not settings.lora_worker_enabled
            or settings.private_manifest_source_sha256 is None
            or settings.source_revision is None
            or manifest_sha256 != settings.private_manifest_source_sha256
            or artifact_identity_sha256 != settings.artifact_identity_sha256
            or source_revision != settings.source_revision
        ):
            raise HTTPException(status_code=404, detail="worker capability not found")
        is_ready, content = await _readiness()
        return JSONResponse(
            status_code=200 if is_ready else 503,
            content=content,
        )

    @app.post("/jobs/i2v", response_model=I2VResult)
    async def generate(request: Request) -> I2VResult:
        content_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
        if content_type != "application/json":
            raise HTTPException(status_code=415, detail="application/json required")
        if (
            not resolved_supervisor.ready
            or resolved_supervisor.comfy_client is None
            or resolved_supervisor.face_detector is None
        ):
            raise HTTPException(status_code=503, detail="worker not ready")
        try:
            body = await _bounded_body(request, settings.max_body_bytes)
            job = I2VJob.model_validate_json(body, strict=True)
        except (ValidationError, ValueError, json.JSONDecodeError):
            raise HTTPException(status_code=400, detail="invalid request") from None
        if job.settings_snapshot.loras and not settings.lora_worker_enabled:
            raise HTTPException(
                status_code=409,
                detail="reviewed LoRAs are not enabled for this worker profile",
            )
        try:
            effective_positive_prompt(job.positive_prompt, job.settings_snapshot)
        except WorkflowError:
            raise HTTPException(status_code=400, detail="invalid reviewed LoRA prompt") from None

        async with execution_lock:
            try:
                return await _run_job(
                    job,
                    settings=settings,
                    supervisor=resolved_supervisor,
                    workflow=workflow,
                )
            except FaceStabilizationError as error:
                status_code = 422 if error.is_contract_failure else 500
                _LOGGER.warning(
                    "stable-expression processing failed: reason_code=%s status=%d "
                    "job_id=%s attempt_id=%s",
                    error.reason.name.lower(),
                    status_code,
                    job.job_id,
                    job.attempt_id,
                )
                if error.is_contract_failure:
                    raise HTTPException(
                        status_code=422,
                        detail="stable-expression face contract failed",
                    ) from None
                raise HTTPException(status_code=500, detail="generation failed") from None
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
    prepared_name = f"prepared-{job.attempt_id}.png"
    prepared_path = settings.runtime_root / "input" / prepared_name
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
        generation_settings = resolve_generation_settings(
            job.settings_snapshot,
            source_width=job.input_snapshot.width,
            source_height=job.input_snapshot.height,
        )
        await asyncio.to_thread(
            prepare_input_image,
            input_path,
            prepared_path,
            width=generation_settings.width,
            height=generation_settings.height,
        )
        face_detector: FaceDetector | None = None
        source_face: SourceFaceAnalysis | None = None
        if generation_settings.face_fidelity == "stable_expression":
            face_detector = supervisor.face_detector
            if face_detector is None:
                raise FaceStabilizationError(FaceStabilizationReason.UNAVAILABLE)
            source_face = await asyncio.to_thread(
                preflight_source_face,
                prepared_path,
                face_detector,
            )
        rendered, seed, _prefix = render_workflow(
            workflow,
            input_filename=prepared_name,
            positive_prompt=job.positive_prompt,
            negative_prompt=job.negative_prompt,
            settings=generation_settings,
            job_id=job.job_id,
            attempt_id=job.attempt_id,
        )
        resolved_positive_prompt = effective_positive_prompt(
            job.positive_prompt,
            generation_settings,
        )
        resolved_negative_prompt = effective_negative_prompt(
            job.negative_prompt,
            generation_settings,
        )
        comfy = supervisor.comfy_client
        if comfy is None:
            raise MediaError("worker lost ComfyUI")
        frames = await comfy.execute(rendered, settings.runtime_root / "output")
        face_metadata: dict[str, object] | None = None
        if generation_settings.face_fidelity == "stable_expression":
            if face_detector is None or source_face is None:
                raise FaceStabilizationError(FaceStabilizationReason.UNAVAILABLE)
            stabilized = await asyncio.to_thread(
                stabilize_face_frames,
                prepared_path,
                frames,
                job_root / "face-stabilized-frames",
                detector=face_detector,
                source_analysis=source_face,
            )
            frames = stabilized.frames
            face_metadata = stabilized.metadata
        video, metadata = await asyncio.to_thread(
            encode_video,
            frames,
            generation_settings,
            job_root,
            source_width=job.input_snapshot.width,
            source_height=job.input_snapshot.height,
        )
        version_id, byte_size, sha256 = await upload_video(
            video,
            job.output_grant,
            timeout_seconds=settings.network_timeout_seconds,
            attempts=settings.network_attempts,
            allow_http=allow_http,
        )
        output_metadata: dict[str, Any] = {
            "workflow": "dasiwa-wan22-i2v-v1",
            "seed": seed,
            "codec": metadata["codec"],
            "pixel_format": metadata["pixel_format"],
            "faststart": metadata["faststart"],
            "native_width": metadata["native_width"],
            "native_height": metadata["native_height"],
            "upscale": metadata["upscale"],
            "loop_mode": metadata["loop_mode"],
            "loop_count": metadata["loop_count"],
            "source_fit": metadata["source_fit"],
            "match_source_aspect": metadata["match_source_aspect"],
            "loras": lora_provenance(generation_settings),
            "effective_positive_prompt": resolved_positive_prompt,
            "effective_negative_prompt": resolved_negative_prompt,
            "face_fidelity": generation_settings.face_fidelity,
        }
        if face_metadata is not None:
            output_metadata["face_stabilization"] = face_metadata
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
                metadata=output_metadata,
            ),
        )
    finally:
        input_path.unlink(missing_ok=True)
        prepared_path.unlink(missing_ok=True)
        shutil.rmtree(job_root, ignore_errors=True)
        output_attempt = (
            settings.runtime_root / "output/i2v" / str(job.job_id) / str(job.attempt_id)
        )
        shutil.rmtree(output_attempt, ignore_errors=True)
