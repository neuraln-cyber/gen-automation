# New Set operator flow

Owners and administrators create a generation set at `/dashboard/new-set`.
The form lists only current compliance-registry approvals: clearly adult
fictional subjects, checkpoints, up to eight ordered LoRAs, and reviewed workflow
profiles. Prompts may include current `__wildcard__` tokens; the exact wildcard
versions are frozen into the release when the form is submitted.

The browser presents this as a settings-first builder. Operators choose one
shared generation profile and image-quality configuration, then queue up to 50
ordered batch cards. Every batch has its own label, image count, prompt and
wildcards, optional negative/detailer overrides, and optional starting seed.
The queue summary continuously shows total images, GPU jobs, and the final-set
target before submission. The visible **Keep best** target follows the queue by
default (up to the supported 100-image final set) and becomes independent after
the operator edits it. Unknown wildcard tokens are rejected in the batch card
before submission.

Named settings presets are stored on the current device. They include the
approved subject, checkpoint, workflow, ordered LoRA stack and weights,
sampling, dimensions, shared prompt defaults, refinement controls, GPU batch
size. They intentionally exclude the run name, prompt batch queue, seed, and
final-set target so loading a style preset does not replace creative queue work.
Presets can be exported and imported as JSON for browser/device transfer.

The complete in-progress automation is also autosaved as a device-local draft,
including its ordered prompt queue. It is restored after refresh or session
interruption and cleared only after the successful status page loads (or when
the operator explicitly clears it).

The form also freezes Clip skip (default `2`), separate face-detailer positive
and negative prompts, and the face mask feather control used as the closest
Impact Pack equivalent to ADetailer's mask blur. Width and height accept
multiples of eight while preserving the existing downstream pixel limits.
The default FaceDetailer maximum size is `1536` with crop factor `1.5`, matching
the sharper A1111 comparison preset.

One submission creates the default `main` project when the database has no
projects, creates and freezes the release specification, revalidates all
server-owned approvals, and expands the deterministic generation jobs. The
browser command is CSRF-protected and carries a signed idempotency key, so a
retry of the same form does not duplicate the release or jobs.

After a successful submission the browser opens
`/dashboard/releases/{release_id}/status`, which shows the frozen digest, plan
size, expected raw outputs, and job-state counts. No provider credential is
needed for this preparation step; queued jobs begin when the cloud GPU
controller is enabled.
