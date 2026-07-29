import hashlib
import json
import math
import warnings
from collections.abc import Mapping
from dataclasses import asdict
from decimal import ROUND_HALF_UP, Decimal, localcontext
from io import BytesIO

import PIL
from PIL import Image, UnidentifiedImageError

from gen_automation.quality.models import (
    DEFAULT_QUALITY_CONFIG,
    MICROS,
    SCORER_VERSION,
    NamedQualityResult,
    NormalizedThumbnail,
    QualityBatchError,
    QualityConfig,
    QualityMetrics,
    QualityResult,
    QualityScoreBreakdown,
    UnsafeImageError,
)

SUPPORTED_IMAGE_FORMATS = frozenset({"JPEG", "PNG", "WEBP"})
DHASH_WIDTH = 8
DHASH_HEIGHT = 8
_MAX_IDENTIFIER_LENGTH = 200

type ImageBytes = bytes | bytearray | memoryview


def analyze_image(
    data: ImageBytes,
    *,
    config: QualityConfig = DEFAULT_QUALITY_CONFIG,
) -> QualityResult:
    """Analyze one image without mutating the input or retaining a Pillow object."""

    payload = _bounded_bytes(data, config.max_input_bytes)
    luminance, image_format, width, height = _decode_luminance(payload, config)
    try:
        thumbnail = _normalized_thumbnail(luminance, config.thumbnail_size)
        metrics = calculate_metrics(thumbnail)
        dhash64 = calculate_dhash64(luminance)
    finally:
        luminance.close()

    breakdown = score_metrics(metrics, config=config)
    return QualityResult(
        sha256=hashlib.sha256(payload).hexdigest(),
        byte_size=len(payload),
        width=width,
        height=height,
        image_format=image_format,
        thumbnail=thumbnail,
        metrics=metrics,
        dhash64=dhash64,
        score_micros=breakdown.total_micros,
        score_breakdown=breakdown,
        config=config,
        config_sha256=quality_config_sha256(config),
        scorer_version=SCORER_VERSION,
        pillow_version=PIL.__version__,
    )


def analyze_batch(
    images: Mapping[str, ImageBytes],
    *,
    config: QualityConfig = DEFAULT_QUALITY_CONFIG,
) -> tuple[NamedQualityResult, ...]:
    """Analyze a bounded mapping in identifier order or fail without partial output."""

    if len(images) > config.max_batch_size:
        raise QualityBatchError("quality batch exceeds the configured limit")

    items = list(images.items())
    for identifier, _ in items:
        _validate_identifier(identifier)
    items.sort(key=lambda item: item[0])

    return tuple(
        NamedQualityResult(
            identifier=identifier,
            result=analyze_image(data, config=config),
        )
        for identifier, data in items
    )


def calculate_metrics(thumbnail: NormalizedThumbnail) -> QualityMetrics:
    """Calculate integer-micro luminance statistics from a normalized thumbnail."""

    values = thumbnail.luminance
    expected_length = thumbnail.width * thumbnail.height
    if expected_length <= 0 or len(values) != expected_length:
        raise ValueError("normalized thumbnail dimensions do not match its bytes")

    count = len(values)
    total = sum(values)
    total_squares = sum(value * value for value in values)
    minimum = min(values)
    maximum = max(values)

    mean_micros = _round_ratio(total * MICROS, 255 * count)
    variance_numerator = count * total_squares - total * total
    std_micros = _round_sqrt_ratio(
        variance_numerator * MICROS * MICROS,
        count * count * 255 * 255,
    )
    dynamic_range_micros = _round_ratio((maximum - minimum) * MICROS, 255)
    entropy_bits_micros = _entropy_bits_micros(values)
    entropy_normalized_micros = _round_ratio(entropy_bits_micros, 8)
    sharpness_micros = _sharpness_micros(
        values,
        width=thumbnail.width,
        height=thumbnail.height,
    )

    return QualityMetrics(
        luminance_mean_micros=mean_micros,
        luminance_std_micros=std_micros,
        dynamic_range_micros=dynamic_range_micros,
        entropy_bits_micros=entropy_bits_micros,
        entropy_normalized_micros=entropy_normalized_micros,
        sharpness_micros=sharpness_micros,
    )


