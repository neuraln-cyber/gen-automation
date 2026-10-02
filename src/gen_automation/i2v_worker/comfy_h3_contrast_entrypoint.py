"""Narrow entrypoint for pinned Tr1dae/ComfyUI-MiniMaxH3_LatentUpscaler.

Installed as that package's __init__.py, not imported as a worker module.
Keep the upstream contrast class/math unchanged. Do not register its unrelated
upscalers, checkpoint downloads, stash HTTP routes or browser extensions.
"""

from importlib import import_module

MiniMaxH3LatentContrast = import_module(".nodes", __package__).MiniMaxH3LatentContrast

NODE_CLASS_MAPPINGS = {"MiniMaxH3LatentContrast": MiniMaxH3LatentContrast}
NODE_DISPLAY_NAME_MAPPINGS = {"MiniMaxH3LatentContrast": "MiniMax H3 Latent Contrast"}
