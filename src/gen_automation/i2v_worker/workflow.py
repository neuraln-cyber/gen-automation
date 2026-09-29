from __future__ import annotations

import copy
import json
import re
import secrets
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import UUID

from gen_automation.i2v_worker.h3_upscale import (
    H3_UPSCALER_FILENAME,
    H3_UPSCALER_ROLE,
    h3_base_canvas,
)
from gen_automation.i2v_worker.lora_catalog import (
    ReviewedLoraPromptError,
    reviewed_lora,
    validate_reviewed_lora_prompt,
)
from gen_automation.i2v_worker.models import GenerationSettings

_EXPECTED_NODE_CLASSES = {
    "1": "LoadImage",
    "2": "UNETLoader",
    "3": "UNETLoader",
    "4": "CLIPLoader",
    "5": "VAELoader",
    "6": "CLIPTextEncode",
    "7": "CLIPTextEncode",
    "8": "ModelSamplingSD3",
    "9": "ModelSamplingSD3",
    "10": "WanImageToVideo",
    "11": "KSamplerAdvanced",
    "12": "KSamplerAdvanced",
    "13": "VAEDecode",
    "14": "SaveImage",
}
_MINIMAX_NODE_CLASSES = {
    "1": "MiniMaxH3Director",
    "2": "UNETLoader",
    "3": "CLIPLoader",
    "4": "VAELoader",
    "5": "VAELoader",
    "6": "MiniMaxH3DirectorGuide",
    "7": "MiniMaxH3SigmaShift",
    "8": "DaSiWa_SeedControl",
    "9": "BasicGuider",
    "10": "KSamplerSelect",
    "11": "BasicScheduler",
    "12": "SamplerCustomAdvanced",
    "13": "VAEDecode",
    "14": "SaveVideo",
    "15": "VAEDecodeAudio",
    "16": "CreateVideo",
    "h3-attention": "ModelAttentionBackend",
    "h3-torch-settings": "ModelPatchTorchSettings",
    "h3-lora-stack": "DaSiWa_LTX2LoraLoader",
    "h3-preview": "ModelPreviewOverrideKJ",
}

_FACE_FIDELITY_POSITIVE = (
    "Facial identity and the source facial expression remain consistent throughout. "
    "The head keeps the exact source angle without turning, tilting, nodding, or "
    "translating. The eyes may perform one subtle natural blink; otherwise the gaze, "
    "eyebrows, cheeks, lips, mouth, and jaw remain stable."
)
_FACE_FIDELITY_NEGATIVE = (
    "face morphing, identity drift, expression change, smile change, frown, eyebrow "
    "movement, mouth movement, lip movement, talking, speaking, lip-sync, chewing, "
    "jaw movement, head turn, head rotation, head tilt, nodding, head movement, gaze "
    "shift, eye direction change, repeated blinking, exaggerated blink"
)
_FACE_FIDELITY_NAG = {
    "nag_scale": 11.0,
    "nag_tau": 2.37,
    "nag_alpha": 0.25,
    "nag_sigma_end": 0.0,
}


class WorkflowError(Exception):
    pass


def load_workflow_template(path: Path, *, profile: str = "wan22") -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise WorkflowError("workflow template is invalid") from None
    expected = _MINIMAX_NODE_CLASSES if profile == "minimax_h3" else _EXPECTED_NODE_CLASSES
    if profile not in {"wan22", "minimax_h3"}:
        raise WorkflowError("workflow profile is invalid")
    if not isinstance(raw, dict) or set(raw) != set(expected):
        raise WorkflowError("workflow template is invalid")
    for node_id, class_type in expected.items():
        node = raw.get(node_id)
        if not isinstance(node, dict) or node.get("class_type") != class_type:
            raise WorkflowError("workflow template is invalid")
    return raw


