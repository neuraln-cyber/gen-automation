"""Pinned H3 source-size delivery contract, shared by control plane and worker."""

import math

H3_UPSCALER_ROLE = "h3_latent_upscaler"
H3_UPSCALER_FILENAME = "minimax_h3_latent_upscaler_3d_conv_v1_bf16.safetensors"
H3_UPSCALER_BYTES = 690592992
H3_UPSCALER_SHA256 = "4f57821f5837f32f7142b67d815606dbd7550f194e5c769f7d6c3f83b146a5e6"
H3_UPSCALER_CODE_REVISION = "fe6658f6d144066f14150d3526247b417683ff2b"
# C-MMH3 v2.3 optional latent refinement node 2777, not the former custom pass.
H3_REFINE_STEPS = 1
H3_REFINE_DENOISE = 0.2


def h3_base_canvas(width: int, height: int) -> tuple[int, int]:
    """Keep the first pass within the existing 768-short-edge / 1 MP envelope.

    Inputs are the final grid-aligned canvas, not arbitrary source dimensions.
    Never enlarge small sources. Snap downward to avoid exceeding the envelope.
    """
    if min(width, height) < 32 or width % 32 or height % 32:
        raise ValueError("H3 refinement canvas must be aligned to 32 pixels")
    scale = min(1.0, 768 / min(width, height), math.sqrt(768 * 1344 / (width * height)))
    return max(32, math.floor(width * scale / 32) * 32), max(
        32, math.floor(height * scale / 32) * 32
    )
