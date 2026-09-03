# MVP status and deployment handoff

Status date: 2026-08-02 (Europe/Sofia)

## Current conclusion

The AWS staging control plane is live at
`https://gen-automation-staging.18-198-84-215.sslip.io`. Its PostgreSQL schema
is migrated through `0015`, the initial owner is enrolled with TOTP, all four
runtime containers are healthy, and the public HTTPS readiness canary passes.
The live S3 conformance suite passes, including exact-version cleanup and the
no-redirect multipart POST used by GPU workers. An encrypted RDS snapshot was
restored into a private temporary clone; its migration revision, 46-table
schema digest, and active-owner count matched the live database before all
drill resources were removed. A final OpenTofu plan reports no drift.
Application secrets were generated only on the host. The approved checkpoint,
LoRAs, detector, and workflows are onboarded with exact identities, and live
SaladCloud generation has produced registered private masters. Patreon, MEGA,
and X publication effects remain disabled until their individual credentials
and canaries are complete.

The credential-free source implementation is complete. Final end-to-end MVP
acceptance still requires semantic anatomy verification, review/derivative
checks, and provider-specific publication canaries. No historical test count is
used as a completion claim.

## Source implementation complete

- FastAPI control plane, PostgreSQL workflow state, migrations through `0015`,
  durable audit/idempotency records, leases, retries, restart recovery, and
  default-stopped external effects.
- Owner/reviewer/publisher authentication with Argon2id, TOTP, RBAC, CSRF,
  same-origin enforcement, recent-authentication checks, and private dashboard
  access.
- Immutable release specifications and current compliance approvals for
  subjects, checkpoints, LoRAs, workflows, watermarks, and publication effects.
- Editable, versioned Forge-style wildcard libraries with nested
  `__wildcard__` expansion and frozen per-job selection evidence.
- ComfyUI workflow profiles for the base Illustrious flow, hires fix, and face
  detailer, with an allowlisted workflow contract. Checkpoints, LoRAs, detector
  artifacts, and workflow definitions can be added or changed through the
  onboarding plan instead of changing controller code.
- One-off artifact onboarding that validates local Safetensors/detector files,
  verifies exact versioned model objects and SHA-256 metadata, validates
  allowlisted ComfyUI workflows, records audited approvals, and emits the
  canonical worker manifest plus its separate digest.
- Private, versioned S3 master storage with conditional writes, checksums,
  exact-version reads, short-lived signed access, quarantine, and a live
  conformance command.
- SaladCloud queue/container orchestration, signed jobs and upload grants,
  immutable worker/model manifest verification, scale-to-zero controls, cost
  limits, emergency stop, and reconciliation.
- Deterministic CPU quality scoring, duplicate detection, frozen ranking, and a
  ranked review dashboard.
- A private semantic gateway image and strict, identity-bound anatomy assessment
  contract. When semantic anatomy is enabled, review cannot complete until each
  image has a terminal result or explicit terminal `unavailable` state. An
  accepted high-confidence severe result requires an authenticated OWNER,
  `semantic_severe_override`, and a written audited justification.
- Dynamic deliverability enforcement across generation, review, derivatives,
  and publication: at most 500 accepted images, post-hires masters bounded to
  8192 by 8192 and 12 million pixels. Newly prepared X outputs retain those
  admitted source dimensions as metadata-free watermarked PNGs: compression
  level 6 first, then level 9 as a lossless size optimization. A PNG still over
  X's 5 MiB ceiling fails terminally before upload with no JPEG or downscale
  fallback; renderer v6 owns this behavior while frozen renderer v4/v5 and
  legacy JPEG recipes remain executable with their original semantics.
- Durable human decisions, clean unwatermarked member/Patreon derivatives, and
  owner-selected X-only watermark/teaser rendering. Raw masters remain
  unchanged.
- Durable publication planning with one bounded approval for exact immutable
  inputs, revocation, restart-safe steps, outcome reconciliation, adult-media
  metadata, and AI labelling.