def render_workflow(
    template: dict[str, Any],
    *,
    input_filename: str,
    positive_prompt: str,
    negative_prompt: str,
    settings: GenerationSettings,
    job_id: UUID,
    attempt_id: UUID,
    model_paths: Mapping[str, str] | None = None,
) -> tuple[dict[str, Any], int, str]:
    seed = settings.seed if settings.seed >= 0 else secrets.randbelow(2**63)
    frame_prefix = f"i2v/{job_id}/{attempt_id}/frame"
    values: dict[str, object] = {
        "input.image": input_filename,
        "prompt.positive": effective_positive_prompt(positive_prompt, settings),
        "prompt.negative": effective_negative_prompt(negative_prompt, settings),
        "generation.seed": seed,
        "generation.width": settings.width,
        "generation.height": settings.height,
        "generation.frame_count": settings.frame_count,
        "generation.duration": settings.frame_count / settings.fps,
        "input.timeline": json.dumps(
            {
                "version": 1,
                "items": [{"type": "image", "slot": 0, "value": input_filename, "enabled": True}],
                "prompt_blocks": [],
                "resolution": {"input_scaling": "Off"},
            },
            separators=(",", ":"),
        ),
        "sampling.steps": settings.steps,
        "sampling.high_end_step": settings.high_end_step,
        "sampling.cfg": settings.cfg,
        "sampling.sampler": settings.sampler,
        "sampling.scheduler": settings.scheduler,
        "sampling.high_shift": settings.high_shift,
        "sampling.low_shift": settings.low_shift,
        "output.frame_prefix": frame_prefix,
        "generation.fps": settings.fps,
        "sampling.video_shift": settings.video_shift,
        "sampling.audio_shift": settings.audio_shift,
    }
    if settings.profile == "minimax_h3":
        if not model_paths:
            raise WorkflowError("MiniMax model bindings are missing")
        values.update({f"model.{role}": path for role, path in model_paths.items()})
    rendered = _replace(copy.deepcopy(template), values)
    if _contains_placeholder(rendered):
        raise WorkflowError("workflow template contains an unresolved binding")
    if settings.profile == "minimax_h3":
        rendered = _inject_h3_loras(rendered, settings)
        rendered["11"]["inputs"]["denoise"] = settings.h3_denoise
        if settings.cfg != 1 or (settings.match_source_resolution and settings.h3_refine_cfg != 1):
            rendered["h3-negative"] = {
                "class_type": "CLIPTextEncode",
                "inputs": {"clip": ["h3-lora-stack", 1], "text": negative_prompt},
            }
        if settings.cfg != 1:
            rendered["9"] = {
                "class_type": "CFGGuider",
                "inputs": {
                    "model": ["h3-preview", 0],
                    "positive": ["6", 0],
                    "negative": ["h3-negative", 0],
                    "cfg": settings.cfg,
                },
            }
        if settings.match_source_resolution:
            rendered = _inject_h3_upscale(rendered, settings, model_paths or {})
        if settings.h3_save_base_video:
            rendered["h3-base-decode"] = {
                "class_type": "ManagedH3DiagnosticDecode",
                "inputs": {"samples": ["12", 0], "vae": ["4", 0], "after_refinement": ["13", 0]},
            }
            rendered["h3-base-create"] = copy.deepcopy(rendered["16"])
            rendered["h3-base-create"]["inputs"]["images"] = ["h3-base-decode", 0]
            rendered["h3-base-save"] = copy.deepcopy(rendered["14"])
            rendered["h3-base-save"]["inputs"].update(
                video=["h3-base-create", 0], filename_prefix=frame_prefix + "-base"
            )
        return rendered, seed, frame_prefix
    rendered = _inject_reviewed_loras(rendered, settings)
    if settings.face_fidelity == "stable_expression":
        rendered = _enable_face_fidelity(rendered)
    return rendered, seed, frame_prefix


def effective_positive_prompt(
    positive_prompt: str,
    settings: GenerationSettings,
) -> str:
    """Append each selected catalog trigger exactly once, case-insensitively."""

    result = positive_prompt
    if settings.loras:
        try:
            validate_reviewed_lora_prompt(
                positive_prompt,
                tuple(selection.catalog_id for selection in settings.loras),
            )
        except ReviewedLoraPromptError:
            raise WorkflowError(
                "reviewed LoRA prompt contains mutually exclusive concept terms"
            ) from None
        for selection in settings.loras:
            entry = reviewed_lora(selection.catalog_id)
            for trigger in entry.automatic_trigger_words:
                result = _append_trigger_once(result, trigger)
    if settings.face_fidelity == "stable_expression":
        result = _append_sentence_once(result, _FACE_FIDELITY_POSITIVE)
    return result


def effective_negative_prompt(
    negative_prompt: str,
    settings: GenerationSettings,
) -> str:
    if settings.face_fidelity == "off":
        return negative_prompt
    return _append_sentence_once(negative_prompt, _FACE_FIDELITY_NEGATIVE)


