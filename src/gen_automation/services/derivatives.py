import hashlib
import re
import warnings
from collections.abc import Buffer, Sequence
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from io import BytesIO
from typing import cast

import PIL
from PIL import (
    Image,
    ImageChops,
    ImageFilter,
    ImageOps,
    UnidentifiedImageError,
)

from gen_automation.domain.canonical import canonical_json_bytes, canonical_sha256
from gen_automation.domain.deliverability import (
    MAX_PIPELINE_MASTER_HEIGHT,
    MAX_PIPELINE_MASTER_PIXELS,
    MAX_PIPELINE_MASTER_WIDTH,
    PATREON_MAX_IMAGE_BYTES,
    X_STATIC_IMAGE_MAX_BYTES,
)

DERIVATIVE_RENDERER_VERSION = "pillow-derivative-v6"
PILLOW_VERSION = PIL.__version__
LEGACY_DERIVATIVE_RENDERER_VERSION = "pillow-derivative-v4"
PREVIOUS_DERIVATIVE_RENDERER_VERSION = "pillow-derivative-v5"
SUPPORTED_DERIVATIVE_RENDERER_VERSIONS = frozenset(
    {
        LEGACY_DERIVATIVE_RENDERER_VERSION,
        PREVIOUS_DERIVATIVE_RENDERER_VERSION,
        DERIVATIVE_RENDERER_VERSION,
    }
)
RELATIVE_SCALE = 1_000_000
SUPPORTED_MASTER_FORMATS = frozenset({"JPEG", "PNG", "WEBP"})
_ABSOLUTE_RECIPE_DIMENSION_LIMIT = 16_384
_PIXEL_BUFFER_BYTES = 4
_FIXED_WORKING_SET_BYTES = 64 * 1024 * 1024
_MAX_WATERMARK_PIXELS = 4_000_000
_JPEG_MIN_QUALITY = 70
_JPEG_DOWNSCALE_NUMERATOR = 3
_JPEG_DOWNSCALE_DENOMINATOR = 4
_FULL_JPEG_MAX_DOWNSCALE_PASSES = 16
_X_JPEG_MAX_DOWNSCALE_PASSES = 6
_VERSION_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_FILENAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_WINDOWS_RESERVED_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{number}" for number in range(1, 10)),
        *(f"LPT{number}" for number in range(1, 10)),
    }
)

type ImageBytes = bytes | bytearray | memoryview


class DerivativeError(Exception):
    """Base error for deterministic derivative rendering."""


class DerivativeRecipeError(DerivativeError, ValueError):
    """The immutable rendering recipe is invalid or unsafe."""


class DerivativeInputError(DerivativeError, ValueError):
    """Image input failed a bounded parser or pixel-safety check."""


class DerivativeRenderError(DerivativeError):
    """A valid recipe and input could not produce a safe derivative."""


class XStaticImagePngTooLargeError(DerivativeRenderError):
    """A lossless, full-dimension X PNG cannot fit the provider byte cap."""


class _OutputLimitExceededError(Exception):
    pass


class _BoundedOutput(BytesIO):
    def __init__(self, maximum_bytes: int) -> None:
        super().__init__()
        self._maximum_bytes = maximum_bytes
        self._high_watermark = 0

    def write(self, buffer: Buffer, /) -> int:
        candidate_high_watermark = max(
            self._high_watermark,
            self.tell() + memoryview(buffer).nbytes,
        )
        if candidate_high_watermark > self._maximum_bytes:
            raise _OutputLimitExceededError
        written = super().write(buffer)
        self._high_watermark = candidate_high_watermark
        return written


class OutputFormat(StrEnum):
    JPEG = "JPEG"
    PNG = "PNG"


class DerivativeTarget(StrEnum):
    FULL_RESOLUTION = "full"
    X_TEASER = "x_teaser"


class TeaserFitMode(StrEnum):
    DOWNSCALE = "downscale"
    CONTAIN = "contain"
    COVER = "cover"


class WatermarkPosition(StrEnum):
    TOP_LEFT = "top_left"
    TOP_RIGHT = "top_right"
    BOTTOM_LEFT = "bottom_left"
    BOTTOM_RIGHT = "bottom_right"


class CensorMode(StrEnum):
    MOSAIC = "mosaic"
    BLUR = "blur"


@dataclass(frozen=True, slots=True)
class JpegEncoding:
    quality: int = 94
    image_format: OutputFormat = field(default=OutputFormat.JPEG, init=False)

    def __post_init__(self) -> None:
        _strict_int(self.quality, "JPEG quality", minimum=70, maximum=100)


@dataclass(frozen=True, slots=True)
class PngEncoding:
    compress_level: int = 9
    image_format: OutputFormat = field(default=OutputFormat.PNG, init=False)

    def __post_init__(self) -> None:
        _strict_int(self.compress_level, "PNG compression level", minimum=0, maximum=9)


type Encoding = JpegEncoding | PngEncoding


@dataclass(frozen=True, slots=True)
class RelativeRegion:
    """A normalized rectangle expressed in integer millionths."""

    x: int
    y: int
    width: int
    height: int

    def __post_init__(self) -> None:
        _strict_int(self.x, "region x", minimum=0, maximum=RELATIVE_SCALE - 1)
        _strict_int(self.y, "region y", minimum=0, maximum=RELATIVE_SCALE - 1)
        _strict_int(self.width, "region width", minimum=1, maximum=RELATIVE_SCALE)
        _strict_int(self.height, "region height", minimum=1, maximum=RELATIVE_SCALE)
        if self.x + self.width > RELATIVE_SCALE or self.y + self.height > RELATIVE_SCALE:
            raise DerivativeRecipeError("censorship region exceeds the normalized canvas")


@dataclass(frozen=True, slots=True)
class MosaicCensor:
    region: RelativeRegion
    block_size: int = 20
    mode: CensorMode = field(default=CensorMode.MOSAIC, init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.region, RelativeRegion):
            raise DerivativeRecipeError("mosaic region is invalid")
        _strict_int(self.block_size, "mosaic block size", minimum=2, maximum=256)


@dataclass(frozen=True, slots=True)
class BlurCensor:
    region: RelativeRegion
    radius: int = 18
    mode: CensorMode = field(default=CensorMode.BLUR, init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.region, RelativeRegion):
            raise DerivativeRecipeError("blur region is invalid")
        _strict_int(self.radius, "blur radius", minimum=1, maximum=64)


type Censor = MosaicCensor | BlurCensor


@dataclass(frozen=True, slots=True)
class WatermarkSpec:
    width: int = 264_000
    margin: int = 12_000
    opacity: int = 255
    position: WatermarkPosition = WatermarkPosition.BOTTOM_RIGHT

    def __post_init__(self) -> None:
        _strict_int(
            self.width,
            "watermark relative width",
            minimum=10_000,
            maximum=500_000,
        )
        _strict_int(
            self.margin,
            "watermark relative margin",
            minimum=0,
            maximum=100_000,
        )
        _strict_int(self.opacity, "watermark opacity", minimum=1, maximum=255)
        if not isinstance(self.position, WatermarkPosition):
            raise DerivativeRecipeError("watermark position is invalid")


