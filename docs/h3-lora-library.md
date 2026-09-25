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

## Select files for a video

In **Image to video → Your H3 LoRAs**, refresh the library, select the files and
set each model strength (1.0 by default; follow the file author's guidance).
Search filters names and trigger words. **Add trigger words** is optional and
changes the prompt only when clicked. **Clear selections** restores the baseline.
Drafts, presets and reused jobs retain selections and strengths. There is no new
selection-count or storage quota. Only your active, verified H3 files are offered.

Each job freezes the artifact ID, SHA-256 and strength. At dispatch the server
issues fresh private grants for those exact stored versions. The Salad worker
downloads only selected nonzero-strength files, verifies size and SHA-256 and
reuses its local content-addressed cache. Native model-only LoRA loaders are
chained before H3 sigma shift, in selection order; a fresh graph is built per job.
No file is automatically selected, and no new delivery infrastructure is created.

Verification of file structure is not a guarantee of H3 architectural compatibility.
The worker rejects a nonzero LoRA that patches no model weights instead of silently
generating without it. Output quality still requires the owner's generation test.
Library operations do not start a GPU. H3 entries remain excluded from image-model
manifests and selectors; WAN adapters are not offered in the H3 selector.

Deletion removes an entry from availability immediately and schedules an
exact-version purge. Queued/running video or image references prevent purge.
Generated images/videos are never deletion targets. Once purged, restore is
unavailable: the owner must upload the file again. The registry/audit record
remains, and retiring a file without choosing storage cleanup keeps its bytes.

Deployment uses migration `20260925_0043`, which widens only the artifact-family
constraint, not image workflow families. Downgrading refuses to remove the H3
family while any H3 artifact records exist. Generation selection additionally
requires the worker with `GenAutomationH3` installed and
`GEN_AUTOMATION_I2V_H3_LORAS_ENABLED=true` on the control plane. Keep this gate
false until the matching immutable worker image/source identity is configured;
the H3 profile, library manager and queue must already be enabled. Legacy WAN
LoRA flags remain false. No base-model manifest change is needed. Tests use
synthetic in-memory files, never production uploads or generation jobs.
