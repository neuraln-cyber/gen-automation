from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import cv2
import numpy as np
import pytest
from numpy.typing import NDArray

from gen_automation.i2v_worker.face_stabilizer import (
    FaceDetector,
    FaceStabilizationConfig,
    FaceStabilizationError,
    face_stabilizer_capability,
    preflight_face_stabilizer,
    preflight_source_face,
    stabilize_face_frames,
)

ImageArray = NDArray[np.uint8]
FloatArray = NDArray[np.float32]


class _FakeDetector:
    def __init__(
        self,
        source_predictions: Sequence[Mapping[str, object]],
        frame_predictions: Sequence[Mapping[str, object]],
    ) -> None:
        self.source_predictions = source_predictions
        self.frame_predictions = iter(frame_predictions)
        self.source_calls = 0
        self.frame_calls = 0
        self.box_arguments: list[list[FloatArray] | None] = []

    def __call__(
        self,
        _image: ImageArray,
        /,
        boxes: list[FloatArray] | None = None,
    ) -> Sequence[Mapping[str, object]]:
        self.box_arguments.append(boxes)
        if self.source_calls == 0:
            self.source_calls += 1
            return self.source_predictions
        self.frame_calls += 1
        prediction = next(self.frame_predictions)
        return (prediction,)


def _keypoints(*, eye_aperture: float = 1.0, dx: float = 0, dy: float = 0) -> FloatArray:
    points = np.asarray(
        (
            (40, 45),
            (38, 65),
            (64, 104),
            (90, 65),
            (88, 45),
            (44, 45),
            (50, 43),
            (57, 45),
            (70, 45),
            (77, 43),
            (84, 45),
            (46, 53),
            (51, 51),
            (57, 53),
            (46, 59),
            (51, 61),
            (57, 59),
            (71, 53),
            (77, 51),
            (83, 53),
            (71, 59),
            (77, 61),
            (83, 59),
            (64, 70),
            (55, 82),
            (61, 84),
            (67, 84),
            (73, 82),
        ),
        dtype=np.float32,
    )
    for eye_start in (11, 17):
        top_mean = float(np.mean(points[eye_start : eye_start + 3, 1]))
        for index in range(eye_start + 3, eye_start + 6):
            points[index, 1] = top_mean + ((points[index, 1] - top_mean) * eye_aperture)
    points[:, 0] += dx
    points[:, 1] += dy
    scores = np.full((28, 1), 0.99, dtype=np.float32)
    return np.concatenate((points, scores), axis=1)


def _prediction(
    *,
    eye_aperture: float = 1.0,
    face_score: float = 0.99,
    dx: float = 0,
    dy: float = 0,
) -> dict[str, object]:
    return {
        "bbox": np.asarray((32, 16, 96, 112, face_score), dtype=np.float32),
        "keypoints": _keypoints(eye_aperture=eye_aperture, dx=dx, dy=dy),
    }


def _paint_source() -> ImageArray:
    image = np.full((128, 128, 3), (255, 255, 255), dtype=np.uint8)
    cv2.ellipse(image, (64, 60), (34, 50), 0, 0, 360, (70, 30, 95), -1)
    cv2.ellipse(image, (64, 64), (24, 42), 0, 0, 360, (170, 160, 150), -1)
    for center in ((52, 56), (77, 56)):
        cv2.ellipse(image, center, (7, 4), 0, 0, 360, (230, 220, 210), -1)
    cv2.line(image, (57, 83), (71, 83), (60, 70, 80), 2)
    return image


def _paint_frame(source: ImageArray, *, index: int, eye_aperture: float) -> ImageArray:
    frame = source.copy()
    frame[5:10, 5:10] = (index, 100, 200)
    cv2.rectangle(frame, (54, 78), (74, 92), (20, 30 + index, 220), -1)
    if eye_aperture < 0.9:
        for center in ((52, 56), (77, 56)):
            cv2.ellipse(
                frame,
                center,
                (7, max(1, round(4 * eye_aperture))),
                0,
                0,
                360,
                (15 + (20 * index), 20, 25),
                -1,
            )
    return frame


