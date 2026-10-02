# Eros Beta 5 deployment contract

Active recipe: `eros_beta5`, native single-image REF2VA with an optional frame-zero guide. This is a reviewed
integration of the creator's checkpoint/settings with native Comfy reference
conditioning, not a claim that an Eros-authored workflow JSON was reproduced.

## Author evidence reviewed 2026-10-01

- [TenStrip's direct workflow answer](https://civitai.red/models/2851079/h3-eros-max?dialog=commentThread&commentId=1347310&highlight=1347617):
  he uses the default ComfyUI templates, with comfy-kitchen attention, manual
  dimensions and input-image cropping for I2V instead of megapixel sizing. He
  says a workflow for a future FAST VSA version will be added later. That future
  version is not a dependency or instruction to change this Beta5 deployment.
- [Current Beta5 description](https://civitai.red/models/2851079/h3-eros-max?modelVersionId=3294059)
  explicitly favors `er_sde/beta57`, 4–6 steps, for style preservation/reduced
  drift. `res_multistep/simple`, 6–9, remains another supported motion recipe.
  Do not mix Beta2/Beta3/Beta4 historical settings into Beta5 defaults.
- [Author clarification on reference prompts](https://huggingface.co/TenStrip/10Eros-Max/discussions/50)
  recommends reference prompting including subject definitions even for I2V.
- Creator examples [142064411](https://civitai.red/images/142064411) and
  [142066607](https://civitai.red/images/142066607) identify Beta5/reference usage
  and respectively 6/8 steps. Their visible metadata does not provide a complete
  graph or sampler/scheduler, so it does not prove our graph is identical.
- The reviewed release/repository supplies no separate Eros-authored H3 JSON.
  The workflow found and used is the official Comfy REF2VA template linked below,
  with the author's explicitly documented checkpoint and sampling substitutions.
  Community reports of drawn-style drift are observations, not causal proof.

`GEN_AUTOMATION_I2V_H3_EROS_AUTHOR_RECIPE_ENABLED` remains false until the exact
capable worker is activated. Opening it selects **native REF2VA, er_sde/beta57,
6 steps, Comfy Kitchen attention** for fresh forms. Six is within the author's
4–6 range, not a claim he mandates six. Existing explicit settings, jobs and
drafts remain unchanged; missing historical settings still deserialize to the
old 8-step/simple/default-attention recipe. The API rejects new capabilities
before activation. No extra Turbo, model/LoRA inventory or strength change.

The actual scheduler uses native `BetaSamplingScheduler(alpha=0.5, beta=0.7)`:
the [RES4LYF beta57 implementation](https://github.com/ClownsharkBatwing/RES4LYF/blob/main/sigmas.py)
calls that same Comfy beta function with those exact parameters, not stock beta's
0.6/0.6. Partial-denoise schedules use the same integer total-step expansion and
tail slice via native `SplitSigmas`. Zero denoise remains an empty schedule.
Real pinned native numerical tests verify exact equality. No RES4LYF monkeypatch
or additional custom-node package is installed. Kitchen is an explicit native
`ModelAttentionBackend` selection after the LoRAs and before SigmaShift; the
worker rejects unavailable Kitchen attention instead of accepting its native
silent PyTorch fallback. CPU tests do not prove GPU execution or visual quality.

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
  H3 imports only the reviewed upscaler, output-only contrast and our export synchronization node.
  Shared legacy WAN components are not enabled/imported for H3.

## Workflow and defaults

[Pinned official native REF2VA template](https://github.com/Comfy-Org/workflow_templates/blob/e7cd011d4ded3411c2f481200544f0be6fdc962e/templates/video_minimax_h3_r2v.json)
defines reference image/VAE/CLIP conditioning and joint video/audio sampling.
The [creator's clarification](https://huggingface.co/TenStrip/10Eros-Max/discussions/50)
recommends reference-style prompting even for image-to-video. Prompt with
`<Picture 1>`; there is no hidden prompt-builder rewrite.

The dashboard's optional first-frame mode combines this reference conditioning
with native `MiniMaxH3AddGuide` at frame zero. The reference remains original-size;
a separate guide contains the whole source fitted to the base canvas with edge
padding, avoiding AddGuide's implicit center crop. It is a VAE conditioning guide,
not a pasted first frame or a guarantee of unchanged style in later frames.
The editable prompt helper uses the six-section
[full-reference prompt guide](https://huggingface.co/MiniMaxAI/MiniMax-H3/blob/main/docs/VIDEO_PROMPT_WRITING_GUIDE_ref_en.md)
recommended by the author: subject definitions, summary, retention analysis,
detailed description, soundscape and music. Style precedes the first shot in
`detailed_description`, not the base-mode `integrated_multimodal_description`.
It retains the entered motion text, does not inspect the image or invent details,
and never silently rewrites a prompt on submission. The owner must complete and
review the scaffold. Existing structured prompts are kept intact. This is not
an LLM enhancement or a claim that generic scaffold text guarantees fidelity.

Historical snapshots without `h3_image_mode` remain `reference` (no forced first
frame). Saved drafts/presets retain that behavior; choose `first_frame` explicitly
to opt in. The earlier first-frame release defaulted fresh forms to the guide after enabling
`GEN_AUTOMATION_I2V_H3_FIRST_FRAME_ENABLED` on the matching worker cutover. The API rejects the
new mode until then, keeping control-plane-first deployments safe.
The author-recipe gate supersedes that fresh-form default with reference-only;
the guide remains a clearly labelled optional native extension, not an Eros
creator-endorsed fix. No saved first-frame request is silently changed.

- Earlier sampling preset: RES Multistep/simple, 6–9 steps on Civitai (6–8 on HF).
  Legacy default8steps, CFG1, denoise1. Keep native video12/audio3 shifts; these are native
  model defaults, not independently published Eros shift recommendations.
- The author style preset exposes exact `er_sde/beta57` after guarded activation.
  All existing sampling controls remain editable, not clamped to the recipe.
- 24fps,17n+5frames,default124. Standard auto-aspect uses our 5090 budget of
  768-short-edge/1.03MP,32-grid,max2048. This is an application resource preset,
  not a creator-required resolution. Original reference pixels are passed to
  native `ref_image_size=match` without FL2VA padding or prior downsampling. The
  native node then scales reference conditioning to generation resolution; this
  does not mean the model attends to full original-resolution pixels.
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

## Optional output contrast (2026-10-03)

`h3_latent_contrast` defaults to **1.0**, an exact graph no-op; historical jobs,
drafts and presets remain neutral. The dashboard's **Eros latent contrast**
number control and **Reset contrast to 1.0** button preserve other settings.
The [Eros author's contrast advice](https://huggingface.co/TenStrip/10Eros-Max/discussions/47)
suggests **0.8–0.9 before video VAE decode** for the overcooked look. This is not
a pure saturation slider, blur/sharpen filter or guarantee of recovered detail.

The actual named node is `MiniMaxH3LatentContrast` from
[Tr1dae's implementation](https://github.com/Tr1dae/ComfyUI-MiniMaxH3_LatentUpscaler/blob/895e3c471164423f0ea0e8eaf45eb701efe641ae/nodes.py),
not the LBH upscaler code. Its unmodified contrast operation compresses/stretches
each video channel around its global mean and uses upstream's default
`preserve_norm=True`. The API accepts finite numbers 0–3 (upstream bounds), only
for Eros when non-neutral. Start conservatively; extremes can degrade output.

The node is inserted only on video-decode edges, after any refinement. The same
factor is used on an optional diagnostic base clip, so its upscale comparison
is not confounded by different contrast settings. Sampling, reference/guide,
LoRAs, source images and audio decode remain untouched. There is no additional
sampler execution. Effective contrast is stored in output metadata.

Only this upstream node is registered by a narrow entrypoint. Its unrelated
upscalers, checkpoint downloads, stash HTTP routes and web scripts are not
enabled. No new weights or model manifest changes. Sources and original
entrypoint are retained for audit; see third-party notices for the upstream's
missing license declaration, which needs review before redistribution.

Keep `GEN_AUTOMATION_I2V_H3_LATENT_CONTRAST_ENABLED=false` until a separately
approved safe deployment activates the matching worker. The UI is disabled and
the API rejects non-neutral jobs before that. Neutral wire snapshots omit the
new field, keeping old strict workers compatible. Do not open the gate merely
because the control plane has updated. Close it before rolling the worker back;
preserve or drain non-neutral queued work rather than rewriting its settings.

`verify_h3_contrast.py` checks real pinned CPU tensors (FP32/FP16/BF16), schema,
non-mutation, audio preservation and neutral identity. The full native Eros
schema check covers contrast on/off, both conditioning modes and refinement.
These are integration checks, not a rendered visual comparison.

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
