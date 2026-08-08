# MEGA completed-set delivery

## Result

MEGA delivery consumes the provider-independent `FinishedSetArchive`. It does
not wait for Patreon and it does not upload Patreon ZIP files. As soon as a
finished archive is ready, the background controller uploads every accepted
full-resolution image as an ordinary file in one MEGA folder.

The source archive already freezes the accepted set in generation-queue order.
The MEGA folder preserves that order with zero-padded filenames:

```text
<remote-root>/<set-name>/
  001.png
  002.png
  ...
  set-manifest.json
  upload-complete.json
```

`<set-name>` is the set's visible title (trimmed only), so a title such as
`Yoruichi - Bleach` is delivered directly to
`/Future/Yoruichi - Bleach/` when `/Future` is configured as the remote root.
No project, version, or hash folders are inserted. A set folder must be empty or
already belong to that exact in-progress delivery. Unexpected, duplicate, or
mismatched files stop the delivery for attention instead of being mixed,
renamed, or overwritten; use a unique set name for a later replacement.

The image bytes are copied exactly. They are not resized, watermarked,
recompressed, or stripped of metadata. X teaser derivatives are not included.
`set-manifest.json` records the immutable source identity, filename, ordinal,
byte length, SHA-256, and generation-queue ordering for every image.
`upload-complete.json` is written and verified last, so its presence means the
entire folder was transferred and accounted for.

The deterministic ZIP download remains available separately in the dashboard.
Preparing or retrying MEGA never blocks that local/S3 download.

## Efficient transfer path

The controller uses the official, long-lived MEGAcmd service:

1. Read and verify one bounded finished-set archive part from private object
   storage. The shared manifest is cached durably after the first validated
   read, so later parts do not re-download the first archive part.
2. Stream its image entries into a private temporary directory without
   recompressing them.
3. Submit up to 100 images in one `mega-put` command. MEGAcmd owns the encrypted
   transfer scheduling, so the application does not start one login or process
   per image and never holds a whole 250-image set in memory.
4. Commit item and byte progress in the database, remove the temporary files,
   and continue with the next part.
5. Upload and download-verify only the two small control files at completion.

Normal delivery sends each image once and does not download it again. If a
command times out or its response is lost, the next attempt reconciles only the
ambiguous filenames. It downloads and hashes those files before adopting them,
which prevents duplicate uploads while avoiding a routine second transfer of
the whole set.

MEGA permits duplicate filenames. A duplicate expected filename or an existing
filename with different bytes is a terminal conflict rather than something the
automation silently overwrites.

## Durable state and API

One `MegaSetDelivery` row freezes the archive identity and remote folder. One
ordered `MegaSetDeliveryItem` row records each image's source object identity,
expected SHA-256/size, remote path, attempts, and completion state. Parent
counters provide live `uploaded / total` image and byte progress. Credentials,
folder keys, exported links, and account sessions are never stored in these
tables.

Authenticated operators can inspect the new extracted-folder delivery at:

```text
GET /api/v1/mega-set-deliveries/{delivery_id}
GET /api/v1/releases/{release_id}/mega-set-deliveries
```

The legacy `/mega-deliveries` endpoints remain readable for historical ZIP
mirror records, but the controller no longer creates those records.

## Official runtime

The separate `control-plane-mega` image installs a pinned official MEGAcmd
build and supplies `mega-ls`, `mega-mkdir`, `mega-find`, `mega-put`, and
`mega-get` on `PATH`. The package URL, version, architecture, and SHA-256 are
checked in `Dockerfile.mega`; upgrades go through an explicit image canary, not
MEGAcmd auto-update inside a running container.

Primary documentation:

- <https://github.com/meganz/MEGAcmd>
- <https://github.com/meganz/MEGAcmd/blob/master/UserGuide.md>
- <https://github.com/meganz/MEGAcmd/blob/master/contrib/docs/commands/put.md>
- <https://github.com/meganz/MEGAcmd/blob/master/contrib/docs/commands/find.md>
- <https://github.com/meganz/MEGAcmd/blob/master/contrib/docs/commands/get.md>

