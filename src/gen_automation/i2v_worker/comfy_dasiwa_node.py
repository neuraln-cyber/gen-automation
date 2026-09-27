"""Headless registration of the creator's unmodified, checksum-pinned loader.

Installed as DaSiWaLoRA/__init__.py beside the upstream loader and logging
helper. No subclass, arithmetic override, key remapping or fallback loader.
Unrelated DaSiWa UI, LLM and model-management nodes are not installed.
"""

from importlib import import_module

DaSiWa_AdvancedLoRALoader = import_module(
    ".nodes_advanced_lora_loader", __package__
).DaSiWa_AdvancedLoRALoader

NODE_CLASS_MAPPINGS = {"DaSiWa_LTX2LoraLoader": DaSiWa_AdvancedLoRALoader}
NODE_DISPLAY_NAME_MAPPINGS = {"DaSiWa_LTX2LoraLoader": "Advanced LoRA Loader"}
