import asyncio
import base64
import binascii
import io
import json
import math
import re
import time
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from functools import partial
from typing import Any

import httpx2
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from PIL import Image, UnidentifiedImageError
from pydantic import TypeAdapter, ValidationError

from gen_automation.domain.deliverability import require_comfy_workflow_deliverability
from gen_automation.gpu_worker.models import (
    ComfyOutput,
    GenerateEnvelope,
    GenerateResponse,
    JsonObject,
    UploadedOutput,
    WorkerSettings,
    validate_approved_workflow,
    validate_upload_url,
)
from gen_automation.gpu_worker.runtime import (
    ComfyExecutor,
    HttpxMultipartUploader,
    MultipartUploader,
    WorkerUploadError,
)
from gen_automation.gpu_worker.security import AuthorizationError, verify_authorization

MAX_JSON_DEPTH = 64
_OUTPUTS_ADAPTER = TypeAdapter(list[ComfyOutput])
_OUTPUT_BRANCH_PATTERN = re.compile(r"^output-(\d{2})-")
_EXPECTED_FORMATS = {
    "image/png": "PNG",
    "image/jpeg": "JPEG",
    "image/webp": "WEBP",
}


class WorkerRequestError(Exception):
    pass


class WorkerOutputError(Exception):
    pass


class WorkerNotReadyError(Exception):
    pass


def _progressive_workflows(
    workflow: JsonObject,
    *,
    expected_count: int,
) -> list[tuple[int | None, JsonObject]]:
    """Split controller-rendered multi-prompt graphs into ordered image graphs.

    Shared loader nodes remain in each graph and are cached by ComfyUI. A legacy
    batched graph without the deterministic ``output-NN-`` branches keeps its
    original all-at-once execution behavior.
    """

    if expected_count <= 1:
        return [(None, workflow)]

    shared: JsonObject = {}
    branches: dict[int, JsonObject] = {}
    saw_prefixed_node = False
    for node_id, node in workflow.items():
        match = _OUTPUT_BRANCH_PATTERN.match(node_id)
        if match is None:
            if node_id.startswith("output-"):
                raise WorkerRequestError("invalid request")
            shared[node_id] = node
            continue
        saw_prefixed_node = True
        output_index = int(match.group(1))
        branches.setdefault(output_index, {})[node_id] = node

    if not saw_prefixed_node:
        return [(None, workflow)]
    if set(branches) != set(range(expected_count)):
        raise WorkerRequestError("invalid request")
    return [
        (output_index, {**shared, **branches[output_index]})
        for output_index in range(expected_count)
    ]


def _execute_if_ready(executor: ComfyExecutor, workflow: JsonObject) -> object:
    if not executor.is_ready():
        raise WorkerNotReadyError
    return executor.execute(workflow)


