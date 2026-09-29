# H3 author environment rebuild (C-MMH3 v2.3)

This replaces the earlier adapted H3 runtime. It is not evidence that visual
quality has recovered; that requires an owner comparison on the deployed worker.

## Immutable baseline

Reference: `darksidewalker/dasiwa-comfyui-workflows` at
`39b3427e6a67ff2e2c98fb5afabc7ce28117dec7`,
`C-MMH3/DaSiWa MiniMaxH3 MythicAlchemy C-MMH3-23.json`.
Original SHA256: `51a20a9e79fb9a505c0bada2e4828f39605738cd98aee327af7cd114f3015bef`.

- ComfyUI **0.37.0**, `73c9bad4d21e7addbe1d13bc92eee0f1431b017d`.
- Frontend **1.53.6**, as explicitly required by the author (the Core tag's
  requirements file still says 1.52.7; it is overridden deliberately).
- Kitchen **0.2.35**, AIMDO **0.5.5**, Torch **2.9.1**, CUDA **13.0**.
- Complete, unmodified DaSiWa pack **0.4.62** (meets 0.4.51+),
  `9f5aef4a2748bba9486dda0a7efec7689462d7e0`.
- Complete KJNodes **1.5.2**, `d3cfe21625e5170126ce06fbfcfe1d88108688c3`.
- Complete author-selected UltimateUpscale repository, **0.0.7**,
  `fe6658f6d144066f14150d3526247b417683ff2b`.
  The workflow contains stale/mixed per-node revisions: `26cf738...` exists but
  lacks its saved rows/columns spatial API; `3b4d972...` was not resolvable in the
  named upstream repository. The pinned public revision supplies that API. This
  is an explicit compatible dependency selection, not a claim of byte-identical
  historical dependencies.

The CUDA13 image enables Kitchen's supported CUDA kernels. H3 startup explicitly
requires `--use-ck-attention`; silently substituting PyTorch attention is forbidden.
The NAG package remains installed for legacy WAN, but its imports are excluded
from the H3 process. Only the three author packages plus the output-only base
diagnostic node are allowed in H3. No custom LoRA math or refinement wrapper remains.

## Models

Use the effective **outer subgraph widget** model selections, not the unused
inner loader defaults. The live checkpoint is now normal **Hybrid V2** (version
3314675), which the creator explicitly lists as REF2VA + FL2VA compatible; see
[normal V2 provenance/defaults](h3-normal-v2.md). Keep both VAEs/upscaler and
the author's linked
`qwen3vl_32b_minimax_h3_int4_convrot.safetensors` from
`Abiray/MiniMax-H3-GGUF`, revision `9fc3454d3ebe1be1bade862cd4a5011f325a22cb`.
14952506709 bytes; SHA256
`21fd2e2f06bc4fc422c6aa20893fe189edbbd9ab3068215f96e7a6cf2f6cb5bb`.
Same-named files in other repositories have different hashes and are not interchangeable.
The source manifest is public provenance only; changing it does not update the
live private binding. That requires a separate verified mirror/cutover receipt.

## Graph and boundary

UNET -> native ModelAttentionBackend (Kitchen) -> KJ ModelPatchTorchSettings
(fp16 accumulation) -> creator Director -> one creator Basic MODEL/CLIP LoRA
stack. The same stack supplies native SigmaShift/preview/BasicGuider, the base
scheduler **before** SigmaShift, DirectorGuide conditioning, and optional
UltimateUpscale. The creator SeedControl supplies the same NOISE to both passes.
LoRA file metadata, owner order/strengths and unit audio/video multipliers are
unchanged. Empty/zero stacks use the creator's pass-through, never a second path.

Optional latent upscale uses the saved v2.3 settings: simple / 1 step / 0.2
denoise, 85-frame chunks with 17-frame overlap and 0.999 anchor, 2x3 tiles,
128px overlaps, 64px fades, later/linear blend, 0.05 masked noise, brightness
match and widening fade. Its implementation and anchor handling are upstream.

Headless boundaries are explicit, not hidden claims of exact UI reproduction:

- The owner input, prompt, seed, dimensions, frame count, sampler/steps/
  shifts and optional source-size selection remain job parameters.
- The app preserves its existing bounded base canvas and source-padding/cropping
  delivery contract; the author's Director receives the resolved canvas.
- The owner switched to **I2VA**, the single-image mode of the FL2VA family.
  Director's `fl2va_model` branch calls native `MiniMaxH3ImageToVideo` with the
  uploaded image at frame zero and no last frame. This deliberately supersedes
  the saved workflow's REF2VA selection, using the creator's mode implementation.
  Native CLIP receives that image and VAE encodes its first-frame keyframe.
- Scene/motion text is bound to the actual creator structured builder's `imd`
  field. Upstream adds the I2VA alignment sentence and official multimodal,
  soundscape and music sections; no custom prompt builder or external overwrite.
  No soundscape is invented and music remains the creator's `N/A` default.
  Existing stored prompts/settings are not rewritten.
- SaveVideo/CreateVideo remain the delivery adapter (MP4/SAR/audio contract).
  UI watermark, optional Forge LLM, RTX SDK, interpolation and
  optional pixel upscalers are not activated or given new models. Complete node
  sources are installed; unused lazy optional runtimes are not installed.
- Preview uses the actual KJ node without a separate tiny-VAE download and with
  one preview frame, rather than the saved UI's 120-frame animated TAE preview.
  This affects preview transport, not the output decoder or sampling graph.
- The existing base diagnostic only adds an ordered decode after refinement.

## Gates

Unit graph/manifest/isolation checks plus real full-package imports, creator/native
LoRA integration, Director/Guide/seed/API checks in the Linux image as UID/GID10002.
The I2VA check executes real native conditioning/AV-latent creation with only
CLIP/VAE inference substituted. It verifies exactly one first-frame anchor,
no REF2VA call, structured prompt alignment, and upstream resize/crop/re-anchor
of the first-frame conditioning through both temporal chunks and all spatial tiles.
CPU checks are not GPU kernel, memory-sufficiency, performance or visual tests.
Require full CI, exact publication identity, verified new encoder mirror and a
fresh safe idle check before cutover. Never replay previous deployment helpers.
No new jobs, cancellations, retries or image-lane changes. A fresh idle boundary
and owner-approved deployment are required before replacing a running worker.
