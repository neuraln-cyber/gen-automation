"""Checkpoint identities and recipe boundaries; legacy snapshots mean Turbo V2."""

from collections.abc import Mapping
from typing import Literal

from gen_automation.i2v_worker.h3_lokken import (
    LOKKEN_BYTES,
    LOKKEN_ENCODER_BYTES,
    LOKKEN_ENCODER_FILENAME,
    LOKKEN_ENCODER_SHA256,
    LOKKEN_FILENAME,
    LOKKEN_SHA256,
    LOKKEN_UPSCALER_BYTES,
    LOKKEN_UPSCALER_FILENAME,
    LOKKEN_UPSCALER_ROLE,
    LOKKEN_UPSCALER_SHA256,
    LOKKEN_VARIANT,
)

H3Variant = Literal[
    "turbo_v2", "hybrid_v2", "fl2va_int8", "eros_beta5", "eros_beta7", "eros_beta7_lokken"
]
H3_TURBO_V2_SHA256 = "37c17fd91971c17e02e60798a05eec48d881e1bb970d6309f58cf134a2d03a6b"
H3_EROS_SHA256 = "4dd965496e5b1b83cd13c65cbe7a535b8a4d94ae768a7646b4e336d52c4781cf"
H3_EROS_BYTES = 20970414464
H3_EROS_FILENAME = "10Eros_Max_h3_TURBO-hybrid_beta5_int8.safetensors"
H3_EROS_BETA7_SHA256 = "8a51ca3f7d290a799d89fefcde4ede60962256955a91dfe4746d3cc07814fd6e"
H3_EROS_BETA7_BYTES = 22893150024
H3_EROS_BETA7_FILENAME = "10Eros_Max_h3_8stepTURBO_beta7_int8HQ.safetensors"
H3_NATIVE_SHA256 = "e889202c41dafb67b10d67b97f0d8541508036a6090af23425a5c2615d03c47a"
H3_NATIVE_BYTES = 20970379616
H3_NATIVE_FILENAME = "minimax_h3_fl2va_pruned_int8_convrot.safetensors"
H3_NATIVE_ENCODER_SHA256 = "35a88d51044231fe332301d7a62aa81e3f2cba62febeb446e2c1e3e0ef76f2c6"
H3_NATIVE_ENCODER_BYTES = 15687142551
H3_NATIVE_ENCODER_FILENAME = "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors"
H3_NORMAL_V2_SHA256 = "4cb8e1eaa9c3e5c664822760890bcbe6078455401cfa43205baf322e856d25f8"
H3_NORMAL_V2_BYTES = 20967669160
H3_NORMAL_V2_FILENAME = "DasiwaMinimaxH3_dasiwaHybridV2_3203130.safetensors"
H3_NATIVE_MODELS = {
    "diffusion_model": (H3_NATIVE_SHA256, H3_NATIVE_BYTES, H3_NATIVE_FILENAME),
    "text_encoder": (H3_NATIVE_ENCODER_SHA256, H3_NATIVE_ENCODER_BYTES, H3_NATIVE_ENCODER_FILENAME),
    "video_vae": (
        "52a2c8c73583c86e4f41cdcce3a6ad0ea562987bc0bf3d60a0cef5f5c8e60c0e",
        2811065184,
        "minimax_h3_video_vae_int8_convrot.safetensors",
    ),
    "audio_vae": (
        "8e505d95dd1561d47abd43d4238fd40d9bb1ae9e147ed0a4cba778d76ae4db48",
        605254808,
        "minimax_h3_audio_vae_fp32.safetensors",
    ),
}
H3_EROS_MODELS = {
    **H3_NATIVE_MODELS,
    "diffusion_model": (H3_EROS_SHA256, H3_EROS_BYTES, H3_EROS_FILENAME),
}
H3_EROS_BETA7_MODELS = {
    **H3_NATIVE_MODELS,
    "diffusion_model": (H3_EROS_BETA7_SHA256, H3_EROS_BETA7_BYTES, H3_EROS_BETA7_FILENAME),
}
H3_LOKKEN_MODELS = {
    **H3_NATIVE_MODELS,
    "diffusion_model": (LOKKEN_SHA256, LOKKEN_BYTES, LOKKEN_FILENAME),
    "text_encoder": (LOKKEN_ENCODER_SHA256, LOKKEN_ENCODER_BYTES, LOKKEN_ENCODER_FILENAME),
    LOKKEN_UPSCALER_ROLE: (
        LOKKEN_UPSCALER_SHA256,
        LOKKEN_UPSCALER_BYTES,
        LOKKEN_UPSCALER_FILENAME,
    ),
}


