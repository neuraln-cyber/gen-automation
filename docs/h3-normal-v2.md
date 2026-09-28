# Normal Hybrid V2 and editable sampling

This is a checkpoint/recipe switch, not a claim that LoRA blur is fixed.
The owner must compare actual output quality after deployment.

## Exact model and defaults

[DaSiWa Hybrid V2, version3314675](https://civitai.red/models/2877206/dasiwa-minimax-h3?modelVersionId=3314675)
INT8 file3203130, 20,967,669,160 bytes, SHA256
`4cb8e1eaa9c3e5c664822760890bcbe6078455401cfa43205baf322e856d25f8`.
This is the non-Turbo checkpoint, not the separate INT4 download.

The creator's non-distilled recommendations are RES Multistep/simple or
Euler/simple, 20–25 steps, video shift10–12 and audio shift3–5. Defaults use
RES Multistep/simple, 20 steps, video12/audio4; CFG1 retains the author's
BasicGuider. Keep the existing author REF2VA environment, Basic LoRA stack,
exact encoder, both VAEs and latent upscaler. No Turbo distillation LoRA was
silently added or removed; owner-selected LoRA files/order/strengths are unchanged.

The [Turbo source manifest](../i2v-models/dasiwa-minimax-h3-turbo-v2.sources.json)
is retained verbatim for rollback. The normal manifest changes only diffusion.
Existing outputs, jobs and presets are never rewritten.

## Owner-requested expert controls

Creator recommendations are defaults, NOT enforced ranges. Native Comfy0.37
samplers/schedulers are selectable, including ER-SDE/Beta and LCM/simple.
Steps, CFG, video/audio shifts and denoise are editable per job. Refinement has
independent steps, CFG, sampler, scheduler and denoise; its defaults remain
one step/simple/CFG1/denoise0.2 and the selected base sampler.

Only native input validity remains: installed names; finite values; steps1–10000,
CFG0–100, positive shifts0.01–100 and denoise0–1. These are Comfy node bounds,
not checkpoint/LoRA recommendations. Existing frame-grid/canvas contracts and
image-generation/resource/cost/idle policies are unchanged. Experimental
combinations can produce poor output, take much longer or run out of memory.

For CFG !=1, the graph uses native CFGGuider with native CLIPTextEncode for the
negative prompt, through the same creator-selected CLIP. Blank text is encoded
as blank negative conditioning, not replaced with the positive embedding. Base
REF2VA positive conditioning remains the creator DirectorGuide output. The real
upstream refiner receives its own CFG and negative conditioning when needed;
without the negative connection it would silently use CFG1. At CFG1 the original
BasicGuider path is unchanged and the negative prompt has no effect on that pass.

New jobs/presets explicitly bind `h3_model_variant=hybrid_v2`; missing identity
means legacy Turbo. API, queue claim/dispatch and worker reject cross-checkpoint
execution. The worker derives identity from the actual diffusion SHA256, not an
untrusted request label. A saved 4-step normal-model recipe is allowed, e.g. with
an owner-selected Turbo LoRA. This does not make old Turbo-model jobs portable.

Drafts are separately stored per checkpoint. The first normal-model draft can
copy old prompts, seed, dimensions and LoRAs but loads creator sampling defaults
with a visible notice. The old draft is retained. Subsequent custom settings are
preserved verbatim. Restore-defaults changes only sampling, not prompts or LoRAs.

## Rollout and rollback

Default configuration remains Turbo and advanced sampling disabled, so a
control-plane-first rollout cannot queue unsupported settings to an old worker.
Enable `GEN_AUTOMATION_I2V_H3_MODEL_VARIANT=hybrid_v2` and
`GEN_AUTOMATION_I2V_H3_ADVANCED_SAMPLING_ENABLED=true` only with the verified
normal manifest and matching published worker, under fresh idle/hold gates.

Preserve both CloudFront delivery switches, distribution, cache/auth/routing
policies, all previous S3 versions and existing read grants. Stream/hash the new
weight into a NEW content-addressed object; grant only that exact version to the
existing worker reader. Verify real worker-credential version/range reads via
the existing CloudFront route for the new weight and preserved encoder before
activation. Never bypass a failed CDN read by switching delivery to direct S3.

The operator receipt documents the root-only rollback snapshot. Roll back the
worker image, private model manifest and recipe flags together at an idle boundary;
do not restore old provider credentials, overwrite unrelated environment changes,
or blindly restore a whole IAM policy. Keep normal and Turbo objects/versions and
exact read grants so either binding remains recoverable. No test jobs or GPU
start/stop/reallocation is part of numerical/CI validation.
