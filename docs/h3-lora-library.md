# H3 LoRA file library

Open **Image to video → Manage H3 LoRA files**, or
`/dashboard/loras?library=h3`. The image LoRA library is unchanged at
`/dashboard/loras`.

The owner/admin can upload a local `.safetensors` file, record its source,
license and trigger words, watch verification, search the library, delete a
file, or restore it while its bytes have not yet been purged. Existing rights
attestations and file-format/upload safety bounds remain in force. No example
LoRAs are bundled or automatically imported.

Browser uploads go directly to the existing private S3 quarantine namespace.
CPU verification checks the file structure, LoRA tensor markers, size and full
SHA-256, then registers the exact immutable object version. No GPU starts and
no new bucket, service, delivery route or public access is introduced. Stored
bytes contribute to the existing model-bucket storage bill; CloudFront does
not include S3 storage.

This release provides **file management only**. A verified library file is not
a guarantee of H3 architectural compatibility and is not automatically added
to a video workflow. Video LoRA application/strength controls are unchanged.
The video worker and generation queue are not modified by library operations.
H3 entries are excluded from all image-model manifests and selectors.

Deletion removes an entry from availability immediately and schedules an
exact-version purge. Queued/running video or image references prevent purge.
Generated images/videos are never deletion targets. Once purged, restore is
unavailable: the owner must upload the file again. The registry/audit record
remains, and retiring a file without choosing storage cleanup keeps its bytes.

Deployment uses migration `20260925_0043`, which widens only the artifact-family
constraint, not image workflow families. Downgrading refuses to remove the H3
family while any H3 artifact records exist. No model-file or GPU-worker rollout
is needed. Tests use synthetic in-memory files, never production uploads or
generation jobs.
