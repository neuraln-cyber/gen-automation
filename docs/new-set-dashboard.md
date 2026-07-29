# New Set operator flow

Owners and administrators create a generation set at `/dashboard/new-set`.
The form lists only current compliance-registry approvals: clearly adult
fictional subjects, checkpoints, up to four ordered LoRAs, and reviewed workflow
profiles. Prompts may include current `__wildcard__` tokens; the exact wildcard
versions are frozen into the release when the form is submitted.

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
