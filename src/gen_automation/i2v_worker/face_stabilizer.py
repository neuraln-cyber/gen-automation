from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import stat
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from dataclasses import field as dataclass_field
from enum import StrEnum
from pathlib import Path
from typing import Protocol, cast

import cv2
import numpy as np
from numpy.typing import NDArray


class FaceStabilizationReason(StrEnum):
    """Fixed, non-sensitive failure codes safe for worker diagnostics."""

    UNAVAILABLE = "face stabilizer is unavailable"
    INVALID_INPUT = "face stabilization input is invalid"
    OUTPUT_FAILED = "face stabilization output failed"
    INVALID_SOURCE_ANALYSIS = "source face preflight token is invalid"
    INVALID_CONFIGURATION = "face stabilization configuration is invalid"
    SOURCE_CONTRACT = "source face contract failed"
    DETECTOR_INFERENCE = "face detector inference failed"
    FRAME_CONTRACT = "frame face contract failed"
    POSE_GUARD = "face pose guard failed"
    BLINK_GUARD = "bilateral blink guard failed"
    OUTER_PIXEL_GUARD = "face outer-pixel guard failed"
    SEAM_GUARD = "face seam guard failed"
    INTERNAL = "face stabilization internal failure"


_CONTRACT_FAILURE_REASONS = frozenset(
    {
        FaceStabilizationReason.SOURCE_CONTRACT,
        FaceStabilizationReason.FRAME_CONTRACT,
        FaceStabilizationReason.POSE_GUARD,
        FaceStabilizationReason.BLINK_GUARD,
        FaceStabilizationReason.SEAM_GUARD,
    }
)


class FaceStabilizationError(Exception):
    """A classified, redacted, fail-closed face-stabilization failure."""

    def __init__(self, reason: FaceStabilizationReason | str) -> None:
        # Unknown strings can arise only from injected/test code. Collapse them
        # to one safe internal code instead of retaining potentially sensitive
        # paths, coordinates, pixels, or upstream exception text.
        self.reason = (
            reason
            if isinstance(reason, FaceStabilizationReason)
            else FaceStabilizationReason.INTERNAL
        )
        super().__init__(self.reason.value)

    @property
    def is_contract_failure(self) -> bool:
        return self.reason in _CONTRACT_FAILURE_REASONS


UInt8Image = NDArray[np.uint8]
FloatArray = NDArray[np.float32]
DetectionPayload = Mapping[str, object]


class FaceDetector(Protocol):
    """The subset of the anime-face-detector LandmarkDetector API we use."""

    def __call__(
        self,
        image: UInt8Image,
        /,
        boxes: list[FloatArray] | None = None,
    ) -> Sequence[DetectionPayload]: ...


class DetectorFactory(Protocol):
    """The public anime_face_detector.create_detector call contract."""

    def __call__(
        self,
        face_detector_name: str = "yolov3",
        landmark_model_name: str = "hrnetv2",
        device: str = "cuda:0",
        flip_test: bool = True,
        box_scale_factor: float = 1.1,
    ) -> FaceDetector: ...


@dataclass(frozen=True, slots=True)
class FaceStabilizationConfig:
    """Strict deterministic guards for the stable-expression mode."""

    alignment_angles: tuple[float, ...] = (0.0, -15.0, 15.0, -30.0, 30.0)
    minimum_face_score: float = 0.95
    minimum_landmark_mean_score: float = 0.85
    minimum_landmark_score: float = 0.60
    minimum_face_side: int = 64
    maximum_translation_ratio: float = 0.12
    maximum_rotation_degrees: float = 7.0
    maximum_scale_delta: float = 0.08
    maximum_anchor_residual_ratio: float = 0.06
    minimum_open_eye_aperture: float = 0.10
    minimum_blink_closure: float = 0.18
    maximum_blink_imbalance: float = 0.20
    face_radius_x_ratio: float = 0.34
    face_radius_y_ratio: float = 0.40
    face_feather_ratio: float = 0.035
    eye_radius_x_ratio: float = 0.14
    eye_radius_y_ratio: float = 0.075
    eye_feather_ratio: float = 0.015
    head_background_min_channel: int = 238
    head_background_max_channel_range: int = 20
    head_background_min_connected_fraction: float = 0.15
    head_background_rim_ratio: float = 0.02
    head_background_min_rim_fraction: float = 0.50
    head_background_corner_ratio: float = 0.05
    head_background_min_corner_fraction: float = 0.80
    head_background_min_corner_count: int = 3
    head_background_max_channel_std: float = 2.0
    head_background_min_aligned_connectivity: float = 0.95
    head_center_y_ratio: float = -0.12
    head_radius_x_ratio: float = 0.64
    head_radius_y_ratio: float = 0.72
    head_feather_ratio: float = 0.025
    minimum_alpha: float = 0.002
    maximum_seam_mean_delta: float = 12.8
    maximum_seam_p95_delta: float = 22.4


@dataclass(frozen=True, slots=True)
class FaceStabilizationResult:
    frames: tuple[Path, ...]
    metadata: dict[str, object]


_DEFAULT_CONFIG = FaceStabilizationConfig()


@dataclass(frozen=True, slots=True)
class SourceFaceAnalysis:
    """Immutable, digest-bound source preflight token.

    Geometry and pixels remain private so callers cannot accidentally serialize
    biometric coordinates into job metadata. All fields are immutable Python
    scalars, tuples, or bytes; no writable NumPy array escapes the preflight.
    """

    width: int
    height: int
    guard_profile_sha256: str
    _source_file_sha256: str = dataclass_field(repr=False)
    _image_bytes: bytes = dataclass_field(repr=False)
    _bbox: tuple[float, ...] = dataclass_field(repr=False)
    _keypoints: tuple[tuple[float, float, float], ...] = dataclass_field(repr=False)
    _face_score: float = dataclass_field(repr=False)
    _landmark_mean_score: float = dataclass_field(repr=False)
    _alignment_angle: float = dataclass_field(repr=False)
    _head_matte_bytes: bytes = dataclass_field(repr=False)
    _head_matte_sha256: str = dataclass_field(repr=False)


