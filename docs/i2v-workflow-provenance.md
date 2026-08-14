# DaSiWa WAN 2.2 I2V v1 provenance

This document records the evidence and implementation choices behind the clean
`dasiwa-wan22-i2v-v1` worker contract. It is an acquisition and reproducibility
record, not a prompt, character, or generation-policy layer.

## Selected baseline

The production template is a minimal, headless ComfyUI graph derived from the
author's **FastFidelity C-AiO v8.9** workflow. The author currently marks C-AiO
active and the standalone C-I2V and C-FLF2V families deprecated. C-SVI remains
active, but is deliberately excluded from v1 because its multi-sampler graph is
not needed to prove reliable I2V inference.

The checked-in contract consists of:

- `workflows/dasiwa-wan22-i2v-v1.api.json`, SHA-256
  `3be64bc05bddf152961940d0433dc135e66c05e03c55166dc31040ff39af6d23`
- `workflows/dasiwa-wan22-i2v-v1.bindings.json`, SHA-256
  `83b893929bc89eff3f2de7bbb7aed3892fee42f91d3d18a52d7b5c95b0c630cd`
- `i2v-models/dasiwa-wan22-i2v-v1.json`, the authoritative artifact and runtime
  manifest

The graph uses only these ComfyUI core classes: `LoadImage`, `UNETLoader`,
`CLIPLoader`, `VAELoader`, `CLIPTextEncode`, `ModelSamplingSD3`,
`WanImageToVideo`, `KSamplerAdvanced`, `VAEDecode`, and `SaveImage`. A selected
reviewed LoRA additionally uses core `LoraLoaderModelOnly`. The optional
`face_fidelity=stable_expression` path replaces only the two sampler classes
with `KSamplerWithNAG (Advanced)` from ComfyUI-NAG commit
`ef8a641be08983cf5f06669f70719b6eecce3c7f`; the worker starts ComfyUI with all
custom nodes disabled except that exact allowlisted directory. The worker expands every
`{"$i2v": "..."}` binding before submitting the prompt to ComfyUI.

`SaveImage` emits the ordered decoded frames. The worker materializes those
frames as `frame-%06d.png` and invokes external FFmpeg outside ComfyUI. The encoding
contract is MP4/H.264, `yuv420p`, `+faststart`, no audio, and exactly the bound
frame count and FPS. The graph intentionally contains no video-combine,
interpolation, upscaling, caching, tiled-VAE, SageAttention, watermark, or
perfect-loop node. The worker can optionally scale decoded frames to the source
image's even dimensions and materialize a deterministic ping-pong output; both
are external delivery transforms and do not change the inference graph.

## Author sources