def calculate_dhash64(luminance: Image.Image) -> int:
    """Return a row-major 64-bit difference hash."""

    resized = luminance.resize(
        (DHASH_WIDTH + 1, DHASH_HEIGHT),
        resample=Image.Resampling.LANCZOS,
    )
    try:
        values = resized.tobytes()
    finally:
        resized.close()

    result = 0
    row_width = DHASH_WIDTH + 1
    for y_position in range(DHASH_HEIGHT):
        row_start = y_position * row_width
        for x_position in range(DHASH_WIDTH):
            result <<= 1
            result |= int(values[row_start + x_position] > values[row_start + x_position + 1])
    return result


def score_metrics(
    metrics: QualityMetrics,
    *,
    config: QualityConfig = DEFAULT_QUALITY_CONFIG,
) -> QualityScoreBreakdown:
    """Return normalized components and rounded weighted contributions."""

    exposure_delta = abs(metrics.luminance_mean_micros - config.exposure_target_micros)
    exposure_component = max(
        0,
        MICROS
        - _round_ratio(
            exposure_delta * MICROS,
            config.exposure_tolerance_micros,
        ),
    )
    contrast_component = _scale_to_target(
        metrics.luminance_std_micros,
        config.contrast_target_micros,
    )
    dynamic_range_component = _clamp_micros(metrics.dynamic_range_micros)
    entropy_component = _clamp_micros(metrics.entropy_normalized_micros)
    sharpness_component = _scale_to_target(
        metrics.sharpness_micros,
        config.sharpness_target_micros,
    )

    return QualityScoreBreakdown(
        exposure_component_micros=exposure_component,
        contrast_component_micros=contrast_component,
        dynamic_range_component_micros=dynamic_range_component,
        entropy_component_micros=entropy_component,
        sharpness_component_micros=sharpness_component,
        exposure_contribution_micros=_weighted_component(
            exposure_component,
            config.exposure_weight_micros,
        ),
        contrast_contribution_micros=_weighted_component(
            contrast_component,
            config.contrast_weight_micros,
        ),
        dynamic_range_contribution_micros=_weighted_component(
            dynamic_range_component,
            config.dynamic_range_weight_micros,
        ),
        entropy_contribution_micros=_weighted_component(
            entropy_component,
            config.entropy_weight_micros,
        ),
        sharpness_contribution_micros=_weighted_component(
            sharpness_component,
            config.sharpness_weight_micros,
        ),
    )


def quality_config_sha256(config: QualityConfig) -> str:
    """Return a canonical digest for the exact frozen scorer configuration."""

    encoded = json.dumps(
        asdict(config),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _bounded_bytes(data: ImageBytes, maximum: int) -> bytes:
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise UnsafeImageError("image input must be bytes-like")
    try:
        size = data.nbytes if isinstance(data, memoryview) else len(data)
    except (TypeError, ValueError):
        raise UnsafeImageError("image input is invalid") from None
    if size <= 0:
        raise UnsafeImageError("image input is empty")
    if size > maximum:
        raise UnsafeImageError("image input exceeds the configured byte limit")
    try:
        payload = bytes(data)
    except (MemoryError, TypeError, ValueError):
        raise UnsafeImageError("image input is invalid") from None
    if len(payload) != size:
        raise UnsafeImageError("image input changed while it was copied")
    return payload


def _decode_luminance(
    payload: bytes,
    config: QualityConfig,
) -> tuple[Image.Image, str, int, int]:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(payload)) as image:
                metadata = _validate_image_header(image, config)
                image.verify()

            with Image.open(BytesIO(payload)) as image:
                loaded_metadata = _validate_image_header(image, config)
                if loaded_metadata != metadata:
                    raise UnsafeImageError("image metadata changed during decode")
                image.load()
                rgba = image.convert("RGBA")
                try:
                    background = Image.new("RGBA", image.size, (255, 255, 255, 255))
                    try:
                        flattened = Image.alpha_composite(background, rgba)
                    finally:
                        background.close()
                    try:
                        luminance = flattened.convert("L")
                    finally:
                        flattened.close()
                finally:
                    rgba.close()
    except UnsafeImageError:
        raise
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        MemoryError,
        OSError,
        OverflowError,
        SyntaxError,
        TypeError,
        UnidentifiedImageError,
        ValueError,
    ):
        raise UnsafeImageError("image data is malformed or unsafe") from None
    except Exception:
        # Pillow format plugins are an untrusted parser boundary. Unknown parser
        # failures are redacted and rejected rather than escaping as partial work.
        raise UnsafeImageError("image data is malformed or unsafe") from None

    image_format, width, height = metadata
    return luminance, image_format, width, height


