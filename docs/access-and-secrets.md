# Accounts, access, and secrets

This is the staged access contract for the MVP. It distinguishes account-owner
actions from credentials that can be injected into automation. Do not paste any
secret, recovery code, signed URL, database URL, or token into chat, GitHub
issues, or source files. Put runtime values directly into the selected
deployment secret store.

The current repository recognizes the `GEN_AUTOMATION_*` and `GEN_WORKER_*`
names shown under **Implemented runtime names**, including the X secret
reference and creator binding. Patreon API names remain reserved for the later
read-only reconciliation phase; the manual Patreon handoff needs no API token.

## When access is needed

| Gate | Needed before the gate | Not needed yet |
| --- | --- | --- |
| Source verification | GitHub repository access. GitHub Actions uses its own short-lived `GITHUB_TOKEN` for CI and GHCR publishing. | Cloud GPU, X, Patreon, and production data credentials. |
| Private staging infrastructure | Control-plane host/project, PostgreSQL TLS URL, versioned S3 asset bucket and control-plane identity, DNS/TLS ingress, application authentication secrets. | Salad and social-publishing tokens may remain absent while GPU allocation and publishing are disabled. |
| Zero-publish GPU canary | Salad organization/project/API key, provider webhook secret, immutable worker image digest, model-artifact bucket and read-only worker identity, approved model manifest, controller signing key. | X and Patreon API credentials. |
| Destination canary | X OAuth 2.0 client/refresh credentials or the owner account's OAuth 1.0a Consumer/API and Access Token pairs. Patreon API v2 credentials are needed only for read reconciliation/webhooks. MEGA needs a dedicated destination plus a one-time pre-authenticated writable-folder profile volume, not an application credential. | Production credentials; use staging/test destinations first. |
| Production | A separate production instance of every database, bucket, provider project, identity, secret, OAuth grant, and publication target. | No staging credential is promoted into production. |

## Account and credential matrix

### GitHub and GHCR

| Item | Minimum access | Secret/configuration | When |
| --- | --- | --- | --- |
| Repository owner/maintainer | Review, merge, branch protection, Actions and package administration for `neuraln-cyber/gen-automation`. | Human GitHub login; local Git/CLI authentication stays in the user's credential manager. | Now. |
| CI image publisher | No separately created token. The workflow grants the run-scoped `GITHUB_TOKEN` `packages:write`, `contents:read`, `id-token:write`, and `attestations:write`. | GitHub-managed `GITHUB_TOKEN`; never copy it out of Actions. | First successful push to `main`. |
| Control-plane image pull | Read only the selected control-plane GHCR package. | Host-native GHCR integration or a dedicated pull credential with `read:packages` only. | Control-plane deployment. |
| Salad worker image pull | Read only the selected GPU-worker package if it remains private. | A registry credential stored in Salad's registry-auth facility, never as a worker environment variable. | GPU canary. |

Only immutable `@sha256:<digest>` image references are deployable. The current
Salad provisioning adapter does not yet inject private-registry authentication.
Therefore one of these must be completed before its canary:

1. configure Salad's provider-managed registry authentication without placing
   the credential in durable deployment JSON; or
2. make only the model-free GPU-worker package public and pull it anonymously.

The control-plane package may remain private. Models, LoRAs, images, prompts,
credentials, and manifests are never baked into either image.

### Control-plane host, DNS, and TLS

| Item | Minimum access | Secret/configuration | When |
| --- | --- | --- | --- |
| Approved container host | One always-on Linux/amd64 service, one-off migration jobs, a secret store, outbound HTTPS, private database access, bounded logs, and a fixed ingress path. One replica is sufficient for the MVP. | The app needs no host API token. A deployment tool may receive a project-scoped deploy token outside the app. | Private staging infrastructure. |
| DNS zone | Create or update only the record for the control-plane hostname. | No DNS token is required at runtime. If infrastructure automation is used, give it a short-lived token restricted to that one zone. | Before public HTTPS/webhooks. |
| TLS ingress | Managed certificate and TLS termination; direct container-port access blocked. | `GEN_AUTOMATION_PUBLIC_BASE_URL=https://<host>` and the exact ingress source networks in `GEN_AUTOMATION_TRUSTED_PROXY_CIDRS`. The certificate private key stays at the ingress. | Before staging startup. |
| Ingress guards | Rate limits for login/signed URLs/webhooks; bounded bodies, headers, connections, and request timeouts. | Set `GEN_AUTOMATION_INGRESS_RATE_LIMIT_CONFIGURED=true` and `GEN_AUTOMATION_INGRESS_REQUEST_GUARDS_CONFIGURED=true` only after those controls are deployed and tested. | Before staging startup. |