@dataclass(frozen=True, slots=True)
class FullDerivativeSpec:
    output_filename: str = "member-full.jpg"
    max_width: int = 4096
    max_height: int = 4096
    encoding: Encoding = field(default_factory=JpegEncoding)

    def __post_init__(self) -> None:
        _validate_target_dimensions(self.max_width, self.max_height)
        _validate_encoding_and_filename(self.encoding, self.output_filename)


@dataclass(frozen=True, slots=True)
class XTeaserSpec:
    output_filename: str = "x-teaser.jpg"
    width: int = 1600
    height: int = 1600
    fit_mode: TeaserFitMode = TeaserFitMode.DOWNSCALE
    allow_upscale: bool = False
    encoding: Encoding = field(default_factory=lambda: JpegEncoding(quality=91))
    censor: Censor | None = None

    def __post_init__(self) -> None:
        _validate_target_dimensions(self.width, self.height)
        if not isinstance(self.fit_mode, TeaserFitMode):
            raise DerivativeRecipeError("teaser fit mode is invalid")
        if not isinstance(self.allow_upscale, bool):
            raise DerivativeRecipeError("teaser allow_upscale must be boolean")
        if self.fit_mode is TeaserFitMode.DOWNSCALE and self.allow_upscale:
            raise DerivativeRecipeError("downscale mode cannot enable upscaling")
        _validate_encoding_and_filename(self.encoding, self.output_filename)
        if self.censor is not None and not isinstance(
            self.censor,
            (MosaicCensor, BlurCensor),
        ):
            raise DerivativeRecipeError("teaser censorship configuration is invalid")


@dataclass(frozen=True, slots=True)
class DerivativeRecipe:
    version: str = "derivative-v1"
    background_rgb: tuple[int, int, int] = (255, 255, 255)
    full: FullDerivativeSpec = field(default_factory=FullDerivativeSpec)
    x_teaser: XTeaserSpec = field(default_factory=XTeaserSpec)
    watermark: WatermarkSpec | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.version, str) or _VERSION_PATTERN.fullmatch(self.version) is None:
            raise DerivativeRecipeError("recipe version is invalid")
        _validate_rgb(self.background_rgb)
        if not isinstance(self.full, FullDerivativeSpec):
            raise DerivativeRecipeError("full derivative specification is invalid")
        if not isinstance(self.x_teaser, XTeaserSpec):
            raise DerivativeRecipeError("X teaser specification is invalid")
        if self.watermark is not None and not isinstance(self.watermark, WatermarkSpec):
            raise DerivativeRecipeError("watermark specification is invalid")
        if self.full.output_filename.casefold() == self.x_teaser.output_filename.casefold():
            raise DerivativeRecipeError("derivative output filenames must be unique")


@dataclass(frozen=True, slots=True)
class DerivativeSafetyLimits:
    max_master_bytes: int = 32 * 1024 * 1024
    max_watermark_bytes: int = 4 * 1024 * 1024
    max_input_width: int = MAX_PIPELINE_MASTER_WIDTH
    max_input_height: int = MAX_PIPELINE_MASTER_HEIGHT
    max_input_pixels: int = MAX_PIPELINE_MASTER_PIXELS
    max_input_aspect_ratio: int = 20
    max_output_width: int = MAX_PIPELINE_MASTER_WIDTH
    max_output_height: int = MAX_PIPELINE_MASTER_HEIGHT
    max_output_pixels: int = MAX_PIPELINE_MASTER_PIXELS
    max_output_bytes: int = 16 * 1024 * 1024
    max_full_output_bytes: int = PATREON_MAX_IMAGE_BYTES
    max_x_teaser_bytes: int = X_STATIC_IMAGE_MAX_BYTES
    max_recipe_bytes: int = 16 * 1024
    max_peak_working_set_bytes: int = 576 * 1024 * 1024

    def __post_init__(self) -> None:
        _strict_int(
            self.max_master_bytes,
            "maximum master bytes",
            minimum=1024,
            maximum=256 * 1024 * 1024,
        )
        _strict_int(
            self.max_watermark_bytes,
            "maximum watermark bytes",
            minimum=128,
            maximum=32 * 1024 * 1024,
        )
        _strict_int(
            self.max_input_width,
            "maximum input width",
            minimum=1,
            maximum=32_768,
        )
        _strict_int(
            self.max_input_height,
            "maximum input height",
            minimum=1,
            maximum=32_768,
        )
        _strict_int(
            self.max_input_pixels,
            "maximum input pixels",
            minimum=1,
            maximum=128_000_000,
        )
        _strict_int(
            self.max_input_aspect_ratio,
            "maximum input aspect ratio",
            minimum=1,
            maximum=100,
        )
        _strict_int(
            self.max_output_width,
            "maximum output width",
            minimum=1,
            maximum=16_384,
        )
        _strict_int(
            self.max_output_height,
            "maximum output height",
            minimum=1,
            maximum=16_384,
        )
        _strict_int(
            self.max_output_pixels,
            "maximum output pixels",
            minimum=1,
            maximum=64_000_000,
        )
        _strict_int(
            self.max_output_bytes,
            "maximum output bytes",
            minimum=1024,
            maximum=128 * 1024 * 1024,
        )
        _strict_int(
            self.max_full_output_bytes,
            "maximum full-output bytes",
            minimum=1024,
            maximum=64 * 1024 * 1024,
        )
        _strict_int(
            self.max_x_teaser_bytes,
            "maximum X teaser bytes",
            minimum=1024,
            maximum=X_STATIC_IMAGE_MAX_BYTES,
        )
        _strict_int(
            self.max_recipe_bytes,
            "maximum recipe bytes",
            minimum=256,
            maximum=64 * 1024,
        )
        _strict_int(
            self.max_peak_working_set_bytes,
            "maximum estimated peak working-set bytes",
            minimum=64 * 1024 * 1024,
            maximum=2 * 1024 * 1024 * 1024,
        )

    def output_byte_limit(self, target: DerivativeTarget) -> int:
        if target is DerivativeTarget.FULL_RESOLUTION:
            return min(self.max_output_bytes, self.max_full_output_bytes)
        if target is DerivativeTarget.X_TEASER:
            return min(self.max_output_bytes, self.max_x_teaser_bytes)
        raise DerivativeRecipeError("derivative target is invalid")


@dataclass(frozen=True, slots=True)
class DerivativeLineage:
    target: DerivativeTarget
    source_sha256: str
    source_byte_size: int
    source_format: str
    source_width: int
    source_height: int
    normalized_width: int
    normalized_height: int
    recipe_version: str
    recipe_sha256: str
    watermark_sha256: str | None
    renderer_version: str
    pillow_version: str
    operations: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RenderedDerivative:
    target: DerivativeTarget
    output_filename: str
    data: bytes
    sha256: str
    byte_size: int
    image_format: OutputFormat
    content_type: str
    extension: str
    width: int
    height: int
    recipe_sha256: str
    lineage_sha256: str
    lineage: DerivativeLineage


@dataclass(frozen=True, slots=True)
class DerivativeBundle:
    source_sha256: str
    recipe_sha256: str
    artifacts: tuple[RenderedDerivative, ...]


@dataclass(frozen=True, slots=True)
class VerifiedWatermark:
    sha256: str
    byte_size: int
    width: int
    height: int
    image_format: str = "PNG"
    content_type: str = "image/png"