@dataclass(frozen=True, slots=True)
class _Detection:
    bbox: FloatArray
    keypoints: FloatArray
    face_score: float
    landmark_mean_score: float


@dataclass(frozen=True, slots=True)
class _SourceAnalysis:
    original_image: UInt8Image
    image: UInt8Image
    detection: _Detection
    alignment_angle: float
    head_matte: FloatArray


@dataclass(frozen=True, slots=True)
class _FrameAnalysis:
    source_to_frame: FloatArray
    eye_apertures: tuple[float, float]
    translation_ratio: float
    rotation_degrees: float
    scale_delta: float
    anchor_residual_ratio: float


@dataclass(frozen=True, slots=True)
class _BlinkSelection:
    center: int
    left_closure: float
    right_closure: float
    imbalance: float


_DEFAULT_YOLO_CHECKPOINT = Path("/opt/i2v/face-models/yolov3.safetensors")
_DEFAULT_HRNET_CHECKPOINT = Path("/opt/i2v/face-models/hrnetv2.safetensors")
_YOLO_CHECKPOINT_ENV = "GEN_I2V_FACE_DETECTOR_YOLO_PATH"
_HRNET_CHECKPOINT_ENV = "GEN_I2V_FACE_LANDMARK_HRNET_PATH"
_STABLE_ANCHOR_INDICES = np.asarray((0, 1, 2, 3, 4, 23), dtype=np.intp)
_LEFT_EYE = slice(11, 17)
_RIGHT_EYE = slice(17, 23)
_BLINK_WEIGHTS = (0.15, 0.55, 1.0, 0.55, 0.15)
_METADATA_SCHEMA = "gen-automation/i2v-face-stabilization/v2"
_CAPABILITY_SCHEMA = "gen-automation/i2v-face-stabilizer-capability/v2"
_ALGORITHM_ID = "dasiwa-static-source-head-single-blink/v2"
_GUARD_PROFILE = "static-head-near-white-v2"
_DETECTOR_REVISION = "7db835de7a3a052eb4d68d241ae9f2cf28a0b509"
_DETECTOR_WHEEL_SHA256 = "9a6a8c1384b7a57fab8ce9988f814271ff88bac52a9dd871490a28b61dff7692"
_YOLO_SHA256 = "23bbc708146bcbc1c910f00fe152adbc70d7658d875a0121eaf4ee61d978b2c4"
_HRNET_SHA256 = "e71271376406a743c01528a0460637fcc06e72aeeea583f85007cc72dc8b7a4a"
_OPENCV_SHA256 = "211e581f5a4670acbbe08fff36a35e9946039d2eea28b80394632d036d1be527"
_YOLO_BYTE_SIZE = 246_354_512
_HRNET_BYTE_SIZE = 38_917_560


def face_stabilizer_capability() -> dict[str, object]:
    """Return the exact non-secret runtime identity advertised by readiness."""

    return {
        "schema": _CAPABILITY_SCHEMA,
        **_face_stabilizer_identity(),
    }


def _face_stabilizer_identity() -> dict[str, object]:
    return {
        "algorithm": _ALGORITHM_ID,
        "guard_profile": _GUARD_PROFILE,
        "guard_profile_sha256": _guard_profile_sha256(_DEFAULT_CONFIG),
        "detector_revision": _DETECTOR_REVISION,
        "detector_wheel_sha256": _DETECTOR_WHEEL_SHA256,
        "yolo_sha256": _YOLO_SHA256,
        "hrnet_sha256": _HRNET_SHA256,
        "opencv_sha256": _OPENCV_SHA256,
    }


def preflight_face_stabilizer(
    *,
    detector_factory: DetectorFactory | None = None,
    yolo_checkpoint: Path | None = None,
    landmark_checkpoint: Path | None = None,
    device: str = "cpu",
) -> FaceDetector:
    """Load the pinned detector without importing it during unit-test collection.

    A supplied ``detector_factory`` has the upstream ``create_detector`` shape.
    Production uses the two explicitly provisioned checkpoint paths so a worker
    can never download model files while serving a job.
    """

    try:
        if detector_factory is not None:
            detector = detector_factory(
                face_detector_name="yolov3",
                landmark_model_name="hrnetv2",
                device=device,
                flip_test=False,
                box_scale_factor=1.1,
            )
        else:
            yolo_path = _resolve_checkpoint(
                yolo_checkpoint,
                environment_name=_YOLO_CHECKPOINT_ENV,
                default=_DEFAULT_YOLO_CHECKPOINT,
            )
            hrnet_path = _resolve_checkpoint(
                landmark_checkpoint,
                environment_name=_HRNET_CHECKPOINT_ENV,
                default=_DEFAULT_HRNET_CHECKPOINT,
            )
            _verify_checkpoint(
                yolo_path,
                expected_sha256=_YOLO_SHA256,
                expected_byte_size=_YOLO_BYTE_SIZE,
            )
            _verify_checkpoint(
                hrnet_path,
                expected_sha256=_HRNET_SHA256,
                expected_byte_size=_HRNET_BYTE_SIZE,
            )
            from anime_face_detector import LandmarkDetector  # type: ignore[import-untyped]

            detector = cast(
                FaceDetector,
                LandmarkDetector(
                    hrnet_path,
                    face_detector_name="yolov3",
                    face_detector_checkpoint_path=yolo_path,
                    device=device,
                    flip_test=False,
                    box_scale_factor=1.1,
                ),
            )
        if not callable(detector):
            raise FaceStabilizationError(FaceStabilizationReason.UNAVAILABLE)
        return detector
    except FaceStabilizationError:
        raise
    except Exception:
        raise FaceStabilizationError(FaceStabilizationReason.UNAVAILABLE) from None


