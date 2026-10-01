# Native MiniMax H3 FL2VA INT8

This is the native **non-Turbo** first-frame image-to-video setup, replacing the
active DaSiWa H3 checkpoint, INT4 encoder, Director/Guide, seed and LoRA nodes.
It does not change the WAN image/video profile or private delivery architecture.
Historical DaSiWa source recipes remain for provenance; rollback requires the
matching old immutable worker image, manifest and controller settings together.
The owner chose to retain privately stored DaSiWa weights, not delete them.

## References and model identity

- Publisher: [tsolful FL2VA INT8 Pruned](https://civitai.red/models/2830065?modelVersionId=3193337),
  file3074134. Its SHA-256 is identical to the official Comfy-Org checkpoint.
- Publisher example workflow file3086841 has no first/last image connected, and
  adds SageAttention/KJ/VHS nodes. We use the suitable native I2V reference instead:
  [official Comfy workflow](https://github.com/Comfy-Org/workflow_templates/blob/e7cd011d4ded3411c2f481200544f0be6fdc962e/templates/video_minimax_h3_i2v.json).
  Reference SHA-256: `34ee39544808fd3b0dc8de9df082940d4d41c3771beb80d3988c1ea5531cec0d`.
- [Official model repository](https://huggingface.co/Comfy-Org/MiniMax-H3/tree/e5eb578a89295337b8ff433a035929ce0279e0b6):
  FL2VA INT8 Convrot diffusion, Qwen3-VL32B NVFP4 AWQ encoder, INT8 video VAE,
  FP32 audio VAE. Full filenames, sizes and hashes are checked by
  `h3_variants.H3_NATIVE_MODELS` on both controller and worker.
- New replacement downloads are pinned in `workflows/minimax-h3-native-model-sources.json`.
  No Turbo LoRA is automatically installed or enabled.

## Execution and settings

`LoadImage -> MiniMaxH3ImageToVideo` supplies **first_frame only**, untouched prompt
text, native image-aware CLIP conditioning, a frozen frame-zero VAE keyframe and
joint audio/video latents. `UNETLoader -> LoraLoaderModelOnly` applies selected
adapters in owner order/strength, using upstream Comfy metadata handling and
patching. There is no custom LoRA math, branch multiplier, prompt builder or
second application during refinement.

Default non-Turbo sampling follows the official template's **linked** 20-step
branch (not its inactive saved scheduler widget): `res_multistep / simple`, CFG1.
Native model defaults are video shift12/audio shift3. A native SigmaShift node
exposes the existing editable controls; its default schedule is numerically
checked equal to the unpatched model. Both scheduler and guider consume the
same shifted model. Steps/CFG/sampler/scheduler/shifts remain owner-editable and
are frozen per job. Old variants cannot be dispatched as `fl2va_int8`.

The API graph is an integration of that official base workflow, **not a claim of
byte-identical UI execution**. Intentional adapters: app-selected grid-aligned
canvas, 124–362 frames at24fps, saved seed, optional negative CFG, optional community
source-size refinement, diagnostic base MP4, private output encoding/upload.
The UI identifies refinement separately from the official base workflow.

## Runtime and isolation

Comfy0.37.0 (`73c9bad4d21e7addbe1d13bc92eee0f1431b017d`), frontend1.53.6,
Torch2.9.1/CUDA13, Kitchen0.2.35, AIMDO0.5.5. The base reference was authored on
Comfy0.33.0; its actual schemas and conditioning are checked against our pinned
0.37.0, not inferred from a version number. Existing Kitchen attention and memory
flags are retained. No KJ Torch FP16-accumulation override is applied.

DaSiWa and KJ node packs are **not installed** in the new image. H3's custom-node
allowlist contains only the pinned community H3 upscaler and the diagnostic
decode-order adapter. WAN/NAG nodes are not imported by the H3 process. The
upscaler's actual native FL2VA keyframe resize/crop/re-anchor and AV packing are
tested with its real upstream code. It is optional and unchanged in purpose.

Container CI imports the final packages as runtime UID/GID10002 and exercises
native node input/output contracts, first-frame conditioning, AV shapes, LoRA
metadata/strength ordering/clean base and default-schedule equivalence on CPU.
This does **not** prove GPU memory sufficiency or end-to-end visual recovery.

## Delivery and cutover

New immutable objects are full-SHA/length verified into private versioned storage.
Only exact object-version reader grants may be added; preserve old rollback grants.
Verify real worker-credential range reads through the existing CloudFront route.
Do not change its distribution, origin, caching or authentication, or substitute
direct S3 delivery. Model downloads and generated video delivery both remain private.

Require fresh idle gates, a coherent rollback snapshot, owned video-only hold,
successful exact CI/publication, single configure and single activation receipts,
then hold release and stopped/idle/public-health/CDN checks. Preserve the image
lane and all completed outputs. Never submit test jobs or claim visual quality
before the owner's comparison.
