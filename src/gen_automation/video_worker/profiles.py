import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from types import MappingProxyType
from typing import Final


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


# The HQ profile is intentionally additive. Existing jobs remain bound to the
# exact v1 descriptor above, including its 480x832/832x480 dimensions and
# 73/121-frame timing contract.
HQ_VIDEO_PROFILE = VideoProfile(
    profile_id="wan2.2-ti2v-5b-comfy-hq-v1",
    adapter="wan-native-comfy",
    adapter_revision="video-worker-adapter-hq-v1",
    model="Comfy-Org/Wan_2.2_ComfyUI_Repackaged",
    model_revision="fb1388adc906ab39ffc26ee40e96b22886b56bc4",
    fps=24,
    default_native_frame_count=73,
    permitted_native_frame_counts=(73,),
    landscape_width=1472,
    landscape_height=1152,
    portrait_width=1152,
    portrait_height=1472,
    loop_mode="ping_pong",
    output_media_type="video/mp4",
    output_codec="h264",
    output_pixel_format="yuv420p",
    require_faststart=True,
)
HQ_VIDEO_PROFILE_SHA256 = "d690fb7b0fc883075e0a73fc711ae3c021b34044c57b6fb6e74dbf25bbf1b22c"
if HQ_VIDEO_PROFILE.descriptor_sha256 != HQ_VIDEO_PROFILE_SHA256:
    raise RuntimeError("HQ video profile descriptor changed")

PINNED_VIDEO_WORKFLOW_SHA256: Final = (
    "ecba3eef1c14abcd4d0d2ba3cec1f53042767f1a177d833dcfd3fd6b11f09ab3"
)
HQ_VIDEO_WORKFLOW_SHA256: Final = "01c5b5350cab319e5cf7ec407971d7fbb269c4eb1e3c91c27c5c3cbb793a5151"


_V1_BUILT_IN_NEGATIVE_PROMPT: Final = (
    "overexposed, static, blurry details, subtitles, worst quality, low quality, "
    "jpeg artifacts, malformed anatomy, extra limbs, fused fingers, duplicate people, "
    "flicker, frame corruption"
)
_HQ_BUILT_IN_NEGATIVE_PROMPT: Final = (
    "overexposed, blurry details, subtitles, worst quality, low quality, jpeg artifacts, "
    "malformed anatomy, extra limbs, fused fingers, duplicate people, flicker, frame "
    "corruption, camera shake, handheld camera, moving camera, camera movement, pan, tilt, "
    "roll, zoom, dolly, orbit, reframing, perspective change, background movement, "
    "background drift, horizon drift, global translation, whole-frame motion, jitter, "
    "judder, frame-to-frame warping, image-boundary motion"
)


