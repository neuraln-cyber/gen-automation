"""Pure validation for the private I2V bootstrap manifest."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from gen_automation.i2v_worker.lora_catalog import (
    LORA_ARTIFACTS_BY_ROLE,
    REQUIRED_LORA_ROLES,
)

BASELINE_I2V_MODEL_ROLES = (
    "diffusion_model_high",
    "diffusion_model_low",
    "text_encoder",
    "vae",
)


def required_i2v_model_roles(*, reviewed_loras_enabled: bool) -> tuple[str, ...]:
    return (
        (*BASELINE_I2V_MODEL_ROLES, *REQUIRED_LORA_ROLES)
        if reviewed_loras_enabled
        else BASELINE_I2V_MODEL_ROLES
    )


def validated_i2v_manifest_objects(
    document: object,
    *,
    reviewed_loras_enabled: bool,
) -> dict[str, Mapping[str, Any]]:
    if (
        not isinstance(document, dict)
        or set(document) != {"schema", "objects"}
        or document.get("schema") != "gen-automation/i2v-private-model-mirror/v1"
        or not isinstance(document.get("objects"), list)
    ):
        raise ValueError("I2V private model manifest is invalid")
    by_role: dict[str, Mapping[str, Any]] = {}
    for value in document["objects"]:
        if not isinstance(value, dict) or not isinstance(value.get("role"), str):
            raise ValueError("I2V private model manifest is invalid")
        role = value["role"]
        if role in by_role:
            raise ValueError("I2V private model manifest duplicates a role")
        by_role[role] = value

    required_roles = required_i2v_model_roles(reviewed_loras_enabled=reviewed_loras_enabled)
    if any(role not in by_role for role in required_roles):
        raise ValueError("I2V private model manifest is incomplete")
    if reviewed_loras_enabled:
        for role in REQUIRED_LORA_ROLES:
            value = by_role[role]
            artifact = LORA_ARTIFACTS_BY_ROLE[role]
            if (
                value.get("target_filename") != artifact.filename
                or value.get("bytes") != artifact.byte_size
                or value.get("sha256") != artifact.sha256
            ):
                raise ValueError("I2V private model manifest has an invalid reviewed LoRA")
    return by_role