The Salad callback URL is
`https://<control-plane-host>/webhooks/salad`. The host must permit the lawful
workload before production data is placed there.

### PostgreSQL

| Identity | Minimum access | Secret/configuration | When |
| --- | --- | --- | --- |
| Migration job | Connect to the single database and own/create/alter the application schema. It runs only as a one-off job. | Inject its TLS URL as `GEN_AUTOMATION_DATABASE_URL` only into the migration job. | Before each application rollout. |
| Runtime application | Connect plus CRUD on application tables/sequences; no database creation, role administration, extension administration, or unrelated-schema access. | Inject its separate TLS URL as `GEN_AUTOMATION_DATABASE_URL` into the control plane. | Staging startup. |
| Managed backup service | Provider-managed backup/PITR permission only; not an application credential. | No backup credential enters the app. | Before storing canary data. |

Use `postgresql+psycopg://...` with certificate verification, preferably
`sslmode=verify-full` and the provider CA. Staging requires automated backups;
production requires point-in-time recovery and a tested restore. Never reuse the
migration role as the continuously running application role.

### Versioned S3 asset storage

The MVP uses one private, versioned asset bucket for staging uploads, immutable
masters, derivatives, and publication packages. A separate model-artifact
bucket or permission boundary is mandatory.

| Identity | Minimum access | Implemented runtime names | When |
| --- | --- | --- | --- |
| Control plane | Bucket reachability; conditional `PutObject`; exact `GetObjectVersion`; `HeadObject`; version-pinned copy; presign GET/POST. Exact-version deletion is limited to staging cleanup. No public access, ACL, bucket-policy, lifecycle, object-lock, or bucket-delete administration. | `GEN_AUTOMATION_STORAGE_ENABLED=true`, `GEN_AUTOMATION_STORAGE_ENDPOINT_URL`, `GEN_AUTOMATION_STORAGE_REGION`, `GEN_AUTOMATION_STORAGE_BUCKET`; either ambient workload identity or the complete short-lived credential set `GEN_AUTOMATION_STORAGE_ACCESS_KEY_ID`, `_SECRET_ACCESS_KEY`, and optional `_SESSION_TOKEN`. | Private staging infrastructure. |
| One-off conformance operator | `HeadBucket`; put/get/copy and exact-version delete only on `conformance/*`. No list/delete-prefix operation. | The same `GEN_AUTOMATION_STORAGE_*` names, injected only into the one-off conformance job, followed by `--confirm-live-storage-conformance`. | Once per endpoint/environment before admission. Revoke afterward. |
| Human storage administrator | Create the bucket, enable versioning/encryption/public-access block, configure exact-origin CORS and retention, inspect provider audit logs. | Human/provider console access; never an application secret. | Provisioning and incident recovery only. |

The bucket must return a nonempty version ID for direct writes, copies,
presigned POST uploads, heads, exact-version reads, and exact-version deletes.
Run the [live storage conformance](storage-conformance.md) before a paid GPU job.

Prefer host workload identity and leave all three explicit credential values
absent when the host supports it. Otherwise inject a short-lived access-key ID,
secret, and session token as one indivisible set from the deployment secret
store. A session token without its key pair and every half-pair configuration
are rejected at startup. The values are resolved only while constructing the
S3 client and are represented as redacted `SecretStr` settings.

### Private model artifacts