def lora_provenance(settings: GenerationSettings) -> list[dict[str, object]]:
    if settings.profile == "minimax_h3":
        return [item.model_dump(mode="json") for item in settings.h3_loras]
    return [
        {
            "catalog_id": selection.catalog_id,
            "creator_name": entry.creator_name,
            "canonical_source_url": entry.canonical_source_url,
            "strength": selection.strength,
            "high": {
                "role": entry.high.role,
                "filename": entry.high.filename,
                "byte_size": entry.high.byte_size,
                "sha256": entry.high.sha256,
                "civitai_model_id": entry.high.civitai_model_id,
                "civitai_version_id": entry.high.civitai_version_id,
                "civitai_file_id": entry.high.civitai_file_id,
                "canonical_version_url": entry.high.canonical_version_url,
            },
            "low": {
                "role": entry.low.role,
                "filename": entry.low.filename,
                "byte_size": entry.low.byte_size,
                "sha256": entry.low.sha256,
                "civitai_model_id": entry.low.civitai_model_id,
                "civitai_version_id": entry.low.civitai_version_id,
                "civitai_file_id": entry.low.civitai_file_id,
                "canonical_version_url": entry.low.canonical_version_url,
            },
            "trigger_words": list(entry.trigger_words),
            "automatic_trigger_words": list(entry.automatic_trigger_words),
            "source_usage": {
                "recorded_at": entry.source_usage.recorded_at,
                "credit_required": entry.source_usage.credit_required,
                "commercial_use": list(entry.source_usage.commercial_use),
                "derivatives_allowed": entry.source_usage.derivatives_allowed,
                "different_license_allowed": (entry.source_usage.different_license_allowed),
            },
        }
        for selection in settings.loras
        for entry in (reviewed_lora(selection.catalog_id),)
    ]


def _inject_h3_loras(workflow: dict[str, Any], settings: GenerationSettings) -> dict[str, Any]:
    # C-MMH3 v2.3 node 2678: one Basic stack, unit branch multipliers,
    # MODEL before SigmaShift and CLIP before conditioning. The creator's
    # unmodified node handles file metadata and native Comfy patching.
    stack = [
        {
            "on": True,
            "lora": f"managed-h3/{selection.sha256}.safetensors",
            "str": selection.strength,
            "vs": 1.0,
            "as": 1.0,
        }
        for selection in settings.h3_loras
        if selection.strength != 0
    ]
    # Keep the author's pass-through stack even when empty. Its MODEL input is
    # the Director-selected, attention/torch-patched model, never raw UNETLoader.
    workflow["h3-lora-stack"]["inputs"]["stack_data"] = json.dumps(
        stack, separators=(",", ":"), allow_nan=False
    )
    return workflow


def _inject_h3_upscale(
    workflow: dict[str, Any], settings: GenerationSettings, model_paths: Mapping[str, str]
) -> dict[str, Any]:
    if model_paths.get(H3_UPSCALER_ROLE) != H3_UPSCALER_FILENAME:
        raise WorkflowError("source-size H3 delivery requires the pinned latent upscaler")
    base_width, base_height = h3_base_canvas(settings.width, settings.height)
    workflow["1"]["inputs"].update(
        width=base_width,
        height=base_height,
        external_width_overwrite=base_width,
        external_height_overwrite=base_height,
    )
    if (base_width, base_height) == (settings.width, settings.height):
        return workflow
    workflow["h3-refine-sigmas"] = {
        "class_type": "BasicScheduler",
        "inputs": {
            "model": ["h3-lora-stack", 0],
            "scheduler": settings.h3_refine_scheduler,
            "steps": settings.h3_refine_steps,
            "denoise": settings.h3_refine_denoise,
        },
    }
    workflow["h3-upscale-params"] = {
        "class_type": "MMH3LatentUpscaleWithModelParams",
        "inputs": {
            "model_name": H3_UPSCALER_FILENAME,
            "width": settings.width,
            "height": settings.height,
            "device": "cuda",
            "precision": "fp16",
            "offload_model": True,
        },
    }
    workflow["h3-temporal-params"] = {
        "class_type": "MMH3TemporalSplitParams",
        "inputs": {"chunk_length": 85, "temporal_overlap": 17, "anchor_strength": 0.999},
    }
    workflow["h3-spatial-params"] = {
        "class_type": "MMH3SpatialSplitParams",
        "inputs": {
            "upscale_width": settings.width,
            "upscale_height": settings.height,
            "tile_size_mode": "rows_cols",
            "tile_width": 512,
            "tile_height": 512,
            "grid_rows": 2,
            "grid_cols": 3,
            "spatial_w_overlap": 128,
            "spatial_h_overlap": 128,
            "fade_width": 64,
            "fade_height": 64,
            "min_tile_size": 256,
            "overlap_mode": "later",
            "overlap_blend": "linear",
            "masked_area_noise": 0.05,
            "brightness_match": True,
            "dynamic_fade": "widening",
            "dynamic_fade_min": 32,
        },
    }
    workflow["h3-source-upscale"] = {
        "class_type": "MMH3UltimateUpscale",
        "inputs": {
            "latent": ["12", 0],
            "conditioning": ["6", 0],
            "model": ["h3-lora-stack", 0],
            "noise": ["8", 1],
            "sampler": ["10", 0],
            "sigmas": ["h3-refine-sigmas", 0],
            "cfg": settings.h3_refine_cfg,
            "latent_upscale_param": ["h3-upscale-params", 0],
            "temporal_split_param": ["h3-temporal-params", 0],
            "spatial_split_param": ["h3-spatial-params", 0],
        },
    }
    if settings.h3_refine_sampler is not None:
        workflow["h3-refine-sampler"] = {
            "class_type": "KSamplerSelect",
            "inputs": {"sampler_name": settings.h3_refine_sampler},
        }
        workflow["h3-source-upscale"]["inputs"]["sampler"] = ["h3-refine-sampler", 0]
    if settings.h3_refine_cfg != 1:
        workflow["h3-source-upscale"]["inputs"]["negative"] = ["h3-negative", 0]
    workflow["13"]["inputs"]["samples"] = ["h3-source-upscale", 0]
    # Audio decode remains connected to the untouched base pass (node 12).
    return workflow


