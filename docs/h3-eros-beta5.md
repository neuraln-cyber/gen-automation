# Eros Beta 5 deployment contract

Active recipe: `eros_beta5`, native single-image REF2VA with an optional frame-zero guide. This is a reviewed
integration of the creator's checkpoint/settings with native Comfy reference
conditioning, not a claim that an Eros-authored workflow JSON was reproduced.

## Model and runtime

- [Creator model/version](https://civitai.red/models/2851079/h3-eros-max?modelVersionId=3294059):
  Beta 5 TURBO Hybrid INT8, file3178732. The version's default W4A8 download is
  **not** this checkpoint. Turbo is fused; no extra Turbo adapter is installed.
- [Creator HF source](https://huggingface.co/TenStrip/10Eros-Max/tree/8a198588c8870ab0d613b3492a3150d091c8c2dd):
  `10Eros_Max_h3_TURBO-hybrid_beta5_int8.safetensors`, 20,970,414,464 bytes,
  SHA256 `4dd965496e5b1b83cd13c65cbe7a535b8a4d94ae768a7646b4e336d52c4781cf`.
- Native INT8 ConvRot quantization; Qwen3-VL 32B NVFP4 AWQ encoder, native H3
  INT8 video VAE and FP32 audio VAE. Complete identities in `h3_variants.py`.
- Comfy0.37.0 at73c9bad4d21e7addbe1d13bc92eee0f1431b017d,
  frontend1.53.6, Torch2.9.1/CUDA13, Kitchen0.2.35, AIMDO0.5.5.
  Native operation requires no DaSiWa/Noda/KJ/SolAttn loader or attention cache.
  H3 imports only the reviewed upscaler and our export synchronization node.
  Shared legacy WAN components are not enabled/imported for H3.

## Workflow and defaults

[Pinned official native REF2VA template](https://github.com/Comfy-Org/workflow_templates/blob/e7cd011d4ded3411c2f481200544f0be6fdc962e/templates/video_minimax_h3_r2v.json)
defines reference image/VAE/CLIP conditioning and joint video/audio sampling.
The [creator's clarification](https://huggingface.co/TenStrip/10Eros-Max/discussions/50)
recommends reference-style prompting even for image-to-video. Prompt with
`<Picture 1>`; there is no hidden prompt-builder rewrite.

The dashboard's **Animate this image** mode combines this reference conditioning
with native `MiniMaxH3AddGuide` at frame zero. The reference remains original-size;
a separate guide contains the whole source fitted to the base canvas with edge
padding, avoiding AddGuide's implicit center crop. It is a VAE conditioning guide,
not a pasted first frame or a guarantee of unchanged style in later frames.
The optional 2D direction button inserts visible, editable positive-prompt text
only when clicked. Neither this prompt preset nor the combined conditioning mode
is claimed as an Eros creator-certified recipe or a proven visual-quality fix.

Historical snapshots without `h3_image_mode` remain `reference` (no forced first
frame). Saved drafts/presets retain that behavior; choose `first_frame` explicitly
to opt in. Fresh forms default to the guide only after the operator enables
`GEN_AUTOMATION_I2V_H3_FIRST_FRAME_ENABLED` on the matching worker cutover. The API rejects the
new mode until then, keeping control-plane-first deployments safe.

- Creator sampling recipe: RES Multistep/simple, 6–9 steps on Civitai (6–8 on HF).
  Default8steps, CFG1, denoise1. Keep native video12/audio3 shifts; these are native
  model defaults, not independently published Eros shift recommendations.
- The other suggested `er_sde/beta57` combination is not installed: `beta57` is
  not silently replaced by native `beta`. All installed choices remain editable.
- 24fps,17n+5frames,default124. Standard auto-aspect uses our 5090 budget of
  768-short-edge/1.03MP,32-grid,max2048. This is an application resource preset,
  not a creator-required resolution. Original reference pixels are passed to
  native `ref_image_size=match` without FL2VA padding or prior downsampling.
- Native `LoraLoaderModelOnly` forwards metadata and original ordered strengths.
  The same patched model is reused for generation/refinement; no double loading,
  implicit multiplier, extra Turbo or CLIP adapter. Creator suggests modest
  extra concept-LoRA strengths, generally0.2–0.6; user strengths are not changed.
- Defaults/presets/job variants are isolated from ConvRot and DaSiWa. Unknown
  checkpoint hashes fail closed. Historical jobs/outputs remain readable and
  cannot silently retry against Eros. Only one diffusion model is in its manifest.

## Optional upscaling

Off by default. Match-original-size uses the pinned H3 latent upscaler and
[community UltimateUpscale](https://github.com/bbaudio-2025/Comfyui-MMH3-UltimateUpscale/tree/fe6658f6d144066f14150d3526247b417683ff2b).
Starting preset4steps/CFG1/denoise0.2/simple, same sampler as generation. This is
an engineering starting point, **not an Eros creator-certified upscale preset**.
Reference conditioning retains its own native dimensions across temporal/spatial
tiles; in first-frame mode, the guide is also carried into upstream keyframe
resize/crop/temporal handling. Audio is frozen/preserved. No FILM/interpolation,
sharpening postfilter or hidden strength changes. Before/after diagnostics
remain optional. It cannot guarantee recovering original image detail.

Explicit refinement counts, including one step, survive controller-to-worker
serialization unchanged. The controller must not omit `1` for Eros, whose missing
value defaults to `4`. The dashboard shows the actual selected refinement count;
Restore model sampling defaults restores four without changing prompts or LoRAs.
Outputs identify the Eros workflow, conditioning mode and effective refine count.

## Verification and safe cutover

`verify_h3_eros_workflow.py` checks actual pinned native dynamic input schemas,
reference AV conditioning, metadata-aware native LoRA arithmetic, common model
routing and real upstream tile/reference packing on CPU. The mandatory image
build imports execute as runtime UID/GID10002. These do **not** prove GPU memory
sufficiency or visual quality; only an owner-generated comparison can do that.

Model delivery remains immutable/version-bound through signed CloudFront; no
public bucket or alternate download route. Prior model objects are private
rollback backups, excluded from the active manifest. Hold remains through
controller restarts and guarded image activation; no synthetic job is created.
