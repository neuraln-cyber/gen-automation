# MEGA completed-set delivery

## Result

When a Patreon publication package becomes available, the controller now
automatically mirrors that exact ZIP to MEGA. The ZIP is the deterministic
finished set built from exactly one clean `full` output for every accepted
release selection. Patreon planning rejects a partial, additional, duplicated,
or reordered content set, so the downstream MEGA ZIP cannot silently omit an
accepted image. X teaser derivatives and X watermarks are not included.

The remote path is frozen when the delivery record is created:

```text
<remote-root>/<project-slug>/<release-slug>/<package-id>/<package-sha256>.zip
```

The SHA-256 filename makes response-loss recovery safe. Before every upload,
the controller looks for that exact path. If it exists, the controller
downloads the node by its opaque handle and verifies both byte length and
SHA-256. It adopts a match and does not upload again. After a new upload it
performs the same download verification before recording success.

The durable database reference contains:

- the immutable Patreon package ID;
- the MEGA remote path and opaque node handle;
- expected SHA-256 and byte length;
- attempt, lease, completion and verification timestamps;
- redacted failure codes.

It never contains a MEGA email, password, account session, folder decryption
key, or writable-link auth-key.

Authenticated publication readers can inspect status at:

```text
GET /api/v1/mega-deliveries/{delivery_id}
GET /api/v1/releases/{release_id}/mega-deliveries
```

## Official runtime

The pinned Debian 13 controller image currently uses official MEGAcmd
`2.5.2-1.1` (or a later explicitly canaried and re-pinned build).
MEGAcmd provides scriptable `mega-*` commands, uploads with `mega-put`, node
handles with `mega-find --print-only-handles`, and exact-handle downloads with
`mega-get`.

Primary documentation:

- <https://github.com/meganz/MEGAcmd>
- <https://github.com/meganz/MEGAcmd/blob/master/UserGuide.md>
- <https://github.com/meganz/MEGAcmd/blob/master/contrib/docs/commands/put.md>
- <https://github.com/meganz/MEGAcmd/blob/master/contrib/docs/commands/find.md>
- <https://github.com/meganz/MEGAcmd/blob/master/contrib/docs/commands/get.md>
- <https://github.com/meganz/MEGAcmd/blob/master/contrib/docs/commands/export.md>

The production controller image must install the pinned official MEGAcmd
package and provide `mega-ls`, `mega-mkdir`, `mega-find`, `mega-put`, and
`mega-get` on `PATH`. Record the package URL, version, and package SHA-256 in the
deployment manifest. Do not allow MEGAcmd to auto-update inside the immutable
container; upgrade it through the normal image canary.

The repository provides a separate image target so the ordinary control plane
does not carry the extra package:

```sh
docker build -f Dockerfile.mega \
  --build-arg MEGACMD_DEB_URL=https://mega.nz/linux/repo/Debian_13/amd64/megacmd_2.5.2-1.1_amd64.deb \
  --build-arg MEGACMD_DEB_SHA256=43907f450e13e712b61c87105eeab9c3568338c36895ad6de9599a3facf43659 \
  -t gen-automation-control-plane-mega .
```

The build accepts only HTTPS URLs below an official MEGA Linux repository and
fails if either argument is missing, the downloaded package digest differs, or
the `.deb` metadata is not package `megacmd`, version `2.5.2-1.1`, architecture
`amd64`. The package's post-install apt source and key are removed so the
immutable runtime cannot silently upgrade. For repeatable/offline production
recovery, retain this exact verified `.deb` privately under its SHA-256. This
Dockerfile still permits only the official URL; a future offline `COPY` target
must retain the same digest and metadata checks. Never place the
credential-bearing MEGAcmd profile in the package cache.

## Least-privilege account setup

Prefer a dedicated MEGA automation account with only the storage quota needed
for finished sets. A writable-folder export further limits the mounted profile
to one destination tree:

1. Create a dedicated destination folder in MEGA.
2. From an operator-controlled MEGAcmd session, create its writable export:

   ```text
   export -a --writable <destination-folder>
   ```

   MEGAcmd returns a folder URL and a separate write auth-key. Its official
   help states that login without the auth-key is read-only and login with it
   has write access.
3. Create a persistent private volume for the uploader profile. Mount it at,
   for example, `/run/gen-automation/mega-profile`, owned by service UID 10001
   with mode `0700`. The `.megaCmd` directory must have the same owner and mode.
4. In a one-time interactive deployment session, start `mega-cmd` with `HOME`
   set to that profile directory and log into the folder URL with its auth-key.
   Use the interactive shell so the write key is not placed in a process
   argument or shell history.
5. Verify the folder-link profile with `mega-ls /`, create the configured
   remote root, and restart the controller. Account-oriented `mega-whoami` is
   not a valid readiness test for a folder-link login. Do not log out:
   MEGAcmd logout deletes the cached session.
6. Remove the bootstrap URL/auth-key from the deployment shell and keep the
   recovery copy in the operator password manager, not application secrets.

MEGAcmd documents that its local cache contains enough session material to
access the account after restart. Therefore the whole profile volume is a
secret:

- mount it only into the controller/uploader;
- never include it in support bundles, ordinary backups, logs, or container
  layers;
- encrypt any deliberate backup separately;
- run one MEGAcmd server/profile writer at a time;
- revoke the writable export or MEGA session if the volume is exposed.

The application passes no login command and inherits no AWS, Salad, X, or
other application secrets into MEGAcmd child processes.

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
GEN_AUTOMATION_BACKGROUND_MEGA_MAX_PACKAGE_BYTES=167772160
```

MEGA delivery also requires object storage, the background controller,
publication orchestration, and a generated Patreon package. The delivery
lease must exceed the full cycle timeout. The cycle timeout must cover an
upload plus a full verification download. The MEGA-specific package limit
defaults to 160 MiB and has a hard configuration ceiling of 512 MiB.

No MEGA credential belongs in `.env`.

## Recovery and operating notes

- A timeout or lost `mega-put` response is treated as ambiguous. The next
  leased attempt always checks and hashes the remote node first.
- Recoverable profile, command, transport, and object-storage failures retry
  indefinitely with capped exponential backoff. They cannot become stranded
  merely because a fixed attempt count was reached. Only a frozen-package
  contract violation or conflicting bytes at the exact remote path is terminal.
- MEGA is optional and is not probed during API/controller startup. A missing
  or temporarily unavailable profile affects only the delivery loop and is
  retried there.
- A path with mismatched bytes or multiple nodes is failed as a conflict
  instead of being overwritten.
- Source bytes are fetched from the package's exact private object-storage
  version and checked before upload.
- The current object-store interface returns one bounded byte buffer. Delivery
  therefore holds up to the configured package limit in memory before writing
  the private temporary file; allow roughly twice that limit for transient
  Python copies plus normal process overhead. Keep the default at 160 MiB.
  A follow-up should add exact-version verified stream-to-temp support to the
  shared object-store contract before permitting packages above 512 MiB.
- Temporary package and verification files are created in a mode-`0700`
  directory and removed at the end of the attempt.
- The verification download doubles MEGA transfer bandwidth. This is
  intentional for end-to-end SHA-256 assurance and can be revisited only if
  MEGA exposes a cryptographically equivalent official remote checksum API.
- Public MEGA links are not automatically created. The stored node handle and
  remote path are private account references; use the destination folder's
  separately managed read link if a Patreon post needs one.
