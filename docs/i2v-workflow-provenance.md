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
`WanImageToVideo`, `KSamplerAdvanced`, `VAEDecode`, and `SaveImage`. It has no
custom-node requirement. The worker expands every `{"$i2v": "..."}` binding
before submitting the prompt to ComfyUI.

`SaveImage` emits the ordered decoded frames. The worker materializes those
frames as `frame-%06d.png` and invokes external FFmpeg outside ComfyUI. The encoding
contract is MP4/H.264, `yuv420p`, `+faststart`, no audio, and exactly the bound
frame count and FPS. The graph intentionally contains no video-combine,
interpolation, upscaling, caching, tiled-VAE, NAG, SageAttention, watermark, or
perfect-loop node.

## Author sources

- [DaSiWa WAN 2.2 I2V 14B Lightspeed model](https://civitai.com/models/1981116)
- [Definitive WAN 2.2 I2V usage guide](https://civitai.com/articles/20293)
- [DaSiWa WAN 2.2 workflows](https://civitai.com/models/1823089)
- [Author workflow repository](https://github.com/darksidewalker/dasiwa-comfyui-workflows)
- [Older C-AiO how-to, explicitly outdated](https://civitai.com/articles/26508)
- [Experimental WAN General NSFW LoRA](https://civitai.com/models/1307155)
- [Official Comfy-Org WAN 2.2 dependency package](https://huggingface.co/Comfy-Org/Wan_2.2_ComfyUI_Repackaged)
- [Author's current ComfyUI installer](https://github.com/darksidewalker/dasiwa-comfyui-installer)

The source C-AiO artifact is Civitai version `2712329`, file `3084725`, named
`DasiwaWan22WorkflowsI2VSVI2_fastfidelityCAioV89.json`, with SHA-256
`9c0646ce576bb08761425a9653cfbd9bd0132580f8e6e88029327d370583c3e09`.
The identical author-repository snapshot was observed at workflow-repository
commit `603b067be2d47e0532fda398f41ad6a2719d075e`.

The author also supplies a basic backend test: Civitai version `2405252`, file
`2295720`, SHA-256
`1145a6c6c2e4bfdbc657bf0fc1b4310d4e10a715af9c481af548008c836967ba`.
Run that health check before the full canary when validating a new worker image.

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

At CFG 1, ordinary negative conditioning has no practical effect. The dashboard
may save and submit a negative prompt, but must describe it honestly. Activating
negative guidance requires a separately reviewed graph with core `NAGuidance`;
it is not silently enabled in v1.

## Optional paired LoRA

The WAN General NSFW v0.08a pair is recorded in the model manifest but disabled
and absent from the executable graph:

| Role | Civitai version/file | Bytes | SHA-256 |
| --- | --- | ---: | --- |
| `NSFW-22-H-e8.safetensors` | `2073605` / `1969798` | 613,516,752 | `34e2144d3cd65360f97d09ccbe03e1c39a096df6c9234af5fe3899d1b63cda39` |
| `NSFW-22-L-e8.safetensors` | `2083303` / `1979213` | 613,516,752 | `d6b783742f4d5fd63a0223ae1d5bf64fc995a6b408480ac2a00528ae0d4146db` |

The author labels the 2.2 pair experimental, unfinished, slightly underbaked,
and seed-sensitive. Although the description calls `nsfwsks` a trigger, the two
2.2 API versions declare no trained words. A future workflow revision may inject
the matched High and Low LoRAs after the bare-model canary passes, beginning at
strength 0.3. The manifest therefore records `graph_injection` as `deferred`.
V1 does not invent inactive LoRA node values. Extra speed-up LoRAs
must not be added because Lightspeed already contains its low-step distillation.

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
