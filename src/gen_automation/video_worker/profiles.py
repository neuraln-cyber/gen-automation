import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from fractions import Fraction
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
    output_content_width: int | None = None
    output_content_height: int | None = None

    def __post_init__(self) -> None:
        if (self.output_content_width is None) != (self.output_content_height is None):
            raise ValueError("incomplete video output dimensions")
        if self.output_content_width is not None and (
            self.output_content_width <= 0
            or self.output_content_height is None
            or self.output_content_height <= 0
        ):
            raise ValueError("invalid video output dimensions")

    @property
    def output_width(self) -> int:
        width = self.output_content_width or self.width
        return width + (width % 2)

    @property
    def output_height(self) -> int:
        height = self.output_content_height or self.height
        return height + (height % 2)

    @property
    def output_frame_count(self) -> int:
        if self.loop_mode == "forward":
            return self.native_frame_count
        if self.loop_mode == "ping_pong":
            # Forward then reverse, without repeating either endpoint.
            return (self.native_frame_count * 2) - 2
        raise ValueError("unsupported video loop mode")

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


# Dedicated prompt-only quantized SmoothMix Wan2.2 I2V-A14B profile. This deliberately
# excludes motion-reference, driving-video, F2LF, T2V, and CLIP-Vision inputs.
A14B_VIDEO_PROFILE = VideoProfile(
    profile_id="wan2.2-smoothmix-i2v-a14b-q3-v1",
    adapter="wan-native-comfy-gguf",
    adapter_revision="video-worker-adapter-smoothmix-i2v-a14b-q3-v1",
    model="Civitai/SmoothMix-Wan2.2-I2V-v2-GGUF",
    model_revision="civitai:2299142@2587054+2587073",
    fps=16,
    default_native_frame_count=81,
    permitted_native_frame_counts=(81,),
    landscape_width=720,
    landscape_height=560,
    portrait_width=560,
    portrait_height=720,
    loop_mode="forward",
    output_media_type="video/mp4",
    output_codec="h264",
    output_pixel_format="yuv420p",
    require_faststart=True,
)
A14B_VIDEO_PROFILE_SHA256 = "0c23b27caeda9d444f266db3a5e4c7ff36aa8705027df09c5485e7a6154178ef"
if A14B_VIDEO_PROFILE.descriptor_sha256 != A14B_VIDEO_PROFILE_SHA256:
    raise RuntimeError("A14B video profile descriptor changed")

A14B_ADULT_VIDEO_PROFILE = VideoProfile(
    **{
        **asdict(A14B_VIDEO_PROFILE),
        "profile_id": "wan2.2-smoothmix-i2v-a14b-q3-adult-v1",
        "adapter_revision": "video-worker-adapter-smoothmix-i2v-a14b-q3-adult-v1",
    }
)
A14B_ADULT_VIDEO_PROFILE_SHA256 = "064fd6e46d63e037d552f9d720e7815ee67d31186c4b9b920c6c5d5cc6a25781"
if A14B_ADULT_VIDEO_PROFILE.descriptor_sha256 != A14B_ADULT_VIDEO_PROFILE_SHA256:
    raise RuntimeError("adult A14B video profile descriptor changed")

PINNED_VIDEO_WORKFLOW_SHA256: Final = (
    "ecba3eef1c14abcd4d0d2ba3cec1f53042767f1a177d833dcfd3fd6b11f09ab3"
)
HQ_VIDEO_WORKFLOW_SHA256: Final = "01c5b5350cab319e5cf7ec407971d7fbb269c4eb1e3c91c27c5c3cbb793a5151"
A14B_VIDEO_WORKFLOW_SHA256: Final = (
    "dbe6c644ec0b403d2d52509199ad405a899ce397712e265d5e20b538fd9ff588"
)
A14B_ADULT_VIDEO_WORKFLOW_SHA256: Final = (
    "4273bfbc4a3f3dbe309e3cf14e8986272b14a35235a1659fdb1f979a49ebee28"
)

A14B_MIN_EDGE: Final = 320
A14B_MAX_EDGE: Final = 1_152
A14B_DIMENSION_MULTIPLE: Final = 16
A14B_MIN_PIXELS: Final = 360_000
A14B_MAX_PIXELS: Final = 403_200
A14B_MIN_ASPECT_RATIO: Final = Fraction(4, 13)
A14B_MAX_ASPECT_RATIO: Final = Fraction(13, 4)
A14B_MAX_OUTPUT_EDGE: Final = 2_048
A14B_MAX_OUTPUT_PIXELS: Final = 4_194_304
A14B_OUTPUT_DIMENSION_POLICY: Final = (
    "a14b-source-output/v1;inference=aspect-fit-16px-360000..403200;"
    "source-ratio=4/13..13/4;output=source-exact;max=2048,4194304;"
    "odd=edge-pad-right-bottom-even;scale=lanczos-aspect-fill-centered-crop"
)
A14B_RENDER_DIMENSIONS: Final = frozenset(
    (width, height)
    for width in range(A14B_MIN_EDGE, A14B_MAX_EDGE + 1, A14B_DIMENSION_MULTIPLE)
    for height in range(A14B_MIN_EDGE, A14B_MAX_EDGE + 1, A14B_DIMENSION_MULTIPLE)
    if A14B_MIN_PIXELS <= width * height <= A14B_MAX_PIXELS
)


