"""Hard MVP limits shared by generation and delivery boundaries."""

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal
from typing import Literal

# One final set may contain every usable image from a large unattended generation
# queue.  Keep this aligned with the review bulk-action ceiling so an owner can
# still operate on the complete set in one command.
MAX_ACCEPTED_IMAGES_PER_RELEASE = 500
MAX_PIPELINE_MASTER_WIDTH = 8192
MAX_PIPELINE_MASTER_HEIGHT = 8192
MAX_PIPELINE_MASTER_PIXELS = 12_000_000
# Archive limits are intentionally independent from the release limit.  Large
# releases are partitioned into deterministic handoff archives while retaining
# one release-wide ordered manifest.
PATREON_MAX_DERIVATIVE_IMAGES = 100
PATREON_MAX_ARCHIVE_PARTS = (
    MAX_ACCEPTED_IMAGES_PER_RELEASE + PATREON_MAX_DERIVATIVE_IMAGES - 1
) // PATREON_MAX_DERIVATIVE_IMAGES
PATREON_MAX_IMAGE_BYTES = 16 * 1024 * 1024
PATREON_MAX_TOTAL_IMAGE_BYTES = 128 * 1024 * 1024
PATREON_MAX_ARCHIVE_BYTES = 160 * 1024 * 1024
X_STATIC_IMAGE_MAX_BYTES = 5 * 1024 * 1024

HIRES_WORKFLOW_NODE_CLASS = "LatentUpscaleBy"
_LATENT_PIXEL_SCALE = 8
_COMFY_OUTPUT_NODE_CLASSES = frozenset({"SaveImage", "SaveImageWebsocket"})


class DeliverabilityError(ValueError):
    """A planned output cannot traverse every required MVP stage."""


@dataclass(frozen=True, slots=True)
class EffectiveGenerationDimensions:
    width: int
    height: int

    @property
    def pixels(self) -> int:
        return self.width * self.height


@dataclass(frozen=True, slots=True)
class _ComfyGeometry:
    kind: Literal["latent", "image"]
    width: int
    height: int

    def decoded(self) -> EffectiveGenerationDimensions:
        scale = _LATENT_PIXEL_SCALE if self.kind == "latent" else 1
        return EffectiveGenerationDimensions(
            width=self.width * scale,
            height=self.height * scale,
        )


def patreon_full_output_byte_budget(accepted_image_count: int) -> int:
    """Return the per-image cap for the largest deterministic archive part.

    Every archive part contains at most ``PATREON_MAX_DERIVATIVE_IMAGES`` paid
    images and one duplicate clean public preview.  The release-wide count is
    validated here, but it must not shrink every derivative as though all 500
    images were placed in one ZIP.
    """

    if (
        isinstance(accepted_image_count, bool)
        or not isinstance(accepted_image_count, int)
        or not 1 <= accepted_image_count <= MAX_ACCEPTED_IMAGES_PER_RELEASE
    ):
        raise DeliverabilityError(
            "Patreon full-output budgeting requires between 1 and "
            f"{MAX_ACCEPTED_IMAGES_PER_RELEASE} accepted images"
        )
    images_in_largest_part = min(
        accepted_image_count,
        PATREON_MAX_DERIVATIVE_IMAGES,
    )
    return min(
        PATREON_MAX_IMAGE_BYTES,
        PATREON_MAX_TOTAL_IMAGE_BYTES // (images_in_largest_part + 1),
    )


