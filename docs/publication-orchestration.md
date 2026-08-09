# Durable publication orchestration

The publication subsystem freezes approved derivative outputs and executes only
from durable, short-lived human authorization. Its two MVP destinations are:

- X: automatic static-image upload and post creation through the configured AWS
  Secrets Manager OAuth provider.
- Patreon: deterministic ZIP generation followed by an optional isolated,
  signed-in browser publisher using Patreon's official creator UI. The same ZIP
  remains available as a manual fallback.

MEGA delivery runs independently from Patreon. It expands the
provider-independent finished-set archive into ordinary full-resolution files,
preserves generation-queue order, and uploads them in bounded batches through a
pre-authenticated official MEGAcmd profile. A remote manifest and completion
marker are verified last. See `docs/mega-delivery.md`.
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
- dashboard captions are conservatively limited to 280 UTF-8 bytes before any
  media upload, avoiding late provider rejection on ordinary accounts;
- schedules must be timezone-aware and no more than 366 days ahead;
- a future-scheduled attempt remains unavailable until its requested post time,
  because this workflow performs the X create-post effect at that instant;
- a queued future post can be cancelled before any potentially successful or
  unresolved provider request; changing its caption or time then creates a new
  immutable intent and requires fresh approval;
- the ordered inputs must exactly match the owner's image selections frozen at
  review completion; omitted, additional, or reordered outputs are rejected;
- each upload follows the intent's frozen `adult_content` choice; text-only
  legacy intents default to `true`, while `false` skips adult-warning metadata;
- post creation sends the intent's independently frozen `made_with_ai` choice;
  generated-teaser and legacy intents default it to `true`;
- an ambiguous upload is retried with a new request and its unconfirmed media ID
  is never stored or reused;
- each provider request has append-only started/completion evidence;
- a transport error proven to occur before any create-post request bytes were
  sent is retried with bounded backoff; timeouts, ambiguous transport,
  408/429/5xx responses, malformed success responses, context-exit uncertainty,
  process loss, and durable completion failures become `UNKNOWN`;
- an unknown create is never retried automatically;
- owners can resolve an unknown X result from the dashboard as confirmed present
  or confirmed absent; confirmed-absent attempts are terminalized before any
  fresh approval is allowed;
- confirming the post present records the exact post ID and HTTPS X URL;
- confirming it absent records evidence and returns the intent to
  `AWAITING_APPROVAL`. It does not post or create a new attempt.

For Patreon:

- content and public preview inputs are JPEG/PNG derivatives only;
- a future-scheduled post is created in Patreon's UI as soon as the frozen
  intent is approved; the future `scheduled_at` remains part of the immutable
  package and Patreon performs the later publication. The attempt is never
  held until that timestamp, so the browser does not try to schedule a post at
  an already-due instant;
- paid content must exactly equal the ordered `full` output for every accepted
  `release_selection`; partial, additional, duplicated, or reordered sets are
  rejected before an intent can be frozen;
- the clean full-output encoder receives a deterministic per-release byte budget
  derived from the accepted-image count, including the duplicated public
  preview, so a rendered set cannot discover the bounded package limit only at
  publication time;
- paid content and the explicitly selected public preview must both be clean
  `full` outputs; a watermarked `x_teaser` can never enter the Patreon package;
- a named human must attest in an explicit IANA timezone that the preview has no
  nudity or sexually explicit content; the attestation must be recent;
- the ZIP is generated deterministically, written conditionally, and an existing
  object is adopted only when version, metadata, bytes, and both hashes match;
- when browser publication is disabled, the intent becomes `AWAITING_HUMAN` and
  an OWNER/PUBLISHER uses the protected package download in Patreon's UI;
- when enabled, the controller authenticates the exact package request to a
  private sidecar. A durable sidecar idempotency record prevents a duplicate
  delivery from creating a second post. Queue timing is not part of the
  request identity, so restart/recovery retains the same frozen intent,
  package digest, and sidecar idempotency key;
