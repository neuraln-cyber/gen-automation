# Generation workflows

## Base Illustrious/SDXL profile

`workflows/illustrious-sdxl-base-v1.json` is the production ComfyUI API
template for the first generation profile. It uses only core, allowlisted
ComfyUI nodes. Its SHA-256 is
`901a50003bfb9aa17c6117a29fc1232a678dcadc19f70a895fe6edf69ccf3fca`:

- `CheckpointLoaderSimple`
- `LoraLoader`
- `CLIPTextEncode`
- `EmptyLatentImage`
- `KSampler`
- `VAEDecode`
- `SaveImage`

The release specification supplies the positive and negative prompts, seed,
width, height, steps, CFG, sampler, scheduler, and output count. CFG defaults
to `5.0` and is bounded to `0.0` through `30.0`. The output count becomes the
latent batch size, so one job produces the exact number of upload grants and
masters declared by `outputs_per_job`.

The template contains one internal `GenAutomationLoraChain` marker. The control
plane removes this marker before signing the worker request and expands it into
zero through four standard `LoraLoader` nodes in release order. Each LoRA's
configured weight is applied to both model and CLIP. This is a deliberately
narrow transform, not a general graph-editing language. More than four LoRAs,
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
Safetensors-validating, and materializing every manifest target. Together these
checks prevent a release from referring to a display name, stale file, or model
that is absent from the running worker. The signed GPU worker repeats the graph
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
`42761a3244e8b69870bd6aed52c35d1f680d641429e2fdce1493ad837e1da547`.
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

## Hires + face detailer profile

`workflows/illustrious-sdxl-hires-detailer-v1.json` runs the same two hires
passes, then sends the decoded batch through Impact Pack `FaceDetailer`. Its
SHA-256 is
`637360c7ddb681d37810bbd34edd4f3d72501b6db5a55eaacfe7e866d1469e2d`.
The release freezes guide size, maximum size, denoise, face threshold, dilation,
and crop factor. Defaults are deliberately conservative: `768`, `1024`,
`0.35`, `0.5`, `10`, and `3.0`.

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
zero; the detailer workflow fails before upload grants are created unless one
is present. Once verified, that exact target filename becomes the only entry in
Impact Subpack's legacy-model whitelist. A detector `.pt` can contain executable
pickle data, so its source and digest must be reviewed with the same care as
worker code.

For the first live detailer canary, provide:

- the approved face detector object (normally a trusted
  `face_yolov8m.pt`-compatible model);
- its private object-store key, exact byte size, and SHA-256; and
- the updated artifact-manifest JSON and manifest SHA-256.

No model-host token is needed when the approved file is already in the private
artifact bucket.

## Registry onboarding

Bundling a JSON template does not automatically make it selectable. Upload the
exact template bytes to private workflow storage and create a current approved
workflow registry record using the path's SHA-256 above. Register the base,
hires, and hires + detailer files as three separate workflow approvals. They
then appear in the New Set workflow selector.

Upstream contracts:
[ComfyUI custom-node whitelist](https://github.com/Comfy-Org/ComfyUI/blob/700821e1364eaab0e8f21c538a2131719fec57bf/comfy/cli_args.py),
[Impact Pack](https://github.com/ltdrdata/ComfyUI-Impact-Pack/tree/429d0159ad429e64d2b3916e6e7be9c22d025c3c),
and
[Impact Subpack](https://github.com/ltdrdata/ComfyUI-Impact-Subpack/tree/50c7b71a6a224734cc9b21963c6d1926816a97f1).
