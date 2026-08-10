import asyncio
import json
import math
import tempfile
import time
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx2
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from gen_automation.video_worker.media import (
    FfprobeMp4Validator,
    SourceImageError,
    ValidatedSourceImage,
    ValidatedVideo,
    VideoOutputError,
    validate_source_image,
)
from gen_automation.video_worker.models import (
    AnimateEnvelope,
    AnimateResponse,
    WorkerSettings,
    validate_grant_url,
)
from gen_automation.video_worker.profiles import PINNED_VIDEO_PROFILE, VideoRenderSpec
from gen_automation.video_worker.runtime import (
    FfmpegPingPongEncoder,
    HttpxSourceDownloader,
    HttpxVideoUploader,
    LoopEncoder,
    LoopEncodingError,
    SourceDownloader,
    SourceDownloadError,
    VideoExecutor,
    VideoUploader,
    VideoUploadError,
    VideoValidator,
)
from gen_automation.video_worker.security import AuthorizationError, verify_authorization

MAX_JSON_DEPTH = 32
_SOURCE_SUFFIXES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
}


class WorkerRequestError(Exception):
    pass


class WorkerNotReadyError(Exception):
    pass


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


def _parse_envelope(body: bytes) -> AnimateEnvelope:
    try:
        raw: Any = json.loads(body, object_pairs_hook=_unique_json_object)
        validated = _validate_json(raw)
        if not isinstance(validated, dict):
            raise WorkerRequestError("invalid request")
        return AnimateEnvelope.model_validate(validated, strict=True)
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError, ValidationError):
        raise WorkerRequestError("invalid request") from None


def _render_if_ready(
    executor: VideoExecutor,
    *,
    source_path: Path,
    native_frames_path: Path,
    render_spec: VideoRenderSpec,
    prompt: str,
    negative_prompt: str,
    seed: int,
) -> None:
    if not executor.is_ready():
        raise WorkerNotReadyError
    executor.render(
        profile=PINNED_VIDEO_PROFILE,
        render_spec=render_spec,
        source_path=source_path,
        native_frames_path=native_frames_path,
        prompt=prompt,
        negative_prompt=negative_prompt,
        seed=seed,
    )