def _write_image(path: Path, image: ImageArray) -> None:
    assert cv2.imwrite(path.as_posix(), image, (cv2.IMWRITE_PNG_COMPRESSION, 6))


def _fixture(
    tmp_path: Path,
    *,
    apertures: tuple[float, ...] = (1, 1, 1, 0.75, 0.45, 0.12, 0.45, 0.75, 1, 1, 1),
    source_prediction: Mapping[str, object] | None = None,
    frame_predictions: Sequence[Mapping[str, object]] | None = None,
) -> tuple[Path, tuple[Path, ...], _FakeDetector]:
    source_image = _paint_source()
    source_path = tmp_path / "prepared.png"
    _write_image(source_path, source_image)
    frame_paths: list[Path] = []
    for index, aperture in enumerate(apertures):
        frame_path = tmp_path / f"native-{index:03d}.png"
        _write_image(
            frame_path,
            _paint_frame(source_image, index=index, eye_aperture=aperture),
        )
        frame_paths.append(frame_path)
    predictions = frame_predictions or tuple(
        _prediction(eye_aperture=aperture) for aperture in apertures
    )
    detector = _FakeDetector(
        (source_prediction or _prediction(),),
        predictions,
    )
    return source_path, tuple(frame_paths), detector


def _test_config(**updates: Any) -> FaceStabilizationConfig:
    return replace(
        FaceStabilizationConfig(alignment_angles=(0.0,)),
        **updates,
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_preflight_uses_injected_create_detector_without_model_files() -> None:
    calls: list[dict[str, object]] = []
    expected = _FakeDetector((_prediction(),), ())

    def factory(
        face_detector_name: str = "yolov3",
        landmark_model_name: str = "hrnetv2",
        device: str = "cuda:0",
        flip_test: bool = True,
        box_scale_factor: float = 1.1,
    ) -> FaceDetector:
        calls.append(
            {
                "face_detector_name": face_detector_name,
                "landmark_model_name": landmark_model_name,
                "device": device,
                "flip_test": flip_test,
                "box_scale_factor": box_scale_factor,
            }
        )
        return expected

    detector = preflight_face_stabilizer(
        detector_factory=factory,
        device="cpu",
    )

    assert detector is expected
    assert calls == [
        {
            "face_detector_name": "yolov3",
            "landmark_model_name": "hrnetv2",
            "device": "cpu",
            "flip_test": False,
            "box_scale_factor": 1.1,
        }
    ]


def test_capability_identity_is_deterministic_and_contains_no_biometric_state() -> None:
    first = face_stabilizer_capability()
    second = face_stabilizer_capability()

    assert first == second
    assert first is not second
    encoded = json.dumps(first, sort_keys=True)
    for forbidden in ("bbox", "keypoints", "image_bytes", "checkpoint_path"):
        assert forbidden not in encoded


def test_preflight_redacts_detector_factory_failures() -> None:
    def factory(
        face_detector_name: str = "yolov3",
        landmark_model_name: str = "hrnetv2",
        device: str = "cuda:0",
        flip_test: bool = True,
        box_scale_factor: float = 1.1,
    ) -> FaceDetector:
        del face_detector_name, landmark_model_name, device, flip_test, box_scale_factor
        raise RuntimeError("secret checkpoint path")

    with pytest.raises(FaceStabilizationError, match=r"^face stabilizer is unavailable$"):
        preflight_face_stabilizer(detector_factory=factory)


@pytest.mark.parametrize(
    "updates",
    (
        {"maximum_seam_mean_delta": float("nan")},
        {"maximum_seam_p95_delta": float("nan")},
    ),
)
def test_nonfinite_visible_seam_thresholds_fail_configuration_before_inference(
    tmp_path: Path,
    updates: dict[str, float],
) -> None:
    source_path, _frames, detector = _fixture(tmp_path)

    with pytest.raises(
        FaceStabilizationError,
        match=r"^face stabilization configuration is invalid$",
    ):
        preflight_source_face(source_path, detector, config=_test_config(**updates))

    assert detector.source_calls == 0
    assert detector.frame_calls == 0


def test_checkpoint_verification_hashes_exact_regular_file(tmp_path: Path) -> None:
    import gen_automation.i2v_worker.face_stabilizer as module

    checkpoint = tmp_path / "detector.safetensors"
    checkpoint.write_bytes(b"exact detector bytes")
    digest = hashlib.sha256(checkpoint.read_bytes()).hexdigest()

    module._verify_checkpoint(
        checkpoint,
        expected_sha256=digest,
        expected_byte_size=checkpoint.stat().st_size,
    )
    with pytest.raises(FaceStabilizationError, match=r"^face stabilizer is unavailable$"):
        module._verify_checkpoint(
            checkpoint,
            expected_sha256="0" * 64,
            expected_byte_size=checkpoint.stat().st_size,
        )
    with pytest.raises(FaceStabilizationError, match=r"^face stabilizer is unavailable$"):
        module._verify_checkpoint(
            checkpoint,
            expected_sha256=digest,
            expected_byte_size=checkpoint.stat().st_size + 1,
        )


def test_source_core_is_locked_one_blink_is_kept_and_outer_pixels_are_exact(
    tmp_path: Path,
) -> None:
    source_path, frames, detector = _fixture(tmp_path)
    config = _test_config()
    source_analysis = preflight_source_face(source_path, detector, config=config)
    assert detector.source_calls == 1
    assert detector.frame_calls == 0

    result = stabilize_face_frames(
        source_path,
        frames,
        tmp_path / "locked",
        detector=detector,
        source_analysis=source_analysis,
        config=config,
    )

    assert detector.source_calls == 1
    assert detector.frame_calls == len(frames)
    assert detector.box_arguments == [None] * (len(frames) + 1)
    assert result.metadata == {
        "schema": "gen-automation/i2v-face-stabilization/v2",
        "algorithm": "dasiwa-static-source-head-single-blink/v2",
        "guard_profile": "static-head-near-white-v2",
        "guard_profile_sha256": (
            "5b70301683fabc1b9da12cfbe17d719b6e346d6c8e7f1da96f5d5c90cc2eb54f"
        ),
        "detector_revision": "7db835de7a3a052eb4d68d241ae9f2cf28a0b509",
        "detector_wheel_sha256": (
            "9a6a8c1384b7a57fab8ce9988f814271ff88bac52a9dd871490a28b61dff7692"
        ),
        "yolo_sha256": "23bbc708146bcbc1c910f00fe152adbc70d7658d875a0121eaf4ee61d978b2c4",
        "hrnet_sha256": "e71271376406a743c01528a0460637fcc06e72aeeea583f85007cc72dc8b7a4a",
        "opencv_sha256": "211e581f5a4670acbbe08fff36a35e9946039d2eea28b80394632d036d1be527",
        "blink_events": 1,
        "blink_center_frame": 5,
        "blink_window_frames": 5,
        "frame_count": 11,
        "metrics": {
            "source_face_score": 0.99,
            "source_landmark_min_score": 0.99,
            "source_landmark_mean_score": 0.99,
            "maximum_translation_ratio": 0.0,
            "maximum_rotation_degrees": 0.0,
            "maximum_scale_delta": 0.0,
            "maximum_anchor_residual_ratio": 0.0,
            "maximum_seam_mean_delta": 0.0,
            "maximum_seam_p95_delta": 0.0,
            "blink_left_closure": 0.88,
            "blink_right_closure": 0.88,
            "blink_imbalance": 0.0,
        },
    }
    source = cv2.imread(source_path.as_posix(), cv2.IMREAD_COLOR)
    assert source is not None
    for index, (input_path, output_path) in enumerate(zip(frames, result.frames, strict=True)):
        original = cv2.imread(input_path.as_posix(), cv2.IMREAD_COLOR)
        output = cv2.imread(output_path.as_posix(), cv2.IMREAD_COLOR)
        assert original is not None and output is not None
        assert np.array_equal(output[5:10, 5:10], original[5:10, 5:10])
        # Opaque attached hair outside the face/eye core stays byte-static.
        assert np.array_equal(output[60, 35], source[60, 35])
        assert np.array_equal(output[83, 64], source[83, 64])
        blink_weights = {3: 0.15, 4: 0.55, 5: 1.0, 6: 0.55, 7: 0.15}
        if index in blink_weights:
            selected = cv2.imread(frames[5].as_posix(), cv2.IMREAD_COLOR)
            assert selected is not None
            weight = blink_weights[index]
            expected = np.rint(
                (source[56, 52].astype(np.float32) * (1.0 - weight))
                + (selected[56, 52].astype(np.float32) * weight)
            ).astype(np.uint8)
            assert np.array_equal(output[56, 52], expected)
        else:
            assert np.array_equal(output[56, 52], source[56, 52])


def test_textured_background_fails_source_preflight_before_detector_or_frames(
    tmp_path: Path,
) -> None:
    source_path, _frames, detector = _fixture(tmp_path)
    textured = cv2.imread(source_path.as_posix(), cv2.IMREAD_COLOR)
    assert textured is not None
    grid_y, grid_x = np.indices(textured.shape[:2])
    checker = ((grid_x + grid_y) % 2) == 0
    textured[checker] = (170, 190, 210)
    textured[~checker] = (50, 70, 90)
    _write_image(source_path, cast(ImageArray, textured))

    with pytest.raises(FaceStabilizationError, match=r"^source face contract failed$"):
        preflight_source_face(source_path, detector, config=_test_config())

    assert detector.source_calls == 0
    assert detector.frame_calls == 0


def test_head_matte_is_immutable_digest_bound_source_state(tmp_path: Path) -> None:
    source_path, frames, detector = _fixture(tmp_path)
    config = _test_config()
    source_analysis = preflight_source_face(source_path, detector, config=config)
    changed_matte = bytearray(source_analysis._head_matte_bytes)
    changed_matte[len(changed_matte) // 2] ^= 1
    tampered = replace(source_analysis, _head_matte_bytes=bytes(changed_matte))

    with pytest.raises(
        FaceStabilizationError,
        match=r"^source face preflight token is invalid$",
    ):
        stabilize_face_frames(
            source_path,
            frames,
            tmp_path / "tampered-matte",
            detector=detector,
            source_analysis=tampered,
            config=config,
        )

    assert detector.source_calls == 1
    assert detector.frame_calls == 0


def test_visible_seam_guard_measures_composed_delta_not_raw_source_delta(
    tmp_path: Path,
) -> None:
    source_path, frames, detector = _fixture(tmp_path)
    source_analysis = preflight_source_face(source_path, detector, config=_test_config())
    source = cv2.imread(source_path.as_posix(), cv2.IMREAD_COLOR)
    assert source is not None
    matte = np.frombuffer(source_analysis._head_matte_bytes, dtype="<f4").reshape(source.shape[:2])
    boundary = (matte >= 0.02) & (matte <= 0.20)
    assert int(np.count_nonzero(boundary)) >= 32
    for frame_path in frames:
        shifted = np.clip(source.astype(np.int16) - 80, 0, 255).astype(np.uint8)
        _write_image(frame_path, cast(ImageArray, shifted))
    raw_delta = np.mean(
        np.abs(source.astype(np.float32) - shifted.astype(np.float32)),
        axis=2,
    )[boundary]
    assert float(np.percentile(raw_delta, 95)) > 22.4

    result = stabilize_face_frames(
        source_path,
        frames,
        tmp_path / "visible-seam",
        detector=detector,
        source_analysis=source_analysis,
        config=_test_config(),
    )

    metrics = cast(dict[str, float], result.metadata["metrics"])
    assert 0 < metrics["maximum_seam_mean_delta"] <= 12.8
    assert 0 < metrics["maximum_seam_p95_delta"] <= 22.4


def test_output_sequence_is_byte_deterministic(tmp_path: Path) -> None:
    source_path, frames, first_detector = _fixture(tmp_path)
    first = stabilize_face_frames(
        source_path,
        frames,
        tmp_path / "first",
        detector=first_detector,
        config=_test_config(),
    )
    _, _, second_detector = _fixture(tmp_path)
    second = stabilize_face_frames(
        source_path,
        frames,
        tmp_path / "second",
        detector=second_detector,
        config=_test_config(),
    )

    assert [_sha256(path) for path in first.frames] == [_sha256(path) for path in second.frames]
    assert first.metadata == second.metadata


def test_low_confidence_or_multiple_source_face_fails_before_output(tmp_path: Path) -> None:
    source_path, frames, low_detector = _fixture(
        tmp_path,
        source_prediction=_prediction(face_score=0.80),
    )
    with pytest.raises(FaceStabilizationError, match=r"^source face contract failed$"):
        preflight_source_face(
            source_path,
            low_detector,
            config=_test_config(),
        )

    assert low_detector.source_calls == 1
    assert low_detector.frame_calls == 0

    multiple_detector = _FakeDetector((_prediction(), _prediction()), ())
    with pytest.raises(FaceStabilizationError, match=r"^source face contract failed$"):
        stabilize_face_frames(
            source_path,
            frames,
            tmp_path / "multiple",
            detector=multiple_detector,
            config=_test_config(),
        )


def test_source_preflight_token_rejects_a_changed_prepared_image_before_frames(
    tmp_path: Path,
) -> None:
    source_path, frames, detector = _fixture(tmp_path)
    config = _test_config()
    source_analysis = preflight_source_face(source_path, detector, config=config)
    changed = cv2.imread(source_path.as_posix(), cv2.IMREAD_COLOR)
    assert changed is not None
    changed[0, 0] = (255, 0, 0)
    _write_image(source_path, cast(ImageArray, changed))

    with pytest.raises(
        FaceStabilizationError,
        match=r"^source face preflight token is invalid$",
    ):
        stabilize_face_frames(
            source_path,
            frames,
            tmp_path / "mismatched-source",
            detector=detector,
            source_analysis=source_analysis,
            config=config,
        )

    assert detector.source_calls == 1
    assert detector.frame_calls == 0


def test_pose_drift_fails_closed_before_materialization(tmp_path: Path) -> None:
    apertures = (1.0,) * 11
    predictions = [_prediction(eye_aperture=value) for value in apertures]
    predictions[0] = _prediction(eye_aperture=1.0, dx=20)
    source_path, frames, detector = _fixture(
        tmp_path,
        apertures=apertures,
        frame_predictions=predictions,
    )
    destination = tmp_path / "drifted"

    with pytest.raises(FaceStabilizationError, match=r"^face pose guard failed$"):
        stabilize_face_frames(
            source_path,
            frames,
            destination,
            detector=detector,
            config=_test_config(),
        )

    assert not destination.exists()


def test_missing_bilateral_blink_fails_closed(tmp_path: Path) -> None:
    apertures = (1.0,) * 11
    source_path, frames, detector = _fixture(tmp_path, apertures=apertures)
    destination = tmp_path / "no-blink"

    with pytest.raises(FaceStabilizationError, match=r"^bilateral blink guard failed$"):
        stabilize_face_frames(
            source_path,
            frames,
            destination,
            detector=detector,
            config=_test_config(),
        )

    assert not destination.exists()


def test_seam_guard_removes_partial_output(tmp_path: Path) -> None:
    source_path, frames, detector = _fixture(tmp_path)
    for frame in frames:
        image = cv2.imread(frame.as_posix(), cv2.IMREAD_COLOR)
        assert image is not None
        image[:] = (175, 175, 175)
        _write_image(frame, cast(ImageArray, image))
    destination = tmp_path / "bad-seam"

    with pytest.raises(FaceStabilizationError, match=r"^face seam guard failed$"):
        stabilize_face_frames(
            source_path,
            frames,
            destination,
            detector=detector,
            config=_test_config(maximum_seam_mean_delta=0.1, maximum_seam_p95_delta=0.2),
        )

    assert not destination.exists()
