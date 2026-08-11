import hashlib
import json
import math
import re
import stat
import struct
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError

from gen_automation.video_worker.models import SourceDownloadGrant
from gen_automation.video_worker.profiles import VideoProfile, VideoRenderSpec

_IMAGE_FORMATS = {
    "image/png": "PNG",
    "image/jpeg": "JPEG",
    "image/webp": "WEBP",
}
_MAX_PROBE_OUTPUT_BYTES = 128 * 1024


class SourceImageError(Exception):
    """Raised when downloaded bytes do not meet the signed image contract."""


class VideoOutputError(Exception):
    """Raised when a generated file is not the pinned MP4 contract."""


@dataclass(frozen=True, slots=True)
class ValidatedVideo:
    sha256: str
    size_bytes: int
    width: int
    height: int
    duration_seconds: float
    fps: float


@dataclass(frozen=True, slots=True)
class ValidatedSourceImage:
    width: int
    height: int
    logical_width: int
    logical_height: int


def validate_source_image(
    path: Path,
    *,
    grant: SourceDownloadGrant,
    max_dimension: int,
    max_pixels: int,
) -> ValidatedSourceImage:
    try:
        status = path.lstat()
        if not stat.S_ISREG(status.st_mode) or status.st_size != grant.size_bytes:
            raise SourceImageError("source image invalid")
        with Image.open(path) as image:
            raw_width = image.width
            raw_height = image.height
            if (
                image.format != _IMAGE_FORMATS[grant.content_type]
                or image.width < 1
                or image.height < 1
                or getattr(image, "n_frames", 1) != 1
            ):
                raise SourceImageError("source image invalid")
            image.verify()
        with Image.open(path) as image:
            orientation = image.getexif().get(274, 1)
            logical_width, logical_height = (
                (image.height, image.width)
                if orientation in {5, 6, 7, 8}
                else (image.width, image.height)
            )
        if (
            logical_width > max_dimension
            or logical_height > max_dimension
            or logical_width * logical_height > max_pixels
        ):
            raise SourceImageError("source image invalid")
    except SourceImageError:
        raise
    except (
        Image.DecompressionBombError,
        UnidentifiedImageError,
        OSError,
        SyntaxError,
        ValueError,
    ):
        raise SourceImageError("source image invalid") from None
    return ValidatedSourceImage(
        width=raw_width,
        height=raw_height,
        logical_width=logical_width,
        logical_height=logical_height,
    )


type ProbeRunner = Callable[[list[str], float], subprocess.CompletedProcess[str]]


def _default_probe_runner(command: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed executable and fixed argument structure
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


@dataclass(slots=True)
class FfprobeMp4Validator:
    ffprobe_path: str = "/usr/bin/ffprobe"
    timeout_seconds: float = 15.0
    runner: ProbeRunner = _default_probe_runner

    def validate(
        self,
        *,
        path: Path,
        profile: VideoProfile,
        render_spec: VideoRenderSpec,
        max_bytes: int,
    ) -> ValidatedVideo:
        try:
            status = path.lstat()
        except OSError:
            raise VideoOutputError("video output invalid") from None
        if not stat.S_ISREG(status.st_mode) or status.st_size < 1 or status.st_size > max_bytes:
            raise VideoOutputError("video output invalid")
        if profile.require_faststart:
            _require_faststart(path, status.st_size)

        command = [
            self.ffprobe_path,
            "-v",
            "error",
            "-count_frames",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            "--",
            str(path),
        ]
        try:
            result = self.runner(command, self.timeout_seconds)
        except (OSError, subprocess.SubprocessError):
            raise VideoOutputError("video output invalid") from None
        if result.returncode != 0 or len(result.stdout.encode("utf-8")) > _MAX_PROBE_OUTPUT_BYTES:
            raise VideoOutputError("video output invalid")
        try:
            probe: Any = json.loads(result.stdout)
            if not isinstance(probe, dict):
                raise ValueError
            streams = probe["streams"]
            container = probe["format"]
            if not isinstance(streams, list) or not isinstance(container, dict):
                raise ValueError
            video_streams = [
                stream
                for stream in streams
                if isinstance(stream, dict) and stream.get("codec_type") == "video"
            ]
            if len(video_streams) != 1:
                raise ValueError
            video = video_streams[0]
            format_names = str(container["format_name"]).split(",")
            if "mp4" not in format_names:
                raise ValueError
            if video.get("codec_name") != profile.output_codec:
                raise ValueError
            if video.get("pix_fmt") != profile.output_pixel_format:
                raise ValueError

            width = _positive_int(video["width"])
            height = _positive_int(video["height"])
            if (
                width % 2
                or height % 2
                or width != render_spec.output_width
                or height != render_spec.output_height
            ):
                raise ValueError
            frame_rate = _frame_rate(video.get("avg_frame_rate") or video["r_frame_rate"])
            if not math.isclose(frame_rate, float(render_spec.fps), rel_tol=0.0, abs_tol=0.01):
                raise ValueError
            duration = _positive_float(container["duration"])
            if not math.isclose(
                duration,
                render_spec.output_duration_seconds,
                rel_tol=0.0,
                abs_tol=(1 / render_spec.fps),
            ):
                raise ValueError
            if "size" in container and _positive_int(container["size"]) != status.st_size:
                raise ValueError
            if _positive_int(video["nb_read_frames"]) != render_spec.output_frame_count:
                raise ValueError
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            raise VideoOutputError("video output invalid") from None

        return ValidatedVideo(
            sha256=_hash_file(path),
            size_bytes=status.st_size,
            width=width,
            height=height,
            duration_seconds=duration,
            fps=frame_rate,
        )


def _positive_int(value: object) -> int:
    if isinstance(value, bool):
        raise ValueError
    if isinstance(value, int):
        result = value
    elif isinstance(value, str) and re.fullmatch(r"[1-9][0-9]*", value):
        result = int(value)
    else:
        raise ValueError
    if result <= 0:
        raise ValueError
    return result


def _positive_float(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ValueError
    return result


def _frame_rate(value: object) -> float:
    if not isinstance(value, str) or len(value) > 32:
        raise ValueError
    rate = Fraction(value)
    if rate <= 0:
        raise ValueError
    return float(rate)


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _require_faststart(path: Path, file_size: int) -> None:
    moov_offset: int | None = None
    mdat_offset: int | None = None
    offset = 0
    try:
        with path.open("rb") as source:
            while offset < file_size:
                source.seek(offset)
                header = source.read(8)
                if len(header) != 8:
                    raise VideoOutputError("video output invalid")
                box_size, box_type = struct.unpack(">I4s", header)
                header_size = 8
                if box_size == 1:
                    extended = source.read(8)
                    if len(extended) != 8:
                        raise VideoOutputError("video output invalid")
                    box_size = struct.unpack(">Q", extended)[0]
                    header_size = 16
                elif box_size == 0:
                    box_size = file_size - offset
                if box_size < header_size or box_size > file_size - offset:
                    raise VideoOutputError("video output invalid")
                if box_type == b"moov" and moov_offset is None:
                    moov_offset = offset
                elif box_type == b"mdat" and mdat_offset is None:
                    mdat_offset = offset
                offset += box_size
    except VideoOutputError:
        raise
    except OSError:
        raise VideoOutputError("video output invalid") from None
    if (
        offset != file_size
        or moov_offset is None
        or mdat_offset is None
        or moov_offset > mdat_offset
    ):
        raise VideoOutputError("video output invalid")