def create_video_worker_app(
    *,
    settings: WorkerSettings,
    executor: VideoExecutor,
    downloader: SourceDownloader | None = None,
    uploader: VideoUploader | None = None,
    validator: VideoValidator | None = None,
    loop_encoder: LoopEncoder | None = None,
    now: Callable[[], float] = time.time,
) -> FastAPI:
    if settings is None or executor is None:
        raise RuntimeError("video worker settings and executor are required")

    owned_client: httpx2.AsyncClient | None = None
    execution_lock = asyncio.Lock()
    execution_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="video-gpu")
    readiness_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="video-ready")
    replay_cache: OrderedDict[str, tuple[int, str, AnimateResponse]] = OrderedDict()

    resolved_downloader = downloader
    resolved_uploader = uploader
    if resolved_downloader is None or resolved_uploader is None:
        owned_client = httpx2.AsyncClient(follow_redirects=False, trust_env=False)
    if resolved_downloader is None:
        assert owned_client is not None
        resolved_downloader = HttpxSourceDownloader(
            client=owned_client,
            timeout_seconds=settings.download_timeout_seconds,
        )
    if resolved_uploader is None:
        assert owned_client is not None
        resolved_uploader = HttpxVideoUploader(
            client=owned_client,
            timeout_seconds=settings.upload_timeout_seconds,
        )
    resolved_validator = validator or FfprobeMp4Validator(
        timeout_seconds=settings.ffprobe_timeout_seconds
    )
    resolved_loop_encoder = loop_encoder or FfmpegPingPongEncoder(
        timeout_seconds=settings.ffmpeg_timeout_seconds
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):  # type: ignore[no-untyped-def]
        try:
            yield
        finally:
            await asyncio.to_thread(execution_pool.shutdown, wait=True, cancel_futures=True)
            await asyncio.to_thread(readiness_pool.shutdown, wait=True, cancel_futures=True)
            if owned_client is not None:
                await owned_client.aclose()

    app = FastAPI(
        title="Animation Video Worker",
        version="video-worker.v1",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": "video-worker.v1"}

    @app.get("/ready")
    async def ready() -> JSONResponse:
        try:
            loop = asyncio.get_running_loop()
            executor_ready = await asyncio.wait_for(
                loop.run_in_executor(readiness_pool, executor.is_ready),
                timeout=settings.readiness_timeout_seconds,
            )
            staging_ready = settings.staging_root.is_dir()
        except Exception:
            executor_ready = False
            staging_ready = False
        is_ready = executor_ready and staging_ready
        return JSONResponse(
            status_code=200 if is_ready else 503,
            content={
                "status": "ready" if is_ready else "not_ready",
                "version": "video-worker.v1",
            },
        )

    @app.post("/jobs/generate", response_model=AnimateResponse)
    async def animate(request: Request) -> AnimateResponse:
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

        payload = envelope.payload
        render_spec = VideoRenderSpec(
            native_frame_count=payload.native_frame_count,
            fps=payload.fps,
            width=payload.width,
            height=payload.height,
            loop_mode=payload.loop_mode,
        )
        try:
            if payload.source.size_bytes > settings.max_source_bytes:
                raise ValueError
            validate_grant_url(payload.source.url, settings.source_origins)
            validate_grant_url(payload.upload.url, frozenset({settings.upload_origin}))
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid request") from None

        async with execution_lock:
            current_time = int(now())
            for attempt_id in [
                key
                for key, (expires_at, _signature, _response) in replay_cache.items()
                if expires_at < current_time
            ]:
                replay_cache.pop(attempt_id, None)
            cached = replay_cache.get(payload.attempt_id)
            if cached is not None:
                _expires_at, cached_signature, cached_response = cached
                if cached_signature != envelope.signature:
                    raise HTTPException(
                        status_code=409,
                        detail="request conflicts with completed attempt",
                    )
                replay_cache.move_to_end(payload.attempt_id)
                return cached_response

            try:
                with tempfile.TemporaryDirectory(
                    prefix=f"video-{payload.attempt_id}-",
                    dir=settings.staging_root,
                ) as raw_job_directory:
                    job_directory = Path(raw_job_directory)
                    source_suffix = _SOURCE_SUFFIXES[payload.source.content_type]
                    source_path = job_directory / f"source{source_suffix}"
                    native_frames_path = job_directory / "native-frames"
                    native_frames_path.mkdir(mode=0o700)
                    output_path = job_directory / "output.mp4"
                    try:
                        await resolved_downloader.download(
                            grant=payload.source,
                            destination=source_path,
                        )
                    except SourceDownloadError:
                        raise HTTPException(
                            status_code=502,
                            detail="source download failed",
                        ) from None
                    except Exception:
                        raise HTTPException(
                            status_code=502,
                            detail="source download failed",
                        ) from None

                    loop = asyncio.get_running_loop()
                    try:
                        raw_source_image = await loop.run_in_executor(
                            execution_pool,
                            lambda: validate_source_image(
                                source_path,
                                grant=payload.source,
                                max_dimension=settings.max_image_dimension,
                                max_pixels=settings.max_image_pixels,
                            ),
                        )
                        if not isinstance(raw_source_image, ValidatedSourceImage):
                            raise SourceImageError("source image invalid")
                        source_image = raw_source_image
                    except SourceImageError:
                        raise HTTPException(
                            status_code=502,
                            detail="source image invalid",
                        ) from None

                    expected_dimensions = (
                        (
                            PINNED_VIDEO_PROFILE.landscape_width,
                            PINNED_VIDEO_PROFILE.landscape_height,
                        )
                        if source_image.width >= source_image.height
                        else (
                            PINNED_VIDEO_PROFILE.portrait_width,
                            PINNED_VIDEO_PROFILE.portrait_height,
                        )
                    )
                    if (render_spec.width, render_spec.height) != expected_dimensions:
                        raise HTTPException(status_code=400, detail="invalid request")

                    try:
                        await loop.run_in_executor(
                            execution_pool,
                            lambda: _render_if_ready(
                                executor,
                                source_path=source_path,
                                native_frames_path=native_frames_path,
                                render_spec=render_spec,
                                prompt=payload.prompt,
                                negative_prompt=payload.negative_prompt,
                                seed=payload.seed,
                            ),
                        )
                    except WorkerNotReadyError:
                        raise HTTPException(status_code=503, detail="worker not ready") from None
                    except Exception:
                        raise HTTPException(status_code=502, detail="generation failed") from None

                    try:
                        await loop.run_in_executor(
                            execution_pool,
                            lambda: resolved_loop_encoder.encode(
                                native_frames_path=native_frames_path,
                                output_path=output_path,
                                render_spec=render_spec,
                            ),
                        )
                    except LoopEncodingError:
                        raise HTTPException(
                            status_code=502,
                            detail="video encoding failed",
                        ) from None
                    except Exception:
                        raise HTTPException(
                            status_code=502,
                            detail="video encoding failed",
                        ) from None

                    try:
                        raw_validated = await loop.run_in_executor(
                            execution_pool,
                            lambda: resolved_validator.validate(
                                path=output_path,
                                profile=PINNED_VIDEO_PROFILE,
                                render_spec=render_spec,
                                max_bytes=settings.max_output_bytes,
                            ),
                        )
                        if not isinstance(raw_validated, ValidatedVideo):
                            raise VideoOutputError("video output invalid")
                        validated = raw_validated
                    except VideoOutputError:
                        raise HTTPException(
                            status_code=502,
                            detail="generation output invalid",
                        ) from None
                    except Exception:
                        raise HTTPException(
                            status_code=502,
                            detail="generation output invalid",
                        ) from None

                    try:
                        await resolved_uploader.upload(grant=payload.upload, source=output_path)
                    except VideoUploadError:
                        raise HTTPException(status_code=502, detail="upload failed") from None
                    except Exception:
                        raise HTTPException(status_code=502, detail="upload failed") from None
            except HTTPException:
                raise
            except OSError:
                raise HTTPException(status_code=503, detail="worker not ready") from None

            response = AnimateResponse(
                job_id=payload.job_id,
                attempt_id=payload.attempt_id,
                profile_id=PINNED_VIDEO_PROFILE.profile_id,
                source_asset_id=payload.source.asset_id,
                output_asset_id=payload.upload.asset_id,
                upload_attempt_id=payload.upload.upload_attempt_id,
                output_sha256=validated.sha256,
                output_size_bytes=validated.size_bytes,
                fps=render_spec.fps,
                width=render_spec.width,
                height=render_spec.height,
                native_frame_count=render_spec.native_frame_count,
                native_duration_seconds=round(render_spec.native_duration_seconds, 6),
                output_frame_count=render_spec.output_frame_count,
                output_duration_seconds=round(render_spec.output_duration_seconds, 6),
            )
            while len(replay_cache) >= settings.max_replay_entries:
                replay_cache.popitem(last=False)
            replay_cache[payload.attempt_id] = (
                envelope.expires_at + settings.clock_skew_seconds,
                envelope.signature,
                response,
            )
            return response

    return app