@dataclass(frozen=True, slots=True)
class FullResolutionWatermarkedImage:
    data: bytes
    sha256: str
    byte_size: int
    image_format: str
    content_type: str
    extension: str
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class _SourceMetadata:
    sha256: str
    byte_size: int
    image_format: str
    source_width: int
    source_height: int
    normalized_width: int
    normalized_height: int
    normalization_operation: str


@dataclass(frozen=True, slots=True)
class _EncodedImage:
    data: bytes
    width: int
    height: int
    operations: tuple[str, ...] = ()


def derivative_recipe_sha256(recipe: DerivativeRecipe) -> str:
    if not isinstance(recipe, DerivativeRecipe):
        raise DerivativeRecipeError("derivative recipe is invalid")
    return canonical_sha256(asdict(recipe))


def verify_watermark_png(
    watermark_png: ImageBytes,
    *,
    limits: DerivativeSafetyLimits | None = None,
) -> VerifiedWatermark:
    """Validate the exact PNG contract used by the production renderer."""

    selected_limits = limits or DEFAULT_DERIVATIVE_LIMITS
    if not isinstance(selected_limits, DerivativeSafetyLimits):
        raise DerivativeRecipeError("derivative safety limits are invalid")
    payload = _bounded_bytes(
        watermark_png,
        maximum=selected_limits.max_watermark_bytes,
        label="watermark",
    )
    watermark = _decode_watermark(payload, selected_limits)
    try:
        return VerifiedWatermark(
            sha256=hashlib.sha256(payload).hexdigest(),
            byte_size=len(payload),
            width=watermark.width,
            height=watermark.height,
        )
    finally:
        watermark.close()


def render_full_resolution_watermark(
    raw_master: ImageBytes,
    watermark_png: ImageBytes,
    position: WatermarkPosition | str,
    *,
    limits: DerivativeSafetyLimits | None = None,
) -> FullResolutionWatermarkedImage:
    """Apply the X watermark geometry without resizing the source image.

    Production callers must invoke this synchronous renderer through a one-shot
    process boundary because both image inputs are untrusted.
    """

    selected_limits = limits or BATCH_WATERMARK_DERIVATIVE_LIMITS
    if not isinstance(selected_limits, DerivativeSafetyLimits):
        raise DerivativeRecipeError("derivative safety limits are invalid")
    try:
        normalized_position = WatermarkPosition(position)
    except ValueError:
        raise DerivativeRecipeError("watermark position is invalid") from None
    master_payload = _bounded_bytes(
        raw_master,
        maximum=selected_limits.max_master_bytes,
        label="raw master",
    )
    watermark_payload = _bounded_bytes(
        watermark_png,
        maximum=selected_limits.max_watermark_bytes,
        label="watermark",
    )
    recipe = DerivativeRecipe(
        version="batch-watermark-v1",
        full=FullDerivativeSpec(
            output_filename="watermarked.jpg",
            max_width=selected_limits.max_output_width,
            max_height=selected_limits.max_output_height,
        ),
        watermark=WatermarkSpec(position=normalized_position),
    )
    _validate_recipe_against_limits(recipe, selected_limits)
    normalized, source = _decode_master(
        master_payload,
        recipe,
        selected_limits,
        watermark_byte_size=len(watermark_payload),
    )
    watermark = _decode_watermark(watermark_payload, selected_limits)
    try:
        rendered = _apply_watermark(
            normalized,
            watermark=watermark,
            spec=recipe.watermark,
            renderer_version=DERIVATIVE_RENDERER_VERSION,
        )
    finally:
        normalized.close()
        watermark.close()
    try:
        _assert_output_geometry(rendered.size, selected_limits)
        encoded, output_format, content_type, extension = _encode_batch_watermark(
            rendered,
            source_format=source.image_format,
            maximum_bytes=selected_limits.max_output_bytes,
        )
        return FullResolutionWatermarkedImage(
            data=encoded,
            sha256=hashlib.sha256(encoded).hexdigest(),
            byte_size=len(encoded),
            image_format=output_format,
            content_type=content_type,
            extension=extension,
            width=rendered.width,
            height=rendered.height,
        )
    finally:
        rendered.close()


def estimate_derivative_peak_working_set_bytes(
    *,
    source_width: int,
    source_height: int,
    source_mode: str,
    source_has_alpha: bool,
    master_byte_size: int,
    watermark_byte_size: int,
    recipe: DerivativeRecipe,
    limits: DerivativeSafetyLimits,
) -> int:
    """Conservatively estimate the renderer's maximum simultaneous live bytes.

    This is a pre-decode admission estimate, not an operating-system memory
    measurement. It deliberately treats every Pillow pixel buffer as four bytes
    per pixel, includes encoded inputs and outputs, and accounts for the
    renderer's largest normalization/compositing phase.
    """

    if (
        isinstance(source_width, bool)
        or not isinstance(source_width, int)
        or source_width <= 0
        or isinstance(source_height, bool)
        or not isinstance(source_height, int)
        or source_height <= 0
        or not isinstance(source_mode, str)
        or not source_mode
        or not isinstance(source_has_alpha, bool)
        or isinstance(master_byte_size, bool)
        or not isinstance(master_byte_size, int)
        or master_byte_size <= 0
        or isinstance(watermark_byte_size, bool)
        or not isinstance(watermark_byte_size, int)
        or watermark_byte_size < 0
    ):
        raise DerivativeInputError("peak working-set estimate inputs are invalid")
    if not isinstance(recipe, DerivativeRecipe):
        raise DerivativeRecipeError("derivative recipe is invalid")
    if not isinstance(limits, DerivativeSafetyLimits):
        raise DerivativeRecipeError("derivative safety limits are invalid")

    source_pixels = source_width * source_height
    full_pixels = min(
        source_pixels,
        recipe.full.max_width * recipe.full.max_height,
    )
    if recipe.x_teaser.fit_mode is TeaserFitMode.DOWNSCALE:
        teaser_pixels = min(
            source_pixels,
            recipe.x_teaser.width * recipe.x_teaser.height,
        )
    else:
        teaser_pixels = recipe.x_teaser.width * recipe.x_teaser.height

    payload_bytes = master_byte_size + watermark_byte_size
    fixed_bytes = _FIXED_WORKING_SET_BYTES + payload_bytes
    source_buffer_bytes = source_pixels * _PIXEL_BUFFER_BYTES
    normalization_buffers = 6 if source_has_alpha else (2 if source_mode == "RGB" else 3)
    normalization_peak = fixed_bytes + normalization_buffers * source_buffer_bytes

    watermark_pixels = (
        min(limits.max_input_pixels, _MAX_WATERMARK_PIXELS) if watermark_byte_size else 0
    )
    watermark_buffer_bytes = watermark_pixels * _PIXEL_BUFFER_BYTES
    watermark_decode_peak = fixed_bytes + source_buffer_bytes + 3 * watermark_buffer_bytes

    full_buffer_bytes = full_pixels * _PIXEL_BUFFER_BYTES
    # Watermarking can simultaneously retain the input, resized watermark and
    # alpha planes, RGBA base/composite, RGB output, and difference image.
    full_transform_buffers = 7 if watermark_byte_size else 2
    full_peak = (
        fixed_bytes
        + source_buffer_bytes
        + full_transform_buffers * full_buffer_bytes
        + watermark_buffer_bytes
        + limits.max_output_bytes
    )

    teaser_buffer_bytes = teaser_pixels * _PIXEL_BUFFER_BYTES
    teaser_transform_buffers = 2
    if recipe.x_teaser.censor is not None:
        teaser_transform_buffers = max(teaser_transform_buffers, 4)
    if watermark_byte_size:
        teaser_transform_buffers = max(teaser_transform_buffers, 7)
    teaser_peak = (
        fixed_bytes
        + source_buffer_bytes
        + teaser_transform_buffers * teaser_buffer_bytes
        + watermark_buffer_bytes
        + 2 * limits.max_output_bytes
    )
    return max(
        normalization_peak,
        watermark_decode_peak,
        full_peak,
        teaser_peak,
    )


