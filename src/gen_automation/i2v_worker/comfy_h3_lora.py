"""H3 low-rank updates without merging/requantizing the INT8 ConvRot base.

Comfy-only dependencies are imported at execution. One injection owns the whole
selected stack: chaining independent forward hooks is unsafe on ejection, and
the native bypass helper replaces earlier adapters sharing the same target.
"""

import hashlib
import logging
import math
from dataclasses import dataclass
from typing import Any

_ATTACHMENT = "managed_h3_lora_stack"
_INJECTION_PREFIX = "managed_h3_lora:"
_TOKEN_CHUNK = 1024


@dataclass(frozen=True)
class _Update:
    up: Any
    down: Any
    scale: float


class _LayerHook:
    def __init__(self, module: Any, updates: tuple[_Update, ...], *, mlp: bool) -> None:
        self.module = module
        self.updates = updates
        self.mlp = mlp
        self.original: Any = None
        self.had_forward = False
        self.runtime: tuple[_Update, ...] = ()

    def inject(self, _patcher: Any) -> None:
        if self.original is not None:
            raise RuntimeError("H3 LoRA hook already active")
        self.had_forward = "forward" in self.module.__dict__
        self.original = self.module.forward
        self.module.forward = self.forward

    def eject(self, _patcher: Any) -> None:
        if self.original is None:
            return
        if self.had_forward:
            self.module.forward = self.original
        else:
            del self.module.forward
        self.original = None
        # Release GPU copies between model unloads/jobs, retaining only CPU data.
        self.runtime = ()

    def add(self, output: Any, x: Any, input_act: Any = None) -> Any:
        import torch.nn.functional as functional  # type: ignore[import-not-found]

        if not self.runtime or (
            self.runtime[0].down.device != x.device or self.runtime[0].down.dtype != x.dtype
        ):
            self.runtime = tuple(
                _Update(
                    update.up.to(device=x.device, dtype=x.dtype),
                    update.down.to(device=x.device, dtype=x.dtype),
                    update.scale,
                )
                for update in self.updates
            )
        flat_x = x.reshape(-1, x.shape[-1])
        # Keep one projection-sized chunk, not several full sequence outputs.
        output = output.contiguous()
        flat_out = output.view(-1, output.shape[-1])
        for start in range(0, flat_x.shape[0], _TOKEN_CHUNK):
            piece = flat_x[start : start + _TOKEN_CHUNK]
            if input_act is not None:
                piece = input_act(piece)
            accumulated = flat_out[start : start + _TOKEN_CHUNK].float().clone()
            for update in self.runtime:
                delta = functional.linear(functional.linear(piece, update.down), update.up)
                accumulated.add_(delta, alpha=update.scale)
            flat_out[start : start + _TOKEN_CHUNK].copy_(accumulated)
        return output

    def forward(self, x: Any, *args: Any, **kwargs: Any) -> Any:
        if self.mlp:
            import comfy.ops  # type: ignore[import-not-found]

            # Pinned H3 MLP calls linear_input_act directly; fc2.forward hooks
            # never run on INT8. Preserve that fused base path, then add the
            # fc2 adapters using the same (unrotated) SwiGLU input.
            hidden = self.module.fc1(x)
            output = comfy.ops.linear_input_act(self.module.fc2, hidden, "swiglu")
            return self.add(output, hidden, comfy.ops.INPUT_ACT_EAGER["swiglu"])
        return self.add(self.original(x, *args, **kwargs), x)