- a confirmed Patreon post URL/ID completes the intent. Login, 2FA/CAPTCHA, a
  missing tier, or a changed editor contract becomes `AWAITING_HUMAN`; an
  uncertain post-click outcome becomes `UNKNOWN` and is never retried blindly;
- a definite pre-submit browser failure becomes `AWAITING_HUMAN` with the
  package available. An unknown result can be confirmed present, or confirmed
  absent to open that same manual package without creating an attempt or
  invoking the sidecar.

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
5. For Patreon, let the configured sidecar publish the exact package. If it
   requests operator action, use
   `POST /publication-intents/{id}/patreon-package:download`, publish in the
   official UI, then `POST /publication-intents/{id}:confirm-present`.
6. For unknown X or Patreon outcomes, investigate externally and call either
   `:confirm-present` or `:confirm-absent` with evidence and the exact current
   digest/lock. Patreon absence switches to the manual package; X absence
   returns to a separately approved future attempt.
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
GEN_AUTOMATION_BACKGROUND_PUBLICATION_TIMEOUT_SECONDS=840
GEN_AUTOMATION_BACKGROUND_PUBLICATION_LEASE_SECONDS=1200
GEN_AUTOMATION_BACKGROUND_PUBLICATION_RETRY_BASE_SECONDS=30
GEN_AUTOMATION_BACKGROUND_PUBLICATION_RETRY_MAX_SECONDS=900
GEN_AUTOMATION_BACKGROUND_PUBLICATION_MAX_PACKAGE_BYTES=167772160
GEN_AUTOMATION_PATREON_BROWSER_PUBLISHING_ENABLED=false
GEN_AUTOMATION_PATREON_BROWSER_SIDECAR_URL=http://patreon-browser:8090/v1/publish
GEN_AUTOMATION_PATREON_BROWSER_PROFILE_REFERENCE=creator-main
GEN_AUTOMATION_PATREON_BROWSER_TIMEOUT_SECONDS=240
# Configure these together when the X destination is enabled:
# GEN_AUTOMATION_X_AUTH_MODE=oauth2
# GEN_AUTOMATION_X_OAUTH_SECRET_REFERENCE=aws-secrets-manager://<exact-arn>
# GEN_AUTOMATION_X_CREATOR_USER_ID=<numeric-id>
```

`PUBLISHING_ENABLED` only registers the bounded controller loop. The object
store, current compliance approvals, human approval, and durable guard are still
required.

## X deployment adapter

The controller injects the AWS Secrets Manager adapter when
`GEN_AUTOMATION_X_OAUTH_SECRET_REFERENCE` and
`GEN_AUTOMATION_X_CREATOR_USER_ID` are both configured. The reference must
contain one complete secret ARN and must exactly match the frozen intent.
`GEN_AUTOMATION_X_AUTH_MODE` selects `oauth2` (default rotating credential) or
`oauth1` (static owner credential read from the separate OAuth 1.0a schema).

For each OAuth 2.0 effect the adapter:

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

For OAuth 1.0a it instead reads `AWSCURRENT` without a database lock or secret
write, validates the strict static schema, signs `GET /2/users/me`, and yields
the same single-attempt transport with per-request signatures. Scheduling,
captions, adult metadata, `made_with_ai`, durable idempotency, and unknown-outcome
handling are authentication-independent.

Tokens exist only in the designated Secrets Manager secret and short-lived
process memory, never in publication tables, audits, exceptions, or logs.
SDK clients use ambient narrow IAM and are closed during application shutdown.
Patreon package/handoff execution remains independent of X credentials.

## Patreon browser deployment

The browser and signed-in profile never enter the control-plane image. Deploy
`Dockerfile.patreon-browser` as one private sidecar with persistent encrypted
profile and idempotency-state mounts, no public ingress, and outbound access
limited to Patreon and its required static hosts. Generate the controller/
sidecar authentication secret directly in the deployment secret store. The
account owner performs the one-time login, 2FA, CAPTCHA, and account verification
in a private headed session; routine runs are headless. See
`docs/patreon-browser-publisher.md`.
