import math
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass

from gen_automation.domain.controlled_duo import (
    DuoQualityMode,
    TrioCompositionPreset,
    WorkflowCapability,
    require_controlled_trio_capabilities,
)
from gen_automation.domain.release_spec import GenerationParameters, WorkflowSpecification

CONTROLLED_TRIO_MARKER_NODE_CLASS = "GenAutomationControlledTrioV1"


class ControlledTrioContractError(ValueError):
    """A fail-closed Controlled Trio binding or workflow-evidence failure."""


@dataclass(frozen=True, slots=True)
class _TrioMaskRegion:
    x: int
    y: int
    width: int
    height: int
    feather_left: int
    feather_top: int
    feather_right: int
    feather_bottom: int

    def as_binding(self) -> dict[str, int]:
        return {
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
            "feather_left": self.feather_left,
            "feather_top": self.feather_top,
            "feather_right": self.feather_right,
            "feather_bottom": self.feather_bottom,
        }


_TRIO_PRESET_REGION_RATIOS: dict[
    TrioCompositionPreset,
    tuple[
        tuple[float, float, float, float],
        tuple[float, float, float, float],
        tuple[float, float, float, float],
    ],
] = {
    TrioCompositionPreset.FLEXIBLE: (
        (0.03, 0.10, 0.28, 0.80),
        (0.36, 0.10, 0.28, 0.80),
        (0.69, 0.10, 0.28, 0.80),
    ),
    TrioCompositionPreset.ROW: (
        (0.02, 0.04, 0.30, 0.92),
        (0.35, 0.04, 0.30, 0.92),
        (0.68, 0.04, 0.30, 0.92),
    ),
    TrioCompositionPreset.TRIANGLE: (
        (0.04, 0.04, 0.38, 0.43),
        (0.58, 0.04, 0.38, 0.43),
        (0.31, 0.53, 0.38, 0.43),
    ),
    TrioCompositionPreset.DEPTH: (
        (0.03, 0.28, 0.34, 0.69),
        (0.40, 0.12, 0.30, 0.62),
        (0.72, 0.03, 0.25, 0.52),
    ),
}
_TRIO_POSITIVE_INVARIANT = (
    "exactly three clearly adult characters, all three visible, no other people"
)
_TRIO_NEGATIVE_INVARIANT = (
    "fourth person, extra person, background person, missing person, duplicate character, "
    "cloned face, merged identities, fused faces, shared face, swapped clothing"
)
_TRIO_LOCAL_NEGATIVE_INVARIANT = (
    "extra face inside this identity region, duplicate subject inside this identity region, "
    "another character's identity traits, identity crossover"
)


def _join_prompt_parts(*parts: str) -> str:
    return ", ".join(part.strip(" ,\n\t") for part in parts if part.strip(" ,\n\t"))


