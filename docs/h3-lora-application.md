# H3 LoRA application

The managed loader applies ordinary linear LoRAs as additive forward updates:
`base(x) + strength * (alpha / rank) * up(down(x))`. Missing alpha means scale 1.
The original INT8 ConvRot weights, scales and rotation metadata are never changed.
Both native A/B and Kohya up/down naming are supported. Every tensor must match;
unsupported adapter variants fail explicitly rather than partially applying.

## Why this differs from the stock merge loader

The previous loader delegated to `LoraLoaderModelOnly`. On our pinned ComfyUI and
comfy-kitchen, both resident and streamed weight-patching paths can requantize a
merged update into INT8. Synthetic CPU tests reproduce rounding error larger than
a small intended update. The older lost-ConvRot-parameters bug is already fixed in
our dependencies; this change must not be described as repairing that bug.

The owner's base/final video comparisons isolate degradation to LoRA-enabled
runs, before upscaling. Eliminating lossy merging addresses a demonstrated
application-path weakness, but only a subsequent owner-run comparison can prove
that it resolves the reported visual defect. No visual-quality guarantee is
inferred from synthetic tests. No generation is submitted by these tests.

## Implementation constraints

- One immutable stack and one lifecycle injection preserve all selected adapters.
  A stack-specific injection key prevents Comfy clone caching from conflating
  different selections or strengths. Ejection releases runtime tensor copies and
  restores the original forwards; no-LoRA runs use the unchanged native path.
- H3's INT8 MLP invokes `linear_input_act` without calling `fc2.forward`. A hook on
  the native MLP preserves its fused base operation and applies the fc2 update
  separately. A plain fc2 forward hook would silently omit these adapters.
- Updates and SwiGLU intermediates are token-chunked to bound temporary memory.
  Selected deltas accumulate in float32 before one output cast. No full model
  dequantization, strength adjustment, resource or workflow-setting change.
- `scripts/verify-h3-lora-math.py` runs inside the immutable worker build using
  its real pinned Comfy modules. It covers FP32/BF16, INT8 ConvRot/plain weights,
  alpha/rank, absent alpha, negative/zero weights, three-adapter stacks, fused
  fc2, clone switching, rejection of invalid/partial files, and unchanged base
  weights/output after ejection. The old resident/streamed paths are negative
  controls. This is a correctness check, not a GPU speed or VRAM benchmark.

References: [pinned Comfy patcher](https://github.com/Comfy-Org/ComfyUI/blob/c2bcbecd82ec5ae66594340b395c24ef0217b238/comfy/model_patcher.py),
[pinned H3 MLP](https://github.com/Comfy-Org/ComfyUI/blob/c2bcbecd82ec5ae66594340b395c24ef0217b238/comfy/ldm/minimax/model.py),
[H3 additive-loader precedent](https://github.com/Larryvrh/ComfyUI-MiniMax-H3-Turbo).
