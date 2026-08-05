# Semantic anatomy visual-learning contract

This document fixes the data and evaluation contract for personalized anatomy
learning. The first learnable model is now an automatically trained, small CPU
meta-classifier over already-stored VLM outputs. Pixel-level visual LoRA training
remains a gated challenger: the control plane can evaluate readiness and seal a
cost-bounded, immutable dry plan, but it cannot spend or submit a RunPod job yet.

Operational sample targets below are conservative readiness gates, not accuracy
guarantees. No model becomes authoritative until a challenger passes the
group-aware holdout gates.

## Label contract

One learning sample binds all of the following:

- raw-master SHA-256 and asset ID;
- semantic profile SHA-256, assessment ID, model/revision, prompt, and schema;
- owner truth: `anatomy_good`, `anatomy_defect`, or `unjudgeable`;
- optional owner-confirmed issue code;
- VLM verdict, confidence, issue codes/confidences, and box presence;
- release set, generation job, generation time, label time, and completed-review
  status; and
- label source: explicit, inferred inspected-and-kept, or inferred anatomy reject.

Every readiness dataset is scoped to one owner and one semantic profile. Labels
from different owners are never merged into one personalized readiness result.
The current schema encodes inferred-source markers in reserved system notes; a
dedicated source column remains the preferred future migration, but the report
recognizes only the two exact reserved markers and treats every other note as
explicit.

Identical content must remain in one split. Duplicate SHA-256 content counts once
for readiness. Selection prefers explicit over inferred evidence, then an
owner-specific defect over a generic defect, then the earliest timestamp and ID.
When duplicate labels conflict, a uniquely stronger explicit evidence tier can
resolve the conflict; conflict within the strongest tier is excluded and
reported. `unjudgeable` is retained for audit and abstention analysis but is not
a binary training label.

Default-kept, uninspected images are not positive examples. The intended
low-friction positive signal is an image that was actually displayed in the
fullscreen reviewer, retained through final review, and materialized at review
completion. An explicit **Anatomy good** action remains stronger training evidence.
Plain rejection is not anatomy evidence. **Reject + anatomy label** stores a
provisional anatomy intent; the final latest decision is materialized in a batch
at review completion. A defect subtype is optional: a correct generic defect is
better than an incorrect specific code.

Review inspection is stored separately from the optimistic task lock, and label
materialization reads only the latest final decision. The readiness report counts
the resulting immutable feedback and deliberately does not reinterpret default
accepts itself.

## Readiness report

`services.semantic_learning_readiness` is read-only and provides:

- class, label-source, owner issue, issue-family, and model issue counts per
  semantic profile;
- duplicate/conflicting-content counts;
- release-set, completed-review-set, generation-batch, and UTC-time diversity;
- whether a set-grouped, batch-grouped, and chronological class-complete split is
  possible;
- the highest-value owner/model disagreements for audit; and
- separate calibration, CPU meta-classifier, and later LoRA readiness gates.

Model issue prevalence counts images containing a code once and separately
reports raw issue occurrences, since one image may contain several boxes with the
same code. True owner/model disagreements are separate from the broader audit
priority queue.

Where generation-job schema v2 metadata is complete, the report also counts
checkpoint SHA-256, ordered LoRA-stack, workflow, and composite style-stack
cohorts. The composite style identity is checkpoint + ordered LoRA SHA/weight +
workflow. Free-form artist/style prompt tags are not parsed: there is no stable
structured style field yet, so that cohort remains pending rather than relying on
fragile prompt text.

The report has a deterministic dataset digest and never starts inference or
training. Profile isolation is intentional: changing model revision, prompt, or
schema produces a different assessment profile. A later training dataset may
combine compatible profiles explicitly, but it must never do so accidentally.

## Stage 1: confidence calibration

The existing CPU/SQL calibration remains the first stage:

- at least 100 binary labels;
- at least 20 good and 20 defective examples; and
- SHA-grouped out-of-fold evaluation.

This changes only the `severe` confidence threshold. It cannot teach the VLM a
new visual pattern, use issue-specific evidence, or correct a `pass` false
negative. It is useful personalization but will plateau.

## Stage 2: CPU meta-classifier

The first trained challenger uses no new runtime dependency. Its pinned v1 input
features are:

- one-hot VLM verdict;
- assessment confidence;
- issue count, maximum issue confidence, and boxed issue count; and
- presence plus maximum confidence for every bounded anatomy issue code.

