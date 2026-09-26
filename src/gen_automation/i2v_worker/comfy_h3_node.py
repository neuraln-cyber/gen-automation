"""Native loader wrapper: reject a nonzero LoRA that patches no model weights.

Installed as the single reviewed GenAutomationH3 ComfyUI extension. Uses the
native loader's format conversion and quantized-model patching implementation.
"""

import re
from typing import Any

from nodes import LoraLoaderModelOnly  # type: ignore[import-not-found]

from gen_automation.i2v_worker.comfy_h3_upscale import ManagedH3SourceUpscale


class ManagedH3LoraLoader(LoraLoaderModelOnly):  # type: ignore[misc]
    def load_lora_model_only(self, model: Any, lora_name: str, strength_model: float) -> tuple[Any]:
        if re.fullmatch(r"managed-h3/[0-9a-f]{64}\.safetensors", lora_name) is None:
            raise ValueError("H3 LoRA filename is outside the managed library")
        before = sum(len(patches) for patches in model.patches.values())
        result = super().load_lora_model_only(model, lora_name, strength_model)
        after = sum(len(patches) for patches in result[0].patches.values())
        if strength_model != 0 and after <= before:
            raise ValueError("H3 LoRA has no compatible model weights; generation was not started")
        return (result[0],)


class ManagedH3DiagnosticDecode:
    """Decode the base only AFTER refinement, without another sampler execution."""

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, Any]:  # noqa: N802
        return {
            "required": {
                "samples": ("LATENT",),
                "vae": ("VAE",),
                "after_refinement": ("IMAGE",),
            }
        }

    RETURN_TYPES = ("IMAGE",)
    FUNCTION = "decode"
    CATEGORY = "GenAutomation/H3"

    def decode(self, samples: Any, vae: Any, after_refinement: Any) -> Any:
        from nodes import VAEDecode

        # The graph dependency deliberately prevents the extra VAE decode from
        # changing model residency during the sampling/refinement comparison.
        return VAEDecode().decode(vae, samples)


NODE_CLASS_MAPPINGS = {
    "ManagedH3LoraLoader": ManagedH3LoraLoader,
    "ManagedH3DiagnosticDecode": ManagedH3DiagnosticDecode,
    "ManagedH3SourceUpscale": ManagedH3SourceUpscale,
}