- Automatic X OAuth/media/post execution with exact creator binding and
  Secrets Manager refresh-token rotation.
- An isolated Patreon Playwright/Chromium sidecar that authenticates controller
  requests with a deployment-injected shared secret and stores durable
  idempotency state separately from its persistent signed-in browser profile.
  It fails closed on login, CAPTCHA/2FA, UI drift, and ambiguous outcomes. The
  deterministic ZIP download and manual official-UI handoff remain available
  as the fallback.
- A pinned MEGAcmd-enabled controller image and restart-safe, explicitly requested
  delivery of the accepted full-resolution files in generation order. Images are
  sent in bounded batches, the outward folder contains image files only, and an
  exact final remote listing is recorded in the database; the private asset
  bucket remains the source of truth.
- Reproducible AWS staging OpenTofu under `infra/aws-staging`: default
  `eu-central-1`, SSM-only EC2, EIP and 80/443 ingress, private RDS PostgreSQL,
  separate versioned asset/model buckets, encrypted root and integration-profile
  volumes, least-privilege IAM, optional Route53, CloudWatch alarms, and an AWS
  monthly budget notification. Cloud-init prepares the host and mounts only; it
  contains no application secret.
- A credential-free AWS container bundle with immutable image inputs,
  checksum-pinned Docker Compose, health-ordered Patreon/controller/nginx/Caddy
  startup, concrete ingress limits, non-root edge services blocked from IMDS,
  and fail-closed preflight validation.
- CI definitions for formatting, linting, strict typing, tests, migrations,
  secret scanning, container builds, SBOMs, vulnerability scanning, GHCR
  publication, provenance, and attestations.

## Where generated images are accessed

The normal operator surface is:

```text
https://<control-plane-host>/dashboard
```

The current staging login is:

```text
https://gen-automation-staging.18-198-84-215.sslip.io/login
```

It shows releases in ranked order. Authorized owners/reviewers can inspect an
exact stored version and select the approved subset. AI-flagged images stay
visible in a clearly separated review section; rejected or held masters remain
private and recoverable.

The authoritative raw masters are private versioned objects:

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

New X teaser revisions are full-dimension, metadata-free PNG artifacts. X may
derive scaled or reformatted display variants after accepting an upload, so the
private immutable object remains the authoritative publishing artifact rather
than X's displayed copy.

The destination-neutral finished-set ZIP parts are private:

```text
finished-set-archives/{archive_id}/part-NNN-of-NNN/{sha256}.zip
```

New parts use the versioned `public-png-v1` profile. A succeeded Patreon `full`
JPEG remains the readiness proof, but the archived media is a separate
full-dimension, lossless PNG rendered from the frozen source in the isolated
image worker. The raw master is never decoded by the controller or copied
outward. Metadata-free PNG artifacts are durably checkpointed under
`public-media/public-png-v1/<render-identity>/<source-sha256>/...` before the
manifest is frozen, then assembled in generation-queue order. ZIP preparation
is checkpointed per part and runs independently of the publishing guard and
Patreon, MEGA, or X state. Every part includes the same v2 set-wide manifest
with separate source Asset and readiness derivative provenance.

Ready pre-profile archives remain immutable and readable as
`legacy-full-derivative-v1`. A completed review can also create a new public
PNG archive without replacing its legacy JPEG archive. New MEGA deliveries use
`<set-name>` without an automatic format suffix. Existing delivery paths remain
unchanged, and collisions with historical exports fail closed instead of
mixing or overwriting their files.

The `public-png-v1` recipe, renderer, Pillow version, encoder, and byte ceiling
are immutable together. After in-flight work drains, any change requires a new
media profile and a non-colliding MEGA destination; the existing profile
identity is never redefined in place.

Destination-specific Patreon package parts remain private and separate:

```text
publication-packages/{publication_intent_id}/part-NNN-of-NNN/{sha256}.zip
```

