import hashlib
import hmac
import os
import stat
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx2

from gen_automation.video_worker.models import SourceDownloadGrant, VideoUploadGrant
from gen_automation.video_worker.profiles import VideoProfile, VideoRenderSpec


class SourceDownloader(Protocol):
    async def download(self, *, grant: SourceDownloadGrant, destination: Path) -> None: ...


class VideoExecutor(Protocol):
    def is_ready(self) -> bool: ...

    def render(
        self,
        *,
        profile: VideoProfile,
        render_spec: VideoRenderSpec,
        source_path: Path,
        native_frames_path: Path,
        prompt: str,
        negative_prompt: str,
        seed: int,
    ) -> None: ...


class VideoUploader(Protocol):
    async def upload(self, *, grant: VideoUploadGrant, source: Path) -> None: ...


class VideoValidator(Protocol):
    def validate(
        self,
        *,
        path: Path,
        profile: VideoProfile,
        render_spec: VideoRenderSpec,
        max_bytes: int,
    ) -> object: ...


class LoopEncoder(Protocol):
    def encode(
        self,
        *,
        native_frames_path: Path,
        output_path: Path,
        render_spec: VideoRenderSpec,
    ) -> None: ...


class SourceDownloadError(Exception):
    """A redacted source-download failure safe for the service layer."""


class VideoExecutionError(Exception):
    """A redacted adapter failure safe for the service layer."""


class VideoUploadError(Exception):
    """A redacted output-upload failure safe for the service layer."""


class LoopEncodingError(Exception):
    """Raised when fixed local MP4 encoding fails."""


@dataclass(slots=True)
class HttpxSourceDownloader:
    client: httpx2.AsyncClient
    timeout_seconds: float

    async def download(self, *, grant: SourceDownloadGrant, destination: Path) -> None:
        digest = hashlib.sha256()
        observed_size = 0
        succeeded = False
        try:
            async with self.client.stream(
                "GET",
                grant.url,
                headers={"accept-encoding": "identity"},
                timeout=self.timeout_seconds,
                follow_redirects=False,
            ) as response:
                if response.status_code != 200:
                    raise SourceDownloadError("source download failed")
                content_encoding = response.headers.get("content-encoding", "identity").lower()
                if content_encoding not in {"", "identity"}:
                    raise SourceDownloadError("source download failed")
                response_type = response.headers.get("content-type", "").split(";", 1)[0]
                if response_type.strip().lower() != grant.content_type:
                    raise SourceDownloadError("source download failed")
                raw_length = response.headers.get("content-length")
                if raw_length is not None:
                    try:
                        declared_length = int(raw_length)
                    except ValueError:
                        raise SourceDownloadError("source download failed") from None
                    if declared_length != grant.size_bytes:
                        raise SourceDownloadError("source download failed")

                destination.parent.mkdir(parents=True, exist_ok=True)
                with destination.open("xb") as output:
                    # Content codings are rejected above, so decoded bytes are
                    # byte-for-byte identical to the signed source object.
                    async for chunk in response.aiter_bytes():
                        observed_size += len(chunk)
                        if observed_size > grant.size_bytes:
                            raise SourceDownloadError("source download failed")
                        digest.update(chunk)
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())

            if observed_size != grant.size_bytes or not hmac.compare_digest(
                digest.hexdigest(),
                grant.sha256,
            ):
                raise SourceDownloadError("source download failed")
            succeeded = True
        except SourceDownloadError:
            raise
        except (httpx2.TimeoutException, httpx2.TransportError, OSError):
            raise SourceDownloadError("source download failed") from None
        finally:
            if not succeeded:
                try:
                    os.unlink(destination)
                except FileNotFoundError:
                    pass
                except OSError:
                    pass


@dataclass(slots=True)
class HttpxVideoUploader:
    client: httpx2.AsyncClient
    timeout_seconds: float

    async def upload(self, *, grant: VideoUploadGrant, source: Path) -> None:
        try:
            with source.open("rb") as video:
                response = await self.client.post(
                    grant.url,
                    data=_copy_fields(grant.fields),
                    files={"file": ("output.mp4", video, grant.content_type)},
                    timeout=self.timeout_seconds,
                    follow_redirects=False,
                )
        except (httpx2.TimeoutException, httpx2.TransportError, OSError):
            raise VideoUploadError("upload failed") from None
        if response.status_code not in {200, 201, 204}:
            raise VideoUploadError("upload failed")


type EncoderRunner = Callable[[list[str], float], subprocess.CompletedProcess[bytes]]


def _default_encoder_runner(
    command: list[str],
    timeout: float,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(  # noqa: S603 - fixed binary/arguments, no shell
        command,
        check=False,
        capture_output=True,
        timeout=timeout,
    )


@dataclass(slots=True)
class FfmpegPingPongEncoder:
    ffmpeg_path: str = "/usr/bin/ffmpeg"
    timeout_seconds: float = 300.0
    runner: EncoderRunner = _default_encoder_runner

    def encode(
        self,
        *,
        native_frames_path: Path,
        output_path: Path,
        render_spec: VideoRenderSpec,
    ) -> None:
        try:
            frame_paths = sorted(native_frames_path.glob("frame-*.png"))
            if len(frame_paths) != render_spec.native_frame_count:
                raise LoopEncodingError("video encoding failed")
            for frame_path in frame_paths:
                status = frame_path.lstat()
                if not stat.S_ISREG(status.st_mode) or status.st_size < 1:
                    raise LoopEncodingError("video encoding failed")
            concat_path = native_frames_path / "ping-pong.ffconcat"
            lines = ["ffconcat version 1.0"]
            forward_and_reverse = frame_paths + frame_paths[-2:0:-1]
            frame_duration = 1 / render_spec.fps
            for frame_path in forward_and_reverse:
                lines.append(f"file '{frame_path.name}'")
                lines.append(f"duration {frame_duration:.12f}")
            # The concat demuxer needs a final repeated file for the preceding
            # duration to be honored; -frames:v excludes that sentinel frame.
            lines.append(f"file '{forward_and_reverse[-1].name}'")
            concat_path.write_text("\n".join(lines) + "\n", encoding="ascii")
            result = self.runner(
                [
                    self.ffmpeg_path,
                    "-nostdin",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "concat",
                    "-safe",
                    "1",
                    "-i",
                    str(concat_path),
                    "-an",
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    "-r",
                    str(render_spec.fps),
                    "-frames:v",
                    str(render_spec.output_frame_count),
                    "-movflags",
                    "+faststart",
                    "-y",
                    str(output_path),
                ],
                self.timeout_seconds,
            )
            if result.returncode != 0:
                raise LoopEncodingError("video encoding failed")
        except LoopEncodingError:
            raise
        except (OSError, subprocess.SubprocessError):
            raise LoopEncodingError("video encoding failed") from None
        finally:
            try:
                os.unlink(native_frames_path / "ping-pong.ffconcat")
            except FileNotFoundError:
                pass
            except OSError:
                pass


def _copy_fields(fields: Mapping[str, str]) -> dict[str, str]:
    return dict(fields)