def effective_generation_dimensions(
    *,
    width: int,
    height: int,
    hires_scale: float,
    workflow_node_classes: Collection[str],
) -> EffectiveGenerationDimensions:
    """Return a conservative upper bound for the workflow's decoded output size."""

    if HIRES_WORKFLOW_NODE_CLASS not in workflow_node_classes:
        return EffectiveGenerationDimensions(width=width, height=height)

    scale = Decimal(str(hires_scale))
    latent_width = Decimal(width // _LATENT_PIXEL_SCALE) * scale
    latent_height = Decimal(height // _LATENT_PIXEL_SCALE) * scale
    return EffectiveGenerationDimensions(
        width=(int(latent_width.to_integral_value(rounding=ROUND_CEILING)) * _LATENT_PIXEL_SCALE),
        height=(int(latent_height.to_integral_value(rounding=ROUND_CEILING)) * _LATENT_PIXEL_SCALE),
    )


def require_generation_deliverability(
    *,
    width: int,
    height: int,
    hires_scale: float,
    workflow_node_classes: Collection[str],
) -> EffectiveGenerationDimensions:
    """Reject a generation whose masters exceed the strictest downstream decoder."""

    effective = effective_generation_dimensions(
        width=width,
        height=height,
        hires_scale=hires_scale,
        workflow_node_classes=workflow_node_classes,
    )
    if (
        effective.width > MAX_PIPELINE_MASTER_WIDTH
        or effective.height > MAX_PIPELINE_MASTER_HEIGHT
        or effective.pixels > MAX_PIPELINE_MASTER_PIXELS
    ):
        raise DeliverabilityError(
            "effective generated image "
            f"{effective.width}x{effective.height} ({effective.pixels} pixels) exceeds "
            "the quality and derivative pipeline limit of "
            f"{MAX_PIPELINE_MASTER_WIDTH}x{MAX_PIPELINE_MASTER_HEIGHT} and "
            f"{MAX_PIPELINE_MASTER_PIXELS} pixels"
        )
    return effective


def require_comfy_workflow_deliverability(
    workflow: Mapping[str, object],
) -> tuple[EffectiveGenerationDimensions, ...]:
    """Validate every executed output path in one exact rendered Comfy graph."""

    if not workflow or any(not isinstance(node_id, str) for node_id in workflow):
        raise DeliverabilityError("rendered workflow geometry is invalid")

    cache: dict[str, _ComfyGeometry] = {}
    visiting: set[str] = set()

    def linked_geometry(
        inputs: Mapping[str, object],
        name: str,
        *,
        expected: Literal["latent", "image"],
    ) -> _ComfyGeometry:
        link = inputs.get(name)
        if (
            not isinstance(link, list)
            or len(link) != 2
            or not isinstance(link[0], str)
            or not link[0]
            or isinstance(link[1], bool)
            or link[1] != 0
        ):
            raise DeliverabilityError("rendered workflow geometry is invalid")
        geometry = infer(link[0])
        if geometry.kind != expected:
            raise DeliverabilityError("rendered workflow geometry is invalid")
        return geometry

    def infer(node_id: str) -> _ComfyGeometry:
        cached = cache.get(node_id)
        if cached is not None:
            return cached
        if node_id in visiting:
            raise DeliverabilityError("rendered workflow geometry contains a cycle")
        raw_node = workflow.get(node_id)
        if not isinstance(raw_node, Mapping):
            raise DeliverabilityError("rendered workflow geometry is invalid")
        node_class = raw_node.get("class_type")
        inputs = raw_node.get("inputs")
        if not isinstance(node_class, str) or not isinstance(inputs, Mapping):
            raise DeliverabilityError("rendered workflow geometry is invalid")

        visiting.add(node_id)
        try:
            if node_class == "EmptyLatentImage":
                width = _positive_comfy_integer(inputs.get("width"))
                height = _positive_comfy_integer(inputs.get("height"))
                geometry = _ComfyGeometry(
                    kind="latent",
                    width=width // _LATENT_PIXEL_SCALE,
                    height=height // _LATENT_PIXEL_SCALE,
                )
                if geometry.width < 1 or geometry.height < 1:
                    raise DeliverabilityError("rendered workflow geometry is invalid")
            elif node_class == "KSampler":
                geometry = linked_geometry(inputs, "latent_image", expected="latent")
            elif node_class == "LatentUpscaleBy":
                source = linked_geometry(inputs, "samples", expected="latent")
                scale = _positive_comfy_scale(inputs.get("scale_by"))
                maximum_latent_dimension = (
                    max(
                        MAX_PIPELINE_MASTER_WIDTH,
                        MAX_PIPELINE_MASTER_HEIGHT,
                    )
                    // _LATENT_PIXEL_SCALE
                )
                if scale > Decimal(maximum_latent_dimension):
                    raise _geometry_limit_error()
                geometry = _ComfyGeometry(
                    kind="latent",
                    width=int(
                        (Decimal(source.width) * scale).to_integral_value(rounding=ROUND_CEILING)
                    ),
                    height=int(
                        (Decimal(source.height) * scale).to_integral_value(rounding=ROUND_CEILING)
                    ),
                )
            elif node_class == "VAEDecode":
                source = linked_geometry(inputs, "samples", expected="latent")
                geometry = _ComfyGeometry(
                    kind="image",
                    width=source.width * _LATENT_PIXEL_SCALE,
                    height=source.height * _LATENT_PIXEL_SCALE,
                )
            elif node_class == "FaceDetailer":
                geometry = linked_geometry(inputs, "image", expected="image")
            elif node_class in _COMFY_OUTPUT_NODE_CLASSES:
                geometry = linked_geometry(inputs, "images", expected="image")
            else:
                raise DeliverabilityError(
                    "rendered workflow output uses unsupported geometry nodes"
                )
            _require_geometry_within_limits(geometry.decoded())
            cache[node_id] = geometry
            return geometry
        finally:
            visiting.discard(node_id)

    outputs = tuple(
        infer(node_id).decoded()
        for node_id, raw_node in workflow.items()
        if isinstance(raw_node, Mapping)
        and raw_node.get("class_type") in _COMFY_OUTPUT_NODE_CLASSES
    )
    if not outputs:
        raise DeliverabilityError("rendered workflow has no supported output")
    return outputs


def _positive_comfy_integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise DeliverabilityError("rendered workflow geometry is invalid")
    return value


def _positive_comfy_scale(value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DeliverabilityError("rendered workflow geometry is invalid")
    scale = Decimal(str(value))
    if not scale.is_finite() or scale <= 0:
        raise DeliverabilityError("rendered workflow geometry is invalid")
    return scale


def _require_geometry_within_limits(
    dimensions: EffectiveGenerationDimensions,
) -> None:
    if (
        dimensions.width > MAX_PIPELINE_MASTER_WIDTH
        or dimensions.height > MAX_PIPELINE_MASTER_HEIGHT
        or dimensions.pixels > MAX_PIPELINE_MASTER_PIXELS
    ):
        raise _geometry_limit_error(dimensions)


def _geometry_limit_error(
    dimensions: EffectiveGenerationDimensions | None = None,
) -> DeliverabilityError:
    detail = (
        ""
        if dimensions is None
        else f" ({dimensions.width}x{dimensions.height}, {dimensions.pixels} pixels)"
    )
    return DeliverabilityError(
        "rendered workflow geometry exceeds the pipeline limit"
        f"{detail}: {MAX_PIPELINE_MASTER_WIDTH}x{MAX_PIPELINE_MASTER_HEIGHT} and "
        f"{MAX_PIPELINE_MASTER_PIXELS} pixels"
    )