def h3_lokken_models_match(objects: Mapping[str, Mapping[str, object]]) -> bool:
    return all(
        (
            objects.get(role, {}).get("sha256"),
            objects.get(role, {}).get("bytes"),
            objects.get(role, {}).get("target_filename"),
        )
        == identity
        for role, identity in H3_LOKKEN_MODELS.items()
    )


def h3_eros_models_match(objects: Mapping[str, Mapping[str, object]]) -> bool:
    """One reviewed Eros checkpoint, paired with the native H3 encoder and VAEs."""
    return all(
        (
            objects.get(role, {}).get("sha256"),
            objects.get(role, {}).get("bytes"),
            objects.get(role, {}).get("target_filename"),
        )
        == identity
        for role, identity in H3_EROS_MODELS.items()
    )


def h3_eros_beta7_models_match(objects: Mapping[str, Mapping[str, object]]) -> bool:
    """Exact Civitai 3429412 INT8 HQ with the reviewed native H3 support files."""
    return all(
        (
            objects.get(role, {}).get("sha256"),
            objects.get(role, {}).get("bytes"),
            objects.get(role, {}).get("target_filename"),
        )
        == identity
        for role, identity in H3_EROS_BETA7_MODELS.items()
    )


def h3_native_models_match(objects: Mapping[str, Mapping[str, object]]) -> bool:
    """Match the complete official pair and VAEs, not just the diffusion filename."""
    return all(
        (
            objects.get(role, {}).get("sha256"),
            objects.get(role, {}).get("bytes"),
            objects.get(role, {}).get("target_filename"),
        )
        == identity
        for role, identity in H3_NATIVE_MODELS.items()
    )


def h3_job_matches_variant(settings: Mapping[str, object], variant: str) -> bool:
    return (
        settings.get("profile", "wan22") != "minimax_h3"
        or (settings.get("h3_model_variant") or "turbo_v2") == variant
    )


def h3_manifest_matches_variant(model: Mapping[str, object], variant: str) -> bool:
    if variant == LOKKEN_VARIANT:
        return (
            model.get("sha256") == LOKKEN_SHA256
            and model.get("bytes") == LOKKEN_BYTES
            and model.get("target_filename") == LOKKEN_FILENAME
        )
    if variant == "eros_beta7":
        return (
            model.get("sha256") == H3_EROS_BETA7_SHA256
            and model.get("bytes") == H3_EROS_BETA7_BYTES
            and model.get("target_filename") == H3_EROS_BETA7_FILENAME
        )
    if variant == "eros_beta5":
        return (
            model.get("sha256") == H3_EROS_SHA256
            and model.get("bytes") == H3_EROS_BYTES
            and model.get("target_filename") == H3_EROS_FILENAME
        )
    if variant == "fl2va_int8":
        return (
            model.get("sha256") == H3_NATIVE_SHA256
            and model.get("bytes") == H3_NATIVE_BYTES
            and model.get("target_filename") == H3_NATIVE_FILENAME
        )
    if variant == "hybrid_v2":
        return (
            model.get("sha256") == H3_NORMAL_V2_SHA256
            and model.get("bytes") == H3_NORMAL_V2_BYTES
            and model.get("target_filename") == H3_NORMAL_V2_FILENAME
        )
    # History may retain this legacy recipe, but an unknown model is never Turbo.
    return variant == "turbo_v2" and model.get("sha256") == H3_TURBO_V2_SHA256
