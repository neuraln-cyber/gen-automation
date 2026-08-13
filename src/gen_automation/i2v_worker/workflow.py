from __future__ import annotations

import copy
import json
import re
import secrets
from pathlib import Path
from typing import Any
from uuid import UUID

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


class WorkflowError(Exception):
    pass


def load_workflow_template(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise WorkflowError("workflow template is invalid") from None
    if not isinstance(raw, dict) or set(raw) != set(_EXPECTED_NODE_CLASSES):
        raise WorkflowError("workflow template is invalid")
    for node_id, class_type in _EXPECTED_NODE_CLASSES.items():
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
) -> tuple[dict[str, Any], int, str]:
    seed = settings.seed if settings.seed >= 0 else secrets.randbelow(2**63)
    frame_prefix = f"i2v/{job_id}/{attempt_id}/frame"
    values: dict[str, object] = {
        "input.image": input_filename,
        "prompt.positive": effective_positive_prompt(positive_prompt, settings),
        "prompt.negative": negative_prompt,
        "generation.seed": seed,
        "generation.width": settings.width,
        "generation.height": settings.height,
        "generation.frame_count": settings.frame_count,
        "sampling.steps": settings.steps,
        "sampling.high_end_step": settings.high_end_step,
        "sampling.cfg": settings.cfg,
        "sampling.sampler": settings.sampler,
        "sampling.scheduler": settings.scheduler,
        "sampling.high_shift": settings.high_shift,
        "sampling.low_shift": settings.low_shift,
        "output.frame_prefix": frame_prefix,
    }
    rendered = _replace(copy.deepcopy(template), values)
    if _contains_placeholder(rendered):
        raise WorkflowError("workflow template contains an unresolved binding")
    rendered = _inject_reviewed_loras(rendered, settings)
    return rendered, seed, frame_prefix


def effective_positive_prompt(
    positive_prompt: str,
    settings: GenerationSettings,
) -> str:
    """Append each selected catalog trigger exactly once, case-insensitively."""

    if not settings.loras:
        return positive_prompt
    try:
        validate_reviewed_lora_prompt(
            positive_prompt,
            tuple(selection.catalog_id for selection in settings.loras),
        )
    except ReviewedLoraPromptError:
        raise WorkflowError(
            "reviewed LoRA prompt contains mutually exclusive concept terms"
        ) from None
    result = positive_prompt
    for selection in settings.loras:
        entry = reviewed_lora(selection.catalog_id)
        for trigger in entry.automatic_trigger_words:
            result = _append_trigger_once(result, trigger)
    return result


def lora_provenance(settings: GenerationSettings) -> list[dict[str, object]]:
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
