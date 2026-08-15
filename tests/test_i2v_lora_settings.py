from __future__ import annotations

import pytest

from gen_automation.domain.i2v_loras import (
    I2VLoraPromptError,
    I2VLoraSelectionError,
    I2VLoraSettingsKind,
    classify_i2v_lora_settings,
    normalize_i2v_settings,
    validate_i2v_lora_prompt,
)
from gen_automation.i2v_worker.lora_catalog import LORA_CATALOG


def test_reviewed_lora_selections_are_frozen_in_catalog_order() -> None:
    normalized = normalize_i2v_settings(
        {
            "steps": 4,
            "loras": [
                {"catalog_id": "smoothmix-xxx-animations-wan22", "strength": 1},
                {"catalog_id": "wan-general-nsfw-v0.08a", "strength": 0.3},
            ],
        }
    )

    assert normalized == {
        "steps": 4,
        "loras": [
            {"catalog_id": "wan-general-nsfw-v0.08a", "strength": 0.3},
            {"catalog_id": "smoothmix-xxx-animations-wan22", "strength": 1.0},
        ],
    }


@pytest.mark.parametrize(
    "loras, message",
    [
        (
            [{"high": "arbitrary.safetensors", "low": "arbitrary.safetensors"}],
            "only catalog_id and strength",
        ),
        ([{"catalog_id": "unknown", "strength": 1}], "not reviewed"),
        (
            [
                {"catalog_id": "bouncing-boobs-wan22", "strength": 1},
                {"catalog_id": "bouncing-boobs-wan22", "strength": 0.5},
            ],
            "must be unique",
        ),
        ([{"catalog_id": "bouncing-boobs-wan22", "strength": float("nan")}], "between"),
        ([{"catalog_id": "bouncing-boobs-wan22", "strength": 2.01}], "between"),
    ],
)
def test_reviewed_lora_selection_rejects_open_or_invalid_values(
    loras: list[dict[str, object]],
    message: str,
) -> None:
    with pytest.raises(I2VLoraSelectionError, match=message):
        normalize_i2v_settings({"loras": loras})


def test_reviewed_lora_selection_limits_simultaneous_stack_not_catalog_size() -> None:
    all_reviewed = [{"catalog_id": catalog_id, "strength": 0.25} for catalog_id in LORA_CATALOG]
    allowed = all_reviewed[:3]

    normalized = normalize_i2v_settings({"loras": list(reversed(allowed))})

    assert normalized["loras"] == allowed
    with pytest.raises(I2VLoraSelectionError, match="at most 3"):
        normalize_i2v_settings({"loras": all_reviewed[:4]})


def test_frozen_queue_classifier_never_dispatches_legacy_arbitrary_files() -> None:
    assert classify_i2v_lora_settings({}) == I2VLoraSettingsKind.BASELINE
    assert (
        classify_i2v_lora_settings(
            {"loras": [{"catalog_id": "wan-general-nsfw-v0.08a", "strength": 0.3}]}
        )
        == I2VLoraSettingsKind.REVIEWED
    )
    assert (
        classify_i2v_lora_settings(
            {
                "loras": [
                    {
                        "high": "arbitrary-high.safetensors",
                        "low": "arbitrary-low.safetensors",
                        "strength": 1.0,
                    }
                ]
            }
        )
        == I2VLoraSettingsKind.INVALID
    )


def test_dream_prompt_allows_zero_or_one_distinct_term_but_rejects_two() -> None:
    settings = {"loras": [{"catalog_id": "dr34ml4y-aio-nsfw-wan22-v2", "strength": 0.7}]}
    validate_i2v_lora_prompt("descriptive motion", settings)
    validate_i2v_lora_prompt("BL0WJ0B motion, bl0wj0b rhythm", settings)
    with pytest.raises(I2VLoraPromptError, match="mutually exclusive"):
        validate_i2v_lora_prompt("bl0wj0b then M15510N4RY", settings)
