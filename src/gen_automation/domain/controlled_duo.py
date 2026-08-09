from collections.abc import Collection, Iterable
from enum import StrEnum


class DuoCompositionPreset(StrEnum):
    """Stable layout identifiers understood by Controlled Duo v2 workflows."""

    CLOSE_PORTRAIT = "close_portrait"
    OVERHEAD = "overhead"
    LOW_ANGLE = "low_angle"
    DIAGONAL_DEPTH = "diagonal_depth"
    BACK_TO_BACK = "back_to_back"
    FULL_BODY = "full_body"


class DuoIsolationMode(StrEnum):
    BALANCED = "balanced"
    STRICT = "strict"


class DuoQualityMode(StrEnum):
    DRAFT = "draft"
    STANDARD = "standard"
    HIGH = "high"


class WorkflowCapability(StrEnum):
    """Explicit, approval-owned workflow behavior flags.

    New capabilities must be declared during workflow approval. The only
    compatibility inference retained is the legacy regional-prompting graph,
    whose node pair predates capability manifests.
    """

    CONTROLLED_DUO_V2 = "controlled_duo_v2"
    DUO_STRICT_ISOLATION = "duo_strict_isolation"
    DUO_HIGH_QUALITY = "duo_high_quality"
    REGIONAL_PROMPTING_V1 = "regional_prompting_v1"


LEGACY_REGIONAL_PROMPT_NODE_CLASSES = frozenset(
    {
        "ConditioningCombine",
        "ConditioningSetAreaPercentage",
    }
)


def effective_workflow_capabilities(
    declared: Iterable[WorkflowCapability | str],
    *,
    reviewed_node_classes: Collection[str],
) -> frozenset[WorkflowCapability]:
    """Return approved capabilities plus the one supported legacy inference."""

    capabilities = {WorkflowCapability(value) for value in declared}
    if LEGACY_REGIONAL_PROMPT_NODE_CLASSES.issubset(reviewed_node_classes):
        capabilities.add(WorkflowCapability.REGIONAL_PROMPTING_V1)
    return frozenset(capabilities)


def require_controlled_duo_capabilities(
    capabilities: Collection[WorkflowCapability],
    *,
    isolation_mode: DuoIsolationMode,
    quality_mode: DuoQualityMode,
) -> None:
    if WorkflowCapability.CONTROLLED_DUO_V2 not in capabilities:
        raise ValueError("Controlled Duo v2 requires an explicitly capable workflow")
    requested_strict = isolation_mode == DuoIsolationMode.STRICT
    workflow_is_strict = WorkflowCapability.DUO_STRICT_ISOLATION in capabilities
    if requested_strict != workflow_is_strict:
        if requested_strict:
            raise ValueError("strict duo isolation requires an explicitly capable workflow")
        raise ValueError("a strict-isolation workflow requires strict duo isolation mode")
    if quality_mode == DuoQualityMode.HIGH:
        raise ValueError("high duo quality is not implemented")