- [DaSiWa WAN 2.2 I2V 14B Lightspeed model](https://civitai.com/models/1981116)
- [Definitive WAN 2.2 I2V usage guide](https://civitai.com/articles/20293)
- [DaSiWa WAN 2.2 workflows](https://civitai.com/models/1823089)
- [Author workflow repository](https://github.com/darksidewalker/dasiwa-comfyui-workflows)
- [Older C-AiO how-to, explicitly outdated](https://civitai.com/articles/26508)
- [Experimental WAN General NSFW LoRA](https://civitai.com/models/1307155)
- [Official Comfy-Org WAN 2.2 dependency package](https://huggingface.co/Comfy-Org/Wan_2.2_ComfyUI_Repackaged)
- [Author's current ComfyUI installer](https://github.com/darksidewalker/dasiwa-comfyui-installer)
- [Normalized Attention Guidance paper](https://arxiv.org/abs/2505.21179)
- [Pinned ComfyUI-NAG implementation](https://github.com/ChenDarYen/ComfyUI-NAG/tree/ef8a641be08983cf5f06669f70719b6eecce3c7f)

The source C-AiO artifact is Civitai version `2712329`, file `3084725`, named
`DasiwaWan22WorkflowsI2VSVI2_fastfidelityCAioV89.json`, with SHA-256
`9c0646ce576bb08761425a9653cfbd9bd0132580f8e6e88029327d370583c3e09`.
The identical author-repository snapshot was observed at workflow-repository
commit `603b067be2d47e0532fda398f41ad6a2719d075e`.

The author also supplies a basic backend test: Civitai version `2405252`, file
`2295720`, SHA-256
`1145a6c6c2e4bfdbc657bf0fc1b4310d4e10a715af9c481af548008c836967ba`.
Run that health check before exact artifact/bootstrap/readiness verification when
validating a new worker image. The rollout itself does not submit a generation.

## Required model artifacts

Use a matching SnatchKiss v11 High/Low pair. Mixing versions, quantizations, or
loading the same half twice is invalid.

| Role | Civitai version/file | Bytes | SHA-256 |
| --- | --- | ---: | --- |
| `DasiwaWAN22I2V14BLightspeed_snatchkissHighV11.safetensors` | `2953474` / `2837908` | 14,528,782,272 | `fa4202ea621725c57b0cbb84543bd6a5548de1d85c0c5a9f18db0bcf91202a54` |
| `DasiwaWAN22I2V14BLightspeed_snatchkissLowV11.safetensors` | `2953485` / `2837910` | 14,528,782,272 | `6e746571355bb589b966a72ed7a8717a09af0aeaf699391138e9788bace224d1` |
| `umt5_xxl_fp8_e4m3fn_scaled.safetensors` | Comfy-Org | 6,735,906,897 | `c3355d30191f1f066b26d93fba017ae9809dce6c627dda5f6a66eaa651204f68` |
| `wan_2.1_vae.safetensors` | Comfy-Org | 253,815,318 | `2fc39d31359a4b0a64f55876d8ff7fa8d780956ae2cb13463b0223e15148976b` |

The four required files total 36,047,286,759 bytes (33.572 GiB). Provisioning
must download to a partial path, verify size and SHA-256, then atomically rename
the file. Civitai downloads may require an API token. Model acquisition should
use the official URLs in the manifest rather than a public mirror.

The exported C-AiO graph points at the author's private/local converted int8
filenames. Those are intentionally replaced by the official primary pruned FP8
filenames above. The VAE is installed under `models/vae/Wan/`; that exact case is
significant on Linux.

## Baseline generation contract

The author recommends 4 steps, CFG 1, Euler with `linear_quadratic`, sigma shift
5, roughly 0.52-0.83 megapixels up to native 720p, and 81 frames at 16 FPS. The
v1 defaults are therefore:

| Setting | Value |
| --- | ---: |
| Width × height | 576 × 1024 (589,824 pixels, divisible by 32) |
| Frames / FPS | 81 / 16 |
| Encoded duration | 5.0625 seconds |
| Total steps | 4 |
| High stage | steps 0 through 2 |
| Low stage | steps 2 through 4 |
| CFG | 1 |
| Sampler / scheduler | `euler` / `linear_quadratic` |
| High / Low sigma shift | 5 / 5 |

Frame counts congruent to 1 modulo 8 preserve WAN's temporal packing. For a
five-second 24 FPS output use 121 frames; 81 frames at 24 FPS truncates the
learned interval. The binding contract has recommendations, not arbitrary short
duration or resolution ceilings. Callers may expose advanced overrides while
keeping the baseline preset intact.

The dashboard's high-resolution profile automatically chooses the closest
source-aspect canvas that is divisible by 32, no larger than 1024 pixels on
either side, and inside the reviewed 0.52-0.83 megapixel range. A 16:9 source
therefore resolves to 1024 x 576; a 1144 x 1480 portrait resolves to 768 x 992
(761,856 native pixels and less than 0.16% aspect error). The verified source is
contained and edge-padded to that canvas before ComfyUI sees it, so WAN's core
resize cannot crop source pixels. Lanczos delivery scaling then restores the
exact even source dimensions. For the portrait example this improves native
sampling density by roughly 29% over the baseline while avoiding the memory and
latency risk of asking WAN to sample 1.69 megapixels directly. Optional ping-pong
cycles order the decoded frames forward and backward without repeating the
endpoints, so cycle boundaries remain adjacent-frame transitions. This is smooth
delivery looping, not generative first/last-frame conditioning. Looped delivery
is limited to 25 seconds after frame/FPS/cycle expansion. The standard 81-frame,
16 FPS profile therefore permits two 160-frame ping-pong cycles (320 frames,
20 seconds); looping remains off for the baseline validation preset.

At CFG 1, ordinary negative conditioning has no practical effect. The default
worker setting remains `face_fidelity=off`, which renders the original graph
without NAG and preserves exact compatibility with frozen v4 jobs. The dashboard
offers `stable_expression` for new jobs. That mode retains the same model pair,
latent topology, steps, seed, LoRAs, and delivery path, but selects the pinned
NAG sampler with reviewed values `scale=11`, `tau=2.37`, `alpha=0.25`, and
`sigma_end=0`. It appends a fixed effective positive anchor that preserves the
source expression and head angle while allowing one subtle blink, and a fixed
effective negative anchor covering expression, mouth, gaze, and head drift.
Authored prompts remain unchanged; both effective prompts and the selected mode
are recorded in output provenance.

The rollback anchor immediately before this feature is control-plane merge
`74ae801c0d61e445209dda5508807587529ce20c`, I2V worker source
`7dd463a4daafc55e7abf088ff1faa85b045200cf`, worker image digest
`sha256:da8d423e188193421d5299c71dd6719fc89ac3bdc04119a7437ed7d5300b47e7`,
and provider contract version 4. Disabling the per-job setting returns to the
original inference path without a provider rollback. The guarded worker rollout
can restore that exact source/digest if image-level rollback is required.

## Reviewed paired LoRAs

All reviewed LoRAs are disabled by default and absent from the checked-in base
graph. A closed-catalog selection inserts each high artifact on the high model
branch and its low artifact on the low model branch, in both cases before
`ModelSamplingSD3`. Filenames never cross the public submission contract.
The manifest's `graph_injection` field records this exact core-node topology for
every reviewed pair.

| Catalog / role | Civitai version/file | Bytes | SHA-256 |
| --- | --- | ---: | --- |
| WAN General high `NSFW-22-H-e8.safetensors` | `2073605` / `1969798` | 613,516,752 | `34e2144d3cd65360f97d09ccbe03e1c39a096df6c9234af5fe3899d1b63cda39` |
| WAN General low `NSFW-22-L-e8.safetensors` | `2083303` / `1979213` | 613,516,752 | `d6b783742f4d5fd63a0223ae1d5bf64fc995a6b408480ac2a00528ae0d4146db` |
| Bouncing Boobs high `BounceHighWan2_2.safetensors` | `2191217` / `2084187` | 306,847,512 | `a4f4398031e9f39571310355f23e2d104c21143f517cf053e06d21f1c48d3d52` |
| Bouncing Boobs low `BounceLowWan2_2.safetensors` | `2191270` / `2084219` | 306,847,504 | `3ba8320137ba7d99885624dc512d8e0ea02f24364eabbe31e803fec785339ecb` |
| M4CROM4STI4 high `wan22-m4crom4sti4-i2v-20epoc-high-k3nk.safetensors` | `2265575` / `2157676` | 306,807,976 | `851c928737235b4a4a2c5993c893c79ee46a3131aa9b16eb56de1dcc576c3ad9` |
| M4CROM4STI4 low `wan22-m4crom4sti4-i2v-20epoc-low-k3nk.safetensors` | `2266727` / `2158834` | 306,807,976 | `c8a940ad5ab59a15c7f39624f694482a020f0dd047cec56f498b58418d3d937c` |
| DR34ML4Y v2 high `DR34ML4Y_I2V_14B_HIGH_V2.safetensors` | `2553151` / `2441563` | 306,807,976 | `d9931756c202bd8d4946c0d163c1269231a6352b51bb4235f6a19894c9ad8c68` |
| DR34ML4Y v2 low `DR34ML4Y_I2V_14B_LOW_V2.safetensors` | `2553271` / `2441662` | 306,807,976 | `066ee4bfafb685c85f08174c8283cd11bc6d36f4845347f20d633ab44581601f` |
| SmoothMix animation high `SmoothXXXAnimation_High.safetensors` | `2376136` / `2266910` | 306,807,280 | `eac4f4341008abb00434d08fed1d4fda4a144bc94cd26b4819f629f930a75181` |
| SmoothMix animation low `SmoothXXXAnimation_Low.safetensors` | `2376143` / `2266915` | 306,807,280 | `ad50dfc46c765a6ccc36d40e8a5f77ac2db041f68266593add12ac5f5eac2d76` |

WAN General's author labels the pair experimental, unfinished, slightly
underbaked, and seed-sensitive; its reviewed starting strength is 0.3 and its
trigger is `nsfwsks`. Bouncing Boobs is a WAN 2.2 I2V-A14B high/low pair with
trained phrase `her breasts are bouncing`; the author reference workflow uses
1.0 standalone, with 0.5-0.6 suggested when stacking. M4CROM4STI4 is a WAN 2.2
I2V-A14B high/low pair with trigger `m4crom4sti4`. Its author publishes no
numeric strength and the model can strongly bias breast size and anatomy, so
0.5 is the conservative implementation default, followed by isolated A/B at
0.7 and 1.0. The worker appends each selected
trigger exactly once and records the effective prompt, catalog IDs, strengths,
artifact filenames, and SHA-256 values in result provenance. Extra speed-up
LoRAs must not be added because Lightspeed already contains its low-step
distillation.

DR34ML4Y v2 supplies five alternative concept words: `m15510n4ry`, `bl0wj0b`,
`c0wg1rl`, `d0gg1e`, and `d0ubl3_bj`. They are not a combined trigger. The
operator chooses the concept appropriate to the prompt, and the worker appends
none automatically. The author publishes no numeric WAN v2 strength for this
stronger pair, so the implementation starts isolated A/B at 0.7 (0.5 when
stacking); this is not an author recommendation. SmoothMix's official paired
animation versions declare no
trained words; the author showcases strength 1.0. A request may select at most
three unique catalog entries at once. This keeps the five-entry catalog
extensible while bounding stacked model patches and avoiding highly confounded
experiments on the 32 GB worker.

The following source-model usage flags were recorded from the official Civitai
API on 2026-08-13. They are provenance presented to the operator, not runtime
prompt or character restrictions:

| Catalog | Credit required | Commercial-use values | Derivatives | Different license |
| --- | --- | --- | --- | --- |
| WAN General NSFW v0.08a | No | `RentCivit` | Allowed | Allowed |
| Bouncing Boobs WAN 2.2 | Yes | `Image`, `RentCivit` | Not allowed | Not allowed |
| M4CROM4STI4 K3NK | Yes | none declared | Not allowed | Allowed |
| DR34ML4Y I2V 14B v2 | No | `RentCivit` | Not allowed | Not allowed |
| SmoothMix XXX Animations | Yes | `RentCivit`, `Image` | Not allowed | Allowed |

### Two-phase LoRA rollout

LoRA worker capability and public submission are deliberately separate. Routine
control-plane deployments preserve the I2V image, manifest, source revision,
high-resolution flag, worker capability flag, and public profile flag. Only the
explicit `i2v-lora-worker` phase-one operation may align those values with a new
provider image; it forces `GEN_AUTOMATION_I2V_LORA_PROFILE_ENABLED=false` before
the replacement worker downloads and verifies the exact reviewed artifacts.
The normal dashboard remains available in a serving maintenance profile during
that long provider bootstrap; only new I2V enqueue, retry, and reorder actions
are frozen, while cancellation remains available.

The reviewed phase-one manifest is an immutable, version-addressed private
object, never an unversioned latest value:

- bucket `gen-automation-staging-861912887470-eu-central-1-models`
- key `worker/i2v/manifests/sha256/f0cd579606c8bc7fbf77ee8353b5c542395576d08f21e9acea37a1e2de19876e.json`
- version `u4bSnCPzDJ4zctrA2Nr66ji0Zh2qPpXX`
- byte length `6153` and source-object SHA-256
  `f0cd579606c8bc7fbf77ee8353b5c542395576d08f21e9acea37a1e2de19876e`

Four hashes have distinct meanings and must never be substituted for one
another:

| Identity | Exact SHA-256 |
| --- | --- |
| Immutable S3 source bytes | `f0cd579606c8bc7fbf77ee8353b5c542395576d08f21e9acea37a1e2de19876e` |
| Canonical compact private-manifest JSON stored on the host | `ebdeca736ee3e9ea4e4b7118c9e4b54dfcfd1bbde5a761f424aa85b1670b806f` |
| Derived 14-role worker model-object JSON | `4ff59362992c7284e2e24fcb7d3ce2c61b6d662f074123777bc621971f33a8fc` |
| Artifact identity over role, bytes, content SHA, and S3 version | `68f6c28831ac2a8e1801ba420c9816a29e09c8cc4738aae85611955553a3d301` |

`promote` and `rollback` first acquire the control-plane deployment lock, close
the public profile, freeze I2V reconciliation and claiming, and restart the
controller in that maintenance state. Before any provider mutation the bounded
operation requires zero active durable I2V jobs or attempts and a direct Salad
read showing no pending or running provider work. It does not submit, cancel,
retry, or reorder jobs. The promote path fetches the exact S3 `VersionId`,
recomputes all four identities, resolves fresh short-lived artifact credentials,
and atomically replaces the provider image, full environment, and exact
capability readiness probe. It then directly reads back the provider contract
and waits for an exact Ready instance before aligning the host and reopening
baseline I2V. The worker wait is bounded at 10,000 seconds, and the enclosing
SSM, OIDC, and workflow limits include explicit headroom for a full automatic
provider and host rollback.

Provider rollback restores the previous image and readiness probe, issues fresh
credentials for the restored worker profile, and null-tombstones environment
keys that did not exist in the previous merge-patched contract. The host
maintenance snapshot is restored if any guard, provider patch, readiness check,
host alignment, or controller health check fails. Durable queued jobs remain
unchanged throughout.

Once bootstrap and ComfyUI are ready, worker `GET /ready` reports a non-secret
`gen-automation/i2v-worker-capability/v1` identity containing the capability
flag, ordered artifact roles, SHA-256 of the exact worker model-object manifest,
and immutable source revision. The endpoint does not expose buckets, keys,
versions, grants, or credentials. A coordinated rollout must compare this
identity with the intended manifest and revision before opening the public gate.
The identity distinguishes the promoted raw private-manifest SHA-256 from the
derived worker-object JSON SHA-256 and includes a digest over each artifact's
role, byte size, SHA-256, and immutable object version. Salad's readiness probe
can bind all three identities in
`/ready/capability/{raw_manifest_sha256}/{artifact_identity_sha256}/{source_revision}`;
an exact 200 therefore proves full bootstrap without exposing a Container Gateway.

After the provider reports the exact expected image digest, the worker is ready,
bootstrap readback confirms every reviewed artifact, and there are no
incompatible active jobs, an operator may use `i2v-lora-profile enable` to set
only `GEN_AUTOMATION_I2V_LORA_PROFILE_ENABLED=true` and restart the control
plane. This publication step must not change the worker image or model manifest.
`i2v-lora-profile disable` is the immediate rollback; it hides the
catalog and rejects new, preset-derived, and retried LoRA work while leaving
cancel available. The first LoRA generation remains an operator-authored queue
submission; rollout automation never submits one.

## RTX 5090 runtime lock

The author's current installer maps RTX 50-series to Python 3.12 and the official
PyTorch CUDA 12.8 wheel set. The first validated image should be frozen by image
digest with:

- ComfyUI `v0.32.0`, commit
  `c2bcbecd82ec5ae66594340b395c24ef0217b238`
- Python 3.12
- Torch 2.9.1, torchvision 0.24.1, torchaudio 2.9.1 from
  `https://download.pytorch.org/whl/cu128`
- FFmpeg with `libx264`

The frontend is not required by the headless worker. The contemporaneous
frontend pin is `v1.51.2`, commit
`ab42ebc2d9c94b65832ee72dbde3c25fccbf374b`.

The full editable C-AiO workflow references DaSiWa Nodes, rgthree, WhiteRabbit,
KJNodes, GGUF, VideoHelperSuite, LTXVideo, and WhatDreamsCost. Their observed
commits are recorded in the model manifest for provenance, but all are excluded
from the minimal worker because none of their classes appears in the API graph.
This keeps the inference lane independent of convenience UI and post-processing
branches.

The model's Civitai record has its own attribution, commercial-use, derivative,
and redistribution flags. Those are source-provenance facts for acquisition and
distribution operations; this workflow does not translate them into prompt or
character restrictions.
