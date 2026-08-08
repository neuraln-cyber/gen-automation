# Object-storage contract

Object storage is the authoritative archive for image bytes. PostgreSQL stores
identity, lifecycle state, checksums, dimensions, lineage, and ranking data; it
does not store images or signed URLs.

## Control-plane credentials

Use ambient workload identity whenever the control-plane host provides it. In
that mode, leave `GEN_AUTOMATION_STORAGE_ACCESS_KEY_ID`,
`GEN_AUTOMATION_STORAGE_SECRET_ACCESS_KEY`, and
`GEN_AUTOMATION_STORAGE_SESSION_TOKEN` absent; the S3 SDK retains its normal
workload-identity credential chain.

If ambient identity is unavailable, inject a least-privilege, short-lived
access-key ID and secret. Include `GEN_AUTOMATION_STORAGE_SESSION_TOKEN` when
the provider issues temporary session credentials. The token is accepted only
with the complete key pair, passed to the SDK only when configured, and never
logged or stored on the object-store adapter. Long-lived static keys are a
fallback, not the preferred production configuration.

## Where files are accessed

The review dashboard shows ranked assets from PostgreSQL and requests a
short-lived, version-pinned download URL only when an image is opened. Raw
masters remain in the private object store under:

```text
masters/{release_id}/{asset_id}/{sha256}.{extension}
```

The implemented ranked dashboard is the operator interface for sorting and
review. It signs the exact stored version only when an authorized owner or
reviewer opens or downloads an image; authorized recovery or export tooling can
also retrieve a raw master by asset ID. Neither access path is public.

Every staged upload and immutable copy carries
`Cache-Control: private, no-store, max-age=0`. Version-pinned signed GETs also
override the response with that cache policy, including attachment downloads.
The dashboard page's own `no-store` header cannot by itself prevent a browser,
forward proxy, or storage CDN from caching a cross-origin raw-master response.

Control-plane-produced artifacts, including watermarked derivatives and
Patreon handoff archives, use a bounded conditional `PutObject` with
`If-None-Match: *`, Content-MD5 transport verification, computed SHA-256
metadata, server-side encryption, and a post-write metadata check. A retry can
adopt a matching durable object, but it can never overwrite bytes already
stored under the same immutable key.

## Upload and promotion

GPU workers never receive credentials for the asset/archive bucket or arbitrary
output object keys. Their separate bootstrap identity is read-only and limited
to approved objects in the private model-artifact source. For each expected
output, the controller allocates a random upload-attempt path:

```text
staging/{release_id}/{job_id}/{asset_id}/{upload_attempt_id}/output
```

It returns a short-lived presigned POST policy with:

- an exact controller-generated key;
- an exact content type and controller-generated metadata;
- required server-side encryption;
- a hard `content-length-range`;
- a maximum 15-minute lifetime.

The controller then:

1. acquires a database verification lease;
2. records the staging `VersionId` and ETag;
3. downloads that exact version with `If-Match`;
4. fully decodes the image in a separate process with format, frame, dimension,
   aspect-ratio, pixel, byte, and wall-time limits;
5. computes the authoritative SHA-256 from the original bytes;
6. conditionally copies the exact source version using destination
   `If-None-Match: *`;
7. verifies or safely adopts an identical existing destination after a retry;
8. transactionally marks the master available and emits audit/outbox events;
9. deletes only the verified staging version.

The raw bytes are not re-encoded or watermarked. Published derivatives will be
new lineage-linked assets, so the raw master remains recoverable.

## Required bucket controls

Production storage must pass a live conformance test before credentials are
enabled:

- private bucket with every public-access mechanism disabled;
- TLS-only endpoint;
- server-side encryption;
- object versioning enabled;
- conditional `CopyObject` destination creation supported;
- lifecycle deletion of incomplete uploads, staging delete markers, and
  noncurrent staging versions;
- CORS restricted to the private dashboard origin;
- no worker permission to list the bucket, read `masters/`, or write outside its
  single signed staging key.

Unit tests use an in-memory adapter. They verify controller semantics, but they
do not replace the paid one-object conformance test against the selected
S3-compatible provider.

## Failure behavior

- Missing or changing staging objects return to an upload/retry state.
- Malformed, oversized, mismatched, animated, or unsafe images are quarantined
  and retained for investigation.
- A conflicting existing master is never overwritten.
- A copy that succeeded before a controller crash is adopted only after its
  original bytes and controller metadata match.
- Cleanup failure leaves the verified master available and records cleanup as
  pending for reconciliation.
- Signed master downloads include the database-recorded object version.
- Raw-master responses explicitly instruct browsers and intermediaries not to
  cache the bytes.

Signed URLs and POST fields are never persisted or logged.

## Cost and retention guardrails

The AWS staging bucket lifecycle is intentionally narrow. Current and
noncurrent objects under `staging/` are eligible for deletion only after the
configured abandoned-upload grace period (seven days by default). A normal
successful collection promotes the exact bytes to `masters/` and deletes its
staging version immediately, so the lifecycle rule primarily collects uploads
left behind by interrupted workers or controller failures.

No lifecycle expiry applies to `masters/`, `derivatives/`,
`finished-set-archives/`, or `publication-packages/`. Database records pin
those objects by key and version, so deleting a noncurrent version merely
because S3 calls it noncurrent could break review, download, or delivery.
Storage-cost cleanup for durable outputs must therefore be an application-aware
operation that first proves no live database lineage references the exact
version; a bucket-wide age rule is not acceptable.