def _aligned_region(
    *,
    canvas_width: int,
    canvas_height: int,
    ratios: tuple[float, float, float, float],
    feather: int,
) -> _TrioMaskRegion:
    x_ratio, y_ratio, width_ratio, height_ratio = ratios
    x = min(canvas_width - 64, max(0, round((canvas_width * x_ratio) / 8.0) * 8))
    y = min(canvas_height - 64, max(0, round((canvas_height * y_ratio) / 8.0) * 8))
    width = min(
        canvas_width - x,
        max(64, round((canvas_width * width_ratio) / 8.0) * 8),
    )
    height = min(
        canvas_height - y,
        max(64, round((canvas_height * height_ratio) / 8.0) * 8),
    )
    return _TrioMaskRegion(
        x=x,
        y=y,
        width=width,
        height=height,
        feather_left=min(feather, width // 4),
        feather_top=min(feather, height // 4),
        feather_right=min(feather, width // 4),
        feather_bottom=min(feather, height // 4),
    )


def _regions_overlap(left: _TrioMaskRegion, right: _TrioMaskRegion) -> bool:
    horizontal = max(left.x, right.x) < min(
        left.x + left.width,
        right.x + right.width,
    )
    vertical = max(left.y, right.y) < min(
        left.y + left.height,
        right.y + right.height,
    )
    return horizontal and vertical


def controlled_trio_bindings(
    generation: GenerationParameters,
) -> dict[str, object] | None:
    if generation.duo_contract_version != 3:
        return None
    preset = generation.composition_preset_id
    if generation.composition_mode != "trio" or not isinstance(
        preset,
        TrioCompositionPreset,
    ):
        raise ControlledTrioContractError("Controlled Trio generation parameters are invalid")

    feather = min(32, max(8, generation.width // 64))
    regions = tuple(
        _aligned_region(
            canvas_width=generation.width,
            canvas_height=generation.height,
            ratios=ratios,
            feather=feather,
        )
        for ratios in _TRIO_PRESET_REGION_RATIOS[preset]
    )
    if any(
        _regions_overlap(regions[left], regions[right]) for left, right in ((0, 1), (0, 2), (1, 2))
    ):
        raise ControlledTrioContractError("Controlled Trio identity regions must be disjoint")

    shared_positive = _join_prompt_parts(
        _TRIO_POSITIVE_INVARIANT,
        generation.prompt,
        generation.interaction_prompt,
        generation.camera_prompt,
    )
    shared_negative = _join_prompt_parts(
        generation.negative_prompt,
        _TRIO_NEGATIVE_INVARIANT,
    )
    local_positive = tuple(
        _join_prompt_parts(
            (
                f"character {label} only, one clearly adult subject in "
                f"character {label}'s identity region"
            ),
            prompt,
            pose,
        )
        for label, prompt, pose in (
            ("A", generation.character_a_prompt, generation.character_a_pose_prompt),
            ("B", generation.character_b_prompt, generation.character_b_pose_prompt),
            ("C", generation.character_c_prompt, generation.character_c_pose_prompt),
        )
    )
    local_negative = tuple(
        _join_prompt_parts(prompt, _TRIO_LOCAL_NEGATIVE_INVARIANT)
        for prompt in (
            generation.character_a_negative_prompt,
            generation.character_b_negative_prompt,
            generation.character_c_negative_prompt,
        )
    )
    base_steps = (
        min(generation.steps, max(8, math.ceil(generation.steps * 0.60)))
        if generation.duo_quality_mode == DuoQualityMode.DRAFT
        else generation.steps
    )
    return {
        "contract_version": 1,
        "composition_preset_id": preset.value,
        "isolation_mode": generation.duo_isolation_mode.value,
        "quality_mode": generation.duo_quality_mode.value,
        "shared_positive_prompt": shared_positive,
        "shared_negative_prompt": shared_negative,
        "character_a_local_positive_prompt": local_positive[0],
        "character_b_local_positive_prompt": local_positive[1],
        "character_c_local_positive_prompt": local_positive[2],
        "character_a_local_negative_prompt": local_negative[0],
        "character_b_local_negative_prompt": local_negative[1],
        "character_c_local_negative_prompt": local_negative[2],
        "character_a": regions[0].as_binding(),
        "character_b": regions[1].as_binding(),
        "character_c": regions[2].as_binding(),
        "base_steps": base_steps,
    }


def _invalid() -> ControlledTrioContractError:
    return ControlledTrioContractError("Controlled Trio workflow evidence is invalid")


def _validate_exact_class_counts(template: Mapping[str, object]) -> None:
    counts = Counter(
        node.get("class_type")
        for node in template.values()
        if isinstance(node, Mapping) and isinstance(node.get("class_type"), str)
    )
    if sum(counts.values()) != len(template) or counts != Counter(
        {
            "CheckpointLoaderSimple": 1,
            "GenAutomationLoraChain": 1,
            "CLIPSetLastLayer": 1,
            "SolidMask": 4,
            "FeatherMask": 3,
            "MaskComposite": 3,
            "CLIPTextEncode": 8,
            "ConditioningSetMask": 6,
            "ConditioningCombine": 6,
            "EmptyLatentImage": 1,
            "KSampler": 1,
            "VAEDecode": 1,
            "SaveImage": 1,
            CONTROLLED_TRIO_MARKER_NODE_CLASS: 1,
        }
    ):
        raise _invalid()


def _node(
    template: Mapping[str, object],
    node_id: object,
    node_class: str,
) -> Mapping[str, object]:
    if not isinstance(node_id, str) or not node_id:
        raise _invalid()
    node = template.get(node_id)
    if not isinstance(node, Mapping) or node.get("class_type") != node_class:
        raise _invalid()
    inputs = node.get("inputs")
    if not isinstance(inputs, Mapping):
        raise _invalid()
    return inputs


def _only_node(
    template: Mapping[str, object],
    node_class: str,
) -> tuple[str, Mapping[str, object]]:
    matches = [
        (node_id, node)
        for node_id, node in template.items()
        if isinstance(node, Mapping) and node.get("class_type") == node_class
    ]
    if len(matches) != 1:
        raise _invalid()
    node_id, node = matches[0]
    inputs = node.get("inputs")
    if not isinstance(inputs, Mapping):
        raise _invalid()
    return node_id, inputs


def _is_binding(value: object, path: str) -> bool:
    return value == {"$gen": path}


def _validate_backbone(
    template: Mapping[str, object],
) -> tuple[str, str, str]:
    checkpoint_id, checkpoint = _only_node(template, "CheckpointLoaderSimple")
    lora_id, lora = _only_node(template, "GenAutomationLoraChain")
    _clip_id, clip = _only_node(template, "CLIPSetLastLayer")
    latent_id, latent = _only_node(template, "EmptyLatentImage")
    if (
        checkpoint != {"ckpt_name": {"$gen": "checkpoint.runtime_filename"}}
        or lora != {"model": [checkpoint_id, 0], "clip": [checkpoint_id, 1]}
        or clip
        != {
            "clip": [lora_id, 1],
            "stop_at_clip_layer": {"$gen": "generation.clip_stop_at_layer"},
        }
        or latent
        != {
            "batch_size": {"$gen": "generation.outputs_per_job"},
            "height": {"$gen": "generation.height"},
            "width": {"$gen": "generation.width"},
        }
    ):
        raise _invalid()
    if (
        len(
            [
                node
                for node in template.values()
                if isinstance(node, Mapping) and node.get("class_type") == "VAEDecode"
            ]
        )
        != 1
        or len(
            [
                node
                for node in template.values()
                if isinstance(node, Mapping) and node.get("class_type") == "SaveImage"
            ]
        )
        != 1
    ):
        raise _invalid()
    return checkpoint_id, lora_id, latent_id


def _validate_mask(
    template: Mapping[str, object],
    *,
    mask_node_id: str,
    character: str,
) -> str:
    composite = _node(template, mask_node_id, "MaskComposite")
    if (
        set(composite) != {"destination", "source", "x", "y", "operation"}
        or composite.get("operation") != "add"
    ):
        raise _invalid()
    destination = composite.get("destination")
    source = composite.get("source")
    if (
        not isinstance(destination, list)
        or len(destination) != 2
        or not isinstance(destination[0], str)
        or destination[1] != 0
        or not isinstance(source, list)
        or len(source) != 2
        or not isinstance(source[0], str)
        or source[1] != 0
    ):
        raise _invalid()
    full = _node(template, destination[0], "SolidMask")
    feather = _node(template, source[0], "FeatherMask")
    region_link = feather.get("mask")
    if (
        not isinstance(region_link, list)
        or len(region_link) != 2
        or not isinstance(region_link[0], str)
        or region_link[1] != 0
    ):
        raise _invalid()
    region = _node(template, region_link[0], "SolidMask")
    if (
        full
        != {
            "value": 0.0,
            "width": {"$gen": "generation.width"},
            "height": {"$gen": "generation.height"},
        }
        or region
        != {
            "value": 1.0,
            "width": {"$gen": f"controlled_trio.{character}.width"},
            "height": {"$gen": f"controlled_trio.{character}.height"},
        }
        or set(feather) != {"mask", "left", "top", "right", "bottom"}
        or not _is_binding(composite.get("x"), f"controlled_trio.{character}.x")
        or not _is_binding(composite.get("y"), f"controlled_trio.{character}.y")
    ):
        raise _invalid()
    for edge in ("left", "top", "right", "bottom"):
        if not _is_binding(
            feather.get(edge),
            f"controlled_trio.{character}.feather_{edge}",
        ):
            raise _invalid()
    return destination[0]


def _prompt_nodes(template: Mapping[str, object], *, clip_node_id: str) -> dict[str, str]:
    prompts: dict[str, str] = {}
    for node_id, node in template.items():
        if not isinstance(node, Mapping) or node.get("class_type") != "CLIPTextEncode":
            continue
        inputs = node.get("inputs")
        if not isinstance(inputs, Mapping):
            raise _invalid()
        text = inputs.get("text")
        if (
            not isinstance(text, Mapping)
            or set(text) != {"$gen"}
            or set(inputs) != {"clip", "text"}
            or not isinstance(text["$gen"], str)
            or text["$gen"] in prompts
            or inputs.get("clip") != [clip_node_id, 0]
        ):
            raise _invalid()
        prompts[text["$gen"]] = node_id
    expected = {
        "controlled_trio.shared_positive_prompt",
        "controlled_trio.shared_negative_prompt",
        "controlled_trio.character_a_local_positive_prompt",
        "controlled_trio.character_b_local_positive_prompt",
        "controlled_trio.character_c_local_positive_prompt",
        "controlled_trio.character_a_local_negative_prompt",
        "controlled_trio.character_b_local_negative_prompt",
        "controlled_trio.character_c_local_negative_prompt",
    }
    if set(prompts) != expected:
        raise _invalid()
    return prompts


def _masked_node(
    template: Mapping[str, object],
    *,
    prompt_node_id: str,
    mask_node_id: str,
) -> str:
    matches = []
    for node_id, node in template.items():
        if not isinstance(node, Mapping) or node.get("class_type") != "ConditioningSetMask":
            continue
        inputs = node.get("inputs")
        if isinstance(inputs, Mapping) and inputs == {
            "conditioning": [prompt_node_id, 0],
            "mask": [mask_node_id, 0],
            "strength": 1.0,
            "set_cond_area": "default",
        }:
            matches.append(node_id)
    if len(matches) != 1:
        raise _invalid()
    return matches[0]


def _combine(
    template: Mapping[str, object],
    *,
    left: str,
    right: str,
) -> str:
    matches = []
    for node_id, node in template.items():
        if not isinstance(node, Mapping) or node.get("class_type") != "ConditioningCombine":
            continue
        inputs = node.get("inputs")
        if inputs == {"conditioning_1": [left, 0], "conditioning_2": [right, 0]}:
            matches.append(node_id)
    if len(matches) != 1:
        raise _invalid()
    return matches[0]


def prepare_controlled_trio_template(
    template: dict[str, object],
    *,
    specification: WorkflowSpecification,
    generation: GenerationParameters,
) -> dict[str, object]:
    markers = [
        (node_id, node)
        for node_id, node in template.items()
        if isinstance(node, Mapping) and node.get("class_type") == CONTROLLED_TRIO_MARKER_NODE_CLASS
    ]
    declares = WorkflowCapability.CONTROLLED_TRIO_V1 in frozenset(specification.capabilities)
    if generation.composition_mode != "trio":
        if markers or declares:
            raise ControlledTrioContractError("Controlled Trio workflow capability is invalid")
        return template
    if generation.duo_contract_version != 3 or len(markers) != 1 or not declares:
        raise ControlledTrioContractError("Controlled Trio workflow capability is invalid")
    _validate_exact_class_counts(template)
    try:
        require_controlled_trio_capabilities(
            frozenset(specification.capabilities),
            isolation_mode=generation.duo_isolation_mode,
            quality_mode=generation.duo_quality_mode,
        )
    except ValueError as error:
        raise ControlledTrioContractError(str(error)) from error

    marker_id, marker = markers[0]
    marker_inputs = marker.get("inputs")
    expected_marker = {
        "contract_version": 1,
        "isolation_mode": "balanced",
        "mask_topology": "three_disjoint_regions_v1",
        "character_a_mask_node_id": "7",
        "character_b_mask_node_id": "10",
        "character_c_mask_node_id": "13",
        "base_sampler_node_id": "35",
        "final_sampler_node_id": "35",
    }
    if marker_inputs != expected_marker:
        raise _invalid()

    checkpoint_id, lora_id, latent_id = _validate_backbone(template)
    _, clip_inputs = _only_node(template, "CLIPSetLastLayer")
    clip_link = clip_inputs.get("clip")
    if not isinstance(clip_link, list) or len(clip_link) != 2:
        raise _invalid()
    clip_id = next(
        node_id
        for node_id, node in template.items()
        if isinstance(node, Mapping) and node.get("class_type") == "CLIPSetLastLayer"
    )
    full_masks = {
        _validate_mask(template, mask_node_id="7", character="character_a"),
        _validate_mask(template, mask_node_id="10", character="character_b"),
        _validate_mask(template, mask_node_id="13", character="character_c"),
    }
    if len(full_masks) != 1:
        raise _invalid()

    prompts = _prompt_nodes(template, clip_node_id=clip_id)
    if (
        len(
            [
                node
                for node in template.values()
                if isinstance(node, Mapping) and node.get("class_type") == "ConditioningSetMask"
            ]
        )
        != 6
    ):
        raise _invalid()
    positive_masked = [
        _masked_node(
            template,
            prompt_node_id=prompts[f"controlled_trio.character_{label}_local_positive_prompt"],
            mask_node_id=mask,
        )
        for label, mask in (("a", "7"), ("b", "10"), ("c", "13"))
    ]
    negative_masked = [
        _masked_node(
            template,
            prompt_node_id=prompts[f"controlled_trio.character_{label}_local_negative_prompt"],
            mask_node_id=mask,
        )
        for label, mask in (("a", "7"), ("b", "10"), ("c", "13"))
    ]
    positive = prompts["controlled_trio.shared_positive_prompt"]
    negative = prompts["controlled_trio.shared_negative_prompt"]
    for masked in positive_masked:
        positive = _combine(template, left=positive, right=masked)
    for masked in negative_masked:
        negative = _combine(template, left=negative, right=masked)

    sampler_id, sampler = _only_node(template, "KSampler")
    if sampler_id != "35" or sampler != {
        "cfg": {"$gen": "generation.cfg"},
        "denoise": 1.0,
        "latent_image": [latent_id, 0],
        "model": [lora_id, 0],
        "negative": [negative, 0],
        "positive": [positive, 0],
        "sampler_name": {"$gen": "generation.sampler"},
        "scheduler": {"$gen": "generation.scheduler"},
        "seed": {"$gen": "generation.seed"},
        "steps": {"$gen": "controlled_trio.base_steps"},
    }:
        raise _invalid()
    decode_id, decode = _only_node(template, "VAEDecode")
    _, save = _only_node(template, "SaveImage")
    if decode != {"samples": [sampler_id, 0], "vae": [checkpoint_id, 2]} or save != {
        "filename_prefix": "gen-automation-controlled-trio-balanced-v1",
        "images": [decode_id, 0],
    }:
        raise _invalid()
    return {node_id: node for node_id, node in template.items() if node_id != marker_id}
