# Eros Beta 7 integration and cutover contract

This recipe replaces the active Beta 5 model after a verified deployment. Beta 5
identities remain readable for historical jobs; those jobs cannot execute against
Beta 7. Changing this source document alone does not change the live binding.

## Checkpoint choice and author evidence

- [Requested Civitai version 3429412](https://civitai.red/models/2851079/h3-eros-max?modelVersionId=3429412),
  INT8 file3329966, matches the author's
  [INT8 HQ file at pinned revision](https://huggingface.co/TenStrip/10Eros-Max/tree/682e73d12dcaf766dd31b4bb2991084119c2df8c):
  `10Eros_Max_h3_8stepTURBO_beta7_int8HQ.safetensors`, 22,893,150,024 bytes,
  SHA256 `8a51ca3f7d290a799d89fefcde4ede60962256955a91dfe4746d3cc07814fd6e`.
  The raw INT8, BF16 and modified learned-activation checkpoints are distinct.
- The [author's model card](https://huggingface.co/TenStrip/10Eros-Max) describes
  FastH3-Omni-derived, gate-free Beta 7 with 8-step Turbo baked in, 90% sparse
  attention from the start, shifts12/3, and Euler/simple or er_sde/sgm_uniform.
- [Sparse-attention clarification](https://huggingface.co/TenStrip/10Eros-Max/discussions/76)
  identifies missing VSA as a cause of background blotches. Use VSA, not SLA or
  a training-free substitute with the same numerical keep ratio.
- [Audio guidance](https://huggingface.co/TenStrip/10Eros-Max/discussions/77)
  favors natural audio cues over the old H3 `overall_soundscape` section.
- [Two-pass discussion](https://huggingface.co/TenStrip/10Eros-Max/discussions/72)
  recommends two passes for quality; single-pass flicker remains possible.
  The author-linked [Lokken example](https://huggingface.co/LokkenJP/TenEros_Max_optimized_learned/tree/main/SampleWorkFlowTwoPassOptimized)
  uses a different learned-activation checkpoint, RTX video-super-resolution
  nodes and a fixed1.2x upscale. This application keeps the exact requested
  original weights and its existing optional source-size latent refinement.
  That is an explicit adaptation, not a byte-identical reproduction of that graph.

Research on2026-10-31 covered the public version/model descriptions, author HF
card, relevant discussions and linked graph. Civitai's gated comment threads were
not accessible; no claim is made to have read all comments. Deployment requires
the operator's license clearance, including a separate applicable EU grant where
needed; the owner's confirmation is recorded in the private rollout journal.

## Workflow and preserved controls

Native REF2VA uses the original reference, ordered metadata-aware model-only
LoRAs, Comfy Kitchen, then core `BlockSparseAttention` before SigmaShift. Both
base generation and optional refinement reuse this model. VSA parameters match
the linked example:10% keep, start0/end1, dense blocks0,1, min_tokens12288,
extra_tokens0, sink_conditioning=exact_kv_and_rows. Gate-free operation is
supported by the pinned native Comfy source; do not add missing gate weights.

Fresh defaults:8steps, er_sde/sgm_uniform, CFG1, shifts12/3,24fps,124frames.
The existing bounded canvas/auto-aspect, first-frame guide, neutral contrast,
LoRA library and strengths, seeds, audio and MP4 delivery remain available.
First-frame guidance remains an optional application extension. The old structured
prompt builder is not offered for Beta 7; entered and stored prompts are unchanged.

Optional match-source-size refinement starts at3steps/simple/CFG1/denoise0.3
and remains off unless selected. It executes only for a larger source canvas.
Its existing tiled latent-upscale implementation is not the linked RTX workflow.
All explicit settings, including1refinement step, denoise0.2 and default attention,
survive controller serialization. Old variants' defaults are unchanged.

## Runtime verification

Comfy revision73c9bad4d21e7addbe1d13bc92eee0f1431b017d and Kitchen0.2.35 already
provide VSA and gate-free H3. No dependency or GPU-resource change is needed.
Worker startup verifies compiled sparse-kernel support and executes a tiny
synthetic CUDA attention probe before downloading weights. Missing/broken kernels
fail startup. Native VSA schema availability is checked before prompt submission.
Native sparse telemetry is enabled; inspect it for the actual producer path.
The upstream recipe deliberately keeps blocks0,1 and sequences below12288tokens
dense. Output metadata records the requested recipe, not proof every layer ran
sparse. CPU graph checks do not establish GPU memory sufficiency or visual quality.

CI must pass actual native dynamic-schema/refinement contracts as runtime UID10002,
full tests, security scans and immutable image publication. Live activation must
preserve independent image work and all historical video jobs, inputs and outputs.

## Private storage and CloudFront

Mirror only the exact Beta7 file, validating full size/SHA before completing an
immutable S3 version. Preserve the four shared support artifacts. Bind the new
version through the existing narrow reader identity and signed CloudFront route;
verify206 responses, exact version/range/size and unchanged distribution/security.
Do not expose object identifiers, credentials, prompts or signed URLs in receipts.

Keep Beta5 available until the exact worker/model binding and CDN proof succeed.
Then remove only the superseded Beta5 checkpoint versions and its reader grants
under the owner's storage-replacement request. Historical jobs and saved media
remain intact; do not delete shared files or broaden storage access. Record exact
targets and terminal receipts privately. No blind replay of mirror or activation.