def apply_h3_lora(model: Any, lora: dict[str, Any], strength: float, identity: str) -> Any:
    """Validate every adapter, clone, then register an additive whole-stack hook.

    No base tensors or module tree are changed here. Comfy's patcher owns injection
    and ejection. Actual strengths/alpha are preserved, including negative values.
    """
    import comfy.lora  # type: ignore[import-not-found]
    import comfy.lora_convert  # type: ignore[import-not-found]
    import comfy.ops
    import torch
    from comfy.ldm.minimax.model import MLP  # type: ignore[import-not-found]
    from comfy.patcher_extension import PatcherInjection  # type: ignore[import-not-found]
    from comfy.weight_adapter import LoRAAdapter  # type: ignore[import-not-found]

    if not math.isfinite(strength):
        raise ValueError("H3 LoRA strength must be finite")
    if strength == 0:
        return model
    with model.use_ejected():
        state = model.model_state_dict()
        key_map = {}
        for key in state:
            if key.startswith("diffusion_model.") and key.endswith(".weight"):
                bare = key.removeprefix("diffusion_model.").removesuffix(".weight")
                for alias in (
                    bare,
                    "diffusion_model." + bare,
                    "lora_unet_" + bare.replace(".", "_"),
                ):
                    key_map[alias] = key
        lora = comfy.lora_convert.convert_lora(lora)
        loaded = comfy.lora.load_lora(lora, key_map, log_missing=False)
        if not loaded:
            raise ValueError("H3 LoRA has no compatible model weights; generation was not started")
        previous = model.get_attachment(_ATTACHMENT)
        stack = dict(previous[0]) if previous else {}
        covered: set[str] = set()
        for key, adapter in loaded.items():
            if not isinstance(adapter, LoRAAdapter):
                raise ValueError("H3 requires ordinary low-rank adapters; unsupported patch type")
            up, down, alpha, mid, dora, reshape = adapter.weights
            if any(value is not None for value in (mid, dora, reshape)):
                raise ValueError("H3 LoRA contains unsupported reshape, mid-layer or DoRA weights")
            if (
                up.ndim != 2
                or down.ndim != 2
                or down.shape[0] == 0
                or up.shape[1] != down.shape[0]
                or (up.shape[0], down.shape[1]) != tuple(state[key].shape)
            ):
                raise ValueError("H3 LoRA tensor shape does not match the selected model")
            scale = strength * (float(alpha) / down.shape[0] if alpha is not None else 1.0)
            if (
                not math.isfinite(scale)
                or not torch.isfinite(up).all()
                or not torch.isfinite(down).all()
            ):
                raise ValueError("H3 LoRA contains non-finite weights or alpha")
            # Detached CPU copies belong to this immutable stack, not hook state.
            update = _Update(up.detach().to("cpu"), down.detach().to("cpu"), scale)
            stack[key] = (*stack.get(key, ()), update)
            covered.update(adapter.loaded_keys)
        if set(lora) - covered:
            raise ValueError(
                "H3 LoRA contains unmatched tensors; partial application is not allowed"
            )

        hooks = []
        for key, updates in stack.items():
            path = key.removesuffix(".weight")
            module = model.model.get_submodule(path)
            mlp = path.endswith(".mlp.fc2")
            if mlp:
                module = model.model.get_submodule(path.removesuffix(".fc2"))
                if type(module) is not MLP or "forward" in module.__dict__:
                    raise ValueError("H3 fused MLP adapter requires the reviewed native MLP")
            elif not (
                isinstance(module, (torch.nn.Linear, comfy.ops.CastWeightBiasOp))
                and hasattr(module, "in_features")
                and hasattr(module, "out_features")
            ):
                raise ValueError("H3 LoRA target is not a supported linear layer")
            hooks.append(_LayerHook(module, updates, mlp=mlp))

        def inject(patcher: Any) -> None:
            try:
                for hook in hooks:
                    hook.inject(patcher)
            except Exception:
                for hook in reversed(hooks):
                    hook.eject(patcher)
                raise

        def eject(patcher: Any) -> None:
            for hook in reversed(hooks):
                hook.eject(patcher)

        clone = model.clone()
        chain = (*previous[1], (identity, strength)) if previous else ((identity, strength),)
        # The pinned patcher compares injection KEYS, not their contents, when
        # deciding whether cached clones have the same weights. Vary this key
        # with the complete ordered stack to force clean cross-job switching.
        signature = hashlib.sha256(repr(chain).encode()).hexdigest()
        for key in list(clone.injections):
            if key.startswith(_INJECTION_PREFIX):
                clone.remove_injections(key)
        clone.set_attachments(_ATTACHMENT, (stack, chain))
        clone.set_injections(_INJECTION_PREFIX + signature, [PatcherInjection(inject, eject)])
    logging.info(
        "Managed H3 additive LoRA: selections=%d targets=%d fused_mlp=%d",
        len(chain),
        len(hooks),
        sum(hook.mlp for hook in hooks),
    )
    return clone
