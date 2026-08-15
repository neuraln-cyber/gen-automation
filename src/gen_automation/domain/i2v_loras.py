"""Closed-catalog LoRA selections shared by the I2V API and services."""

from __future__ import annotations

import math
from collections.abc import Mapping
from enum import StrEnum
from typing import Any

from gen_automation.i2v_worker.lora_catalog import (
    LORA_CATALOG,
    MAX_REVIEWED_LORA_SELECTIONS,
    MAX_REVIEWED_LORA_STRENGTH,
    MIN_REVIEWED_LORA_STRENGTH,
    ReviewedLoraPromptError,
    validate_reviewed_lora_prompt,
)


class I2VLoraSelectionError(ValueError):
    """A submitted I2V LoRA selection is outside the reviewed catalog."""


class I2VLoraPromptError(I2VLoraSelectionError):
    """A prompt violates a reviewed catalog entry's trigger contract."""


class I2VLoraSettingsKind(StrEnum):
    BASELINE = "baseline"
    REVIEWED = "reviewed"
    INVALID = "invalid"


def normalize_i2v_settings(settings: Mapping[str, Any]) -> dict[str, Any]:
    """Copy settings and canonicalize only the closed-catalog LoRA field.

    Other I2V settings remain forward-compatible and are validated by the worker
    contract. LoRAs are different: filenames must never cross the public API.
    """

    normalized = dict(settings)
    if "loras" not in normalized:
        return normalized
    raw_loras = normalized["loras"]
    if not isinstance(raw_loras, list):
        raise I2VLoraSelectionError("I2V LoRAs must be a list")
    if len(raw_loras) > MAX_REVIEWED_LORA_SELECTIONS:
        raise I2VLoraSelectionError(
            f"at most {MAX_REVIEWED_LORA_SELECTIONS} reviewed I2V LoRAs may be selected"
        )

    by_catalog_id: dict[str, dict[str, float | str]] = {}
    for raw_selection in raw_loras:
        if not isinstance(raw_selection, dict) or set(raw_selection) != {
            "catalog_id",
            "strength",
        }:
            raise I2VLoraSelectionError("each I2V LoRA must contain only catalog_id and strength")
        catalog_id = raw_selection["catalog_id"]
        if not isinstance(catalog_id, str) or catalog_id not in LORA_CATALOG:
            raise I2VLoraSelectionError("I2V LoRA catalog identifier is not reviewed")
        if catalog_id in by_catalog_id:
            raise I2VLoraSelectionError("I2V LoRA selections must be unique")
        strength = raw_selection["strength"]
        if (
            isinstance(strength, bool)
            or not isinstance(strength, int | float)
            or not math.isfinite(strength)
            or strength < MIN_REVIEWED_LORA_STRENGTH
            or strength > MAX_REVIEWED_LORA_STRENGTH
        ):
            raise I2VLoraSelectionError(
                "I2V LoRA strength must be between "
                f"{MIN_REVIEWED_LORA_STRENGTH} and {MAX_REVIEWED_LORA_STRENGTH}"
            )
        by_catalog_id[catalog_id] = {
            "catalog_id": catalog_id,
            "strength": float(strength),
        }

    normalized["loras"] = [
        by_catalog_id[catalog_id] for catalog_id in LORA_CATALOG if catalog_id in by_catalog_id
    ]
    return normalized


def selected_i2v_loras(settings: Mapping[str, Any] | None) -> tuple[dict[str, Any], ...]:
    """Return normalized selections, treating an omitted field as no LoRA."""

    if settings is None or "loras" not in settings:
        return ()
    normalized = normalize_i2v_settings(settings)
    return tuple(normalized["loras"])


def classify_i2v_lora_settings(settings: Mapping[str, Any]) -> I2VLoraSettingsKind:
    """Classify frozen queue settings without repairing legacy/unreviewed forms."""

    try:
        return (
            I2VLoraSettingsKind.REVIEWED
            if selected_i2v_loras(settings)
            else I2VLoraSettingsKind.BASELINE
        )
    except I2VLoraSelectionError:
        return I2VLoraSettingsKind.INVALID


def validate_i2v_lora_prompt(
    positive_prompt: str,
    settings: Mapping[str, Any],
) -> None:
    """Reject mutually exclusive manual concepts before a paid worker attempt."""

    selections = selected_i2v_loras(settings)
    try:
        validate_reviewed_lora_prompt(
            positive_prompt,
            tuple(str(selection["catalog_id"]) for selection in selections),
        )
    except ReviewedLoraPromptError as error:
        raise I2VLoraPromptError(str(error)) from None
