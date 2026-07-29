# Generation workflows

## Base Illustrious/SDXL profile

`workflows/illustrious-sdxl-base-v1.json` is the production ComfyUI API
template for the first generation profile. It uses only core, allowlisted
ComfyUI nodes:

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
4. renders the manifest `target_filename` into the Comfy graph; and
5. verifies that the rendered graph contains exactly one matching checkpoint
   loader and exactly the requested LoRA loaders and weights.

Worker readiness already depends on successfully downloading, hashing,
Safetensors-validating, and materializing every manifest target. Together these
checks prevent a release from referring to a display name, stale file, or model
that is absent from the running worker.

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

Hires-fix and detailer profiles are intentionally separate workflow versions.
They do not change or weaken this base profile's node allowlist.
