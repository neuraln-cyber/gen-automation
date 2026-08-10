from collections.abc import Collection, Iterable
from enum import StrEnum


class DuoCompositionPreset(StrEnum):
    """Stable layout identifiers understood by Controlled Duo v2 workflows."""

    FLEXIBLE = "flexible"
    CLOSE_PORTRAIT = "close_portrait"
    OVERHEAD = "overhead"
    LOW_ANGLE = "low_angle"
    DIAGONAL_DEPTH = "diagonal_depth"
    BACK_TO_BACK = "back_to_back"
    FULL_BODY = "full_body"


class TrioCompositionPreset(StrEnum):
    """Stable identity-region layouts understood by Controlled Trio v1."""

    FLEXIBLE = "trio_flexible"
    ROW = "trio_row"
    TRIANGLE = "trio_triangle"
    DEPTH = "trio_depth"


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
    CONTROLLED_TRIO_V1 = "controlled_trio_v1"
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
    require_coherent_workflow_capabilities(
        capabilities,
        reviewed_node_classes=reviewed_node_classes,
    )
    return frozenset(capabilities)


def require_coherent_workflow_capabilities(
    capabilities: Collection[WorkflowCapability],
    *,
    reviewed_node_classes: Collection[str] = (),
) -> None:
    values = frozenset(capabilities)
    controlled = values.intersection(
        {
            WorkflowCapability.CONTROLLED_DUO_V2,
            WorkflowCapability.CONTROLLED_TRIO_V1,
        }
    )
    if len(controlled) > 1:
        raise ValueError("a workflow cannot declare both Controlled Duo and Controlled Trio")
    if (
        WorkflowCapability.DUO_STRICT_ISOLATION in values
        and WorkflowCapability.CONTROLLED_DUO_V2 not in values
    ):
        raise ValueError("strict duo isolation requires Controlled Duo v2")
    if WorkflowCapability.DUO_HIGH_QUALITY in values:
        raise ValueError("high duo quality is not implemented")
    supports_legacy_regional = (
        WorkflowCapability.REGIONAL_PROMPTING_V1 in values
        or LEGACY_REGIONAL_PROMPT_NODE_CLASSES.issubset(reviewed_node_classes)
    )
    if controlled and supports_legacy_regional:
        raise ValueError("controlled and legacy regional workflow capabilities cannot be mixed")


def require_controlled_duo_capabilities(
    capabilities: Collection[WorkflowCapability],
    *,
    isolation_mode: DuoIsolationMode,
    quality_mode: DuoQualityMode,
) -> None:
    require_coherent_workflow_capabilities(capabilities)
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


def require_controlled_trio_capabilities(
    capabilities: Collection[WorkflowCapability],
    *,
    isolation_mode: DuoIsolationMode,
    quality_mode: DuoQualityMode,
) -> None:
    require_coherent_workflow_capabilities(capabilities)
    if WorkflowCapability.CONTROLLED_TRIO_V1 not in capabilities:
        raise ValueError("Controlled Trio v1 requires an explicitly capable workflow")
    if isolation_mode != DuoIsolationMode.BALANCED:
        raise ValueError("Controlled Trio v1 currently supports balanced isolation only")
    if quality_mode == DuoQualityMode.HIGH:
        raise ValueError("high trio quality is not implemented")
