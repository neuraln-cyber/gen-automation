from collections.abc import Callable
from io import BytesIO

import PIL
import pytest
from PIL import Image, ImageDraw, ImageFilter

from gen_automation.quality import (
    MICROS,
    SCORER_VERSION,
    DuplicateCandidate,
    QualityBatchError,
    QualityConfig,
    QualityConfigurationError,
    UnsafeImageError,
    analyze_batch,
    analyze_image,
    cluster_near_duplicates,
    hamming_distance,
    quality_config_sha256,
)


def _encoded(
    image: Image.Image,
    *,
    image_format: str = "PNG",
    **save_options: object,
) -> bytes:
    output = BytesIO()
    image.save(output, format=image_format, **save_options)
    return output.getvalue()


def _checkerboard(size: int = 128, block: int = 4) -> Image.Image:
    image = Image.new("L", (size, size))
    pixels = image.load()
    assert pixels is not None
    for y_position in range(size):
        for x_position in range(size):
            pixels[x_position, y_position] = (
                255 if (x_position // block + y_position // block) % 2 else 0
            )
    return image


def _shape_image(*, offset: int = 0) -> Image.Image:
    image = Image.new("L", (128, 96), 16)
    draw = ImageDraw.Draw(image)
    draw.rectangle((20 + offset, 15, 86 + offset, 72), fill=225)
    draw.ellipse((42 + offset, 28, 70 + offset, 56), fill=48)
    draw.line((8, 88, 116, 8), fill=160, width=3)
    return image


def test_blank_image_has_zero_texture_metrics_and_stable_versions() -> None:
    result = analyze_image(_encoded(Image.new("L", (64, 64), 128)))

    assert result.metrics.luminance_mean_micros == 501_961
    assert result.metrics.luminance_std_micros == 0
    assert result.metrics.dynamic_range_micros == 0
    assert result.metrics.entropy_bits_micros == 0
    assert result.metrics.entropy_normalized_micros == 0
    assert result.metrics.sharpness_micros == 0
    assert result.thumbnail.width == 64
    assert result.thumbnail.height == 64
    assert len(result.thumbnail.luminance) == 64 * 64
    assert result.score_micros == result.score_breakdown.total_micros
    assert result.config == QualityConfig()
    assert result.config_sha256 == quality_config_sha256(result.config)
    assert len(result.config_sha256) == 64
    assert result.scorer_version == SCORER_VERSION
    assert result.pillow_version == PIL.__version__
    assert result.dhash_hex == f"{result.dhash64:016x}"


def test_sharp_image_scores_above_blurred_version() -> None:
    sharp = _checkerboard()
    blurred = sharp.filter(ImageFilter.GaussianBlur(radius=4))

    sharp_result = analyze_image(_encoded(sharp))
    blurred_result = analyze_image(_encoded(blurred))

    assert sharp_result.metrics.sharpness_micros > blurred_result.metrics.sharpness_micros
    assert sharp_result.metrics.dynamic_range_micros >= (
        blurred_result.metrics.dynamic_range_micros
    )
    assert sharp_result.score_micros > blurred_result.score_micros


def test_configuration_digest_is_stable_and_changes_with_scoring_policy() -> None:
    default = QualityConfig()
    changed = QualityConfig(
        exposure_weight_micros=150_000,
        contrast_weight_micros=150_000,
    )

    assert quality_config_sha256(default) == quality_config_sha256(QualityConfig())
    assert quality_config_sha256(default) != quality_config_sha256(changed)


def test_entropy_and_dynamic_range_distinguish_gradient_from_blank() -> None:
    gradient = Image.new("L", (256, 64))
    pixels = gradient.load()
    assert pixels is not None
    for y_position in range(gradient.height):
        for x_position in range(gradient.width):
            pixels[x_position, y_position] = x_position

    blank_result = analyze_image(_encoded(Image.new("L", (256, 64), 127)))
    gradient_result = analyze_image(_encoded(gradient))

    assert gradient_result.metrics.entropy_bits_micros > 5 * MICROS
    assert gradient_result.metrics.entropy_normalized_micros > 600_000
    assert gradient_result.metrics.dynamic_range_micros > 950_000
    assert gradient_result.metrics.entropy_bits_micros > (blank_result.metrics.entropy_bits_micros)


def test_analysis_is_repeatable_and_does_not_mutate_mutable_input() -> None:
    payload = bytearray(_encoded(_shape_image()))
    original = bytes(payload)

    first = analyze_image(payload)
    second = analyze_image(memoryview(payload))

    assert bytes(payload) == original
    assert first == second


def test_pixel_identical_encodings_have_exact_dhash() -> None:
    image = _shape_image()
    low_compression = _encoded(image, compress_level=0)
    high_compression = _encoded(image, compress_level=9)

    first = analyze_image(low_compression)
    second = analyze_image(high_compression)

    assert low_compression != high_compression
    assert first.sha256 != second.sha256
    assert first.dhash64 == second.dhash64
    assert hamming_distance(first.dhash64, second.dhash64) == 0


def test_visually_near_images_have_near_dhashes() -> None:
    first = analyze_image(_encoded(_shape_image(offset=0)))
    second = analyze_image(_encoded(_shape_image(offset=1)))

    distance = hamming_distance(first.dhash64, second.dhash64)

    assert 0 <= distance <= 8


def test_hamming_distance_uses_all_64_bits_and_rejects_invalid_values() -> None:
    assert hamming_distance(0, (1 << 64) - 1) == 64
    assert hamming_distance(0b1010, 0b0011) == 2

    for invalid in (-1, 1 << 64, True, "0"):
        with pytest.raises(ValueError, match="unsigned 64-bit"):
            hamming_distance(invalid, 0)  # type: ignore[arg-type]


def test_clustering_is_order_independent_transitive_and_explainable() -> None:
    candidates = (
        DuplicateCandidate("a", 0b0000, 100_000),
        DuplicateCandidate("b", 0b0001, 900_000),
        DuplicateCandidate("c", 0b0011, 800_000),
        DuplicateCandidate("d", (1 << 64) - 1, 700_000),
    )
    config = QualityConfig(near_duplicate_hamming_threshold=1)

    forward = cluster_near_duplicates(candidates, config=config)
    reverse = cluster_near_duplicates(reversed(candidates), config=config)

    assert forward == reverse
    assert tuple(cluster.member_ids for cluster in forward) == (("a", "b", "c"), ("d",))
    assert forward[0].representative_id == "b"
    assert forward[0].max_pairwise_hamming == 2
    assert forward[0].link_threshold == 1
    assert len(forward[0].cluster_id) == 64
    assert forward[1].max_pairwise_hamming == 0


def test_exact_duplicate_clustering_and_result_candidate() -> None:
    result = analyze_image(_encoded(_shape_image()))
    candidates = (
        DuplicateCandidate.from_result("second", result),
        DuplicateCandidate.from_result("first", result),
    )

    clusters = cluster_near_duplicates(
        candidates,
        config=QualityConfig(near_duplicate_hamming_threshold=0),
    )

    assert len(clusters) == 1
    assert clusters[0].member_ids == ("first", "second")
    assert clusters[0].representative_id == "first"
    assert clusters[0].max_pairwise_hamming == 0


def test_duplicate_clustering_rejects_duplicate_ids_invalid_scores_and_large_batches() -> None:
    with pytest.raises(QualityBatchError, match="identifiers"):
        cluster_near_duplicates(
            (
                DuplicateCandidate("same", 0, 1),
                DuplicateCandidate("same", 1, 2),
            )
        )

    with pytest.raises(QualityBatchError, match="score"):
        cluster_near_duplicates((DuplicateCandidate("bad", 0, MICROS + 1),))

    config = QualityConfig(max_batch_size=2)
    with pytest.raises(QualityBatchError, match="exceeds"):
        cluster_near_duplicates(
            (
                DuplicateCandidate("a", 0, 1),
                DuplicateCandidate("b", 0, 1),
                DuplicateCandidate("c", 0, 1),
            ),
            config=config,
        )


def test_batch_is_bounded_and_sorted_without_mutating_mapping() -> None:
    payload = _encoded(Image.new("RGB", (32, 32), "navy"))
    images = {"z": payload, "a": payload}
    snapshot = dict(images)

    results = analyze_batch(images, config=QualityConfig(max_batch_size=2))

    assert tuple(item.identifier for item in results) == ("a", "z")
    assert images == snapshot

    with pytest.raises(QualityBatchError, match="batch"):
        analyze_batch(
            {"a": payload, "b": payload, "c": payload},
            config=QualityConfig(max_batch_size=2),
        )


@pytest.mark.parametrize(
    "payload, config, message",
    [
        (b"", QualityConfig(), "empty"),
        (b"not an image", QualityConfig(), "malformed or unsafe"),
        (b"x" * 11, QualityConfig(max_input_bytes=10), "byte limit"),
        (
            _encoded(Image.new("RGB", (33, 16), "black")),
            QualityConfig(max_width=32),
            "dimensions",
        ),
        (
            _encoded(Image.new("RGB", (20, 20), "black")),
            QualityConfig(max_pixels=399),
            "pixels",
        ),
        (
            _encoded(Image.new("RGB", (21, 1), "black")),
            QualityConfig(),
            "aspect ratio",
        ),
        (
            _encoded(Image.new("RGB", (16, 16), "red"), image_format="GIF"),
            QualityConfig(),
            "unsupported",
        ),
    ],
)
def test_malformed_and_oversized_inputs_fail_closed(
    payload: bytes,
    config: QualityConfig,
    message: str,
) -> None:
    with pytest.raises(UnsafeImageError, match=message):
        analyze_image(payload, config=config)


def test_multiframe_image_is_rejected() -> None:
    output = BytesIO()
    first = Image.new("RGB", (16, 16), "red")
    second = Image.new("RGB", (16, 16), "blue")
    first.save(
        output,
        format="WEBP",
        save_all=True,
        append_images=[second],
        duration=100,
        loop=0,
    )

    with pytest.raises(UnsafeImageError, match="multi-frame"):
        analyze_image(output.getvalue())


@pytest.mark.parametrize(
    "factory",
    [
        lambda: QualityConfig(max_input_bytes=0),
        lambda: QualityConfig(max_width=True),
        lambda: QualityConfig(max_pixels=64_000_001),
        lambda: QualityConfig(max_aspect_ratio_micros=MICROS - 1),
        lambda: QualityConfig(thumbnail_size=15),
        lambda: QualityConfig(max_batch_size=0),
        lambda: QualityConfig(near_duplicate_hamming_threshold=65),
        lambda: QualityConfig(exposure_tolerance_micros=0),
        lambda: QualityConfig(contrast_target_micros=0),
        lambda: QualityConfig(sharpness_target_micros=MICROS + 1),
        lambda: QualityConfig(exposure_weight_micros=99_999),
        lambda: QualityConfig(sharpness_weight_micros=-1),
    ],
)
def test_invalid_quality_configuration_is_rejected(
    factory: Callable[[], QualityConfig],
) -> None:
    with pytest.raises(QualityConfigurationError):
        factory()
