from __future__ import annotations

import asyncio
import hashlib
import json
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
from PIL import Image, UnidentifiedImageError

from gen_automation.i2v_worker.models import (
    DownloadGrant,
    GenerationSettings,
    InputSnapshot,
    UploadGrant,
)


class MediaError(Exception):
    """A redacted media transfer or verification failure."""


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


def encode_video(
    frames: tuple[Path, ...],
    settings: GenerationSettings,
    job_root: Path,
) -> tuple[Path, dict[str, Any]]:
    if len(frames) != settings.frame_count:
        raise MediaError("generated frame count is invalid")
    frame_root = job_root / "frames"
    frame_root.mkdir(parents=True, exist_ok=False)
    for index, source in enumerate(frames):
        target = frame_root / f"frame-{index:06d}.png"
        try:
            shutil.copyfile(source, target)
        except OSError:
            raise MediaError("generated frame materialization failed") from None
    output = job_root / "output.mp4"
    command = (
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
        str(settings.frame_count),
        "-c:v",
        "libx264",
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
    metadata = _probe_video(output, settings)
    return output, metadata


def _probe_video(path: Path, settings: GenerationSettings) -> dict[str, Any]:
    command = (
        "ffprobe",
        "-v",
        "error",
        "-count_frames",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=codec_name,pix_fmt,width,height,avg_frame_rate,nb_read_frames:format=duration",
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
            or int(stream["width"]) != settings.width
            or int(stream["height"]) != settings.height
            or int(stream["nb_read_frames"]) != settings.frame_count
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
        "width": settings.width,
        "height": settings.height,
        "frame_count": settings.frame_count,
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