def _inject_reviewed_loras(
    workflow: dict[str, Any],
    settings: GenerationSettings,
) -> dict[str, Any]:
    if not settings.loras:
        return workflow
    high_model: list[object] = ["2", 0]
    low_model: list[object] = ["3", 0]
    for index, selection in enumerate(settings.loras, start=1):
        entry = reviewed_lora(selection.catalog_id)
        high_node_id = f"lora-high-{index}"
        low_node_id = f"lora-low-{index}"
        workflow[high_node_id] = {
            "class_type": "LoraLoaderModelOnly",
            "inputs": {
                "model": high_model,
                "lora_name": entry.high.filename,
                "strength_model": selection.strength,
            },
        }
        workflow[low_node_id] = {
            "class_type": "LoraLoaderModelOnly",
            "inputs": {
                "model": low_model,
                "lora_name": entry.low.filename,
                "strength_model": selection.strength,
            },
        }
        high_model = [high_node_id, 0]
        low_model = [low_node_id, 0]
    workflow["8"]["inputs"]["model"] = high_model
    workflow["9"]["inputs"]["model"] = low_model
    return workflow


def _enable_face_fidelity(workflow: dict[str, Any]) -> dict[str, Any]:
    conditioning = workflow["10"]
    if conditioning.get("class_type") != "WanImageToVideo" or conditioning.get("inputs", {}).get(
        "start_image"
    ) != ["1", 0]:
        raise WorkflowError("face-fidelity conditioning topology is invalid")

    for node_id in ("11", "12"):
        node = workflow[node_id]
        if node.get("class_type") != "KSamplerAdvanced":
            raise WorkflowError("face-fidelity sampler topology is invalid")
        node["class_type"] = "KSamplerWithNAG (Advanced)"
        node["inputs"].update(_FACE_FIDELITY_NAG)
        node["inputs"]["nag_negative"] = ["10", 1]
    return workflow


def _append_sentence_once(value: str, sentence: str) -> str:
    if sentence.casefold() in value.casefold():
        return value
    stripped = value.rstrip()
    if not stripped:
        return sentence
    separator = " " if stripped.endswith((".", "!", "?")) else ". "
    return f"{stripped}{separator}{sentence}"


def _append_trigger_once(prompt: str, trigger: str) -> str:
    pattern = _trigger_pattern(trigger)
    matches = list(pattern.finditer(prompt))
    if not matches:
        return f"{prompt}, {trigger}" if prompt else trigger
    if len(matches) == 1:
        return prompt
    first = matches[0]
    pieces = [prompt[: first.end()]]
    cursor = first.end()
    for match in matches[1:]:
        pieces.append(prompt[cursor : match.start()])
        cursor = match.end()
    pieces.append(prompt[cursor:])
    return "".join(pieces)


def _trigger_pattern(trigger: str) -> re.Pattern[str]:
    return re.compile(rf"(?<!\w){re.escape(trigger)}(?!\w)", re.IGNORECASE)


def _replace(value: object, bindings: dict[str, object]) -> Any:
    if isinstance(value, dict):
        if set(value) == {"$i2v"}:
            key = value["$i2v"]
            if not isinstance(key, str) or key not in bindings:
                raise WorkflowError("workflow binding is invalid")
            return bindings[key]
        return {key: _replace(item, bindings) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace(item, bindings) for item in value]
    return value


def _contains_placeholder(value: object) -> bool:
    if isinstance(value, dict):
        return "$i2v" in value or any(_contains_placeholder(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_placeholder(item) for item in value)
    return False
