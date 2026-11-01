# Lokken simple-v5 integration

New recipe identity: `eros_beta7_lokken` / `lokken-simple-v5-hq-20261101`.
Historical Beta5/Beta7 jobs and presets are not migrated or reinterpreted.
The source workflow SHA-256 is
`d0273aff260458898adb07d984be701d14aef9892303c98da84e83e362a4623d` (78,111 bytes).

## Reviewed source

Lokken's [simple-v5 companion workflow](https://huggingface.co/LokkenJP/TenEros_Max_optimized_learned/blob/f2dd2b315ea27c0a340ba7f3e03ef078e4753c28/SampleWorkFlowTwoPassOptimized/H3_Turbo_2Pass_ErosMax_b7_simple_v5.json)
uses the recommended HQ learned W6/INT8 checkpoint, not compact, BF16, the
original INT8-HQ file, or an unverified `beta7fixed` replacement. Native W6
requires ComfyUI >=0.38.0 and Kitchen >=0.2.36. No second quantization or Turbo
adapter is applied. Owner-selected LoRAs retain their ordering and strengths.

Model identity tuples (SHA-256, bytes, filename) are enforced independently at
control-plane manifest validation and worker startup in `H3_LOKKEN_MODELS`.
The INT4 encoder is pinned to Merserk/MiniMax-H3-INT4-ConvRot revision
`3c167bc916cab6cf3b85b9e3769952757ebb2e5d`. The graph selects that filename but
does not supply its checksum, and its embedded download metadata points at a
different encoder. We therefore record the selected source explicitly rather
than claim the graph uniquely specified those bytes.

FP16 upscaler source: LBH-123-AI/Minimax_h3_latent_Upscaler revision
`3f941d5d182014dd5c0a5e16330420ee2d4aa0c6`, file
`minimax_h3_latent_upscaler_3d_conv_v1/minimax_h3_latent_upscaler_3d_conv_v1_fp16.safetensors`.
The graph uses an older basename; the installed filename identifies the
reviewed FP16 v1 artifact explicitly. The historical BF16 upscaler is separate.

## Tensor and sampling contract

1. Model -> owner model-only LoRAs -> shifts 12/3 -> VSA10% -> Kitchen.
2. Reference conditioning, original-image guide at frame zero, 0.3 MP base,
   ER-SDE / sgm_uniform, 8 steps, CFG1, denoise1.
3. Base **denoised** output -> separate AV -> learned 3D video latent upscale
   (FP16, temporal chunking, force unload, 1 MP, 32 alignment).
4. Rejoin the unmodified base audio; add a separate frame-zero guide from the
   original image at the new latent size. Same noise seed/model/sampler,
   3 refinement steps / simple / denoise0.3 / CFG1.
5. Decode refinement **output** video, but export **first-pass audio**.
6. RTX Super Resolution 1.2x / ULTRA / strength1 / RGB10A2; then exact Lokken
   post-processing: grain0.005, saturation mix0.25, CRF18, medium, batches4,
   profile auto, interpolation and Lanczos upscale disabled.

MP means 1024² pixels, not decimal millions. Refinement follows the *rounded
base* aspect. RTX truncates scaled dimensions then rounds to multiples of8.
The API/worker/UI output contracts account for these separate geometries.
SaveVideo's automatic path remuxes the exact Lokken H.264 packets. Delivery
copies video and AAC audio without another lossy encode; only square-pixel
signalling, private-metadata removal and MP4 faststart are applied.
10-bit intermediate RGB is not HDR; final SDR H.264 is 8-bit yuv420p.
Upstream grain is stochastic and is not controlled by the sampling seed.

## Deliberate integration boundaries

The app uses Full mode only. It keeps its native 24fps duration selector and
per-attempt filenames/grants instead of copying demonstration content. It does
not enable experimental refmods, continuation/stitching, Preview caching or
frame interpolation. Saved node-version labels are not authoritative dependency
pins: the graph's Lokken revision contains no node code and its LBH revision
predates the graph's force-unload/chunking interface. Compatible implementations
are separately pinned, imported and schema-tested as the non-root runtime user.

Optional higher-detail controls expose the author's 0.4–0.5 MP base, 1.2–1.5 MP
refinement and editable sampling steps. More memory/time does not guarantee
better results; the author notes diminishing returns beyond 10–12 base steps.
Neither the recipe nor this integration guarantees flicker-free output.

No agent-run GPU test or GPU start is authorized. CPU graph/encoder contracts,
regression tests and immutable CI are necessary but cannot establish visual
quality or RTX compatibility on the provider's actual device/driver. The owner
must run a short clip before a large batch. No silent fallback to another
checkpoint, ordinary resize, dense attention or alternative encoder is allowed.

## Rollout boundary

Code changes do not authorize starting hardware or replacing artifacts in
place. Stage additive, fully hashed exact versions; preserve current models for
rollback; prove signed CloudFront range delivery. Require exact-source CI,
automatic immutable publication/staging, fresh global-idle/preservation checks,
exclusive owned fencing and a once-only guarded I2V cutover. Never modify
independent image work. See `THIRD_PARTY_LICENSES.md` for dependency source access.
