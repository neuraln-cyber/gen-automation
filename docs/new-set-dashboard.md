# New Set operator flow

Owners and administrators create a generation set at `/dashboard/new-set`.
The form lists only current compliance-registry approvals: clearly adult
fictional subjects, checkpoints, up to eight ordered LoRAs, and reviewed workflow
profiles. Prompts may include current `__wildcard__` tokens; the exact wildcard
versions are frozen into the release when the form is submitted.

`/dashboard/experiments/new` renders this same form and submission contract as
Experiment Lab. Its only additional behavior is the deployment-wide warm GPU
panel: the first Lab submission starts or touches the bounded warm lease, and
the status page offers a one-click path back to queue another independent batch
plan. A submission opens the default 15-minute follow-up window; operators may also
prewarm that window or explicitly start the 90-minute Focus session while iterating.
Experiment Lab has no comparison
variants or Lab-specific image-count
cap; Automation's existing form, provider-job, storage, deliverability, and
budget invariants remain authoritative.

The browser presents this as a settings-first builder. Operators choose one
shared generation profile and image-quality configuration, then queue up to 50
ordered batch cards. Every batch has its own label, image count, prompt and
wildcards, optional negative/detailer overrides, and optional starting seed.
The **Characters in each image** control supports the established single flow,
Controlled Duo, or capability-gated Controlled Trio. Duo requires two distinct
approved clearly-adult fictional subjects; Trio requires three. Trio is offered
only when an approved workflow explicitly declares `controlled_trio_v1`, and
its first contract supports Balanced isolation only.

Controlled compositions separate persistent identity/appearance, individual
pose, local negative direction, combined group interaction, and camera. A and B
have independent freeform fields; Trio adds the same fields for C. Operators
may freely describe each character's pose and a complete coordinated pose for
the pair or trio. The layout selector controls disjoint identity-region guides,
not a fixed pose menu, and it does not promise deterministic body geometry.
**Auto / flexible** leaves the most compositional freedom.

Current wildcard tokens can be inserted directly into any A/B/C identity,
pose, or negative field and into the group interaction or camera. For a
multi-character pose wildcard, each line should be one complete coordinated
pose for all participating characters. The exact wildcard versions and their
resolved generation evidence are frozen with the release.

The set-level controlled fields are reusable defaults across the ordered queue.
Each batch may override identity, pose, negative, interaction, or camera fields:
leaving **Override** off inherits the default, enabling it with text replaces
the default, and enabling it with a blank value explicitly clears the inherited
field. This lets consecutive small batches change individual or shared poses
without re-entering the approved cast or rebuilding the Automation/Lab form.
No custom extension is installed for these controls.
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

Every queue keeps the exact requested batch image counts. The controller can
place up to 25 outputs in one provider job. Jobs of up to eight outputs carry the
full signed request inline; larger jobs carry a signed, immutable reference to
the same bounded request in private object storage. The provider-facing queue
message therefore remains below 256 KiB without changing the requested image
count.

The configured final-set size is a maximum review goal, not a requirement to keep
every generated master. An open review can be completed with any non-empty accepted
subset up to that goal. Completion atomically freezes only those accepted masters
and records their actual count for both the deterministic Patreon handoff and
the extracted full-resolution MEGA folder; unaccepted and undecided raw masters
remain available in private storage.

Named settings presets are stored on the current device. They include the
approved subject or ordered multi-character cast and controlled prompts,
checkpoint, workflow, ordered LoRA stack and weights,
sampling, dimensions, shared prompt defaults, refinement controls, GPU batch
size. They intentionally exclude the run name, prompt batch queue, seed, and
final-set target so loading a style preset does not replace creative queue work.
Presets can be exported and imported as JSON for browser/device transfer.

Exact pose-map guidance is intentionally outside this release. A future opt-in
ControlNet path would need reviewed model and pose-map artifacts, explicit
signed inputs, and a separate canaried worker because it is expected to add
roughly 2.5 GB to cold-start downloads. The dashboard must not imply that this
uninstalled capability already exists.

The complete in-progress automation is also autosaved as a device-local draft,
including its ordered prompt queue. It is restored after refresh or session
interruption. A normal Automation draft is cleared after the successful status
page loads. An Experiment Lab submission instead retains the reusable profile and
prompt queue while clearing its title, slug, and submission identity, so the next
test cannot collide with or replay the queued run. Either draft can be cleared
explicitly by the operator.

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
