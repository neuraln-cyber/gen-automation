"""Exercise the installed creator loader and pinned Comfy on synthetic CPU data.

No media, model downloads, GPU work or generation prompts. This checks the
integration contract; it cannot establish end-to-end visual quality.
"""

import gc
import importlib
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


def verify_creator_lora(nodes) -> None:
    import comfy.sd
    import folder_paths
    import torch
    from comfy.model_patcher import ModelPatcher
    from safetensors.torch import save_file

    cls = nodes.NODE_CLASS_MAPPINGS["DaSiWa_LTX2LoraLoader"]
    upstream = importlib.import_module(cls.__module__)
    assert cls is upstream.DaSiWa_AdvancedLoRALoader
    assert cls.RETURN_TYPES == ("MODEL", "CLIP") and cls.FUNCTION == "apply_stack"
    assert upstream._load_lora is comfy.sd.load_lora_for_models
    assert "ManagedH3LoraLoader" not in nodes.NODE_CLASS_MAPPINGS

    cpu = torch.device("cpu")
    root = torch.nn.Module()
    root.diffusion_model = torch.nn.Module()
    root.diffusion_model.proj = torch.nn.Linear(8, 8, bias=False)
    root.model_config = SimpleNamespace(unet_config={})
    source = ModelPatcher(root, cpu, cpu)

    class TinyClip:
        # Minimal CLIP facade; uses real ModelPatcher and native LoRA key mapping.
        def __init__(self, patcher):
            self.patcher = patcher
            self.cond_stage_model = patcher.model

        def clone(self):
            return TinyClip(self.patcher.clone())

        def add_patches(self, weights, strength):
            return self.patcher.add_patches(weights, strength)

    text = torch.nn.Module()
    text.proj = torch.nn.Linear(8, 8, bias=False)
    clip = TinyClip(ModelPatcher(text, cpu, cpu))
    before = root.diffusion_model.proj.weight.detach().clone()
    text_before = text.proj.weight.detach().clone()
    loader = cls()
    assert loader.apply_stack(source, clip, "[]", "Basic", False) == (source, clip)
    with tempfile.TemporaryDirectory(prefix="h3-creator-contract-") as directory:
        paths, stack, metadata, deltas = {}, [], [], []
        for index, strength in enumerate((0.65, -0.2, 1.0)):
            name = f"managed-h3/{index:064x}.safetensors"
            path = Path(directory) / f"{index}.safetensors"
            up = torch.full((8, 2), (index + 1) * 0.02)
            down = torch.full((2, 8), 0.03)
            weights = {
                "diffusion_model.proj.lora_A.weight": down,
                "diffusion_model.proj.lora_B.weight": up,
                "text_encoders.proj.lora_A.weight": down.clone(),
                "text_encoders.proj.lora_B.weight": up.clone(),
            }
            md = {"contract_adapter": str(index)}
            save_file(weights, str(path), metadata=md)
            paths[name] = str(path)
            metadata.append(md)
            deltas.append((up @ down) * strength)
            stack.append({"on": True, "lora": name, "str": strength, "vs": 1, "as": 1})
        with (
            patch.object(folder_paths, "get_full_path", side_effect=lambda kind, name: paths[name]),
            patch.object(upstream, "_load_lora", wraps=upstream._load_lora) as native,
        ):
            selected, selected_clip = loader.apply_stack(
                source, clip, json.dumps(stack), "Basic", False
            )
            assert native.call_count == 3
            for call, row, md in zip(native.call_args_list, stack, metadata, strict=True):
                assert call.args[3:] == (row["str"], row["str"])
                assert call.kwargs == {"lora_metadata": md}
            assert native.call_args_list[0].args[:2] == (source, clip)
            assert selected is not source and selected_clip is not clip
            assert len(selected.patches["diffusion_model.proj.weight"]) == 3
            assert len(selected_clip.patcher.patches["proj.weight"]) == 3
            actual = selected.patch_weight_to_device(
                "diffusion_model.proj.weight", device_to=cpu, return_weight=True
            )
            actual_text = selected_clip.patcher.patch_weight_to_device(
                "proj.weight", device_to=cpu, return_weight=True
            )
            torch.testing.assert_close(actual, before + sum(deltas))
            torch.testing.assert_close(actual_text, text_before + sum(deltas))
            assert not source.patches and not clip.patcher.patches
            assert not upstream._LORA_FILE_CACHE
            native.reset_mock()
            zero = [{**stack[0], "str": 0}, {**stack[1], "on": False}]
            assert loader.apply_stack(source, clip, json.dumps(zero), "Basic", False) == (
                source,
                clip,
            )
            native.assert_not_called()
            assert loader.apply_stack(source, clip, "[]", "Basic", False) == (source, clip)
        # Release safetensors mappings before removing the temporary fixtures
        # (Windows, unlike the Linux worker, forbids unlinking open mappings).
        del call, selected, selected_clip
        gc.collect()
    torch.testing.assert_close(root.diffusion_model.proj.weight, before, rtol=0, atol=0)
    torch.testing.assert_close(text.proj.weight, text_before, rtol=0, atol=0)
    print(
        "Creator LoRA contract passed: native MODEL/CLIP, metadata, ordered strengths, clean base."
    )
