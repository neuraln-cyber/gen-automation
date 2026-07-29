# Live object-storage conformance

The storage conformance command is a destructive-to-its-own-test-data, one-off
operator check for the configured S3-compatible asset store. It does not use the
database and is not an application startup probe. Run it before admitting a
storage provider or endpoint to staging or production.

The command refuses to load storage configuration or credentials unless its
explicit command-line opt-in is the only argument:

```console
gen-automation-storage-conformance --confirm-live-storage-conformance
```

The equivalent immutable-image module invocation is:

```console
python3.12 -m gen_automation.storage_conformance_cli \
  --confirm-live-storage-conformance
```

Configure the normal `GEN_AUTOMATION_STORAGE_*` settings on the one-off job.
`GEN_AUTOMATION_STORAGE_ENABLED` must be true and the configured backend must be
S3-compatible. The opt-in is deliberately a command argument, not a persistent
environment setting.

## What the command proves

Each run generates a cryptographically random 256-bit identifier and operates
only on three exact keys below:

```text
conformance/<random-run-id>/
```

It performs these bounded checks:

1. reaches the configured bucket with the existing `ping` contract;
2. conditionally writes a small PNG and verifies its SHA-256 metadata;
3. proves a duplicate conditional write is rejected;
4. requires a nonempty, non-`null` object version ID and reads that exact
   version back under a 4 KiB ceiling;
5. creates a version-pinned, conditional immutable copy, proves a duplicate
   copy is rejected, and reads the copied exact version;
6. creates a 60-second SigV4 presigned download, requires the complete
   query-signature contract and exactly one matching version ID, without
   fetching or printing it; and
7. creates and executes a bounded, 60-second multipart presigned POST with the
   same no-redirect form shape used by GPU workers, then verifies and reads its
   exact stored version.

The command never calls a list API and never deletes a prefix or bucket. In a
`finally` block it considers only the three fixed keys generated for that run
and deletes only an exact `(key, version ID)` pair that has all of the following
attribution evidence:

- the expected random-run marker and payload SHA-256 metadata;
- the expected content type and byte count;
- a nonempty, non-`null` object version ID; and
- an exact-version read whose bytes match the expected SHA-256.

This permits safe recovery of an object created by a presigned POST whose HTTP
response was lost after upload. A version ID appearing only in an untrusted or
inconsistent response header is never cleanup-eligible. If a later writer wins a
race or the current object cannot be attributed, the command leaves the
ambiguous version in place. Cleanup continues after an individual delete
failure. A successful delete response is not sufficient: the command counts a
version as deleted only after a subsequent exact-version read returns
not-found.

If a provider omits or returns a `null` direct-write version ID, the run fails
closed and does not issue an unversioned delete. Investigate any residue through
provider audit/request logs instead of listing or bulk-deleting the namespace.

Output is a compact JSON report containing fixed step names, machine codes,
counts, and pass/fail state. It excludes bucket names, object keys, version IDs,
credentials, signed URLs/form fields, response bodies, and object bytes. Exit
status is `0` only when all checks and exact-version cleanup succeed, `1` for a
live conformance failure, and `2` when the explicit opt-in or configuration is
missing.

The multipart client rejects redirects, disables environment proxy discovery,
uses bounded connect/read/write/pool timeouts, and never forwards
`Authorization`, `Cookie`, or `Proxy-Authorization` headers from a grant. Error
reports do not include redirect locations, signed form values, provider
exception text, or response bodies.

## Narrow conformance identity

Use a dedicated short-lived operator identity, not the application identity.
For AWS S3, grant only:

- `s3:ListBucket` on the single configured bucket, required by `HeadBucket`;
- `s3:PutObject` on `arn:aws:s3:::<bucket>/conformance/*`;
- `s3:GetObject` and `s3:GetObjectVersion` on that same object ARN; and
- `s3:DeleteObjectVersion` on that same object ARN.

Some S3-compatible providers map exact-version deletion to both
`DeleteObjectVersion` and `DeleteObject`; add the latter only when the provider
documents that requirement. Do not grant bucket deletion, prefix deletion,
ACL, policy, lifecycle, object-lock administration, KMS administration, or
unrestricted list permissions. The bucket must have versioning enabled (not
suspended) and must return usable version IDs for direct PUT, multipart POST,
copy, HEAD, and exact-version GET/DELETE behavior.

Run the job from an environment allowed to reach only the configured HTTPS
storage endpoint. Rotate or revoke the one-off identity after the check.

## Provider pre-screen

As of 2026-07-28, Salad S4 is not an asset-archive candidate: its
[documented limits](https://docs.salad.com/storage/explanation/overview) are
100 MB per file and automatic deletion after 30 days. Cloudflare R2 also does
not meet this application's current contract: its
[S3 compatibility table](https://developers.cloudflare.com/r2/api/s3/api/)
lists bucket versioning as unimplemented, and its
[presigned URL documentation](https://developers.cloudflare.com/r2/api/s3/presigned-urls/)
states that multipart form `POST` is unsupported.

Those services may still be evaluated for narrowly scoped, content-addressed
model downloads when their published terms permit the intended data, but they
must not be used for staging uploads, raw masters, derivatives, or publication
packages. AWS S3 is a technical candidate for the authoritative asset bucket;
it still requires a successful live run of this suite. Recheck current primary
documentation at provisioning time rather than assuming this dated pre-screen
remains valid.

## Live test opt-in

Unit tests use fakes and never contact storage. The live pytest is skipped by
default. To run it intentionally with the same configured credentials:

```console
GEN_AUTOMATION_RUN_LIVE_STORAGE_CONFORMANCE=I_UNDERSTAND_THIS_WRITES_LIVE_OBJECTS \
python3.12 -m pytest tests/test_storage_conformance_live.py
```

The console command remains the preferred operator interface because its
command-line opt-in is visible in the one-off job specification.
