# Animation Studio

Animation Studio turns one verified library image into a short image-to-video
clip. The browser remains pinned to one economical profile instead of exposing
a matrix of models and advanced controls:

- Wan 2.2 TI2V 5B through pinned, native ComfyUI nodes;
- 832x480 landscape or 480x832 portrait, selected from the source orientation;
- 24 fps and either 73 native frames (about 3 seconds) or 121 native frames
  (about 5 seconds);
- a deterministic H.264 MP4 ping-pong encode, yielding about 6 or 10 seconds
  of loop-ready playback without a second diffusion pass; and
- one to three variants per submission, with at most one active Salad worker.

An internal-only `hq_native` canary profile uses the same pinned Wan 2.2 TI2V
5B weights, built-in Comfy nodes, sampler, and 24 fps timing at exactly 73
native frames. It renders 1472x1152 landscape or 1152x1472 portrait, permits one
variant and one provider attempt, plans a conservative 3,058-second render, and
bounds Comfy execution at 5,400 seconds under the controller's 6,300-second
watchdog. The worker removes `static` from this profile's built-in negative and
adds explicit camera-shake, pan, tilt, roll, zoom, reframing, and background-
drift blockers. The standard v1 descriptor and workflow remain unchanged. This
selector is intentionally absent from the browser until the canary is accepted.

The status page can cancel the whole variant group. Variants that have not
reached Salad are cancelled locally; an accepted provider job is cancelled and
reconciled through the controller before its reservation is released.

The browser surface is `/dashboard/animations`. It is owner/administrator only
and is hidden with a 404 unless `GEN_AUTOMATION_VIDEO_GENERATION_ENABLED=true`.

## Submission contract

The operator selects one `AVAILABLE` raw master from the private library and
may enter a motion prompt. The server freezes the exact source object version,
SHA-256, byte size, media type, dimensions, profile identity, seed, and render
shape before queueing work. A source and generated clip are never addressed by
an arbitrary browser-supplied object key.

Every submission requires fresh rights and lawful-use confirmations. NSFW and
explicit choices reveal three additional confirmations: all depicted people
are adults, sexual content is consensual, and the request does not sexualize a
real person. These are per-submission checkboxes, not a character blocklist or
an external moderation service. The server stores the selected rating and the
attestation policy version with each immutable job.

The current controls intentionally omit long-form video, audio, arbitrary
resolution, user-supplied negative prompts, custom models, motion-reference
video, interpolation, and upscaling.

## Model delivery and caching

Salad does not provide a writable persistent volume that survives a replica
replacement or true scale-to-zero. Its image cache applies to OCI layers, not
files downloaded by application code. The video worker therefore never fetches
model weights at runtime.

The production image bakes three immutable files into a stable model layer:

| File | Bytes | SHA-256 |
| --- | ---: | --- |
| `wan2.2_ti2v_5B_fp16.safetensors` | 9,999,658,848 | `456f901338bd9eadbded3828b819109a9b68e8a525ca5cf8d0049a69fcfeca1e` |
| `umt5_xxl_fp8_e4m3fn_scaled.safetensors` | 6,735,906,897 | `c3355d30191f1f066b26d93fba017ae9809dce6c627dda5f6a66eaa651204f68` |
| `wan2.2_vae.safetensors` | 1,409,400,960 | `e40321bd36b9709991dae2530eb4ac303dd168276980d3e9bc4b6e2b75fed156` |

They total 18,144,966,705 bytes. All paths are pinned to revision
`fb1388adc906ab39ffc26ee40e96b22886b56bc4` of
`Comfy-Org/Wan_2.2_ComfyUI_Repackaged`. BuildKit verifies each checksum before
committing the layer, and worker startup independently verifies the manifest,
sizes, and hashes before readiness can succeed. Application source is copied in
a later layer, so normal code changes reuse the large model layer.

The Salad group uses `image_caching=true`. The dedicated GHCR package must be
publicly readable by digest because the MVP deliberately does not distribute a
long-lived registry credential to Salad. Publication fails unless the exact
digest can be inspected without registry credentials. The publication runner
logs out of GHCR after all authenticated verification, then performs the final
raw digest inspection anonymously. Cached image
layers can remain in Salad's cache for up to 30 days, so repeated starts avoid
a new origin-registry download when the layer is cached. A newly selected node
may still need to receive the cached layer; this design removes paid runtime
model downloads, not all cold-start transfer time.

CI builds the `runtime-contract` Docker target, which contains the exact
ComfyUI/runtime/queue adapter but deliberately excludes the 18.145-GB model
layer. CI generates and scans that runtime-contract SBOM. A production
publication is explicit and must run from `main` for an exact successful
main-push CI commit (the current `main` commit, so signed provenance and checked
out source cannot diverge), build the default `production` target with pinned
Buildx, BuildKit, and SBOM-generator versions/digests, verify the immutable
image labels and compressed manifest size, verify GitHub provenance, extract
and scan the exact production digest's attached SBOM, prove anonymous pull
access, and pin the deployed reference by digest. A first publication may need
the sole operator to change the new GHCR package visibility to public and rerun
the otherwise idempotent workflow.