| Identity | Minimum access | Implemented runtime names | When |
| --- | --- | --- | --- |
| Model uploader | Put approved Safetensors objects and read their exact versions back for hash verification. No asset-bucket access. This is an operator-only temporary identity; the control-plane role has no broad model-object read. | Use operator-scoped temporary credentials with only the exact plan keys; no application environment name. | Model onboarding. Revoke or disable afterward. |
| Salad GPU bootstrap | `GetObjectVersion` only for each exact key and S3 VersionId in `salad_worker_artifact_object_versions`. No bucket list, unversioned read, write, delete, asset/archive read, or database access. | `GEN_AUTOMATION_SALAD_WORKER_ARTIFACT_BUCKET`, `_REGION`, and the nonsecret `_ROLE_ARN`. The controller uses its ambient EC2 identity to assume that role and supplies `_ACCESS_KEY_ID`, `_SECRET_ACCESS_KEY`, and `_SESSION_TOKEN` to the worker as one temporary set. Custom endpoints and configured long-lived keys are rejected in role mode. | Immediately before group creation and each queue submission. |

Also inject:

- `GEN_AUTOMATION_SALAD_WORKER_MODEL_MANIFEST_JSON`;
- `GEN_AUTOMATION_SALAD_WORKER_MODEL_MANIFEST_SHA256`; and
- `GEN_AUTOMATION_SALAD_WORKER_ALLOWED_UPLOAD_ORIGIN`, set to the exact HTTPS
  origin observed in real presigned upload grants.

The manifest contains opaque object IDs, exact sizes, filenames, and SHA-256
values. Model source, license, commercial-use permission, adult-use permission,
and hashes must be approved before adding an object to it.

The STS session is capped at three hours. The controller rotates it immediately
before enqueueing and waits for Salad to apply the new container-group version.
An allocation delayed beyond that window cannot be refreshed inside an already
started container; bootstrap fails closed and the job must retry through the
controller so a new session is installed before the next enqueue.

### SaladCloud

| Item | Minimum access | Implemented runtime names | When |
| --- | --- | --- | --- |
| Dedicated automation user | Access only to the dedicated staging organization/project where the provider permits it. Salad API keys are user-wide rather than fine-grained, so do not use a personal owner token. | `GEN_AUTOMATION_SALAD_API_KEY`. | Immediately before zero-publish GPU deployment. |
| Provider identifiers | Dedicated organization/project and deterministic queue/container-group base names. Queue and group resources can be created by the controller. | `GEN_AUTOMATION_SALAD_ORGANIZATION`, `_PROJECT`, `_QUEUE_NAME`, `_CONTAINER_GROUP_NAME`. | GPU deployment. |
| Webhook verification | Read the organization webhook signing secret; no management credential enters the worker. | `GEN_AUTOMATION_SALAD_WEBHOOK_SECRET`. | Before enabling Salad callbacks. |
| Immutable worker | Pull the exact CI-published worker digest. | `GEN_AUTOMATION_SALAD_WORKER_IMAGE=...@sha256:<digest>`. | GPU deployment. |
| Job signing | Controller-only Ed25519 private key; workers receive only the derived public-key set. | `GEN_AUTOMATION_WORKER_SIGNING_KEY_ID`, `GEN_AUTOMATION_WORKER_SIGNING_PRIVATE_KEY`. | GPU deployment. |

Keep `GEN_AUTOMATION_GPU_ALLOCATION_ENABLED=false` until storage conformance,
database migration/restore, authentication, budget guards, worker image pull,
and a synthetic worker contract test pass. Set daily/monthly hard budgets before
the one paid job.

The account owner must complete payment authorization, account verification,
and terms acceptance. These owner actions are not represented as application
configuration.

### Application-generated secrets

These require no third-party account and should be generated directly into the
deployment secret store:

| Secret | Implemented runtime name | Rotation rule |
| --- | --- | --- |
| Session/CSRF root | `GEN_AUTOMATION_SESSION_SECRET` | Random unpadded base64url 32-byte value; separate per environment. Rotating invalidates sessions. |
| TOTP encryption keyring | `GEN_AUTOMATION_AUTH_TOTP_ACTIVE_KEY_ID`, `GEN_AUTOMATION_AUTH_TOTP_ENCRYPTION_KEYS` | Random 32-byte keys in a JSON keyring. Retain old key versions until all enrolled seeds are re-encrypted. |
| Worker signing private key | `GEN_AUTOMATION_WORKER_SIGNING_PRIVATE_KEY` | Controller-only Ed25519 seed. Rotate with an overlapping worker public-key set. |

