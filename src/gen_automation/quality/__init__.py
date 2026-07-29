"""Pure, deterministic CPU image-quality analysis."""

from gen_automation.quality.analysis import (
    analyze_batch,
    analyze_image,
    calculate_dhash64,
    calculate_metrics,
    quality_config_sha256,
    score_metrics,
)
from gen_automation.quality.duplicates import (
    cluster_near_duplicates,
    hamming_distance,
)
from gen_automation.quality.models import (
    DEFAULT_QUALITY_CONFIG,
    MICROS,
    SCORER_VERSION,
    DuplicateCandidate,
    DuplicateCluster,
    NamedQualityResult,
    NormalizedThumbnail,
    QualityAnalysisError,
    QualityBatchError,
    QualityConfig,
    QualityConfigurationError,
    QualityMetrics,
    QualityResult,
    QualityScoreBreakdown,
    UnsafeImageError,
)

__all__ = [
    "DEFAULT_QUALITY_CONFIG",
    "MICROS",
    "SCORER_VERSION",
    "DuplicateCandidate",
    "DuplicateCluster",
    "NamedQualityResult",
    "NormalizedThumbnail",
    "QualityAnalysisError",
    "QualityBatchError",
    "QualityConfig",
    "QualityConfigurationError",
    "QualityMetrics",
    "QualityResult",
    "QualityScoreBreakdown",
    "UnsafeImageError",
    "analyze_batch",
    "analyze_image",
    "calculate_dhash64",
    "calculate_metrics",
    "cluster_near_duplicates",
    "hamming_distance",
    "quality_config_sha256",
    "score_metrics",
]
