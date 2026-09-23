# Private delivery validation — 2026-09-23

Release base: `3c8ddaa9d20f57b12791ded88ef9cb3c7c33750e` (verified live image label).
CloudFormation stack: `gen-automation-private-delivery`, us-east-1.
Distribution: `E20RTI5Q1A6OBY`, `dzrvatfyqdx7p.cloudfront.net`.
Subscription: CloudFront PRO, ACTIVE. No new S3/IAM grants were made.

The bounded live canary passed 36 checks across models and assets: two exact
versions of each fixture; full and ranged contents; no-store response header;
browser CORS; unsigned requests after authorized reads; expired signatures;
cross-key, cross-bucket and version tampering; SDK header-signed ranged reads.
Only the four fixture versions created by each run were deleted. No existing
objects were deleted or modified. Request URLs/headers/credentials were not logged.

A full 6,230,011-byte existing model was downloaded through CloudFront using the
application's existing worker read-only STS role. Exact version, length and full
SHA-256 matched the manifest. This ran on the control-plane host; its 0.24-second
transfer is NOT evidence of remote GPU-worker cold-start performance.

Application regression coverage exercises real botocore signatures across a
503 retry, exact range/version retention, both switches off, route validation,
named-export direct-S3 behavior, unchanged uploads, runtime binding propagation,
model-store routing and browser CSP. Existing related bootstrap/storage/config/
dashboard/workflow tests pass. Ruff formatting/lint and full source mypy pass.

Named exports remain direct S3 because the live test showed CloudFront removing
the signed response-content-disposition parameter. The same failure occurred
with a custom HTTPS origin and with restoration in the viewer function. Those
experimental changes were reverted; no unsigned header workaround was adopted.

Production activation is still pending. At the last preflight there were three
running and eight queued Salad jobs. Do not bypass the idle guard, claim cold
bootstrap success, or report realized bill savings before the remaining rollout
and billing verification steps in README are complete.