def preflight_source_face(
    prepared_source: Path,
    detector: FaceDetector,
    *,
    config: FaceStabilizationConfig = _DEFAULT_CONFIG,
) -> SourceFaceAnalysis:
    """Validate the prepared source before expensive video generation begins."""

    _validate_config(config)
    source, source_file_sha256 = _read_prepared_source(prepared_source)
    _validate_source_background(source, config)
    analysis = _analyze_source(source, detector, config)
    height, width = analysis.image.shape[:2]
    head_matte_bytes = analysis.head_matte.astype("<f4", copy=False).tobytes(order="C")
    return SourceFaceAnalysis(
        width=width,
        height=height,
        guard_profile_sha256=_guard_profile_sha256(config),
        _source_file_sha256=source_file_sha256,
        _image_bytes=analysis.image.tobytes(order="C"),
        _bbox=tuple(float(value) for value in analysis.detection.bbox),
        _keypoints=tuple(
            (float(point[0]), float(point[1]), float(point[2]))
            for point in analysis.detection.keypoints
        ),
        _face_score=analysis.detection.face_score,
        _landmark_mean_score=analysis.detection.landmark_mean_score,
        _alignment_angle=analysis.alignment_angle,
        _head_matte_bytes=head_matte_bytes,
        _head_matte_sha256=hashlib.sha256(head_matte_bytes).hexdigest(),
    )


def stabilize_face_frames(
    prepared_source: Path,
    frames: tuple[Path, ...],
    destination: Path,
    *,
    detector: FaceDetector | None = None,
    detector_factory: DetectorFactory | None = None,
    detector_device: str = "cpu",
    source_analysis: SourceFaceAnalysis | None = None,
    config: FaceStabilizationConfig = _DEFAULT_CONFIG,
) -> FaceStabilizationResult:
    """Lock the source expression while retaining one native bilateral blink.

    ``prepared_source`` and every generated frame must have identical native WAN
    dimensions. Original frame files are never changed. The returned frame paths
    are deterministic PNG copies in ``destination`` and are ready for the normal
    encoder. Any uncertain detection, pose, blink, seam, or output condition fails
    closed with a redacted ``FaceStabilizationError``.
    """

    _validate_config(config)
    if not frames:
        raise FaceStabilizationError(FaceStabilizationReason.INVALID_INPUT)
    resolved_detector = detector
    if resolved_detector is None:
        resolved_detector = preflight_face_stabilizer(
            detector_factory=detector_factory,
            device=detector_device,
        )

    prepared_image, source_file_sha256 = _read_prepared_source(prepared_source)
    source_token = source_analysis
    if source_token is None:
        source_token = preflight_source_face(
            prepared_source,
            resolved_detector,
            config=config,
        )
    analyzed_source = _restore_source_analysis(
        source_token,
        prepared_image=prepared_image,
        source_file_sha256=source_file_sha256,
        config=config,
    )
    height, width = analyzed_source.image.shape[:2]
    frame_analyses = _analyze_frames(
        frames,
        expected_width=width,
        expected_height=height,
        source=analyzed_source,
        detector=resolved_detector,
        config=config,
    )
    blink = _select_blink(analyzed_source.detection, frame_analyses, config)

    created = False
    try:
        destination.mkdir(parents=True, exist_ok=False, mode=0o700)
        created = True
        output_frames, maximum_seam_mean, maximum_seam_p95 = _materialize_frames(
            frames,
            destination=destination,
            source=analyzed_source,
            analyses=frame_analyses,
            blink_center=blink.center,
            config=config,
        )
    except FaceStabilizationError:
        if created:
            shutil.rmtree(destination, ignore_errors=True)
        raise
    except (OSError, ValueError, cv2.error):
        if created:
            shutil.rmtree(destination, ignore_errors=True)
        raise FaceStabilizationError(FaceStabilizationReason.OUTPUT_FAILED) from None

    return FaceStabilizationResult(
        frames=output_frames,
        metadata={
            "schema": _METADATA_SCHEMA,
            **_face_stabilizer_identity(),
            "guard_profile_sha256": source_token.guard_profile_sha256,
            "blink_events": 1,
            "blink_center_frame": blink.center,
            "blink_window_frames": len(_BLINK_WEIGHTS),
            "frame_count": len(output_frames),
            "metrics": {
                "source_face_score": _metric(analyzed_source.detection.face_score),
                "source_landmark_min_score": _metric(
                    float(np.min(analyzed_source.detection.keypoints[:, 2]))
                ),
                "source_landmark_mean_score": _metric(
                    analyzed_source.detection.landmark_mean_score
                ),
                "maximum_translation_ratio": _metric(
                    max(item.translation_ratio for item in frame_analyses)
                ),
                "maximum_rotation_degrees": _metric(
                    max(abs(item.rotation_degrees) for item in frame_analyses)
                ),
                "maximum_scale_delta": _metric(max(item.scale_delta for item in frame_analyses)),
                "maximum_anchor_residual_ratio": _metric(
                    max(item.anchor_residual_ratio for item in frame_analyses)
                ),
                "maximum_seam_mean_delta": _metric(maximum_seam_mean),
                "maximum_seam_p95_delta": _metric(maximum_seam_p95),
                "blink_left_closure": _metric(blink.left_closure),
                "blink_right_closure": _metric(blink.right_closure),
                "blink_imbalance": _metric(blink.imbalance),
            },
        },
    )


def _resolve_checkpoint(
    explicit: Path | None,
    *,
    environment_name: str,
    default: Path,
) -> Path:
    if explicit is not None:
        return explicit
    configured = os.environ.get(environment_name)
    return Path(configured) if configured else default


def _verify_checkpoint(
    path: Path,
    *,
    expected_sha256: str,
    expected_byte_size: int,
) -> None:
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != expected_byte_size:
            raise FaceStabilizationError(FaceStabilizationReason.UNAVAILABLE)
        with path.open("rb") as payload:
            digest = hashlib.file_digest(payload, "sha256").hexdigest()
        if digest != expected_sha256:
            raise FaceStabilizationError(FaceStabilizationReason.UNAVAILABLE)
    except FaceStabilizationError:
        raise
    except (OSError, ValueError):
        raise FaceStabilizationError(FaceStabilizationReason.UNAVAILABLE) from None