If a first publication is interrupted after the tag is pushed but before its
GitHub provenance is attached, reuse intentionally fails closed. Delete only
that exact unattested `sha-<source>` package version after verifying the failed
run, then rerun from the same current `main` commit; the workflow never signs a
pre-existing, unverified digest.

## Salad lane and cost behavior

Video has a separate queue and a separate current `SaladDeployment` with
purpose `video`. It cannot replace or be selected as the image-generation lane.
The contract is:

- replicas 0, autoscaler minimum 0, maximum 1, desired queue length 1;
- one active render at a time;
- no idle warm lease in the MVP;
- up to three variants admitted together so one cold start can serve a small
  back-to-back batch; and
- scale back to zero as soon as the queue and active attempts drain.

At the default maximum rate of USD 0.35/hour, the conservative planning windows
are 6 minutes for a 3-second native clip (about USD 0.035) and 10 minutes for a
5-second native clip (about USD 0.058). These are reservation estimates, not a
price guarantee. The per-job status value is deliberately labelled a
conservative usage estimate: it is bounded wall-clock time multiplied by the
configured hourly ceiling, not a provider invoice. Deployment-level Salad
billing observations remain authoritative, and global daily/monthly budget
controls account for both image and video reservations.

Model/image download time in Salad's provider image-preparation phase is not
provider-billed; runtime inference and CPU postprocessing occur in a running
container and are billed. No permanent warm replica is kept merely to retain
weights.

## Worker boundary

The controller keeps standard jobs on the original signed `video-worker.v1`
wire shape so an older standard worker remains compatible during rollout. HQ
jobs use `video-worker.v2`, which additionally signs the execution-contract
SHA covering the HQ profile, workflow, built-in negative, timeout, cost plan,
and one-attempt policy. Both contain exact presigned source and single-output
grants, their expected origins and media types, the frozen profile, prompt,
seed, shape, and attempt identifiers. The worker:

1. validates the signature, expiry, replay cache, profile, and grant origins;
2. downloads and verifies the source bytes and image bounds;
3. runs a loopback-only pinned ComfyUI process with all custom nodes disabled;
4. validates every native output frame;
5. creates the ping-pong H.264/yuv420p/faststart MP4 locally;
6. verifies the final media metadata, hash, and size; and
7. uploads only to the signed output grant.

No Salad artifact credentials, database credentials, provider API key, model
download token, or runtime network model fetch is present in the video worker.

## Rollout

The feature remains disabled after an ordinary control-plane deployment. A
staging enablement requires all of the following:

1. migration `20260811_0035` applied and checked;
2. runtime-contract build, smoke, SBOM, and vulnerability scan green;
3. an explicitly published production video-worker image whose compressed
   layers are below Salad's 35-GB limit with operational headroom, whose exact
   SBOM scan is green, and whose digest is anonymously readable;
4. exact OCI source/profile/workflow/model labels verified and the digest placed
   in `GEN_AUTOMATION_SALAD_VIDEO_WORKER_IMAGE`;
5. a distinct queue, group, and compatible 24-GB-or-larger GPU class list;
6. a read-only deployment/budget audit; and
7. one owner-approved, one-variant, 3-second staging canary.

`GEN_AUTOMATION_SALAD_VIDEO_CONTAINER_PRIORITY=batch` may be set for the
isolated video group. When it is unset, the video lane inherits
`GEN_AUTOMATION_SALAD_CONTAINER_PRIORITY`; the override never changes the image
lane deployment intent.

The canary must preserve the image lane, create no more than one video replica,
produce one verified MP4, settle queue/attempt/outbox work to zero, and return
the video lane to zero replicas. On any mismatch, disable
`GEN_AUTOMATION_VIDEO_GENERATION_ENABLED`, stop new video admission, reconcile
the video group to zero, and retain job/output records for audit. Do not delete
or mutate the image-generation group as part of video rollback.

Once a `purpose=video` deployment or video job exists, rollback is
feature-flagged and fix-forward: keep migration `20260810_0034`, also keep
`20260811_0035` once an HQ-profile job exists, and retain a binary that
understands per-purpose deployments while the cancellation-only loop drains
work. Do not deploy a pre-purpose binary or downgrade the schema; the migrations
intentionally refuse those downgrades while their video state exists.

## References

- [Salad container registries and image caching](https://docs.salad.com/container-engine/explanation/infrastructure-platform/container-registries)
- [Salad core container concepts](https://docs.salad.com/container-engine/explanation/core-concepts/overview)
- [Salad model-delivery guidance](https://docs.salad.com/container-engine/how-to-guides/ai-machine-learning/manage-stable-diffusion-models)
- [Wan 2.2 official repository](https://github.com/Wan-Video/Wan2.2)
