# MVP status and deployment handoff

Status date: 2026-07-29 (Europe/Sofia)

## Current conclusion

The AWS staging control plane is live at
`https://gen-automation-staging.18-198-84-215.sslip.io`. Its PostgreSQL schema
is migrated through `0015`, the initial owner is enrolled with TOTP, all four
runtime containers are healthy, and the public HTTPS readiness canary passes.
Application secrets were generated only on the host. External GPU allocation
and Patreon, MEGA, and X publication effects remain disabled until their
individual credentials and canaries are complete.

The credential-free source implementation is complete. Final end-to-end MVP
acceptance still requires model onboarding, one bounded GPU generation,
semantic anatomy verification, review/derivative checks, and provider-specific
publication canaries. No historical test count is used as a completion claim.

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
  and publication: at most 100 accepted images, post-hires masters bounded to
  8192 by 8192 and 12 million pixels, and X outputs deterministically adapted to
  the 5 MiB image ceiling through bounded JPEG quality/downscaling.
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
- A pinned MEGAcmd-enabled controller image and restart-safe automatic mirror of
  the exact clean Patreon ZIP. Success requires a verification download with
  matching byte length and SHA-256; the private asset bucket remains the source
  of truth.
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

The deterministic full-set ZIP is private:

```text
publication-packages/{publication_intent_id}/{sha256}.zip
```

That same exact ZIP can be published through the Patreon browser sidecar,
downloaded for the manual fallback, and mirrored to MEGA. Raw masters are never
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
   budget in the management account. Complete the S3 live conformance and
   database restore exercises.
2. Upload and onboard the approved checkpoint, LoRAs, optional detector, and
   workflows; retain the emitted exact manifest and digest.
3. Configure SaladCloud and run a zero-publication synthetic worker canary,
   followed by one bounded paid generation with one replica and scale-to-zero.
4. Deploy the private semantic upstream and gateway, test pass/review/severe/
   unavailable behavior, then enable the terminal anatomy gate.
5. Review one generated release, accept a bounded set, render its clean Patreon
   outputs, and select/render only the watermarked X teasers.
6. Bootstrap the persistent Patreon and MEGA profiles through owner-controlled
   sessions, then run one low-risk Patreon publish and MEGA mirror canary.
   Exercise the Patreon manual ZIP fallback as part of the canary.
7. Add the exact X secret ARN to the instance role, authorize the creator, and
    run one approved X canary with sensitive-media handling enabled.
8. Repeat the proven sequence with separate production resources and
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
| Before model onboarding and the first GPU job | Exact checkpoint/LoRA/detector files or their approved object versions; SHA-256 values; source/license/commercial/adult-use evidence; a temporary model-uploader identity | Upload only to the private model bucket, verify metadata, run the onboarding plan against the migrated database and both buckets, and produce the immutable worker manifest/digest. Disable the uploader's write access afterward. |
| Immediately before the Salad zero-publish canary | SaladCloud organization/project, dedicated automation API key, webhook signing secret, payment authorization/billing limits, queue/group names, and the immutable worker image digest; provider-managed GHCR pull access if that image is private | Create/reconcile the queue and worker group, verify callbacks and signed jobs, cap replicas at one for the paid canary, and scale to zero afterward. The worker receives only narrowly scoped model-artifact read access and signed asset-upload grants, never asset-bucket or database credentials. |
| Before enabling semantic anatomy QC | Private OpenAI-compatible vision-model endpoint, exact model identifier and immutable revision, private network route, and an upstream API key only if that model server requires one | Deploy the pinned upstream plus `Dockerfile.semantic-gateway`, configure the same model/revision on both sides, run semantic canaries, then set `GEN_AUTOMATION_SEMANTIC_ANATOMY_ENABLED=true`. The controller-to-gateway contract itself requires no API key and must not be exposed publicly without a separate authenticated boundary. |
| After AWS exists, before the X canary | X developer Project/App, confidential client ID/secret, creator refresh token, exact numeric creator user ID, account authorization for `tweet.read tweet.write users.read media.write offline.access`, API billing cap, and sensitive-media configuration | Create one AWS Secrets Manager JSON secret outside OpenTofu. Re-plan with only its complete ARN as `x_oauth_secret_arn` so IAM is limited to that secret, configure the same non-secret ARN reference and creator ID in the app, then run one freshly approved canary. |
| After the Patreon sidecar and encrypted profile/state mounts exist | Existing Adult/18+ Patreon creator account, completed identity/age checks, exact tier/tag choices, and one owner-controlled headed Chromium login with password/2FA/CAPTCHA | Persist only the signed-in Chromium profile under `/profiles` and idempotency SQLite state under `/state`. Generate the controller/sidecar shared secret in the deployment secret store; it is not a Patreon credential. No Patreon API key is required. If login expires, UI selectors change, or the result is ambiguous, the intent needs operator reconciliation and the manual package remains available. |
| After the MEGAcmd-enabled image and encrypted profile mount exist | Dedicated MEGA account or writable destination folder with quota, plus one owner-controlled MEGAcmd profile bootstrap | Store the authenticated MEGAcmd profile only on the encrypted persistent volume and run the verified upload/download canary. The application receives a profile path and remote root, not a MEGA email, password, session, folder key, write auth-key, or API token. |
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
and its manual fallback are both exercised, the exact full-set ZIP is verified
on MEGA, and only the owner-selected watermarked teasers reach the authorized X
account.
