# SPDX-License-Identifier: AGPL-3.0-only
"""Pinned Lokken simple-v5 recipe, independent of historical Eros workflows.

Source: LokkenJP/TenEros_Max_optimized_learned at
f2dd2b315ea27c0a340ba7f3e03ef078e4753c28. No model downloads at inference.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from gen_automation.i2v_worker.models import GenerationSettings

LOKKEN_VARIANT = "eros_beta7_lokken"
LOKKEN_SHA256 = "01a696efc96708f3edd15ff20fa3f56068974ba08a1b4c5801cef2bd49600ad7"
LOKKEN_BYTES = 18691644302
LOKKEN_FILENAME = "10Eros_Max_h3_8stepTURBO_beta7_activation_learned_HQ.safetensors"
LOKKEN_UPSCALER_ROLE = "h3_lokken_upscaler"
LOKKEN_UPSCALER_FILENAME = "minimax_h3_latent_upscaler_3d_conv_v1_fp16.safetensors"
LOKKEN_UPSCALER_BYTES = 690592672
LOKKEN_UPSCALER_SHA256 = "043e5a48e161610ef6c3ea974645220354d06fa618abca15f76d084812eb55c2"
LOKKEN_ENCODER_FILENAME = "qwen3vl_32b_minimax_h3_int4_convrot.safetensors"
LOKKEN_ENCODER_BYTES = 14952506624
LOKKEN_ENCODER_SHA256 = "4389571ab5db4180bcae33d13d07855319530f60a85ab0f1061379401a8ef66c"
LOKKEN_RECIPE = "lokken-simple-v5-hq-20261101"


@dataclass(frozen=True)
class LokkenGeometry:
    base_width: int
    base_height: int
    refine_width: int
    refine_height: int
    output_width: int
    output_height: int


def megapixel_canvas(width: int, height: int, megapixels: float) -> tuple[int, int]:
    """Comfy ResolutionSelector/LBH convention: 1024² pixels, nearest 32."""
    if width <= 0 or height <= 0 or not math.isfinite(megapixels) or megapixels <= 0:
        raise ValueError("invalid Lokken canvas")
    ratio = width / height
    target_height = math.sqrt(megapixels * 1024 * 1024 / ratio)
    return (
        max(32, round(target_height * ratio / 32) * 32),
        max(32, round(target_height / 32) * 32),
    )


def lokken_geometry(settings: GenerationSettings) -> LokkenGeometry:
    recipe = settings.h3_lokken
    if settings.h3_model_variant != LOKKEN_VARIANT or recipe is None:
        raise ValueError("Lokken geometry requires an explicit Lokken recipe")
    base_w, base_h = megapixel_canvas(settings.width, settings.height, recipe.base_megapixels)
    refine_w, refine_h = megapixel_canvas(base_w, base_h, recipe.target_megapixels)
    # NVIDIA node first truncates w*scale, then rounds to an eight-pixel grid.
    output_w = max(8, round(int(refine_w * recipe.rtx_scale) / 8) * 8)
    output_h = max(8, round(int(refine_h * recipe.rtx_scale) / 8) * 8)
    if min(base_w, base_h) < 128 or max(output_w, output_h) > 4096:
        raise ValueError("Lokken aspect/size exceeds the reviewed output envelope")
    if refine_w <= base_w or refine_h <= base_h:
        raise ValueError("Lokken full generation requires spatial upscaling")
    return LokkenGeometry(base_w, base_h, refine_w, refine_h, output_w, output_h)


def build_lokken_workflow(workflow: dict[str, Any], settings: GenerationSettings) -> dict[str, Any]:
    """Adapt the native REF2VA template; keep the author's actual tensor routing.

    Called after the immutable model bindings and native owner LoRA chain, before
    any legacy tiled-upscale/attention/guide injections. No new prompt content.
    """
    recipe = settings.h3_lokken
    if recipe is None:
        raise ValueError("Lokken recipe missing")
    geometry = lokken_geometry(settings)

    def node(name: str, kind: str, **inputs: object) -> None:
        workflow[name] = {"class_type": kind, "inputs": inputs}

    # Author ordering: native model/optional owner LoRAs -> sigma shift -> VSA -> Kitchen.
    node(
        "lokken-vsa",
        "BlockSparseAttention",
        model=["7", 0],
        selection="vsa",
        **{"selection.keep_percent": 10.0},
        start_percent=0.0,
        end_percent=1.0,
        dense_blocks="0,1",
        min_tokens=12288,
        extra_tokens=0,
        sink_conditioning="exact_kv_and_rows",
        verbose=False,
    )
    node(
        "lokken-kitchen",
        "ModelAttentionBackend",
        model=["lokken-vsa", 0],
        attention="comfy kitchen attention",
    )
    workflow["6"]["inputs"].update(width=geometry.base_width, height=geometry.base_height)
    workflow["11"]["inputs"].update(model=["lokken-kitchen", 0], denoise=settings.h3_denoise)
    # Both guides deliberately use the original image, independently resized by AddGuide.
    node(
        "lokken-base-guide",
        "MiniMaxH3AddGuide",
        positive=["6", 0],
        latent=["6", 1],
        vae=["4", 0],
        image=["1", 0],
        frame_idx=0,
    )
    node("9", "BasicGuider", model=["lokken-kitchen", 0], conditioning=["lokken-base-guide", 0])
    # The denoised output (slot 1), not the sampling output, feeds learned upscaling/audio.
    node("lokken-separate", "LTXVSeparateAVLatent", av_latent=["12", 1])
    node(
        "lokken-upscale",
        "MinimaxH3LatentUpscaler3D",
        latent=["lokken-separate", 0],
        model_name=LOKKEN_UPSCALER_FILENAME,
        mode="megapixels",
        **{"mode.megapixels": recipe.target_megapixels},
        align=32,
        enable_temporal_chunking=True,
        force_unload=True,
        device="cuda",
        precision="fp16",
    )
    node(
        "lokken-concat",
        "LTXVConcatAVLatent",
        video_latent=["lokken-upscale", 0],
        audio_latent=["lokken-separate", 1],
    )
    node(
        "lokken-refine-guide",
        "MiniMaxH3AddGuide",
        positive=["6", 0],
        latent=["lokken-concat", 0],
        vae=["4", 0],
        image=["1", 0],
        frame_idx=0,
    )
    node(
        "lokken-refine-guider",
        "BasicGuider",
        model=["lokken-kitchen", 0],
        conditioning=["lokken-refine-guide", 0],
    )
    node(
        "lokken-refine-sigmas",
        "BasicScheduler",
        model=["lokken-kitchen", 0],
        scheduler=settings.h3_refine_scheduler,
        steps=settings.h3_refine_steps,
        denoise=settings.h3_refine_denoise,
    )
    node(
        "lokken-refine",
        "SamplerCustomAdvanced",
        noise=["8", 0],
        guider=["lokken-refine-guider", 0],
        sampler=["10", 0],
        sigmas=["lokken-refine-sigmas", 0],
        latent_image=["lokken-concat", 0],
    )
    workflow["13"]["inputs"]["samples"] = ["lokken-refine", 0]
    workflow["15"]["inputs"]["samples"] = ["lokken-separate", 1]
    node(
        "lokken-rtx",
        "RTXVideoSuperResolution",
        images=["13", 0],
        resize_type="scale by multiplier",
        **{"resize_type.scale": recipe.rtx_scale},
        quality="ULTRA",
        strength=1.0,
        image_encoding="10-bit RGB (RGB10A2)",
    )
    node(
        "lokken-post",
        "LokkenVideoPostProcessing",
        images=["lokken-rtx", 0],
        audio_pass=["15", 0],
        upscale=False,
        upscale_method="lanczos",
        upscale_by=2.0,
        interpolate=False,
        original_fps=24.0,
        interpolate_by=2,
        dual_pass_rife=False,
        grain=recipe.grain_strength > 0,
        grain_intensity=max(0.001, recipe.grain_strength),
        saturation_mix=0.25,
        compress=True,
        crf=18,
        batching_mode="custom",
        custom_batch_size=4,
        encoder_preset=recipe.encoder_preset,
        h264_profile="auto",
    )
    workflow["14"]["inputs"]["video"] = ["lokken-post", 0]
    del workflow["16"]  # Avoid a competing native encoder/decode path.
    return workflow