def _validate_json(value: object, *, depth: int = 0) -> object:
    if depth > MAX_JSON_DEPTH:
        raise WorkerRequestError("invalid request")
    if value is None or isinstance(value, (bool, str, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise WorkerRequestError("invalid request")
        return value
    if isinstance(value, list):
        return [_validate_json(item, depth=depth + 1) for item in value]
    if isinstance(value, dict):
        result: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise WorkerRequestError("invalid request")
            result[key] = _validate_json(item, depth=depth + 1)
        return result
    raise WorkerRequestError("invalid request")


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise WorkerRequestError("invalid request")
        result[key] = value
    return result


async def _read_bounded_body(request: Request, max_body_bytes: int) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError:
            raise WorkerRequestError("invalid request") from None
        if declared_length < 0:
            raise WorkerRequestError("invalid request")
        if declared_length > max_body_bytes:
            raise HTTPException(status_code=413, detail="request body too large")

    parts: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > max_body_bytes:
            raise HTTPException(status_code=413, detail="request body too large")
        parts.append(chunk)
    return b"".join(parts)


def _parse_envelope(body: bytes) -> GenerateEnvelope:
    try:
        raw: Any = json.loads(body, object_pairs_hook=_unique_json_object)
        validated = _validate_json(raw)
        if not isinstance(validated, dict):
            raise WorkerRequestError("invalid request")
        return GenerateEnvelope.model_validate(validated, strict=True)
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError, ValidationError):
        raise WorkerRequestError("invalid request") from None


def _decode_and_verify_outputs(
    raw_outputs: object,
    *,
    expected_count: int,
    max_output_bytes: int,
    max_total_output_bytes: int,
    max_image_dimension: int,
    max_image_pixels: int,
) -> list[tuple[ComfyOutput, bytes]]:
    try:
        outputs = _OUTPUTS_ADAPTER.validate_python(raw_outputs, strict=True)
    except ValidationError:
        raise WorkerOutputError("generation output invalid") from None

    if len(outputs) != expected_count:
        raise WorkerOutputError("generation output invalid")
    indices = [output.output_index for output in outputs]
    if len(indices) != len(set(indices)) or set(indices) != set(range(expected_count)):
        raise WorkerOutputError("generation output invalid")

    decoded: list[tuple[ComfyOutput, bytes]] = []
    total_bytes = 0
    for output in outputs:
        maximum_encoded = ((max_output_bytes + 2) // 3) * 4
        if len(output.data_base64) > maximum_encoded:
            raise WorkerOutputError("generation output invalid")
        try:
            content = base64.b64decode(output.data_base64, validate=True)
        except (binascii.Error, ValueError):
            raise WorkerOutputError("generation output invalid") from None
        if not content or len(content) > max_output_bytes:
            raise WorkerOutputError("generation output invalid")
        total_bytes += len(content)
        if total_bytes > max_total_output_bytes:
            raise WorkerOutputError("generation output invalid")

        try:
            with Image.open(io.BytesIO(content)) as image:
                if (
                    image.format != _EXPECTED_FORMATS[output.media_type]
                    or image.width > max_image_dimension
                    or image.height > max_image_dimension
                    or image.width * image.height > max_image_pixels
                    or image.width < 1
                    or image.height < 1
                    or getattr(image, "n_frames", 1) != 1
                ):
                    raise WorkerOutputError("generation output invalid")
                image.verify()
        except WorkerOutputError:
            raise
        except (
            Image.DecompressionBombError,
            UnidentifiedImageError,
            OSError,
            ValueError,
            SyntaxError,
        ):
            raise WorkerOutputError("generation output invalid") from None
        decoded.append((output, content))
    return decoded


def create_worker_app(
    *,
    settings: WorkerSettings,
    executor: ComfyExecutor,
    uploader: MultipartUploader | None = None,
    now: Callable[[], float] = time.time,
) -> FastAPI:
    if settings is None or executor is None:
        raise RuntimeError("worker settings and executor are required")

    owned_client: httpx2.AsyncClient | None = None
    execution_lock = asyncio.Lock()
    execution_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="gpu-work")
    readiness_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="gpu-ready")
    replay_cache: OrderedDict[str, tuple[int, str, GenerateResponse]] = OrderedDict()
    resolved_uploader = uploader
    if resolved_uploader is None:
        owned_client = httpx2.AsyncClient(
            follow_redirects=False,
            trust_env=False,
        )
        resolved_uploader = HttpxMultipartUploader(
            client=owned_client,
            timeout_seconds=settings.upload_timeout_seconds,
        )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):  # type: ignore[no-untyped-def]
        try:
            yield
        finally:
            await asyncio.to_thread(
                execution_pool.shutdown,
                wait=True,
                cancel_futures=True,
            )
            await asyncio.to_thread(
                readiness_pool.shutdown,
                wait=True,
                cancel_futures=True,
            )
            if owned_client is not None:
                await owned_client.aclose()

    app = FastAPI(
        title="Generation GPU Worker",
        version="1",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": "v1"}

    @app.get("/ready")
    async def ready() -> JSONResponse:
        try:
            loop = asyncio.get_running_loop()
            is_ready = await asyncio.wait_for(
                loop.run_in_executor(readiness_pool, executor.is_ready),
                timeout=settings.readiness_timeout_seconds,
            )
        except Exception:
            is_ready = False
        status_code = 200 if is_ready else 503
        status = "ready" if is_ready else "not_ready"
        return JSONResponse(status_code=status_code, content={"status": status, "version": "v1"})

    @app.post("/jobs/generate", response_model=GenerateResponse)
    async def generate(request: Request) -> GenerateResponse:
        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise HTTPException(status_code=415, detail="application/json required")

        try:
            body = await _read_bounded_body(request, settings.max_body_bytes)
            envelope = _parse_envelope(body)
        except WorkerRequestError:
            raise HTTPException(status_code=400, detail="invalid request") from None

        try:
            verify_authorization(envelope, settings, now=now)
        except AuthorizationError:
            raise HTTPException(status_code=401, detail="invalid authorization") from None

        if len(envelope.payload.uploads) > settings.max_outputs:
            raise HTTPException(status_code=400, detail="invalid request")
        try:
            for grant in envelope.payload.uploads:
                validate_upload_url(grant.url, settings.upload_origin)
            validate_approved_workflow(
                envelope.payload.workflow,
                settings.approved_workflow_node_classes,
            )
            require_comfy_workflow_deliverability(envelope.payload.workflow)
            progressive_workflows = _progressive_workflows(
                envelope.payload.workflow,
                expected_count=len(envelope.payload.uploads),
            )
            for _output_index, workflow in progressive_workflows:
                validate_approved_workflow(
                    workflow,
                    settings.approved_workflow_node_classes,
                )
                require_comfy_workflow_deliverability(workflow)
        except (ValueError, WorkerRequestError):
            raise HTTPException(status_code=400, detail="invalid request") from None

        # One worker owns one GPU execution lane. Serializing here also makes the
        # bounded replay cache race-free: an identical successful Salad retry
        # returns the prior stable response without regenerating or reuploading.
        async with execution_lock:
            current_time = int(now())
            expired = [
                attempt_id
                for attempt_id, (cache_expires_at, _signature, _response) in replay_cache.items()
                if cache_expires_at < current_time
            ]
            for attempt_id in expired:
                replay_cache.pop(attempt_id, None)

            cached = replay_cache.get(envelope.payload.attempt_id)
            if cached is not None:
                _cache_expires_at, cached_signature, cached_response = cached
                if cached_signature != envelope.signature:
                    raise HTTPException(
                        status_code=409,
                        detail="request conflicts with completed attempt",
                    )
                replay_cache.move_to_end(envelope.payload.attempt_id)
                return cached_response

            loop = asyncio.get_running_loop()
            grants_by_index = {grant.output_index: grant for grant in envelope.payload.uploads}
            uploaded: list[UploadedOutput] = []
            total_output_bytes = 0
            for branch_output_index, workflow in progressive_workflows:
                try:
                    raw_outputs = await loop.run_in_executor(
                        execution_pool,
                        _execute_if_ready,
                        executor,
                        workflow,
                    )
                except WorkerNotReadyError:
                    raise HTTPException(status_code=503, detail="worker not ready") from None
                except Exception:
                    raise HTTPException(status_code=502, detail="generation failed") from None

                expected_branch_count = (
                    len(envelope.payload.uploads) if branch_output_index is None else 1
                )
                try:
                    outputs = await loop.run_in_executor(
                        execution_pool,
                        partial(
                            _decode_and_verify_outputs,
                            raw_outputs,
                            expected_count=expected_branch_count,
                            max_output_bytes=settings.max_output_bytes,
                            max_total_output_bytes=(
                                settings.max_total_output_bytes - total_output_bytes
                            ),
                            max_image_dimension=settings.max_image_dimension,
                            max_image_pixels=settings.max_image_pixels,
                        ),
                    )
                except WorkerOutputError:
                    raise HTTPException(
                        status_code=502,
                        detail="generation output invalid",
                    ) from None

                for output, content in sorted(outputs, key=lambda item: item[0].output_index):
                    output_index = (
                        output.output_index if branch_output_index is None else branch_output_index
                    )
                    grant = grants_by_index[output_index]
                    if output.media_type != grant.content_type:
                        raise HTTPException(
                            status_code=502,
                            detail="generation output invalid",
                        )
                    try:
                        await resolved_uploader.upload(
                            grant=grant,
                            content=content,
                            media_type=output.media_type,
                        )
                    except WorkerUploadError:
                        raise HTTPException(status_code=502, detail="upload failed") from None
                    except Exception:
                        raise HTTPException(status_code=502, detail="upload failed") from None
                    total_output_bytes += len(content)
                    uploaded.append(
                        UploadedOutput(
                            asset_id=grant.asset_id,
                            upload_attempt_id=grant.upload_attempt_id,
                            output_index=output_index,
                        )
                    )

            response = GenerateResponse(
                job_id=envelope.payload.job_id,
                attempt_id=envelope.payload.attempt_id,
                outputs=uploaded,
            )
            while len(replay_cache) >= settings.max_replay_entries:
                replay_cache.popitem(last=False)
            replay_cache[envelope.payload.attempt_id] = (
                envelope.expires_at + settings.clock_skew_seconds,
                envelope.signature,
                response,
            )
            return response

    return app
