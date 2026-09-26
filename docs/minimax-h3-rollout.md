# MiniMax H3: private Salad video rollout

Status: implementation prepared; production activation requires the non-generating
rollout checks below. The owner will submit the first video test. Do not queue a
canary, sample, or other generation on the owner's behalf, and do not interpret
CPU tests or artifact access checks as proof that GPU generation has succeeded.

## Scope

### September 26 memory/recovery correction (prepared, not activated)

The no-LoRA source-size run completed. The following three-LoRA run exhausted
GPU memory during base sampling (3/4), before refinement. Logs recorded 32,192
MiB reserved and 28,458 MiB peak allocated. The CUDA error also broke cleanup in
`unload_all_models()` and killed ComfyUI's prompt thread. HTTP history remained
empty and GPU statistics kept returning 500, so the application waited forever.

The worker now checks GPU runtime health during empty-history polling. Three
consecutive failures trigger recovery; healthy slow sampling has no new time
limit. Explicit CUDA OOM history also triggers recovery. Only the ComfyUI child
is replaced, preserving the queue consumer and downloaded model/LoRA files.
The original execution returns an error even when recovery succeeds; there is
no internal replay or invented output. Existing provider retry policy is
unchanged. Failed recovery closes readiness and health for provider recovery.

H3 alone uses the native expandable-segment allocator, 8 GiB reserve, 4 GiB
DynamicVRAM headroom, and no cross-prompt graph cache. This gives activation and
LoRA patch buffers more room; offloading can trade speed for memory safety.
Image worker configuration, output dimensions, frame counts, selected LoRAs,
model artifacts and private delivery routes are unchanged. CPU tests prove
failure handling and configuration, not that every LoRA combination fits on
the GPU. Activation requires approval to replace the currently unhealthy worker;
do not create or retry a test video on the owner's behalf.

The first release animates one source image with a motion/audio direction prompt.
It uses DaSiWa Hybrid Turbo v2 INT8 with native ComfyUI MiniMax H3 nodes, not the
creator's optional multi-reference/director extensions. Image generation stays
unchanged. Salad is the provider; RunPod and the old WAN LoRAs remain disabled.
Historical WAN settings and outputs remain readable but cannot enter the H3 queue.

## Match original image size: community AI upscale + refinement

The dashboard's **Match original image size · H3 AI upscale** option derives
the output from the immutable source image, not its preview. It is an opt-in
two-pass workflow, not native full-resolution motion generation and **not**
MiniMax's hosted Regenerate-2K service. No additional inference provider is used.

For a 1144 × 1480 image:

