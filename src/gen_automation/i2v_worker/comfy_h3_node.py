"""Managed H3 nodes, including non-destructive additive LoRA application."""

import re
from typing import Any

from nodes import LoraLoaderModelOnly  # type: ignore[import-not-found]

from gen_automation.i2v_worker.comfy_h3_lora import apply_h3_lora
from gen_automation.i2v_worker.comfy_h3_upscale import ManagedH3SourceUpscale


class ManagedH3LoraLoader(LoraLoaderModelOnly):  # type: ignore[misc]
    def load_lora_model_only(self, model: Any, lora_name: str, strength_model: float) -> tuple[Any]:
        if re.fullmatch(r"managed-h3/[0-9a-f]{64}\.safetensors", lora_name) is None:
            raise ValueError("H3 LoRA filename is outside the managed library")
        if strength_model == 0:
            return (model,)
        import comfy.utils  # type: ignore[import-not-found]
        import folder_paths  # type: ignore[import-not-found]

        path = folder_paths.get_full_path_or_raise("loras", lora_name)
        lora = comfy.utils.load_torch_file(path, safe_load=True)
        return (apply_h3_lora(model, lora, strength_model, lora_name),)


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
