# Security and trust boundaries

## Content and legal gate

Generation must fail closed unless:

- every depicted character is present in an approved, clearly-adult allowlist;
- no subject is canonically minor, minor-presenting, age-ambiguous, or an
  "aged-up" version of a minor;
- no real-person sexual likeness is used without documented consent;
- commercial derivative and distribution rights are documented for every
  copyrighted character or franchise; popularity or "fan art" labeling is not
  treated as permission;
- the checkpoint and every LoRA are approved for the intended commercial use;
- the configured cloud, storage, and publishing providers permit the workload;
- the operator's jurisdiction permits production and distribution.

These are metadata and human-governance controls. An image classifier is not an
acceptable substitute for age, consent, rights, or jurisdiction verification.

## Trust boundaries

1. The public internet is untrusted.
2. The browser is untrusted after every request.
3. SaladCloud workers are ephemeral and not trusted with long-lived credentials.
4. Provider webhooks and callbacks are untrusted until authenticated.
5. User-supplied workflow files and custom nodes are code and require review.
6. Object names and metadata are untrusted input.
7. Published remote state can diverge from local state.

## Secrets

- Secrets are never committed, logged, embedded in images, or written into
  workflow JSON.
- Production startup fails if placeholder/default secrets are detected.
- The control plane receives provider credentials through its deployment secret
  store.
- GPU workers receive short-lived, job-scoped output URLs and a separate
  bootstrap-only model identity restricted to `GetObject` on approved artifact
  objects.
- Separate identities are used for control plane, worker, backup, and human
  administration.
- Keys are scoped to the narrowest resource and operation possible.
- Rotation never requires rebuilding an image.
- Production and non-production use different credentials and data stores.
- Provider tokens are redacted from headers, query strings, exceptions, and
  structured logs.
- Model credentials are scrubbed from the worker environment after verified
  bootstrap and are never inherited by ComfyUI or the embedded queue forwarder.
- The controller signs jobs with Ed25519. Its private key never enters Salad;
  workers receive only public verification keys, so a compromised worker cannot
  mint jobs accepted by another worker.
- Worker bootstrap secrets are created and injected only for
  staging/production deployment; local control-plane development does not
  require them.
- Deployment-secret-store values enter the controller as `SecretStr` settings
  and are resolved only into an explicit Salad worker binding allowlist while
  constructing the container-group request. The resolver is not exposed on app
  state and is cleared after the controller stops.

## Storage

- Buckets and objects are private by default.
- Browser access uses short-lived presigned URLs.
- Raw masters cannot be overwritten in place.
- Workers upload only to predetermined staging keys and cannot list the bucket.
- Checksums are verified before an asset becomes available for review.
- Lifecycle rules may delete quarantined rejects only after the configured
  recovery interval.
- Backups are encrypted under a separate credential and failure domain.

## Application access

- The control-plane container serves plain HTTP and disables Uvicorn proxy
  header rewriting. TLS is mandatory at the trusted ingress outside local
  development, and direct access to the container port is restricted to that
  ingress.
- Staging and production startup requires nonempty trusted-proxy CIDRs plus
  explicit assertions that ingress authentication rate limits and request
  guards are deployed. Request guards bound request-body and header sizes,
  connection concurrency, and request timeouts, including chunked requests
  without `Content-Length`.
- Trusted-proxy CIDRs contain only the ingress source addresses, never an entire
  VPC or broad internal network. A compromised workload inside a trusted range
  could otherwise spoof a client address.
- Login ignores forwarded addresses from an untrusted direct peer. A trusted
  peer must supply a bounded, valid IP-only `X-Forwarded-For` chain, which is
  evaluated from right to left; missing or malformed chains fail closed. The
  ingress must overwrite client-supplied forwarding headers or append its
  socket-observed client address.
- The synthetic local-owner authentication bypass defaults off, is allowed only
  in local/test environments, and is mutually exclusive with real
  authentication.
- Administrative sessions use secure, HTTP-only, same-site cookies.
- Passwords use Argon2id.
- Login, secret changes, publication, deletion, and role changes require recent
  authentication.
- State-changing browser requests require CSRF protection.
- Rate limits apply to authentication, signed-URL issuance, and webhooks.
- TOTP is mandatory for every production administrative login and
  reauthentication.
- Normal administrator invitations require an owner with `MANAGE_USERS`,
  same-origin and CSRF proof, and recent authentication. The 256-bit capability
  is returned only once, stored only as SHA-256, bounded by expiry, and accepted
  only in JSON request bodies.
- Invitation creation never returns a TOTP seed. A valid capability must first
  be proved to the JSON inspection endpoint; one-time completion rebinds the
  encrypted seed from enrollment-ID AAD to administrator-ID AAD and wipes the
  enrollment copy.
- ComfyUI and its manager are not exposed publicly.

## Supply chain

- Production model files use Safetensors; pickle-backed model formats fail
  validation unless converted in isolation.
- Custom nodes are disabled in the first production worker; every base and
  source image is pinned by immutable digest or revision.
- ComfyUI Manager and runtime node installation are excluded from production.
- Containers run as non-root and read-only where supported.
- CI scans dependencies, container images, SBOMs, and committed content for
  secrets. The secret scan uses the checksum-pinned Gitleaks CLI over complete
  reachable Git history, with findings fully redacted in logs. It also decodes
  encoded material and inspects one archive layer. Its sole allowlist entry is
  constrained to one exact non-secret test fixture. CI rejects shallow history
  and Git blobs over the scanner's 20 MiB per-file limit instead of silently
  leaving either content class unscanned.

A passing scan is a preventive guard, not evidence that credentials are safe.
If a secret is committed, revoke or rotate it immediately before removing it
from the repository and history. Deleting it in a later commit does not remove
it from earlier commits. Review the pinned scanner and detection rules during
dependency maintenance; do not replace the exact pin with `latest`.

## Webhooks and callbacks

- Verify provider signatures over the raw request body where supported.
- Reject stale timestamps and replayed event IDs.
- Store the event before processing it.
- Treat ordering as undefined.
- Return success only after durable receipt, not after full processing.

## Audit

Audit records are append-only and include:

- actor or service identity
- action
- affected resource and version
- timestamp
- correlation and idempotency IDs
- before/after security-relevant fields
- client and provider request identifiers

Secrets, prompt text marked private, presigned URLs, and raw authentication
headers are redacted.

Prompts, thumbnails, images, signed URLs, and request bodies are not sent to
third-party analytics or error-reporting services.

## Recovery

- Database backups are automated and restoration is tested.
- Object inventory is periodically reconciled with database asset records.
- Provider publication state is reconciled using stored remote IDs.
- A global kill switch prevents new GPU allocation and publication.
- Per-provider circuit breakers stop retry storms and unexpected spending.
- Budgets are enforced locally even when provider limits also exist.
