"""CPU numerical/lifecycle contract against the worker's actual pinned ComfyUI.

Uses tiny synthetic tensors only: no models, media, generation or GPU. Executed
in the immutable worker build so an incompatible native change fails publication.
"""

import json
import os
import sys
from types import SimpleNamespace


def verify_h3_lora_math() -> None:
    import comfy.lora
    import comfy.ops
    import torch
    import torch.nn.functional as functional
    from comfy.ldm.minimax.model import MLP
    from comfy.model_patcher import LowVramPatch, ModelPatcher
    from comfy.quant_ops import QuantizedTensor

    from gen_automation.i2v_worker.comfy_h3_lora import _TOKEN_CHUNK, apply_h3_lora

    torch.manual_seed(44)
    torch.set_num_threads(2)
    cpu = torch.device("cpu")
    ops = comfy.ops.mixed_precision_ops(compute_dtype=torch.float32)

    def quantized_linear(layer):
        weight = torch.randn(layer.out_features, layer.in_features) * 0.03
        quant = QuantizedTensor.from_float(
            weight.to(torch.bfloat16),
            "TensorWiseINT8Layout",
            is_weight=True,
            per_channel=True,
            convrot=True,
            convrot_groupsize=256,
        ).to(torch.float32)
        layer.weight = torch.nn.Parameter(quant, requires_grad=False)
        layer.quant_format = "int8_tensorwise"
        layer.layout_type = "TensorWiseINT8Layout"

    root = torch.nn.Module()
    root.diffusion_model = torch.nn.Module()
    root.diffusion_model.proj = ops.Linear(256, 256, bias=False)
    root.diffusion_model.plain = ops.Linear(256, 256, bias=False)
    root.diffusion_model.mlp = MLP(256, 256, dtype=torch.float32, operations=ops)
    for name, layer in root.diffusion_model.named_modules():
        if hasattr(layer, "in_features") and hasattr(layer, "out_features"):
            if name == "plain":
                layer.weight = torch.nn.Parameter(torch.randn(256, 256) * 0.03, requires_grad=False)
            else:
                quantized_linear(layer)
    root.model_config = SimpleNamespace(unet_config={})
    source = ModelPatcher(root, cpu, cpu)
    x = torch.randn(_TOKEN_CHUNK + 3, 256)
    weights_before = {name: value.detach().clone() for name, value in root.state_dict().items()}

    targets = ("proj", "plain", "mlp.fc1", "mlp.fc2")
    matrices = {}
    kohya, native = {}, {}
    for name in targets:
        layer = root.diffusion_model.get_submodule(name)
        a = torch.randn(16, layer.in_features) * 0.03
        b = torch.randn(layer.out_features, 16) * 0.03
        matrices[name] = (b, a)
        alias = "lora_unet_" + name.replace(".", "_")
        kohya[alias + ".lora_down.weight"] = a
        kohya[alias + ".lora_up.weight"] = b
        kohya[alias + ".alpha"] = torch.tensor(8.0)
        native["diffusion_model." + name + ".lora_A.weight"] = a * 0.25
        native["diffusion_model." + name + ".lora_B.weight"] = b

    base_proj = root.diffusion_model.proj(x)
    base_mlp = root.diffusion_model.mlp(x)
    first = apply_h3_lora(source, kohya, 0.8, "first")
    stacked = apply_h3_lora(first, native, -0.3, "second")
    third = apply_h3_lora(stacked, kohya, 1.0, "third")
    changed = apply_h3_lora(source, kohya, 0.2, "first")
    assert not source.patches and not first.patches and not stacked.patches
    assert not source.injections and len(stacked.injections) == 1
    assert not first.clone_has_same_weights(changed)
    assert not first.clone_has_same_weights(stacked)
    assert apply_h3_lora(source, {}, 0, "ignored") is source

    def update(tensor, name, coefficient):
        b, a = matrices[name]
        return functional.linear(functional.linear(tensor, a), b) * coefficient

    max_error = 0.0
    # Switch stack/base/changed selections repeatedly: no lingering hooks/delta.
    with torch.inference_mode():
        for selected, coefficient in (
            (first, 0.4),
            (stacked, 0.325),
            (third, 0.825),
            (changed, 0.1),
            (first, 0.4),
        ):
            selected.inject_model()
            for name in ("proj", "plain", "mlp.fc1"):
                layer = root.diffusion_model.get_submodule(name)
                # Baseline uses the same quantized execution as production.
                selected.eject_model()
                expected = layer(x) + update(x, name, coefficient)
                selected.inject_model()
                actual = layer(x)
                torch.testing.assert_close(actual, expected, rtol=2e-5, atol=2e-6)
                max_error = max(max_error, (actual - expected).abs().max().item())
            # fc1 was independently checked above. Use its exact output for
            # the fused reference: tiny floating addition-order differences
            # can cross an INT8 activation-rounding boundary in fc2.
            hidden = root.diffusion_model.mlp.fc1(x)
            expected = comfy.ops.linear_input_act(root.diffusion_model.mlp.fc2, hidden, "swiglu")
            expected += update(comfy.ops.INPUT_ACT_EAGER["swiglu"](hidden), "mlp.fc2", coefficient)
            torch.testing.assert_close(root.diffusion_model.mlp(x), expected, rtol=2e-5, atol=2e-6)
            selected.eject_model()
            assert all("forward" not in layer.__dict__ for layer in root.diffusion_model.modules())
            torch.testing.assert_close(root.diffusion_model.proj(x), base_proj, rtol=0, atol=0)
            torch.testing.assert_close(root.diffusion_model.mlp(x), base_mlp, rtol=0, atol=0)

    for name, value in root.state_dict().items():
        torch.testing.assert_close(value, weights_before[name], rtol=0, atol=0)

    for bad in (
        {"unknown.lora_A.weight": torch.ones(1, 1)},
        {**kohya, "unknown.weight": torch.ones(1)},
        {**kohya, "lora_unet_proj.alpha": torch.tensor(float("nan"))},
        {**kohya, "lora_unet_proj.lora_up.weight": torch.ones(128, 16)},
    ):
        try:
            apply_h3_lora(source, bad, 1.0, "invalid")
        except ValueError:
            pass
        else:
            raise AssertionError("Invalid/partial adapter was accepted")

    # Negative control: actual old resident AND low-VRAM patching requantize the
    # base and introduce error beyond a small intended low-rank update.
    q = root.diffusion_model.proj.weight
    original = q.dequantize().float()
    a, b = torch.randn(16, 256) * 0.003, torch.randn(256, 16) * 0.003
    small = {"diffusion_model.proj.lora_A.weight": a, "diffusion_model.proj.lora_B.weight": b}
    loaded = comfy.lora.load_lora(small, {"diffusion_model.proj": "diffusion_model.proj.weight"})
    old = source.clone()
    old.add_patches(loaded, 1.0)
    key = "diffusion_model.proj.weight"
    resident = (
        old.patch_weight_to_device(key, device_to=cpu, return_weight=True).dequantize().float()
    )
    streamed = LowVramPatch(key, old.patches)(original.clone())
    streamed = (
        q.requantize_from_float(streamed, scale="recalculate", stochastic_rounding=42)
        .dequantize()
        .float()
    )
    expected_weight = original + b @ a
    delta_rms = (b @ a).square().mean().sqrt().item()
    errors = [(out - expected_weight).square().mean().sqrt().item() for out in (resident, streamed)]
    assert all(error > delta_rms for error in errors), (delta_rms, errors)

    # Exercise the deployed BF16 compute dtype as well as the tight FP32
    # reference above. Accumulate selected updates in float32 and round once.
    root.to(dtype=torch.bfloat16)
    bf_x = x.to(torch.bfloat16)

    def bf_update(base, value, name):
        up, down = matrices[name]
        first_delta = functional.linear(
            functional.linear(value, down.to(torch.bfloat16)), up.to(torch.bfloat16)
        )
        second_delta = functional.linear(
            functional.linear(value, (down * 0.25).to(torch.bfloat16)), up.to(torch.bfloat16)
        )
        return (
            base.float()
            .add(first_delta, alpha=0.4)
            .add(second_delta, alpha=-0.3)
            .to(torch.bfloat16)
        )

    with torch.inference_mode():
        bf_base = root.diffusion_model.proj(bf_x)
        bf_expected = bf_update(bf_base, bf_x, "proj")
        stacked.inject_model()
        torch.testing.assert_close(root.diffusion_model.proj(bf_x), bf_expected, rtol=0, atol=0)
        hidden = root.diffusion_model.mlp.fc1(bf_x)
        bf_expected = bf_update(
            comfy.ops.linear_input_act(root.diffusion_model.mlp.fc2, hidden, "swiglu"),
            comfy.ops.INPUT_ACT_EAGER["swiglu"](hidden),
            "mlp.fc2",
        )
        torch.testing.assert_close(root.diffusion_model.mlp(bf_x), bf_expected, rtol=0, atol=0)
        stacked.eject_model()
        torch.testing.assert_close(root.diffusion_model.proj(bf_x), bf_base, rtol=0, atol=0)
    print(
        json.dumps(
            {
                "h3_lora_math": "passed",
                "max_additive_error": max_error,
                "resident_merge_error": errors[0],
                "streamed_merge_error": errors[1],
                "delta_rms": delta_rms,
                "base_weights_unchanged": True,
                "fused_fc2_verified": True,
            }
        )
    )


if __name__ == "__main__":
    sys.argv = ["h3-lora-math", "--cpu"]
    sys.path.insert(0, os.environ.get("H3_COMFY_ROOT", "/opt/comfyui"))
    import comfy.options

    comfy.options.enable_args_parsing()
    verify_h3_lora_math()