def _read_prepared_source(path: Path) -> tuple[UInt8Image, str]:
    try:
        payload = path.read_bytes()
        encoded = np.frombuffer(payload, dtype=np.uint8)
        image = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
    except (OSError, ValueError, cv2.error):
        image = None
        payload = b""
    if image is None or image.ndim != 3 or image.shape[2] != 3 or image.dtype != np.uint8:
        raise FaceStabilizationError(FaceStabilizationReason.INVALID_INPUT)
    return cast(UInt8Image, image), hashlib.sha256(payload).hexdigest()


def _restore_source_analysis(
    token: SourceFaceAnalysis,
    *,
    prepared_image: UInt8Image,
    source_file_sha256: str,
    config: FaceStabilizationConfig,
) -> _SourceAnalysis:
    if (
        prepared_image.shape[:2] != (token.height, token.width)
        or source_file_sha256 != token._source_file_sha256
        or token.guard_profile_sha256 != _guard_profile_sha256(config)
        or len(token._image_bytes) != token.width * token.height * 3
        or len(token._bbox) != 5
        or len(token._keypoints) != 28
        or any(len(point) != 3 for point in token._keypoints)
        or len(token._head_matte_bytes) != token.width * token.height * 4
        or hashlib.sha256(token._head_matte_bytes).hexdigest() != token._head_matte_sha256
    ):
        raise FaceStabilizationError(FaceStabilizationReason.INVALID_SOURCE_ANALYSIS)
    try:
        image = np.frombuffer(token._image_bytes, dtype=np.uint8).reshape(
            (token.height, token.width, 3)
        )
        bbox = np.asarray(token._bbox, dtype=np.float32)
        keypoints = np.asarray(token._keypoints, dtype=np.float32)
        head_matte = np.frombuffer(token._head_matte_bytes, dtype="<f4").reshape(
            (token.height, token.width)
        )
    except (TypeError, ValueError):
        raise FaceStabilizationError(FaceStabilizationReason.INVALID_SOURCE_ANALYSIS) from None
    if (
        not np.isfinite(bbox).all()
        or not np.isfinite(keypoints).all()
        or not np.isfinite(head_matte).all()
        or float(np.min(head_matte)) < 0.0
        or float(np.max(head_matte)) > 1.0
    ):
        raise FaceStabilizationError(FaceStabilizationReason.INVALID_SOURCE_ANALYSIS)
    return _SourceAnalysis(
        original_image=prepared_image.copy(),
        image=cast(UInt8Image, image.copy()),
        detection=_Detection(
            bbox=bbox,
            keypoints=keypoints,
            face_score=token._face_score,
            landmark_mean_score=token._landmark_mean_score,
        ),
        alignment_angle=token._alignment_angle,
        head_matte=cast(FloatArray, head_matte.astype(np.float32, copy=True)),
    )