Each exact Patreon package can be downloaded for the manual fallback. MEGA
independently expands the provider-neutral finished set into ordered ordinary
image files. Its manifests and completion evidence remain private rather than
being copied into the outward MEGA folder.
Single-part sets can also be published through the Patreon browser sidecar; a
multipart set waits for the operator so the official UI is not driven with an
incomplete subset. Raw masters are never
watermarked, moved, overwritten, or made public.

## No-access work versus live MVP work

### Complete without additional access

- Application, worker, semantic gateway, Patreon browser, and MEGA image source.
- Database migrations, IaC source, example non-secret configuration, and
  deployment/runbook contracts.
- Credential-free unit/integration contracts, fakes, and static image/IaC
  checks.
- Wildcard editing, artifact onboarding, ranking/review, terminal anatomy gate,
  deliverability bounds, derivative rendering, and durable publication logic.

### Remaining live MVP work

1. Confirm the AWS notification subscription and create the linked-account
   budget in the management account.
2. Deploy the private semantic upstream and gateway, test pass/review/severe/
   unavailable behavior, then enable the terminal anatomy gate.
3. Review one generated release, accept a bounded set, render its clean Patreon
   outputs, and select/render only the watermarked X teasers.
4. Bootstrap the persistent Patreon and MEGA profiles through owner-controlled
   sessions, then run one low-risk Patreon publish and extracted-folder MEGA
   canary.
   Exercise the Patreon manual ZIP fallback as part of the canary.
5. Add the exact X secret ARN to the instance role, authorize the creator, and
   run one approved X canary with sensitive-media handling enabled.
6. Repeat the proven sequence with separate production resources and
   credentials; do not promote staging secrets or profile volumes.

## Accounts and access: exact timing

Do not paste passwords, API keys, OAuth values, cookies, signed URLs, database
URLs, recovery codes, or private keys into chat or Git. Put secret values
directly into the deployment/provider secret store and provide only non-secret
resource identifiers or confirmation here.

