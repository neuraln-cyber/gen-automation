from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import shutil
import struct
import subprocess
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx2
from PIL import Image, ImageOps, UnidentifiedImageError

from gen_automation.i2v_worker.models import (
    DownloadGrant,
    GenerationSettings,
    InputSnapshot,
    UploadGrant,
)


class MediaError(Exception):
    """A redacted media transfer or verification failure."""


_WAN_MIN_NATIVE_PIXELS = 520_000
_WAN_MAX_NATIVE_PIXELS = 830_000
_WAN_MAX_NATIVE_SIDE = 1_024


def validate_grant_url(url: str, *, allow_http: bool) -> None:
    try:
        parsed = urlsplit(url)
        _ = parsed.port
    except ValueError:
        raise MediaError("object grant is invalid") from None
    allowed_schemes = {"https"} | ({"http"} if allow_http else set())
    if (
        parsed.scheme not in allowed_schemes
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise MediaError("object grant is invalid")


async def download_input(
    grant: DownloadGrant,
    snapshot: InputSnapshot,
    destination: Path,
    *,
    timeout_seconds: float,
    attempts: int,
    allow_http: bool,
) -> None:
    if grant.expires_at.astimezone(UTC) <= datetime.now(UTC):
        raise MediaError("input grant expired")
    validate_grant_url(str(grant.url), allow_http=allow_http)
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(f".{destination.name}.download")
    for attempt in range(attempts):
        digest = hashlib.sha256()
        size = 0
        try:
            async with httpx2.AsyncClient(
                follow_redirects=False,
                trust_env=False,
                timeout=httpx2.Timeout(timeout_seconds, connect=min(timeout_seconds, 30)),
            ) as client:
                async with client.stream("GET", str(grant.url)) as response:
                    if response.status_code != 200:
                        raise MediaError("input download failed")
                    declared = response.headers.get("content-length")
                    if declared is not None and int(declared) != snapshot.byte_size:
                        raise MediaError("input download failed")
                    with partial.open("wb") as output:
                        async for chunk in response.aiter_bytes(1024 * 1024):
                            size += len(chunk)
                            if size > snapshot.byte_size:
                                raise MediaError("input download failed")
                            digest.update(chunk)
                            output.write(chunk)
                        output.flush()
                        os.fsync(output.fileno())
            if size != snapshot.byte_size or digest.hexdigest() != snapshot.sha256:
                raise MediaError("input download failed")
            _verify_image(partial, snapshot)
            os.replace(partial, destination)
            return
        except MediaError:
            if partial.exists():
                partial.unlink()
            if attempt + 1 >= attempts:
                raise
        except (OSError, ValueError, httpx2.HTTPError):
            if partial.exists():
                partial.unlink()
            if attempt + 1 >= attempts:
                raise MediaError("input download failed") from None
        await asyncio.sleep(min(2**attempt, 16))
    raise MediaError("input download failed")


def _verify_image(path: Path, snapshot: InputSnapshot) -> None:
    expected = {
        "image/png": "PNG",
        "image/jpeg": "JPEG",
        "image/webp": "WEBP",
    }[snapshot.content_type]
    try:
        with Image.open(path) as image:
            if (
                image.format != expected
                or image.width != snapshot.width
                or image.height != snapshot.height
                or getattr(image, "n_frames", 1) != 1
            ):
                raise MediaError("input image is invalid")
            image.verify()
    except MediaError:
        raise
    except (Image.DecompressionBombError, UnidentifiedImageError, OSError, ValueError):
        raise MediaError("input image is invalid") from None


def resolve_generation_settings(
    settings: GenerationSettings,
    *,
    source_width: int,
    source_height: int,
) -> GenerationSettings:
    if not settings.match_source_aspect:
        return settings
    width, height = _source_aspect_native_dimensions(source_width, source_height)
    return settings.model_copy(update={"width": width, "height": height})


def _source_aspect_native_dimensions(source_width: int, source_height: int) -> tuple[int, int]:
    if source_width <= 0 or source_height <= 0:
        raise MediaError("source dimensions are invalid")
    source_ratio = source_width / source_height
    candidates: list[tuple[float, int, int, int]] = []
    for width in range(32, _WAN_MAX_NATIVE_SIDE + 1, 32):
        for height in range(32, _WAN_MAX_NATIVE_SIDE + 1, 32):
            pixels = width * height
            if _WAN_MIN_NATIVE_PIXELS <= pixels <= _WAN_MAX_NATIVE_PIXELS:
                ratio_error = abs(math.log((width / height) / source_ratio))
                candidates.append((ratio_error, -pixels, width, height))
    if not candidates:
        raise MediaError("no WAN generation dimensions are available")
    _error, _negative_pixels, width, height = min(candidates)
    return width, height


def prepare_input_image(
    source: Path,
    destination: Path,
    *,
    width: int,
    height: int,
) -> None:
    """Contain a verified source in the WAN canvas without discarding pixels."""

    try:
        with Image.open(source) as opened:
            image = ImageOps.exif_transpose(opened).convert("RGB")
        fitted = ImageOps.contain(image, (width, height), Image.Resampling.LANCZOS)
        left = (width - fitted.width) // 2
        top = (height - fitted.height) // 2
        canvas = Image.new("RGB", (width, height))
        canvas.paste(fitted, (left, top))
        right = width - left - fitted.width
        bottom = height - top - fitted.height
        if left:
            edge = fitted.crop((0, 0, 1, fitted.height)).resize((left, fitted.height))
            canvas.paste(edge, (0, top))
        if right:
            edge = fitted.crop((fitted.width - 1, 0, fitted.width, fitted.height)).resize(
                (right, fitted.height)
            )
            canvas.paste(edge, (left + fitted.width, top))
        if top:
            edge = canvas.crop((0, top, width, top + 1)).resize((width, top))
            canvas.paste(edge, (0, 0))
        if bottom:
            edge = canvas.crop((0, top + fitted.height - 1, width, top + fitted.height)).resize(
                (width, bottom)
            )
            canvas.paste(edge, (0, top + fitted.height))
        destination.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(destination, format="PNG")
    except (OSError, ValueError):
        raise MediaError("input image preparation failed") from None


def encode_video(
    frames: tuple[Path, ...],
    settings: GenerationSettings,
    job_root: Path,
    *,
    source_width: int,
    source_height: int,
) -> tuple[Path, dict[str, Any]]:
    if len(frames) != settings.frame_count:
        raise MediaError("generated frame count is invalid")
    target_width, target_height = _output_dimensions(
        settings,
        source_width=source_width,
        source_height=source_height,
    )
    frame_indices = _output_frame_indices(settings)
    frame_root = job_root / "frames"
    frame_root.mkdir(parents=True, exist_ok=False)
    for index, source_index in enumerate(frame_indices):
        source = frames[source_index]
        target = frame_root / f"frame-{index:06d}.png"
        try:
            shutil.copyfile(source, target)
        except OSError:
            raise MediaError("generated frame materialization failed") from None
    output = job_root / "output.mp4"
    command: tuple[str, ...] = (
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-framerate",
        str(settings.fps),
        "-start_number",
        "0",
        "-i",
        (frame_root / "frame-%06d.png").as_posix(),
        "-frames:v",
        str(len(frame_indices)),
        "-c:v",
        "libx264",
    )
    output_filter = _delivery_scale_filter(
        native_width=settings.width,
        native_height=settings.height,
        target_width=target_width,
        target_height=target_height,
    )
    if output_filter is not None:
        command += (
            "-vf",
            output_filter,
        )
    command += (
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        "-an",
        output.as_posix(),
    )
    try:
        subprocess.run(  # noqa: S603
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=True,
            close_fds=True,
        )
    except (OSError, subprocess.CalledProcessError):
        raise MediaError("video encoding failed") from None
    metadata = _probe_video(
        output,
        settings,
        width=target_width,
        height=target_height,
        frame_count=len(frame_indices),
    )
    metadata.update(
        {
            "native_width": settings.width,
            "native_height": settings.height,
            "loop_mode": "ping_pong" if settings.loop else "none",
            "loop_count": settings.loop_count if settings.loop else 1,
            "upscale": settings.upscale,
            "source_fit": "contain_edge_pad",
            "match_source_aspect": settings.match_source_aspect,
        }
    )
    return output, metadata


def _output_dimensions(
    settings: GenerationSettings,
    *,
    source_width: int,
    source_height: int,
) -> tuple[int, int]:
    if settings.upscale == "none":
        return settings.width, settings.height
    # H.264 yuv420p requires even dimensions. Preserve an even source exactly;
    # for an odd source, choose the nearest smaller encodable dimensions.
    width = source_width - (source_width % 2)
    height = source_height - (source_height % 2)
    if width < 2 or height < 2:
        raise MediaError("source dimensions cannot be encoded")
    return width, height


def _delivery_scale_filter(
    *,
    native_width: int,
    native_height: int,
    target_width: int,
    target_height: int,
) -> str | None:
    if (target_width, target_height) == (native_width, native_height):
        return None
    # The source was contained in the native canvas. Scale the encoded result
    # until it covers the exact source-sized target, then crop only the small
    # edge-padding discrepancy instead of stretching the generated subject.
    return (
        f"scale={target_width}:{target_height}:flags=lanczos:"
        "force_original_aspect_ratio=increase,"
        f"crop={target_width}:{target_height},setsar=1"
    )


def _output_frame_indices(settings: GenerationSettings) -> tuple[int, ...]:
    forward = tuple(range(settings.frame_count))
    if not settings.loop:
        return forward
    # Omitting both endpoints on the reverse leg makes every cycle boundary a
    # normal adjacent-frame transition: ... 2, 1, 0, 1, 2 ...
    cycle = forward + tuple(range(settings.frame_count - 2, 0, -1))
    return cycle * settings.loop_count


def _probe_video(
    path: Path,
    settings: GenerationSettings,
    *,
    width: int,
    height: int,
    frame_count: int,
) -> dict[str, Any]:
    command = (
        "ffprobe",
        "-v",
        "error",
        "-count_frames",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=codec_name,pix_fmt,width,height,sample_aspect_ratio,avg_frame_rate,nb_read_frames:format=duration",
        "-of",
        "json",
        path.as_posix(),
    )
    try:
        result = subprocess.run(  # noqa: S603
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=True,
            close_fds=True,
        )
        payload = json.loads(result.stdout)
        stream = payload["streams"][0]
        duration = float(payload["format"]["duration"])
        numerator, denominator = map(int, stream["avg_frame_rate"].split("/", 1))
        fps = numerator / denominator
        if (
            stream["codec_name"] != "h264"
            or stream["pix_fmt"] != "yuv420p"
            or int(stream["width"]) != width
            or int(stream["height"]) != height
            or stream.get("sample_aspect_ratio") not in {"1:1", "N/A"}
            or int(stream["nb_read_frames"]) != frame_count
            or abs(fps - settings.fps) > 0.001
            or duration <= 0
            or not _has_faststart(path)
        ):
            raise ValueError
    except (
        OSError,
        ValueError,
        KeyError,
        IndexError,
        subprocess.CalledProcessError,
        json.JSONDecodeError,
    ):
        raise MediaError("encoded video contract is invalid") from None
    return {
        "width": width,
        "height": height,
        "frame_count": frame_count,
        "fps": fps,
        "duration_ms": round(duration * 1000),
        "codec": "h264",
        "pixel_format": "yuv420p",
        "faststart": True,
    }


def _has_faststart(path: Path) -> bool:
    moov_offset: int | None = None
    mdat_offset: int | None = None
    try:
        size = path.stat().st_size
        offset = 0
        with path.open("rb") as stream:
            while offset + 8 <= size:
                stream.seek(offset)
                header = stream.read(16)
                if len(header) < 8:
                    return False
                box_size = struct.unpack(">I", header[:4])[0]
                box_type = header[4:8]
                header_size = 8
                if box_size == 1:
                    if len(header) < 16:
                        return False
                    box_size = struct.unpack(">Q", header[8:16])[0]
                    header_size = 16
                elif box_size == 0:
                    box_size = size - offset
                if box_size < header_size or offset + box_size > size:
                    return False
                if box_type == b"moov":
                    moov_offset = offset
                if box_type == b"mdat":
                    mdat_offset = offset
                    break
                offset += box_size
    except OSError:
        return False
    return moov_offset is not None and mdat_offset is not None and moov_offset < mdat_offset


async def upload_video(
    path: Path,
    grant: UploadGrant,
    *,
    timeout_seconds: float,
    attempts: int,
    allow_http: bool,
) -> tuple[str | None, int, str]:
    if grant.expires_at.astimezone(UTC) <= datetime.now(UTC):
        raise MediaError("output grant expired")
    validate_grant_url(str(grant.url), allow_http=allow_http)
    size = (await asyncio.to_thread(path.stat)).st_size
    sha256 = await asyncio.to_thread(_sha256, path)
    headers = {**grant.headers, "Content-Length": str(size)}
    for attempt in range(attempts):
        try:
            async with httpx2.AsyncClient(
                follow_redirects=False,
                trust_env=False,
                timeout=httpx2.Timeout(timeout_seconds, connect=min(timeout_seconds, 30)),
            ) as client:
                response = await client.put(
                    str(grant.url),
                    headers=headers,
                    content=_file_chunks(path),
                )
            if response.status_code in {200, 201, 204}:
                return response.headers.get("x-amz-version-id"), size, sha256
            if response.status_code not in {408, 425, 429, 500, 502, 503, 504}:
                raise MediaError("output upload failed")
        except MediaError:
            raise
        except (OSError, httpx2.HTTPError):
            pass
        if attempt + 1 < attempts:
            await asyncio.sleep(min(2**attempt, 16))
    raise MediaError("output upload failed")


async def _file_chunks(path: Path) -> AsyncIterator[bytes]:
    with path.open("rb") as stream:
        while chunk := await asyncio.to_thread(stream.read, 1024 * 1024):
            yield chunk


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()
