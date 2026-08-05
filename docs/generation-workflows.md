# Generation workflows

## Base Illustrious/SDXL profile

`workflows/illustrious-sdxl-base-v1.json` is the production ComfyUI API
template for the first generation profile. It uses only core, allowlisted
ComfyUI nodes. Its SHA-256 is
`1d099e8ed6a73ddf30cce4b8a5970aa17de16377fd248f5a654a32f65fba9834`:

- `CheckpointLoaderSimple`
- `LoraLoader`
- `CLIPSetLastLayer`
- `CLIPTextEncode`
- `EmptyLatentImage`
- `KSampler`
- `VAEDecode`
- `SaveImage`

The release specification supplies the positive and negative prompts, seed,
width, height, steps, CFG, sampler, scheduler, Clip skip, and output count. CFG is
bounded to `0.0` through `30.0`; the New Set preset uses `6.0`. The output count
becomes the latent batch size, so one job produces the exact number of upload
grants and masters declared by `outputs_per_job`.

Clip skip defaults to `2`; the controller binds that as
`CLIPSetLastLayer.stop_at_clip_layer=-2` after the complete LoRA CLIP chain.
Source canvas dimensions may use any multiple of eight within `512` through
`4096`, subject to the unchanged downstream pixel limits.

The template contains one internal `GenAutomationLoraChain` marker. The control
plane removes this marker before signing the worker request and expands it into
zero through eight standard `LoraLoader` nodes in release order. Each LoRA's
configured weight is applied to both model and CLIP. This is a deliberately
narrow transform, not a general graph-editing language. More than eight LoRAs,
multiple markers, malformed links, generated-node collisions, or loader nodes
that do not match the release fail before provider submission.

## Runtime artifact binding

Release artifact `name` values are display labels; ComfyUI never receives them
as filenames. Before creating upload grants or a Salad job, the controller:

1. validates the separately pinned worker-manifest SHA-256;
2. matches the checkpoint and every LoRA by artifact kind and exact SHA-256;
3. also requires the manifest S3 object ID to equal the release storage key
   when the manifest uses an S3 source;
4. renders the manifest `target_filename` into the Comfy graph;
5. verifies that the rendered graph contains exactly one matching checkpoint
   loader and exactly the requested LoRA loaders and weights; and
6. traces every exact rendered output path through the supported latent,
   sampler, decode, detailer, and save nodes. Every source canvas and every
   chained latent upscale must remain at or below `8192x8192` and 12 million
   pixels before upload grants are created.

Worker readiness already depends on successfully downloading, hashing,
Safetensors-validating, and materializing every manifest target. Missing
targets download with a bounded concurrency of four after aggregate free space
is reserved; result ordering and validation remain manifest-deterministic.
Together these checks prevent a release from referring to a display name, stale
file, or model that is absent from the running worker. The signed GPU worker repeats the graph
geometry check immediately before the exact graph reaches ComfyUI.

## Registering the template

Upload the exact workflow bytes to the private workflow object key, calculate
their SHA-256, and use that object key and digest in both the workflow approval
and release specification. Do not edit an object in place after approval; use a
new workflow version and digest.

The credential-free synthetic contract in `tests/test_worker_inputs.py` runs
the production template through manifest resolution, bounded LoRA expansion,
job signing, the worker API, staged uploads, image verification, and immutable
`masters/` promotion. A live GPU canary is still required after the real
checkpoint, LoRAs, object store, and provider bindings are supplied.

## Hires profile

`workflows/illustrious-sdxl-hires-v1.json` is a core-node-only two-pass
Illustrious/SDXL workflow. Its SHA-256 is
`207992e773506c2d199a7e1037d8d677ecde44353582032d953d4b0fe1410152`.
The first `KSampler` produces the normal latent, `LatentUpscaleBy` enlarges it,
and a second `KSampler` refines it before the one final VAE decode.

The New Set form freezes three simple controls into every release:

- hires scale: `1.0` through `3.0`, default `1.5`;
- second-pass denoise: `0.05` through `1.0`, default `0.35`; and
- core latent interpolation: `bislerp` (default), `bicubic`, `bilinear`,
  `nearest-exact`, or `area`.

The base workflow ignores these values. A profile is selected by choosing its
approved workflow in New Set; there is no runtime graph editing or automatic
profile guessing.

## Base + face detailer profile

`workflows/illustrious-sdxl-base-detailer-v1.json` decodes the first sampling
pass directly into Impact Pack `FaceDetailer`; it does not upscale or run a
second sampler. Its SHA-256 is
`0cbfadda4e4ca9d915253f22b62010e6f78a38bb9ab052caff5c29ab0a1303fd`.
This is the closest bundled match for a Forge generation that uses ADetailer
without Hires fix.

The detailer profiles encode their own positive and negative prompts using the
post-LoRA, Clip-skip-adjusted CLIP. They also freeze `feather` (default `4`) as
the closest available control to ADetailer's mask blur. A blank detailer prompt
inherits its corresponding main prompt, preserving the previous profile
behavior while allowing either conditioning to be overridden independently.