Build the runtime with:

```sh
docker build -f Dockerfile.mega -t gen-automation-control-plane-mega .
```

Never place the credential-bearing MEGAcmd profile in an image layer, build
cache, support bundle, or ordinary backup.

## One-time authentication

There is no separate MEGA application API key for this integration. MEGAcmd
stores a resumable authenticated session in its profile, so the account
password does not need to be stored by the application.

Recommended setup:

1. Create or choose the destination folder in the user's MEGA account.
2. Mount an encrypted persistent volume at
   `/run/gen-automation/mega-profile`, owned by service UID 10001 with mode
   `0700`. Its `.megaCmd` directory must have the same owner and mode.
3. In a one-time operator-controlled interactive session, set `HOME` to that
   directory and sign in with official MEGAcmd. Enter the password and any MFA
   code only in that secure prompt, never in chat, an environment file, process
   arguments, or shell history.
4. Enable HTTPS transfers (`mega-https on`), verify the session with
   `mega-whoami` and `mega-ls`, and create the configured remote root.
5. Restart the controller with the authenticated volume mounted. Do not run
   `mega-logout`; logout deletes the cached session.

A writable folder link plus its write auth-key can be bootstrapped instead when
least-privilege upload-only access is preferred. Enter both interactively and
keep the recovery copy in a password manager. A normal account session is more
convenient if future workflows need account-side folder/export management.

Treat the whole profile volume as a full-access secret: mount it only into one
uploader instance, encrypt deliberate backups separately, and revoke the MEGA
session or writable link if the volume is exposed. The child process receives a
minimal environment and no AWS, Salad, X, Patreon, or application secrets.

## Configuration

```dotenv
GEN_AUTOMATION_MEGA_DELIVERY_ENABLED=true
GEN_AUTOMATION_MEGA_PROFILE_HOME=/run/gen-automation/mega-profile
GEN_AUTOMATION_MEGA_REMOTE_ROOT=/AutomatedSets
GEN_AUTOMATION_BACKGROUND_MEGA_TIMEOUT_SECONDS=660
GEN_AUTOMATION_BACKGROUND_MEGA_COMMAND_TIMEOUT_SECONDS=300
GEN_AUTOMATION_BACKGROUND_MEGA_LEASE_SECONDS=900
GEN_AUTOMATION_BACKGROUND_MEGA_RETRY_BASE_SECONDS=300
GEN_AUTOMATION_BACKGROUND_MEGA_RETRY_MAX_SECONDS=3600
GEN_AUTOMATION_BACKGROUND_MEGA_BATCH_SIZE=100
GEN_AUTOMATION_BACKGROUND_MEGA_MAX_PACKAGE_BYTES=167772160
```

`BACKGROUND_MEGA_BATCH_SIZE` is the maximum number of images in one official
MEGAcmd upload command. The default 100 minimizes process overhead while
keeping restart work bounded. Do not run multiple upload processes against the
same profile; one active delivery per MEGA account lets MEGAcmd use its own
transfer connections efficiently.

`BACKGROUND_MEGA_MAX_PACKAGE_BYTES` is the maximum size of one source archive
part read from object storage. The default 160 MiB matches the finished-set
archive builder and bounds memory/disk use. It is not a MEGA ZIP size and does
not change the individual uploaded files.

No MEGA credential belongs in `.env`.

## Recovery and operating notes

- Profile, command, network, and source-storage failures use capped exponential
  backoff and remain restart-safe.
- A frozen-source contract violation, mismatched remote bytes, or duplicate
  expected filename fails closed for operator attention.
- The source object version, archive SHA-256, manifest SHA-256, individual
  image SHA-256, and byte lengths are checked before progress is recorded.
- Temporary files use a mode-`0700` directory and mode-`0600` files and are
  removed after every bounded cycle.
- MEGA is optional and is not probed during API startup. A missing profile only
  pauses MEGA delivery; generation, review, ZIP download, and other destinations
  remain usable.
- Public MEGA links are not created automatically. Finished folders remain
  private unless the owner deliberately enables a future sharing policy.
