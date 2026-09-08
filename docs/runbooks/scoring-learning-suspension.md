# Scoring and anatomy learning suspension

## Suspended mode

Set these explicit values in `/etc/gen-automation/control-plane.env`:

```dotenv
GEN_AUTOMATION_QUALITY_SCORING_ENABLED=false
GEN_AUTOMATION_SEMANTIC_ANATOMY_ENABLED=false
GEN_AUTOMATION_SEMANTIC_LEARNING_ENABLED=false
```

Keep `anatomy` out of `COMPOSE_PROFILES` in `/etc/gen-automation/deploy.env`.
The semantic gateway is opt-in via that Compose profile and is no longer a
mandatory dashboard dependency. Stop any previously started gateway explicitly;
changing a profile alone does not stop an existing container.

With these flags off, the controller installs no quality, anatomy, or learning
loops. Learning dashboard routes return 404 and its navigation is hidden. Review
pages skip anatomy/learning queries and controls. New review manifests are created
from verified asset metadata in generation order, with no object-store reads,
image analysis, duplicate detection, or external model calls. They use an explicit
`skipped` state and display **Not scored**, not a fabricated quality percentage.
Existing frozen rankings are reused without modification. Generation, collection,
manual selection, watermarking, derivatives, and delivery remain enabled.

Stop upstream queued anatomy requests after stopping submissions; verify both
queued/in-progress jobs and active workers are zero. Do not delete stored images,
labels, model artifacts, network volumes, or database history to suspend compute.
Shared EC2/RDS/storage still serve the dashboard; disabling these features does
not eliminate their fixed charges.

## Recovery and re-enabling

Pre-change source revision: `61bb069abe74e4e6c01e67096ff16e9f52007f03`.
Local Git bundle: `D:\Code\model-staging\scoring-suspension-20260909\pre-suspension.bundle`.
Root-only live config and image-pin backup:
`/var/lib/gen-automation/backups/scoring-suspension-20260909`.
The unrelated pending tag-autocomplete change is saved separately in that local
folder and is not part of this deployment.

Preferred restoration is configuration-only on the current compatible code:

1. Check the upstream endpoint budget/scaling and review the paused backlog before
   enabling anatomy; thousands of old queued assessments may otherwise resume.
2. Add `anatomy` to `COMPOSE_PROFILES` in `deploy.env`, retaining any other profiles.
   Start the gateway and verify readiness before enabling anatomy requests.
3. Turn on the desired flags above and recreate the control plane using the normal
   idle-checked deployment procedure. Restore learning policy only if desired.
   Re-enabling quality scoring may score still-open, previously unscored sets;
   review that backlog before resuming it too.
4. Verify only the requested loops resume and check provider billing/queue health.

Migration `20260909_0042` adds the explicit unscored state and preserves the frozen
ranking integrity guards. **Do not downgrade to old code after manual-review
snapshots exist.** They are valid history, not disposable cache. The migration
refuses destructive downgrade in that case. Re-enabling the features does not
require a schema downgrade or deleting these snapshots.

Before any full-version disaster recovery, restore matching database and image
snapshots together, with an explicit plan for work created since the backup.
