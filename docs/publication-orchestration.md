# Durable publication orchestration

The publication subsystem freezes approved derivative outputs and executes only
from durable, short-lived human authorization. Its two MVP destinations are:

- X: automatic static-image upload and post creation through the configured AWS
  Secrets Manager OAuth provider.
- Patreon: deterministic ZIP generation followed by a human publish/schedule
  action in Patreon's official creator UI.

MEGA delivery is an optional automatic downstream mirror of the Patreon ZIP.
It uses a pre-authenticated official MEGAcmd writable-folder profile, verifies
the remote ZIP by full SHA-256 download, and stores only an opaque node handle.
See `docs/mega-delivery.md`.
MEGA is a secondary completed-set destination; the private object store remains
the source of truth for masters, derivatives, and publication packages.

## Safety boundary

Planning snapshots the current `READY_TO_PUBLISH` release and current release
version, the exact ordered derivative outputs, immutable object versions and
hashes, target, schedule, copy/configuration digest, and current subject/model/
LoRA/workflow approval registry. There is one logical intent per release version,
target, and configuration digest.

Planning, approval, revocation, and every external-effect boundary revalidate:

1. the release/version is still current and publishable;
2. its canonical specification digest still matches;
3. all current compliance registry approvals remain valid;
4. the latest publication approval matches the exact intent digest and lock
   version and has not expired; and
5. the durable global publication guard is enabled at the expected epoch.

The settings flag only starts the runner. It cannot bypass the database guard.
The migration and `create_all` path initialize that guard stopped. Enabling it
requires an authenticated OWNER, same-origin request, CSRF proof, recent
authentication, and the current epoch/lock version.

## Durable execution

Each approval creates a new attempt and ordered steps. Attempts are leased, and
expired leases are recovered on restart. Provider errors and audit data contain
only bounded safe codes/details; configuration stores an allow-listed secret
reference such as `aws-secrets-manager://...`, never a token.

For X:

- only one to four `x_teaser` derivatives may be used;
- the ordered inputs must exactly match the owner's image selections frozen at
  review completion; omitted, additional, or reordered outputs are rejected;
- each upload includes the adult-content metadata supported by the transport;
- an ambiguous upload is retried with a new request and its unconfirmed media ID
  is never stored or reused;
- each provider request has append-only started/completion evidence;
- create-post transport errors, timeouts, 408/429/5xx responses, malformed
  success responses, context-exit uncertainty, process loss, and durable
  completion failures all become `UNKNOWN`;
- an unknown create is never retried automatically;
- confirming the post present records the exact post ID and HTTPS X URL;
- confirming it absent records evidence and returns the intent to
  `AWAITING_APPROVAL`. It does not post or create a new attempt.

For Patreon:

- content and public preview inputs are JPEG/PNG derivatives only;
- paid content must exactly equal the ordered `full` output for every accepted
  `release_selection`; partial, additional, duplicated, or reordered sets are
  rejected before an intent can be frozen;
- paid content and the explicitly selected public preview must both be clean
  `full` outputs; a watermarked `x_teaser` can never enter the Patreon package;
- a named human must attest in an explicit IANA timezone that the preview has no
  nudity or sexually explicit content; the attestation must be recent;
- the ZIP is generated deterministically, written conditionally, and an existing
  object is adopted only when version, metadata, bytes, and both hashes match;
- the intent becomes `AWAITING_HUMAN`;
- an OWNER/PUBLISHER downloads the signed package, publishes in Patreon's
  official UI, and records the exact Patreon post ID and HTTPS URL.

## Operator API sequence

All routes are under `/api/v1`. Mutation routes require OWNER/PUBLISHER unless
noted, same-origin, CSRF, and recent authentication.

1. `POST /publication-intents` — freeze one X or Patreon intent.
2. `GET /publication-intents/{id}` or
   `GET /releases/{release_id}/publication-intents` — inspect digest, inputs,
   approvals, attempts, steps, package, and reconciliation history.
3. `POST /publication-intents/{id}:approve` — approve the exact current digest
   and lock version for a bounded lifetime.
4. `GET /publication-guard`, then OWNER-only `POST /publication-guard` — enable
   with the current epoch/lock version after deployment checks.
5. For Patreon,
   `POST /publication-intents/{id}/patreon-package:download`, publish in the
   official UI, then `POST /publication-intents/{id}:confirm-present`.
6. For unknown X outcomes, investigate externally and call either
   `:confirm-present` or `:confirm-absent` with evidence and the exact current
   digest/lock.
7. `POST /publication-intents/{id}:revoke` — append a revocation and stop any
   not-yet-started effect. OWNER-only guard disable is the global emergency stop.

Idempotency keys are mandatory for mutation commands. Reusing a key with the
same canonical request returns the stored result; changing the request returns a
conflict.

## Runtime configuration

```text
GEN_AUTOMATION_BACKGROUND_RUNTIME_ENABLED=true
GEN_AUTOMATION_STORAGE_ENABLED=true
GEN_AUTOMATION_PUBLISHING_ENABLED=true
GEN_AUTOMATION_BACKGROUND_PUBLICATION_TIMEOUT_SECONDS=300
GEN_AUTOMATION_BACKGROUND_PUBLICATION_LEASE_SECONDS=600
GEN_AUTOMATION_BACKGROUND_PUBLICATION_RETRY_BASE_SECONDS=30
GEN_AUTOMATION_BACKGROUND_PUBLICATION_RETRY_MAX_SECONDS=900
GEN_AUTOMATION_BACKGROUND_PUBLICATION_MAX_PACKAGE_BYTES=167772160
```

`PUBLISHING_ENABLED` only registers the bounded controller loop. The object
store, current compliance approvals, human approval, and durable guard are still
required.

## X deployment adapter

The controller injects the AWS Secrets Manager adapter when
`GEN_AUTOMATION_X_OAUTH_SECRET_REFERENCE` and
`GEN_AUTOMATION_X_CREATOR_USER_ID` are both configured. The reference must
contain one complete secret ARN and must exactly match the frozen intent.

For each effect the adapter:

1. takes a bounded PostgreSQL advisory transaction lock derived from the exact
   reference;
2. reads `AWSCURRENT`, recovers any interrupted
   `GEN_AUTOMATION_PENDING` rotation, and refreshes a missing or near-expiry
   access token;
3. writes the new credential as a pending version and promotes it with an
   explicit `AWSCURRENT` compare-and-swap;
4. releases the credential lock before publication I/O;
5. calls `GET /2/users/me`, retains only the verified numeric ID, and fails
   closed if it differs from the configured creator ID; and
6. yields the existing single-attempt X transport.

Tokens exist only in the designated Secrets Manager secret and short-lived
process memory, never in publication tables, audits, exceptions, or logs.
SDK clients use ambient narrow IAM and are closed during application shutdown.
Patreon package/handoff execution remains independent of X credentials.
