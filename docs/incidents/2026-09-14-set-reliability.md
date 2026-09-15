# Set interruptions on 2026-09-14

## Evidence and scope

The affected release finished all 320 outputs across 17 jobs before this repair.
On 2026-09-15 the queue was idle and the GPU group had scaled down normally.
Saved assets, generation parameters, targets, and review state were not changed.

The deployed control plane was revision `cd681addc28cdfc8835aeb13fbb1df68c262441d`.
The published worker was revision `61bb069abe74e4e6c01e67096ff16e9f52007f03`,
image digest `sha256:deb4084ef908753d363409d56d05f9b3f86e5f789acbd1808c978a0ed5960412`.
That worker already contained the two-minute stream-receive heartbeat watchdog.

## Separate failure modes

1. **Listener alive, no queue consumption.** Twice, the instance was ready,
   Comfy queues were empty and all provider requests were pending. Restarting
   only the listener restored consumption and saved-output progress. In the
   pinned upstream listener, a server cancellation can return `nil` from the
   poller, and `NotFound` returns an error that its owner only logs. Both leave
   a non-nil cancellation handle for a stopped goroutine. A stable ready state
   cannot restart it. In addition, `AcceptJobs` can block in gRPC wait-for-ready
   before the existing receive heartbeat timer starts. The exact triggering
   stream error was not retained in the incident logs; these code defects are
   reproducible and explain why process-only supervision cannot guarantee recovery.
2. **Storage credential lifetime.** The failed requests' exact referenced-payload
   URLs returned `ExpiredToken`, while replacement requests returned HTTP 206.
   This strongly implicates expired temporary credentials in the observed HTTP
   502 sequence; the original 502 response body was unavailable. Presigning
   previously accepted a three-hour URL lifetime without checking the underlying
   IAM session's remaining life. The live signer uses refreshable IAM-role
   credentials, not permanent access keys.
3. **Automatic recovery disabled for current metadata.** The deployed validator
   recognized only the legacy three-field runtime-admission shape. New requests
   include rollout and instance binding, so the 600-second output-progress
   watchdog was ineligible. The 6,300-second hard watchdog remained. The source
   correction was already present in `692a97c` but had not been deployed.
4. **Provider replacement and retry replay.** One worker disappeared and its
   replacement downloaded the image and models before becoming ready. The
   provider's reason for replacing it is unknown. Subsequent retries replayed
   previously saved slots before the unique-output counter advanced; these
   periods were active work, not another listener stall.

## Repairs

- Supervise poller termination inside the listener with cancelable exponential
  backoff. Reconnect on server cancellation, missing-job errors and unexpected
  returns without canceling an active HTTP executor or changing current-job
  ownership. Preserve terminal instance/auth handling and operator cancellation.
- Bound stream establishment as well as stream receive silence, and close
  canceled stream contexts on all error exits.
- Include real gRPC reconnection regression tests in the pinned worker build.
  Include the heartbeat patch in immutable worker source selection so a patch-only
  change cannot reuse an older worker image.
- Check signing credentials against the full requested grant life plus a
  60-second margin. When necessary, obtain a fresh SDK session through the same
  credential chain and client settings. Serialize signer refreshes, rate-limit
  them, and leave concurrent storage connections intact.
- If fresh credentials still cannot cover the grant, roll back partial upload
  preparation and defer the same unsubmitted attempt without burning execution
  retries or retaining its budget reservation. Show a storage-credentials wait
  reason. Do not issue a knowingly short-lived grant or extend permissions.
- Deploy the existing current-metadata watchdog correction with these changes.

Opaque, explicitly configured temporary tokens without expiry metadata cannot
promise a known signing lifetime and now fail closed. A prolonged provider
credential outage can still defer new work; its reason is recorded rather than
silently issuing unusable links. These changes do not prevent provider machine
loss or remove the cost of replaying saved output slots after a genuine failure.

## Verification

- Focused Python storage, worker-input, collection, submission, watchdog,
  supervisor, image-contract and publication tests.
- Full-project Ruff formatting/lint and mypy checks.
- Both patches applied to a fresh checkout of the exact pinned upstream commit;
  Go worker/CLI tests run repeatedly, followed by `go vet`.
- gRPC tests reproduce server cancellation and missing-job responses, then
  receive the next job without a readiness transition or current-job mutation.
- Credential tests cover concurrent refresh, too-short refreshed credentials,
  refresh failure, rotation during signing, partial-preparation rollback and
  retrying the same prepared attempt.

Deployment must use immutable images and the normal idle-work preflight. This
document records the repair design, not a claim that a particular image is live.

AWS documents that a presigned URL expires with its underlying temporary
credentials even when the requested URL lifetime is longer:
https://docs.aws.amazon.com/AmazonS3/latest/userguide/using-presigned-url.html