def derive_a14b_render_dimensions(source_width: int, source_height: int) -> tuple[int, int]:
    """Fit the source aspect to a bounded 480p-area canvas without stretching."""

    if source_width <= 0 or source_height <= 0:
        raise ValueError("invalid source dimensions")
    source_ratio = Fraction(source_width, source_height)
    if not A14B_MIN_ASPECT_RATIO <= source_ratio <= A14B_MAX_ASPECT_RATIO:
        raise ValueError("unsupported source aspect ratio")
    return min(
        A14B_RENDER_DIMENSIONS,
        key=lambda dimensions: (
            abs(Fraction(*dimensions) - source_ratio) / source_ratio,
            -(dimensions[0] * dimensions[1]),
            dimensions[0],
            dimensions[1],
        ),
    )


def is_a14b_render_dimensions(width: int, height: int) -> bool:
    return (width, height) in A14B_RENDER_DIMENSIONS


def derive_a14b_output_dimensions(source_width: int, source_height: int) -> tuple[int, int]:
    """Return the deterministic even H.264 canvas for the logical source size."""

    if source_width <= 0 or source_height <= 0:
        raise ValueError("invalid source dimensions")
    output_width = source_width + (source_width % 2)
    output_height = source_height + (source_height % 2)
    if (
        max(output_width, output_height) > A14B_MAX_OUTPUT_EDGE
        or output_width * output_height > A14B_MAX_OUTPUT_PIXELS
    ):
        raise ValueError("unsupported source output dimensions")
    return output_width, output_height


def is_a14b_output_dimensions(width: int, height: int) -> bool:
    return (
        width > 0
        and height > 0
        and width % 2 == 0
        and height % 2 == 0
        and max(width, height) <= A14B_MAX_OUTPUT_EDGE
        and width * height <= A14B_MAX_OUTPUT_PIXELS
    )


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
_A14B_BUILT_IN_NEGATIVE_PROMPT: Final = (
    "motionless subject, frozen pose, frozen torso, frozen hips, breathing-only motion, "
    "blinking-only motion, micro-motion only, barely perceptible motion, slow motion, pause, "
    "asymmetric blink, wink, one eye closes before the other, repeated blinking, eyes stuck "
    "closed, robotic motion, jerky motion, jitter, stutter, judder, flicker, frame-to-frame "
    "warping, face morphing, identity drift, facial drift, hand morphing, body deformation, "
    "malformed anatomy, extra limbs, extra fingers, fused fingers, camera shake, handheld "
    "camera, moving camera, pan, tilt, roll, zoom, dolly, orbit, reframing, perspective drift, "
    "background drift, background movement, overexposed, blurry, low quality, jpeg artifacts, "
    "subtitles, frame corruption"
)


