# RunPod visual-LoRA training boundary

`services.semantic_visual_lora_training` defines the paid-training boundary for
the personalized anatomy model. It currently creates a sealed **dry plan** only.
It does not import a RunPod client, read a RunPod key, make a network request,
reserve money, create an endpoint, or start a GPU.

This separation is deliberate. Reviews can accumulate training data immediately,
while paid training remains impossible until the dataset is useful and the
provider lifecycle is durable.

## Admission

The plan builder loads or accepts the existing per-owner, per-semantic-profile
readiness report and fails closed unless both of these gates pass:

- the visual-LoRA gate: at least 2,000 unique binary labels, 1,000 good, 500
  defective, 500 explicit labels, five completed sets, ten generation batches,
  at least 300 owner-confirmed defect subtypes, three sufficiently represented
  issue families, and a chronological split; and
- the promotion-evaluation gate: an untouched chronological holdout large enough
  to measure false rejection conservatively.

Admission also requires the persisted owner standing policy to belong to the
same owner, have both `learning_enabled` and `auto_train_visual` enabled, and set
`max_visual_run_microusd` at or above the sealed plan's maximum cost. That policy
is the durable authorization for unattended runs. There is no per-run prompt or
fresh confirmation. Missing, disabled, mismatched, or underfunded policy facts
fail closed before a provider operation can exist.

The exact owner and profile must occur once. The dataset counts and readiness
dataset digest must still equal the current report. A stale export cannot be
trained after the owner adds or corrects labels; it must be exported and planned
again under a new digest.

The dataset also names the exact assessment model and immutable model commit.
Those values must reproduce the semantic-profile digest and must match the base
model consumed by the trainer.

## Immutable request

The request binds all of these values:

- owner, semantic profile, current readiness dataset digest, exported label
  manifest digest, and exact class/source/issue-coded-defect counts;
- group key, UTC cutoff, class counts, and digest of the chronological split;
- versioned S3 identities, byte digests, exact sizes, and media types for the
  dataset archive, split manifest, and training-recipe manifest;
- trainer contract, base-model commit, and an OCI trainer image pinned with
  `@sha256:`;
- ordered acceptable RunPod GPU types, exactly one GPU and worker, a zero-worker
  floor, a hard runtime, an hourly micro-USD ceiling, and its ceiling-rounded
  total micro-USD envelope; and
- an `if_absent` Safetensors output destination that is always a shadow
  challenger, cannot alter review decisions, and cannot auto-promote.

No presigned URL, API key, bearer token, or other credential is part of the
request identity. The canonical request SHA-256 produces the deterministic
idempotency key `visual-lora-v1:<request-sha256>`. Any identity or setting change
therefore creates a different request instead of silently reusing a training
result.

`RunPodVisualLoraTrainingPlan` explicitly reports:

```text
mutates_runpod = false
provider_spend_started = false
provider_submission_available = false
requires_durable_idempotency_claim = true
requires_persisted_standing_policy = true
per_run_confirmation_required = false
```

The plan records the admitted standing-policy lock version and cap for audit,
but the canonical training request remains tied to training inputs and limits.
Changing only a policy version does not create a duplicate model-training
identity.

## Required dataset exporter

Before the first paid run, a deterministic exporter should create the three
immutable input objects. It must use the same deduplication selected by the
readiness service, include binary labels only, preserve raw-master SHA-256, keep
whole release/job groups in one side of the split, and write objects with
`write-if-absent`. Generic anatomy defects remain useful binary evidence for
calibration, CPU meta-classification, and evaluation, but they are not subtype
targets for visual SFT. Only owner-confirmed issue-coded defects may populate
those subtype targets. After each write the exporter must read back the object
version, size, and SHA-256 before passing the identity into this planner.

The label manifest should contain every selected feedback, assessment, asset,
group, source, truth, and optional owner issue identity. The split manifest
should contain the ordered training and holdout asset SHA-256 lists. The recipe
manifest should contain all hyperparameters, package versions, random seeds,
normalization rules, target modules, and output format. The control plane need
not install a machine-learning framework to validate their identities.

## Persistence before provider submission

A later migration should add a training-request ledger rather than submitting
directly from an HTTP route. Recommended persisted fields are:

- request UUID, owner UUID, semantic profile SHA-256, readiness dataset SHA-256,
  split-manifest SHA-256, canonical request SHA-256, and unique idempotency key;
- canonical request JSON and state: `planned`, `submitting`, `submitted`,
  `running`, `succeeded`, `failed`, `outcome_unknown`, or `cancelled`;
- maximum runtime, hourly and total micro-USD caps, budget reservation identity,
  provider job ID, provider status/version, and timestamps; and
- output object version, SHA-256, size, evaluation-report identity, challenger
  state, and failure/reconciliation detail.

The transaction must re-read the current persisted policy, verify that learning
and automatic visual training remain enabled, verify its cap still covers the
plan, claim the idempotency key, and reserve the RunPod budget before any provider
POST. This is an automatic policy check, not a user prompt. Automatic mutation
retries remain zero. If the POST outcome is ambiguous, store `outcome_unknown`
and reconcile by the immutable request identity; never send a blind duplicate
training job. Release unused budget after a terminal state and hard-cancel at the
request's runtime or cost ceiling.

## Artifact validation and promotion

A successful provider status is insufficient. Download the exact output object,
verify its version, SHA-256, maximum size, and Safetensors structure, then register
it as an untrusted shadow challenger. Evaluate champion and challenger on the
sealed holdout and persist the metrics described in
`semantic-visual-learning-contract.md`.

Promotion remains a separate owner-visible operation with one-step rollback.
Until that operation exists and passes its gates, the adapter stays in shadow
mode and has no authority over keep/reject decisions.

## Public service surface

The main entry points are:

- `evaluate_visual_lora_training_gate` for a read-only UI/status check;
- `evaluate_visual_lora_standing_policy_admission` for pure unattended-policy
  admission without a database write or provider call;
- `build_runpod_visual_lora_training_plan` for a report already in memory;
- `build_runpod_visual_lora_training_plan_from_database` for a read-only current
  owner/profile check;
- `maximum_visual_lora_cost_microusd` for the exact bounded cost envelope; and
- `require_matching_visual_lora_replay` for a future durable-ledger replay.

The remaining exported strict/frozen models describe dataset, split, input
artifact, trainer, limits, shadow output, request, and plan identities. The module
contains no submission function by design.
