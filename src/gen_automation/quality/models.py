from dataclasses import dataclass, field

MICROS = 1_000_000
SCORER_VERSION = "cpu-quality-v1"

HARD_MAX_INPUT_BYTES = 256 * 1024 * 1024
HARD_MAX_IMAGE_DIMENSION = 32_768
HARD_MAX_IMAGE_PIXELS = 64_000_000
HARD_MAX_THUMBNAIL_SIZE = 256
HARD_MAX_BATCH_SIZE = 1_024


class QualityAnalysisError(Exception):
    """Base error for deterministic quality analysis."""


class UnsafeImageError(QualityAnalysisError):
    """The supplied bytes are malformed, unsupported, or exceed a safety bound."""


class QualityBatchError(QualityAnalysisError):
    """A batch or duplicate-candidate collection violates its bounded contract."""


class QualityConfigurationError(ValueError):
    """Quality configuration is invalid."""


@dataclass(frozen=True, slots=True)
class QualityConfig:
    max_input_bytes: int = 50 * 1024 * 1024
    max_width: int = 16_384
    max_height: int = 16_384
    max_pixels: int = 32_000_000
    max_aspect_ratio_micros: int = 20 * MICROS
    thumbnail_size: int = 64
    max_batch_size: int = 500
    near_duplicate_hamming_threshold: int = 6

    exposure_target_micros: int = 500_000
    exposure_tolerance_micros: int = 500_000
    contrast_target_micros: int = 200_000
    sharpness_target_micros: int = 100_000

    exposure_weight_micros: int = 100_000
    contrast_weight_micros: int = 200_000
    dynamic_range_weight_micros: int = 200_000
    entropy_weight_micros: int = 200_000
    sharpness_weight_micros: int = 300_000

    def __post_init__(self) -> None:
        integer_fields = (
            "max_input_bytes",
            "max_width",
            "max_height",
            "max_pixels",
            "max_aspect_ratio_micros",
            "thumbnail_size",
            "max_batch_size",
            "near_duplicate_hamming_threshold",
            "exposure_target_micros",
            "exposure_tolerance_micros",
            "contrast_target_micros",
            "sharpness_target_micros",
            "exposure_weight_micros",
            "contrast_weight_micros",
            "dynamic_range_weight_micros",
            "entropy_weight_micros",
            "sharpness_weight_micros",
        )
        for name in integer_fields:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise QualityConfigurationError(f"{name} must be an integer")

        bounded_fields = (
            ("max_input_bytes", 1, HARD_MAX_INPUT_BYTES),
            ("max_width", 1, HARD_MAX_IMAGE_DIMENSION),
            ("max_height", 1, HARD_MAX_IMAGE_DIMENSION),
            ("max_pixels", 1, HARD_MAX_IMAGE_PIXELS),
            ("max_aspect_ratio_micros", MICROS, 100 * MICROS),
            ("thumbnail_size", 16, HARD_MAX_THUMBNAIL_SIZE),
            ("max_batch_size", 1, HARD_MAX_BATCH_SIZE),
            ("near_duplicate_hamming_threshold", 0, 64),
            ("exposure_target_micros", 0, MICROS),
            ("exposure_tolerance_micros", 1, MICROS),
            ("contrast_target_micros", 1, MICROS),
            ("sharpness_target_micros", 1, MICROS),
        )
        for name, minimum, maximum in bounded_fields:
            value = getattr(self, name)
            if value < minimum or value > maximum:
                raise QualityConfigurationError(f"{name} must be between {minimum} and {maximum}")

        weight_names = (
            "exposure_weight_micros",
            "contrast_weight_micros",
            "dynamic_range_weight_micros",
            "entropy_weight_micros",
            "sharpness_weight_micros",
        )
        weights = tuple(getattr(self, name) for name in weight_names)
        if any(weight < 0 or weight > MICROS for weight in weights):
            raise QualityConfigurationError("score weights must be between zero and one million")
        if sum(weights) != MICROS:
            raise QualityConfigurationError("score weights must sum to one million")


DEFAULT_QUALITY_CONFIG = QualityConfig()


@dataclass(frozen=True, slots=True)
class NormalizedThumbnail:
    width: int
    height: int
    luminance: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class QualityMetrics:
    luminance_mean_micros: int
    luminance_std_micros: int
    dynamic_range_micros: int
    entropy_bits_micros: int
    entropy_normalized_micros: int
    sharpness_micros: int


@dataclass(frozen=True, slots=True)
class QualityScoreBreakdown:
    exposure_component_micros: int
    contrast_component_micros: int
    dynamic_range_component_micros: int
    entropy_component_micros: int
    sharpness_component_micros: int
    exposure_contribution_micros: int
    contrast_contribution_micros: int
    dynamic_range_contribution_micros: int
    entropy_contribution_micros: int
    sharpness_contribution_micros: int

    @property
    def total_micros(self) -> int:
        return (
            self.exposure_contribution_micros
            + self.contrast_contribution_micros
            + self.dynamic_range_contribution_micros
            + self.entropy_contribution_micros
            + self.sharpness_contribution_micros
        )


@dataclass(frozen=True, slots=True)
class QualityResult:
    sha256: str
    byte_size: int
    width: int
    height: int
    image_format: str
    thumbnail: NormalizedThumbnail
    metrics: QualityMetrics
    dhash64: int
    score_micros: int
    score_breakdown: QualityScoreBreakdown
    config: QualityConfig
    config_sha256: str
    scorer_version: str
    pillow_version: str

    @property
    def dhash_hex(self) -> str:
        return f"{self.dhash64:016x}"


@dataclass(frozen=True, slots=True)
class NamedQualityResult:
    identifier: str
    result: QualityResult


@dataclass(frozen=True, slots=True)
class DuplicateCandidate:
    identifier: str
    dhash64: int
    quality_score_micros: int

    @classmethod
    def from_result(cls, identifier: str, result: QualityResult) -> "DuplicateCandidate":
        return cls(
            identifier=identifier,
            dhash64=result.dhash64,
            quality_score_micros=result.score_micros,
        )


@dataclass(frozen=True, slots=True)
class DuplicateCluster:
    cluster_id: str
    representative_id: str
    member_ids: tuple[str, ...]
    link_threshold: int
    max_pairwise_hamming: int