Training readiness requires at least 500 binary labels, 200 good examples, 150
defects, three completed review sets, five generation batches, and a
class-complete chronological split. Training should keep all defects and cap weak
positives to approximately two per defect. The untouched evaluation set must
preserve natural prevalence.

Training readiness is not promotion readiness. Evaluation readiness additionally
requires a chronological whole-group split leaving at least 200 good and 100
defective training examples, at least 50 holdout defects, and enough holdout good
examples that zero false rejects would have a one-sided 95% binomial upper bound
of 2% or less. With no observed false rejects this normally requires about 150
good holdout examples. The readiness report publishes the exact cutoff, class
counts, and bound. Actual challenger predictions and confidence intervals remain
mandatory before promotion.

Start with a deterministic regularized linear classifier implemented in the
training environment. Do not add a large ML framework to the control plane.
Version its feature schema, training-set digest, split manifest, hyperparameters,
and output thresholds. If a linear model cannot beat calibrated VLM confidence,
keep the existing champion.

This stage is implemented as a durable owner-scoped lifecycle. A standing policy
collects labels without further prompts, queues one idempotent challenger after
the exact readiness and retraining-delta gates pass, fits it on CPU, evaluates it
against the current champion on the frozen holdout, and records an append-only
promotion or rejection event. Training runs survive restarts and never incur GPU
cost. A promoted artifact remains advisory and reversible; the pinned VLM result
is retained as evidence.

## Group-aware split

Preferred split groups are, in order:

1. whole release sets;
2. whole generation jobs/batches; and
3. raw-master SHA-256 as the minimum leakage boundary.

Champion/challenger evaluation uses a chronological tail of whole groups. Both
training and holdout must contain good and defective images. Images from one
batch must never be divided between fit and holdout merely to satisfy a sample
count. Reported random cross-validation may supplement this test, but cannot
replace it.

## Two-threshold triage and promotion

The classifier produces an estimated defect probability and two separately
versioned thresholds:

- `p >= reject_threshold`: reversible AI reject;
- `p <= keep_threshold`: AI anatomy pass; and
- between thresholds: owner review.

Every master remains recoverable. Promotion is based on the exact same untouched
holdout for champion and challenger and records, at minimum:

- good-image false-reject rate and its 95% upper confidence bound;
- auto-reject precision;
- defect recall and F1;
- auto-keep negative predictive value and defect-leak rate;
- balanced accuracy; and
- automated coverage/manual-review fraction.

Initial operational safety targets are a good-image false-reject upper bound no
higher than 2%, auto-reject precision of at least 95%, auto-keep negative
predictive value of at least 95%, and no regression in defect recall. These are
operator-adjustable targets, not promises. A challenger must also improve either
safe coverage or a quality metric; ties retain the champion. Promotion requires
an immutable model artifact and one-step rollback.

## Active learning

Extra owner attention is spent only where it has high information value:

1. owner defect versus VLM `pass`;
2. owner good versus VLM `severe`;
3. owner good versus VLM `review`;
4. confirmed defects with VLM `review`; and
5. generic defects in an underrepresented issue family.

A small random audit sample must remain, preventing the model from learning only
its known uncertainty region. Predicted issue codes may be suggested in the UI,
but only an owner confirmation makes them owner issue truth.

## Stage 3: visual LoRA challenger

Do not fine-tune continuously or on every review. The initial operational gate is
at least 2,000 binary labels, 1,000 good examples, 500 defects, 500 explicit owner
labels, five completed review sets, ten generation batches, a promotion-capable
chronological holdout, and at least three issue families with 50 owner-confirmed
examples each.

Train a versioned LoRA challenger only in a separate GPU job after the gate is
met, then run it in shadow mode. The same champion/challenger rules apply. New
checkpoints, LoRAs, styles, multi-character compositions, or prompt distributions
are drift cohorts; they remain assisted until their audit metrics are safe.

Before any paid job can be planned, build the input contract with
`build_semantic_visual_dataset_manifest_from_database`. The manifest reuses the
readiness module's exact owner/profile dedupe, conflict-resolution, and selected
chronological whole-group holdout. Every row binds its label to the assessed raw
master's S3 bucket, key, non-null version ID, SHA-256, exact byte size, and media
type. Current asset identity must still equal the immutable semantic-assessment
snapshot or construction fails closed. Canonical asset, label, split, and full
manifest digests make replay deterministic. The manifest intentionally contains
no prompts, generation metadata beyond non-sensitive cohort digests, presigned
URLs, or credentials; building it performs no network or GPU operation.
