# Automatic quality scoring

Automatic scoring is a CPU control-plane workload. It does not allocate a GPU
and it does not call an external model API. Enable it only after private,
versioned object storage and the durable background controller are configured:

```text
GEN_AUTOMATION_BACKGROUND_RUNTIME_ENABLED=true
GEN_AUTOMATION_STORAGE_ENABLED=true
GEN_AUTOMATION_QUALITY_SCORING_ENABLED=true
```

## Durable flow

1. The controller creates one `ScoringRun` only for the current
   `ReleaseVersion` while its release is in `reviewing`.
2. Run creation snapshots every available raw master's storage backend, bucket,
   key, nonempty object version, SHA-256, byte size, format, width, and height.
3. A controller instance claims one due `AssetScore` with a database lease and
   commits ownership before reading storage. Claims use compare-and-swap
   predicates; another replica cannot complete the same lease.
4. The worker reads the exact object version with a byte ceiling. It checks
   length and SHA-256 before any image parser runs.
5. Pillow decoding and scoring happen in a fresh Linux `spawn` child. The child
   installs and verifies hard address-space, RSS, and core-dump limits, then
   removes every inherited environment variable before parsing. Parent IPC and
   wall time are bounded. The process is never reused.
6. A successful result, or a sanitized corrupt/exhausted outcome, is staged on
   the score without changing it to a terminal score state. Processing is
   sequential, so one controller cycle holds at most one raw master in memory.
7. Only after every member of the frozen input manifest is durably staged does
   the service validate the complete snapshot and atomically write all terminal
   score signals, rankings, the ranking-manifest digest, and the completed run.
   There is no partially terminal ranking.

Completed runs are immutable and replay as no-ops. The run identity is unique
for release version, configuration digest, and scorer version.

## Failure and recovery behavior

- A malformed supported-format image is staged immediately as
  `corrupt_image` for human review.
- Storage failures, analysis timeouts, resource exhaustion, child crashes, and
  protocol failures retry with bounded exponential backoff.
- An expired processing lease is safe to reclaim because analysis has no
  external side effect. If the final allowed lease expires, the score is staged
  with the sanitized `analysis_lease_expired` signal so the batch can still
  freeze for human review.
- Stored error details are generic. Object keys, provider responses, image
  bytes, signed URLs, credentials, and exception messages are not persisted or
  logged by this path.
- Non-Linux execution fails closed when the isolated analyzer is invoked.

Resource limits are not a syscall or network sandbox. The controller deployment
must also run non-root with a restrictive seccomp/AppArmor profile, no
provider-control credentials, and deny-by-default egress. Its object-storage
identity should be limited to exact reads/writes under the application prefixes.

The lease must exceed the whole quality-cycle timeout. The cycle timeout must
cover the isolated analysis timeout plus cleanup, and controller staleness must
exceed the longest cycle plus maximum jittered delay. Startup validation rejects
unsafe combinations.

## Resource defaults

The default parser wall-time limit is 45 seconds and the hard child memory limit
is 768 MiB. Input byte, dimension, pixel, aspect-ratio, thumbnail, and batch
bounds remain frozen in `QualityConfig`. Raising one bound requires reviewing
the memory allowance and running the real Linux subprocess test in CI.

This first scorer ranks deterministic image-health signals (exposure, contrast,
dynamic range, entropy, sharpness, blank detection, and near-duplicate hashes).
It is a triage aid, not an anatomy, character-identity, rights, or policy model.
Human review remains mandatory.
