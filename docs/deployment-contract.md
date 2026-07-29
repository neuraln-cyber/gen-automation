# Deployment contract

These requirements are acceptance gates for staging and production.

## Environment separation

- Development, staging, and production have separate Salad projects, databases,
  buckets, credentials, callback secrets, and publication targets.
- Production data cannot be copied to development.
- The GPU worker is treated as untrusted and ephemeral.
- Only the control plane can approve, archive, or publish.

## Worker permissions

A generation worker receives:

- bootstrap-only, preferably short-lived S3 credentials with `GetObject` access
  only to the approved private model-artifact prefix;
- short-lived presigned writes for predetermined
  `staging/{job_id}/{output_index}` objects;
- a Salad Job Queue worker credential.

It never receives:

- database credentials;
- Salad management credentials;
- bucket list or archive read permission;
- X or policy-approved archive credentials;
- backup credentials;
- access to outputs from other jobs.

The model-artifact identity is distinct from the asset archive identity. It
cannot list either bucket and cannot read masters or write/delete any object.
Checkpoint and LoRA object identifiers come from an approved manifest; exact
sizes and hashes are verified before ComfyUI starts. Artifact credential
environment variables are scrubbed after bootstrap and are not inherited by
ComfyUI or the embedded Salad queue forwarder.

The controller-only Ed25519 private signing key stays in the control-plane
secret store. GPU workers receive only the corresponding public verification
keys. Inline manifest JSON and artifact credentials are supplied as
staging/production secret references resolved only while creating the container
group. Live secret values are never stored in durable provider configuration,
committed to `.env.example`, or baked into the image. These credentials are not
needed until staging deployment.

The controller resolver accepts only the reviewed `GEN_WORKER_*` bootstrap
targets and their fixed `deployment-config://salad-worker/...` aliases.
Staging/production GPU startup requires the complete binding set; arbitrary
environment-variable names, alternate references, missing values, and extra
resolved values fail before the provider mutation.

The controller verifies object key, byte size, file signature, dimensions, and
SHA-256 before promoting a staged output into the immutable master prefix.

## Supply chain

- Production models must be Safetensors. Pickle-backed `.ckpt`, `.pt`, and
  similar formats are rejected unless converted in an isolated environment.
- Every model and LoRA has a source, licence record, checksum, approval state,
  and commercial/adult-use review.
- The only production custom nodes are the digest-pinned, allowlisted Impact
  Pack and Impact Subpack revisions required by the approved FaceDetailer
  workflow. Adding or changing any other custom node requires a separately
  pinned and audited worker-image revision.
- ComfyUI Manager and runtime installation are absent in production.
- Images run as non-root with a read-only root filesystem where supported.
- CI generates an SBOM, scans images and dependencies, and performs secret
  scanning.
- Production deploys use an immutable image digest.

## Storage

- Account-level public access is blocked.
- TLS, server-side encryption, and object versioning are enabled.
- Bucket CORS permits only the dashboard origin.
- Browser asset URLs expire in 5–15 minutes and are never persisted.
- Review responses set `Cache-Control: private, no-store`,
  `Referrer-Policy: no-referrer`, and `X-Robots-Tag: noindex, noimageindex`.
- Reject decisions never mutate raw masters. Rejected masters may transition to
  lower-cost private archive storage under an explicit retention policy, but
  are not silently deleted by review.
- Masters are never overwritten.

## Authentication and network

- Public registration is disabled.
- Roles exist for `owner`, `admin`, `reviewer`, and `publisher`; review and
  publication permissions are separated except for the owner.
- Production administrative access requires MFA.
- The control-plane container uses plain HTTP behind the TLS-terminating
  ingress, disables Uvicorn proxy-header rewriting, and is not directly
  reachable from the public internet.
- Protected environments configure a nonempty allowlist of narrow ingress
  source CIDRs. They do not trust a whole VPC or general workload subnet.
- The ingress overwrites untrusted forwarding headers or appends its
  socket-observed client address. Missing or malformed forwarding chains from a
  trusted peer fail closed.
- Startup assertions confirm that ingress authentication rate limits, bounded
  body/header sizes, connection concurrency, and request timeouts are deployed
  and tested, including for chunked requests without `Content-Length`.
- The local synthetic-owner bypass is disabled in staging and production and
  cannot coexist with real authentication.
- Sessions use `Secure`, `HttpOnly`, `SameSite=Strict` cookies and CSRF
  protection.
- Sensitive actions require recent reauthentication.
- SSH is key-only, root/password login is disabled, and access is restricted by
  VPN or source network.
- ComfyUI and its manager UI are never publicly exposed.

## Webhooks

- Salad callbacks are verified over the raw body using Svix signature, ID, and
  timestamp headers.
- Missing, invalid, stale, or replayed callbacks fail closed.
- Callback IDs have a database uniqueness constraint.
- Callback payload object keys are never trusted; expected keys come from local
  state.
- The public callback endpoint durably stores valid events and returns quickly.
- A reconciler makes callbacks an optimization rather than the sole truth.

## Privacy

- Prompts, images, thumbnails, signed URLs, request bodies, and private release
  names are excluded from third-party analytics and error reporting.
- Published derivatives have prompts, software paths, EXIF, GPS, and private
  metadata stripped.
- GPU hosts may observe plaintext inputs during inference; private real-person
  images and identifying customer information must not be processed.

## Recovery and cost control

- PostgreSQL target: recovery point no worse than 15 minutes and recovery time no
  worse than 4 hours.
- Approved masters, manifests, and derivatives replicate nightly to a distinct
  backup failure domain.
- Restores are tested quarterly.
- GPU replicas, per-release generations, daily spend, and monthly spend have hard
  local limits.
- A global kill switch prevents new GPU allocation and publication.
- Three exhausted job attempts enter a dead-letter state for manual replay.

## Go-live tests

Production is not approved until tests demonstrate:

1. unauthenticated dashboard access fails;
2. public bucket access and listing fail;
3. expired or replayed webhooks have no effect;
4. a worker cannot read masters, list the bucket, or publish;
5. killing a worker mid-job produces one verified logical result;
6. duplicate callbacks do not duplicate archive uploads or X posts;
7. loss of a publish response enters reconciliation rather than blind retry;
8. a database backup and sampled master can be restored.
