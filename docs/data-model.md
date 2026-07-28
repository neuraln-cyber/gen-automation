# Data model and state machines

PostgreSQL stores metadata and workflow state; object storage holds every binary
asset. IDs are UUIDv7 values, timestamps are UTC, and JSONB is used for frozen
versioned configuration rather than relationships.

## Core tables

### Release intent

- `projects`: top-level ownership and defaults.
- `releases`: mutable release identity, current phase and health, desired accepted
  count, schedule, and optimistic `lock_version`.
- `release_versions`: immutable canonical specification JSON and SHA-256,
  workflow/container digest, prompt template, model manifest, and publishing plan.
- `compliance_checks`: immutable results/evidence for adult-character approval,
  real-person exclusion or consent, model licence, destination eligibility, and
  applicable jurisdictional restrictions.

Every downstream row references a specific release version.

### Administrative identity

- `admin_users`: active administrative identity, role, bounded Argon2id password
  hash, encrypted TOTP seed, replay counter, and credential/lock versions.
- `admin_sessions`: opaque session and CSRF digests, absolute/idle expiry,
  recent-authentication time, MFA state, and revocation.
- `admin_enrollments`: one-time invitation capability digest, invited identity
  snapshot, bounded expiry, enrollment-bound encrypted TOTP seed, and
  pending/consumed/revoked lifecycle.

Only one pending enrollment may exist for a normalized username. The plaintext
capability is never stored. Consuming it creates the active user and re-encrypts
the TOTP seed under user-ID AAD in the same transaction.

### Generation

- `generation_jobs`: logical job key, canonical parameters/hash, provider, state,
  priority, lease, retry schedule, and redacted failure information.
- `generation_attempts`: each remote attempt, provider ID, image digest, timing,
  request metadata, and outcome.

Logical generation jobs are unique by release version and logical key. Provider
attempts are append-only.

### Assets

- `assets`: kind, lifecycle, storage identity/version, checksum, dimensions,
  byte size, MIME type, output ordinal, perceptual hash, metadata, and retention.
- `asset_lineage`: parent/child relationship and recipe/version.

Raw masters become immutable after verification. A duplicate completion for the
same job/output with different bytes is quarantined as a conflict.

### Ranking and review

- `scoring_runs`: immutable scorer/model versions and weights.
- `asset_scores`: individual typed signals and explanations.
- `asset_rankings`: frozen aggregate score, rank, and QC disposition.
- `review_tasks`: review round, state, ranking run, assignee, and lease.
- `review_decisions`: append-only decision events.
- `release_selections`: the approved set, display order, role, and tier.

Asset storage lifecycle is independent from review. Rejecting an image does not
delete or mutate it.

### Derivatives and publishing

- `derivative_recipes`: versioned configuration/hash and code/image digest.
- `derivatives`: source, recipe version, target, logical key, state, and output.
- `publication_intents`: immutable target/configuration digest and exact frozen
  release-version/output manifest. It stores only an allow-listed secret reference,
  never provider credentials.
- `publication_inputs`: ordered derivative-output snapshots including immutable
  storage version, SHA-256, dimensions, media type, recipe, target, and role.
- `publication_approvals`: append-only approve/revoke events bound to the exact
  intent digest and optimistic lock version, with a short expiry.
- `publication_attempts`: leased execution/retry state for one approved attempt.
- `publication_steps`: ordered X upload/post or Patreon package/handoff steps.
- `publication_effect_events`: append-only start/completion evidence for every X
  provider request. An unmatched create-post start is always reconciled as
  `UNKNOWN`, never retried.
- `publication_packages`: immutable storage identity and hashes for the deterministic
  Patreon ZIP.
- `publication_reconciliations`: append-only human/provider evidence confirming an
  unknown X result present or absent.
- `publication_provider_guards`: the durable global kill switch. The migration and
  local schema bootstrap both initialize it stopped.

### Infrastructure

- `audit_events`: append-only actor, action, transition, reason, and correlation.
- `outbox_events`: transactional dispatch of background work.
- `inbox_receipts`: provider webhook/message deduplication.
- `idempotency_records`: scope/key, request hash, status, and stored response.

