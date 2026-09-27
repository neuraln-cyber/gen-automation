# H3 creator LoRA integration

The worker uses **DaSiWa's actual Advanced LoRA Loader**, serialized as
`DaSiWa_LTX2LoraLoader` in the creator's C-MMH3 v2.5 workflow. The former
`ManagedH3LoraLoader`, additive forward hooks, fused-MLP overrides and custom
LoRA arithmetic have been deleted. There is no alternate application path.

## Pinned upstream implementation

- Repository: <https://github.com/darksidewalker/ComfyUI-DaSiWa-Nodes>
- Revision: `9f5aef4a2748bba9486dda0a7efec7689462d7e0`
- Unmodified `nodes/nodes_advanced_lora_loader.py` SHA-256:
  `26954c67c71a547226fdc523566783a33d03ffb2e667fb1d5930386d4cefd2cf`.
- The upstream logging helper and GPL-3.0 license are also checksum-pinned.
- `comfy_dasiwa_node.py` only registers the upstream class under the creator's
  exact node name. It does not subclass it, patch it or implement LoRA loading.
  Unrelated upstream UI/LLM/model-management nodes are not installed.

## Creator workflow contract

Reference: [C-MMH3 v2.5](https://civitai.red/models/2831978/dasiwa-minimax-h3-workflows-or-t2va-or-fl2va-or-ref2va),
version 3195699/file 3249252. The author's
[workflow snapshot](https://github.com/darksidewalker/dasiwa-comfyui-workflows/blob/39b3427e6a67ff2e2c98fb5afabc7ce28117dec7/C-MMH3/DaSiWa%20MiniMaxH3%20MythicAlchemy%20C-MMH3-25.json)
is byte-identical (SHA-256 `5eefef133d4e4ecff0a342d94bc9289f7ac58ea84aa88310df89f94dcc7d5bf1`).

The renderer reproduces node 2678's LoRA contract:

- One ordered stack with `model_type="Basic"`, `use_cache=false`.
- Each selected nonzero adapter appears exactly once with the owner's strength;
  video/audio multipliers remain 1. No LTX branch splitting or hidden scaling.
- Native UNET MODEL and CLIP enter the stack. Its MODEL output feeds SigmaShift,
  then base sampling and refinement. Its CLIP output feeds H3 conditioning.
- The creator reads weights **and metadata** and delegates unchanged to native
  Comfy `load_lora_for_models`. Native clone/patch lifecycle and supported adapter
  formats are retained; no custom quantization or forward injection.
- No-LoRA/all-zero runs keep their original MODEL and CLIP routes. Diagnostic
  output and refinement reuse the selected model without applying the stack again.

The dashboard, private artifact grants, hash-verified downloads, immutable
selection snapshots and original strengths are unchanged. Selected files are
materialized and verified before Comfy execution; missing/corrupt downloads fail
before invoking upstream (whose standalone missing-file behavior is a warning).
No new model files, GPU resources, delivery route or idle-policy change.

## Verification and limitation

Renderer tests cover stack order/strengths, both output connections, no-LoRA and
zero selection, base/refinement/diagnostic reuse, UI snapshots and private grants.
The immutable container build imports the exact upstream class and runs
`verify_h3_creator_lora.py` against real pinned Comfy with tiny synthetic CPU
adapters. It verifies native model/CLIP patches, metadata, negative/zero strengths,
three-adapter stacking and unchanged original model/CLIP after a new selection.

These are integration checks, **not proof that the reported blur is resolved**.
The previous additive implementation's numerical checks passed but the owner's
visual comparison still failed. Only a new owner-run comparison can establish
visual recovery. No generation is queued by the checks or deployment.
