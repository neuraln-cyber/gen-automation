# Architecture

## System context

The control plane owns intent and workflow state. GPU workers are replaceable,
interruptible executors. Object storage owns files. External publishing systems
own only their published copies.

```text
Browser
  |
  v
Control plane API/UI ---- PostgreSQL
  |        |                  |
  |        +-- internal work -+
  |
  +---- S3-compatible object storage
  |
  +---- SaladCloud Job Queue ---- ephemeral ComfyUI GPU workers
  |
  +---- X / Patreon handoff / policy-approved archive
```

## Deployable components

### Control plane

A single Python service initially provides:

- REST API
- server-rendered review UI
- authentication and authorization
- release and job orchestration
- signed asset access
- provider reconciliation
- publication scheduling

Long-running or retryable control-plane work is claimed through PostgreSQL. This
avoids introducing Redis or a separate queue until measured load requires it.

### PostgreSQL

PostgreSQL is the source of truth for:

- release intent and frozen specifications
- generation and processing jobs
- asset identities, hashes, and locations
- review decisions and score versions
- derivative recipes and results
- publication attempts and remote identifiers
- outbox events and audit records

Large binary data is never stored in PostgreSQL.

### Object storage

Private S3-compatible storage is the source of truth for:

- model and LoRA artifacts
- immutable raw masters
- review proxies and thumbnails
- approved masters
- destination derivatives and archives
- immutable workflow and metadata snapshots

Objects are addressed using stable UUIDs and content hashes. Human-readable
release names are metadata, not uniqueness boundaries.

### GPU worker

The GPU image contains code and pinned custom nodes, but not long-lived secrets
or the production model library. At startup it:

1. obtains short-lived access to the required artifacts;
2. downloads and verifies the declared model manifest;
3. starts ComfyUI API;
4. executes a warm-up workflow;
5. reports readiness;
6. claims generation jobs through the Salad Job Queue;
7. uploads each master and metadata before acknowledging completion.

### Publishing adapters

Each destination implements a narrow adapter:

- `XPublisher`
- `PatreonHandoff`
- an optional archive adapter enabled only after the provider approves the
  exact workload in writing

Adapters accept a frozen publication plan and return a durable remote reference
or a deterministic prepared handoff. They do not decide which assets are
approved.

## Request and event flow

1. An operator creates a draft release.
2. Validation checks adult-only subject allowlists and model/LoRA licenses.
3. Approval freezes a versioned release specification.
4. The control plane expands it into idempotent generation jobs.
5. SaladCloud capacity is started and jobs enter its Job Queue.
6. Workers generate and upload immutable masters.
7. The [automatic quality runtime](quality-scoring.md) reads exact-version
   masters sequentially, stages isolated deterministic signals, and atomically
   freezes a ranked review queue.
8. Human review records decisions without moving or altering masters.
9. Release approval freezes destination-specific derivative recipes.
10. The [automatic derivative runtime](derivative-rendering.md) claims one
    frozen selection at a time, renders inside a bounded child, conditionally
    writes immutable checksum-addressed outputs, and atomically registers their
    assets and lineage.
11. Publishing adapters upload or prepare handoff packages.
12. Reconciliation verifies remote state and the control plane records completion.

## Idempotency

Every externally visible command has an idempotency key derived from stable
identities, for example:

```text
generation:{release_version_id}:{ordinal}
derivative:{master_asset_id}:{recipe_version_id}
publication:{release_version_id}:{destination}:{publication_plan_version}
```

Retries return the previously committed result when the key already succeeded.
External remote IDs are recorded before a job is acknowledged.

## Failure model

- GPU nodes can disappear at any time.
- Job delivery can occur more than once.
- Webhooks can be duplicated, delayed, forged, or reordered.
- Storage upload can succeed while database registration fails.
- Publication can succeed while the response is lost.
- Provider APIs can rate-limit or partially fail.

All workflows therefore use at-least-once execution, idempotent effects,
reconciliation loops, bounded retries, and dead-letter states.

## Scaling posture

The initial target is a single creator:

- one small control-plane instance
- one PostgreSQL instance
- zero GPU workers while idle
- one GPU worker during normal batches
- optional second worker for backlog

The API remains stateless so another control-plane replica can be added later.