def _execution_contract_sha256(
    *,
    profile_sha256: str,
    workflow_sha256: str,
    built_in_negative_prompt: str,
    execution_timeout_seconds: int,
    planning_runtime_seconds: tuple[tuple[int, int], ...],
    max_attempts: int,
    output_dimension_policy: str | None = None,
    completed_replay_ttl_seconds: int | None = None,
) -> str:
    descriptor: dict[str, object] = {
        "schema": "video-profile-execution/v1",
        "profile_sha256": profile_sha256,
        "workflow_sha256": workflow_sha256,
        "built_in_negative_prompt": built_in_negative_prompt,
        "execution_timeout_seconds": execution_timeout_seconds,
        "planning_runtime_seconds": planning_runtime_seconds,
        "max_attempts": max_attempts,
    }
    if output_dimension_policy is not None:
        descriptor["output_dimension_policy"] = output_dimension_policy
    if completed_replay_ttl_seconds is not None:
        descriptor["completed_replay_ttl_seconds"] = completed_replay_ttl_seconds
    encoded = json.dumps(
        descriptor,
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
    output_dimension_policy: str | None = None
    completed_replay_ttl_seconds: int | None = None

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

A14B_VIDEO_EXECUTION_CONTRACT_SHA256 = (
    "3c31ee0e82b15c582dfccfb5cc83bde80b55dd835f4afd217f3ce342accc6e99"
)
if A14B_VIDEO_EXECUTION_CONTRACT_SHA256 != _execution_contract_sha256(
    profile_sha256=A14B_VIDEO_PROFILE_SHA256,
    workflow_sha256=A14B_VIDEO_WORKFLOW_SHA256,
    built_in_negative_prompt=_A14B_BUILT_IN_NEGATIVE_PROMPT,
    execution_timeout_seconds=17_100,
    planning_runtime_seconds=((81, 18_000),),
    max_attempts=1,
    output_dimension_policy=A14B_OUTPUT_DIMENSION_POLICY,
    completed_replay_ttl_seconds=7_200,
):
    raise RuntimeError("pinned A14B execution contract changed")

A14B_VIDEO_PROFILE_REGISTRATION = VideoProfileRegistration(
    profile=A14B_VIDEO_PROFILE,
    profile_sha256=A14B_VIDEO_PROFILE_SHA256,
    workflow_sha256=A14B_VIDEO_WORKFLOW_SHA256,
    job_contract_sha256=A14B_VIDEO_EXECUTION_CONTRACT_SHA256,
    built_in_negative_prompt=_A14B_BUILT_IN_NEGATIVE_PROMPT,
    execution_timeout_seconds=17_100,
    planning_runtime_seconds=((81, 18_000),),
    max_attempts=1,
    output_dimension_policy=A14B_OUTPUT_DIMENSION_POLICY,
    completed_replay_ttl_seconds=7_200,
)

A14B_ADULT_VIDEO_EXECUTION_CONTRACT_SHA256 = (
    "014fc4502c334bdcd6ec9a8acad08b7599316dd0ddf3788e2101f40588d3e859"
)
if A14B_ADULT_VIDEO_EXECUTION_CONTRACT_SHA256 != _execution_contract_sha256(
    profile_sha256=A14B_ADULT_VIDEO_PROFILE_SHA256,
    workflow_sha256=A14B_ADULT_VIDEO_WORKFLOW_SHA256,
    built_in_negative_prompt=_A14B_BUILT_IN_NEGATIVE_PROMPT,
    execution_timeout_seconds=17_100,
    planning_runtime_seconds=((81, 18_000),),
    max_attempts=1,
    output_dimension_policy=A14B_OUTPUT_DIMENSION_POLICY,
    completed_replay_ttl_seconds=7_200,
):
    raise RuntimeError("pinned adult A14B execution contract changed")

A14B_ADULT_VIDEO_PROFILE_REGISTRATION = VideoProfileRegistration(
    profile=A14B_ADULT_VIDEO_PROFILE,
    profile_sha256=A14B_ADULT_VIDEO_PROFILE_SHA256,
    workflow_sha256=A14B_ADULT_VIDEO_WORKFLOW_SHA256,
    job_contract_sha256=A14B_ADULT_VIDEO_EXECUTION_CONTRACT_SHA256,
    built_in_negative_prompt=_A14B_BUILT_IN_NEGATIVE_PROMPT,
    execution_timeout_seconds=17_100,
    planning_runtime_seconds=((81, 18_000),),
    max_attempts=1,
    output_dimension_policy=A14B_OUTPUT_DIMENSION_POLICY,
    completed_replay_ttl_seconds=7_200,
)

VIDEO_PROFILE_REGISTRY: Mapping[str, VideoProfileRegistration] = MappingProxyType(
    {
        registration.profile.profile_id: registration
        for registration in (
            PINNED_VIDEO_PROFILE_REGISTRATION,
            HQ_VIDEO_PROFILE_REGISTRATION,
            A14B_VIDEO_PROFILE_REGISTRATION,
            A14B_ADULT_VIDEO_PROFILE_REGISTRATION,
        )
    }
)


def _profile_registry_contract_sha256(
    registrations: tuple[VideoProfileRegistration, ...],
) -> str:
    descriptor = {
        "schema": "video-profile-registry/v1",
        "profiles": [
            (
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
                | (
                    {"output_dimension_policy": registration.output_dimension_policy}
                    if registration.output_dimension_policy is not None
                    else {}
                )
                | (
                    {"completed_replay_ttl_seconds": (registration.completed_replay_ttl_seconds)}
                    if registration.completed_replay_ttl_seconds is not None
                    else {}
                )
            )
            for registration in sorted(
                registrations,
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
if (
    _profile_registry_contract_sha256(
        (PINNED_VIDEO_PROFILE_REGISTRATION, HQ_VIDEO_PROFILE_REGISTRATION)
    )
    != VIDEO_PROFILE_REGISTRY_SHA256
):
    raise RuntimeError("pinned video profile registry changed")

A14B_VIDEO_PROFILE_REGISTRY_SHA256 = (
    "a0ddd172e300f72fc60c4ae87c4f7d1ff077a0ffa2a9725c7aae2d74ecffa7b9"
)
if (
    _profile_registry_contract_sha256(
        (A14B_VIDEO_PROFILE_REGISTRATION, A14B_ADULT_VIDEO_PROFILE_REGISTRATION)
    )
    != A14B_VIDEO_PROFILE_REGISTRY_SHA256
):
    raise RuntimeError("pinned A14B video profile registry changed")


def get_video_profile_registration(profile_id: str) -> VideoProfileRegistration | None:
    return VIDEO_PROFILE_REGISTRY.get(profile_id)


def require_video_profile_registration(profile_id: str) -> VideoProfileRegistration:
    registration = get_video_profile_registration(profile_id)
    if registration is None:
        raise ValueError("unsupported video profile")
    return registration