def _guard_profile_sha256(config: FaceStabilizationConfig) -> str:
    encoded = json.dumps(
        asdict(config),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _metric(value: float) -> float:
    return round(float(value), 6)


def _validate_config(config: FaceStabilizationConfig) -> None:
    probabilities = (
        config.minimum_face_score,
        config.minimum_landmark_mean_score,
        config.minimum_landmark_score,
        config.minimum_blink_closure,
        config.maximum_blink_imbalance,
        config.head_background_min_connected_fraction,
        config.head_background_rim_ratio,
        config.head_background_min_rim_fraction,
        config.head_background_corner_ratio,
        config.head_background_min_corner_fraction,
        config.head_background_min_aligned_connectivity,
        config.minimum_alpha,
    )
    ratios = (
        config.maximum_translation_ratio,
        config.maximum_scale_delta,
        config.maximum_anchor_residual_ratio,
        config.minimum_open_eye_aperture,
        config.face_radius_x_ratio,
        config.face_radius_y_ratio,
        config.face_feather_ratio,
        config.eye_radius_x_ratio,
        config.eye_radius_y_ratio,
        config.eye_feather_ratio,
        config.head_radius_x_ratio,
        config.head_radius_y_ratio,
        config.head_feather_ratio,
        config.head_background_max_channel_std,
    )
    if (
        not config.alignment_angles
        or any(not math.isfinite(item) for item in config.alignment_angles)
        or any(not 0 < item < 1 for item in probabilities)
        or any(not math.isfinite(item) or item <= 0 for item in ratios)
        or not -1 < config.head_center_y_ratio < 1
        or not 0 <= config.head_background_min_channel <= 255
        or not 0 <= config.head_background_max_channel_range <= 255
        or not 1 <= config.head_background_min_corner_count <= 4
        or config.minimum_face_side < 32
        or not 0 < config.maximum_rotation_degrees <= 30
        or not math.isfinite(config.maximum_seam_mean_delta)
        or not math.isfinite(config.maximum_seam_p95_delta)
        or config.maximum_seam_mean_delta <= 0
        or config.maximum_seam_p95_delta <= config.maximum_seam_mean_delta
    ):
        raise FaceStabilizationError(FaceStabilizationReason.INVALID_CONFIGURATION)


def _read_image(path: Path) -> UInt8Image:
    try:
        image = cv2.imread(path.as_posix(), cv2.IMREAD_COLOR)
    except (OSError, cv2.error):
        image = None
    if image is None or image.ndim != 3 or image.shape[2] != 3 or image.dtype != np.uint8:
        raise FaceStabilizationError(FaceStabilizationReason.INVALID_INPUT)
    return cast(UInt8Image, image)


def _near_white_background_masks(
    image: UInt8Image,
    config: FaceStabilizationConfig,
) -> tuple[NDArray[np.bool_], NDArray[np.bool_]]:
    channel_minimum = np.min(image, axis=2)
    channel_range = np.max(image, axis=2) - channel_minimum
    near_white = (channel_minimum >= config.head_background_min_channel) & (
        channel_range <= config.head_background_max_channel_range
    )
    _count, labels = cv2.connectedComponents(
        near_white.astype(np.uint8),
        connectivity=8,
    )
    border_labels = np.unique(
        np.concatenate((labels[0, :], labels[-1, :], labels[:, 0], labels[:, -1]))
    )
    border_labels = border_labels[border_labels != 0]
    connected = np.isin(labels, border_labels) if border_labels.size else np.zeros_like(near_white)
    return near_white, connected.astype(np.bool_)


def _connected_background_is_uniform(
    image: UInt8Image,
    connected: NDArray[np.bool_],
    config: FaceStabilizationConfig,
) -> bool:
    pixels = image[connected]
    if pixels.size == 0:
        return False
    deviations = np.std(pixels.astype(np.float32), axis=0)
    return bool(np.all(deviations <= config.head_background_max_channel_std))


def _validate_source_background(
    source: UInt8Image,
    config: FaceStabilizationConfig,
) -> None:
    """Accept only the reviewed, border-connected near-white source class."""

    near_white, connected = _near_white_background_masks(source, config)
    height, width = source.shape[:2]
    short_side = min(height, width)
    rim_width = max(1, round(short_side * config.head_background_rim_ratio))
    corner_side = max(1, round(short_side * config.head_background_corner_ratio))
    rim = np.zeros((height, width), dtype=np.bool_)
    rim[:rim_width, :] = True
    rim[-rim_width:, :] = True
    rim[:, :rim_width] = True
    rim[:, -rim_width:] = True
    corner_fractions = (
        float(np.mean(near_white[:corner_side, :corner_side])),
        float(np.mean(near_white[:corner_side, -corner_side:])),
        float(np.mean(near_white[-corner_side:, :corner_side])),
        float(np.mean(near_white[-corner_side:, -corner_side:])),
    )
    valid_corners = sum(
        fraction >= config.head_background_min_corner_fraction for fraction in corner_fractions
    )
    if (
        float(np.mean(connected)) < config.head_background_min_connected_fraction
        or float(np.mean(near_white[rim])) < config.head_background_min_rim_fraction
        or valid_corners < config.head_background_min_corner_count
        or not _connected_background_is_uniform(source, connected, config)
    ):
        raise FaceStabilizationError(FaceStabilizationReason.SOURCE_CONTRACT)


def _build_head_matte(
    aligned_source: UInt8Image,
    detection: _Detection,
    config: FaceStabilizationConfig,
) -> FloatArray:
    near_white, connected = _near_white_background_masks(aligned_source, config)
    near_white_count = int(np.count_nonzero(near_white))
    connectivity = (
        float(np.count_nonzero(connected)) / near_white_count if near_white_count else 0.0
    )
    if (
        float(np.mean(connected)) < config.head_background_min_connected_fraction
        or connectivity < config.head_background_min_aligned_connectivity
        or not _connected_background_is_uniform(aligned_source, connected, config)
    ):
        raise FaceStabilizationError(FaceStabilizationReason.SOURCE_CONTRACT)

    height, width = aligned_source.shape[:2]
    x0, y0, x1, y1 = (float(value) for value in detection.bbox[:4])
    face_width = x1 - x0
    face_height = y1 - y0
    ellipse = np.zeros((height, width), dtype=np.uint8)
    cv2.ellipse(
        ellipse,
        (
            round((x0 + x1) / 2),
            round(((y0 + y1) / 2) + (face_height * config.head_center_y_ratio)),
        ),
        (
            max(1, round(face_width * config.head_radius_x_ratio)),
            max(1, round(face_height * config.head_radius_y_ratio)),
        ),
        0,
        0,
        360,
        1,
        -1,
    )
    foreground = np.logical_not(connected)
    binary = (foreground & ellipse.astype(np.bool_)).astype(np.float32)
    matte = cast(
        FloatArray,
        cv2.GaussianBlur(binary, (0, 0), face_width * config.head_feather_ratio),
    )
    matte = np.clip(matte, 0.0, 1.0).astype(np.float32)
    matte[matte < config.minimum_alpha] = 0.0
    center_x = min(width - 1, max(0, round((x0 + x1) / 2)))
    center_y = min(height - 1, max(0, round((y0 + y1) / 2)))
    if int(np.count_nonzero(matte)) < 32 or float(matte[center_y, center_x]) < 0.99:
        raise FaceStabilizationError(FaceStabilizationReason.SOURCE_CONTRACT)
    return matte


def _analyze_source(
    source: UInt8Image,
    detector: FaceDetector,
    config: FaceStabilizationConfig,
) -> _SourceAnalysis:
    candidates: list[tuple[float, int, UInt8Image, _Detection, float]] = []
    multiple_faces_seen = False
    for order, angle in enumerate(config.alignment_angles):
        aligned = _rotate_image(source, angle)
        predictions = _call_detector(detector, aligned)
        detections = _parse_detections(
            predictions,
            width=aligned.shape[1],
            height=aligned.shape[0],
            config=config,
        )
        if len(detections) > 1:
            multiple_faces_seen = True
            continue
        if not detections:
            continue
        detection = detections[0]
        eye_roll = abs(_eye_roll_degrees(detection.keypoints))
        score = (
            detection.face_score
            + (0.10 * detection.landmark_mean_score)
            - (0.003 * eye_roll)
            - (0.0002 * abs(angle))
        )
        candidates.append((score, -order, aligned, detection, angle))
    if multiple_faces_seen or not candidates:
        raise FaceStabilizationError(FaceStabilizationReason.SOURCE_CONTRACT)
    _score, _tie_break, aligned, detection, angle = max(candidates, key=lambda item: item[:2])
    if abs(_eye_roll_degrees(detection.keypoints)) > config.maximum_rotation_degrees:
        raise FaceStabilizationError(FaceStabilizationReason.SOURCE_CONTRACT)
    if any(
        aperture < config.minimum_open_eye_aperture
        for aperture in _eye_apertures(detection.keypoints)
    ):
        raise FaceStabilizationError(FaceStabilizationReason.SOURCE_CONTRACT)
    return _SourceAnalysis(
        original_image=source.copy(),
        image=aligned,
        detection=detection,
        alignment_angle=angle,
        head_matte=_build_head_matte(aligned, detection, config),
    )


def _call_detector(
    detector: FaceDetector,
    image: UInt8Image,
) -> Sequence[DetectionPayload]:
    try:
        predictions = detector(image)
    except Exception:
        raise FaceStabilizationError(FaceStabilizationReason.DETECTOR_INFERENCE) from None
    if isinstance(predictions, (str, bytes)) or not isinstance(predictions, Sequence):
        raise FaceStabilizationError(FaceStabilizationReason.DETECTOR_INFERENCE)
    return predictions


def _parse_detections(
    predictions: Sequence[DetectionPayload],
    *,
    width: int,
    height: int,
    config: FaceStabilizationConfig,
) -> tuple[_Detection, ...]:
    detections: list[_Detection] = []
    for prediction in predictions:
        if not isinstance(prediction, Mapping):
            raise FaceStabilizationError(FaceStabilizationReason.DETECTOR_INFERENCE)
        try:
            bbox = np.asarray(prediction["bbox"], dtype=np.float32)
        except (KeyError, TypeError, ValueError):
            raise FaceStabilizationError(FaceStabilizationReason.DETECTOR_INFERENCE) from None
        if bbox.shape != (5,) or not np.isfinite(bbox).all():
            raise FaceStabilizationError(FaceStabilizationReason.DETECTOR_INFERENCE)
        face_score = float(bbox[4])
        if face_score < config.minimum_face_score:
            continue
        try:
            keypoints = np.asarray(prediction["keypoints"], dtype=np.float32)
        except (KeyError, TypeError, ValueError):
            raise FaceStabilizationError(FaceStabilizationReason.DETECTOR_INFERENCE) from None
        if keypoints.shape != (28, 3) or not np.isfinite(keypoints).all():
            raise FaceStabilizationError(FaceStabilizationReason.DETECTOR_INFERENCE)
        x0, y0, x1, y1 = (float(value) for value in bbox[:4])
        face_width = x1 - x0
        face_height = y1 - y0
        landmark_scores = keypoints[:, 2]
        landmark_mean = float(np.mean(landmark_scores))
        if (
            face_score > 1.0
            or face_width < config.minimum_face_side
            or face_height < config.minimum_face_side
            or x0 < 0
            or y0 < 0
            or x1 > width - 1
            or y1 > height - 1
            or float(np.min(landmark_scores)) < config.minimum_landmark_score
            or landmark_mean < config.minimum_landmark_mean_score
            or float(np.min(keypoints[:, 0])) < 0
            or float(np.max(keypoints[:, 0])) > width - 1
            or float(np.min(keypoints[:, 1])) < 0
            or float(np.max(keypoints[:, 1])) > height - 1
        ):
            continue
        detections.append(
            _Detection(
                bbox=bbox,
                keypoints=keypoints,
                face_score=face_score,
                landmark_mean_score=landmark_mean,
            )
        )
    return tuple(detections)


def _analyze_frames(
    frames: tuple[Path, ...],
    *,
    expected_width: int,
    expected_height: int,
    source: _SourceAnalysis,
    detector: FaceDetector,
    config: FaceStabilizationConfig,
) -> tuple[_FrameAnalysis, ...]:
    analyses: list[_FrameAnalysis] = []
    source_points = source.detection.keypoints[_STABLE_ANCHOR_INDICES, :2]
    source_center = np.asarray(
        (
            (source.detection.bbox[0] + source.detection.bbox[2]) / 2,
            (source.detection.bbox[1] + source.detection.bbox[3]) / 2,
        ),
        dtype=np.float32,
    )
    face_width = float(source.detection.bbox[2] - source.detection.bbox[0])
    for frame_path in frames:
        frame = _read_image(frame_path)
        if frame.shape[:2] != (expected_height, expected_width):
            raise FaceStabilizationError(FaceStabilizationReason.INVALID_INPUT)
        aligned = _rotate_image(frame, source.alignment_angle)
        predictions = _call_detector(detector, aligned)
        detections = _parse_detections(
            predictions,
            width=expected_width,
            height=expected_height,
            config=config,
        )
        if len(detections) != 1:
            raise FaceStabilizationError(FaceStabilizationReason.FRAME_CONTRACT)
        detection = detections[0]
        matrix, scale, rotation, residual = _similarity_transform(
            source_points,
            detection.keypoints[_STABLE_ANCHOR_INDICES, :2],
        )
        mapped_center = (matrix[:, :2] @ source_center) + matrix[:, 2]
        translation_ratio = float(np.linalg.norm(mapped_center - source_center)) / face_width
        scale_delta = abs(scale - 1.0)
        residual_ratio = residual / face_width
        if (
            translation_ratio > config.maximum_translation_ratio
            or abs(rotation) > config.maximum_rotation_degrees
            or scale_delta > config.maximum_scale_delta
            or residual_ratio > config.maximum_anchor_residual_ratio
        ):
            raise FaceStabilizationError(FaceStabilizationReason.POSE_GUARD)
        analyses.append(
            _FrameAnalysis(
                source_to_frame=matrix,
                eye_apertures=_eye_apertures(detection.keypoints),
                translation_ratio=translation_ratio,
                rotation_degrees=rotation,
                scale_delta=scale_delta,
                anchor_residual_ratio=residual_ratio,
            )
        )
    return tuple(analyses)


def _similarity_transform(
    source: FloatArray,
    target: FloatArray,
) -> tuple[FloatArray, float, float, float]:
    source64 = np.asarray(source, dtype=np.float64)
    target64 = np.asarray(target, dtype=np.float64)
    source_mean = np.mean(source64, axis=0)
    target_mean = np.mean(target64, axis=0)
    source_centered = source64 - source_mean
    target_centered = target64 - target_mean
    variance = float(np.sum(source_centered * source_centered))
    if variance <= 1e-6:
        raise FaceStabilizationError(FaceStabilizationReason.POSE_GUARD)
    covariance = source_centered.T @ target_centered
    left, singular_values, right_transpose = np.linalg.svd(covariance)
    rotation_matrix = right_transpose.T @ left.T
    if np.linalg.det(rotation_matrix) < 0:
        right_transpose[-1, :] *= -1
        rotation_matrix = right_transpose.T @ left.T
    scale = float(np.sum(singular_values)) / variance
    if not math.isfinite(scale) or scale <= 0:
        raise FaceStabilizationError(FaceStabilizationReason.POSE_GUARD)
    linear = scale * rotation_matrix
    translation = target_mean - (linear @ source_mean)
    mapped = (source64 @ linear.T) + translation
    residual = float(np.sqrt(np.mean(np.sum((mapped - target64) ** 2, axis=1))))
    rotation_degrees = math.degrees(math.atan2(linear[1, 0], linear[0, 0]))
    matrix = np.column_stack((linear, translation)).astype(np.float32)
    return matrix, scale, rotation_degrees, residual


def _eye_apertures(keypoints: FloatArray) -> tuple[float, float]:
    return (
        _eye_aperture(keypoints[_LEFT_EYE, :2]),
        _eye_aperture(keypoints[_RIGHT_EYE, :2]),
    )


def _eye_aperture(points: FloatArray) -> float:
    top = np.asarray(points[:3], dtype=np.float64)
    bottom = np.asarray(points[3:], dtype=np.float64)
    eye_vector = top[-1] - top[0]
    width = float(np.linalg.norm(eye_vector))
    if width <= 1e-6:
        return 0.0
    normal = np.asarray((-eye_vector[1], eye_vector[0])) / width
    aperture = abs(float(np.dot(np.mean(bottom, axis=0) - np.mean(top, axis=0), normal)))
    return aperture / width


def _eye_roll_degrees(keypoints: FloatArray) -> float:
    left_center = np.mean(keypoints[_LEFT_EYE, :2], axis=0)
    right_center = np.mean(keypoints[_RIGHT_EYE, :2], axis=0)
    difference = right_center - left_center
    if float(np.linalg.norm(difference)) <= 1e-6:
        return 180.0
    return math.degrees(math.atan2(float(difference[1]), float(difference[0])))


def _select_blink(
    source: _Detection,
    analyses: tuple[_FrameAnalysis, ...],
    config: FaceStabilizationConfig,
) -> _BlinkSelection:
    if len(analyses) < 9:
        raise FaceStabilizationError(FaceStabilizationReason.BLINK_GUARD)
    source_apertures = _eye_apertures(source.keypoints)
    candidates: list[tuple[float, int, int, float, float, float]] = []
    for center in range(4, len(analyses) - 4):
        before = analyses[center - 4 : center - 2]
        after = analyses[center + 3 : center + 5]
        current = analyses[center].eye_apertures
        closures: list[float] = []
        for eye_index in (0, 1):
            open_before = float(np.mean([item.eye_apertures[eye_index] for item in before]))
            open_after = float(np.mean([item.eye_apertures[eye_index] for item in after]))
            local_open = min(open_before, open_after, source_apertures[eye_index] * 1.25)
            if local_open < source_apertures[eye_index] * 0.55 or local_open <= 1e-6:
                closures = []
                break
            closures.append((local_open - current[eye_index]) / local_open)
        if len(closures) != 2:
            continue
        imbalance = abs(closures[0] - closures[1])
        if (
            min(closures) < config.minimum_blink_closure
            or imbalance > config.maximum_blink_imbalance
        ):
            continue
        score = min(closures) - (0.25 * imbalance)
        candidates.append((score, -center, center, closures[0], closures[1], imbalance))
    if not candidates:
        raise FaceStabilizationError(FaceStabilizationReason.BLINK_GUARD)
    _score, _tie_break, center, left_closure, right_closure, imbalance = max(candidates)
    return _BlinkSelection(
        center=center,
        left_closure=left_closure,
        right_closure=right_closure,
        imbalance=imbalance,
    )


def _materialize_frames(
    frames: tuple[Path, ...],
    *,
    destination: Path,
    source: _SourceAnalysis,
    analyses: tuple[_FrameAnalysis, ...],
    blink_center: int,
    config: FaceStabilizationConfig,
) -> tuple[tuple[Path, ...], float, float]:
    height, width = source.image.shape[:2]
    face_mask = _face_mask((height, width), source.detection, config)
    eye_mask = _eye_mask((height, width), source.detection, face_mask, config)
    head_mask = source.head_matte
    inverse_rotation = _rotation_matrix((height, width), -source.alignment_angle)
    outputs: list[Path] = []
    blink_weights = {
        blink_center + offset: weight
        for offset, weight in zip(range(-2, 3), _BLINK_WEIGHTS, strict=True)
    }
    source_float = source.image.astype(np.float32)
    original_source_float = source.original_image.astype(np.float32)
    closed_frame = _rotate_image(_read_image(frames[blink_center]), source.alignment_angle)
    closed_frame_to_source = cv2.invertAffineTransform(analyses[blink_center].source_to_frame)
    registered_closed_target = cv2.warpAffine(
        closed_frame,
        closed_frame_to_source,
        (width, height),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_REFLECT_101,
    ).astype(np.float32)
    maximum_seam_mean = 0.0
    maximum_seam_p95 = 0.0
    for index, frame_path in enumerate(frames):
        original = _read_image(frame_path)
        replacement = source_float.copy()
        blink_weight = blink_weights.get(index)
        if blink_weight is not None:
            blink_alpha = (eye_mask * blink_weight)[..., None]
            replacement = (replacement * (1.0 - blink_alpha)) + (
                registered_closed_target * blink_alpha
            )
        if source.alignment_angle:
            replacement_original = cv2.warpAffine(
                replacement,
                inverse_rotation,
                (width, height),
                flags=cv2.INTER_LINEAR,
                borderMode=cv2.BORDER_REFLECT_101,
            )
            face_alpha_original = cv2.warpAffine(
                face_mask,
                inverse_rotation,
                (width, height),
                flags=cv2.INTER_LINEAR,
                borderMode=cv2.BORDER_CONSTANT,
                borderValue=0,
            )
            head_alpha_original = cv2.warpAffine(
                head_mask,
                inverse_rotation,
                (width, height),
                flags=cv2.INTER_LINEAR,
                borderMode=cv2.BORDER_CONSTANT,
                borderValue=0,
            )
        else:
            replacement_original = replacement
            face_alpha_original = face_mask.copy()
            head_alpha_original = head_mask.copy()
        face_alpha_original[face_alpha_original < config.minimum_alpha] = 0.0
        head_alpha_original[head_alpha_original < config.minimum_alpha] = 0.0
        static_head_alpha = head_alpha_original * (1.0 - face_alpha_original)
        union_alpha = cast(
            FloatArray,
            np.clip(face_alpha_original + static_head_alpha, 0.0, 1.0).astype(np.float32),
        )
        face_alpha = face_alpha_original[..., None]
        head_alpha = static_head_alpha[..., None]
        generated_alpha = (1.0 - union_alpha)[..., None]
        original_float = original.astype(np.float32)
        composed = np.clip(
            np.rint(
                (original_float * generated_alpha)
                + (original_source_float * head_alpha)
                + (replacement_original * face_alpha)
            ),
            0,
            255,
        ).astype(np.uint8)
        outside = union_alpha == 0
        composed[outside] = original[outside]
        if not np.array_equal(composed[outside], original[outside]):
            raise FaceStabilizationError(FaceStabilizationReason.OUTER_PIXEL_GUARD)
        seam_mean, seam_p95 = _guard_visible_seam(
            original,
            composed,
            union_alpha,
            config,
        )
        maximum_seam_mean = max(maximum_seam_mean, seam_mean)
        maximum_seam_p95 = max(maximum_seam_p95, seam_p95)
        output = destination / f"frame-{index:06d}.png"
        if not cv2.imwrite(
            output.as_posix(),
            composed,
            (cv2.IMWRITE_PNG_COMPRESSION, 6),
        ):
            raise FaceStabilizationError(FaceStabilizationReason.OUTPUT_FAILED)
        outputs.append(output)
    return tuple(outputs), maximum_seam_mean, maximum_seam_p95


def _face_mask(
    dimensions: tuple[int, int],
    detection: _Detection,
    config: FaceStabilizationConfig,
) -> FloatArray:
    height, width = dimensions
    x0, y0, x1, y1 = (float(value) for value in detection.bbox[:4])
    face_width = x1 - x0
    face_height = y1 - y0
    mask = np.zeros((height, width), dtype=np.float32)
    cv2.ellipse(
        mask,
        (round((x0 + x1) / 2), round((y0 + y1) / 2)),
        (
            max(1, round(face_width * config.face_radius_x_ratio)),
            max(1, round(face_height * config.face_radius_y_ratio)),
        ),
        0,
        0,
        360,
        1.0,
        -1,
    )
    blurred = cast(
        FloatArray,
        cv2.GaussianBlur(mask, (0, 0), face_width * config.face_feather_ratio),
    )
    return np.clip(blurred, 0.0, 1.0).astype(np.float32)


def _eye_mask(
    dimensions: tuple[int, int],
    detection: _Detection,
    face_mask: FloatArray,
    config: FaceStabilizationConfig,
) -> FloatArray:
    height, width = dimensions
    face_width = float(detection.bbox[2] - detection.bbox[0])
    face_height = float(detection.bbox[3] - detection.bbox[1])
    mask = np.zeros((height, width), dtype=np.float32)
    for eye_slice in (_LEFT_EYE, _RIGHT_EYE):
        points = detection.keypoints[eye_slice, :2]
        center = np.mean(points, axis=0)
        point_width = float(np.max(points[:, 0]) - np.min(points[:, 0]))
        point_height = float(np.max(points[:, 1]) - np.min(points[:, 1]))
        radius_x = min(
            face_width * 0.17,
            max(face_width * config.eye_radius_x_ratio, point_width * 0.80),
        )
        radius_y = min(
            face_height * 0.10,
            max(face_height * config.eye_radius_y_ratio, point_height * 0.85),
        )
        cv2.ellipse(
            mask,
            (round(float(center[0])), round(float(center[1]))),
            (max(1, round(radius_x)), max(1, round(radius_y))),
            0,
            0,
            360,
            1.0,
            -1,
        )
    blurred = cast(
        FloatArray,
        cv2.GaussianBlur(mask, (0, 0), face_width * config.eye_feather_ratio),
    )
    return np.minimum(np.clip(blurred, 0.0, 1.0), face_mask).astype(np.float32)


def _guard_visible_seam(
    generated: UInt8Image,
    composed: UInt8Image,
    union_mask: FloatArray,
    config: FaceStabilizationConfig,
) -> tuple[float, float]:
    boundary = (union_mask >= 0.02) & (union_mask <= 0.20)
    if int(np.count_nonzero(boundary)) < 32:
        raise FaceStabilizationError(FaceStabilizationReason.SEAM_GUARD)
    delta = np.mean(
        np.abs(composed.astype(np.float32) - generated.astype(np.float32)),
        axis=2,
    )[boundary]
    mean_delta = float(np.mean(delta))
    p95_delta = float(np.percentile(delta, 95))
    if mean_delta > config.maximum_seam_mean_delta or p95_delta > config.maximum_seam_p95_delta:
        raise FaceStabilizationError(FaceStabilizationReason.SEAM_GUARD)
    return mean_delta, p95_delta


def _rotate_image(image: UInt8Image, angle: float) -> UInt8Image:
    if angle == 0:
        return image.copy()
    height, width = image.shape[:2]
    return cast(
        UInt8Image,
        cv2.warpAffine(
            image,
            _rotation_matrix((height, width), angle),
            (width, height),
            flags=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_REPLICATE,
        ),
    )


def _rotation_matrix(dimensions: tuple[int, int], angle: float) -> FloatArray:
    height, width = dimensions
    return cast(
        FloatArray,
        cv2.getRotationMatrix2D((width / 2, height / 2), angle, 1.0).astype(np.float32),
    )