## Hires + face detailer profile

`workflows/illustrious-sdxl-hires-detailer-v1.json` runs the same two hires
passes, then sends the decoded batch through Impact Pack `FaceDetailer`. Its
SHA-256 is
`d0b9a01f848632ca6f5c0635f4f10e3bd0c5255fd827c7b6f88caf17de865a20`.
The release freezes guide size, maximum size, denoise, face threshold, dilation,
crop factor, and feather. The New Set preset values are `768`, `1024`, `0.4`,
`0.3`, `4`, `3.0`, and `4`.

## Two-character area-composition profiles

The four `illustrious-sdxl-couple-*-v1.json` templates mirror the base, base +
detailer, hires, and hires + detailer profiles while keeping two character
identities spatially separated with ComfyUI core nodes. Their SHA-256 values
are:

- base: `539bfdf81d9668b6e0c60c77034ac5ff3c4d233e468c1f3dd1fe398415892923`;
- base + detailer: `62f8db236c95a5c88f130c1fc10fa34dbc7455dba45695081aa5bc19d2bde890`;
- hires: `b674f45c8b62f6742b74c1364f4c6b172ef2ed3446f4e29034e1c1da3727773c`;
  and
- hires + detailer:
  `9f2d863469bcc6ab069244c72797e19aa8a95aa9f253170fdd5066a754fe586e`.

Each template encodes the normal positive prompt as full-frame scene and style
conditioning. It independently encodes `character_a_prompt` and
`character_b_prompt`, assigns them to overlapping left and right regions with
`ConditioningSetAreaPercentage`, and merges all three conditionings with
`ConditioningCombine`. Character A covers `x=0.0..0.55`; character B covers
`x=0.45..1.0`. The ten-percent overlap avoids an unconditioned seam while the
separate regional embeddings reduce identity and outfit blending.

Both samplers in each hires graph receive the same combined regional
conditioning. Dropping the regions for the refinement pass can merge character
traits. The negative prompt remains full-frame. Detailer variants keep one
generic detailer prompt and let the existing face detector refine every face it
finds; they do not assign character identities during the face pass.

These profiles use no additional extension, model artifact, or sampling pass.
Their only additional runtime node classes are `ConditioningSetAreaPercentage`
and `ConditioningCombine`, both built into the pinned ComfyUI revision. Style
LoRAs remain global because the workflow has one model path; character-specific
LoRAs are not spatially isolated by this profile.

The worker image pins:

- Impact Pack commit `429d0159ad429e64d2b3916e6e7be9c22d025c3c`;
- Impact Subpack commit `50c7b71a6a224734cc9b21963c6d1926816a97f1`;
- every Python dependency and wheel hash in `requirements-comfy.lock`; and
- only those two custom-node directories in ComfyUI's custom-node whitelist.

The signed worker API independently permits only `FaceDetailer` and
`UltralyticsDetectorProvider` from those packages. It does not permit arbitrary
Impact Pack nodes.

### Detector artifact

The detector is not downloaded by ComfyUI, Impact Pack, or Ultralytics. Add one
`detector` entry to the existing immutable worker artifact manifest. The entry
must use a basename-only `.pt` target, an exact byte size, and an exact SHA-256.
At startup the worker downloads it with the read-only object-store identity,
verifies the digest and size, checks that it is a modern PyTorch ZIP archive,
and materializes it only under
`/opt/comfyui/models/ultralytics/bbox`.

Exactly zero or one detector is supported. Base and hires workflows work with
zero; either detailer workflow fails before upload grants are created unless one
is present. Once verified, that exact target filename becomes the only entry in
Impact Subpack's legacy-model whitelist. A detector `.pt` can contain executable
pickle data, so its source and digest must be reviewed with the same care as
worker code.

For the first live detailer canary, provide:

- the approved face detector object (the current preset uses the pinned
  `face_yolov8n.pt` model);
- its private object-store key, exact byte size, and SHA-256; and
- the updated artifact-manifest JSON and manifest SHA-256.

No model-host token is needed when the approved file is already in the private
artifact bucket.

## Registry onboarding

Bundling a JSON template does not automatically make it selectable. Upload the
exact template bytes to private workflow storage and create a current approved
workflow registry record using the path's SHA-256 above. Register the base,
base + detailer, hires, hires + detailer, and their four couple counterparts as
eight separate workflow approvals. They
then appear in the New Set workflow selector.

Upstream contracts:
[ComfyUI custom-node whitelist](https://github.com/Comfy-Org/ComfyUI/blob/700821e1364eaab0e8f21c538a2131719fec57bf/comfy/cli_args.py),
[Impact Pack](https://github.com/ltdrdata/ComfyUI-Impact-Pack/tree/429d0159ad429e64d2b3916e6e7be9c22d025c3c),
and
[Impact Subpack](https://github.com/ltdrdata/ComfyUI-Impact-Subpack/tree/50c7b71a6a224734cc9b21963c6d1926816a97f1).
