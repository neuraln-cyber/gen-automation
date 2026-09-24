# MiniMax H3: private Salad video rollout

Status: implementation prepared; production activation and a real GPU clip still
require the rollout checks below. Do not interpret CPU tests as generation proof.

## Scope

The first release animates one source image with a motion/audio direction prompt.
It uses DaSiWa Hybrid Turbo v2 INT8 with native ComfyUI MiniMax H3 nodes, not the
creator's optional multi-reference/director extensions. Image generation stays
unchanged. Salad is the provider; RunPod and the old WAN LoRAs remain disabled.
Historical WAN settings and outputs remain readable but cannot enter the H3 queue.

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
5. Use the RTX 5090 profile with sufficient host RAM (64 GiB candidate). Check
   live availability and exact readiness identity before submitting a clip.
6. Verify one benign first-frame 124-frame/4-step clip: model load, native nodes,
   H.264 MP4, audio, playback and CloudFront-only transport. Stop the test worker
   after the check; do not launch repeated paid tests without a concrete failure.
7. Open the H3 dashboard only after success. Record actual image digest, manifest
   identities, measured generation time and any remaining limitations here.

If legacy WAN weights have been removed, reverting code alone is not a functional
WAN rollback: restoring that lane requires intentionally re-downloading its
pinned legacy files. Never delete generated media or unrelated LoRAs as part of
the model migration.