| When needed | Account or input | Exact purpose and handling |
| --- | --- | --- |
| Final source integration | Existing GitHub maintainer login and authenticated `gh`/Git credential manager | Push the integrated branch and let Actions build, scan, attest, and publish immutable GHCR image digests. Actions uses its own run-scoped `GITHUB_TOKEN`; no PAT should be sent in chat. |
| Before the first `tofu init` or plan | AWS account plus a short-lived AWS IAM Identity Center/SSO session or assumed deployment role; a separately pre-created private, encrypted, versioned, public-blocked state bucket; backend bucket name, key, and Region | Initialize the native-locking S3 backend and plan the staging module. The role needs the scoped provisioning permissions and `iam:PassRole`; static AWS keys do not belong in files or chat. The application asset/model buckets are created by the module and must not be reused for state. |
| In the first AWS plan | Alarm/budget notification email; approved monthly budget; optional non-secret name/tag choices | `notification_email` is required by the module. Review the plan and estimated spend before apply. After apply, the owner must accept the SNS email subscription; a budget alarm is notification, not a hard stop. |
| Before public HTTPS and provider callbacks | Either an existing Route53 public hosted-zone ID plus the intended lowercase hostname, or access to create the equivalent record at the external DNS provider | Route53 inputs may remain null for an IP-only infrastructure canary, but DNS must point to the EIP before Caddy obtains a public certificate, the dashboard uses its final origin, or Salad callbacks are enabled. No DNS credential is needed by the running application. |
| Immediately after AWS apply | SSM access through the deployed operator role | Verify the host and encrypted profile mounts, create separate PostgreSQL migration/runtime roles from the RDS-managed bootstrap secret, run migrations, test backup/restore, and deploy the pinned images. No RDS master password is requested from the user or stored in OpenTofu. |
| Completed for staging; repeat before onboarding new artifact revisions or the first production GPU job | Exact checkpoint/LoRA/detector files or their approved object versions; SHA-256 values; source/license/commercial/adult-use evidence; a temporary model-uploader identity | Upload only to the private model bucket, verify metadata, run the onboarding plan against the migrated database and both buckets, and produce the immutable worker manifest/digest. Disable the uploader's write access afterward. |
| Completed for staging generation; repeat before a new or production Salad canary | SaladCloud organization/project, dedicated automation API key, webhook signing secret, payment authorization/billing limits, queue/group names, and the immutable worker image digest; provider-managed GHCR pull access if that image is private | Create/reconcile the queue and worker group, verify callbacks and signed jobs, cap replicas at one for the paid canary, and scale to zero afterward. The worker receives only narrowly scoped model-artifact read access and signed asset-upload grants, never asset-bucket or database credentials. |
| Before enabling semantic anatomy QC | Private OpenAI-compatible vision-model endpoint, exact model identifier and immutable revision, private network route, and an upstream API key only if that model server requires one | Deploy the pinned upstream plus `Dockerfile.semantic-gateway`, configure the same model/revision on both sides, run semantic canaries, then set `GEN_AUTOMATION_SEMANTIC_ANATOMY_ENABLED=true`. The controller-to-gateway contract itself requires no API key and must not be exposed publicly without a separate authenticated boundary. |
| After AWS exists, before the X canary | X developer Project/App, exact numeric creator user ID, API billing cap, sensitive-media configuration, and either OAuth 2.0 client/refresh credentials with `tweet.read tweet.write users.read media.write offline.access` or owner OAuth 1.0a Consumer/API Key+Secret and regenerated Read-and-write Access Token+Secret | Create the matching mode-specific AWS Secrets Manager JSON secret outside OpenTofu. Re-plan with its complete ARN as `x_oauth_secret_arn` and the matching `x_oauth_auth_mode`; configure the same non-secret ARN, `GEN_AUTOMATION_X_AUTH_MODE`, and creator ID in the app, then run one freshly approved canary. OAuth 1.0a IAM is read-only. |
| After the Patreon sidecar and encrypted profile/state mounts exist | Existing Adult/18+ Patreon creator account, completed identity/age checks, exact tier/tag choices, and one owner-controlled headed Chromium login with password/2FA/CAPTCHA | Persist only the signed-in Chromium profile under `/profiles` and idempotency SQLite state under `/state`. Generate the controller/sidecar shared secret in the deployment secret store; it is not a Patreon credential. No Patreon API key is required. If login expires, UI selectors change, or the result is ambiguous, the intent needs operator reconciliation and the manual package remains available. |
| After the MEGAcmd-enabled image and encrypted profile mount exist | Dedicated MEGA account or writable destination folder with quota, exact remote destination path, plus one owner-controlled MEGAcmd profile bootstrap | Store the authenticated MEGAcmd profile only on the encrypted persistent volume and run an extracted-folder canary that checks order, exact bytes, progress, marker-last completion, and ambiguous-response recovery. The application receives a profile path and remote root, not a MEGA email, password, session, folder key, write auth-key, or API token. |
| Production rollout | Separate production AWS resources, provider projects, model/object identities, OAuth grants, profiles, secrets, DNS, budgets, and notification recipients | Re-run the staging gates with production-scoped access. Never copy a staging database credential, X grant, Patreon profile, MEGA profile, or worker credential into production. |

Application session/TOTP keys, worker-signing keys, and the Patreon internal
shared secret are generated directly in the deployment secret store after AWS
provisioning; they are not third-party account inputs. Account creation,
identity/age/tax verification, terms acceptance, payment authorization,
copyright/model/distribution-rights evidence, OAuth consent, X sensitive-media
settings, and Patreon Adult/18+ classification remain owner actions.

## MVP completion gate

The MVP is complete only when the final integrated local suite and GitHub
Actions CI pass, AWS staging and restore checks pass, one bounded generation
travels from approved artifacts through ranking/review and derivative rendering,
the semantic terminal gate behaves fail-closed, Patreon automatic publishing
and its manual fallback are both exercised, the ordered full-resolution set is
verified as extracted files on MEGA, and only the owner-selected watermarked
teasers reach the authorized X account.
