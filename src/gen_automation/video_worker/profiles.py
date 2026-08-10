import hashlib
import json
from dataclasses import asdict, dataclass


@dataclass(frozen=True, slots=True)
class VideoProfile:
    """Immutable contract between the HTTP worker and a model adapter."""

    profile_id: str
    adapter: str
    adapter_revision: str
    model: str
    model_revision: str
    fps: int
    default_native_frame_count: int
    permitted_native_frame_counts: tuple[int, ...]
    landscape_width: int
    landscape_height: int
    portrait_width: int
    portrait_height: int
    loop_mode: str
    output_media_type: str
    output_codec: str
    output_pixel_format: str
    require_faststart: bool

    @property
    def maximum_native_duration_seconds(self) -> float:
        return max(self.permitted_native_frame_counts) / self.fps

    @property
    def descriptor_sha256(self) -> str:
        descriptor = json.dumps(
            asdict(self),
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        return hashlib.sha256(descriptor).hexdigest()


@dataclass(frozen=True, slots=True)
class VideoRenderSpec:
    native_frame_count: int
    fps: int
    width: int
    height: int
    loop_mode: str

    @property
    def output_frame_count(self) -> int:
        # Forward then reverse, without repeating either endpoint.
        return (self.native_frame_count * 2) - 2

    @property
    def native_duration_seconds(self) -> float:
        return self.native_frame_count / self.fps

    @property
    def output_duration_seconds(self) -> float:
        return self.output_frame_count / self.fps


# This is a profile pin, not a claim that model weights are present in the
# skeleton image.  The adapter revision is intentionally explicit so a future
# native-Comfy implementation cannot silently change the workflow contract.
PINNED_VIDEO_PROFILE = VideoProfile(
    profile_id="wan2.2-ti2v-5b-comfy-v1",
    adapter="wan-native-comfy",
    adapter_revision="video-worker-adapter-v1",
    model="Comfy-Org/Wan_2.2_ComfyUI_Repackaged",
    model_revision="fb1388adc906ab39ffc26ee40e96b22886b56bc4",
    fps=24,
    default_native_frame_count=73,
    permitted_native_frame_counts=(73, 121),
    landscape_width=832,
    landscape_height=480,
    portrait_width=480,
    portrait_height=832,
    loop_mode="ping_pong",
    output_media_type="video/mp4",
    output_codec="h264",
    output_pixel_format="yuv420p",
    require_faststart=True,
)

# Replacing any profile property requires an intentional contract update.
PINNED_VIDEO_PROFILE_SHA256 = "a83c946f9a61bac7cf3794fc9aa4debacc2fc676c13957deaed42ecf82c7e2e4"
if PINNED_VIDEO_PROFILE.descriptor_sha256 != PINNED_VIDEO_PROFILE_SHA256:
    raise RuntimeError("pinned video profile descriptor changed")
