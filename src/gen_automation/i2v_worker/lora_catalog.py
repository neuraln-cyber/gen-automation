"""Closed, reviewed LoRA catalog for the DaSiWa WAN 2.2 I2V worker.

The queue accepts catalog identifiers, never filenames.  Every catalog entry is a
paired WAN 2.2 A14B LoRA: one artifact is applied to the high-noise diffusion
model and the other to the low-noise diffusion model.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

LoraCatalogId = Literal[
    "wan-general-nsfw-v0.08a",
    "bouncing-boobs-wan22",
    "m4crom4sti4-natural-breasts-k3nk",
    "dr34ml4y-aio-nsfw-wan22-v2",
    "smoothmix-xxx-animations-wan22",
]

LoraArtifactRole = Literal[
    "lora_wan_general_nsfw_high",
    "lora_wan_general_nsfw_low",
    "lora_bouncing_boobs_high",
    "lora_bouncing_boobs_low",
    "lora_m4crom4sti4_high",
    "lora_m4crom4sti4_low",
    "lora_dr34ml4y_high",
    "lora_dr34ml4y_low",
    "lora_smoothmix_animations_high",
    "lora_smoothmix_animations_low",
]

MIN_REVIEWED_LORA_STRENGTH = 0.01
MAX_REVIEWED_LORA_STRENGTH = 2.0
MAX_REVIEWED_LORA_SELECTIONS = 5


@dataclass(frozen=True, slots=True)
class ReviewedLoraArtifact:
    role: LoraArtifactRole
    filename: str
    byte_size: int
    sha256: str
    civitai_model_id: int
    civitai_version_id: int
    civitai_file_id: int

    @property
    def install_path(self) -> str:
        return f"models/loras/{self.filename}"

    @property
    def canonical_version_url(self) -> str:
        return (
            f"https://civitai.com/models/{self.civitai_model_id}"
            f"?modelVersionId={self.civitai_version_id}"
        )


@dataclass(frozen=True, slots=True)
class ReviewedPairedLora:
    catalog_id: LoraCatalogId
    display_name: str
    creator_name: str
    canonical_source_url: str
    high: ReviewedLoraArtifact
    low: ReviewedLoraArtifact
    trigger_words: tuple[str, ...]
    automatic_trigger_words: tuple[str, ...]
    recommended_initial_strength: float
    strength_guidance: str
    usage_notes: str
    source_usage: SourceUsage


@dataclass(frozen=True, slots=True)
class SourceUsage:
    recorded_at: str
    credit_required: bool
    commercial_use: tuple[str, ...]
    derivatives_allowed: bool
    different_license_allowed: bool


class ReviewedLoraPromptError(ValueError):
    """A prompt combines mutually exclusive terms from a reviewed entry."""


_WAN_GENERAL = ReviewedPairedLora(
    catalog_id="wan-general-nsfw-v0.08a",
    display_name="WAN General NSFW v0.08a",
    creator_name="CubeyAI",
    canonical_source_url="https://civitai.com/models/1307155",
    high=ReviewedLoraArtifact(
        role="lora_wan_general_nsfw_high",
        filename="NSFW-22-H-e8.safetensors",
        byte_size=613_516_752,
        sha256="34e2144d3cd65360f97d09ccbe03e1c39a096df6c9234af5fe3899d1b63cda39",
        civitai_model_id=1_307_155,
        civitai_version_id=2_073_605,
        civitai_file_id=1_969_798,
    ),
    low=ReviewedLoraArtifact(
        role="lora_wan_general_nsfw_low",
        filename="NSFW-22-L-e8.safetensors",
        byte_size=613_516_752,
        sha256="d6b783742f4d5fd63a0223ae1d5bf64fc995a6b408480ac2a00528ae0d4146db",
        civitai_model_id=1_307_155,
        civitai_version_id=2_083_303,
        civitai_file_id=1_979_213,
    ),
    trigger_words=("nsfwsks",),
    automatic_trigger_words=("nsfwsks",),
    recommended_initial_strength=0.3,
    strength_guidance=(
        "Conservative implementation A/B starting point; the author's published 0.9 "
        "value applies to the older WAN 2.1 release"
    ),
    usage_notes="Experimental, unfinished, slightly underbaked, and seed-sensitive.",
    source_usage=SourceUsage(
        recorded_at="2026-08-13",
        credit_required=False,
        commercial_use=("RentCivit",),
        derivatives_allowed=True,
        different_license_allowed=True,
    ),
)

_BOUNCING_BOOBS = ReviewedPairedLora(
    catalog_id="bouncing-boobs-wan22",
    display_name="Bouncing Boobs WAN 2.2",
    creator_name="ai_build_art",
    canonical_source_url="https://civitai.com/models/1343431",
    high=ReviewedLoraArtifact(
        role="lora_bouncing_boobs_high",
        filename="BounceHighWan2_2.safetensors",
        byte_size=306_847_512,
        sha256="a4f4398031e9f39571310355f23e2d104c21143f517cf053e06d21f1c48d3d52",
        civitai_model_id=1_343_431,
        civitai_version_id=2_191_217,
        civitai_file_id=2_084_187,
    ),
    low=ReviewedLoraArtifact(
        role="lora_bouncing_boobs_low",
        filename="BounceLowWan2_2.safetensors",
        byte_size=306_847_504,
        sha256="3ba8320137ba7d99885624dc512d8e0ea02f24364eabbe31e803fec785339ecb",
        civitai_model_id=1_343_431,
        civitai_version_id=2_191_270,
        civitai_file_id=2_084_219,
    ),
    trigger_words=("her breasts are bouncing",),
    automatic_trigger_words=("her breasts are bouncing",),
    recommended_initial_strength=1.0,
    strength_guidance="Author standalone workflow value; use 0.5-0.6 when stacking",
    usage_notes="WAN 2.2 I2V-A14B high/low motion pair.",
    source_usage=SourceUsage(
        recorded_at="2026-08-13",
        credit_required=True,
        commercial_use=("Image", "RentCivit"),
        derivatives_allowed=False,
        different_license_allowed=False,
    ),
)

_M4CROM4STI4 = ReviewedPairedLora(
    catalog_id="m4crom4sti4-natural-breasts-k3nk",
    display_name="M4CROM4STI4 Natural Breasts Physics (K3NK)",
    creator_name="K3NK",
    canonical_source_url="https://civitai.com/models/1852647",
    high=ReviewedLoraArtifact(
        role="lora_m4crom4sti4_high",
        filename="wan22-m4crom4sti4-i2v-20epoc-high-k3nk.safetensors",
        byte_size=306_807_976,
        sha256="851c928737235b4a4a2c5993c893c79ee46a3131aa9b16eb56de1dcc576c3ad9",
        civitai_model_id=1_852_647,
        civitai_version_id=2_265_575,
        civitai_file_id=2_157_676,
    ),
    low=ReviewedLoraArtifact(
        role="lora_m4crom4sti4_low",
        filename="wan22-m4crom4sti4-i2v-20epoc-low-k3nk.safetensors",
        byte_size=306_807_976,
        sha256="c8a940ad5ab59a15c7f39624f694482a020f0dd047cec56f498b58418d3d937c",
        civitai_model_id=1_852_647,
        civitai_version_id=2_266_727,
        civitai_file_id=2_158_834,
    ),
    trigger_words=("m4crom4sti4",),
    automatic_trigger_words=("m4crom4sti4",),
    recommended_initial_strength=0.5,
    strength_guidance=(
        "Conservative implementation starting point; A/B 0.7 then 1.0 (the author "
        "publishes no numeric value)"
    ),
    usage_notes=(
        "Strong breast-size/anatomy bias; may alter anatomy, face, hands, or pubic detail. "
        "Trained at low resolution."
    ),
    source_usage=SourceUsage(
        recorded_at="2026-08-13",
        credit_required=True,
        commercial_use=(),
        derivatives_allowed=False,
        different_license_allowed=True,
    ),
)

_DR34ML4Y = ReviewedPairedLora(
    catalog_id="dr34ml4y-aio-nsfw-wan22-v2",
    display_name="DR34ML4Y All-In-One NSFW I2V 14B v2",
    creator_name="c0ur4ge",
    canonical_source_url="https://civitai.com/models/1811313",
    high=ReviewedLoraArtifact(
        role="lora_dr34ml4y_high",
        filename="DR34ML4Y_I2V_14B_HIGH_V2.safetensors",
        byte_size=306_807_976,
        sha256="d9931756c202bd8d4946c0d163c1269231a6352b51bb4235f6a19894c9ad8c68",
        civitai_model_id=1_811_313,
        civitai_version_id=2_553_151,
        civitai_file_id=2_441_563,
    ),
    low=ReviewedLoraArtifact(
        role="lora_dr34ml4y_low",
        filename="DR34ML4Y_I2V_14B_LOW_V2.safetensors",
        byte_size=306_807_976,
        sha256="066ee4bfafb685c85f08174c8283cd11bc6d36f4845347f20d633ab44581601f",
        civitai_model_id=1_811_313,
        civitai_version_id=2_553_271,
        civitai_file_id=2_441_662,
    ),
    trigger_words=("m15510n4ry", "bl0wj0b", "c0wg1rl", "d0gg1e", "d0ubl3_bj"),
    automatic_trigger_words=(),
    recommended_initial_strength=0.7,
    strength_guidance=(
        "Conservative implementation A/B starting point; the author publishes no numeric "
        "WAN v2 strength (use 0.5 when stacking)"
    ),
    usage_notes=(
        "Choose the one concept keyword matching the requested motion; the worker does not "
        "auto-append mutually exclusive keywords."
    ),
    source_usage=SourceUsage(
        recorded_at="2026-08-13",
        credit_required=False,
        commercial_use=("RentCivit",),
        derivatives_allowed=False,
        different_license_allowed=False,
    ),
)

_SMOOTHMIX_ANIMATIONS = ReviewedPairedLora(
    catalog_id="smoothmix-xxx-animations-wan22",
    display_name="SmoothMix XXX Animations WAN 2.2",
    creator_name="DigitalPastel",
    canonical_source_url="https://civitai.com/models/2040641",
    high=ReviewedLoraArtifact(
        role="lora_smoothmix_animations_high",
        filename="SmoothXXXAnimation_High.safetensors",
        byte_size=306_807_280,
        sha256="eac4f4341008abb00434d08fed1d4fda4a144bc94cd26b4819f629f930a75181",
        civitai_model_id=2_040_641,
        civitai_version_id=2_376_136,
        civitai_file_id=2_266_910,
    ),
    low=ReviewedLoraArtifact(
        role="lora_smoothmix_animations_low",
        filename="SmoothXXXAnimation_Low.safetensors",
        byte_size=306_807_280,
        sha256="ad50dfc46c765a6ccc36d40e8a5f77ac2db041f68266593add12ac5f5eac2d76",
        civitai_model_id=2_040_641,
        civitai_version_id=2_376_143,
        civitai_file_id=2_266_915,
    ),
    trigger_words=(),
    automatic_trigger_words=(),
    recommended_initial_strength=1.0,
    strength_guidance=(
        "Author showcase value is 1.0; use about 0.3 when face stability is the priority"
    ),
    usage_notes=(
        "Generic motion enhancer; the author recommends combining it with a scene-specific "
        "LoRA for complex or niche poses. Higher strength can animate everything, including "
        "the face."
    ),
    source_usage=SourceUsage(
        recorded_at="2026-08-13",
        credit_required=True,
        commercial_use=("RentCivit", "Image"),
        derivatives_allowed=False,
        different_license_allowed=True,
    ),
)

LORA_CATALOG: Mapping[str, ReviewedPairedLora] = MappingProxyType(
    {
        entry.catalog_id: entry
        for entry in (
            _WAN_GENERAL,
            _BOUNCING_BOOBS,
            _M4CROM4STI4,
            _DR34ML4Y,
            _SMOOTHMIX_ANIMATIONS,
        )
    }
)

LORA_ARTIFACTS_BY_ROLE: Mapping[str, ReviewedLoraArtifact] = MappingProxyType(
    {
        artifact.role: artifact
        for entry in LORA_CATALOG.values()
        for artifact in (entry.high, entry.low)
    }
)

REQUIRED_LORA_ROLES: tuple[str, ...] = tuple(LORA_ARTIFACTS_BY_ROLE)


def reviewed_lora(catalog_id: str) -> ReviewedPairedLora:
    try:
        return LORA_CATALOG[catalog_id]
    except KeyError:
        raise ValueError("LoRA catalog identifier is not reviewed") from None


def validate_reviewed_lora_prompt(
    positive_prompt: str,
    catalog_ids: tuple[str, ...],
) -> None:
    for catalog_id in catalog_ids:
        entry = reviewed_lora(catalog_id)
        if entry.automatic_trigger_words or len(entry.trigger_words) < 2:
            continue
        present = [
            trigger
            for trigger in entry.trigger_words
            if re.search(
                rf"(?<!\w){re.escape(trigger)}(?!\w)",
                positive_prompt,
                re.IGNORECASE,
            )
        ]
        if len(present) > 1:
            raise ReviewedLoraPromptError(
                "reviewed LoRA prompt contains mutually exclusive concept terms"
            )