def render_platform_derivatives(
    raw_master: ImageBytes,
    *,
    recipe: DerivativeRecipe,
    watermark_png: ImageBytes | None = None,
    targets: Sequence[DerivativeTarget | str] | None = None,
    limits: DerivativeSafetyLimits | None = None,
    renderer_version: str = DERIVATIVE_RENDERER_VERSION,
) -> DerivativeBundle:
    """Render without mutating inputs.

    This synchronous entry point is for deterministic unit tests and trusted
    callers only. Production jobs with untrusted image bytes must use the
    one-shot boundary in ``derivative_isolation``.
    """

    if not isinstance(recipe, DerivativeRecipe):
        raise DerivativeRecipeError("derivative recipe is invalid")
    if limits is None:
        limits = DEFAULT_DERIVATIVE_LIMITS
    if not isinstance(limits, DerivativeSafetyLimits):
        raise DerivativeRecipeError("derivative safety limits are invalid")
    if renderer_version not in SUPPORTED_DERIVATIVE_RENDERER_VERSIONS:
        raise DerivativeRecipeError("derivative renderer version is unsupported")
    recipe_payload = canonical_json_bytes(asdict(recipe))
    if len(recipe_payload) > limits.max_recipe_bytes:
        raise DerivativeRecipeError("serialized derivative recipe exceeds the safety limit")
    recipe_sha256 = hashlib.sha256(recipe_payload).hexdigest()
    _validate_recipe_against_limits(recipe, limits)
    selected_targets = _normalize_render_targets(targets)

    master_payload = _bounded_bytes(
        raw_master,
        maximum=limits.max_master_bytes,
        label="raw master",
    )
    teaser_requested = DerivativeTarget.X_TEASER in selected_targets
    if watermark_png is not None and (recipe.watermark is None or not teaser_requested):
        raise DerivativeRecipeError("watermark bytes are accepted only for a watermarked X teaser")
    if teaser_requested and recipe.watermark is not None and watermark_png is None:
        raise DerivativeRecipeError("a watermarked X teaser requires watermark bytes")
    watermark_payload = (
        _bounded_bytes(
            watermark_png,
            maximum=limits.max_watermark_bytes,
            label="watermark",
        )
        if watermark_png is not None
        else None
    )

    normalized, source = _decode_master(
        master_payload,
        recipe,
        limits,
        watermark_byte_size=len(watermark_payload) if watermark_payload is not None else 0,
    )
    watermark: Image.Image | None = None
    watermark_sha256: str | None = None
    try:
        if watermark_payload is not None:
            watermark = _decode_watermark(watermark_payload, limits)
            watermark_sha256 = hashlib.sha256(watermark_payload).hexdigest()
        artifacts: list[RenderedDerivative] = []
        if DerivativeTarget.FULL_RESOLUTION in selected_targets:
            artifacts.append(
                _render_full(
                    normalized,
                    source=source,
                    recipe=recipe,
                    recipe_sha256=recipe_sha256,
                    limits=limits,
                    renderer_version=renderer_version,
                )
            )
        if DerivativeTarget.X_TEASER in selected_targets:
            artifacts.append(
                _render_teaser(
                    normalized,
                    source=source,
                    recipe=recipe,
                    recipe_sha256=recipe_sha256,
                    watermark=watermark,
                    watermark_sha256=watermark_sha256,
                    limits=limits,
                    renderer_version=renderer_version,
                )
            )
    finally:
        normalized.close()
        if watermark is not None:
            watermark.close()

    return DerivativeBundle(
        source_sha256=source.sha256,
        recipe_sha256=recipe_sha256,
        artifacts=tuple(artifacts),
    )


def _normalize_render_targets(
    values: Sequence[DerivativeTarget | str] | None,
) -> tuple[DerivativeTarget, ...]:
    if values is None:
        return (DerivativeTarget.FULL_RESOLUTION, DerivativeTarget.X_TEASER)
    if isinstance(values, (str, bytes)) or not values:
        raise DerivativeRecipeError("render targets must be a non-empty sequence")
    try:
        requested = {DerivativeTarget(value) for value in values}
    except ValueError:
        raise DerivativeRecipeError("render target is invalid") from None
    if len(requested) != len(values):
        raise DerivativeRecipeError("render targets must be unique")
    return tuple(target for target in DerivativeTarget if target in requested)


def _decode_master(
    payload: bytes,
    recipe: DerivativeRecipe,
    limits: DerivativeSafetyLimits,
    *,
    watermark_byte_size: int,
) -> tuple[Image.Image, _SourceMetadata]:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(payload)) as image:
                header = _validate_master_header(image, limits)
                image.verify()
            with Image.open(BytesIO(payload)) as image:
                loaded_header = _validate_master_header(image, limits)
                if loaded_header != header:
                    raise DerivativeInputError("master metadata changed during decode")
                source_has_alpha = _image_has_alpha(image)
                estimated_peak = estimate_derivative_peak_working_set_bytes(
                    source_width=image.width,
                    source_height=image.height,
                    source_mode=image.mode,
                    source_has_alpha=source_has_alpha,
                    master_byte_size=len(payload),
                    watermark_byte_size=watermark_byte_size,
                    recipe=recipe,
                    limits=limits,
                )
                if estimated_peak > limits.max_peak_working_set_bytes:
                    raise DerivativeInputError(
                        "estimated renderer peak working set exceeds the safety limit"
                    )
                image.load()
                transposed = ImageOps.exif_transpose(image)
                normalized, normalization_operation = _normalize_master_pixels(
                    image,
                    transposed=transposed,
                    background_rgb=recipe.background_rgb,
                    source_has_alpha=source_has_alpha,
                )
                normalized.info.clear()
    except DerivativeInputError:
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
        raise DerivativeInputError("raw master is malformed or unsafe") from None
    except Exception:
        raise DerivativeInputError("raw master is malformed or unsafe") from None

    image_format, source_width, source_height = header
    return normalized, _SourceMetadata(
        sha256=hashlib.sha256(payload).hexdigest(),
        byte_size=len(payload),
        image_format=image_format,
        source_width=source_width,
        source_height=source_height,
        normalized_width=normalized.width,
        normalized_height=normalized.height,
        normalization_operation=normalization_operation,
    )


def _image_has_alpha(image: Image.Image) -> bool:
    return "A" in image.getbands() or "transparency" in image.info