def _validate_image_header(
    image: Image.Image,
    config: QualityConfig,
) -> tuple[str, int, int]:
    image_format = (image.format or "").upper()
    if image_format not in SUPPORTED_IMAGE_FORMATS:
        raise UnsafeImageError("image format is unsupported")
    width, height = image.size
    if width <= 0 or height <= 0:
        raise UnsafeImageError("image dimensions must be positive")
    if width > config.max_width or height > config.max_height:
        raise UnsafeImageError("image dimensions exceed the configured limit")
    if width * height > config.max_pixels:
        raise UnsafeImageError("image pixels exceed the configured limit")
    shorter_side = min(width, height)
    longer_side = max(width, height)
    if longer_side * MICROS > shorter_side * config.max_aspect_ratio_micros:
        raise UnsafeImageError("image aspect ratio exceeds the configured limit")
    if bool(getattr(image, "is_animated", False)) or int(getattr(image, "n_frames", 1)) != 1:
        raise UnsafeImageError("multi-frame images are unsupported")
    return image_format, width, height


def _normalized_thumbnail(
    luminance: Image.Image,
    size: int,
) -> NormalizedThumbnail:
    resized = luminance.resize(
        (size, size),
        resample=Image.Resampling.LANCZOS,
    )
    try:
        values = resized.tobytes()
    finally:
        resized.close()
    if len(values) != size * size:
        raise UnsafeImageError("normalized thumbnail is invalid")
    return NormalizedThumbnail(width=size, height=size, luminance=values)


def _entropy_bits_micros(values: bytes) -> int:
    counts = [0] * 256
    for value in values:
        counts[value] += 1

    with localcontext() as context:
        context.prec = 50
        context.rounding = ROUND_HALF_UP
        sample_count = Decimal(len(values))
        logarithm_two = Decimal(2).ln()
        entropy = Decimal(0)
        for count in counts:
            if count == 0:
                continue
            probability = Decimal(count) / sample_count
            entropy -= probability * (probability.ln() / logarithm_two)
        rounded = (entropy * MICROS).to_integral_value(rounding=ROUND_HALF_UP)
    return max(0, min(8 * MICROS, int(rounded)))


def _sharpness_micros(values: bytes, *, width: int, height: int) -> int:
    if width < 3 or height < 3:
        return 0
    total = 0
    sample_count = 0
    for y_position in range(1, height - 1):
        row_start = y_position * width
        for x_position in range(1, width - 1):
            index = row_start + x_position
            laplacian = (
                4 * values[index]
                - values[index - 1]
                - values[index + 1]
                - values[index - width]
                - values[index + width]
            )
            total += abs(laplacian)
            sample_count += 1
    return _round_ratio(total * MICROS, sample_count * 4 * 255)


def _round_ratio(numerator: int, denominator: int) -> int:
    if numerator < 0 or denominator <= 0:
        raise ValueError("rounding requires a nonnegative ratio")
    quotient, remainder = divmod(numerator, denominator)
    return quotient + int(remainder * 2 >= denominator)


def _round_sqrt_ratio(numerator: int, denominator: int) -> int:
    if numerator < 0 or denominator <= 0:
        raise ValueError("square-root rounding requires a nonnegative ratio")
    floor_value = math.isqrt(numerator // denominator)
    midpoint_times_four = 4 * floor_value * floor_value + 4 * floor_value + 1
    if 4 * numerator >= denominator * midpoint_times_four:
        return floor_value + 1
    return floor_value


def _scale_to_target(value: int, target: int) -> int:
    if value <= 0:
        return 0
    return min(MICROS, _round_ratio(value * MICROS, target))


def _clamp_micros(value: int) -> int:
    return min(MICROS, max(0, value))


def _weighted_component(component: int, weight: int) -> int:
    return _round_ratio(component * weight, MICROS)


def _validate_identifier(identifier: object) -> None:
    if (
        not isinstance(identifier, str)
        or not identifier
        or len(identifier) > _MAX_IDENTIFIER_LENGTH
        or identifier != identifier.strip()
        or any(ord(character) < 32 for character in identifier)
    ):
        raise QualityBatchError("quality identifier is invalid")
