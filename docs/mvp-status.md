# MVP status and deployment handoff

Status date: 2026-07-28 (Europe/Sofia)

## Where the earlier run stopped

The best retained task evidence places the earlier interruption at approximately
17:31:43 local time. At that point the derivative schema work existed, but the
automatic derivative runner, durable publication orchestrator, concrete X OAuth
adapter, final verification, and deployment handoff were not complete. No crash
log from that execution is attached, so the exact termination cause cannot be
proven.

Work resumed from the existing repository rather than being restarted.

## Source implementation now complete

- FastAPI control plane, PostgreSQL workflow state, migrations 0001 through
  0013, audit records, idempotency, leases, and restart recovery.
- Owner/reviewer/publisher authentication, Argon2id, TOTP, RBAC, CSRF,
  same-origin checks, recent authentication, and private dashboard access.
- Immutable release specifications, subject/model/LoRA/workflow approvals, and
  current-approval revalidation before generation or publication effects.
- Editable, versioned Forge-style wildcard libraries with nested
  `__wildcard__` expansion and frozen per-job selection evidence.
- Private, exact-version S3 master storage with conditional writes, checksum
  verification, short-lived signed access, quarantine rather than automatic
  deletion, and a live conformance command.
- Pinned ComfyUI GPU worker, model-manifest verification, signed jobs and upload
  grants, SaladCloud queue/container orchestration, scale-to-zero configuration,
  hard daily/monthly budgets, emergency stop, and reconciliation.
- Isolated automatic quality scoring, duplicate detection, frozen ranking
  manifests, and the ranked review dashboard.
- Durable human decisions, owner-selected X images, clean member/Patreon
  derivatives, and deterministic X-only watermark/teaser rendering.
- Durable publication planning, one bounded approval per exact intent,
  revocation, restart-safe steps, default-stopped publication guard, immutable
  input snapshots, and reconciliation.
- Deterministic Patreon handoff ZIP generation and protected download. Patreon
  publishing remains a deliberate action in the official creator UI because its
  public API does not document post creation.
- Automatic X media/post execution, adult-media metadata, AI labelling,
  no-blind-retry handling for ambiguous creates, and an AWS Secrets Manager OAuth
  adapter with refresh-token rotation, restart recovery, PostgreSQL
  serialization, and exact `/2/users/me` account binding.
- CI definitions for formatting, linting, strict typing, tests, migrations,
  secret scanning, both container builds, SBOMs, vulnerability scanning, GHCR
  publication, provenance, and attestations.

There is no separate Salad support-ticket, approval-reference, or
adult-classification runtime gate. GPU allocation depends only on configured
Salad access, private storage, worker signing, budgets, the normal operator
enable switch, and a valid generation release.

## Where images are accessed

The normal operator surface is:

```text
https://<control-plane-host>/dashboard
```

It shows releases in ranked order. An authorized owner or reviewer can open or
download an exact stored version through a short-lived URL. Rejected images stay
private and recoverable under the configured retention policy.

The authoritative raw masters are in the private versioned asset bucket:

```text
masters/{release_id}/{asset_id}/{sha256}.{extension}
```

Clean member/Patreon outputs and selected-only watermarked X teasers are
separate immutable objects:

```text
derivatives/{release_id}/{release_version_id}/{job_id}/
  {recipe_id}-{recipe_config_sha256}/{source_sha256}/
  {target}/{output_sha256}.{extension}
```

Patreon handoff archives are private:

```text
publication-packages/{publication_intent_id}/{sha256}.zip
```

They are downloaded from the authenticated publication API using a short-lived
exact-version URL. Raw masters are never watermarked, moved, overwritten, or
made public.

## What remains before a live MVP

The remaining work is deployment and canary work, not missing core workflow
source:

1. Push the branch and let GitHub Actions build and verify immutable control-
   plane and worker images.
2. Provision private staging: one Linux control-plane service, PostgreSQL,
   private versioned AWS S3 asset storage, a separate model-artifact permission
   boundary, DNS/TLS, and backup/restore.
