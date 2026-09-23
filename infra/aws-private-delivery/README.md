# Private delivery cost reduction

This separate CloudFormation stack owns only the new CloudFront distribution,
path-routing function, required WAF ACL and flat-rate plan. Existing OpenTofu
resources, S3 policies, IAM identities, object versions and uploads are unchanged.
Deploy in us-east-1; S3 origins remain in eu-central-1.

The PRO subscription is created pending approval. Verify its tier and resources,
then explicitly approve it through PricingPlanManager using its current ETag.
Do not route production traffic until the plan reads ACTIVE. A paid subscription
cancellation takes effect at the end of its billing period.

## Security-critical contract

S3 remains private and authorizes EVERY request using the caller's existing S3
SigV4 header or presigned query. CloudFront receives no independent S3 read grant,
origin access identity, or origin access control. No signing key is added.

CachingDisabled and AllViewerExceptHostHeader are mandatory on every behavior.
The latter restores the regional S3 Host used by the original signature. The
viewer function removes only /models or /assets before forwarding the original
object path. Queries, exact version IDs, range headers and signatures survive.
The worker signs for S3 BEFORE its transport destination is rewritten.

NEVER enable caching on this distribution: doing so could serve a previously
authorized response without checking the next caller's S3 permission. Keep
request sampling/access logging off because headers and queries carry credentials.
Use aggregate bytes, request and error metrics instead.

Unsigned requests, expired/tampered grants, cross-key/version substitutions and
unsigned requests AFTER an authorized request must fail. Full and range GETs
must retain exact lengths, content ranges, versions and checksums. Prove these
against the live canary before enabling application flags.

This is private pass-through delivery, not a public bucket or a new authorization
system. Its purpose is fixed-price transfer; cache hit rate is not a success metric.

## Application switches and bounded exception

Both switches default to absent/off. Configure the exact distribution hostname,
not an https URL, in the root-owned deployment environment:

- `GEN_AUTOMATION_SALAD_WORKER_ARTIFACT_DELIVERY_DOMAIN`: worker GetObject transport
  and externally issued model read URLs, including I2V model grants.
- `GEN_AUTOMATION_STORAGE_DELIVERY_DOMAIN`: unnamed external asset read URLs.

Uploads, internal same-region reads, database, backups and generation settings
are unchanged. The CSP admits the exact asset delivery host in addition to S3;
existing upload URLs and already-issued links remain usable.

**Named downloads deliberately stay on S3.** The live compatibility gate found
that CloudFront removed `response-content-disposition` before S3 signature
verification, including with HTTPS custom origins. Do not remove that parameter
from a signed URL, weaken signing, or ship broken archive download names.
Each grant issued while enabled logs the credential-free
`private_delivery_download_grant` event with route `cloudfront` or
`direct_s3_named_download` and kind `assets`/`models`. Count the latter as residual
S3 traffic, not as savings. There is no automatic failure/retry bypass to S3.

## Deployment and rollback

1. Read back every distribution behavior and confirm disabled caching, full
   authorization forwarding, unchanged private buckets and ACTIVE PRO plan.
2. Deploy immutable application/worker images using the existing CI and staging
   deploy workflow. Its active/accepted-queued-work preflight must pass; never
   bypass it or restart a healthy worker to force adoption.
3. Keep both switches absent for initial deployment. At a safe idle boundary,
   back up `/etc/gen-automation/control-plane.env` privately, set the worker
   switch and use the supported reconciler to start the next worker. Prove the
   next real bootstrap retains size/version/SHA-256 validation and job progress.
4. Set the asset switch after its live private/no-store/version/range smoke
   check, and verify dashboard media/CORS while direct uploads still work.
5. Rollback: remove the affected variable (not an empty value), then reload the
   control plane at the same idle boundary. For a worker already downloading,
   retain the known-good worker rather than destructively restarting it. Future
   worker configurations and newly issued asset grants use direct S3 again.
   Existing presigned CDN grants remain valid until their original expiry, so
   keep the distribution enabled through that period. No database rollback is
   involved. The PRO fee is not instantly refunded on cancellation.
6. Compare CloudFront `BytesDownloaded`, `Requests`, and `5xxErrorRate` in
   CloudWatch's us-east-1/Global distribution metrics against S3 internet bytes
   and the baseline billing audit after 72 hours of representative activity.
   Exclude deliberate negative canary requests from error-rate assessment.

Savings depend on bytes actually routed, including the named-export exception;
the baseline 50–65% opportunity is not a measured result of this deployment.

References: [AWS origin authorization guidance](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/add-origin-custom-headers.html),
[managed origin policies](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/using-managed-origin-request-policies.html),
[subscription resource](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-pricingplanmanager-subscription.html).
