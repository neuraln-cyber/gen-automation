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


NODE_CLASS_MAPPINGS = {
    "ManagedH3LoraLoader": ManagedH3LoraLoader,
    "ManagedH3SourceUpscale": ManagedH3SourceUpscale,
}