Production also requires
`GEN_AUTOMATION_AUTH_ENABLED=true`,
`GEN_AUTOMATION_AUTH_REQUIRE_TOTP=true`, and the development bypass disabled.
The one-time owner bootstrap password and TOTP recovery material are delivered
to the account owner out of band and are not stored in deployment logs.

### X destination

X is not required for generation, ranking, review, derivatives, or the Patreon
handoff MVP. It is needed only when the X destination gate is activated.

Create an X developer Project/App for the exact creator account. OAuth 2.0
Authorization Code with PKCE remains supported and requests this fixed scope set:

```text
tweet.read tweet.write users.read media.write offline.access
```

`tweet.write` creates the post, `media.write` uploads/labels media, and
`offline.access` permits a refresh token. X documents these scopes in its
[OAuth 2.0 PKCE contract](https://docs.x.com/fundamentals/authentication/oauth-2-0/authorization-code).
If X does not grant `media.write` to the owner-account OAuth 2.0 token, use the
OAuth 1.0a owner-account fallback described below. Do not use an app-only bearer
token for either mode.

| Value | Location | Handling |
| --- | --- | --- |
| Private credentials for the selected X user-authentication mode | One mode-specific AWS Secrets Manager JSON secret | Never place these values in environment variables, application tables, logs, or conversation. Do not mix the OAuth 2.0 and OAuth 1.0a schemas. |
| Exact secret reference | `GEN_AUTOMATION_X_OAUTH_SECRET_REFERENCE` | Non-secret `aws-secrets-manager://` reference containing the complete secret ARN. |
| Expected creator user ID | `GEN_AUTOMATION_X_CREATOR_USER_ID` | Non-secret binding used to prevent posting through the wrong authorized account. |
| Authentication mode | `GEN_AUTOMATION_X_AUTH_MODE` | Set to `oauth2` for the rotating schema or `oauth1` for the static read-only schema. |

The initial secret is:

```json
{
  "schema": "gen-automation/x-oauth/v1",
  "client_id": "CONFIDENTIAL_CLIENT_ID",
  "client_secret": "CONFIDENTIAL_CLIENT_SECRET",
  "refresh_token": "CREATOR_REFRESH_TOKEN"
}
```

The separate OAuth 1.0a fallback secret is
`gen-automation-staging/x/oauth1` and contains exactly:

```json
{
  "schema": "gen-automation/x-oauth1/v1",
  "consumer_key": "CONSUMER_OR_API_KEY",
  "consumer_secret": "CONSUMER_OR_API_SECRET",
  "access_token": "OWNER_ACCESS_TOKEN",
  "access_token_secret": "OWNER_ACCESS_TOKEN_SECRET"
}
```

#### One-time staging authorization from Windows

Configure the X Developer Portal app as an **Automated App or Bot** (a
confidential OAuth 2.0 client) and register this callback URL exactly, with no
trailing slash:

```text
http://127.0.0.1:8765/callback
```

The callback must use `127.0.0.1`, not `localhost`; X performs exact redirect
URI matching. From an interactive Windows PowerShell in the repository, first
authenticate the fixed staging AWS profile and then run:

```powershell
$env:AWS_CONFIG_FILE="$env:TEMP\gen-automation-staging-aws-config"
aws sso login --profile gen-automation-staging
.\scripts\bootstrap-x-oauth.ps1
```

The default helper flow accepts no arguments. It verifies AWS account
`861912887470` and the `GenAutomationStagingDeployer` SSO assumed role before
asking for either X value, reads the OAuth client ID and client secret without
echo, opens the consent page, validates the loopback callback state and PKCE
exchange, and verifies the authorized account with `GET /2/users/me`. It then
creates `gen-automation-staging/x/oauth` directly in Secrets Manager with only
the initial JSON schema above. It fails rather than overwriting an existing
secret.

If X's consent page cannot authorize the app, the Developer Portal can issue a
credential for the app owner's account. Under **Keys & Tokens**, select
**Generate** for the OAuth 2.0 Access Token, retain the generated **refresh
token** (the access token is not used), and run:

```powershell
.\scripts\bootstrap-x-oauth.ps1 -PortalRefreshToken
```

This alternate mode reads the OAuth 2.0 client ID, client secret, and generated
refresh token through three hidden prompts. No credential is passed on the
process command line. The helper immediately submits the refresh token to X
with confidential HTTP Basic client authentication, requires a bearer access
token, a returned refresh token, and the complete fixed scope set, and checks
the posting identity with `GET /2/users/me`. Only X's returned refresh token is
stored; the copied portal token and short-lived access token are discarded.
This binds the secret to the account shown beside the portal's Generate button
without relying on the failing browser consent page.

If the generated OAuth 2.0 token is missing `media.write`, switch to OAuth 1.0a.
In the Developer Portal first select **Read and write** under App permissions,
save the change, and only then generate or regenerate the owner account's OAuth
1.0a **Access Token and Secret**. Use the **Consumer/API Key and Secret** plus
that **Access Token and Secret**; do not enter the OAuth 2.0 Client ID/Secret,
Bearer Token, or OAuth 2.0 access/refresh token. Run:

```powershell
.\scripts\bootstrap-x-oauth.ps1 -OAuth1
```

The four prompts do not echo. The helper signs `GET /2/users/me`, verifies the
numeric creator identity, and only then creates the separate OAuth 1.0a secret.
It fails instead of overwriting an existing secret. OAuth 1.0a uses no loopback
callback and has no refresh token.

Successful stdout contains exactly two lines: the complete secret ARN and the
numeric creator user ID. It never prints an access token, refresh token, client
credential, authorization code, or X response body. Keep the ARN and creator ID
as non-secret deployment inputs, but do not copy any credential into tfvars,
shell history, deployment logs, source control, or chat. The required scopes
are requested as one fixed set:

```text
tweet.read tweet.write users.read media.write offline.access
```

The callback listener exists only for the authorization window and binds only
to IPv4 loopback. The AWS profile needs `sts:GetCallerIdentity` plus permission
to describe and create this exact staging secret (including its fixed tags).

The controller resolves the selected secret immediately before every X effect.
OAuth 2.0 caches its current access token and expiry only in Secrets Manager,
uses the PostgreSQL lock and version-stage compare-and-swap, and therefore needs
`GetSecretValue`, `PutSecretValue`, and `UpdateSecretVersionStage` on the exact
ARN. OAuth 1.0a reads `AWSCURRENT` for every lease, never writes or rotates the
static values, and receives only `DescribeSecret` and `GetSecretValue`. Neither
mode stores credentials in PostgreSQL or logs. Do not grant `ListSecrets`,
create/delete access, wildcard resources, or static AWS access keys.

The creator must personally authorize the app, select an
API plan/billing cap, configure the account's sensitive-media setting, and
confirm that the adult-content and automation rules permit the intended use.
The durable publication orchestrator, fresh human approval, adult metadata,
AI labeling, unknown-outcome reconciliation, and the AWS Secrets Manager OAuth
adapter are implemented. Before every lease the adapter calls
`GET /2/users/me`, retains only the numeric ID, and fails closed on a mismatch.

`GEN_AUTOMATION_PUBLISHING_ENABLED=true` may be used for Patreon package
processing after storage and the background runtime are enabled. It does not
enable external effects by itself: the durable database guard is initialized
stopped and an authenticated OWNER must enable it with the current epoch and
lock version. Do not enable X production publication until the OAuth adapter,
exact secret reference, ambient IAM role, and
`GEN_AUTOMATION_X_CREATOR_USER_ID` binding have passed a staging canary. For the
OAuth 1.0a fallback set both `GEN_AUTOMATION_X_AUTH_MODE=oauth1` and OpenTofu
`x_oauth_auth_mode = "oauth1"`; otherwise the app and least-privilege IAM modes
will disagree.

### Patreon destination

Patreon publication uses Patreon's official creator interface because its public
API does not document post creation, media upload, editing, or scheduling. No
Patreon API key is needed to build/download the deterministic handoff package or
to run the optional isolated browser publisher. The browser sidecar uses a
persistent profile created by a one-time owner-controlled login; no username,
password, token, or cookie belongs in application configuration.

For later read-only reconciliation, create a Patreon API v2 client and use only:

```text
campaigns campaigns.posts
```

Patreon documents `campaigns.posts` as read access to campaign posts in its
[API v2 scope reference](https://docs.patreon.com/). Add
`w:campaigns.webhook` only if the system will create/manage its own webhooks;
it is not needed for polling a recorded post ID.

| Value | Reserved publication name | Handling |
| --- | --- | --- |
| API v2 client ID | `GEN_AUTOMATION_PATREON_CLIENT_ID` | Configuration in the secret store. |
| API v2 client secret | `GEN_AUTOMATION_PATREON_CLIENT_SECRET` | Secret store only. |
| Creator access token | `GEN_AUTOMATION_PATREON_ACCESS_TOKEN` | Read-only reconciliation; resolve only in the reconciler. |
| Creator refresh token, when issued | `GEN_AUTOMATION_PATREON_REFRESH_TOKEN` | Long-lived secret; encrypt and rotate. |
| Campaign ID | `GEN_AUTOMATION_PATREON_CAMPAIGN_ID` | Non-secret binding that prevents cross-campaign reconciliation. |
| Webhook signing secret, optional | `GEN_AUTOMATION_PATREON_WEBHOOK_SECRET` | Required only after a verified Patreon webhook receiver is implemented. |

The creator must personally classify the page Adult/18+, complete required
verification, perform the initial profile login/2FA/CAPTCHA, consent to any
optional read-only OAuth, and visually approve the public preview. See
`docs/patreon-browser-publisher.md` for the sidecar and manual-fallback runbook.

### MEGA completed-set destination

Create a dedicated MEGA account or destination folder with enough quota and
install a pinned official MEGAcmd build in the controller/uploader image. In a
one-time interactive bootstrap, either sign in to the dedicated account or use
a writable-folder URL and its separate write auth-key. Enter the password,
MFA, URL, or auth-key only in the secure prompt, never in chat or application
configuration. The persistent MEGAcmd profile volume is secret session material
and must be mode `0700`, mounted only into the uploader, and excluded from logs
and ordinary backups.

The application receives only:

```text
GEN_AUTOMATION_MEGA_PROFILE_HOME=/run/gen-automation/mega-profile
GEN_AUTOMATION_MEGA_REMOTE_ROOT=/AutomatedSets
```

It does not receive a MEGA email, password, API token, session ID, folder key,
or auth-key. See `docs/mega-delivery.md` for bootstrap and revocation details.

### Optional monitoring

The cost-efficient MVP can use host health checks, provider billing alerts,
PostgreSQL backup alerts, and sanitized application logs without another
credential. If an external error/metrics service is added later, create a
project that contractually accepts the workload and send only redacted
operational metadata. Do not send images, prompts, thumbnails, signed URLs,
request bodies, authorization headers, or release names.

`GEN_AUTOMATION_MONITORING_DSN` is a reserved name, not an implemented setting.
No monitoring token is required for the first canary.

## Account-owner actions that automation cannot perform

The project can provision resources after receiving scoped access, but it cannot
act as the person or business account owner for:

- creating provider accounts or completing identity, age, tax, or business
  verification;
- accepting provider terms and acceptable-use policies;
- supplying payment instruments or authorizing open-ended spend;
- proving copyright, commercial derivative, model, LoRA, and distribution
  rights;
- granting X or Patreon OAuth consent and selecting the authorized creator
  account;
- configuring X sensitive-media/account disclosures;
- classifying the Patreon page Adult/18+, approving its public preview, or
  clicking publish/schedule;
- retaining recovery codes and approving production secret recovery; and
- deciding whether a registry package or endpoint may be public.

Once these owner actions are complete, provide access by granting the project or
deployment service the minimum role and placing named values directly in the
secret store. Send only a confirmation and non-secret resource identifiers in
chat.