def _execution_contract_sha256(
    *,
    profile_sha256: str,
    workflow_sha256: str,
    built_in_negative_prompt: str,
    execution_timeout_seconds: int,
    planning_runtime_seconds: tuple[tuple[int, int], ...],
    max_attempts: int,
) -> str:
    encoded = json.dumps(
        {
            "schema": "video-profile-execution/v1",
            "profile_sha256": profile_sha256,
            "workflow_sha256": workflow_sha256,
            "built_in_negative_prompt": built_in_negative_prompt,
            "execution_timeout_seconds": execution_timeout_seconds,
            "planning_runtime_seconds": planning_runtime_seconds,
            "max_attempts": max_attempts,
        },
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


HQ_VIDEO_EXECUTION_CONTRACT_SHA256 = (
    "00fb341e491f295b2db16a32626a6383d83c6cda88978b29479caf245c817387"
)
if HQ_VIDEO_EXECUTION_CONTRACT_SHA256 != _execution_contract_sha256(
    profile_sha256=HQ_VIDEO_PROFILE_SHA256,
    workflow_sha256=HQ_VIDEO_WORKFLOW_SHA256,
    built_in_negative_prompt=_HQ_BUILT_IN_NEGATIVE_PROMPT,
    execution_timeout_seconds=5400,
    planning_runtime_seconds=((73, 3058),),
    max_attempts=1,
):
    raise RuntimeError("pinned HQ execution contract changed")


@dataclass(frozen=True, slots=True)
class VideoProfileRegistration:
    """Immutable execution policy paired with an immutable model profile."""

    profile: VideoProfile
    profile_sha256: str
    workflow_sha256: str
    job_contract_sha256: str
    built_in_negative_prompt: str
    execution_timeout_seconds: int
    planning_runtime_seconds: tuple[tuple[int, int], ...]
    max_attempts: int

    def estimated_runtime_seconds(self, native_frame_count: int) -> int:
        try:
            return dict(self.planning_runtime_seconds)[native_frame_count]
        except KeyError:
            raise ValueError("unsupported native frame count") from None


PINNED_VIDEO_PROFILE_REGISTRATION = VideoProfileRegistration(
    profile=PINNED_VIDEO_PROFILE,
    profile_sha256=PINNED_VIDEO_PROFILE_SHA256,
    workflow_sha256=PINNED_VIDEO_WORKFLOW_SHA256,
    # Preserve the existing persisted v1 request identity. Its full execution
    # set is additionally pinned by VIDEO_PROFILE_REGISTRY_SHA256 below.
    job_contract_sha256=PINNED_VIDEO_PROFILE_SHA256,
    built_in_negative_prompt=_V1_BUILT_IN_NEGATIVE_PROMPT,
    execution_timeout_seconds=1800,
    planning_runtime_seconds=((73, 360), (121, 600)),
    max_attempts=3,
)
HQ_VIDEO_PROFILE_REGISTRATION = VideoProfileRegistration(
    profile=HQ_VIDEO_PROFILE,
    profile_sha256=HQ_VIDEO_PROFILE_SHA256,
    workflow_sha256=HQ_VIDEO_WORKFLOW_SHA256,
    job_contract_sha256=HQ_VIDEO_EXECUTION_CONTRACT_SHA256,
    built_in_negative_prompt=_HQ_BUILT_IN_NEGATIVE_PROMPT,
    # Leave 15 minutes of the 6,300-second provider watchdog for startup,
    # ping-pong encoding, upload, and controller reconciliation.
    execution_timeout_seconds=5400,
    planning_runtime_seconds=((73, 3058),),
    max_attempts=1,
)

VIDEO_PROFILE_REGISTRY: Mapping[str, VideoProfileRegistration] = MappingProxyType(
    {
        registration.profile.profile_id: registration
        for registration in (
            PINNED_VIDEO_PROFILE_REGISTRATION,
            HQ_VIDEO_PROFILE_REGISTRATION,
        )
    }
)


def _profile_registry_contract_sha256() -> str:
    descriptor = {
        "schema": "video-profile-registry/v1",
        "profiles": [
            {
                "profile_id": registration.profile.profile_id,
                "profile_sha256": registration.profile_sha256,
                "workflow_sha256": registration.workflow_sha256,
                "job_contract_sha256": registration.job_contract_sha256,
                "built_in_negative_prompt": registration.built_in_negative_prompt,
                "execution_timeout_seconds": registration.execution_timeout_seconds,
                "planning_runtime_seconds": registration.planning_runtime_seconds,
                "max_attempts": registration.max_attempts,
            }
            for registration in sorted(
                VIDEO_PROFILE_REGISTRY.values(),
                key=lambda item: item.profile.profile_id,
            )
        ],
    }
    encoded = json.dumps(
        descriptor,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


VIDEO_PROFILE_REGISTRY_SHA256 = "8f536e93c5e097aa70c8036c778c1a454fb8ce8ad5d0fd1dd614b67afd4c80eb"
if _profile_registry_contract_sha256() != VIDEO_PROFILE_REGISTRY_SHA256:
    raise RuntimeError("pinned video profile registry changed")


def get_video_profile_registration(profile_id: str) -> VideoProfileRegistration | None:
    return VIDEO_PROFILE_REGISTRY.get(profile_id)


def require_video_profile_registration(profile_id: str) -> VideoProfileRegistration:
    registration = get_video_profile_registration(profile_id)
    if registration is None:
        raise ValueError("unsupported video profile")
    return registration