## Release state

```text
DRAFT -> VALIDATING -> READY -> GENERATING -> REVIEWING
                                      ^           |
                                      + REGENERATE+
                                                  |
                                              APPROVED
                                                  |
                                              RENDERING
                                                  |
                                         READY_TO_PUBLISH
                                                  |
                                             PUBLISHING
                                                  |
                                              PUBLISHED
```

Any nonterminal phase can enter `PAUSED` or `CANCELLED`. Operational condition is
tracked separately as `HEALTHY`, `WARNING`, or `BLOCKED`.

Guards:

- `READY` requires all mandatory compliance checks and a frozen specification.
- `REVIEWING` requires terminal requested generation jobs and a verified master.
- `APPROVED` requires the configured selection count and a human decision.
- `READY_TO_PUBLISH` requires every mandatory derivative to be verified.
- `PUBLISHED` requires every mandatory publication intent to succeed or an
  explicitly confirmed manual Patreon handoff.

## Generation state

```text
QUEUED -> CLAIMED -> SUBMITTING -> RUNNING -> COLLECTING -> VERIFYING -> SUCCEEDED
                         |             |
                         +----------> UNKNOWN

retryable failure -> RETRY_WAIT -> QUEUED
retry exhausted  -> FAILED
active           -> CANCEL_REQUESTED -> CANCELLED
```

`SUCCEEDED` requires the expected object count, checksum, dimensions, and storage
registration. `UNKNOWN` is reconciled with the provider before any resubmission.

## Asset state

```text
EXPECTED -> UPLOADING -> VERIFYING -> AVAILABLE -> ARCHIVED
                              |            |
                         QUARANTINED    PURGE_PENDING -> PURGED
```

Purging preserves the asset identity, checksum, metadata, and deletion audit
record.

## Review state

```text
PENDING_QC -> QC_COMPLETE -> PENDING_HUMAN -> CLAIMED
                                               |-> ACCEPTED
                                               |-> REJECTED
                                               +-> REGENERATE_REQUESTED
```

Lease expiry returns a claimed task to `PENDING_HUMAN`. Overriding a terminal
decision creates a new review round.

## Derivative state

```text
REQUESTED -> QUEUED -> PROCESSING -> VERIFYING -> READY
retryable failure -> RETRY_WAIT -> QUEUED
retry exhausted  -> FAILED
superseded       -> STALE
pre-ready        -> CANCELLED
```

A changed watermark or recipe creates a new derivative version; it never
replaces a prior object in place.

## Publication state

```text
AWAITING_APPROVAL --fresh approval--> READY -> PROCESSING -> PUBLISHED
                                            |
                                            +-> AWAITING_HUMAN (Patreon UI)
                                            |        `-> confirmed present -> PUBLISHED
                                            |
                                            +-> UNKNOWN (ambiguous X create)
                                                     |-> confirmed present -> PUBLISHED
                                                     `-> confirmed absent -> AWAITING_APPROVAL

safe pre-post/upload retry -> READY
definitive failure         -> FAILED
revocation/expired approval -> AWAITING_APPROVAL
```

An X create-post timeout, transport/5xx response, malformed success response,
process exit, lost lease, or failure to durably record a returned post is
`UNKNOWN`, not failed. Confirming absence never posts and never creates an
attempt; a new, separately approved attempt is required. Media-upload ambiguity
is retryable because unconfirmed media IDs are discarded and never reused.

## Idempotency

```text
generation =
  SHA256(release version + workflow/image digest + model/LoRA hashes +
         canonical parameters + seed + output ordinal)

derivative =
  SHA256(source checksum + recipe version + target)

publication =
  SHA256(release/version + target + configuration/copy digest +
         exact versioned derivative-output manifest)
```

The same key and request returns the committed result. Reusing a key with a
different request hash returns `409 Conflict`. State transition and outbox insert
occur in one transaction using optimistic compare-and-swap on `lock_version`.
