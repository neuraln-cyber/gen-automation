# New Set operator flow

Owners and administrators create a generation set at `/dashboard/new-set`.
The form lists only current compliance-registry approvals: clearly adult
fictional subjects, checkpoints, up to eight ordered LoRAs, and reviewed workflow
profiles. Prompts may include current `__wildcard__` tokens; the exact wildcard
versions are frozen into the release when the form is submitted.

The form also freezes Clip skip (default `2`), separate face-detailer positive
and negative prompts, and the face mask feather control used as the closest
Impact Pack equivalent to ADetailer's mask blur. Width and height accept
multiples of eight while preserving the existing downstream pixel limits.

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