def _normalize_master_pixels(
    source_image: Image.Image,
    *,
    transposed: Image.Image,
    background_rgb: tuple[int, int, int],
    source_has_alpha: bool,
) -> tuple[Image.Image, str]:
    if not source_has_alpha:
        if transposed.mode == "RGB" and transposed is not source_image:
            return transposed, "normalize_opaque_rgb"
        try:
            normalized = (
                transposed.copy() if transposed.mode == "RGB" else transposed.convert("RGB")
            )
        finally:
            if transposed is not source_image:
                transposed.close()
        return normalized, "normalize_opaque_rgb"

    try:
        rgba = transposed.convert("RGBA")
    finally:
        if transposed is not source_image:
            transposed.close()
    try:
        background = Image.new("RGBA", rgba.size, (*background_rgb, 255))
        try:
            flattened = Image.alpha_composite(background, rgba)
        finally:
            background.close()
    finally:
        rgba.close()
    try:
        normalized = flattened.convert("RGB")
    finally:
        flattened.close()
    return normalized, "flatten_alpha_to_rgb"


def _decode_watermark(
    payload: bytes,
    limits: DerivativeSafetyLimits,
) -> Image.Image:
    watermark_pixel_limit = min(limits.max_input_pixels, _MAX_WATERMARK_PIXELS)
    watermark_dimension_limit = min(
        limits.max_input_width,
        limits.max_input_height,
        4096,
    )
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(payload)) as image:
                header = _validate_watermark_header(
                    image,
                    pixel_limit=watermark_pixel_limit,
                    dimension_limit=watermark_dimension_limit,
                )
                image.verify()
            with Image.open(BytesIO(payload)) as image:
                loaded_header = _validate_watermark_header(
                    image,
                    pixel_limit=watermark_pixel_limit,
                    dimension_limit=watermark_dimension_limit,
                )
                if loaded_header != header:
                    raise DerivativeInputError("watermark metadata changed during decode")
                image.load()
                watermark = image.convert("RGBA")
                watermark.info.clear()
                alpha = watermark.getchannel("A")
                try:
                    minimum_alpha, maximum_alpha = cast(tuple[int, int], alpha.getextrema())
                finally:
                    alpha.close()
                if minimum_alpha >= 255 or maximum_alpha <= 0:
                    watermark.close()
                    raise DerivativeInputError(
                        "watermark must contain both transparent and visible pixels"
                    )
    except DerivativeInputError:
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
        raise DerivativeInputError("watermark is malformed or unsafe") from None
    except Exception:
        raise DerivativeInputError("watermark is malformed or unsafe") from None
    return watermark


def _validate_master_header(
    image: Image.Image,
    limits: DerivativeSafetyLimits,
) -> tuple[str, int, int]:
    image_format = (image.format or "").upper()
    if image_format not in SUPPORTED_MASTER_FORMATS:
        raise DerivativeInputError("raw master format is unsupported")
    width, height = image.size
    _validate_input_geometry(width, height, limits)
    if bool(getattr(image, "is_animated", False)) or int(getattr(image, "n_frames", 1)) != 1:
        raise DerivativeInputError("multi-frame raw masters are unsupported")
    return image_format, width, height


def _validate_watermark_header(
    image: Image.Image,
    *,
    pixel_limit: int,
    dimension_limit: int,
) -> tuple[str, int, int]:
    image_format = (image.format or "").upper()
    if image_format != "PNG":
        raise DerivativeInputError("watermark must be a PNG image")
    width, height = image.size
    if (
        width <= 0
        or height <= 0
        or width > dimension_limit
        or height > dimension_limit
        or width * height > pixel_limit
    ):
        raise DerivativeInputError("watermark dimensions exceed the safety limit")
    if max(width, height) > min(width, height) * 50:
        raise DerivativeInputError("watermark aspect ratio exceeds the safety limit")
    if bool(getattr(image, "is_animated", False)) or int(getattr(image, "n_frames", 1)) != 1:
        raise DerivativeInputError("multi-frame watermarks are unsupported")
    if "A" not in image.getbands() and "transparency" not in image.info:
        raise DerivativeInputError("watermark PNG does not contain an alpha channel")
    return image_format, width, height


def _render_full(
    source_image: Image.Image,
    *,
    source: _SourceMetadata,
    recipe: DerivativeRecipe,
    recipe_sha256: str,
    limits: DerivativeSafetyLimits,
    renderer_version: str,
) -> RenderedDerivative:
    resized = _resize_to_box(
        source_image,
        maximum_width=recipe.full.max_width,
        maximum_height=recipe.full.max_height,
        allow_upscale=False,
    )
    operations = [
        "exif_transpose",
        source.normalization_operation,
        f"downscale:{resized.width}x{resized.height}",
    ]
    try:
        operations.append("watermark:none")
        return _build_artifact(
            resized,
            target=DerivativeTarget.FULL_RESOLUTION,
            output_filename=recipe.full.output_filename,
            encoding=recipe.full.encoding,
            source=source,
            recipe=recipe,
            recipe_sha256=recipe_sha256,
            watermark_sha256=None,
            operations=(*operations, f"encode:{recipe.full.encoding.image_format.value}"),
            limits=limits,
            renderer_version=renderer_version,
        )
    finally:
        resized.close()


def _render_teaser(
    source_image: Image.Image,
    *,
    source: _SourceMetadata,
    recipe: DerivativeRecipe,
    recipe_sha256: str,
    watermark: Image.Image | None,
    watermark_sha256: str | None,
    limits: DerivativeSafetyLimits,
    renderer_version: str,
) -> RenderedDerivative:
    fitted = _fit_teaser(source_image, recipe.x_teaser, recipe.background_rgb)
    operations = [
        "exif_transpose",
        source.normalization_operation,
        f"fit:{recipe.x_teaser.fit_mode.value}:{fitted.width}x{fitted.height}",
    ]
    try:
        censored = _apply_censorship(fitted, recipe.x_teaser.censor)
    finally:
        fitted.close()
    try:
        censor = recipe.x_teaser.censor
        operations.append(f"censor:{censor.mode.value}" if censor is not None else "censor:none")
        rendered = _apply_watermark(
            censored,
            watermark=watermark,
            spec=recipe.watermark,
            renderer_version=renderer_version,
        )
    finally:
        censored.close()
    try:
        operations.append(
            f"watermark:{watermark_sha256}" if watermark_sha256 is not None else "watermark:none"
        )
        return _build_artifact(
            rendered,
            target=DerivativeTarget.X_TEASER,
            output_filename=recipe.x_teaser.output_filename,
            encoding=recipe.x_teaser.encoding,
            source=source,
            recipe=recipe,
            recipe_sha256=recipe_sha256,
            watermark_sha256=watermark_sha256,
            operations=(*operations, f"encode:{recipe.x_teaser.encoding.image_format.value}"),
            limits=limits,
            renderer_version=renderer_version,
        )
    finally:
        rendered.close()


