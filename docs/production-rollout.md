# Production rollout

This checklist turns the development phases into explicit production gates. A
gate must be recorded as passed before the next one begins. Paid GPU capacity
and external publishing remain disabled until their individual gates pass.

## Gate 1: source and supply chain

- Merge only a reviewed commit for which formatting, lint, type checking,
  migrations, coverage, secret scanning, every container build, SBOM generation,
  and critical-vulnerability scans pass.
- Publish the control-plane, MEGA-enabled control plane, GPU worker, semantic
  gateway, and Patreon browser images to a private registry by immutable digest.
  Deployment manifests must use digests, never mutable tags.
- A successful `CI` push run on `main` publishes the tested Linux/amd64 images
  to GHCR under the commit SHA, attaches BuildKit provenance/SBOM data, and
  creates a GitHub/Sigstore attestation for each registry digest. Deployment
  still requires selecting and recording each immutable digest explicitly.
- Record the ComfyUI commit, custom-node commits, Python lock files, renderer
  version, and container digest in the deployment change.
- Keep staging and production GitHub environments separate and require approval
  for production jobs.

## Gate 2: private infrastructure

- Provision a managed PostgreSQL database with TLS, automated backups, point-in-
  time recovery, and a tested restore destination.
- Provision private, versioned S3-compatible buckets for raw masters,
  derivatives, model artifacts, and backups. Public access is blocked.
- Give the control plane only the object permissions it needs. Give GPU workers
  read-only access to approved model artifacts and narrowly scoped write access
  to generation staging keys.
- Configure bucket CORS for the exact review-dashboard origin and only the
  methods and headers used by short-lived signed URLs.
- Put all credentials in the deployment secret store. Secrets are never placed
  in Git, image layers, release specifications, logs, support tickets, or chat.
- Put the control plane behind TLS ingress with explicit trusted-proxy CIDRs,
  request/body/header limits, connection limits, timeouts, and rate limiting.

## Gate 3: operator recovery and compliance

- Run Alembic migrations as a one-off job before starting application replicas.
- Run the one-off owner bootstrap command, enroll TOTP, and confirm login.
- Use the authenticated invitation workflow to enroll a second owner. Store
  recovery material separately and test recent-authentication enforcement.
- Register current subject, checkpoint, LoRA, and workflow approvals. A subject
  approval must contain evidence that the fictional subject is unmistakably an
  adult and that commercial adult derivative and distribution rights exist.
- Run the artifact-onboarding plan against the exact mounted Safetensors,
  detector, and workflow files. Record immutable S3 version IDs, sizes, SHA-256
  digests, and the generated worker-manifest trust anchor.
- Keep generation blocked when any approval is missing, superseded, or revoked.

## Gate 4: zero-publish staging

- Start the control plane with publishing disabled and the GPU emergency stop
  engaged.
- Verify database migration parity, authentication, TOTP, RBAC, signed raw-master
  access, audit events, backup creation, and restore in staging.
- Run synthetic worker and object-store conformance tests without production
  models.
- Register a staging SaladCloud queue and container group by immutable worker
  digest.
- Configure one worker, the smallest viable GPU class, a short idle timeout, and
  conservative daily and monthly hard budgets.

## Gate 5: paid GPU canary

- Approve one lawful test release with a small, fixed output count.
- Temporarily clear the GPU emergency stop for that release only.
- Verify queue submission, model hash checks, generation, direct private upload,
  exact-version collection, usage metering, reservation release, idle shutdown,
  and remote-resource reconciliation.
- Confirm that a timeout or operator stop cannot silently resubmit work or leave
  paid capacity running.
- Re-engage the emergency stop after the canary and reconcile the provider bill
  with the local spend ledger.

## Gate 6: review and derivative canary

- Run deterministic scoring and confirm the ranking references the frozen raw-
  master object key and version.
- Review through the authenticated dashboard. Rejections and superseded
  decisions remain append-only and do not delete raw masters.
- Complete with a non-empty curated subset no larger than the configured review
  goal, then confirm the task freezes that actual count as its exact final-set
  size before rendering full-resolution and X teaser derivatives in a bounded
  CPU worker process.
- When semantic anatomy is enabled, verify that completion waits for every
  configured assessment to become completed or terminally unavailable. Confirm
  a high-confidence severe acceptance requires an explicit OWNER override and
  durable attestation.
- Verify recipe, source, watermark, renderer, Pillow, object-version, and output
  hashes. Confirm that private metadata is absent from every derivative.

## Gate 7: destination activation

- Bootstrap the Patreon browser profile in a private headed session, then remove
  operator ingress and canary the isolated headless sidecar against one low-risk
  post. Verify package authentication, durable idempotency, exact tier/public
  preview/content ordering, confirmed post identity, and manual fallback.
  CAPTCHA/2FA and account verification remain owner actions.
- Enable X only after its selected OAuth 2.0 rotating JSON or OAuth 1.0a static
  owner JSON is stored in AWS Secrets Manager, the exact mode-matched full-ARN
  reference and creator ID are configured, and the controller has narrow
  ambient IAM. Canary refresh rotation when using OAuth 2.0 and always verify
  `GET /2/users/me` binding before opening the publication guard. Every teaser
  upload receives the adult-content warning; every post is marked as
  AI-generated.
- Require a fresh human approval immediately before any external post. An
  ambiguous create-post timeout enters `UNKNOWN` and is reconciled instead of
  being blindly retried.
- If the optional MEGA destination is enabled, build `Dockerfile.mega` with the
  pinned official MEGAcmd Linux-repository URL and its independently recorded
  SHA-256. Mount one
  mode-`0700`, pre-authenticated writable-folder profile volume into exactly one
  controller replica. Canary one small finished set and require ordered
  full-resolution files, matching image/byte progress, and verified manifest
  and completion-marker control files before enabling normal deliveries. Then
  simulate a lost batch-upload response and confirm the next lease hashes and
  adopts only the ambiguous remote files without uploading duplicates. See
  `docs/mega-delivery.md`.
- MEGA remains a secondary completed-set destination. Salad S4 is temporary
  storage (30-day retention and a 100 MB object limit), not the authoritative
  archive. Use only a private, persistent S3-compatible bucket that passes the
  repository's exact-version, conditional-write, presigned-upload, and cleanup
  conformance suite; expose handoff downloads only through short-lived links.

## Gate 8: production

- Repeat the staging restore drill and paid canary using production identities
  and production hard budgets.
- Enable one release at a time until cost, acceptance rate, failure rate, and
  shutdown behavior are stable.
- Alert on authentication blocks, compliance revocations, dead letters,
  reconciliation drift, budget thresholds, stale controllers, failed backups,
  and any worker that remains active past the idle deadline.
- Rotate credentials and TOTP encryption keys on schedule while retaining only
  the key versions needed to decrypt active records.

## Required operator handoff

Before Gate 4, the operator provides access through the relevant provider or
deployment secret store—not by pasting credentials into conversation—to:

- the GitHub repository and private container registry;
- the SaladCloud organization/project, API identity, and queue;
- the managed PostgreSQL instance;
- the versioned S3-compatible storage and CORS configuration;
- DNS/TLS ingress and its trusted-proxy CIDRs; and
- later, the X developer application and Patreon creator integration.

Account creation, identity/age verification, payment authorization, accepting
provider terms, and granting third-party application consent must be completed
by the account owner.
