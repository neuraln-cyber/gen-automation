from __future__ import annotations

import copy
import json
import secrets
from pathlib import Path
from typing import Any
from uuid import UUID

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
        "prompt.positive": positive_prompt,
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
    return rendered, seed, frame_prefix


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