1. Edge-pad the original to 1152 × 1504 (H3's 32-pixel grid), retaining its detail
   for the refinement keyframe.
2. Generate motion/audio at 768 × 992. The base canvas uses at most 768 pixels
   on its short edge and the existing 768 × 1344 pixel-area budget.
3. Separate the native AV latent; upscale **video only** with the learned 3D BF16
   H3 upscaler to 1152 × 1504. Offload H3 first and unload the upscaler afterward.
4. Re-encode the original full-size source keyframe; reuse text embeddings and
   the same LoRA-patched H3 model. Refine with Euler/simple, CFG 1, four steps,
   denoise 0.35, 1024-pixel tiles (25% overlap / 50% fade) and 56-frame temporal
   chunks with 22-frame overlap. Use the upstream temporal anchors, color match,
   and no extra seam-polish pass. These are initial integration defaults, not a
   claim of officially recommended settings or GPU-verified optimal quality.
5. Decode video; retain base-pass audio unchanged by refinement. Trim only the
   added border, delivering 1144 × 1480 H.264/AAC at the original 24 fps/duration.

A 1152 × 1504 source follows the same base/refinement path without final cropping.
Small sources that already fit the base envelope skip upscaling and refinement.
Matching dimensions does not guarantee pixel-identical original detail. The
extra pass consumes additional Salad GPU time; tiling reduces working memory
but does not guarantee that every clip fits a 5090. There is no silent switch
to interpolation/Lanczos or a paid API if AI refinement fails.

The new source list `i2v-models/h3-latent-upscaler.sources.json` pins one checkpoint:
690,592,992 bytes (0.643 GiB), SHA-256
`4f57821f5837f32f7142b67d815606dbd7550f194e5c769f7d6c3f83b146a5e6`.
Upstream code is pinned to `40316cf008b2fd8663263270669eb4da23f89d2c` in the worker
image. The existing four-model source list is unchanged. Five files total
40,761,724,703 bytes; incremental Standard storage at the price below is about
$0.016/month before credits/tax, plus requests and larger retained videos.
This does not promise an unchanged total AWS bill or free Salad compute.

Standard mode and existing queued jobs retain their graph and settings. The
`match_source_resolution` flag persists in drafts/presets/jobs. Width/height in
that mode describe the padded **refinement** canvas; output provenance records
base `native_width`/`native_height`, `upscale=h3_latent_refine`, and final dimensions.
The backend and worker independently derive sizing from the source. Even source
dimensions and the existing 2048-per-side technical bound remain.

### Safe activation (do not interrupt the current worker)

1. Leave `GEN_AUTOMATION_I2V_H3_SOURCE_RESOLUTION_ENABLED=false`. Mirror the
   separate upscaler source through the existing streaming mirror tool. Merge
   its verified exact-version entry into the private manifest; preserve the four
   H3 objects and all unrelated image permissions. Extend only its exact-version
   read authorization. Verify signed CloudFront access; do not add direct-S3
   fallback or change subscriptions. Do not put weights in the worker image.
2. Build the matching immutable worker/control-plane artifacts. The worker build
   imports the actual pinned nodes on CPU and checks their API; local shape,
   graph, audio identity and synthetic FFmpeg tests are not a GPU quality test.
3. Only after current jobs finish and both video queues are empty and the Salad
   group is stopped, install the new worker identity and five-object manifest.
   Until then leave the live worker, pending jobs and live manifest untouched.
4. Enable the flag with matching control-plane settings. Configuration rejects
   enabling without the pinned upscaler; startup verifies all extension nodes;
   old four-model workers reject source-size jobs before inference. Exact model,
   source and manifest identities remain part of the existing readiness contract.
5. Hand over for the owner's test. **Do not queue a test video or start a GPU.**
   Validate quality, seams, stability and memory on that user-initiated clip
   before describing performance as verified. Disable the flag for new jobs if
   rollback is needed; finish/cancel owner-approved source-size jobs before
   reverting to an older worker. Keep generated media and LoRAs.

False/absent mode fields are omitted from provider payloads for compatibility.
No budget quota, deletion policy, hard execution deadline or new provider is added.

### September 25: jobs reserved but never dispatched

The two owner-cancelled attempts remained Salad `pending`, with no `started_at`.
ComfyUI finished startup but never received a prompt. The video image omitted the
Salad queue-consumer binary and its supervisor never started a consumer; the
platform does not inject this process. This was a dispatch failure, not evidence
that H3 inference or high-resolution refinement ran out of memory.

The video image now builds the same pinned SDK and verified strict-HTTP,
stream-heartbeat and IMDS-recovery patches as the image worker. The supervisor
starts it only after ComfyUI readiness, strips model/AWS credentials from its
environment, and fails readiness/health if it exits. Salad configuration cannot
disable the consumer. Startup stages and sanitized failures are logged. The UI
labels the existing `claimed` state **Waiting for worker**, not generation progress.
Cancelled jobs stay cancelled; deployment must not start a GPU or enqueue a test.

The same audit found the Salad controller still sent `i2v-salad-job/v1` and
expected `i2v-salad-result/v1`, while the HTTP worker requires `i2v-job/v2` and
returns `i2v-result/v2`. Dispatch now shares the worker's schema constants and
result reconciliation accepts the actual envelope while retaining legacy-result
compatibility. Tests validate submitted payloads against the actual worker model
and feed an HTTP worker response into the controller parser.

See [Salad queue-worker integration](https://docs.salad.com/container-engine/how-to-guides/job-processing/queue-worker).

Sources: [community implementation](https://github.com/LBH-123-AI/Comfyui_Minimax_h3_latent_Upscaler/tree/40316cf008b2fd8663263270669eb4da23f89d2c),
[pinned upscaler weights](https://huggingface.co/LBH-123-AI/Minimax_h3_latent_Upscaler/tree/3f941d5d182014dd5c0a5e16330420ee2d4aa0c6).

Exact upstream pins are in
`i2v-models/dasiwa-minimax-h3-turbo-v2.sources.json`. The four files total
40,071,131,711 bytes: checkpoint 20,967,669,168; encoder 15,687,142,551;
video VAE 2,811,065,184; audio VAE 605,254,808. There is no public model mirror.

Sources: [DaSiWa version](https://civitai.com/api/v1/model-versions/3314686),
[native ComfyUI guide](https://docs.comfy.org/tutorials/video/minimax/minimax-h3-native),
[pinned supporting files](https://huggingface.co/Comfy-Org/MiniMax-H3/tree/bf92c4091e333e69b8ca1998e0a669f15cb0832b).

## Delivery costs

Reuse the existing private CloudFront distribution and subscription. Do not
create a second subscription. Operator-only account billing, resource identities
and verification receipts are kept outside this repository. Uncached pass-through
continues to let S3 authorize each individual signed request; no public grants,
cache-policy changes, or weakened IAM are required.

[AWS's plan documentation](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/flat-rate-pricing-plan.html)
states that origin-to-CloudFront transfer is free and plan delivery has no overage
charges. This covers delivery, not all AWS services. Pro's 50 GB S3 Standard
credit is shared across the payer account, not allocated per model.

At the checked Frankfurt Standard/frequent-access price of $0.0245/GiB-month,
the complete H3 bundle costs about $0.91/month before any credits or tax. Subtract
the actual retired bundle's storage cost to estimate the net change, accounting
for its prior Intelligent-Tiering access tier. Requests, tax and videos are separate. For scale,
another 100 GiB retained costs approximately $2.45/month at that storage tier.
This is an estimate, not a fixed total-bill guarantee.

No new hard usage, storage or spending caps are enabled. There is no library
quota, automatic media deletion, monthly cutoff, job-count cap or new hard
execution deadline. Optional startup/execution watchdog settings
default to unset. Preserve the existing owner-selected idle policy. Technical
model-shape validation and bounded network retries are not billing quotas.

## Delivery path

- Mirror once upstream → private regional S3, streaming 64 MiB parts. Verify the
  complete byte count and SHA-256 before committing each S3 version.
- Pin the four S3 VersionIds in the worker's least-privilege reader policy and
  immutable manifest. Preserve unrelated image model permissions.
- Worker model GETs use the existing `/models` CloudFront route, parallel ranges,
  resumed partial files and final SHA-256 validation. No direct-S3 fallback.
- Input images and clip playback use the same distribution's `/assets` route.
  The worker rejects an off-route input grant when private delivery is required.
- Clip uploads stay direct S3 PUT: inbound transfer is not the old egress charge.
- The existing private route rejects signed `response-content-disposition`
  overrides (live HTTP 403, including ASCII filenames). H3 attachment requests
  therefore use the normal private playback grant; the dashboard downloads a
  Blob and assigns the local filename. Never fall back to a named direct-S3 URL.

## Optional base/final quality comparison

The H3 advanced controls include **Diagnostic · keep video before upscaling**.
It is off by default and requires original-resolution delivery. For an
owner-started test, it retains the base-resolution clip and the normal final clip
from the **same sampling run**, seed, prompt, audio and selected LoRAs. It does
not lower strengths, run another sampler or automatically queue a comparison.

In completed videos, use **Final video / Before upscale** to switch the player.
Download saves the selected variant. The base clip is intentionally smaller
(for example, 768 × 992 for a 1152 × 1504 canvas); compare distortion and detail,
not pixel dimensions alone. A damaged base points to the base-generation path;
a clean base with a damaged final points to upscaling/refinement. Neither result
alone proves that a particular LoRA or its strength is the root cause.

The extra VAE decode runs after refinement and adds some GPU time and one
private MP4's storage/upload cost, but no extra sampling pass or model storage.
Both playback/download variants use the existing private CloudFront route.
Turn the option off after diagnosis. Previously generated clips do not gain a
base video retroactively. Both outputs must pass attempt-bound identity and
checksum verification before the diagnostic job is registered as complete.

Deployment: leave `GEN_AUTOMATION_I2V_H3_DIAGNOSTICS_ENABLED=false` until the matching worker
with `ManagedH3DiagnosticDecode` is installed. Only replace the video worker at
an owner-approved idle boundary with both application and provider queues empty;
do not cancel work or start a GPU test to enable this option. Then enable the
control-plane flag. Ordinary jobs omit the disabled setting in their worker
payloads for compatibility. On rollback, disable the flag before replacing the
worker and leave any pending diagnostic jobs untouched for an operator decision.

## Activation critical path

1. Finish and verify all four private mirror objects and the immutable manifest.
2. Extend exact-version reader authorization with those four objects, retaining
   current image artifacts. Remove only confirmed obsolete WAN permissions.
3. Build the immutable worker and control-plane images using the normal CI and
   publication process. Leave public H3 submissions disabled during preparation.
4. Wait for a safe control-plane deployment boundary; do not interrupt image jobs.
   Configure `i2v_profile=minimax_h3`, private delivery required, the same model
   and asset distribution, exact manifest/source identity, Salad enabled, WAN
   LoRA flags and RunPod disabled. Preserve existing user idle/usage policies.
5. Configure the RTX 5090 profile with sufficient host RAM (64 GiB candidate),
   matching native workflow and exact artifact-readiness identity. Confirm both
   application and provider video queues are empty and the video group is stopped.
   Do not start a GPU simply to check readiness.
6. Verify the deployed settings, dashboard controls, source-upload/download routes,
   and exact private artifact access without submitting a generation. Enable the
   H3 dashboard for the owner's first test; no prior GPU clip is an activation gate.
7. Hand off with the worker stopped and zero operator-created video jobs. Distinguish
   "configured for the owner's test" from "GPU generation verified." The owner
   chooses the image and motion/audio prompt and presses Generate. Confirmed runtime
   performance and output quality remain unknown until that user-initiated test.

If legacy WAN weights have been removed, reverting code alone is not a functional
WAN rollback: restoring that lane requires intentionally re-downloading its
pinned legacy files. Never delete generated media or unrelated LoRAs as part of
the model migration.