3. Run live S3 conformance and a database restore drill.
4. Bootstrap the owner, enroll TOTP, and register the approved subjects,
   Safetensors checkpoint/LoRAs, workflow, watermark, hashes, and evidence.
5. Configure SaladCloud, run a synthetic zero-publish worker check, then one
   small paid generation canary with a hard budget and maximum one replica.
6. Review and render one release through the dashboard.
7. Put the X OAuth secret in AWS Secrets Manager, authorize the exact creator
   account, and run one destination canary. Patreon needs no API credential for
   the MVP handoff; publish the generated package in Patreon's official UI.
8. Install pinned official MEGAcmd, mount a pre-authenticated writable-folder
   profile, and run one completed-set mirror canary.
9. Repeat with separate production resources and one release at a time.

The private versioned asset bucket remains the source of truth. The finished
clean Patreon ZIP can now be mirrored automatically to a configured MEGA
writable-folder profile. The controller verifies the remote byte length and
SHA-256 by downloading the exact MEGA node before it records success; see
`docs/mega-delivery.md`.

## Accounts and access, by time

Do not paste secrets, tokens, passwords, database URLs, signed URLs, recovery
codes, or private keys into chat or Git. Put them directly into the selected
deployment secret store.

| When | Account/access | Exact use |
| --- | --- | --- |
| Source publication | GitHub maintainer access and an authenticated local GitHub CLI session | Push the current branch, run Actions, publish GHCR images, and open the draft PR. No personal access token should be sent in chat. |
| Private staging | Linux container host/project, DNS record access, TLS ingress, PostgreSQL migration URL, separate PostgreSQL runtime URL | Run migrations, the control plane, authentication, dashboard, backups, and webhooks. |
| Private staging | AWS account with a private versioned S3 bucket and a narrowly scoped control-plane role | Store staging uploads, raw masters, derivatives, and publication packages; run exact-version conformance. |
| GPU canary | SaladCloud organization/project, dedicated automation API key, webhook signing secret, queue/group names, billing cap | Provision the queue and worker, allocate at most one GPU, submit jobs, reconcile cost, and scale to zero. No special workload approval reference is required by the application. |
| GPU canary | GHCR worker-image pull access if private; private model bucket/object access; model manifest; controller Ed25519 signing key | Pull the immutable worker and approved checkpoint/LoRAs without exposing the asset archive. |
| X canary | X developer Project/App, confidential Automated App/bot client ID and secret, creator refresh token, exact numeric creator user ID | OAuth scopes: `tweet.read tweet.write users.read media.write offline.access`. Store the client values and refresh token as one AWS Secrets Manager JSON secret; configure only its full ARN reference in the app. |
| Patreon MVP | Existing Adult/18+ creator account and normal creator UI access | No Patreon API key is needed. Download the deterministic package, publish/schedule it in the official UI, and record the returned post URL. |
| MEGA completed-set mirror | Dedicated MEGA account/folder with sufficient quota, official MEGAcmd, and a one-time pre-authenticated writable-folder profile volume | No MEGA password, session, folder key, or auth-key is supplied to the application. The controller stores only the path, node handle, hash, size, and status. |
| Later Patreon reconciliation | Patreon API v2 client and campaign ID | Optional read-only post reconciliation/webhooks; not required for the first MVP. |

Account creation, identity/age/tax verification, payment authorization, provider
terms acceptance, X OAuth consent, X sensitive-media settings, Patreon
Adult/18+ classification, and proof of content/model/distribution rights remain
account-owner actions.

## Final local verification

- Ruff format: applied.
- Ruff lint: passed.
- Strict mypy: passed for 116 source files.
- Pytest: 842 passed, 6 expected skips.
- Coverage: 75.01%, above the intentionally lean 75% gate.
- Alembic upgrade/schema check/downgrade tests: 3 passed.
- High-signal credential-pattern scan: no matches.
- Local Docker, `gitleaks`, PostgreSQL CLI, and GitHub CLI are not installed;
  container, Linux isolation, PostgreSQL, and full gitleaks checks therefore run
  in GitHub Actions and staging.