def _resize_to_box(
    image: Image.Image,
    *,
    maximum_width: int,
    maximum_height: int,
    allow_upscale: bool,
) -> Image.Image:
    width, height = image.size
    if not allow_upscale and width <= maximum_width and height <= maximum_height:
        return image.copy()
    if maximum_width * height <= maximum_height * width:
        resized_width = maximum_width
        resized_height = max(1, height * maximum_width // width)
    else:
        resized_height = maximum_height
        resized_width = max(1, width * maximum_height // height)
    if not allow_upscale and resized_width >= width and resized_height >= height:
        return image.copy()
    return image.resize(
        (resized_width, resized_height),
        resample=Image.Resampling.LANCZOS,
    )


def _fit_teaser(
    image: Image.Image,
    spec: XTeaserSpec,
    background_rgb: tuple[int, int, int],
) -> Image.Image:
    if spec.fit_mode is TeaserFitMode.DOWNSCALE:
        return _resize_to_box(
            image,
            maximum_width=spec.width,
            maximum_height=spec.height,
            allow_upscale=False,
        )
    if spec.fit_mode is TeaserFitMode.CONTAIN:
        contained = _resize_to_box(
            image,
            maximum_width=spec.width,
            maximum_height=spec.height,
            allow_upscale=spec.allow_upscale,
        )
        try:
            canvas = Image.new("RGB", (spec.width, spec.height), background_rgb)
            canvas.paste(
                contained,
                (
                    (spec.width - contained.width) // 2,
                    (spec.height - contained.height) // 2,
                ),
            )
            return canvas
        finally:
            contained.close()

    source_width, source_height = image.size
    if source_width * spec.height > source_height * spec.width:
        crop_height = source_height
        crop_width = max(1, source_height * spec.width // spec.height)
    else:
        crop_width = source_width
        crop_height = max(1, source_width * spec.height // spec.width)
    if not spec.allow_upscale and (crop_width < spec.width or crop_height < spec.height):
        raise DerivativeRenderError("cover fit would require prohibited upscaling")
    left = (source_width - crop_width) // 2
    top = (source_height - crop_height) // 2
    cropped = image.crop((left, top, left + crop_width, top + crop_height))
    try:
        if cropped.size == (spec.width, spec.height):
            return cropped.copy()
        return cropped.resize(
            (spec.width, spec.height),
            resample=Image.Resampling.LANCZOS,
        )
    finally:
        cropped.close()


def _apply_censorship(image: Image.Image, censor: Censor | None) -> Image.Image:
    if censor is None:
        return image.copy()
    left, top, right, bottom = _region_pixels(censor.region, image.size)
    region = image.crop((left, top, right, bottom))
    try:
        if isinstance(censor, MosaicCensor):
            small_width = max(1, (region.width + censor.block_size - 1) // censor.block_size)
            small_height = max(1, (region.height + censor.block_size - 1) // censor.block_size)
            reduced = region.resize(
                (small_width, small_height),
                resample=Image.Resampling.BOX,
            )
            try:
                transformed = reduced.resize(
                    region.size,
                    resample=Image.Resampling.NEAREST,
                )
            finally:
                reduced.close()
        else:
            transformed = region.filter(ImageFilter.GaussianBlur(radius=censor.radius))
        try:
            output = image.copy()
            output.paste(transformed, (left, top))
            return output
        finally:
            transformed.close()
    finally:
        region.close()


def _apply_watermark(
    image: Image.Image,
    *,
    watermark: Image.Image | None,
    spec: WatermarkSpec | None,
    renderer_version: str,
) -> Image.Image:
    if watermark is None or spec is None:
        return image.copy()
    margin = min(image.size) * spec.margin // RELATIVE_SCALE
    available_width = image.width - 2 * margin
    available_height = image.height - 2 * margin
    if available_width <= 0 or available_height <= 0:
        raise DerivativeRenderError("watermark margin leaves no drawable canvas")
    prepared = (
        _trim_watermark_to_visible_alpha(watermark)
        if renderer_version != LEGACY_DERIVATIVE_RENDERER_VERSION
        else watermark.copy()
    )
    try:
        target_width = max(1, image.width * spec.width // RELATIVE_SCALE)
        target_width = min(target_width, available_width)
        target_height = max(1, prepared.height * target_width // prepared.width)
        if target_height > available_height:
            target_height = available_height
            target_width = max(1, prepared.width * target_height // prepared.height)
        resized = prepared.resize(
            (target_width, target_height),
            resample=Image.Resampling.LANCZOS,
        )
        try:
            alpha = resized.getchannel("A")
            try:
                alpha_table = tuple((value * spec.opacity + 127) // 255 for value in range(256))
                adjusted_alpha = alpha.point(alpha_table)
            finally:
                alpha.close()
            try:
                resized.putalpha(adjusted_alpha)
            finally:
                adjusted_alpha.close()

            left = (
                margin
                if spec.position in {WatermarkPosition.TOP_LEFT, WatermarkPosition.BOTTOM_LEFT}
                else image.width - margin - resized.width
            )
            top = (
                margin
                if spec.position in {WatermarkPosition.TOP_LEFT, WatermarkPosition.TOP_RIGHT}
                else image.height - margin - resized.height
            )
            base = image.convert("RGBA")
            try:
                composited = base.copy()
                composited.alpha_composite(resized, dest=(left, top))
                output = composited.convert("RGB")
                output.info.clear()
                try:
                    difference = ImageChops.difference(image, output)
                    try:
                        if difference.getbbox() is None:
                            raise DerivativeRenderError(
                                "watermark is not visible on the derivative"
                            )
                    finally:
                        difference.close()
                    return output
                except BaseException:
                    output.close()
                    raise
                finally:
                    composited.close()
            finally:
                base.close()
        finally:
            resized.close()
    finally:
        prepared.close()


def _trim_watermark_to_visible_alpha(watermark: Image.Image) -> Image.Image:
    alpha = watermark.getchannel("A")
    try:
        bounds = alpha.getbbox()
    finally:
        alpha.close()
    if bounds is None:
        raise DerivativeRenderError("watermark has no visible alpha bounds")
    return watermark.crop(bounds)


def _build_artifact(
    image: Image.Image,
    *,
    target: DerivativeTarget,
    output_filename: str,
    encoding: Encoding,
    source: _SourceMetadata,
    recipe: DerivativeRecipe,
    recipe_sha256: str,
    watermark_sha256: str | None,
    operations: tuple[str, ...],
    limits: DerivativeSafetyLimits,
    renderer_version: str,
) -> RenderedDerivative:
    _assert_output_geometry(image.size, limits)
    maximum_bytes = limits.output_byte_limit(target)
    if target is DerivativeTarget.FULL_RESOLUTION:
        encoded = _encode_full(
            image,
            encoding,
            maximum_bytes=maximum_bytes,
        )
    elif target is DerivativeTarget.X_TEASER:
        encoded = _encode_x_teaser(
            image,
            encoding,
            maximum_bytes=maximum_bytes,
            renderer_version=renderer_version,
        )
    else:
        raise DerivativeRecipeError("derivative target is invalid")
    lineage_operations = operations
    if encoded.operations:
        if operations and operations[-1].startswith("encode:"):
            lineage_operations = (*operations[:-1], *encoded.operations, operations[-1])
        else:
            lineage_operations = (*operations, *encoded.operations)
    lineage = DerivativeLineage(
        target=target,
        source_sha256=source.sha256,
        source_byte_size=source.byte_size,
        source_format=source.image_format,
        source_width=source.source_width,
        source_height=source.source_height,
        normalized_width=source.normalized_width,
        normalized_height=source.normalized_height,
        recipe_version=recipe.version,
        recipe_sha256=recipe_sha256,
        watermark_sha256=watermark_sha256,
        renderer_version=renderer_version,
        pillow_version=PILLOW_VERSION,
        operations=lineage_operations,
    )
    image_format = encoding.image_format
    content_type = "image/jpeg" if image_format is OutputFormat.JPEG else "image/png"
    extension = "jpg" if image_format is OutputFormat.JPEG else "png"
    return RenderedDerivative(
        target=target,
        output_filename=output_filename,
        data=encoded.data,
        sha256=hashlib.sha256(encoded.data).hexdigest(),
        byte_size=len(encoded.data),
        image_format=image_format,
        content_type=content_type,
        extension=extension,
        width=encoded.width,
        height=encoded.height,
        recipe_sha256=recipe_sha256,
        lineage_sha256=canonical_sha256(asdict(lineage)),
        lineage=lineage,
    )


def _encode_full(
    image: Image.Image,
    encoding: Encoding,
    *,
    maximum_bytes: int,
) -> _EncodedImage:
    budget_operation = f"full-budget-limit:{maximum_bytes}"
    if isinstance(encoding, JpegEncoding):
        encoded = _encode_adaptive_jpeg(
            image,
            encoding=encoding,
            maximum_bytes=maximum_bytes,
            max_downscale_passes=_FULL_JPEG_MAX_DOWNSCALE_PASSES,
            operation_prefix="full-budget",
            failure_message="encoded full JPEG exceeds its per-release byte budget",
        )
        return _EncodedImage(
            data=encoded.data,
            width=encoded.width,
            height=encoded.height,
            operations=(budget_operation, *encoded.operations),
        )
    payload = _try_encode_image(image, encoding, maximum_bytes=maximum_bytes)
    if payload is None:
        raise DerivativeRenderError(
            "encoded full PNG exceeds its per-release output byte limit; "
            "automatic lossy conversion is forbidden"
        )
    return _EncodedImage(
        data=payload,
        width=image.width,
        height=image.height,
        operations=(budget_operation,),
    )


def _encode_x_teaser(
    image: Image.Image,
    encoding: Encoding,
    *,
    maximum_bytes: int,
    renderer_version: str,
) -> _EncodedImage:
    if not isinstance(encoding, JpegEncoding):
        if renderer_version != DERIVATIVE_RENDERER_VERSION:
            return _EncodedImage(
                data=_encode_image(image, encoding, maximum_bytes=maximum_bytes),
                width=image.width,
                height=image.height,
            )
        payload = _try_encode_image(
            image,
            encoding,
            maximum_bytes=maximum_bytes,
        )
        operations: tuple[str, ...] = ()
        if payload is None and encoding.compress_level != 9:
            payload = _try_encode_image(
                image,
                PngEncoding(compress_level=9),
                maximum_bytes=maximum_bytes,
            )
            if payload is not None:
                operations = ("x-cap-png-compress-level:9",)
        if payload is None:
            raise XStaticImagePngTooLargeError(
                "full-resolution lossless X PNG exceeds the static image byte limit "
                f"({maximum_bytes} bytes); automatic JPEG conversion and downscaling "
                "are forbidden"
            )
        return _EncodedImage(
            data=payload,
            width=image.width,
            height=image.height,
            operations=operations,
        )

    return _encode_adaptive_jpeg(
        image,
        encoding=encoding,
        maximum_bytes=maximum_bytes,
        max_downscale_passes=_X_JPEG_MAX_DOWNSCALE_PASSES,
        operation_prefix="x-cap",
        failure_message="encoded X teaser exceeds the static image byte limit",
    )


def _encode_adaptive_jpeg(
    image: Image.Image,
    *,
    encoding: JpegEncoding,
    maximum_bytes: int,
    max_downscale_passes: int,
    operation_prefix: str,
    failure_message: str,
) -> _EncodedImage:
    candidate = image.copy()
    try:
        for downscale_pass in range(max_downscale_passes + 1):
            encoded = _best_fitting_jpeg(
                candidate,
                requested_quality=encoding.quality,
                maximum_bytes=maximum_bytes,
            )
            if encoded is not None:
                payload, quality = encoded
                operations: list[str] = []
                if candidate.size != image.size:
                    operations.append(
                        f"{operation_prefix}-downscale:{candidate.width}x{candidate.height}"
                    )
                if quality != encoding.quality:
                    operations.append(f"{operation_prefix}-jpeg-quality:{quality}")
                return _EncodedImage(
                    data=payload,
                    width=candidate.width,
                    height=candidate.height,
                    operations=tuple(operations),
                )
            if downscale_pass == max_downscale_passes:
                break
            next_width = max(
                1,
                candidate.width * _JPEG_DOWNSCALE_NUMERATOR // _JPEG_DOWNSCALE_DENOMINATOR,
            )
            next_height = max(
                1,
                candidate.height * _JPEG_DOWNSCALE_NUMERATOR // _JPEG_DOWNSCALE_DENOMINATOR,
            )
            if (next_width, next_height) == candidate.size:
                break
            resized = candidate.resize(
                (next_width, next_height),
                resample=Image.Resampling.LANCZOS,
            )
            candidate.close()
            candidate = resized
    finally:
        candidate.close()
    raise DerivativeRenderError(failure_message)


def _best_fitting_jpeg(
    image: Image.Image,
    *,
    requested_quality: int,
    maximum_bytes: int,
) -> tuple[bytes, int] | None:
    requested = _try_encode_image(
        image,
        JpegEncoding(quality=requested_quality),
        maximum_bytes=maximum_bytes,
    )
    if requested is not None:
        return requested, requested_quality
    if requested_quality == _JPEG_MIN_QUALITY:
        return None

    minimum = _try_encode_image(
        image,
        JpegEncoding(quality=_JPEG_MIN_QUALITY),
        maximum_bytes=maximum_bytes,
    )
    if minimum is None:
        return None
    best_payload = minimum
    best_quality = _JPEG_MIN_QUALITY
    lower = _JPEG_MIN_QUALITY + 1
    upper = requested_quality - 1
    while lower <= upper:
        quality = (lower + upper) // 2
        payload = _try_encode_image(
            image,
            JpegEncoding(quality=quality),
            maximum_bytes=maximum_bytes,
        )
        if payload is None:
            upper = quality - 1
        else:
            best_payload = payload
            best_quality = quality
            lower = quality + 1
    return best_payload, best_quality


def _encode_image(
    image: Image.Image,
    encoding: Encoding,
    *,
    maximum_bytes: int,
) -> bytes:
    payload = _try_encode_image(image, encoding, maximum_bytes=maximum_bytes)
    if payload is None:
        raise DerivativeRenderError("encoded derivative exceeds the output byte limit")
    return payload


def _try_encode_image(
    image: Image.Image,
    encoding: Encoding,
    *,
    maximum_bytes: int,
) -> bytes | None:
    clean = Image.new("RGB", image.size)
    clean.paste(image)
    output = _BoundedOutput(maximum_bytes)
    try:
        if isinstance(encoding, JpegEncoding):
            clean.save(
                output,
                format="JPEG",
                quality=encoding.quality,
                subsampling=0,
                optimize=False,
                progressive=False,
                exif=b"",
            )
        else:
            clean.save(
                output,
                format="PNG",
                compress_level=encoding.compress_level,
                optimize=False,
            )
        return output.getvalue()
    except _OutputLimitExceededError:
        return None
    except (MemoryError, OSError, OverflowError, ValueError):
        raise DerivativeRenderError("derivative encoding failed") from None
    finally:
        clean.close()
        output.close()


def _encode_batch_watermark(
    image: Image.Image,
    *,
    source_format: str,
    maximum_bytes: int,
) -> tuple[bytes, str, str, str]:
    if source_format == "PNG":
        return (
            _encode_image(image, PngEncoding(compress_level=9), maximum_bytes=maximum_bytes),
            "PNG",
            "image/png",
            "png",
        )
    if source_format == "JPEG":
        return (
            _encode_image(image, JpegEncoding(quality=95), maximum_bytes=maximum_bytes),
            "JPEG",
            "image/jpeg",
            "jpg",
        )
    if source_format != "WEBP":
        raise DerivativeRenderError("watermarked image format is unsupported")

    clean = Image.new("RGB", image.size)
    clean.paste(image)
    output = _BoundedOutput(maximum_bytes)
    try:
        clean.save(
            output,
            format="WEBP",
            quality=95,
            method=6,
            exif=b"",
            icc_profile=None,
        )
        return output.getvalue(), "WEBP", "image/webp", "webp"
    except _OutputLimitExceededError:
        raise DerivativeRenderError(
            "encoded watermarked image exceeds the output byte limit"
        ) from None
    except (MemoryError, OSError, OverflowError, ValueError):
        raise DerivativeRenderError("watermarked image encoding failed") from None
    finally:
        clean.close()
        output.close()


def _region_pixels(
    region: RelativeRegion,
    size: tuple[int, int],
) -> tuple[int, int, int, int]:
    width, height = size
    left = region.x * width // RELATIVE_SCALE
    top = region.y * height // RELATIVE_SCALE
    right = (region.x + region.width) * width + RELATIVE_SCALE - 1
    bottom = (region.y + region.height) * height + RELATIVE_SCALE - 1
    right //= RELATIVE_SCALE
    bottom //= RELATIVE_SCALE
    right = min(width, right)
    bottom = min(height, bottom)
    if right <= left or bottom <= top:
        raise DerivativeRenderError("censorship region is empty after pixel normalization")
    return left, top, right, bottom


def _validate_input_geometry(
    width: int,
    height: int,
    limits: DerivativeSafetyLimits,
) -> None:
    if width <= 0 or height <= 0:
        raise DerivativeInputError("raw master dimensions must be positive")
    if width > limits.max_input_width or height > limits.max_input_height:
        raise DerivativeInputError("raw master dimensions exceed the safety limit")
    if width * height > limits.max_input_pixels:
        raise DerivativeInputError("raw master pixels exceed the safety limit")
    if max(width, height) > min(width, height) * limits.max_input_aspect_ratio:
        raise DerivativeInputError("raw master aspect ratio exceeds the safety limit")


def _assert_output_geometry(
    size: tuple[int, int],
    limits: DerivativeSafetyLimits,
) -> None:
    width, height = size
    if (
        width <= 0
        or height <= 0
        or width > limits.max_output_width
        or height > limits.max_output_height
        or width * height > limits.max_output_pixels
    ):
        raise DerivativeRenderError("derivative dimensions exceed the output safety limit")


def _validate_recipe_against_limits(
    recipe: DerivativeRecipe,
    limits: DerivativeSafetyLimits,
) -> None:
    full_width, full_height = recipe.full.max_width, recipe.full.max_height
    full_pixels = min(full_width * full_height, limits.max_input_pixels)
    if (
        full_width > limits.max_output_width
        or full_height > limits.max_output_height
        or full_pixels > limits.max_output_pixels
    ):
        raise DerivativeRecipeError("recipe output dimensions exceed the safety limit")

    teaser_width, teaser_height = recipe.x_teaser.width, recipe.x_teaser.height
    teaser_pixels = teaser_width * teaser_height
    if recipe.x_teaser.fit_mode is TeaserFitMode.DOWNSCALE:
        # A downscale box is only an axis ceiling. Its area can exceed the
        # output-pixel cap because the admitted source is independently capped.
        teaser_pixels = min(teaser_pixels, limits.max_input_pixels)
    if (
        teaser_width > limits.max_output_width
        or teaser_height > limits.max_output_height
        or teaser_pixels > limits.max_output_pixels
    ):
        raise DerivativeRecipeError("recipe output dimensions exceed the safety limit")


def _validate_target_dimensions(width: int, height: int) -> None:
    _strict_int(
        width,
        "target width",
        minimum=1,
        maximum=_ABSOLUTE_RECIPE_DIMENSION_LIMIT,
    )
    _strict_int(
        height,
        "target height",
        minimum=1,
        maximum=_ABSOLUTE_RECIPE_DIMENSION_LIMIT,
    )


def _validate_encoding_and_filename(encoding: Encoding, filename: str) -> None:
    if not isinstance(encoding, (JpegEncoding, PngEncoding)):
        raise DerivativeRecipeError("derivative encoding is invalid")
    _validate_safe_filename(filename)
    expected_extension = ".jpg" if isinstance(encoding, JpegEncoding) else ".png"
    if not filename.casefold().endswith(expected_extension):
        raise DerivativeRecipeError(
            f"output filename must end with {expected_extension} for its encoding"
        )


def _validate_safe_filename(filename: str) -> None:
    if (
        not isinstance(filename, str)
        or _FILENAME_PATTERN.fullmatch(filename) is None
        or ".." in filename
        or filename.endswith(".")
    ):
        raise DerivativeRecipeError("output filename must be a safe logical basename")
    stem = filename.split(".", 1)[0].upper()
    if stem in _WINDOWS_RESERVED_NAMES:
        raise DerivativeRecipeError("output filename is reserved")


def _validate_rgb(value: tuple[int, int, int]) -> None:
    if not isinstance(value, tuple) or len(value) != 3:
        raise DerivativeRecipeError("background RGB value is invalid")
    for component in value:
        _strict_int(component, "background RGB component", minimum=0, maximum=255)


def _bounded_bytes(
    value: ImageBytes | None,
    *,
    maximum: int,
    label: str,
) -> bytes:
    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise DerivativeInputError(f"{label} must be bytes-like")
    try:
        size = value.nbytes if isinstance(value, memoryview) else len(value)
    except (TypeError, ValueError):
        raise DerivativeInputError(f"{label} is invalid") from None
    if size <= 0:
        raise DerivativeInputError(f"{label} is empty")
    if size > maximum:
        raise DerivativeInputError(f"{label} exceeds the input byte limit")
    try:
        payload = bytes(value)
    except (MemoryError, TypeError, ValueError):
        raise DerivativeInputError(f"{label} is invalid") from None
    if len(payload) != size:
        raise DerivativeInputError(f"{label} changed while it was copied")
    return payload


def _strict_int(
    value: int,
    label: str,
    *,
    minimum: int,
    maximum: int,
) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise DerivativeRecipeError(f"{label} must be between {minimum} and {maximum}")


DEFAULT_DERIVATIVE_LIMITS = DerivativeSafetyLimits()
BATCH_WATERMARK_DERIVATIVE_LIMITS = DerivativeSafetyLimits(
    max_output_bytes=64 * 1024 * 1024,
    max_full_output_bytes=64 * 1024 * 1024,
)
