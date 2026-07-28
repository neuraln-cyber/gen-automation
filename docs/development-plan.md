# Development plan

This plan is ordered so that each phase produces a testable vertical capability
and later integrations do not dictate the core data model.

## Phase 0 — Foundation

1. Establish the repository boundary and clean baseline.
2. Record architecture, trust boundaries, deployment contract, and decisions.
3. Define release, generation, asset, review, derivative, and publication state
   machines.
4. Establish configuration and secret-handling rules.

Acceptance criteria:

- The system boundary and sources of truth are unambiguous.
- No provider credentials are required to run unit tests.
- External integrations are behind explicit interfaces.

## Phase 1 — Control plane

1. Bootstrap the Python package and locked dependency set.
2. Add typed configuration with fail-closed production validation.
3. Add FastAPI application lifecycle, structured logging, and health endpoints.
4. Add PostgreSQL models and Alembic migrations.
5. Implement release specifications and immutable generation manifests.
6. Add the internal outbox/job mechanism and idempotency keys.
7. Add unit, integration, migration, and configuration tests.

Acceptance criteria:

- A release can be created, validated, persisted, and read through the API.
- Invalid or incomplete production configuration prevents startup.
- Retried commands do not create duplicate releases or jobs.

## Phase 2 — Asset storage

1. [Complete] Implement the S3-compatible storage adapter.
2. [Complete] Define controller-owned object keys and metadata.
3. [Complete] Add size-limited presigned uploads and version-pinned reads.
4. [Complete] Register assets only after isolated decode and checksum
   verification.
5. [Complete] Add conditional immutable promotion, retry adoption, verification
   leases, audit events, and staging cleanup tracking.
6. [Pending provider selection] Run the storage conformance suite against the
   production S3-compatible service and configure lifecycle rules.

Acceptance criteria:

- A master can be uploaded once and retrieved using a temporary signed URL.
- A changed file cannot overwrite an existing master under the same identity.
- Database and object-store reconciliation identifies orphaned records/files.

## Phase 3 — GPU worker

1. Pin the ComfyUI API base image and all custom-node revisions.
2. Implement startup model-manifest download and SHA-256 verification.
3. Build the Illustrious workflow adapter.
4. Add worker readiness, warm-up, job execution, and completion callbacks.
5. Upload masters and metadata directly to object storage.
6. Add synthetic smoke workflows that do not require production models.

Acceptance criteria:

- A submitted specification produces registered masters without using local
  persistent GPU storage.
- Duplicate delivery of a job is safe.
- Interrupted jobs are retried without duplicating masters.

## Phase 4 — SaladCloud orchestration

1. Implement SaladCloud authentication and API client.
2. Create or update the worker container group from versioned configuration.
3. Attach and submit to a Salad Job Queue.
4. Scale/start workers when jobs exist and stop them after the idle window.
5. Reconcile remote container/job state with local state.
6. Add budget, concurrency, and circuit-breaker controls.

Acceptance criteria:

- A queued release can bring up GPU capacity, complete, and release capacity.
- Provider failures are visible and recoverable.
- The configured daily/monthly budget cannot be exceeded silently.

## Phase 5 — Quality control and review

1. Add integrity, dimension, blank-image, blur, and perceptual-duplicate checks.
2. Add pluggable aesthetic, prompt-alignment, face, and anatomy scorers.
3. Store score versions and ranking explanations.
4. Build the authenticated review dashboard.
5. Add keyboard-first accept, reject, compare, regenerate, and variation actions.
6. Keep rejected files quarantined under a configurable retention policy.

Acceptance criteria:

- Reviewers can rank and process an entire release without downloading files.
- Rankings are reproducible and can be recalculated without changing masters.
- No automated score can permanently delete an asset.

## Phase 6 — Derivatives and publishing

1. Implement immutable derivative recipes.
2. Add watermark, resize, crop, blur/obscure, contact-sheet, and archive steps.
3. Use the private versioned asset archive plus checksum-verified handoff
   packages and mirror completed clean packages through the official MEGAcmd
   adapter.
4. Add X media upload and scheduled post creation.
5. Generate Patreon-ready copy, tier mapping, attachments, and publication
   checklist.
6. Add publication attempts, retries, remote IDs, and reconciliation.

Acceptance criteria:

- Re-running a derivative or publication command is idempotent.
- Raw masters are never modified.
- An approved release produces every configured destination artifact.

## Phase 7 — Production hardening and deployment

1. Add administrator authentication, session controls, and optional TOTP.
2. Add metrics, traces, error reporting, audit exports, and operational alerts.
3. Add encrypted backups and test restoration.
4. Add container vulnerability and secret scanning in CI.
5. Deploy staging, execute failure drills, and run an end-to-end pilot release.
6. Deploy production and document operating and recovery procedures.

Acceptance criteria:

- A database and asset backup can be restored into a clean environment.
- Key rotation and provider outage procedures have been tested.
- The pilot release completes without manual file movement.

## Deferred experiment system

After production publishing is stable:

1. Add experiment definitions and factorial/Bayesian parameter selection.
2. Generate paired comparisons rather than unrelated grids.
3. Capture human preference signals and cost/latency telemetry.
4. Optimize accepted-images-per-dollar subject to quality constraints.
5. Promote an experiment configuration to a versioned production recipe.
