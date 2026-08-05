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
For large sets, **Build a large wildcard queue quickly** accepts one compact
`image-count wildcard-name` line per stage. For example, `50 sfw`, `100 nnsfw`,
`50 nsfw`, `20 oral`, `100 reworked`, and `20 group` creates all six batch cards
in exactly that order while copying the shared starting prompt into each one.
The queue summary continuously shows total images, GPU jobs, and the final-set
target before submission. The visible **Final set size** starts at the smaller of
the queue total or 250 images, matching the normal workflow of generating roughly
400 masters and curating about 250. Editing it makes the goal independent; **Keep
all generated** explicitly makes it follow the queue. Unknown wildcard tokens are
rejected in the batch card before submission.

The configured final-set size is a maximum review goal, not a requirement to keep
every generated master. An open review can be completed with any non-empty accepted
subset up to that goal. Completion atomically freezes only those accepted masters
and records their actual count as the exact downstream Patreon/MEGA package size;
unaccepted and undecided raw masters remain available in private storage.

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
server-owned approvals, and expands the deterministic generation jobs. The GPU
scheduler uses the frozen job ordinal after priority, so equal-priority jobs are
submitted in queue order rather than database insertion order. Ranked master
and review screens can switch between quality ranking and original generation
order; generation order uses the frozen batch index and image number. The
browser command is CSRF-protected and carries a signed idempotency key, so a
retry of the same form does not duplicate the release or jobs.

After a successful submission the browser opens
`/dashboard/releases/{release_id}/status`, which shows the frozen digest, plan
size, expected raw outputs, and job-state counts. No provider credential is
needed for this preparation step; queued jobs begin when the cloud GPU
controller is enabled.
